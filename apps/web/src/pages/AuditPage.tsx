import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { Icon } from '../components/Icon'
import { usePolling } from '../hooks/usePolling'
import { useApp } from '../store'
import type { AuditEntry } from '../types'

const PAGE_SIZE = 50

const ACTION_LABELS: Record<string, string> = {
  'site.create': '创建 Site',
  'site.update': '更新 Site',
  'site.delete': '删除 Site',
  'site.probe': '探测 Site',
  'server.create': '创建实例',
  'server.update': '更新实例',
  'server.delete': '删除实例',
  'server.restart': '重启实例',
  'server.reset': '重置实例',
  'server.client_kick': '剔除客户端',
  'tags.replace': '替换标签表',
  'area.write': '写入数据区',
  'tag.write': '写入标签',
  'user.create': '创建用户',
  'user.delete': '删除用户',
  'user.password_change': '修改密码',
  'user.login': '登录',
  'user.logout': '退出登录',
  'maintenance.kill': '清理残留进程',
}

const ACTION_OPTIONS = Object.entries(ACTION_LABELS).sort((a, b) =>
  a[1].localeCompare(b[1], 'zh'),
)

function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action
}

/** One readable line for the params cell; the full JSON stays one click away. */
function summarizeParams(entry: AuditEntry): string {
  const params = entry.params ?? {}
  const changed = params.changed
  if (changed !== null && typeof changed === 'object' && !Array.isArray(changed)) {
    const parts = Object.entries(changed as Record<string, unknown>)
      .filter((entry): entry is [string, [unknown, unknown]] => {
        const value = entry[1]
        return Array.isArray(value) && value.length === 2
      })
      .map(([key, value]) => `${key}: ${String(value[0])} → ${String(value[1])}`)
    if (parts.length) return parts.join('，')
  }
  if (entry.action === 'area.write') {
    return `area: ${String(params.area)} ${String(params.address)} = ${String(params.value)}`
  }
  const parts = Object.entries(params)
    .filter(([, value]) => value === null || typeof value !== 'object')
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${String(value)}`)
  return parts.join('，')
}

export function AuditPage() {
  const notify = useApp((state) => state.notify)
  const [actor, setActor] = useState('')
  const [action, setAction] = useState('')
  const [q, setQ] = useState('')
  const [items, setItems] = useState<AuditEntry[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadFirst = async () => {
    try {
      const res = await api.audit({
        actor: actor.trim() || undefined,
        action: action || undefined,
        q: q.trim() || undefined,
        limit: PAGE_SIZE,
        offset: 0,
      })
      setItems(res.items)
      setTotal(res.total)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  // Debounce the text filters so typing does not fire a request per keystroke.
  useEffect(() => {
    const timer = window.setTimeout(() => void loadFirst(), 300)
    return () => window.clearTimeout(timer)
    // loadFirst closes over the current filters; the deps below are what change them.
  }, [actor, action, q])

  // Refresh the first page every 5s. Once the user has loaded more, the poll
  // stands down - otherwise it would collapse the list back to page one.
  usePolling(() => {
    if (items.length <= PAGE_SIZE) void loadFirst()
  }, 5000, true)

  const loadMore = async () => {
    setLoadingMore(true)
    try {
      const res = await api.audit({
        actor: actor.trim() || undefined,
        action: action || undefined,
        q: q.trim() || undefined,
        limit: PAGE_SIZE,
        offset: items.length,
      })
      setItems((prev) => [...prev, ...res.items])
      setTotal(res.total)
    } catch (err) {
      notify('error', err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingMore(false)
    }
  }

  const hasMore = items.length < total

  return (
    <div className="page">
      <nav className="crumbs">
        <Link to="/" className="crumb-link">
          <Icon name="arrowLeft" size={14} /> 全部 Sites
        </Link>
      </nav>

      <header className="page-head">
        <div>
          <span className="eyebrow">
            <Icon name="clock" size={14} /> 审计
          </span>
          <h1>审计记录</h1>
          <p>谁在什么时候对什么做了什么。仅管理员可查看，每 5 秒刷新。</p>
        </div>
        <div className="head-actions">
          <button type="button" className="btn ghost" onClick={() => void loadFirst()}>
            <Icon name="restart" size={15} /> {loading ? '刷新中…' : '刷新'}
          </button>
        </div>
      </header>

      <div className="instance-filters">
        <label className="instance-search">
          <Icon name="search" size={15} />
          <input
            value={actor}
            aria-label="操作人"
            placeholder="操作人"
            onChange={(event) => setActor(event.target.value)}
          />
        </label>
        <select
          className="audit-action-select"
          value={action}
          aria-label="动作"
          onChange={(event) => setAction(event.target.value)}
        >
          <option value="">全部动作</option>
          {ACTION_OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <label className="instance-search">
          <Icon name="search" size={15} />
          <input
            value={q}
            aria-label="对象或详情"
            placeholder="对象或详情"
            onChange={(event) => setQ(event.target.value)}
          />
        </label>
      </div>

      {error ? (
        <div className="error-box">
          <Icon name="alert" size={16} /> {error}
        </div>
      ) : null}

      <div className="table-wrap">
        <table className="data-table audit-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>操作人</th>
              <th>动作</th>
              <th>对象</th>
              <th>详情</th>
              <th>参数</th>
            </tr>
          </thead>
          <tbody>
            {loading && items.length === 0 ? (
              <tr>
                <td colSpan={6} className="table-empty">
                  正在加载审计记录…
                </td>
              </tr>
            ) : null}

            {!loading && items.length === 0 ? (
              <tr>
                <td colSpan={6} className="table-empty">
                  没有匹配的审计记录
                </td>
              </tr>
            ) : null}

            {items.map((entry) => {
              const summary = summarizeParams(entry)
              return (
                <tr key={entry.id}>
                  <td className="audit-time mono">
                    {new Date(entry.created_at).toLocaleString()}
                  </td>
                  <td className="mono">{entry.actor || '—'}</td>
                  <td className="audit-action-cell">
                    <span className="audit-action">{actionLabel(entry.action)}</span>
                    {entry.params?.source === 'ai' ? (
                      <span className="audit-ai" title="由 AI 发起">
                        AI
                      </span>
                    ) : null}
                  </td>
                  <td className="audit-target mono">{entry.target || '—'}</td>
                  <td className="audit-detail">{entry.detail || '—'}</td>
                  <td>
                    {summary ? (
                      <details className="audit-params">
                        <summary>{summary}</summary>
                        <pre className="audit-json">
                          {JSON.stringify(entry.params, null, 2)}
                        </pre>
                      </details>
                    ) : (
                      '—'
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {hasMore ? (
        <div className="audit-more">
          <button
            type="button"
            className="btn ghost"
            disabled={loadingMore}
            onClick={() => void loadMore()}
          >
            {loadingMore
              ? '加载中…'
              : `加载更多（已显示 ${items.length} / ${total}）`}
          </button>
        </div>
      ) : null}
    </div>
  )
}
