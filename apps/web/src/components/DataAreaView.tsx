import { useEffect, useState } from 'react'
import { api } from '../api'
import { usePolling } from '../hooks/usePolling'
import { useApp } from '../store'
import type { AreaRead, ModbusArea, Server } from '../types'
import { Icon } from './Icon'
import { Modal } from './Modal'

interface AreaMeta {
  value: ModbusArea
  fc: string
  label: string
  cn: string
  bits: boolean
  refBase: number
}

const AREAS: AreaMeta[] = [
  { value: 'coil', fc: '01', label: 'Coils', cn: '线圈', bits: true, refBase: 0 },
  { value: 'discrete', fc: '02', label: 'Discrete Inputs', cn: '离散输入', bits: true, refBase: 100000 },
  { value: 'holding', fc: '03', label: 'Holding Registers', cn: '保持寄存器', bits: false, refBase: 400000 },
  { value: 'input', fc: '04', label: 'Input Registers', cn: '输入寄存器', bits: false, refBase: 300000 },
]

const PAGE_SIZE = 50

type Radix = 2 | 10 | 16

interface EditState {
  offset: number
  value: string
  radix: Radix
}

function areaMeta(area: ModbusArea): AreaMeta {
  return AREAS.find((item) => item.value === area) ?? AREAS[2]
}

/** Accept both 0-based offsets (100) and the usual 4xxxx/3xxxx/1xxxx refs. */
function parseStart(text: string, area: ModbusArea): number | null {
  const trimmed = text.trim()
  if (!/^\d+$/.test(trimmed)) return null
  const value = Number(trimmed)
  if (!Number.isSafeInteger(value)) return null

  if (area === 'holding' && value >= 400001 && value <= 465536) return value - 400001
  if (area === 'input' && value >= 300001 && value <= 365536) return value - 300001
  if (area === 'discrete' && value >= 100001 && value <= 165536) return value - 100001
  // Coil refs are usually written 000001..065536. A 6-digit value is
  // therefore a reference, while "100" keeps meaning offset 100.
  if (area === 'coil' && trimmed.length === 6 && value >= 1 && value <= 65536) {
    return value - 1
  }
  if (value < 0 || value > 65535) return null
  return value
}

function refText(meta: AreaMeta, offset: number): string {
  if (meta.value === 'coil') return String(offset + 1).padStart(6, '0')
  return String(meta.refBase + offset + 1)
}

function formatValue(value: number | boolean | undefined, radix: Radix, bits: boolean): string {
  if (value === undefined) return '—'
  const number = typeof value === 'boolean' ? (value ? 1 : 0) : Number(value)
  if (radix === 10) return String(number)
  if (radix === 2) {
    return '0b' + (bits ? number.toString(2) : (number & 0xffff).toString(2).padStart(16, '0'))
  }
  const hex = (bits ? number : number & 0xffff).toString(16).toUpperCase()
  return '0x' + (bits ? hex : hex.padStart(4, '0'))
}

function normalizeEntry(text: string, radix: Radix): string {
  const trimmed = text.trim()
  if (radix === 2) return trimmed.replace(/^0b/i, '')
  if (radix === 16) return trimmed.replace(/^0x/i, '')
  return trimmed
}

function parseEntry(text: string, radix: Radix): number | null {
  const body = normalizeEntry(text, radix)
  if (!body) return null
  if (radix === 2 && !/^[01]+$/.test(body)) return null
  if (radix === 10 && !/^\d+$/.test(body)) return null
  if (radix === 16 && !/^[0-9a-fA-F]+$/.test(body)) return null
  const value = parseInt(body, radix)
  return Number.isSafeInteger(value) ? value : null
}

function entryError(text: string, radix: Radix, bits: boolean): string | null {
  const body = normalizeEntry(text, radix)
  if (!body) return '请输入数值'
  if (radix === 2 && !/^[01]+$/.test(body)) return 'BIN 只能包含 0 和 1'
  if (radix === 10 && !/^\d+$/.test(body)) return 'DEC 只能包含数字'
  if (radix === 16 && !/^[0-9a-fA-F]+$/.test(body)) return 'HEX 只能包含 0–9 / A–F'
  const value = parseInt(body, radix)
  if (!Number.isSafeInteger(value)) return '数值超出范围'
  if (bits) return value === 0 || value === 1 ? null : '线圈 / 离散输入只能是 0 或 1'
  return value >= 0 && value <= 0xffff ? null : '寄存器是无符号 16 位，范围 0–65535'
}

function formatEntry(value: number, radix: Radix): string {
  if (radix === 10) return String(value)
  if (radix === 2) return value.toString(2)
  return value.toString(16).toUpperCase()
}

interface Props {
  server: Server
  running: boolean
}

export function DataAreaView({ server, running }: Props) {
  const notify = useApp((state) => state.notify)

  const [area, setArea] = useState<ModbusArea>('holding')
  const [startText, setStartText] = useState('0')
  const [countText, setCountText] = useState(String(PAGE_SIZE))
  const [radix, setRadix] = useState<Radix>(10)
  const [page, setPage] = useState(0)
  const [data, setData] = useState<AreaRead | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<EditState | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const meta = areaMeta(area)
  const start = parseStart(startText, area)
  const count = Number(countText)
  const countValid = Number.isInteger(count) && count >= 1 && count <= 1000
  const rangeError =
    start === null
      ? '起始地址需为 0–65535，或当前数据区的引用地址（如 400100）'
      : !countValid
        ? '长度需为 1–1000'
        : start + count > 65536
          ? `读取范围越界：${start} + ${count} > 65536`
          : null
  const valid = rangeError === null
  const pageCount = valid ? Math.max(1, Math.ceil(count / PAGE_SIZE)) : 1
  const safePage = Math.min(page, pageCount - 1)
  const pageStart = start === null ? 0 : start + safePage * PAGE_SIZE
  const pageLength = valid ? Math.min(PAGE_SIZE, count - safePage * PAGE_SIZE) : 0

  const load = async () => {
    if (!valid || start === null || pageLength <= 0) return
    try {
      const result = await api.area(server.id, area, pageStart, pageLength)
      setData(result)
      setError(null)
    } catch (loadError) {
      setData(null)
      setError(loadError instanceof Error ? loadError.message : String(loadError))
    }
  }

  useEffect(() => {
    if (running && valid) void load()
    // load is intentionally not a dependency: it changes with the query and
    // would otherwise re-create the interval on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [area, start, count, safePage, running])

  usePolling(() => void load(), 1000, running && valid)

  const stale = !data || data.area !== area || data.address !== pageStart || data.count !== pageLength
  const values = stale ? [] : data.values

  const switchArea = (next: ModbusArea) => {
    setArea(next)
    setData(null)
    setError(null)
    setPage(0)
    setEditing(null)
    setSaveError(null)
    if (parseStart(startText, next) === null) setStartText('0')
  }

  const editError = editing ? entryError(editing.value, editing.radix, meta.bits) : null
  const editNumber = editing && editError === null ? parseEntry(editing.value, editing.radix) : null

  const openEditor = (offset: number, value: number | boolean | undefined) => {
    if (!running) return
    const number = typeof value === 'boolean' ? (value ? 1 : 0) : typeof value === 'number' ? value : 0
    setSaveError(null)
    setEditing({ offset, value: formatEntry(number, radix), radix })
  }

  const closeEditor = () => {
    setEditing(null)
    setSaveError(null)
  }

  const switchEditRadix = (next: Radix) => {
    setEditing((current) => {
      if (!current) return current
      const number = parseEntry(current.value, current.radix)
      return {
        ...current,
        radix: next,
        value: number === null ? '' : formatEntry(number, next),
      }
    })
  }

  const saveEntry = async () => {
    if (!editing || editNumber === null || editError !== null) return
    setSaving(true)
    setSaveError(null)
    try {
      await api.writeArea(server.id, area, editing.offset, editNumber)
      notify('ok', `${refText(meta, editing.offset)} ← ${editNumber}`)
      setEditing(null)
      await load()
    } catch (writeError) {
      setSaveError(writeError instanceof Error ? writeError.message : String(writeError))
    } finally {
      setSaving(false)
    }
  }

  const currentRaw = editing ? values[editing.offset - pageStart] : undefined
  const currentText =
    editing === null
      ? '—'
      : typeof currentRaw === 'boolean'
        ? currentRaw
          ? '1'
          : '0'
        : typeof currentRaw === 'number'
          ? String(currentRaw)
          : '—'

  return (
    <div className="area-view">
      <div className="area-toolbar">
        <div className="field area-picker-field">
          <span>数据区 / 功能码</span>
          <div className="area-picker" role="group" aria-label="数据区">
            {AREAS.map((item) => (
              <button
                key={item.value}
                type="button"
                className={`btn tiny${area === item.value ? ' on' : ''}`}
                title={`${item.fc} ${item.label} · ${item.cn}`}
                onClick={() => switchArea(item.value)}
              >
                <b>{item.fc}</b> {item.cn}
              </button>
            ))}
          </div>
        </div>

        <label className="field">
          <span>起始地址</span>
          <input
            className="input mono"
            value={startText}
            inputMode="numeric"
            placeholder="0 或 400001"
            onChange={(event) => {
              setStartText(event.target.value)
              setPage(0)
            }}
          />
        </label>

        <label className="field small-field">
          <span>长度</span>
          <input
            className="input mono"
            type="number"
            min={1}
            max={1000}
            value={countText}
            onChange={(event) => {
              setCountText(event.target.value)
              setPage(0)
            }}
          />
        </label>

        <div className="field">
          <span>数据格式</span>
          <div className="radix-picker" role="group" aria-label="数据格式">
            {([2, 10, 16] as Radix[]).map((item) => (
              <button
                key={item}
                type="button"
                className={`btn tiny${radix === item ? ' on' : ''}`}
                onClick={() => setRadix(item)}
              >
                {item === 2 ? 'BIN' : item === 10 ? 'DEC' : 'HEX'}
              </button>
            ))}
          </div>
        </div>

        <button type="button" className="btn ghost area-refresh" onClick={() => void load()}>
          <Icon name="restart" size={15} /> 刷新
        </button>
      </div>

      <div className="area-summary">
        <span className="mono">
          {meta.fc} · unit {data?.unit_id ?? server.options.unit_id}
        </span>
        {valid ? (
          <span className="mono">
            偏移 {start}–{(start as number) + count - 1} · 引用 {refText(meta, start as number)}–
            {refText(meta, (start as number) + count - 1)}
          </span>
        ) : null}
        {valid && count > PAGE_SIZE ? (
          <span className="mono">
            第 {safePage + 1}/{pageCount} 页 · 当前 {refText(meta, pageStart)}–
            {refText(meta, pageStart + pageLength - 1)}
          </span>
        ) : null}
        <span className="muted">实时刷新 1s · 双击单元格写入 · 空白地址默认 0</span>
      </div>

      {rangeError ? (
        <div className="error-box">
          <Icon name="alert" size={16} /> {rangeError}
        </div>
      ) : null}

      {error ? (
        <div className="error-box">
          <Icon name="alert" size={16} /> {error}
        </div>
      ) : null}

      {!running ? (
        <div className="notice">
          <Icon name="alert" size={15} /> 实例未运行，启动或重启后才能读取数据区。
        </div>
      ) : null}

      {valid ? (
        <div className="area-grid">
          {Array.from({ length: pageLength }, (_, index) => {
            const offset = pageStart + index
            return (
              <div
                className={`area-cell${running ? ' editable' : ''}`}
                key={offset}
                title={running ? `偏移 ${offset} · 双击写入` : `偏移 ${offset}`}
                role={running ? 'button' : undefined}
                tabIndex={running ? 0 : undefined}
                onDoubleClick={() => openEditor(offset, values[index])}
                onKeyDown={(event) => {
                  if (!running || (event.key !== 'Enter' && event.key !== ' ')) return
                  event.preventDefault()
                  openEditor(offset, values[index])
                }}
              >
                <div className="area-cell-top">
                  <span className="area-ref mono">
                    {refText(meta, offset)}
                    <span className="area-offset"> +{offset}</span>
                  </span>
                  {running ? <Icon name="pencil" size={11} className="area-edit-icon" /> : null}
                </div>
                <div className={`area-cell-value mono${meta.bits ? ' bit' : ''}`}>
                  {formatValue(values[index], radix, meta.bits)}
                </div>
              </div>
            )
          })}
        </div>
      ) : null}

      {valid && count > PAGE_SIZE ? (
        <div className="area-pager">
          <button
            type="button"
            className="btn tiny"
            disabled={safePage === 0}
            onClick={() => setPage(safePage - 1)}
          >
            <Icon name="chevronLeft" size={14} /> 上一页
          </button>
          <span className="mono">
            第 {safePage + 1} / {pageCount} 页 · 每页 {PAGE_SIZE}
          </span>
          <button
            type="button"
            className="btn tiny"
            disabled={safePage >= pageCount - 1}
            onClick={() => setPage(safePage + 1)}
          >
            下一页 <Icon name="chevronRight" size={14} />
          </button>
        </div>
      ) : null}

      <Modal
        open={editing !== null}
        title={editing ? `写入 ${meta.cn} ${refText(meta, editing.offset)}` : '写入数据区'}
        subtitle={
          editing
            ? `FC${meta.fc} · 偏移 ${editing.offset} · 保存后立即对已连接的 client 生效`
            : undefined
        }
        width={460}
        onClose={closeEditor}
        footer={
          <>
            <button type="button" className="btn ghost" onClick={closeEditor}>
              取消
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={saving || editError !== null || editNumber === null}
              onClick={() => void saveEntry()}
            >
              {saving ? '保存中…' : '保存'}
            </button>
          </>
        }
      >
        {editing ? (
          <>
            <div className="area-edit-now">
              <span>当前值</span>
              <strong className="mono">{currentText}</strong>
              <small className="mono">
                {typeof currentRaw === 'number' || typeof currentRaw === 'boolean'
                  ? `${formatValue(currentRaw, 2, meta.bits)} · ${formatValue(currentRaw, 16, meta.bits)}`
                  : ''}
              </small>
            </div>

            <div className="field">
              <span>输入进制</span>
              <div className="radix-picker" role="group" aria-label="输入进制">
                {([2, 10, 16] as Radix[]).map((item) => (
                  <button
                    key={item}
                    type="button"
                    className={`btn tiny${editing.radix === item ? ' on' : ''}`}
                    onClick={() => switchEditRadix(item)}
                  >
                    {item === 2 ? 'BIN' : item === 10 ? 'DEC' : 'HEX'}
                  </button>
                ))}
              </div>
            </div>

            <label className="field">
              <span>新值</span>
              <div className={`area-entry${editError ? ' invalid' : ''}`}>
                <span className="area-entry-prefix mono">
                  {editing.radix === 2 ? '0b' : editing.radix === 16 ? '0x' : ''}
                </span>
                <input
                  className="input mono"
                  autoFocus
                  value={editing.value}
                  placeholder={meta.bits ? '0 或 1' : '0 – 65535'}
                  onChange={(event) =>
                    setEditing((current) => (current ? { ...current, value: event.target.value } : current))
                  }
                  onKeyDown={(event) => {
                    if (event.key !== 'Enter') return
                    event.preventDefault()
                    void saveEntry()
                  }}
                />
              </div>
              <small className="hint">
                {meta.bits
                  ? '0 = OFF，1 = ON'
                  : '无符号 16 位寄存器，范围 0–65535；写入会覆盖该地址原来的值。'}
              </small>
            </label>

            {editError ? <p className="area-entry-error">{editError}</p> : null}

            {area === 'input' || area === 'discrete' ? (
              <div className="notice">
                <Icon name="alert" size={15} />
                <span>
                  {meta.cn}对 Modbus client 是只读区（FC{meta.fc}），这里的写入是模拟器侧预置值，
                  client 读取时能看到。
                </span>
              </div>
            ) : null}

            {saveError ? (
              <div className="error-box">
                <Icon name="alert" size={16} /> {saveError}
              </div>
            ) : null}
          </>
        ) : null}
      </Modal>
    </div>
  )
}
