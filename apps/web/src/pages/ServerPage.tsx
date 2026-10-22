import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { copyText } from '../api'
import { DataAreaView } from '../components/DataAreaView'
import { Icon } from '../components/Icon'
import { StatusPill } from '../components/StatusPill'
import { TagsEditor } from '../components/TagsEditor'
import { ValueTable } from '../components/ValueTable'
import { usePolling } from '../hooks/usePolling'
import { useApp } from '../store'
import { PROTOCOL_META, endpointText, uptime, type Tag } from '../types'

type Tab = 'area' | 'values' | 'tags' | 'logs'

export function ServerPage() {
  const { serverId = '' } = useParams()
  const navigate = useNavigate()
  const server = useApp((state) => state.server)
  const values = useApp((state) => state.values)
  const logs = useApp((state) => state.logs)
  const logsLoading = useApp((state) => state.logsLoading)
  const system = useApp((state) => state.system)
  const sites = useApp((state) => state.sites)
  const loadServer = useApp((state) => state.loadServer)
  const refreshValues = useApp((state) => state.refreshValues)
  const loadLogs = useApp((state) => state.loadLogs)
  const saveTags = useApp((state) => state.saveTags)
  const restartServer = useApp((state) => state.restartServer)
  const updateServer = useApp((state) => state.updateServer)
  const deleteServer = useApp((state) => state.deleteServer)
  const notify = useApp((state) => state.notify)

  // Modbus users usually want the raw register view first: it is what a real
  // client sees, including addresses that have no tag attached.
  const [tab, setTab] = useState<Tab>('area')
  const [saving, setSaving] = useState(false)
  const logRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    if (serverId) void loadServer(serverId)
  }, [serverId, loadServer])

  const current = server?.id === serverId ? server : null
  const running = current?.status === 'running'

  usePolling(() => void refreshValues(serverId), 1000, Boolean(serverId) && running)
  usePolling(() => void loadServer(serverId, true), 4000, Boolean(serverId))
  usePolling(() => void loadLogs(serverId), 2500, Boolean(serverId) && tab === 'logs')

  useEffect(() => {
    if (tab === 'logs' && serverId) void loadLogs(serverId)
  }, [tab, serverId, loadLogs])

  useEffect(() => {
    if (tab !== 'logs' || !logRef.current) return
    logRef.current.scrollTop = logRef.current.scrollHeight
  }, [logs, tab])

  if (!current) {
    return (
      <div className="page">
        <p className="muted">正在加载实例…</p>
      </div>
    )
  }

  const meta = PROTOCOL_META[current.protocol]
  const site = sites.find((item) => item.id === current.site_id)
  const remote = Boolean(site && !site.local)
  const lanIp =
    system?.addresses.find((address) => address !== '0.0.0.0' && address !== '127.0.0.1') ??
    '127.0.0.1'
  // A remote instance is reachable at the target's address, not at ours.
  const endpoint = endpointText(current, remote ? (site?.host ?? lanIp) : lanIp)
  const localEndpoint =
    current.protocol === 'opcua'
      ? `opc.tcp://127.0.0.1:${current.port}`
      : `127.0.0.1:${current.port}`
  const startedAt = values?.started_at ?? current.started_at
  const isModbus = current.protocol === 'modbus_tcp'
  const effectiveTab: Tab = isModbus
    ? tab === 'values' || tab === 'tags'
      ? 'area'
      : tab
    : tab === 'area'
      ? 'values'
      : tab

  const copyEndpoint = async () => {
    const ok = await copyText(endpoint)
    notify(ok ? 'ok' : 'error', ok ? '连接地址已复制' : '复制失败，请手动选择')
  }

  const save = async (tags: Tag[]) => {
    setSaving(true)
    const ok = await saveTags(serverId, tags)
    setSaving(false)
    return ok
  }

  return (
    <div className="page">
      <nav className="crumbs">
        <Link to={`/sites/${current.site_id}`} className="crumb-link">
          <Icon name="arrowLeft" size={14} /> 返回 Site
        </Link>
      </nav>

      <header className="page-head server-head">
        <div>
          <span className="eyebrow">
            <span className="proto-badge small" style={{ '--accent': meta.accent } as CSSProperties}>
              {meta.short}
            </span>
            {meta.label}
            <StatusPill status={current.status} error={current.error} />
          </span>
          <h1>{current.name}</h1>
          <button type="button" className="endpoint big mono" onClick={() => void copyEndpoint()}>
            <Icon name="plug" size={14} />
            {endpoint}
            <Icon name="copy" size={13} />
          </button>
          {remote ? (
            <small className="endpoint-hint">
              运行在 <span className="mono">{site?.host}</span> 上，{site?.deploy?.label ?? '远程部署'}
            </small>
          ) : current.bind_ip === '0.0.0.0' ? (
            <small className="endpoint-hint">
              本机客户端也可用 <span className="mono">{localEndpoint}</span>
            </small>
          ) : null}
        </div>
        <div className="head-actions">
          <button
            type="button"
            className="btn ghost"
            onClick={() => void restartServer(current.id)}
            disabled={current.status === 'starting'}
          >
            <Icon name="restart" size={15} /> 重启
          </button>
          <button
            type="button"
            className="btn danger"
            onClick={() => {
              if (window.confirm(`删除实例「${current.name}」？`)) {
                void deleteServer(current).then(() => navigate(`/sites/${current.site_id}`))
              }
            }}
          >
            <Icon name="trash" size={15} /> 删除
          </button>
        </div>
      </header>

      {current.status === 'error' && current.error ? (
        <div className="error-box">
          <Icon name="alert" size={16} />
          <span>{current.error}</span>
          <button type="button" className="btn tiny" onClick={() => void restartServer(current.id)}>
            重试
          </button>
        </div>
      ) : null}

      {current.bind_ip !== '0.0.0.0' ? (
        <div className="warn-box">
          <Icon name="alert" size={16} />
          <span>
            当前只监听 <b className="mono">{current.bind_ip}:{current.port}</b>，用
            <code>127.0.0.1:{current.port}</code> 会连不上。需要本机和局域网都能连时，改为
            0.0.0.0（会重启实例）。
          </span>
          <button
            type="button"
            className="btn tiny"
            onClick={() => void updateServer(current.id, { bind_ip: '0.0.0.0' })}
          >
            改为 0.0.0.0
          </button>
        </div>
      ) : null}

      <div className="stat-row">
        <div className="stat">
          <small>监听</small>
          <strong className="mono">{current.bind_ip}</strong>
          <span className="mono">:{current.port}</span>
        </div>
        <div className="stat">
          {isModbus ? (
            <>
              <small>数据区</small>
              <strong>4</strong>
              <span> 个</span>
            </>
          ) : (
            <>
              <small>变量</small>
              <strong>{current.tags.length}</strong>
            </>
          )}
        </div>
        <div className="stat">
          <small>客户端连接</small>
          <strong>{values?.clients ?? current.clients}</strong>
        </div>
        <div className="stat">
          <small>运行时长</small>
          <strong>{uptime(startedAt)}</strong>
        </div>
        <div className="stat">
          <small>配置版本</small>
          <strong>v{current.config_version}</strong>
        </div>
      </div>

      <div className="tabs">
        {isModbus ? (
          <button
            type="button"
            className={`tab${effectiveTab === 'area' ? ' active' : ''}`}
            onClick={() => setTab('area')}
          >
            <Icon name="chip" size={15} /> 数据区
          </button>
        ) : (
          <>
            <button
              type="button"
              className={`tab${effectiveTab === 'values' ? ' active' : ''}`}
              onClick={() => setTab('values')}
            >
              <Icon name="bolt" size={15} /> 实时变量
            </button>
            <button
              type="button"
              className={`tab${effectiveTab === 'tags' ? ' active' : ''}`}
              onClick={() => setTab('tags')}
            >
              <Icon name="chip" size={15} /> 标签表
            </button>
          </>
        )}
        <button
          type="button"
          className={`tab${effectiveTab === 'logs' ? ' active' : ''}`}
          onClick={() => setTab('logs')}
        >
          <Icon name="terminal" size={15} /> 日志
        </button>
        <span className="tab-note">
          {running
            ? effectiveTab === 'area'
              ? '实时刷新 1s · 双击单元格写入'
              : effectiveTab === 'values'
                ? '实时刷新 1s（页面隐藏时暂停）'
                : effectiveTab === 'logs'
                  ? '日志 2.5s 刷新'
                  : '保存会重启实例'
            : '实例未运行'}
        </span>
      </div>

      {effectiveTab === 'area' && isModbus ? (
        <section className="panel">
          <DataAreaView server={current} running={Boolean(running)} />
        </section>
      ) : null}

      {effectiveTab === 'values' ? (
        <section className="panel">
          <ValueTable server={current} values={values?.values ?? {}} />
          <p className="hint">
            {current.protocol === 'modbus_tcp'
              ? 'Modbus 客户端默认 unit id 1；地址 0 对应 HR0 / CO0，不需要 +1。'
              : current.protocol === 's7'
                ? 'S7 客户端连接端口后可直接读写 DB / M / I / Q；默认 rack 0 slot 1。'
                : 'OPC UA 客户端用上方 endpoint 连接，变量位于 Objects 下的实例文件夹。'}
          </p>
        </section>
      ) : null}

      {effectiveTab === 'tags' ? (
        <section className="panel">
          <TagsEditor server={current} onSave={save} saving={saving} />
        </section>
      ) : null}

      {effectiveTab === 'logs' ? (
        <section className="panel">
          <div className="tags-toolbar">
            <div>
              <strong>运行日志</strong>
              <p className="hint">
                {remote
                  ? `runtime 的 stdout/stderr，落盘在 ${site?.host} 的 ${site?.deploy_dir}/instances/${current.id}/runtime.log`
                  : `native runtime 的 stdout/stderr，落盘在 data/logs/${current.id}.log`}
              </p>
            </div>
            <button type="button" className="btn ghost" onClick={() => void loadLogs(serverId, 400)}>
              <Icon name="restart" size={15} /> {logsLoading ? '刷新中…' : '刷新'}
            </button>
          </div>
          <pre ref={logRef} className="log-view">
            {logs || '暂无日志'}
          </pre>
        </section>
      ) : null}
    </div>
  )
}
