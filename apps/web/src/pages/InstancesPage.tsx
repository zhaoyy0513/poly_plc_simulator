import { useMemo, useState, type CSSProperties } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { api, copyText } from '../api'
import { useConfirm } from '../components/ConfirmModal'
import { Icon } from '../components/Icon'
import { StatusPill } from '../components/StatusPill'
import { usePolling } from '../hooks/usePolling'
import { useApp } from '../store'
import {
  KIND_META,
  PROTOCOL_META,
  connectionText,
  isLink,
  linkParamsText,
  type Protocol,
  type ServerKind,
  type ServerWithSite,
} from '../types'

type ProtocolFilter = 'all' | Protocol
type KindFilter = 'all' | ServerKind

function protocolFilterOf(value: string | null): ProtocolFilter {
  if (value === 'modbus_tcp' || value === 's7' || value === 'opcua') return value
  return 'all'
}

function kindOf(server: ServerWithSite): ServerKind {
  return isLink(server) ? 'link' : 'sim'
}

export function InstancesPage() {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  // Opening an instance from this table must return here, not to the site page.
  const returnTo = `${location.pathname}${location.search}`
  const protocolFilter = protocolFilterOf(params.get('protocol'))
  const system = useApp((state) => state.system)
  const notify = useApp((state) => state.notify)
  const confirm = useConfirm()

  const [servers, setServers] = useState<ServerWithSite[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [kindFilter, setKindFilter] = useState<KindFilter>('all')
  const [search, setSearch] = useState('')
  const [powerBusy, setPowerBusy] = useState<string | null>(null)

  const lanIp =
    system?.addresses.find((address) => address !== '0.0.0.0' && address !== '127.0.0.1') ??
    '127.0.0.1'

  const load = async () => {
    try {
      setServers(await api.allServers())
      setError(null)
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : String(loadError))
    } finally {
      setLoading(false)
    }
  }

  usePolling(() => void load(), 5000)

  const counts = useMemo(() => {
    const next: Record<ProtocolFilter, number> = {
      all: servers.length,
      modbus_tcp: 0,
      s7: 0,
      opcua: 0,
    }
    for (const server of servers) next[server.protocol] += 1
    return next
  }, [servers])

  const visible = useMemo(() => {
    const query = search.trim().toLowerCase()
    return servers.filter((server) => {
      if (protocolFilter !== 'all' && server.protocol !== protocolFilter) return false
      if (kindFilter !== 'all' && kindOf(server) !== kindFilter) return false
      if (!query) return true
      const haystack = [
        server.name,
        server.site_name,
        server.site_host,
        server.target_host ?? '',
        server.endpoint ?? '',
        String(server.port),
      ]
        .join(' ')
        .toLowerCase()
      return haystack.includes(query)
    })
  }, [servers, protocolFilter, kindFilter, search])

  const running = servers.filter(
    (server) => server.enabled && server.status === 'running',
  ).length
  const disabled = servers.filter((server) => !server.enabled).length
  const links = servers.filter((server) => isLink(server)).length

  const selectProtocol = (next: ProtocolFilter) => {
    const updated = new URLSearchParams(params)
    if (next === 'all') updated.delete('protocol')
    else updated.set('protocol', next)
    setParams(updated, { replace: true })
  }

  const copyEndpoint = async (server: ServerWithSite) => {
    const siteHost = server.site_local ? undefined : server.site_host
    const ok = await copyText(connectionText(server, lanIp, siteHost))
    notify(ok ? 'ok' : 'error', ok ? '连接地址已复制' : '复制失败，请手动选择')
  }

  const toggleEnabled = async (server: ServerWithSite) => {
    if (server.enabled) {
      const message = isLink(server)
        ? `停用连接「${server.name}」会断开到 ${server.target_host}:${server.target_port ?? server.port} 的连接，重新启用后会再次连接。继续？`
        : `停用「${server.name}」会停止进程并断开所有 client，端口 ${server.port} 会被释放。继续？`
      const ok = await confirm({
        title: isLink(server) ? '停用连接' : '停用实例',
        message,
        confirmText: '停用',
        danger: true,
      })
      if (!ok) return
    }
    setPowerBusy(server.id)
    try {
      const updated = await api.updateServer(server.id, { enabled: !server.enabled })
      setServers((list) =>
        list.map((item) => (item.id === updated.id ? { ...item, ...updated } : item)),
      )
      notify(
        updated.status === 'error' ? 'error' : 'ok',
        updated.error ??
          (updated.enabled
            ? `实例「${updated.name}」已启用`
            : `实例「${updated.name}」已停用`),
      )
    } catch (toggleError) {
      notify(
        'error',
        toggleError instanceof Error ? toggleError.message : String(toggleError),
      )
    } finally {
      setPowerBusy(null)
    }
  }

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
            <Icon name="layers" size={14} /> 实例总览
          </span>
          <h1>所有实例</h1>
          <p>
            跨 site 查看 Modbus TCP / S7 / OPC UA 实例，以及每个实例实际监听或连接到的地址。
          </p>
        </div>
        <div className="head-actions">
          <button type="button" className="btn ghost" onClick={() => void load()}>
            <Icon name="restart" size={15} /> {loading ? '刷新中…' : '刷新'}
          </button>
        </div>
      </header>

      <div className="instance-summary">
        <span>
          <strong>{servers.length}</strong> 个实例
        </span>
        <span>
          <strong>{running}</strong> 运行中
        </span>
        {disabled > 0 ? (
          <span>
            <strong>{disabled}</strong> 已停用
          </span>
        ) : null}
        <span>
          <strong>{links}</strong> 连接
        </span>
        <span>
          <strong>{servers.length - links}</strong> 自建
        </span>
        <span className="instance-summary-note">
          {visible.length === servers.length
            ? '按协议或身份筛选'
            : `当前显示 ${visible.length} / ${servers.length}`}
        </span>
      </div>

      <div className="tabs instance-tabs">
        <button
          type="button"
          className={`tab${protocolFilter === 'all' ? ' active' : ''}`}
          onClick={() => selectProtocol('all')}
        >
          <Icon name="layers" size={15} /> 全部 <span className="tab-count">{counts.all}</span>
        </button>
        {(Object.keys(PROTOCOL_META) as Protocol[]).map((protocol) => {
          const meta = PROTOCOL_META[protocol]
          return (
            <button
              key={protocol}
              type="button"
              className={`tab${protocolFilter === protocol ? ' active' : ''}`}
              onClick={() => selectProtocol(protocol)}
            >
              <span
                className="proto-badge small"
                style={{ '--accent': meta.accent } as CSSProperties}
              >
                {meta.short}
              </span>
              {meta.label} <span className="tab-count">{counts[protocol]}</span>
            </button>
          )
        })}
        <span className="tab-note">状态每 5 秒刷新</span>
      </div>

      <div className="instance-filters">
        <label className="instance-search">
          <Icon name="search" size={15} />
          <input
            value={search}
            aria-label="搜索实例"
            placeholder="搜索名称、site 或地址"
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <div className="instance-seg" role="group" aria-label="实例身份">
          {([
            ['all', '全部'],
            ['sim', '自建'],
            ['link', '连接'],
          ] as const).map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={kindFilter === value ? 'active' : ''}
              aria-pressed={kindFilter === value}
              onClick={() => setKindFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <div className="error-box">
          <Icon name="alert" size={16} /> {error}
        </div>
      ) : null}

      <div className="table-wrap">
        <table className="data-table instances-table">
          <thead>
            <tr>
              <th>实例</th>
              <th>Site</th>
              <th>连接信息</th>
              <th>状态</th>
              <th className="col-data">运行信息</th>
              <th className="col-actions" />
            </tr>
          </thead>
          <tbody>
            {loading && servers.length === 0 ? (
              <tr>
                <td colSpan={6} className="table-empty">
                  正在加载实例…
                </td>
              </tr>
            ) : null}

            {!loading && visible.length === 0 ? (
              <tr>
                <td colSpan={6} className="table-empty">
                  {servers.length === 0
                    ? '还没有实例。先创建一个 Site，再在 Site 里创建实例。'
                    : protocolFilter === 'all'
                      ? '没有符合筛选条件的实例。'
                      : `还没有 ${PROTOCOL_META[protocolFilter].label} 实例。`}{' '}
                  <Link to="/" className="crumb-link">
                    去 Sites <Icon name="chevronRight" size={13} />
                  </Link>
                </td>
              </tr>
            ) : null}

            {visible.map((server) => {
              const meta = PROTOCOL_META[server.protocol]
              const link = isLink(server)
              const kind = kindOf(server)
              const endpoint = connectionText(
                server,
                lanIp,
                server.site_local ? undefined : server.site_host,
              )
              return (
                <tr
                  key={server.id}
                  className={server.enabled ? undefined : 'instance-row is-disabled'}
                >
                  <td>
                    <div className="instance-main">
                      <span
                        className="proto-badge small"
                        style={{ '--accent': meta.accent } as CSSProperties}
                      >
                        {meta.short}
                      </span>
                      <div className="instance-title">
                        <Link
                          to={`/servers/${server.id}`}
                          state={{ from: returnTo }}
                          className="instance-name"
                        >
                          {server.name}
                        </Link>
                        <span
                          className="kind-chip"
                          style={{ '--accent': KIND_META[kind].accent } as CSSProperties}
                          title={KIND_META[kind].hint}
                        >
                          <Icon name={KIND_META[kind].icon} size={11} />
                          {KIND_META[kind].short}
                        </span>
                      </div>
                    </div>
                  </td>

                  <td>
                    <div className="instance-site">
                      <strong>{server.site_name || '—'}</strong>
                      <small className="mono">
                        {server.site_local ? '本机' : server.site_host || '—'}
                      </small>
                    </div>
                  </td>

                  <td>
                    <div className="instance-conn">
                      <small>{link ? '连接目标' : '监听'}</small>
                      <button
                        type="button"
                        className="endpoint mono"
                        title="复制连接地址"
                        onClick={() => void copyEndpoint(server)}
                      >
                        {endpoint}
                        <Icon name="copy" size={12} />
                      </button>
                    </div>
                  </td>

                  <td>
                    <StatusPill
                      status={server.status}
                      error={server.error}
                      enabled={server.enabled}
                    />
                    {server.status === 'error' && server.error ? (
                      <small className="instance-error" title={server.error}>
                        {server.error}
                      </small>
                    ) : null}
                  </td>

                  <td className="col-data">
                    <div className="instance-stats">
                      {link ? (
                        <>
                          <span>
                            延迟{' '}
                            <strong>
                              {server.latency_ms != null ? `${server.latency_ms} ms` : '—'}
                            </strong>
                          </span>
                          <span>
                            {linkParamsText(server)}
                          </span>
                        </>
                      ) : (
                        <>
                          <span>
                            <strong>
                              {server.protocol === 'modbus_tcp' ? 4 : server.tags.length}
                            </strong>{' '}
                            {server.protocol === 'modbus_tcp' ? '个数据区' : '变量'}
                          </span>
                          <span>
                            <strong>{server.clients}</strong> 连接
                          </span>
                        </>
                      )}
                    </div>
                  </td>

                  <td className="col-actions">
                    <div className="instance-actions">
                      <button
                        type="button"
                        className="icon-btn tiny"
                        title={
                          server.enabled
                            ? kind === 'link'
                              ? '停用连接'
                              : '停用实例'
                            : kind === 'link'
                              ? '启用连接'
                              : '启用实例'
                        }
                        aria-label={
                          server.enabled
                            ? kind === 'link'
                              ? '停用连接'
                              : '停用实例'
                            : kind === 'link'
                              ? '启用连接'
                              : '启用实例'
                        }
                        disabled={powerBusy === server.id}
                        onClick={() => void toggleEnabled(server)}
                      >
                        <Icon name="power" size={13} />
                      </button>
                      <Link
                        className="btn tiny ghost"
                        to={`/servers/${server.id}`}
                        state={{ from: returnTo }}
                      >
                        打开 <Icon name="chevronRight" size={13} />
                      </Link>
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
