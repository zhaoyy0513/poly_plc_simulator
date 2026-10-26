import { useEffect, useState } from 'react'
import { api } from '../api'
import { useApp } from '../store'
import {
  KIND_META,
  PROTOCOL_META,
  type CreateServerBody,
  type LinkOptions,
  type LinkTestResult,
  type Protocol,
  type Server,
  type ServerKind,
  type Site,
} from '../types'
import { DeployNotice } from './DeployNotice'
import { Icon } from './Icon'
import { Modal } from './Modal'

const BASE_PORT: Record<Protocol, number> = {
  modbus_tcp: 15020,
  s7: 15102,
  opcua: 15400,
}

/**
 * A link dials the target's own well-known port, not one of ours: 502 for
 * Modbus, 102 for S7. Switching protocol re-suggests it, because a port
 * chosen for the wrong handshake is the one thing a link cannot recover from.
 */
const LINK_DEFAULT_PORT: Record<Protocol, number> = {
  modbus_tcp: 502,
  s7: 102,
  opcua: 4840,
}

function suggestPort(protocol: Protocol, servers: Server[], usedPorts: number[] = []): number {
  const used = new Set([...servers.map((server) => server.port), ...usedPorts])
  let port = BASE_PORT[protocol]
  while (used.has(port)) port += 1
  return port
}

/**
 * Keep the raw text in state instead of a number.
 *
 * `<input type="number" value={0}>` cannot be emptied: deleting the last digit
 * sends `''`, `Number('')` becomes 0, and the controlled input puts the 0 back.
 * The user is then stuck until they type another digit. Parsing at submit time
 * lets the field be temporarily empty while typing.
 */
function parsePortText(text: string): number | null {
  const trimmed = text.trim()
  if (!/^\d+$/.test(trimmed)) return null
  const value = Number(trimmed)
  return Number.isInteger(value) && value >= 1 && value <= 65535 ? value : null
}

/** Re-probe a machine this stale, in case Python or Docker got installed. */
const PROBE_TTL_MS = 5 * 60 * 1000

const DEFAULT_LINK_OPTIONS: LinkOptions = {
  unit_id: 1,
  rack: 0,
  slot: 1,
  timeout_ms: 3000,
}

interface Props {
  open: boolean
  siteId: string
  site?: Site
  servers: Server[]
  initialKind?: ServerKind
  onClose: () => void
}

export function CreateServerModal({
  open,
  siteId,
  site,
  servers,
  initialKind = 'sim',
  onClose,
}: Props) {
  const system = useApp((state) => state.system)
  const createServer = useApp((state) => state.createServer)
  const probeSite = useApp((state) => state.probeSite)

  const remote = Boolean(site && !site.local)
  const [kind, setKind] = useState<ServerKind>(initialKind)
  const [protocol, setProtocol] = useState<Protocol>('modbus_tcp')
  const [name, setName] = useState('')
  const [bindIp, setBindIp] = useState('0.0.0.0')
  const [portText, setPortText] = useState('15020')
  const [endpointUrl, setEndpointUrl] = useState('')
  const [sampleTags, setSampleTags] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [probing, setProbing] = useState(false)

  // Link only.
  const [targetHost, setTargetHost] = useState('')
  const [targetPortText, setTargetPortText] = useState('502')
  const [linkOptions, setLinkOptions] = useState<LinkOptions>(DEFAULT_LINK_OPTIONS)
  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState<LinkTestResult | null>(null)

  // Ports on the target machine mean nothing to this one, so suggestions have
  // to come from the site's own probe rather than from /api/system.
  const busyPorts = remote ? (site?.last_probe?.ports_in_use ?? []) : (system?.used_ports ?? [])
  const link = kind === 'link'

  useEffect(() => {
    if (!open) return
    setKind(initialKind)
    setProtocol('modbus_tcp')
    setName('')
    setBindIp('0.0.0.0')
    setPortText(String(suggestPort('modbus_tcp', servers, busyPorts)))
    setEndpointUrl('')
    setSampleTags(false)
    setError(null)
    setTargetHost('')
    setTargetPortText('502')
    setLinkOptions(DEFAULT_LINK_OPTIONS)
    setTest(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialKind])

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
    setError(null)
    // The test result described the old protocol's handshake on the old port.
    setTest(null)
    if (link) {
      setTargetPortText(String(LINK_DEFAULT_PORT[next]))
      // An S7 link has no raw data areas to fall back on, so its page lives
      // or dies by the tag table - seed it by default.
      setSampleTags(next === 's7')
    } else {
      setPortText(String(suggestPort(next, servers, busyPorts)))
    }
  }

  const chooseKind = (next: ServerKind) => {
    // Clicking the active segment must not wipe what the user already typed -
    // in particular the port, which the sim side suggests per protocol.
    if (next === kind) return
    setKind(next)
    setError(null)
    setTest(null)
    if (next === 'link') {
      // Links support only the two protocols that have a client. Falling back
      // here - rather than trusting whatever the sim side last selected -
      // keeps the picker from showing a choice the backend would refuse.
      const supported: Protocol = protocol === 's7' ? 's7' : 'modbus_tcp'
      setProtocol(supported)
      setTargetPortText(String(LINK_DEFAULT_PORT[supported]))
      setSampleTags(supported === 's7')
    } else {
      // Coming back from a link: the listen-port suggestion still carries the
      // base port of whatever protocol the sim side had before.
      setPortText(String(suggestPort(protocol, servers, busyPorts)))
    }
  }

  /**
   * "测试连接" is not a nicety here: the backend refuses to save a link that
   * cannot be reached, so a successful test is what makes 创建 enabled at all.
   */
  const runTest = async (): Promise<LinkTestResult | null> => {
    const host = targetHost.trim()
    if (!host) {
      setError('先填目标地址')
      return null
    }
    const targetPort = parsePortText(targetPortText)
    if (targetPort === null) {
      setError('端口需为 1–65535')
      return null
    }
    setTesting(true)
    setError(null)
    try {
      const result = await api.linkTest(siteId, host, targetPort, linkOptions, protocol)
      setTest(result)
      return result
    } catch (testError) {
      const failed: LinkTestResult = {
        ok: false,
        error: testError instanceof Error ? testError.message : String(testError),
        latency_ms: null,
      }
      setTest(failed)
      return failed
    } finally {
      setTesting(false)
    }
  }

  const submit = async () => {
    const targetPort = parsePortText(targetPortText)
    const listenPort = parsePortText(portText)
    if (link && targetPort === null) {
      setError('端口需为 1–65535')
      return
    }
    if (!link && listenPort === null) {
      setError('端口需为 1–65535')
      return
    }

    const finalName =
      name.trim() ||
      (link
        ? `${targetHost.trim()}:${targetPortText.trim() || '?'}`
        : `${PROTOCOL_META[protocol].label} ${servers.filter((server) => server.protocol === protocol).length + 1}`)

    if (link) {
      const result = await runTest()
      if (!result || !result.ok) return
    }

    const body: CreateServerBody = link
      ? {
          name: finalName,
          kind: 'link',
          protocol,
          bind_ip: '0.0.0.0',
          port: targetPort as number,
          target_host: targetHost.trim(),
          target_port: targetPort as number,
          target_options: linkOptions,
          // Modbus links are refused tags outright; S7 ones use the table as
          // their only view of the far side.
          sample_tags: protocol === 's7' ? sampleTags : false,
        }
      : {
          name: finalName,
          protocol,
          bind_ip: remote ? '0.0.0.0' : bindIp,
          port: listenPort as number,
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
  const blocked = !link && remote && site?.deploy && !site.deploy.ready && site.deploy.carrier !== 'unknown'
  const linkReady =
    link && targetHost.trim().length > 0 && parsePortText(targetPortText) !== null

  // A sim on a remote site runs whatever we install there; a link is dialed by
  // the control plane itself, so it is limited to what *this* machine can
  // import - and, for now, to the two protocols that have a client at all.
  const protocolSupported = (item: Protocol) =>
    link
      ? item !== 'opcua' && (system?.protocols.includes(item) ?? false)
      : remote || (system?.protocols.includes(item) ?? false)

  const protocolPicker = (
    <div className="field">
      <span>协议</span>
      <div className="protocol-pick">
        {(Object.keys(PROTOCOL_META) as Protocol[]).map((item) => {
          const supported = protocolSupported(item)
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
              <small>
                {supported
                  ? PROTOCOL_META[item].hint
                  : link && item === 'opcua'
                    ? '连接实例暂不支持'
                    : '本机不可用'}
              </small>
            </button>
          )
        })}
      </div>
    </div>
  )

  return (
    <Modal
      open={open}
      title="新建实例"
      subtitle={
        link
          ? `平台作为客户端连接一台已经在跑的 ${PROTOCOL_META[protocol].label} server，不部署、不占端口。`
          : remote
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
            disabled={busy || probing || Boolean(blocked) || (link && !linkReady)}
            onClick={() => void submit()}
          >
            {busy ? (link ? '连接中…' : remote ? '部署中…' : '创建中…') : link ? '测试并连接' : '创建并启动'}
          </button>
        </>
      }
    >
      {/* Two segments, not a dropdown: the two kinds are different enough that
          hiding one behind a menu would make the difference invisible. */}
      <div className="field">
        <span>这个实例是什么</span>
        <div className="kind-pick">
          {(Object.keys(KIND_META) as ServerKind[]).map((item) => (
            <button
              key={item}
              type="button"
              className={`pick${kind === item ? ' active' : ''}${item === 'link' ? ' link' : ''}`}
              onClick={() => chooseKind(item)}
            >
              <i style={{ background: KIND_META[item].accent }} />
              <strong>
                <Icon name={KIND_META[item].icon} size={14} /> {KIND_META[item].label}
              </strong>
              <small>
                {item === 'sim'
                  ? '平台部署并运行，进程由平台管理'
                  : '连接别人正在跑的 server，读写直接落到那台机器'}
              </small>
            </button>
          ))}
        </div>
      </div>

      {link ? (
        <>
          {protocolPicker}

          <div className="row">
            <label className="field grow">
              <span>名称</span>
              <input
                className="input"
                value={name}
                placeholder="留空用「地址:端口」命名"
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label className="field port-field">
              <span>端口</span>
              <input
                className="input mono"
                type="number"
                min={1}
                max={65535}
                inputMode="numeric"
                value={targetPortText}
                onChange={(event) => {
                  setTargetPortText(event.target.value)
                  setTest(null)
                }}
              />
            </label>
          </div>

          <label className="field">
            <span>目标地址</span>
            <div className="link-target-row">
              <input
                className="input mono"
                value={targetHost}
                placeholder="例如 10.102.129.18"
                onChange={(event) => {
                  setTargetHost(event.target.value)
                  setTest(null)
                }}
              />
              <button
                type="button"
                className="btn ghost"
                disabled={testing || !targetHost.trim()}
                onClick={() => void runTest()}
              >
                <Icon name="plug" size={15} /> {testing ? '测试中…' : '测试连接'}
              </button>
            </div>
            <small className="hint">
              平台会从本机直接连这个地址，所以填的必须是<strong>控制平面所在机器可达</strong>的 IP。
            </small>
          </label>

          <div className="row">
            {protocol === 's7' ? (
              <>
                <label className="field small-field">
                  <span>Rack</span>
                  <input
                    className="input mono"
                    type="number"
                    min={0}
                    max={7}
                    value={linkOptions.rack}
                    onChange={(event) => {
                      setLinkOptions({ ...linkOptions, rack: Number(event.target.value) })
                      setTest(null)
                    }}
                  />
                </label>
                <label className="field small-field">
                  <span>Slot</span>
                  <input
                    className="input mono"
                    type="number"
                    min={0}
                    max={31}
                    value={linkOptions.slot}
                    onChange={(event) => {
                      setLinkOptions({ ...linkOptions, slot: Number(event.target.value) })
                      setTest(null)
                    }}
                  />
                </label>
              </>
            ) : (
              <label className="field small-field">
                <span>Unit ID</span>
                <input
                  className="input mono"
                  type="number"
                  min={0}
                  max={255}
                  value={linkOptions.unit_id}
                  onChange={(event) => {
                    setLinkOptions({ ...linkOptions, unit_id: Number(event.target.value) })
                    setTest(null)
                  }}
                />
              </label>
            )}
            <label className="field small-field">
              <span>超时 (ms)</span>
              <input
                className="input mono"
                type="number"
                min={100}
                max={60000}
                step={100}
                value={linkOptions.timeout_ms}
                onChange={(event) => {
                  setLinkOptions({ ...linkOptions, timeout_ms: Number(event.target.value) })
                  setTest(null)
                }}
              />
            </label>
          </div>

          {protocol === 's7' ? (
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
                  S7 连接没有四个原始数据区可看，变量表就是它的全部视图。示例地址多半和对方 PLC
                  对不上，连上后去「标签表」改成真实地址即可。
                </small>
              </span>
            </label>
          ) : null}

          {test ? (
            <div className={`link-test${test.ok ? ' ok' : ' bad'}`}>
              <Icon name={test.ok ? 'check' : 'alert'} size={16} />
              <span>
                {test.ok
                  ? test.warning
                    ? `设备有响应，但拒绝了探测（${test.warning}）；连接仍会建立`
                    : `连接正常，往返 ${test.latency_ms ?? 0} ms`
                  : `连不上：${test.error ?? '未知原因'}`}
              </span>
            </div>
          ) : (
            <p className="hint">
              创建前会先连一次：连不上的地址不会被保存，不会出现一张从出生就是红的卡片。
            </p>
          )}

          <div className="notice">
            <Icon name="bolt" size={15} />
            <span>
              {protocol === 's7'
                ? 'S7 连接实例按 DB / M / I / Q 地址直接读写那台 PLC，没有仿真值；看到的值只由那台机器自己的 client 或这里的控制台写入产生。'
                : '连接实例没有标签表、没有仿真值，只有四个原始数据区；值只由那台机器自己的 client 或这里的控制台写入产生。'}
              写入会<strong>直接落到对方设备</strong>，页面会要求二次确认。
            </span>
          </div>
        </>
      ) : (
        <>
          {remote ? (
            <DeployNotice plan={site?.deploy ?? null} host={site?.host ?? ''} probing={probing} />
          ) : null}

          {protocolPicker}

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
                inputMode="numeric"
                value={portText}
                onChange={(event) => setPortText(event.target.value)}
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
        </>
      )}

      {error ? (
        <div className="error-box">
          <Icon name="alert" size={16} /> {error}
        </div>
      ) : null}
    </Modal>
  )
}
