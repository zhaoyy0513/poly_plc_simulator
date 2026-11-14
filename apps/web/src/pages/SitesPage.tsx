import { useEffect, useState, type CSSProperties } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useConfirm } from '../components/ConfirmModal'
import { Icon } from '../components/Icon'
import { Modal } from '../components/Modal'
import { SiteAccessFields } from '../components/SiteAccessFields'
import { SpotlightCard } from '../components/SpotlightCard'
import { useApp } from '../store'
import {
  DEFAULT_ACCESS,
  PROTOCOL_META,
  type Protocol,
  type Site,
  type SiteAccessDraft,
  type SiteProbe,
} from '../types'

/**
 * The probe chip. A refused probe is not always a failure: a Windows target is
 * refused on purpose (Windows machines run the control plane themselves), and
 * painting that red sends people hunting for an SSH problem that does not exist.
 */
function probeChip(site: Site): { text: string; tone: string } {
  if (!site.last_probe_at) return { text: '未验证', tone: '' }
  if (site.last_probe?.ok) return { text: '已验证', tone: ' ok' }
  if (site.last_probe?.os === 'Windows') return { text: '仅本机运行', tone: '' }
  return { text: '连接失败', tone: ' bad' }
}

export function SitesPage() {
  const sites = useApp((state) => state.sites)
  const system = useApp((state) => state.system)
  const createSite = useApp((state) => state.createSite)
  const deleteSite = useApp((state) => state.deleteSite)
  const navigate = useNavigate()
  const confirm = useConfirm()

  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [access, setAccess] = useState<SiteAccessDraft>(DEFAULT_ACCESS)
  const [busy, setBusy] = useState(false)
  const [probing, setProbing] = useState(false)
  const [probe, setProbe] = useState<SiteProbe | null>(null)

  const probeAccess = useApp((state) => state.probeAccess)

  // A leftover result from the last attempt would be misleading once the
  // dialog is reopened or the credentials change.
  useEffect(() => {
    if (open) return
    setProbe(null)
    setProbing(false)
  }, [open])

  const changeAccess = (next: SiteAccessDraft) => {
    setAccess(next)
    setProbe(null)
  }

  const testConnection = async () => {
    setProbing(true)
    setProbe(await probeAccess(access))
    setProbing(false)
  }

  const lanIp =
    system?.addresses.find((address) => address !== '0.0.0.0' && address !== '127.0.0.1') ??
    '127.0.0.1'
  // The built app is served by the control plane, so 5173 is only true while
  // running the Vite dev server. Report the origin the page is actually on;
  // in dev the API still lives on the control-plane port behind the proxy.
  //
  // Never guess a fallback port: `location.port` is empty on 80/443, and a
  // hardcoded number there would print an address that does not exist. Show
  // the real origin and let the protocol carry the default.
  const pageOrigin = window.location.origin
  const apiOrigin = pageOrigin.includes(':5173')
    ? pageOrigin.replace(':5173', ':51513')
    : pageOrigin
  const serverCount = sites.reduce((total, site) => total + site.server_count, 0)
  const protocolCounts: Record<Protocol, number> = {
    modbus_tcp: 0,
    s7: 0,
    opcua: 0,
  }
  for (const site of sites) {
    for (const protocol of Object.keys(protocolCounts) as Protocol[]) {
      protocolCounts[protocol] += site.protocol_counts?.[protocol] ?? 0
    }
  }

  const submit = async () => {
    if (!name.trim()) return
    setBusy(true)
    const ok = await createSite(name.trim(), description.trim(), access)
    setBusy(false)
    if (ok) {
      setName('')
      setDescription('')
      setAccess(DEFAULT_ACCESS)
      setOpen(false)
    }
  }

  return (
    <div className="page">
      <header className="hero">
        <div className="hero-copy">
          <span className="eyebrow">
            <Icon name="bolt" size={14} /> 统一控制面 · 三种协议
          </span>
          <h1>
            PLC 服务端<span className="grad">模拟器</span>
          </h1>
          <p>
            每台 site 代表一台目标服务器，下面可以创建互相隔离的 Modbus TCP / S7 / OPC UA
            实例。每个实例独立进程、独立端口、独立数据，真实 PLC 客户端直连即可。
          </p>
          <div className="hero-actions">
            <button type="button" className="btn primary" onClick={() => setOpen(true)}>
              <Icon name="plus" size={16} /> 新建 Site
            </button>
            {sites[0] ? (
              <button
                type="button"
                className="btn ghost"
                onClick={() => navigate(`/sites/${sites[0].id}`)}
              >
                进入 {sites[0].name} <Icon name="chevronRight" size={15} />
              </button>
            ) : null}
          </div>
        </div>

        <div className="hero-panel">
          <div className="hero-stat">
            <span>Sites</span>
            <strong>{sites.length}</strong>
          </div>
          <button
            type="button"
            className="hero-stat hero-stat-link"
            onClick={() => navigate('/instances')}
            title="查看所有实例"
          >
            <span>实例</span>
            <strong>{serverCount}</strong>
            <em>
              查看全部 <Icon name="chevronRight" size={11} />
            </em>
          </button>
          <div className="hero-stat">
            <span>可用协议</span>
            <strong>{system?.protocols.length ?? 0}/3</strong>
          </div>
          <div className="hero-connect">
            <div>
              <small>本机访问</small>
              <code>{pageOrigin.replace('127.0.0.1', lanIp).replace('localhost', lanIp)}</code>
            </div>
            <div>
              <small>API</small>
              <code>{apiOrigin.replace('127.0.0.1', lanIp).replace('localhost', lanIp)}</code>
            </div>
          </div>
        </div>
      </header>

      <section className="section-head">
        <h2>Sites</h2>
        <span className="muted">一个 Site = 一台被测服务器</span>
      </section>

      {sites.length === 0 ? (
        <SpotlightCard className="empty-card">
          <span className="empty-icon">
            <Icon name="server" size={26} />
          </span>
          <h3>先创建一个 Site</h3>
          <p>例如「12 服务器」，然后就可以在下面创建三种协议的模拟器实例。</p>
          <button type="button" className="btn primary" onClick={() => setOpen(true)}>
            <Icon name="plus" size={16} /> 新建 Site
          </button>
        </SpotlightCard>
      ) : (
        <div className="grid sites-grid">
          {sites.map((site) => (
            <SpotlightCard
              key={site.id}
              className="site-card"
              as="button"
              onClick={() => navigate(`/sites/${site.id}`)}
            >
              <div className="site-top">
                <span className="site-avatar">{site.name.slice(0, 1).toUpperCase()}</span>
                <button
                  type="button"
                  className="icon-btn danger"
                  title="删除 site"
                  onClick={(event) => {
                    event.stopPropagation()
                    void (async () => {
                      const ok = await confirm({
                        title: '删除 Site',
                        message: `确定删除 site「${site.name}」及其下所有实例？`,
                        confirmText: '删除',
                        danger: true,
                      })
                      if (ok) void deleteSite(site.id)
                    })()
                  }}
                >
                  <Icon name="trash" size={15} />
                </button>
              </div>
              <h3>{site.name}</h3>
              <p className="site-host mono">
                {site.local
                  ? '本机 · 不经 SSH'
                  : `${site.ssh_user ? `${site.ssh_user}@` : ''}${site.host}:${site.ssh_port}`}
              </p>
              <div className="site-foot">
                <span className="pill">
                  <Icon name="server" size={13} /> {site.server_count} 个实例
                </span>
                <span
                  className={`chip${probeChip(site).tone}`}
                  title={site.last_probe?.error ?? ''}
                >
                  {probeChip(site).text}
                </span>
                <span className="linkish">
                  打开 <Icon name="chevronRight" size={14} />
                </span>
              </div>
            </SpotlightCard>
          ))}
        </div>
      )}

      <section className="section-head protocols">
        <h2>协议能力</h2>
        <span className="muted">由后端实测上报</span>
      </section>
      <div className="grid protocol-grid">
        {(Object.keys(PROTOCOL_META) as Protocol[]).map((protocol) => {
          const supported = system?.protocols.includes(protocol) ?? false
          const count = protocolCounts[protocol]
          const body = (
            <>
              <span className="protocol-dot" style={{ background: PROTOCOL_META[protocol].accent }} />
              <div className="protocol-card-body">
                <strong>{PROTOCOL_META[protocol].label}</strong>
                <p>{PROTOCOL_META[protocol].hint}</p>
                <small className="protocol-count">
                  {supported
                    ? `${count} 个实例 · 查看列表`
                    : `这台机器还用不了（缺少依赖），暂无实例`}{' '}
                  <Icon name="chevronRight" size={12} />
                </small>
              </div>
              <span className={`chip${supported ? ' ok' : ''}`}>{supported ? '可用' : '不可用'}</span>
            </>
          )
          // An unavailable protocol has no list worth showing. Rendering it as
          // a div instead of a Link keeps it from looking like a broken link.
          if (!supported) {
            return (
              <div
                key={protocol}
                className="protocol-card disabled"
                style={{ '--accent': PROTOCOL_META[protocol].accent } as CSSProperties}
                aria-disabled="true"
                title={`${PROTOCOL_META[protocol].label}在这台机器上不可用`}
              >
                {body}
              </div>
            )
          }
          return (
            <Link
              key={protocol}
              to={`/instances?protocol=${protocol}`}
              className="protocol-card clickable"
              style={{ '--accent': PROTOCOL_META[protocol].accent } as CSSProperties}
              title={`查看所有 ${PROTOCOL_META[protocol].label} 实例`}
            >
              {body}
            </Link>
          )
        })}
      </div>

      <Modal
        open={open}
        title="新建 Site"
        subtitle="Site 对应一台目标服务器；本机模式下它就是当前这台机器。"
        onClose={() => setOpen(false)}
        footer={
          <>
            <button type="button" className="btn ghost" onClick={() => setOpen(false)}>
              取消
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={busy || !name.trim()}
              onClick={() => void submit()}
            >
              {busy ? '创建中…' : '创建 Site'}
            </button>
          </>
        }
        width={680}
      >
        <label className="field">
          <span>名称</span>
          <input
            className="input"
            value={name}
            autoFocus
            placeholder="例如：12 服务器"
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void submit()
            }}
          />
        </label>
        <label className="field">
          <span>描述（可选）</span>
          <input
            className="input"
            value={description}
            placeholder="例如：产线 A 的测试机"
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
        <SiteAccessFields value={access} onChange={changeAccess} />

        {access.host.trim() ? (
          <div className="access-test">
            <button
              type="button"
              className="btn ghost"
              disabled={probing || !access.host.trim() || !access.ssh_user.trim()}
              onClick={() => void testConnection()}
            >
              <Icon name="plug" size={15} /> {probing ? '测试中…' : '测试连接'}
            </button>
            <span className="hint">先确认能连上再创建，不用先保存。</span>
          </div>
        ) : null}

        {probe ? (
          probe.ok ? (
            <div className={`notice${probe.python && probe.deploy_ok ? ' ok' : ''}`}>
              <Icon name="bolt" size={15} />
              <span>
                可以连接：{probe.distro || probe.os} {probe.arch} · {probe.python || '没找到 Python'}
                {probe.docker ? ` · ${probe.docker}` : ''}
                {probe.deploy_ok ? '' : ' · 部署目录不可写'}
                {probe.python ? '' : '（目标机需要 Python 3.10+ 或 Docker，部署时用到）'}
              </span>
            </div>
          ) : probe.os === 'Windows' ? (
            // Not a failure to fix: Windows machines are local sites by
            // decision. Red would send the user hunting for an SSH problem
            // that does not exist.
            <div className="notice">
              <Icon name="bolt" size={15} />
              <span>{probe.error}</span>
            </div>
          ) : (
            <div className="error-box">
              <Icon name="alert" size={16} /> {probe.error}
            </div>
          )
        ) : null}

        <div className="notice">
          <Icon name="bolt" size={15} />
          <span>
            填了目标机器后，在这个 site 下创建的实例会部署到那台机器上运行；本机 site
            的实例直接在当前机器上起进程。<strong>远程部署只支持 Linux</strong>——Windows
            机器请直接在那台上运行控制面，host 留空即可。
          </span>
        </div>
      </Modal>
    </div>
  )
}
