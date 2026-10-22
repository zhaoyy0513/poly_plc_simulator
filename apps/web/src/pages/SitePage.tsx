import { useEffect, useState, type CSSProperties } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { copyText } from '../api'
import { CreateServerModal } from '../components/CreateServerModal'
import { Icon } from '../components/Icon'
import { SpotlightCard } from '../components/SpotlightCard'
import { StatusPill } from '../components/StatusPill'
import { usePolling } from '../hooks/usePolling'
import { useApp } from '../store'
import { PROTOCOL_META, endpointText, type Server } from '../types'

export function SitePage() {
  const { siteId = '' } = useParams()
  const navigate = useNavigate()
  const site = useApp((state) => state.sites.find((item) => item.id === siteId))
  const servers = useApp((state) => state.serversBySite[siteId]) ?? []
  const system = useApp((state) => state.system)
  const loadServers = useApp((state) => state.loadServers)
  const deleteServer = useApp((state) => state.deleteServer)
  const restartServer = useApp((state) => state.restartServer)
  const notify = useApp((state) => state.notify)

  const [open, setOpen] = useState(false)
  const lanIp =
    system?.addresses.find((address) => address !== '0.0.0.0' && address !== '127.0.0.1') ??
    '127.0.0.1'

  useEffect(() => {
    void loadServers(siteId)
  }, [siteId, loadServers])

  usePolling(() => void loadServers(siteId, true), 2500, Boolean(siteId))

  const copy = async (server: Server) => {
    const ok = await copyText(endpointText(server, lanIp))
    notify(ok ? 'ok' : 'error', ok ? '连接地址已复制' : '复制失败，请手动选择')
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
            <Icon name="server" size={14} /> Site · {system?.hostname ?? 'local'}
          </span>
          <h1>{site?.name ?? 'Site'}</h1>
          <p>{site?.description || '这台「服务器」上的模拟器实例，互相隔离运行。'}</p>
        </div>
        <button type="button" className="btn primary" onClick={() => setOpen(true)}>
          <Icon name="plus" size={16} /> 新建实例
        </button>
      </header>

      <div className="grid server-grid">
        {servers.map((server) => {
          const meta = PROTOCOL_META[server.protocol]
          const endpoint = endpointText(server, lanIp)
          return (
            <SpotlightCard
              key={server.id}
              className="server-card"
              onClick={() => navigate(`/servers/${server.id}`)}
            >
              <div className="server-top">
                <span className="proto-badge" style={{ '--accent': meta.accent } as CSSProperties}>
                  {meta.short}
                </span>
                <StatusPill status={server.status} error={server.error} />
                <div className="server-tools">
                  <button
                    type="button"
                    className="icon-btn"
                    title="复制连接地址"
                    onClick={(event) => {
                      event.stopPropagation()
                      void copy(server)
                    }}
                  >
                    <Icon name="copy" size={15} />
                  </button>
                  <button
                    type="button"
                    className="icon-btn"
                    title="重启"
                    onClick={(event) => {
                      event.stopPropagation()
                      void restartServer(server.id)
                    }}
                  >
                    <Icon name="restart" size={15} />
                  </button>
                  <button
                    type="button"
                    className="icon-btn danger"
                    title="删除"
                    onClick={(event) => {
                      event.stopPropagation()
                      if (window.confirm(`删除实例「${server.name}」？`)) {
                        void deleteServer(server)
                      }
                    }}
                  >
                    <Icon name="trash" size={15} />
                  </button>
                </div>
              </div>

              <h3>{server.name}</h3>
              <button
                type="button"
                className="endpoint mono"
                onClick={(event) => {
                  event.stopPropagation()
                  void copy(server)
                }}
              >
                <Icon name="plug" size={13} />
                {endpoint}
              </button>

              {server.status === 'error' && server.error ? (
                <p className="server-error" title={server.error}>
                  {server.error}
                </p>
              ) : null}

              <div className="server-foot">
                <span>
                  <strong>{server.tags.length}</strong> 变量
                </span>
                <span>
                  <strong>{server.clients}</strong> 连接
                </span>
                <span className="mono">:{server.port}</span>
              </div>
            </SpotlightCard>
          )
        })}

        <button type="button" className="add-card" onClick={() => setOpen(true)}>
          <span>
            <Icon name="plus" size={22} />
          </span>
          <strong>新建模拟器</strong>
          <small>Modbus TCP / S7 / OPC UA</small>
        </button>
      </div>

      {servers.length === 0 ? (
        <p className="empty-line">这个 site 还没有实例，点击「新建实例」开始。</p>
      ) : null}

      <CreateServerModal open={open} siteId={siteId} servers={servers} onClose={() => setOpen(false)} />
    </div>
  )
}
