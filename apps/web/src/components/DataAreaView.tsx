import { useEffect, useState } from 'react'
import { api } from '../api'
import { usePolling } from '../hooks/usePolling'
import { useApp } from '../store'
import { isLink, type AreaRead, type ModbusArea, type Server } from '../types'
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
const AREA_STATE_PREFIX = 'poly.area.'

type Radix = 2 | 10 | 16

interface AreaState {
  area: ModbusArea
  startText: string
  countText: string
  radix: Radix
  page: number
}

/**
 * Survive the tab switch. The data area is unmounted when the user opens the
 * connection/log tab, so its start address has to live somewhere outside React
 * state. sessionStorage keeps it for this browser tab without making a stale
 * address permanent after a restart.
 */
function readAreaState(serverId: string): Partial<AreaState> {
  try {
    const raw = sessionStorage.getItem(`${AREA_STATE_PREFIX}${serverId}`)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return {}
    const state = parsed as Partial<AreaState>
    return {
      area: AREAS.some((item) => item.value === state.area) ? state.area : undefined,
      startText: typeof state.startText === 'string' ? state.startText : undefined,
      countText: typeof state.countText === 'string' ? state.countText : undefined,
      radix:
        state.radix === 2 || state.radix === 10 || state.radix === 16
          ? state.radix
          : undefined,
      page:
        typeof state.page === 'number' && Number.isInteger(state.page) && state.page >= 0
          ? state.page
          : undefined,
    }
  } catch {
    return {}
  }
}

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
  const loadServer = useApp((state) => state.loadServer)
  // A write on a link instance lands on somebody else's machine, with no undo.
  // So it gets an explicit "yes, I mean the real device" checkbox, and the
  // areas a real Modbus server refuses to take are not offered at all.
  const link = isLink(server)
  const readOnly = (area: ModbusArea) => link && (area === 'input' || area === 'discrete')
  // A sim instance may be configured to expose only part of the 16-bit space
  // (so out-of-range reads answer ILLEGAL_ADDRESS, like a real device). A link
  // has no such setting: its map belongs to the far side, so we ask and let it
  // answer.
  const limit = link ? 65536 : (server.options.register_count ?? 65536)

  const [initial] = useState(() => readAreaState(server.id))
  const [area, setArea] = useState<ModbusArea>(initial.area ?? 'holding')
  const [startText, setStartText] = useState(initial.startText ?? '0')
  const [countText, setCountText] = useState(
    initial.countText ?? String(Math.min(PAGE_SIZE, limit)),
  )
  const [radix, setRadix] = useState<Radix>(initial.radix ?? 10)
  const [page, setPage] = useState(initial.page ?? 0)
  const [data, setData] = useState<AreaRead | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<EditState | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [confirmed, setConfirmed] = useState(false)

  useEffect(() => {
    try {
      const state: AreaState = { area, startText, countText, radix, page }
      sessionStorage.setItem(`${AREA_STATE_PREFIX}${server.id}`, JSON.stringify(state))
    } catch {
      // Private mode / full storage: losing a UI preference is not worth an error.
    }
  }, [server.id, area, startText, countText, radix, page])

  const meta = areaMeta(area)
  const start = parseStart(startText, area)
  const count = Number(countText)
  const countValid = Number.isInteger(count) && count >= 1 && count <= 1000
  const rangeError =
    start === null
      ? '起始地址需为 0–65535，或当前数据区的引用地址（如 400100）'
      : !countValid
        ? '长度需为 1–1000'
        : start + count > limit
          ? `超出本实例的有效范围：只暴露 ${limit} 个地址（0–${limit - 1}），当前 ${start} + ${count} = ${start + count}`
          : null
  const valid = rangeError === null
  const pageCount = valid ? Math.max(1, Math.ceil(count / PAGE_SIZE)) : 1
  const safePage = Math.min(page, pageCount - 1)
  const pageStart = start === null ? 0 : start + safePage * PAGE_SIZE
  const pageLength = valid ? Math.min(PAGE_SIZE, count - safePage * PAGE_SIZE) : 0

  const load = async () => {
    // A disabled instance is a desired state, not a failure. Refuse before
    // asking: the API would answer 409 anyway, and asking first is what used
    // to reconnect a stopped link (the client re-opens a closed socket on its
    // own, so "just try it" is not harmless).
    if (!running) {
      setData(null)
      setError(
        server.enabled
          ? '实例未运行，请先启动或重启后再刷新。'
          : '实例已停用，请先启用后再刷新数据区。',
      )
      return
    }
    if (!valid || start === null || pageLength <= 0) return
    try {
      const result = await api.area(server.id, area, pageStart, pageLength)
      setData(result)
      setError(null)
    } catch (loadError) {
      setData(null)
      const message = loadError instanceof Error ? loadError.message : String(loadError)
      setError(message)
      // The far side refused because the instance is off, which means the page
      // is showing a stale enabled state (disabled from the overview, say).
      // Re-read the instance so the banner and the buttons agree with reality,
      // and say it out loud rather than letting old numbers sit on screen.
      if (/已停用/.test(message)) {
        notify('error', message)
        void loadServer(server.id, true)
      }
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
    setConfirmed(false)
    if (parseStart(startText, next) === null) setStartText('0')
  }

  const editError = editing ? entryError(editing.value, editing.radix, meta.bits) : null
  const editNumber = editing && editError === null ? parseEntry(editing.value, editing.radix) : null

  const openEditor = (offset: number, value: number | boolean | undefined) => {
    if (!running) return
    // Not an error dialog: the far side simply will not take a write there, so
    // the cell says so instead of failing after the fact.
    if (readOnly(area)) return
    const number = typeof value === 'boolean' ? (value ? 1 : 0) : typeof value === 'number' ? value : 0
    setSaveError(null)
    setConfirmed(false)
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
    if (link && !confirmed) return
    setSaving(true)
    setSaveError(null)
    try {
      await api.writeArea(server.id, area, editing.offset, editNumber)
      const address = refText(meta, editing.offset)
      notify(
        'ok',
        link
          ? `已写入 ${server.target_host}:${server.target_port ?? server.port} · ${meta.cn} · 地址 ${address} · 新值 ${editNumber}`
          : `已写入 ${meta.cn} · 地址 ${address} · 新值 ${editNumber}`,
      )
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
                className={`btn tiny${area === item.value ? ' on' : ''}${readOnly(item.value) ? ' read-only' : ''}`}
                title={
                  readOnly(item.value)
                    ? `${item.fc} ${item.label} · 对 Modbus client 是只读区，连接实例不能写入`
                    : `${item.fc} ${item.label} · ${item.cn}`
                }
                onClick={() => switchArea(item.value)}
              >
                <b>{item.fc}</b> {item.cn}
                {readOnly(item.value) ? <span className="ro-flag">只读</span> : null}
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

        <button
          type="button"
          className="btn ghost area-refresh"
          onClick={() => void load()}
          disabled={!running}
          title={
            running
              ? '立即刷新当前数据区'
              : server.enabled
                ? '实例未运行，请先启动或重启'
                : '实例已停用，请先启用后再刷新'
          }
        >
          <Icon name="restart" size={15} /> 刷新
        </button>
      </div>

      <div className="area-summary">
        <span className="mono">
          {meta.fc} · unit {data?.unit_id ?? (link ? (server.target_options?.unit_id ?? 1) : server.options.unit_id)}
        </span>
        {valid ? (
          <span className="mono">
            偏移 {start}–{(start as number) + count - 1} · 引用 {refText(meta, start as number)}–
            {refText(meta, (start as number) + count - 1)}
            {limit < 65536 ? ` · 有效 0–${limit - 1}` : ''}
          </span>
        ) : null}
        {valid && count > PAGE_SIZE ? (
          <span className="mono">
            第 {safePage + 1}/{pageCount} 页 · 当前 {refText(meta, pageStart)}–
            {refText(meta, pageStart + pageLength - 1)}
          </span>
        ) : null}
        <span className="muted">
          {link
            ? readOnly(area)
              ? '连接实例 · 该区对真实 server 只读，不能写入'
              : '实时刷新 1s · 双击单元格写入（直接落到对方设备，需二次确认）'
            : '实时刷新 1s · 双击单元格写入 · 空白地址默认 0'}
        </span>
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
        // A full-width block, not a line of small print: this is the one state
        // where showing stale numbers would be actively misleading, so the
        // panel says why. The control that fixes it is the power button in the
        // page header - one place for every tab, instead of one per panel.
        <div className={`area-blocked${server.enabled ? '' : ' is-disabled'}`}>
          <span className="area-blocked-icon">
            <Icon name={server.enabled ? 'alert' : 'power'} size={20} />
          </span>
          <div className="area-blocked-copy">
            <strong>{server.enabled ? '实例未在运行' : '实例已停用'}</strong>
            <p>
              {server.enabled
                ? '进程没起来或已退出，启动 / 重启后这里才会显示真实数据。'
                : link
                  ? `已断开到 ${server.target_host}:${server.target_port ?? server.port} 的连接。停用不会删除实例，点右上角「启用连接」后会再次连接。`
                  : '进程已停止、端口已释放。停用不会删除实例，点右上角「启用」后会重新监听。'}
            </p>
          </div>
        </div>
      ) : null}

      {link && readOnly(area) ? (
        <div className="notice">
          <Icon name="alert" size={15} />{' '}
          {meta.cn}对 Modbus client 是只读区（FC{meta.fc}），真实 server 不会接受写入，
          所以连接实例不提供写入入口；保持寄存器和线圈可以正常写。
        </div>
      ) : null}

      {/* While the instance is off, no grid at all: an empty grid of zeros
          reads like "the device says everything is 0", which is a lie. */}
      {valid && running ? (
        <div className="area-grid">
          {Array.from({ length: pageLength }, (_, index) => {
            const offset = pageStart + index
            const writable = running && !readOnly(area)
            return (
              <div
                className={`area-cell${writable ? ' editable' : ''}${readOnly(area) ? ' read-only' : ''}`}
                key={offset}
                title={
                  readOnly(area)
                    ? `偏移 ${offset} · 只读区，连接实例不能写入`
                    : writable
                      ? `偏移 ${offset} · 双击写入`
                      : `偏移 ${offset}`
                }
                role={writable ? 'button' : undefined}
                tabIndex={writable ? 0 : undefined}
                onDoubleClick={() => openEditor(offset, values[index])}
                onKeyDown={(event) => {
                  if (!writable || (event.key !== 'Enter' && event.key !== ' ')) return
                  event.preventDefault()
                  openEditor(offset, values[index])
                }}
              >
                <div className="area-cell-top">
                  <span className="area-ref mono">
                    {refText(meta, offset)}
                    <span className="area-offset"> +{offset}</span>
                  </span>
                  {writable ? <Icon name="pencil" size={11} className="area-edit-icon" /> : null}
                </div>
                <div className={`area-cell-value mono${meta.bits ? ' bit' : ''}`}>
                  {formatValue(values[index], radix, meta.bits)}
                </div>
              </div>
            )
          })}
        </div>
      ) : null}

      {valid && running && count > PAGE_SIZE ? (
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
        title={editing ? `写入${meta.cn}` : '写入数据区'}
        subtitle={
          editing
            ? link
              ? `地址 ${refText(meta, editing.offset)} · 偏移 ${editing.offset} · FC${meta.fc} · 将写入 ${server.target_host}:${server.target_port ?? server.port}，立即生效且无法撤销`
              : `地址 ${refText(meta, editing.offset)} · 偏移 ${editing.offset} · FC${meta.fc} · 保存后立即对已连接的 client 生效`
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
              disabled={saving || editError !== null || editNumber === null || (link && !confirmed)}
              onClick={() => void saveEntry()}
            >
              {saving ? '保存中…' : link ? '确认写入' : '保存'}
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

            {link ? (
              <label className="check-row link-confirm">
                <input
                  type="checkbox"
                  className="check"
                  checked={confirmed}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                <span>
                  我确认写入外部设备
                  <small className="hint">
                    这个值会直接写到 <span className="mono">
                      {server.target_host}:{server.target_port ?? server.port}
                    </span>{' '}
                    的真实设备上，没有撤销。
                  </small>
                </span>
              </label>
            ) : area === 'input' || area === 'discrete' ? (
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
