import { useState } from 'react'
import { useApp } from '../store'
import { formatAddress, formatValue, type Server, type Tag } from '../types'
import { Icon } from './Icon'

const INT_TYPES = new Set(['int16', 'uint16', 'int32', 'uint32'])

export function ValueTable({ server, values }: { server: Server; values: Record<string, unknown> }) {
  const write = useApp((state) => state.write)
  const notify = useApp((state) => state.notify)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [pending, setPending] = useState<string | null>(null)

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
    setPending(tag.name)
    const ok = await write(server.id, tag.name, value)
    setPending(null)
    if (ok && tag.sim.mode !== 'static') {
      notify('info', `${tag.name} 由 ${tag.sim.mode} 仿真驱动，写入会在下个周期被覆盖`)
    }
    setDrafts((current) => ({ ...current, [tag.name]: '' }))
  }

  const toggle = async (tag: Tag, value: unknown) => {
    const ok = await write(server.id, tag.name, !value)
    if (ok && tag.sim.mode !== 'static') {
      notify('info', `${tag.name} 由 ${tag.sim.mode} 仿真驱动，写入会被覆盖`)
    }
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
                    {tag.sim.mode !== 'static' ? (
                      <span className="chip sim" title="由仿真引擎驱动，外部写入会被覆盖">
                        {tag.sim.mode}
                      </span>
                    ) : null}
                  </div>
                  {tag.description ? <small className="tag-desc">{tag.description}</small> : null}
                </td>
                <td className="mono muted">{formatAddress(tag, server.protocol)}</td>
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
                还没有变量，去「标签表」添加。
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  )
}
