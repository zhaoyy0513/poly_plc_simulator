import type {
  AiAnalyzeResult,
  AiConfig,
  AiModelsResult,
  AiTestResult,
  AreaRead,
  AuditEntry,
  ClientConnection,
  CreateServerBody,
  DeployPlan,
  LinkOptions,
  LinkTestResult,
  PortCheckResult,
  Protocol,
  RuntimeState,
  RuntimeProcess,
  Server,
  ServerOptions,
  ServerWithSite,
  Site,
  SiteAccessDraft,
  SiteProbe,
  SystemInfo,
  Tag,
  User,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const isForm = init?.body instanceof FormData
  const response = await fetch(path, {
    ...init,
    headers: {
      // Let the browser set the multipart boundary; a forced JSON content-type
      // makes the server reject the upload.
      ...(isForm ? {} : { 'Content-Type': 'application/json' }),
      ...(init?.headers ?? {}),
    },
  })
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      const detail = body?.detail
      if (typeof detail === 'string') message = detail
      else if (Array.isArray(detail)) message = detail.map((item) => item.msg ?? JSON.stringify(item)).join('; ')
    } catch {
      // keep the status text
    }
    if (response.status === 401 && !path.startsWith('/api/login')) {
      try {
        const { useApp } = await import('./store')
        useApp.setState({ user: null, users: [] })
      } catch {
        // The login page can still fail before the store module is loaded.
      }
    }
    throw new Error(message)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  body: JSON.stringify(body),
})

export const api = {
  system: () => request<SystemInfo>('/api/system'),

  login: (username: string, password: string) =>
    request<void>('/api/login', json('POST', { username, password })),
  logout: () => request<void>('/api/logout', { method: 'POST' }),
  me: () => request<User>('/api/me'),

  users: () => request<User[]>('/api/users'),
  createUser: (username: string) =>
    request<User>('/api/users', json('POST', { username })),
  deleteUser: (id: string) => request<void>(`/api/users/${id}`, { method: 'DELETE' }),
  changePassword: (id: string, password: string) =>
    request<User>(`/api/users/${id}/password`, json('POST', { password })),

  sites: () => request<Site[]>('/api/sites'),
  createSite: (name: string, description = '', access: Partial<SiteAccessDraft> = {}) =>
    request<Site>('/api/sites', json('POST', { name, description, ...access })),
  updateSite: (id: string, patch: Partial<SiteAccessDraft & { name: string; description: string }>) =>
    request<Site>(`/api/sites/${id}`, json('PATCH', patch)),
  // `password` is only used for this one attempt, so it can be tried before saving.
  probeSite: (id: string, password?: string) =>
    request<{ id: string; probe: SiteProbe; deploy: DeployPlan } & Site>(
      `/api/sites/${id}/probe`,
      json('POST', password ? { password } : {}),
    ),
  // Test credentials for a machine that is not a site yet (create dialog).
  probeAccess: (access: Partial<SiteAccessDraft>) =>
    request<{ probe: SiteProbe }>('/api/sites/probe', json('POST', access)),
  deleteSite: (id: string) => request<void>(`/api/sites/${id}`, { method: 'DELETE' }),

  servers: (siteId: string) => request<Server[]>(`/api/sites/${siteId}/servers`),
  /** Every instance across every site, for the overview page. */
  allServers: () => request<ServerWithSite[]>('/api/servers'),
  createServer: (siteId: string, body: CreateServerBody) =>
    request<Server>(`/api/sites/${siteId}/servers`, json('POST', body)),
  /**
   * Ask the machine itself whether a port is free, before submitting.
   * Same check the save runs - it is a query, so an occupied port is a
   * `available: false` answer rather than an error.
   */
  portCheck: (siteId: string, port: number, bindIp: string, exclude?: string) =>
    request<PortCheckResult>(
      `/api/sites/${siteId}/port-check?port=${port}&bind_ip=${encodeURIComponent(bindIp)}` +
        (exclude ? `&exclude=${encodeURIComponent(exclude)}` : ''),
    ),
  /**
   * Reach somebody else's server once, before it becomes an instance.
   * Nothing is stored, and nothing is cached - it has to tell the truth about
   * the address right now, not about the last time we looked.
   *
   * `protocol` decides which client dials out, so a wrong guess here probes
   * the wrong port with the wrong handshake and reports a dead machine that
   * is very much alive.
   */
  linkTest: (
    siteId: string,
    host: string,
    port: number,
    options: LinkOptions,
    protocol: Protocol = 'modbus_tcp',
  ) =>
    request<LinkTestResult>(
      `/api/sites/${siteId}/link-test?host=${encodeURIComponent(host)}&port=${port}&protocol=${protocol}`,
      json('POST', options),
    ),
  server: (id: string) => request<Server>(`/api/servers/${id}`),
  deleteServer: (id: string) => request<void>(`/api/servers/${id}`, { method: 'DELETE' }),
  restartServer: (id: string) =>
    request<Server>(`/api/servers/${id}/restart`, { method: 'POST' }),
  updateServer: (
    id: string,
    body: Partial<{
      name: string
      bind_ip: string
      port: number
      enabled: boolean
      target_host: string
      target_port: number
      target_options: LinkOptions
      options: Partial<ServerOptions>
    }>,
  ) => request<Server>(`/api/servers/${id}`, json('PATCH', body)),

  values: (id: string) => request<RuntimeState>(`/api/servers/${id}/values`),
  serverConnections: (id: string) =>
    request<{ connections: ClientConnection[] }>(`/api/servers/${id}/connections`),
  kickServerConnection: (id: string, connId: string) =>
    request<void>(`/api/servers/${id}/connections/${connId}/kick`, { method: 'POST' }),
  resetServer: (id: string) =>
    request<{ ok: boolean }>(`/api/servers/${id}/reset`, { method: 'POST' }),
  maintenanceRuntimes: () =>
    request<{ runtimes: RuntimeProcess[] }>('/api/maintenance/runtimes'),
  killRuntimes: (pids: number[]) =>
    request<{ killed: number[] }>('/api/maintenance/kill', json('POST', { pids })),
  area: (id: string, area: string, address: number, count: number) =>
    request<AreaRead>(
      `/api/servers/${id}/area?area=${encodeURIComponent(area)}&address=${address}&count=${count}`,
    ),
  writeArea: (id: string, area: string, address: number, value: number) =>
    request<{ ok: boolean }>(`/api/servers/${id}/area`, json('POST', { area, address, value })),
  write: (id: string, tag: string, value: unknown) =>
    request<{ ok: boolean }>(`/api/servers/${id}/write`, json('POST', { tag, value })),
  saveTags: (id: string, tags: Tag[]) =>
    request<Server>(`/api/servers/${id}/tags`, json('PUT', tags)),
  logs: (id: string, lines = 200) =>
    request<{ server_id: string; log: string; link?: boolean }>(
      `/api/servers/${id}/logs?lines=${lines}`,
    ),

  aiConfig: () => request<AiConfig>('/api/ai/config'),
  saveAiConfig: (body: {
    base_url: string
    api_key?: string
    model: string
    enabled: boolean
    timeout_ms: number
  }) => request<AiConfig>('/api/ai/config', json('POST', body)),
  testAiConfig: (body: { base_url: string; api_key?: string; model?: string; timeout_ms?: number }) =>
    request<AiTestResult>('/api/ai/test', json('POST', body)),
  aiModels: () => request<AiModelsResult>('/api/ai/models'),
  aiAnalyze: (form: FormData) =>
    request<AiAnalyzeResult>('/api/ai/analyze', { method: 'POST', body: form }),

  audit: (params: { actor?: string; action?: string; q?: string; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams()
    if (params.actor) query.set('actor', params.actor)
    if (params.action) query.set('action', params.action)
    if (params.q) query.set('q', params.q)
    if (params.limit) query.set('limit', String(params.limit))
    if (params.offset) query.set('offset', String(params.offset))
    const qs = query.toString()
    return request<{ items: AuditEntry[]; total: number }>(`/api/audit${qs ? `?${qs}` : ''}`)
  },
}

export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // fall through to the legacy path
  }
  try {
    const area = document.createElement('textarea')
    area.value = text
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(area)
    return ok
  } catch {
    return false
  }
}
