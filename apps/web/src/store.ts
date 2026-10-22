import { create } from 'zustand'
import { api } from './api'
import type { CreateServerBody, RuntimeState, Server, Site, SystemInfo, Tag } from './types'

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

  createSite: (name: string, description?: string) => Promise<boolean>
  deleteSite: (siteId: string) => Promise<void>
  createServer: (siteId: string, body: CreateServerBody) => Promise<Server>
  deleteServer: (server: Server) => Promise<void>
  restartServer: (id: string) => Promise<void>
  updateServer: (
    id: string,
    patch: Partial<{ name: string; bind_ip: string; port: number; enabled: boolean }>,
  ) => Promise<boolean>
  saveTags: (id: string, tags: Tag[]) => Promise<boolean>
  write: (id: string, tag: string, value: unknown) => Promise<boolean>

  notify: (kind: Toast['kind'], text: string) => void
  clearToast: () => void
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
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

  createSite: async (name, description = '') => {
    try {
      const site = await api.createSite(name, description)
      set((state) => ({ sites: [...state.sites, { ...site, server_count: 0 }] }))
      get().notify('ok', `Site「${site.name}」已创建`)
      return true
    } catch (error) {
      get().notify('error', errorMessage(error))
      return false
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
          site.id === siteId ? { ...site, server_count: site.server_count + 1 } : site,
        ),
      }
    })
    if (server.status === 'error') {
      get().notify('error', server.error ?? '实例创建后启动失败')
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
            ? { ...site, server_count: Math.max(0, site.server_count - 1) }
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
      get().notify(server.status === 'error' ? 'error' : 'ok', server.error ?? '实例配置已更新')
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
