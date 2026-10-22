import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Icon } from '../components/Icon'
import { Modal } from '../components/Modal'
import { SiteAccessFields } from '../components/SiteAccessFields'
import { SpotlightCard } from '../components/SpotlightCard'
import { useApp } from '../store'
import { DEFAULT_ACCESS, PROTOCOL_META, type Protocol, type SiteAccessDraft } from '../types'

export function SitesPage() {
  const sites = useApp((state) => state.sites)
  const system = useApp((state) => state.system)
  const createSite = useApp((state) => state.createSite)
  const deleteSite = useApp((state) => state.deleteSite)
  const navigate = useNavigate()

  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [access, setAccess] = useState<SiteAccessDraft>(DEFAULT_ACCESS)
  const [busy, setBusy] = useState(false)

  const lanIp =
    system?.addresses.find((address) => address !== '0.0.0.0' && address !== '127.0.0.1') ??
    '127.0.0.1'
  const serverCount = sites.reduce((total, site) => total + site.server_count, 0)

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
            <Icon name="bolt" size={14} /> 本机原生运行 · 三种协议
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
          <div className="hero-stat">
            <span>实例</span>
            <strong>{serverCount}</strong>
          </div>
          <div className="hero-stat">
            <span>可用协议</span>
            <strong>{system?.protocols.length ?? 0}/3</strong>
          </div>
          <div className="hero-connect">
            <div>
              <small>本机访问</small>
              <code>http://{lanIp}:5173</code>
            </div>
            <div>
              <small>API</small>
              <code>http://{lanIp}:8000</code>
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
                    if (window.confirm(`删除 site「${site.name}」及其下所有实例？`)) {
                      void deleteSite(site.id)
                    }
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
                  className={`chip${site.last_probe && site.last_probe.ok ? ' ok' : ''}${site.last_probe_at && site.last_probe && !site.last_probe.ok ? ' bad' : ''}`}
                  title={site.last_probe?.error ?? ''}
                >
                  {!site.last_probe_at
                    ? '未验证'
                    : site.last_probe?.ok
                      ? '已验证'
                      : '连接失败'}
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
          return (
            <div key={protocol} className={`protocol-card${supported ? '' : ' disabled'}`}>
              <span className="protocol-dot" style={{ background: PROTOCOL_META[protocol].accent }} />
              <div>
                <strong>{PROTOCOL_META[protocol].label}</strong>
                <p>{PROTOCOL_META[protocol].hint}</p>
              </div>
              <span className={`chip${supported ? ' ok' : ''}`}>{supported ? '可用' : '不可用'}</span>
            </div>
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
        <SiteAccessFields value={access} onChange={setAccess} />
        <div className="notice">
          <Icon name="bolt" size={15} />
          <span>
            填了目标机器后，在这个 site 下创建的实例会部署到那台机器上运行。创建后可以在 site
            页用「测试连接」验证 SSH 是否通。
          </span>
        </div>
      </Modal>
    </div>
  )
}
