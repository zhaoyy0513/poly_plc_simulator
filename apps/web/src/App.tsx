import { useEffect, useState } from 'react'
import { BrowserRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { Aurora } from './components/Aurora'
import { Icon } from './components/Icon'
import { Toast } from './components/Toast'
import { SitePage } from './pages/SitePage'
import { ServerPage } from './pages/ServerPage'
import { SitesPage } from './pages/SitesPage'
import { useApp } from './store'
import { PROTOCOL_META, type Protocol } from './types'

type Theme = 'dark' | 'light'

function Sidebar({
  collapsed,
  onToggle,
  theme,
  onToggleTheme,
}: {
  collapsed: boolean
  onToggle: () => void
  theme: Theme
  onToggleTheme: () => void
}) {
  const sites = useApp((state) => state.sites)
  const system = useApp((state) => state.system)
  const lanIp = system?.addresses.find((address) => address !== '0.0.0.0' && address !== '127.0.0.1')

  return (
    <aside className={`sidebar${collapsed ? ' collapsed' : ''}`}>
      <button
        type="button"
        className="sidebar-toggle"
        onClick={onToggle}
        title={collapsed ? '展开侧边栏' : '收起侧边栏'}
        aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'}
      >
        <Icon name={collapsed ? 'chevronRight' : 'chevronLeft'} size={16} />
      </button>

      <NavLink to="/" className="brand" end>
        <span className="brand-mark">
          <Icon name="chip" size={20} />
        </span>
        <span>
          <strong>POLY</strong>
          <small>PLC SIMULATOR</small>
        </span>
      </NavLink>

      <div className="side-label">
        <span>Sites</span>
        <NavLink to="/" className="side-mini" aria-label="全部 site">
          <Icon name="layers" size={14} />
        </NavLink>
      </div>

      <nav className="side-nav">
        {sites.length === 0 ? <p className="side-empty">还没有 site</p> : null}
        {sites.map((site) => (
          <NavLink
            key={site.id}
            to={`/sites/${site.id}`}
            className={({ isActive }) => `side-item${isActive ? ' active' : ''}`}
          >
            <span className="side-avatar">{site.name.slice(0, 1).toUpperCase()}</span>
            <span className="side-item-text">
              <strong>{site.name}</strong>
              <small>{site.server_count} 个实例</small>
            </span>
            <Icon name="chevronRight" size={15} />
          </NavLink>
        ))}
      </nav>

      <div className="side-foot">
        <button
          type="button"
          className="theme-toggle"
          onClick={onToggleTheme}
          title={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
          aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
        >
          <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={15} />
          <span>{theme === 'dark' ? '浅色主题' : '深色主题'}</span>
        </button>
        <div className="cap-row">
          {(Object.keys(PROTOCOL_META) as Protocol[]).map((protocol) => {
            const supported = system?.protocols.includes(protocol) ?? false
            return (
              <span
                key={protocol}
                className={`cap ${supported ? 'on' : 'off'}`}
                title={system?.protocol_notes[protocol] ?? (supported ? '可用' : '不可用')}
              >
                <i style={{ background: PROTOCOL_META[protocol].accent }} />
                {PROTOCOL_META[protocol].short}
              </span>
            )
          })}
        </div>
        <div className="side-meta">
          <Icon name="wifi" size={14} />
          <span>{lanIp ?? '127.0.0.1'}</span>
        </div>
        {system ? <p className="side-os">{system.hostname}</p> : null}
      </div>
    </aside>
  )
}

const SIDEBAR_KEY = 'poly.sidebar.collapsed'
const THEME_KEY = 'poly.theme'

export default function App() {
  const loadSystem = useApp((state) => state.loadSystem)
  const loadSites = useApp((state) => state.loadSites)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(SIDEBAR_KEY) === '1')
  const [theme, setTheme] = useState<Theme>(() =>
    localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark',
  )

  const toggleSidebar = () => {
    setCollapsed((current) => {
      const next = !current
      localStorage.setItem(SIDEBAR_KEY, next ? '1' : '0')
      return next
    })
  }

  const toggleTheme = () => setTheme((current) => (current === 'dark' ? 'light' : 'dark'))

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem(THEME_KEY, theme)
  }, [theme])

  useEffect(() => {
    void loadSystem()
    void loadSites()
  }, [loadSystem, loadSites])

  return (
    <BrowserRouter>
      <Aurora />
      <div className={`shell${collapsed ? ' sidebar-collapsed' : ''}`}>
        <Sidebar
          collapsed={collapsed}
          onToggle={toggleSidebar}
          theme={theme}
          onToggleTheme={toggleTheme}
        />
        <main className="main">
          <Routes>
            <Route path="/" element={<SitesPage />} />
            <Route path="/sites/:siteId" element={<SitePage />} />
            <Route path="/servers/:serverId" element={<ServerPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
      <Toast />
    </BrowserRouter>
  )
}
