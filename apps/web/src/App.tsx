import { useEffect, useState } from 'react'
import { BrowserRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { AiConfigModal } from './components/AiConfigModal'
import { Aurora } from './components/Aurora'
import { ConfirmProvider } from './components/ConfirmModal'
import { Icon } from './components/Icon'
import { Toast } from './components/Toast'
import { PasswordChangeModal } from './components/PasswordChangeModal'
import { MaintenanceModal } from './components/MaintenanceModal'
import { UserManageModal } from './components/UserManageModal'
import { AuditPage } from './pages/AuditPage'
import { InstancesPage } from './pages/InstancesPage'
import { LoginPage } from './pages/LoginPage'
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
  const user = useApp((state) => state.user)
  const logout = useApp((state) => state.logout)
  const serverCount = sites.reduce((total, site) => total + site.server_count, 0)
  const lanIp = system?.addresses.find((address) => address !== '0.0.0.0' && address !== '127.0.0.1')
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const [userManageOpen, setUserManageOpen] = useState(false)
  const [maintenanceOpen, setMaintenanceOpen] = useState(false)
  const [aiConfigOpen, setAiConfigOpen] = useState(false)
  const [passwordOpen, setPasswordOpen] = useState(false)

  const handleLogout = async () => {
    await logout()
    setUserMenuOpen(false)
  }

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
        <NavLink
          to="/instances"
          className={({ isActive }) => `side-item side-item-all${isActive ? ' active' : ''}`}
        >
          <span className="side-avatar">
            <Icon name="layers" size={14} />
          </span>
          <span className="side-item-text">
            <strong>全部实例</strong>
            <small>{serverCount} 个 · 按协议查看</small>
          </span>
          <Icon name="chevronRight" size={15} />
        </NavLink>
        {user?.is_admin ? (
          <NavLink
            to="/audit"
            className={({ isActive }) => `side-item${isActive ? ' active' : ''}`}
          >
            <span className="side-avatar">
              <Icon name="clock" size={14} />
            </span>
            <span className="side-item-text">
              <strong>审计</strong>
              <small>操作记录</small>
            </span>
            <Icon name="chevronRight" size={15} />
          </NavLink>
        ) : null}
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
        <div className="user-section">
          {user && (
            <div className="user-menu-wrap">
              <button
                type="button"
                className="user-btn"
                onClick={() => setUserMenuOpen(!userMenuOpen)}
              >
                <Icon name="user" size={15} />
                <span>{user.username}</span>
                {user.is_admin && <span className="user-badge-sm">管理员</span>}
                <Icon name={userMenuOpen ? 'chevronUp' : 'chevronDown'} size={12} />
              </button>
              {userMenuOpen && (
                <div className="user-menu">
                  {user.is_admin && (
                    <button
                      type="button"
                      onClick={() => {
                        setUserManageOpen(true)
                        setUserMenuOpen(false)
                      }}
                    >
                      <Icon name="users" size={14} />
                      <span>用户管理</span>
                    </button>
                  )}
                  {user.is_admin && (
                    <button
                      type="button"
                      onClick={() => {
                        setMaintenanceOpen(true)
                        setUserMenuOpen(false)
                      }}
                    >
                      <Icon name="alert" size={14} />
                      <span>清理残留</span>
                    </button>
                  )}
                  {user.is_admin && (
                    <button
                      type="button"
                      onClick={() => {
                        setAiConfigOpen(true)
                        setUserMenuOpen(false)
                      }}
                    >
                      <Icon name="bolt" size={14} />
                      <span>AI 配置</span>
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      setPasswordOpen(true)
                      setUserMenuOpen(false)
                    }}
                  >
                    <Icon name="key" size={14} />
                    <span>修改密码</span>
                  </button>
                  <button type="button" onClick={handleLogout}>
                    <Icon name="logOut" size={14} />
                    <span>退出登录</span>
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
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
        <UserManageModal open={userManageOpen} onClose={() => setUserManageOpen(false)} />
        <MaintenanceModal open={maintenanceOpen} onClose={() => setMaintenanceOpen(false)} />
        <AiConfigModal open={aiConfigOpen} onClose={() => setAiConfigOpen(false)} />
        {user ? (
          <PasswordChangeModal
            open={passwordOpen}
            userId={user.id}
            onClose={() => setPasswordOpen(false)}
          />
        ) : null}
      </div>
    </aside>
  )
}

const SIDEBAR_KEY = 'poly.sidebar.collapsed'
const THEME_KEY = 'poly.theme'
const PASSWORD_NOTICE_KEY = 'poly.passwordNotice.dismissed'

export default function App() {
  const loadSystem = useApp((state) => state.loadSystem)
  const loadSites = useApp((state) => state.loadSites)
  const loadMe = useApp((state) => state.loadMe)
  const user = useApp((state) => state.user)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(SIDEBAR_KEY) === '1')
  const [theme, setTheme] = useState<Theme>(() => {
    // Saved choice wins; otherwise follow the OS, defaulting to light.
    const saved = localStorage.getItem(THEME_KEY)
    if (saved === 'light' || saved === 'dark') return saved
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  })
  const [dismissedUserId, setDismissedUserId] = useState(
    () => localStorage.getItem(PASSWORD_NOTICE_KEY),
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
    void loadMe()
  }, [loadMe])

  useEffect(() => {
    if (user) {
      void loadSystem()
      void loadSites()
    }
  }, [user, loadSystem, loadSites])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem(THEME_KEY, theme)
  }, [theme])

  return (
    <ConfirmProvider>
      <BrowserRouter>
        <Aurora />
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="*"
            element={
              user ? (
                <div className={`shell${collapsed ? ' sidebar-collapsed' : ''}`}>
                  <Sidebar
                    collapsed={collapsed}
                    onToggle={toggleSidebar}
                    theme={theme}
                    onToggleTheme={toggleTheme}
                  />
                  {user?.must_change_password && dismissedUserId !== user.id ? (
                    <div className="password-notice" role="status">
                      <Icon name="key" size={16} />
                      <span>当前账号仍使用默认密码，请在用户菜单中尽快修改。</span>
                      <button
                        type="button"
                        className="icon-btn"
                        aria-label="关闭提醒"
                        onClick={() => {
                          localStorage.setItem(PASSWORD_NOTICE_KEY, user.id)
                          setDismissedUserId(user.id)
                        }}
                      >
                        <Icon name="x" size={14} />
                      </button>
                    </div>
                  ) : null}
                  <main className="main">
                    <Routes>
                      <Route path="/" element={<SitesPage />} />
                      <Route path="/instances" element={<InstancesPage />} />
                      <Route path="/audit" element={<AuditPage />} />
                      <Route path="/sites/:siteId" element={<SitePage />} />
                      <Route path="/servers/:serverId" element={<ServerPage />} />
                      <Route path="*" element={<Navigate to="/" replace />} />
                    </Routes>
                  </main>
                </div>
              ) : (
                <Navigate to="/login" replace />
              )
            }
          />
        </Routes>
        <Toast />
      </BrowserRouter>
    </ConfirmProvider>
  )
}
