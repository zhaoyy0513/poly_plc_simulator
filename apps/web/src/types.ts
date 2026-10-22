export type Protocol = 'modbus_tcp' | 's7' | 'opcua'
export type DataType = 'bool' | 'int16' | 'uint16' | 'int32' | 'uint32' | 'float32' | 'float64'
export type SimMode = 'static' | 'sine' | 'random' | 'ramp' | 'toggle'
export type WordOrder = 'big' | 'little'
export type ServerStatus = 'running' | 'starting' | 'error' | 'stopped'

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
}

export interface ServerOptions {
  unit_id: number
  rack: number
  slot: number
  endpoint_url: string | null
  advertise_host: string | null
  namespace: string | null
}

export interface Server {
  id: string
  site_id: string
  name: string
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
  tags: Tag[]
}

export interface RuntimeState {
  status: ServerStatus
  error: string | null
  values: Record<string, unknown>
  clients: number
  endpoint: string | null
  started_at: number | null
}

export interface CreateServerBody {
  name: string
  protocol: Protocol
  bind_ip: string
  port: number
  options?: Partial<ServerOptions>
  sample_tags?: boolean
}

export type ModbusArea = 'coil' | 'discrete' | 'holding' | 'input'

export interface AreaRead {
  server_id: string
  area: ModbusArea
  address: number
  count: number
  unit_id: number
  values: (number | boolean)[]
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

export function formatAddress(tag: Tag, protocol: Protocol): string {
  if (protocol === 'opcua') return 'NodeId / ' + tag.name
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

export function endpointText(server: Server, lanIp: string): string {
  const host = server.bind_ip === '0.0.0.0' ? lanIp : server.bind_ip
  if (server.protocol === 'opcua') return server.endpoint ?? `opc.tcp://${host}:${server.port}`
  return `${host}:${server.port}`
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
