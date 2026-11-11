import { useState } from 'react'
import { useApp } from '../store'
import { formatAddress, formatValue, isLink, type Server, type Tag } from '../types'
import { Icon } from './Icon'
import { Modal } from './Modal'

const INT_TYPES = new Set(['int16', 'uint16', 'int32', 'uint32'])

export function ValueTable({ server, values }: { server: Server; values: Record<string, unknown> }) {
  const write = useApp((state) => state.write)
  const notify = useApp((state) => state.notify)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [pending, setPending] = useState<string | null>(null)
  // A write to a link lands on a real device with no undo, so it goes through
  // the same confirm the data-area editor uses. The tag table is the S7 link's
  // only view of the far side, which makes this the write path that matters.
  const link = isLink(server)
  const [confirming, setConfirming] = useState<{ tag: Tag; value: unknown } | null>(null)
  const [confirmed, setConfirmed] = useState(false)

  const perform = async (tag: Tag, value: unknown) => {
    setPending(tag.name)
    const ok = await write(server.id, tag.name, value)
    setPending(null)
    // The simulation note describes *our* engine. A link has none - the far
    // side decides when a value changes, and it never overwrites our write.
    if (ok && !link && tag.sim.mode !== 'static') {
      notify('info', `${tag.name} 由 ${tag.sim.mode} 仿真驱动，写入会在下个周期被覆盖`)
    }
    setDrafts((current) => ({ ...current, [tag.name]: '' }))
  }

  const askOrWrite = (tag: Tag, value: unknown) => {
    if (!link) {
      void perform(tag, value)
      return
    }
    setConfirmed(false)
    setConfirming({ tag, value })
  }

  const submit = async (tag: Tag) => {
    const raw = (drafts[tag.name] ?? '').trim()
    if (!raw) {
      notify('info', '先填写要写入的值')
      return
    }
    let value: unknown = raw
    if (tag.data_type === 'bool') value = raw === '1' || raw.toLowerCase() === 'true'
    else if (INT_TYPES.has(tag.data_type)) value = Math.trunc(Number(raw))
    else value = Number(raw)
    if (typeof value === 'number' && Number.isNaN(value)) {
      notify('error', `${raw} 不是合法数值`)
      return
    }
    askOrWrite(tag, value)
  }

  const toggle = async (tag: Tag, value: unknown) => {
    askOrWrite(tag, !value)
  }

  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>变量</th>
            <th>地址</th>
            <th>类型</th>
            <th>当前值</th>
            <th className="write-col">写入</th>
          </tr>
        </thead>
        <tbody>
          {server.tags.map((tag) => {
            const value = values[tag.name]
            return (
              <tr key={tag.name}>
                <td>
                  <div className="tag-name">
                    <span className="mono">{tag.name}</span>
                    {!tag.writable ? <span className="chip">只读</span> : null}
                    {tag.sim.mode !== 'static' && !link ? (
                      <span className="chip sim" title="由仿真引擎驱动，外部写入会被覆盖">
                        {tag.sim.mode}
                      </span>
                    ) : null}
                  </div>
                  {tag.description ? <small className="tag-desc">{tag.description}</small> : null}
                </td>
                <td className="mono muted">
                  {formatAddress(
                    tag,
                    server.protocol,
                    // Only a link needs the far side's namespace index: a sim
                    // names its own variables and owns the namespace.
                    link ? (server.target_options?.namespace_index ?? 2) : undefined,
                  )}
                </td>
                <td className="mono muted">{tag.data_type}</td>
                <td>
                  <span className={`value${tag.data_type === 'bool' ? ' bool' : ''}`}>
                    {formatValue(value, tag)}
                  </span>
                </td>
                <td>
                  {tag.writable ? (
                    tag.data_type === 'bool' ? (
                      <button
                        type="button"
                        className={`btn tiny${value ? ' on' : ''}`}
                        disabled={pending === tag.name}
                        onClick={() => void toggle(tag, value)}
                      >
                        <Icon name="bolt" size={13} /> 切换
                      </button>
                    ) : (
                      <div className="write-cell">
                        <input
                          className="input mono compact"
                          inputMode="decimal"
                          placeholder="值"
                          value={drafts[tag.name] ?? ''}
                          onChange={(event) =>
                            setDrafts((current) => ({ ...current, [tag.name]: event.target.value }))
                          }
                          onKeyDown={(event) => {
                            if (event.key === 'Enter') void submit(tag)
                          }}
                        />
                        <button
                          type="button"
                          className="icon-btn"
                          title="写入"
                          disabled={pending === tag.name}
                          onClick={() => void submit(tag)}
                        >
                          <Icon name="check" size={15} />
                        </button>
                      </div>
                    )
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            )
          })}
          {server.tags.length === 0 ? (
            <tr>
              <td colSpan={5} className="table-empty">
                {link
                  ? server.protocol === 'opcua'
                    ? '未定义变量，地址空间为空。去「标签表」添加对方的真实 NodeId。'
                    : '未定义变量，地址空间为空。去「标签表」添加对方的真实地址。'
                  : '未定义变量，地址空间为空。去「标签表」添加变量。'}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>

      <Modal
        open={confirming !== null}
        title="写入外部设备"
        subtitle="这个值会直接写到那台机器的真实地址上，没有撤销。"
        onClose={() => setConfirming(null)}
        footer={
          <>
            <button type="button" className="btn ghost" onClick={() => setConfirming(null)}>
              取消
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={!confirmed || pending !== null}
              onClick={() => {
                if (!confirming) return
                const { tag, value } = confirming
                setConfirming(null)
                void perform(tag, value)
              }}
            >
              写入
            </button>
          </>
        }
      >
        {confirming ? (
          <>
            <div className="area-edit-now">
              <span>变量</span>
              <strong className="mono">{confirming.tag.name}</strong>
              <small className="mono">
                新值 {formatValue(confirming.value, confirming.tag)} ·{' '}
                {formatAddress(
                  confirming.tag,
                  server.protocol,
                  link ? (server.target_options?.namespace_index ?? 2) : undefined,
                )}
              </small>
            </div>
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
                  这个值会直接写到{' '}
                  <span className="mono">
                    {server.target_host}:{server.target_port ?? server.port}
                  </span>{' '}
                  的真实设备上，没有撤销。
                </small>
              </span>
            </label>
          </>
        ) : null}
      </Modal>
    </div>
  )
}
