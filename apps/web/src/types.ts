export type Protocol = 'modbus_tcp' | 's7' | 'opcua'
export type DataType = 'bool' | 'int16' | 'uint16' | 'int32' | 'uint32' | 'float32' | 'float64'
export type SimMode = 'static' | 'sine' | 'random' | 'ramp' | 'toggle'
export type WordOrder = 'big' | 'little'
export type ServerStatus = 'running' | 'starting' | 'error' | 'stopped'
/**
 * `sim` = the platform deploys and runs the server itself.
 * `link` = the platform is a *client* of a server somebody else is running:
 * same page, same data areas, but every read and write is a real round trip
 * to a machine we do not control.
 */
export type ServerKind = 'sim' | 'link'

export interface SystemInfo {
  os: string
  arch: string
  python: string
  hostname: string
  addresses: string[]
  protocols: Protocol[]
  protocol_notes: Record<string, string>
  data_dir: string
  used_ports: number[]
}

export interface SiteProbe {
  ok: boolean
  error: string | null
  os: string
  arch: string
  distro: string
  python: string
  python_bin: string
  python_candidates: string[]
  python_ok: boolean
  package_manager: string
  docker: string
  docker_ok: boolean
  uv: string
  deploy_dir: string
  deploy_ok: boolean
  ports_in_use: number[]
  checked_at: string
}

/**
 * How the platform will run instances on a machine.
 *
 * Reported, never chosen: if it says "自动安装 Python 3.11" the user's options
 * are to accept it or to fix the machine themselves and come back.
 */
export interface DeployPlan {
  carrier: 'native' | 'uv' | 'docker' | 'unsupported' | 'unknown' | string
  label: string
  reason: string
  action: string
  python: string
  ready: boolean
}

/** A site is one target machine. An empty `host` means the platform box itself. */
export interface Site {
  id: string
  name: string
  description: string
  created_at: string
  server_count: number
  /** How many instances of each protocol this site holds. */
  protocol_counts?: Record<Protocol, number>
  host: string
  ssh_port: number
  ssh_user: string
  ssh_configured: boolean
  deploy_dir: string
  python_bin: string
  local: boolean
  last_probe_at: string
  last_probe: Partial<SiteProbe>
  deploy: DeployPlan
}

export interface SiteAccessDraft {
  host: string
  ssh_port: number
  ssh_user: string
  ssh_password: string
  deploy_dir: string
}

export const DEFAULT_ACCESS: SiteAccessDraft = {
  host: '',
  ssh_port: 22,
  ssh_user: '',
  ssh_password: '',
  deploy_dir: '~/.poly-sim',
}

export interface SimSpec {
  mode: SimMode
  min: number
  max: number
  period_ms: number
  step: number
}

export interface Tag {
  id?: string
  position?: number
  name: string
  data_type: DataType
  area: string
  address: number
  bit: number | null
  db_number: number | null
  word_order: WordOrder
  writable: boolean
  description: string
  sim: SimSpec
  /**
   * OPC UA only: the full NodeId on the far side (`ns=4;s=Main.Temp`). Empty
   * means "derive it from the link's namespace index plus the tag name", which
   * is how our own server names variables - so a link to a simulator needs no
   * address at all.
   */
  node_id?: string | null
}

export interface ServerOptions {
  unit_id: number
  rack: number
  slot: number
  endpoint_url: string | null
  advertise_host: string | null
  namespace: string | null
  /**
   * Modbus only: points per area, from address 0. `null` = the whole 16-bit
   * space (0-65535). A number makes the instance refuse addresses past its map
   * with ILLEGAL_ADDRESS, like a real device.
   */
  register_count: number | null
}

/** How to talk to a server somebody else is running. Link instances only. */
export interface LinkOptions {
  unit_id: number
  /** S7 only: rack and slot of the PLC on the far side. */
  rack: number
  slot: number
  timeout_ms: number
  /**
   * OPC UA only: which namespace the bare tag names live in. The index is
   * assigned by the far server, so it cannot be guessed - 2 is what *our* own
   * server ends up on, and the probe result lists the far side's table by name.
   */
  namespace_index: number
}

export interface Server {
  id: string
  site_id: string
  name: string
  kind: ServerKind
  protocol: Protocol
  bind_ip: string
  port: number
  runtime: string
  enabled: boolean
  options: ServerOptions
  config_version: number
  created_at: string
  status: ServerStatus
  error: string | null
  endpoint: string | null
  clients: number
  started_at: number | null
  latency_ms?: number | null
  /** Link only: the machine we connect to. `port` mirrors `target_port`. */
  target_host?: string
  target_port?: number
  target_options?: LinkOptions
  tags: Tag[]
}

export interface RuntimeState {
  status: ServerStatus
  error: string | null
  values: Record<string, unknown>
  clients: number
  endpoint: string | null
  started_at: number | null
  latency_ms?: number | null
}

/** One live client TCP connection on a Modbus sim instance. */
export interface ClientConnection {
  id: string
  remote: string
  connected_at: number
  last_activity: number
  unit_id: number | null
  requests: number
}

export interface PresetItem {
  area: ModbusArea
  address: number
  value: number
}

export interface CreateServerBody {
  name: string
  kind?: ServerKind
  source?: 'manual' | 'ai'
  protocol: Protocol
  bind_ip: string
  port: number
  options?: Partial<ServerOptions>
  sample_tags?: boolean
  /** Link only. */
  target_host?: string
  target_port?: number
  target_options?: LinkOptions
  /** Modbus sim only: raw points written right after creation. */
  preset?: PresetItem[]
}

export interface AiConfig {
  configured: boolean
  base_url: string
  model: string
  enabled: boolean
  timeout_ms: number
}

export interface AiDraft {
  name: string
  protocol: Protocol
  kind: ServerKind
  source: string
  bind_ip: string
  port: number
  options: ServerOptions
  tags: Tag[]
  preset: PresetItem[]
  target_host?: string
  target_port?: number
  target_options?: LinkOptions
}

export interface AiAnalyzeResult {
  items: AiDraft[]
  warnings: string[]
}

export interface LinkTestResult {
  ok: boolean
  error: string | null
  /** Present when the device answered but refused the probe (e.g. FC3 addr 0). */
  warning?: string | null
  latency_ms: number | null
  endpoint?: string
  /**
   * OPC UA only: the far server's namespace table, indexed by position. This is
   * where the user finds the namespace index a tag has to use; it is assigned by
   * the far side and appears nowhere else.
   */
  namespaces?: string[]
}

/** Answer of the live port pre-flight - the same check the save will run. */
export interface PortCheckResult {
  port: number
  bind_ip: string
  available: boolean
  reason: string | null
}

/**
 * One poly_sim_runtime process seen on this machine, ours or a leftover from
 * an earlier run that is still holding its port. `owned` means the platform
 * recognizes it as one of its own running instances; `is_test` marks a
 * process started by the test suite, which is the only kind the cleanup may
 * end.
 */
export interface RuntimeProcess {
  pid: number
  create_time: number
  config: string
  control_port: number
  owned: boolean
  is_test: boolean
}

/** A server row enriched by the global overview endpoint. */
export type ServerWithSite = Server & {
  site_name: string
  site_host: string
  site_local: boolean
}

export type ModbusArea = 'coil' | 'discrete' | 'holding' | 'input'

export interface AreaRead {
  server_id: string
  area: ModbusArea
  address: number
  count: number
  unit_id: number
  /** Addressable points per area on this instance (65536 when unconfigured). */
  limit: number
  values: (number | boolean)[]
}

/**
 * What an instance *is*, shown as a chip next to the protocol badge.
 *
 * The protocol badge still owns protocol colour. Identity is communicated
 * primarily by the grouped sections on the site page, then by this labelled
 * chip and a thin card rail. The accent never fills a whole card, so the two
 * groups remain distinguishable without relying on colour alone.
 */
export const KIND_META: Record<
  ServerKind,
  { label: string; short: string; hint: string; accent: string; icon: string }
> = {
  sim: {
    label: '自建',
    short: '自建',
    hint: '平台部署并运行在这个 site 的机器上，进程由平台管理',
    accent: 'var(--kind-sim)',
    icon: 'server',
  },
  link: {
    label: '连接',
    short: '连接',
    hint: '平台作为客户端连接别人正在跑的 server，自己不占进程和端口',
    accent: 'var(--kind-link)',
    icon: 'plug',
  },
}

export function isLink(server: Pick<Server, 'kind'>): boolean {
  return server.kind === 'link'
}

/**
 * Whether live numbers mean anything right now.
 *
 * `enabled` is the desired state and `status` is the observed one, and they
 * disagree for a moment on every stop (the flag lands before the process is
 * gone) and on every start. Trusting either alone shows stale values as live:
 * a status that lags keeps polling a dead instance, an enabled flag that lags
 * shows numbers for something the user just switched off.
 */
export function isRunning(server: Pick<Server, 'enabled' | 'status'>): boolean {
  return server.enabled === true && server.status === 'running'
}

/** The address that matters for this instance: where it listens, or where it connects. */
export function connectionText(server: Server, lanIp: string, siteHost?: string): string {
  if (isLink(server)) {
    return (
      server.endpoint ?? `modbus-tcp://${server.target_host ?? ''}:${server.target_port ?? server.port}`
    )
  }
  // A local site has no host of its own, so it falls back to the LAN address -
  // `||`, not `??`: an empty host string is exactly the local case.
  const host = server.bind_ip === '0.0.0.0' ? siteHost || lanIp : server.bind_ip
  if (server.protocol === 'opcua') return server.endpoint ?? `opc.tcp://${host}:${server.port}`
  return `${host}:${server.port}`
}

/** One line under the card title: where the instance lives, or where it points. */
export function kindSubtitle(server: Server, site?: Site): string {
  if (isLink(server)) {
    return `连接 → ${server.target_host ?? '?'}:${server.target_port ?? server.port}`
  }
  if (site && !site.local) return `部署在 ${site.host}`
  return '本机进程'
}

/**
 * The one parameter worth printing on a link card.
 *
 * Modbus addresses a unit, S7 addresses a rack/slot pair, OPC UA addresses a
 * namespace - and printing "unit 1" on an S7 link is a number about nothing.
 */
export function linkParamsText(server: Pick<Server, 'protocol' | 'target_options'>): string {
  if (server.protocol === 'opcua') {
    return `ns ${server.target_options?.namespace_index ?? 2}`
  }
  if (server.protocol === 's7') {
    return `rack ${server.target_options?.rack ?? 0} / slot ${server.target_options?.slot ?? 1}`
  }
  return `unit ${server.target_options?.unit_id ?? 1}`
}

export const PROTOCOL_META: Record<Protocol, { label: string; short: string; hint: string; accent: string }> = {
  modbus_tcp: {
    label: 'Modbus TCP',
    short: 'MB',
    hint: '寄存器 / 线圈，手写标准帧，支持 FC 1/2/3/4/5/6/15/16',
    accent: 'var(--mb)',
  },
  s7: {
    label: 'Siemens S7',
    short: 'S7',
    hint: 'DB / M / I / Q 面积映射，python-snap7 纯 Python 服务端',
    accent: 'var(--s7)',
  },
  opcua: {
    label: 'OPC UA',
    short: 'UA',
    hint: '标准地址空间，endpoint 可配置，证书/加密可后续扩展',
    accent: 'var(--ua)',
  },
}

export const DATA_TYPES: DataType[] = [
  'bool',
  'int16',
  'uint16',
  'int32',
  'uint32',
  'float32',
  'float64',
]

export const SIM_MODES: SimMode[] = ['static', 'sine', 'random', 'ramp', 'toggle']

export const MODBUS_AREAS = ['holding', 'input', 'coil', 'discrete'] as const
export const S7_AREAS = ['db', 'm', 'i', 'q'] as const

export function defaultTag(protocol: Protocol): Tag {
  if (protocol === 's7') {
    return {
      name: 'tag_' + Math.random().toString(36).slice(2, 6),
      data_type: 'uint16',
      area: 'db',
      address: 0,
      bit: null,
      db_number: 1,
      word_order: 'big',
      writable: true,
      description: '',
      sim: { mode: 'static', min: 0, max: 100, period_ms: 5000, step: 1 },
    }
  }
  if (protocol === 'opcua') {
    return {
      name: 'tag_' + Math.random().toString(36).slice(2, 6),
      data_type: 'float32',
      area: 'opcua',
      address: 0,
      bit: null,
      db_number: null,
      word_order: 'big',
      writable: true,
      description: '',
      sim: { mode: 'static', min: 0, max: 100, period_ms: 5000, step: 1 },
    }
  }
  return {
    name: 'tag_' + Math.random().toString(36).slice(2, 6),
    data_type: 'uint16',
    area: 'holding',
    address: 0,
    bit: null,
    db_number: null,
    word_order: 'big',
    writable: true,
    description: '',
    sim: { mode: 'static', min: 0, max: 100, period_ms: 5000, step: 1 },
  }
}

export function formatValue(value: unknown, tag: Tag): string {
  if (value === null || value === undefined) return '—'
  if (tag.data_type === 'bool') return value ? 'TRUE' : 'FALSE'
  if (typeof value === 'number') {
    if (tag.data_type === 'float32' || tag.data_type === 'float64') {
      return Number.isInteger(value) ? value.toFixed(1) : value.toFixed(3)
    }
    return String(value)
  }
  return String(value)
}

export function formatAddress(tag: Tag, protocol: Protocol, namespaceIndex?: number | null): string {
  if (protocol === 'opcua') {
    const explicit = (tag.node_id ?? '').trim()
    if (explicit) return explicit
    // A simulator names the variable `s=<name>` in its own namespace; a link
    // needs the far side's index spelled out, because that is the number the
    // user has to type and get right.
    return typeof namespaceIndex === 'number'
      ? `ns=${namespaceIndex};s=${tag.name}`
      : `s=${tag.name}`
  }
  if (protocol === 'modbus_tcp') {
    const map: Record<string, string> = { holding: 'HR', input: 'IR', coil: 'CO', discrete: 'DI' }
    return `${map[tag.area] ?? tag.area.toUpperCase()} ${tag.address}`
  }
  if (tag.area === 'db') {
    return tag.data_type === 'bool'
      ? `DB${tag.db_number ?? 1}.DBX${tag.address}.${tag.bit ?? 0}`
      : `DB${tag.db_number ?? 1}.DBB${tag.address}`
  }
  const area = tag.area.toUpperCase()
  return tag.data_type === 'bool' ? `${area}${tag.address}.${tag.bit ?? 0}` : `${area}B${tag.address}`
}

export function uptime(startedAt: number | null | undefined): string {
  if (!startedAt) return '—'
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - startedAt))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

export interface User {
  id: string
  username: string
  is_admin: boolean
  must_change_password: boolean
  created_at: string
}

export interface AuditEntry {
  id: number
  created_at: string
  actor: string
  action: string
  target: string
  detail: string
  params: Record<string, unknown>
}
