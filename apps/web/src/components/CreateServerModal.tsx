import { useEffect, useState } from 'react'
import { useApp } from '../store'
import { PROTOCOL_META, type CreateServerBody, type Protocol, type Server, type Site } from '../types'
import { DeployNotice } from './DeployNotice'
import { Icon } from './Icon'
import { Modal } from './Modal'

const BASE_PORT: Record<Protocol, number> = {
  modbus_tcp: 15020,
  s7: 15102,
  opcua: 15400,
}

function suggestPort(protocol: Protocol, servers: Server[], usedPorts: number[] = []): number {
  const used = new Set([...servers.map((server) => server.port), ...usedPorts])
  let port = BASE_PORT[protocol]
  while (used.has(port)) port += 1
  return port
}

/** Re-probe a machine this stale, in case Python or Docker got installed. */
const PROBE_TTL_MS = 5 * 60 * 1000

interface Props {
  open: boolean
  siteId: string
  site?: Site
  servers: Server[]
  onClose: () => void
}

export function CreateServerModal({ open, siteId, site, servers, onClose }: Props) {
  const system = useApp((state) => state.system)
  const createServer = useApp((state) => state.createServer)
  const probeSite = useApp((state) => state.probeSite)

  const remote = Boolean(site && !site.local)
  const [protocol, setProtocol] = useState<Protocol>('modbus_tcp')
  const [name, setName] = useState('')
  const [bindIp, setBindIp] = useState('0.0.0.0')
  const [port, setPort] = useState(15020)
  const [endpointUrl, setEndpointUrl] = useState('')
  const [sampleTags, setSampleTags] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [probing, setProbing] = useState(false)

  // Ports on the target machine mean nothing to this one, so suggestions have
  // to come from the site's own probe rather than from /api/system.
  const busyPorts = remote ? (site?.last_probe?.ports_in_use ?? []) : (system?.used_ports ?? [])

  useEffect(() => {
    if (!open) return
    setProtocol('modbus_tcp')
    setName('')
    setBindIp('0.0.0.0')
    setPort(suggestPort('modbus_tcp', servers, busyPorts))
    setEndpointUrl('')
    setSampleTags(false)
    setError(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // The dialog decides nothing about deployment - it just makes sure the
  // answer on screen is current before the user commits to creating anything.
  useEffect(() => {
    if (!open || !site || site.local) return
    const checked = site.last_probe_at ? Date.parse(site.last_probe_at) : 0
    if (checked && Date.now() - checked < PROBE_TTL_MS) return
    setProbing(true)
    void probeSite(site.id).finally(() => setProbing(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, site?.id])

  const chooseProtocol = (next: Protocol) => {
    setProtocol(next)
    setPort(suggestPort(next, servers, busyPorts))
    setError(null)
  }

  const submit = async () => {
    const finalName =
      name.trim() ||
      `${PROTOCOL_META[protocol].label} ${servers.filter((server) => server.protocol === protocol).length + 1}`
    const body: CreateServerBody = {
      name: finalName,
      protocol,
      bind_ip: remote ? '0.0.0.0' : bindIp,
      port: Number(port),
      options: protocol === 'opcua' ? { endpoint_url: endpointUrl.trim() || null } : {},
      sample_tags: protocol === 'modbus_tcp' ? false : sampleTags,
    }
    setBusy(true)
    setError(null)
    try {
      await createServer(siteId, body)
      onClose()
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError))
    } finally {
      setBusy(false)
    }
  }

  const addresses = system?.addresses ?? ['0.0.0.0']
  const blocked = remote && site?.deploy && !site.deploy.ready && site.deploy.carrier !== 'unknown'

  return (
    <Modal
      open={open}
      title="新建模拟器实例"
      subtitle={
        remote
          ? `实例会部署到 ${site?.host} 上并立即启动。`
          : '每个实例是独立进程，拥有自己的端口和数据面。'
      }
      width={680}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            {blocked ? '关闭' : '取消'}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={busy || probing || Boolean(blocked)}
            onClick={() => void submit()}
          >
            {busy ? (remote ? '部署中…' : '创建中…') : '创建并启动'}
          </button>
        </>
      }
    >
      {remote ? (
        <DeployNotice plan={site?.deploy ?? null} host={site?.host ?? ''} probing={probing} />
      ) : null}

      <div className="field">
        <span>协议</span>
        <div className="protocol-pick">
          {(Object.keys(PROTOCOL_META) as Protocol[]).map((item) => {
            // A remote site's capabilities come from what we install there,
            // not from what this machine happens to import.
            const supported = remote || (system?.protocols.includes(item) ?? false)
            return (
              <button
                key={item}
                type="button"
                className={`pick${protocol === item ? ' active' : ''}`}
                disabled={!supported}
                onClick={() => chooseProtocol(item)}
              >
                <i style={{ background: PROTOCOL_META[item].accent }} />
                <strong>{PROTOCOL_META[item].label}</strong>
                <small>{supported ? PROTOCOL_META[item].hint : '本机不可用'}</small>
              </button>
            )
          })}
        </div>
      </div>

      <div className="row">
        <label className="field grow">
          <span>名称</span>
          <input
            className="input"
            value={name}
            placeholder="留空自动命名"
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        {remote ? null : (
          <label className="field">
            <span>监听地址</span>
            <select className="input" value={bindIp} onChange={(event) => setBindIp(event.target.value)}>
              {addresses.map((address) => (
                <option key={address} value={address}>
                  {address === '0.0.0.0' ? '0.0.0.0（所有网卡）' : address}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="field port-field">
          <span>端口</span>
          <input
            className="input mono"
            type="number"
            min={1}
            max={65535}
            value={port}
            onChange={(event) => setPort(Number(event.target.value))}
          />
        </label>
      </div>

      {remote ? (
        <p className="hint bind-hint">
          监听地址固定 <span className="mono">0.0.0.0</span>（{site?.host} 的所有网卡），
          端口不能和该机器上已在用的端口冲突。
        </p>
      ) : (
        <p className="hint bind-hint">
          监听地址选 <span className="mono">0.0.0.0</span> 时，本机可用
          <span className="mono"> 127.0.0.1 </span>、其他机器可用局域网 IP；选具体网卡时只有该地址能连。
        </p>
      )}

      {protocol === 'modbus_tcp' ? (
        <p className="hint">
          Modbus 实例只提供 01/02/03/04 四个原始数据区，地址由连接的 client 自行定义；创建后可在实例页双击单元格预置数值。
        </p>
      ) : (
        <label className="check-row">
          <input
            type="checkbox"
            className="check"
            checked={sampleTags}
            onChange={(event) => setSampleTags(event.target.checked)}
          />
          <span>
            创建示例变量（温度 / 压力 / 计数器 / 运行状态）
            <small className="hint">
              不勾选则创建空地址空间，所有值默认 0；之后可在「标签表」里添加变量。
            </small>
          </span>
        </label>
      )}

      {protocol === 'opcua' ? (
        <label className="field">
          <span>Endpoint URL（可选）</span>
          <input
            className="input mono"
            value={endpointUrl}
            placeholder={
              remote
                ? `留空自动使用 ${site?.host ?? '目标机 IP'}，例如 opc.tcp://${site?.host ?? '10.0.0.1'}:4840`
                : '留空自动使用局域网 IP，例如 opc.tcp://192.168.1.10:4840'
            }
            onChange={(event) => setEndpointUrl(event.target.value)}
          />
          <small className="hint">
            客户端拿到的是这个 URL，必须从客户端机器可达；端口映射时尤其要显式填写。
          </small>
        </label>
      ) : null}

      <div className="notice">
        <Icon name="bolt" size={15} />
        <span>
          {remote
            ? `创建会先做一次性准备（上传源码、装依赖，首次约 1 分钟），之后每个实例只起一个进程；实例跑在 ${site?.host} 上，本机不占用端口。`
            : protocol === 'modbus_tcp'
              ? '创建后立即启动；端口在整机范围内不能重复（本机模式下多个 site 共用端口空间）。数据区写入立即生效，不会重启实例。'
              : '创建后立即启动；端口在整机范围内不能重复（本机模式下多个 site 共用端口空间）。之后修改标签表会自动重启该实例（1–3 秒）。'}
        </span>
      </div>

      {error ? (
        <div className="error-box">
          <Icon name="alert" size={16} /> {error}
        </div>
      ) : null}
    </Modal>
  )
}
