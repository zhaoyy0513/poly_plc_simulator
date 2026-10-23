import { create } from 'zustand'
import { api } from './api'
import type {
  CreateServerBody,
  LinkOptions,
  Protocol,
  RuntimeState,
  Server,
  Site,
  SiteAccessDraft,
  SiteProbe,
  SystemInfo,
  Tag,
} from './types'

export interface Toast {
  id: number
  kind: 'ok' | 'error' | 'info'
  text: string
}

let toastSeq = 0

interface AppState {
  system: SystemInfo | null
  sites: Site[]
  serversBySite: Record<string, Server[]>
  server: Server | null
  values: RuntimeState | null
  logs: string
  logsLoading: boolean
  loadingServer: boolean
  toast: Toast | null

  loadSystem: () => Promise<void>
  loadSites: () => Promise<void>
  loadServers: (siteId: string, silent?: boolean) => Promise<void>
  loadServer: (id: string, silent?: boolean) => Promise<void>
  refreshValues: (id: string) => Promise<void>
  loadLogs: (id: string, lines?: number) => Promise<void>

  createSite: (name: string, description?: string, access?: Partial<SiteAccessDraft>) => Promise<boolean>
  updateSite: (siteId: string, patch: Partial<SiteAccessDraft & { name: string; description: string }>) => Promise<boolean>
  probeSite: (siteId: string, password?: string) => Promise<SiteProbe | null>
  probeAccess: (access: Partial<SiteAccessDraft>) => Promise<SiteProbe | null>
  deleteSite: (siteId: string) => Promise<void>
  createServer: (siteId: string, body: CreateServerBody) => Promise<Server>
  deleteServer: (server: Server) => Promise<void>
  restartServer: (id: string) => Promise<void>
  updateServer: (
    id: string,
    patch: Partial<{
      name: string
      bind_ip: string
      port: number
      enabled: boolean
      target_host: string
      target_port: number
      target_options: LinkOptions
    }>,
  ) => Promise<boolean>
  saveTags: (id: string, tags: Tag[]) => Promise<boolean>
  write: (id: string, tag: string, value: unknown) => Promise<boolean>

  notify: (kind: Toast['kind'], text: string) => void
  clearToast: () => void
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** Keep the home protocol cards in sync without waiting for a full reload. */
function bumpProtocolCount(
  site: Site,
  protocol: Protocol,
  delta: number,
): Record<Protocol, number> {
  const counts: Record<Protocol, number> = {
    modbus_tcp: site.protocol_counts?.modbus_tcp ?? 0,
    s7: site.protocol_counts?.s7 ?? 0,
    opcua: site.protocol_counts?.opcua ?? 0,
  }
  counts[protocol] = Math.max(0, counts[protocol] + delta)
  return counts
}

export const useApp = create<AppState>((set, get) => ({
  system: null,
  sites: [],
  serversBySite: {},
  server: null,
  values: null,
  logs: '',
  logsLoading: false,
  loadingServer: false,
  toast: null,

  notify: (kind, text) => set({ toast: { id: ++toastSeq, kind, text } }),
  clearToast: () => set({ toast: null }),

  loadSystem: async () => {
    try {
      set({ system: await api.system() })
    } catch (error) {
      get().notify('error', `无法连接后端: ${errorMessage(error)}`)
    }
  },

  loadSites: async () => {
    try {
      set({ sites: await api.sites() })
    } catch (error) {
      get().notify('error', `加载 site 失败: ${errorMessage(error)}`)
    }
  },

  loadServers: async (siteId, silent = false) => {
    try {
      const servers = await api.servers(siteId)
      set((state) => ({ serversBySite: { ...state.serversBySite, [siteId]: servers } }))
    } catch (error) {
      if (!silent) get().notify('error', `加载实例失败: ${errorMessage(error)}`)
    }
  },

  loadServer: async (id, silent = false) => {
    if (!silent) set({ loadingServer: true })
    try {
      const server = await api.server(id)
      if (get().server === null || get().server?.id === id || !silent) {
        set({ server })
      }
      set((state) => {
        const list = state.serversBySite[server.site_id]
        if (!list) return {}
        return {
          serversBySite: {
            ...state.serversBySite,
            [server.site_id]: list.map((item) => (item.id === server.id ? server : item)),
          },
        }
      })
    } catch (error) {
      if (!silent) get().notify('error', `加载实例失败: ${errorMessage(error)}`)
    } finally {
      if (!silent) set({ loadingServer: false })
    }
  },

  refreshValues: async (id) => {
    try {
      const values = await api.values(id)
      if (get().server?.id === id) set({ values })
    } catch {
      // transient failures are expected while an instance restarts
    }
  },

  loadLogs: async (id, lines = 300) => {
    set({ logsLoading: true })
    try {
      const { log } = await api.logs(id, lines)
      if (get().server?.id === id) set({ logs: log })
    } catch (error) {
      set({ logs: `无法读取日志: ${errorMessage(error)}` })
    } finally {
      set({ logsLoading: false })
    }
  },

  createSite: async (name, description = '', access = {}) => {
    try {
      const site = await api.createSite(name, description, access)
      set((state) => ({
        sites: [
          ...state.sites,
          {
            ...site,
            server_count: 0,
            protocol_counts: { modbus_tcp: 0, s7: 0, opcua: 0 },
          },
        ],
      }))
      get().notify('ok', `Site「${site.name}」已创建`)
      return true
    } catch (error) {
      get().notify('error', errorMessage(error))
      return false
    }
  },

  updateSite: async (siteId, patch) => {
    try {
      const site = await api.updateSite(siteId, patch)
      set((state) => ({
        sites: state.sites.map((item) => (item.id === siteId ? { ...item, ...site } : item)),
      }))
      get().notify('ok', 'Site 访问信息已更新')
      return true
    } catch (error) {
      get().notify('error', errorMessage(error))
      return false
    }
  },

  probeSite: async (siteId, password) => {
    try {
      const result = await api.probeSite(siteId, password)
      set((state) => ({
        sites: state.sites.map((item) =>
          item.id === siteId
            ? {
                ...item,
                host: result.host,
                ssh_port: result.ssh_port,
                ssh_user: result.ssh_user,
                ssh_configured: result.ssh_configured,
                deploy_dir: result.deploy_dir,
                python_bin: result.python_bin,
                local: result.local,
                last_probe_at: result.last_probe_at,
                last_probe: result.last_probe ?? result.probe,
                deploy: result.deploy ?? item.deploy,
                server_count: item.server_count,
              }
            : item,
        ),
      }))
      get().notify(result.probe.ok ? 'ok' : 'error', result.probe.ok ? '连接正常' : result.probe.error ?? '连接失败')
      return result.probe
    } catch (error) {
      get().notify('error', errorMessage(error))
      return null
    }
  },

  probeAccess: async (access) => {
    try {
      const { probe } = await api.probeAccess(access)
      get().notify(
        probe.ok ? 'ok' : 'error',
        probe.ok ? 'SSH 连接正常' : probe.error ?? '连接失败',
      )
      return probe
    } catch (error) {
      get().notify('error', errorMessage(error))
      return null
    }
  },

  deleteSite: async (siteId) => {
    try {
      await api.deleteSite(siteId)
      set((state) => ({ sites: state.sites.filter((site) => site.id !== siteId) }))
      get().notify('ok', 'Site 及其下的实例已删除')
    } catch (error) {
      get().notify('error', errorMessage(error))
    }
  },

  createServer: async (siteId, body) => {
    const server = await api.createServer(siteId, body)
    set((state) => {
      const list = state.serversBySite[siteId] ?? []
      return {
        serversBySite: { ...state.serversBySite, [siteId]: [...list, server] },
        sites: state.sites.map((site) =>
          site.id === siteId
            ? {
                ...site,
                server_count: site.server_count + 1,
                protocol_counts: bumpProtocolCount(site, server.protocol, 1),
              }
            : site,
        ),
      }
    })
    if (server.status === 'error') {
      get().notify('error', server.error ?? '实例创建后启动失败')
    } else if (server.kind === 'link') {
      get().notify('ok', `已连接到 ${server.target_host}:${server.target_port}`)
    } else {
      get().notify('ok', `实例「${server.name}」已创建`)
    }
    return server
  },

  deleteServer: async (server) => {
    try {
      await api.deleteServer(server.id)
      set((state) => ({
        serversBySite: {
          ...state.serversBySite,
          [server.site_id]: (state.serversBySite[server.site_id] ?? []).filter(
            (item) => item.id !== server.id,
          ),
        },
        sites: state.sites.map((site) =>
          site.id === server.site_id
            ? {
                ...site,
                server_count: Math.max(0, site.server_count - 1),
                protocol_counts: bumpProtocolCount(site, server.protocol, -1),
              }
            : site,
        ),
      }))
      get().notify('ok', `实例「${server.name}」已删除`)
    } catch (error) {
      get().notify('error', errorMessage(error))
    }
  },

  restartServer: async (id) => {
    try {
      const server = await api.restartServer(id)
      set({ server })
      get().notify(server.status === 'error' ? 'error' : 'ok', server.error ?? '实例已重启')
      if (server.status !== 'error') {
        window.setTimeout(() => void get().refreshValues(id), 800)
      }
    } catch (error) {
      get().notify('error', errorMessage(error))
    }
  },

  updateServer: async (id, patch) => {
    try {
      const server = await api.updateServer(id, patch)
      set({ server })
      set((state) => {
        const list = state.serversBySite[server.site_id]
        if (!list) return {}
        return {
          serversBySite: {
            ...state.serversBySite,
            [server.site_id]: list.map((item) => (item.id === server.id ? server : item)),
          },
        }
      })
      const message =
        patch.enabled === undefined
          ? '实例配置已更新'
          : patch.enabled
            ? `实例「${server.name}」已启用`
            : `实例「${server.name}」已停用`
      get().notify(server.status === 'error' ? 'error' : 'ok', server.error ?? message)
      window.setTimeout(() => void get().refreshValues(id), 900)
      return true
    } catch (error) {
      get().notify('error', errorMessage(error))
      return false
    }
  },

  saveTags: async (id, tags) => {
    try {
      const server = await api.saveTags(id, tags)
      set({ server })
      get().notify('ok', '标签表已保存，实例已重启')
      window.setTimeout(() => void get().refreshValues(id), 900)
      return true
    } catch (error) {
      get().notify('error', errorMessage(error))
      return false
    }
  },

  write: async (id, tag, value) => {
    try {
      await api.write(id, tag, value)
      get().notify('ok', `${tag} ← ${String(value)}`)
      window.setTimeout(() => void get().refreshValues(id), 150)
      return true
    } catch (error) {
      get().notify('error', errorMessage(error))
      return false
    }
  },
}))
