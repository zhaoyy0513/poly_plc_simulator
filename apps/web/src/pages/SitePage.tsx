import { useEffect, useState, type CSSProperties } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { copyText } from '../api'
import { CreateServerModal } from '../components/CreateServerModal'
import { DeployNotice } from '../components/DeployNotice'
import { Icon } from '../components/Icon'
import { Modal } from '../components/Modal'
import { SiteAccessFields } from '../components/SiteAccessFields'
import { SpotlightCard } from '../components/SpotlightCard'
import { StatusPill } from '../components/StatusPill'
import { usePolling } from '../hooks/usePolling'
import { useApp } from '../store'
import {
  DEFAULT_ACCESS,
  PROTOCOL_META,
  endpointText,
  type Server,
  type SiteAccessDraft,
} from '../types'

export function SitePage() {
  const { siteId = '' } = useParams()
  const navigate = useNavigate()
  const site = useApp((state) => state.sites.find((item) => item.id === siteId))
  const servers = useApp((state) => state.serversBySite[siteId]) ?? []
  const system = useApp((state) => state.system)
  const loadServers = useApp((state) => state.loadServers)
  const deleteServer = useApp((state) => state.deleteServer)
  const restartServer = useApp((state) => state.restartServer)
  const probeSite = useApp((state) => state.probeSite)
  const updateSite = useApp((state) => state.updateSite)
  const notify = useApp((state) => state.notify)

  const [open, setOpen] = useState(false)
  const [accessOpen, setAccessOpen] = useState(false)
  const [draft, setDraft] = useState<SiteAccessDraft>(DEFAULT_ACCESS)
  const [probing, setProbing] = useState(false)
  const [savingAccess, setSavingAccess] = useState(false)
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

  const openAccess = () => {
    setDraft({
      host: site?.host ?? '',
      ssh_port: site?.ssh_port ?? 22,
      ssh_user: site?.ssh_user ?? '',
      ssh_password: '',
      deploy_dir: site?.deploy_dir || DEFAULT_ACCESS.deploy_dir,
    })
    setAccessOpen(true)
  }

  const test = async (password?: string) => {
    setProbing(true)
    await probeSite(siteId, password || undefined)
    setProbing(false)
  }

  const saveAccess = async () => {
    setSavingAccess(true)
    const ok = await updateSite(siteId, {
      host: draft.host,
      ssh_port: draft.ssh_port,
      ssh_user: draft.ssh_user,
      deploy_dir: draft.deploy_dir,
      ...(draft.ssh_password ? { ssh_password: draft.ssh_password } : {}),
    })
    setSavingAccess(false)
    if (ok) setAccessOpen(false)
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
            <Icon name="server" size={14} /> Site ·{' '}
            {site && !site.local ? site.host : (system?.hostname ?? 'local')}
          </span>
          <h1>{site?.name ?? 'Site'}</h1>
          <p>{site?.description || '这台「服务器」上的模拟器实例，互相隔离运行。'}</p>
        </div>
        <button type="button" className="btn primary" onClick={() => setOpen(true)}>
          <Icon name="plus" size={16} /> 新建实例
        </button>
      </header>

      {site ? (
        <>
          {!site.local ? <DeployNotice plan={site.deploy} host={site.host} /> : null}

          <section className="panel site-access">
            <div className="tags-toolbar">
              <div>
                <strong>目标机器</strong>
                <p className="hint">
                  {site.local
                    ? '本机执行：实例直接在这台机器上起进程，不经过 SSH。'
                    : `${site.ssh_user || '(未填用户名)'}@${site.host}:${site.ssh_port} · 部署目录 ${site.deploy_dir} · 密码${site.ssh_configured ? '已配置' : '未配置'}`}
                </p>
              </div>
              <div className="access-actions">
                <button
                  type="button"
                  className="btn ghost"
                  disabled={probing}
                  onClick={() => void test()}
                >
                  <Icon name="plug" size={15} /> {probing ? '测试中…' : '测试连接'}
                </button>
                <button type="button" className="btn ghost" onClick={openAccess}>
                  <Icon name="chip" size={15} /> 访问信息
                </button>
              </div>
            </div>

            {site.last_probe_at ? (
              <div className="probe-grid">
                <div>
                  <small>系统</small>
                  <span className="mono">{site.last_probe.distro || site.last_probe.os || '—'}</span>
                </div>
                <div>
                  <small>Python</small>
                  <span className="mono">{site.last_probe.python || '—'}</span>
                </div>
                <div>
                  <small>Docker</small>
                  <span className="mono">{site.last_probe.docker || '—'}</span>
                </div>
                <div>
                  <small>部署目录可写</small>
                  <span className="mono">{site.last_probe.deploy_ok ? '是' : '否'}</span>
                </div>
                <div className="probe-ports">
                  <small>已占用端口</small>
                  <span className="mono">
                    {site.last_probe.ports_in_use?.length
                      ? `${site.last_probe.ports_in_use.slice(0, 12).join(', ')}${site.last_probe.ports_in_use.length > 12 ? ' …' : ''}`
                      : '—'}
                  </span>
                </div>
              </div>
            ) : (
              <p className="hint">
                还没探测过这台机器。点「测试连接」，平台会 SSH 上去看系统、Python 版本和端口占用情况。
              </p>
            )}

            {site.last_probe?.error ? (
              <div className="error-box">
                <Icon name="alert" size={16} /> {site.last_probe.error}
              </div>
            ) : null}
          </section>
        </>
      ) : null}

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

      <CreateServerModal
        open={open}
        siteId={siteId}
        site={site}
        servers={servers}
        onClose={() => setOpen(false)}
      />

      <Modal
        open={accessOpen}
        title="访问信息"
        subtitle="平台用这个身份 SSH 登录这台机器"
        width={620}
        onClose={() => setAccessOpen(false)}
        footer={
          <>
            <button type="button" className="btn ghost" onClick={() => setAccessOpen(false)}>
              取消
            </button>
            <button
              type="button"
              className="btn ghost"
              disabled={probing}
              onClick={() => void test(draft.ssh_password)}
            >
              {probing ? '测试中…' : '测试连接'}
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={savingAccess}
              onClick={() => void saveAccess()}
            >
              {savingAccess ? '保存中…' : '保存'}
            </button>
          </>
        }
      >
        <SiteAccessFields
          value={draft}
          onChange={setDraft}
          passwordPlaceholder={site?.ssh_configured ? '已配置，留空则不改' : undefined}
        />
      </Modal>
    </div>
  )
}
