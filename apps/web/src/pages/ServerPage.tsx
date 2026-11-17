import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { api, copyText } from '../api'
import { ClientConnectionsModal } from '../components/ClientConnectionsModal'
import { DataAreaView } from '../components/DataAreaView'
import { useConfirm } from '../components/ConfirmModal'
import { Icon } from '../components/Icon'
import { StatusPill } from '../components/StatusPill'
import { Modal } from '../components/Modal'
import { TagsEditor } from '../components/TagsEditor'
import { ValueTable } from '../components/ValueTable'
import { usePolling } from '../hooks/usePolling'
import { useApp } from '../store'
import {
  KIND_META,
  PROTOCOL_META,
  connectionText,
  isLink,
  isRunning,
  linkParamsText,
  uptime,
  type Tag,
} from '../types'

type Tab = 'area' | 'values' | 'tags' | 'logs'

export function ServerPage() {
  const { serverId = '' } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
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
  const confirm = useConfirm()
  const notify = useApp((state) => state.notify)

  // Modbus users usually want the raw register view first: it is what a real
  // client sees, including addresses that have no tag attached.
  const [tab, setTab] = useState<Tab>('area')
  const [saving, setSaving] = useState(false)
  const [powerBusy, setPowerBusy] = useState(false)
  // Modbus map size, edited in place: `null` = not editing. Empty string is a
  // real choice here ("the whole 16-bit space"), so it cannot mean "closed".
  const [mapEdit, setMapEdit] = useState<string | null>(null)
  const [mapBusy, setMapBusy] = useState(false)
  const [connectionsOpen, setConnectionsOpen] = useState(false)
  const logRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    if (serverId) void loadServer(serverId)
  }, [serverId, loadServer])

  const current = server?.id === serverId ? server : null
  // `enabled` is the desired state; a stale running status from a request that
  // raced the stop must not keep the data-area poller alive.
  const running = current ? isRunning(current) : false

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
  const link = isLink(current)
  const from = (location.state as { from?: unknown } | null)?.from
  const fromInstances =
    typeof from === 'string' && from.startsWith('/instances') ? from : null
  const backTo = fromInstances ?? `/sites/${current.site_id}`
  const lanIp =
    system?.addresses.find((address) => address !== '0.0.0.0' && address !== '127.0.0.1') ??
    '127.0.0.1'
  // A remote instance is reachable at the target's address, not at ours; a
  // link instance is reachable at *its* target's address.
  const endpoint = connectionText(current, remote ? (site?.host ?? lanIp) : lanIp, site?.host)
  const localEndpoint =
    current.protocol === 'opcua'
      ? `opc.tcp://127.0.0.1:${current.port}`
      : `127.0.0.1:${current.port}`
  const startedAt = running ? (values?.started_at ?? current.started_at) : null
  // Same reasoning as startedAt, and the same trap: `current.latency_ms` is
  // whatever the last successful round trip happened to be, which for a link
  // the user just switched off is a number about a connection that no longer
  // exists. `values` can also be stale - it stops being refreshed the moment
  // the instance stops - so gate on `running` rather than on nullness.
  const latency = running ? (values?.latency_ms ?? current.latency_ms ?? null) : null
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

  const saveMapSize = async () => {
    if (!current || mapEdit === null) return
    const raw = mapEdit.trim()
    const value = raw ? Number(raw) : null
    if (value !== null && (!Number.isInteger(value) || value < 1 || value > 65536)) {
      notify('error', '有效寄存器数需为 1–65536，留空表示整段 0–65535')
      return
    }
    setMapBusy(true)
    try {
      // Saving restarts the instance: the data plane is reallocated, so values
      // past the new end are gone and everything else starts from 0 again.
      await updateServer(current.id, {
        options: { ...current.options, register_count: value },
      })
      setMapEdit(null)
      notify('ok', value ? `每区改为 ${value} 个地址（0–${value - 1}）` : '改回整段 0–65535')
    } catch (error) {
      notify('error', error instanceof Error ? error.message : String(error))
    } finally {
      setMapBusy(false)
    }
  }

  const toggleEnabled = async () => {
    if (current.enabled) {
      const target = `${current.target_host}:${current.target_port ?? current.port}`
      const message = link
        ? `停用连接「${current.name}」会断开到 ${target} 的连接，重新启用后会再次连接。继续？`
        : `停用「${current.name}」会停止进程并断开所有 client，端口 ${current.port} 会被释放。继续？`
      const ok = await confirm({
        title: link ? '停用连接' : '停用实例',
        message,
        confirmText: '停用',
        danger: true,
      })
      if (!ok) return
    }
    setPowerBusy(true)
    await updateServer(current.id, { enabled: !current.enabled })
    setPowerBusy(false)
  }

  const save = async (tags: Tag[]) => {
    setSaving(true)
    const ok = await saveTags(serverId, tags)
    setSaving(false)
    return ok
  }

  // Two reset paths, offered in the dialog: in-place zeroing keeps the process
  // and every client connection (needs a current runtime); restart drops all
  // clients but works on any runtime, including ones adopted from before the
  // reset endpoint existed.
  const [resetOpen, setResetOpen] = useState(false)
  const [resetBusy, setResetBusy] = useState<'inplace' | 'restart' | null>(null)

  const resetInPlace = async () => {
    if (!current) return
    setResetBusy('inplace')
    try {
      await api.resetServer(current.id)
      notify('ok', '数据区已清零，client 连接保持')
      void refreshValues(serverId)
      setResetOpen(false)
    } catch (error) {
      notify('error', error instanceof Error ? error.message : String(error))
    } finally {
      setResetBusy(null)
    }
  }

  const restartAndClear = async () => {
    if (!current) return
    setResetBusy('restart')
    try {
      await restartServer(current.id)
      notify('ok', '实例已重启，数据已清零（client 需重连）')
      setResetOpen(false)
    } catch (error) {
      notify('error', error instanceof Error ? error.message : String(error))
    } finally {
      setResetBusy(null)
    }
  }

  return (
    <div className="page">
      <nav className="crumbs">
        <Link to={backTo} className="crumb-link">
          <Icon name="arrowLeft" size={14} /> {fromInstances ? '返回全部实例' : '返回 Site'}
        </Link>
      </nav>

      <header className="page-head server-head">
        <div>
          <span className="eyebrow">
            <span className="proto-badge small" style={{ '--accent': meta.accent } as CSSProperties}>
              {meta.short}
            </span>
            {meta.label}
            <span
              className="kind-chip"
              style={{ '--accent': KIND_META[link ? 'link' : 'sim'].accent } as CSSProperties}
              title={KIND_META[link ? 'link' : 'sim'].hint}
            >
              <Icon name={KIND_META[link ? 'link' : 'sim'].icon} size={12} />
              {KIND_META[link ? 'link' : 'sim'].short}
            </span>
            <StatusPill status={current.status} error={current.error} enabled={current.enabled} />
          </span>
          <h1>{current.name}</h1>
          <button type="button" className="endpoint big mono" onClick={() => void copyEndpoint()}>
            <Icon name="plug" size={14} />
            {endpoint}
            <Icon name="copy" size={13} />
          </button>
          {link ? (
            <small className="endpoint-hint">
              平台作为客户端连接 <span className="mono">
                {current.target_host}:{current.target_port ?? current.port}
              </span>
              ，{linkParamsText(current)}，这里的每次读写都是一次真实往返
            </small>
          ) : remote ? (
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
            className={current.enabled ? 'btn ghost' : 'btn primary'}
            onClick={() => void toggleEnabled()}
            disabled={powerBusy}
          >
            <Icon name="power" size={15} />{' '}
            {powerBusy
              ? '处理中…'
              : current.enabled
                ? link
                  ? '停用连接'
                  : '停用'
                : link
                  ? '启用连接'
                  : '启用'}
          </button>
          {!link && current.enabled ? (
            <>
              <button
                type="button"
                className="btn ghost"
                onClick={() => setResetOpen(true)}
                disabled={!running || resetBusy !== null}
                title="把四个数据区清零，进程与 client 连接保持不变"
              >
                <Icon name="restart" size={15} /> 重置数据区
              </button>
              <button
                type="button"
                className="btn ghost"
                onClick={() => void restartServer(current.id)}
                disabled={current.status === 'starting'}
              >
                <Icon name="restart" size={15} /> 重启
              </button>
            </>
          ) : null}
          <button
            type="button"
            className="btn danger"
            onClick={() => {
              void (async () => {
                const ok = await confirm({
                  title: '删除实例',
                  message: `确定删除实例「${current.name}」？`,
                  confirmText: '删除',
                  danger: true,
                })
                if (ok) {
                  void deleteServer(current).then(() => navigate(backTo, { replace: true }))
                }
              })()
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
          {current.enabled ? (
            <button
              type="button"
              className="btn tiny"
              onClick={() => void restartServer(current.id)}
            >
              {link ? '重新连接' : '重试'}
            </button>
          ) : null}
        </div>
      ) : null}

      {link ? (
        <div className="link-banner">
          <Icon name="plug" size={16} />
          <span>
            这是<strong>连接</strong>实例：平台没有在这台机器上部署任何进程，只是作为客户端连着
            <span className="mono"> {current.target_host}:{current.target_port ?? current.port}</span>。
            页面里看到的值来自那台机器，写入也会直接落到它上面——没有撤销，请先确认地址。
          </span>
        </div>
      ) : null}

      {!link && current.bind_ip !== '0.0.0.0' ? (
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
          <small>{link ? '连接目标' : '监听'}</small>
          <strong className="mono">{link ? current.target_host ?? '—' : current.bind_ip}</strong>
          <span className="mono">:{current.port}</span>
        </div>
        <div className="stat">
          {isModbus ? (
            <>
              <small>数据区</small>
              <strong>4</strong>
              {/* A link's map is the far device's business: only a sim
                  instance knows how many points it exposes, and only a sim
                  instance can be told to change it. */}
              {link ? (
                <span> 个</span>
              ) : mapEdit === null ? (
                <span>
                  {' '}
                  个 · 每区 {current.options.register_count ?? 65536} 点{' '}
                  <button
                    type="button"
                    className="btn tiny"
                    onClick={() => setMapEdit(String(current.options.register_count ?? 65536))}
                    title="超出这个范围的读写会回异常码 02；留空表示整段 0–65535"
                  >
                    改
                  </button>
                </span>
              ) : (
                <span className="map-edit">
                  <input
                    className="input mono"
                    type="number"
                    min={1}
                    max={65536}
                    inputMode="numeric"
                    value={mapEdit}
                    autoFocus
                    placeholder="留空=65536"
                    onChange={(event) => setMapEdit(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') void saveMapSize()
                      if (event.key === 'Escape') setMapEdit(null)
                    }}
                  />
                  <button
                    type="button"
                    className="btn tiny"
                    disabled={mapBusy}
                    onClick={() => void saveMapSize()}
                  >
                    {mapBusy ? '保存中…' : '保存'}
                  </button>
                  <button
                    type="button"
                    className="btn ghost tiny"
                    disabled={mapBusy}
                    onClick={() => setMapEdit(null)}
                  >
                    取消
                  </button>
                </span>
              )}
            </>
          ) : (
            <>
              <small>变量</small>
              <strong>{current.tags.length}</strong>
            </>
          )}
        </div>
        {/* The two below describe a live connection. A stopped instance has
            neither, so they read "—" instead of the last value before the
            stop - which is what made a disabled link look like it was still
            talking to its target. */}
        <div className="stat">
          <small>{link ? '往返延迟' : '客户端连接'}</small>
          {link || !isModbus ? (
            <strong>
              {link
                ? latency != null
                  ? `${latency} ms`
                  : '—'
                : (values?.clients ?? current.clients)}
            </strong>
          ) : (
            <button
              type="button"
              className="stat-link"
              aria-label={`查看客户端连接（${values?.clients ?? current.clients}）`}
              title="查看客户端连接"
              disabled={!running}
              onClick={() => setConnectionsOpen(true)}
            >
              {values?.clients ?? current.clients}
            </button>
          )}
          {link ? (
            <span className="stat-note">{latency != null ? '最近一次探测' : '未连接'}</span>
          ) : null}
        </div>
        <div className="stat">
          <small>连接时长</small>
          <strong>{uptime(startedAt)}</strong>
          <span className="stat-note">{running ? '本次连接' : '未连接'}</span>
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
          <Icon name={link ? 'plug' : 'terminal'} size={15} /> {link ? '连接' : '日志'}
        </button>
        <span className="tab-note">
          {running
            ? effectiveTab === 'area'
              ? link
                ? '实时刷新 1s · 双击单元格写入（会落到对方设备）'
                : '实时刷新 1s · 双击单元格写入'
              : effectiveTab === 'values'
                ? '实时刷新 1s（页面隐藏时暂停）'
                : effectiveTab === 'logs'
                  ? link
                    ? '连接历史 2.5s 刷新'
                    : '日志 2.5s 刷新'
                  : '保存会重启实例'
            : link
              ? '未连接'
              : '实例未运行'}
        </span>
      </div>

      {effectiveTab === 'area' && isModbus ? (
        <section className="panel">
          <DataAreaView key={current.id} server={current} running={Boolean(running)} />
        </section>
      ) : null}

      {effectiveTab === 'values' ? (
        <section className="panel">
          {link && current.tags.length === 0 ? (
            <div className="notice">
              <Icon name="plug" size={15} />
              <span>
                连接实例没有原始数据区，变量表就是唯一视图。请在「标签表」填写对方真实地址或
                NodeId；平台不会自动补演示变量。
              </span>
            </div>
          ) : null}
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
              <strong>{link ? '连接历史' : '运行日志'}</strong>
              <p className="hint">
                {link
                  ? '连接实例没有进程可看，这里是平台到那台机器的连接记录：每次探测、每次失败的原因和往返延迟。'
                  : remote
                    ? `runtime 的 stdout/stderr，落盘在 ${site?.host} 的 ${site?.deploy_dir}/instances/${current.id}/runtime.log`
                    : `native runtime 的 stdout/stderr，落盘在 data/logs/${current.id}.log`}
              </p>
            </div>
            <button type="button" className="btn ghost" onClick={() => void loadLogs(serverId, 400)}>
              <Icon name="restart" size={15} /> {logsLoading ? '刷新中…' : '刷新'}
            </button>
          </div>
          <pre ref={logRef} className="log-view">
            {logs || (link ? '暂无连接记录' : '暂无日志')}
          </pre>
        </section>
      ) : null}

      <ClientConnectionsModal
        open={connectionsOpen}
        serverId={serverId}
        onClose={() => setConnectionsOpen(false)}
      />
      <Modal
        open={resetOpen}
        title="重置数据区"
        subtitle="两种方式都会把数据清零，且无法撤销"
        width={560}
        onClose={() => setResetOpen(false)}
      >
        <div className="kind-pick">
          <button
            type="button"
            className="pick"
            disabled={resetBusy !== null}
            onClick={() => void resetInPlace()}
          >
            <strong>清零 · 保持连接</strong>
            <small>
              进程和 client 连接保持不变，寄存器/变量立即回 0。要求实例已运行最新版 runtime。
            </small>
          </button>
          <button
            type="button"
            className="pick"
            disabled={resetBusy !== null}
            onClick={() => void restartAndClear()}
          >
            <strong>重启并清零</strong>
            <small>重启实例进程，断开所有 client 后从零开始。适用于旧版 runtime 或想彻底重来。</small>
          </button>
        </div>
        <p className="hint">两种操作都无法撤销；link 连接实例的数据在对方设备，不支持重置。</p>
      </Modal>
    </div>
  )
}
