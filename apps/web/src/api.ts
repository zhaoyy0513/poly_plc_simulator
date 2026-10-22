import type {
  AreaRead,
  CreateServerBody,
  RuntimeState,
  Server,
  Site,
  SiteAccessDraft,
  SiteProbe,
  SystemInfo,
  Tag,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
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

  sites: () => request<Site[]>('/api/sites'),
  createSite: (name: string, description = '', access: Partial<SiteAccessDraft> = {}) =>
    request<Site>('/api/sites', json('POST', { name, description, ...access })),
  updateSite: (id: string, patch: Partial<SiteAccessDraft & { name: string; description: string }>) =>
    request<Site>(`/api/sites/${id}`, json('PATCH', patch)),
  // `password` is only used for this one attempt, so it can be tried before saving.
  probeSite: (id: string, password?: string) =>
    request<{ id: string; probe: SiteProbe } & Site>(
      `/api/sites/${id}/probe`,
      json('POST', password ? { password } : {}),
    ),
  // Test credentials for a machine that is not a site yet (create dialog).
  probeAccess: (access: Partial<SiteAccessDraft>) =>
    request<{ probe: SiteProbe }>('/api/sites/probe', json('POST', access)),
  deleteSite: (id: string) => request<void>(`/api/sites/${id}`, { method: 'DELETE' }),

  servers: (siteId: string) => request<Server[]>(`/api/sites/${siteId}/servers`),
  createServer: (siteId: string, body: CreateServerBody) =>
    request<Server>(`/api/sites/${siteId}/servers`, json('POST', body)),
  server: (id: string) => request<Server>(`/api/servers/${id}`),
  deleteServer: (id: string) => request<void>(`/api/servers/${id}`, { method: 'DELETE' }),
  restartServer: (id: string) =>
    request<Server>(`/api/servers/${id}/restart`, { method: 'POST' }),
  updateServer: (id: string, body: Partial<{ name: string; bind_ip: string; port: number; enabled: boolean }>) =>
    request<Server>(`/api/servers/${id}`, json('PATCH', body)),

  values: (id: string) => request<RuntimeState>(`/api/servers/${id}/values`),
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
    request<{ server_id: string; log: string }>(`/api/servers/${id}/logs?lines=${lines}`),
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
