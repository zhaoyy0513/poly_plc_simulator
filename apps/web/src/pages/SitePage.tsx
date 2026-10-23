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
  KIND_META,
  PROTOCOL_META,
  connectionText,
  isLink,
  kindSubtitle,
  type Server,
  type ServerKind,
  type SiteAccessDraft,
} from '../types'

const KIND_GROUP_COPY: Record<
  ServerKind,
  { title: string; description: string; add: string; addHint: string }
> = {
  sim: {
    title: '自建实例',
    description: '平台部署并运行，进程和端口都由平台管理。',
    add: '新建自建实例',
    addHint: '在这台机器上启动一个独立进程',
  },
  link: {
    title: '连接实例',
    description: '平台作为 client 访问外部 server，不占本机进程和端口。',
    add: '连接一台 server',
    addHint: '只保存地址，读写直接落到对方设备',
  },
}

export function SitePage() {
  const { siteId = '' } = useParams()
  const navigate = useNavigate()
  const site = useApp((state) => state.sites.find((item) => item.id === siteId))
  const servers = useApp((state) => state.serversBySite[siteId]) ?? []
  const system = useApp((state) => state.system)
  const loadServers = useApp((state) => state.loadServers)
  const deleteServer = useApp((state) => state.deleteServer)
  const restartServer = useApp((state) => state.restartServer)
  const updateServer = useApp((state) => state.updateServer)
  const probeSite = useApp((state) => state.probeSite)
  const updateSite = useApp((state) => state.updateSite)
  const notify = useApp((state) => state.notify)

  const [open, setOpen] = useState(false)
  const [createKind, setCreateKind] = useState<ServerKind>('sim')
  const [powerBusy, setPowerBusy] = useState<string | null>(null)
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
    const ok = await copyText(connectionText(server, lanIp, site?.host))
    notify(ok ? 'ok' : 'error', ok ? '连接地址已复制' : '复制失败，请手动选择')
  }

  const openCreate = (kind: ServerKind) => {
    setCreateKind(kind)
    setOpen(true)
  }

  const toggleEnabled = async (server: Server) => {
    if (server.enabled) {
      const message = isLink(server)
        ? `停用连接「${server.name}」会断开到 ${server.target_host}:${server.target_port ?? server.port} 的连接，重新启用后会再次连接。继续？`
        : `停用「${server.name}」会停止进程并断开所有 client，端口 ${server.port} 会被释放。继续？`
      if (!window.confirm(message)) return
    }
    setPowerBusy(server.id)
    await updateServer(server.id, { enabled: !server.enabled })
    setPowerBusy(null)
  }

  const renderServerCard = (server: Server) => {
    const meta = PROTOCOL_META[server.protocol]
    const kind = isLink(server) ? 'link' : 'sim'
    const endpoint = connectionText(server, lanIp, site?.host)
    return (
      <SpotlightCard
        key={server.id}
        className={`server-card${kind === 'link' ? ' is-link' : ''}${server.enabled ? '' : ' is-disabled'}`}
        onClick={() => navigate(`/servers/${server.id}`)}
      >
        <div className="server-top">
          <span className="proto-badge" style={{ '--accent': meta.accent } as CSSProperties}>
            {meta.short}
          </span>
          <span
            className="kind-chip"
            style={{ '--accent': KIND_META[kind].accent } as CSSProperties}
            title={KIND_META[kind].hint}
          >
            <Icon name={KIND_META[kind].icon} size={12} />
            {KIND_META[kind].short}
          </span>
          <StatusPill status={server.status} error={server.error} enabled={server.enabled} />
          <div className="server-tools">
            <button
              type="button"
              className="icon-btn"
              title={
                server.enabled
                  ? kind === 'link'
                    ? '停用连接'
                    : '停用实例'
                  : kind === 'link'
                    ? '启用连接'
                    : '启用实例'
              }
              disabled={powerBusy === server.id}
              onClick={(event) => {
                event.stopPropagation()
                void toggleEnabled(server)
              }}
            >
              <Icon name="power" size={15} />
            </button>
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
            {server.enabled ? (
              <button
                type="button"
                className="icon-btn"
                title={kind === 'link' ? '重新连接' : '重启'}
                onClick={(event) => {
                  event.stopPropagation()
                  void restartServer(server.id)
                }}
              >
                <Icon name="restart" size={15} />
              </button>
            ) : null}
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
        <p className="server-sub">{kindSubtitle(server, site)}</p>
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
          {kind === 'link' ? (
            <>
              <span>
                延迟 <strong>{server.latency_ms != null ? `${server.latency_ms} ms` : '—'}</strong>
              </span>
              <span>
                unit <strong>{server.target_options?.unit_id ?? 1}</strong>
              </span>
              <span className="mono">:{server.target_port ?? server.port}</span>
            </>
          ) : (
            <>
              <span>
                <strong>{server.tags.length}</strong> 变量
              </span>
              <span>
                <strong>{server.clients}</strong> 连接
              </span>
              <span className="mono">:{server.port}</span>
            </>
          )}
        </div>
      </SpotlightCard>
    )
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
        <button type="button" className="btn primary" onClick={() => openCreate('sim')}>
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

      <div className="kind-groups">
        {(Object.keys(KIND_GROUP_COPY) as ServerKind[]).map((kind) => {
          const group = servers.filter((server) =>
            kind === 'link' ? isLink(server) : !isLink(server),
          )
          const groupCopy = KIND_GROUP_COPY[kind]
          return (
            <section
              key={kind}
              className={`kind-group kind-group-${kind}`}
              style={{ '--kind': KIND_META[kind].accent } as CSSProperties}
              aria-labelledby={`kind-group-${kind}`}
            >
              <div className="kind-group-head">
                <span className="kind-group-icon" aria-hidden="true">
                  <Icon name={KIND_META[kind].icon} size={16} />
                </span>
                <div className="kind-group-copy">
                  <div className="kind-group-title">
                    <h2 id={`kind-group-${kind}`}>{groupCopy.title}</h2>
                    <span className="kind-group-count">{group.length} 个</span>
                  </div>
                  <p>{groupCopy.description}</p>
                </div>
              </div>

              <div className="grid server-grid">
                {group.map(renderServerCard)}
                <button type="button" className="add-card" onClick={() => openCreate(kind)}>
                  <span>
                    <Icon name={kind === 'sim' ? 'plus' : 'plug'} size={22} />
                  </span>
                  <strong>{groupCopy.add}</strong>
                  <small>{groupCopy.addHint}</small>
                </button>
              </div>
            </section>
          )
        })}
      </div>

      <CreateServerModal
        open={open}
        siteId={siteId}
        site={site}
        servers={servers}
        initialKind={createKind}
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
