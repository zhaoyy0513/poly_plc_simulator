import { useState } from 'react'
import { Navigate } from 'react-router-dom'
import { Icon } from '../components/Icon'
import { useApp } from '../store'

export function LoginPage() {
  const login = useApp((state) => state.login)
  const user = useApp((state) => state.user)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  if (user) return <Navigate to="/" replace />

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      await login(username, password)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-brand">
          <Icon name="chip" size={32} />
          <h1>POLY</h1>
          <p>PLC Simulator</p>
        </div>
        <form onSubmit={onSubmit} className="login-form">
          <label className="field">
            <span>用户名</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              required
            />
          </label>
          <label className="field">
            <span>密码</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error ? <p className="login-error">{error}</p> : null}
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? '登录中...' : '登录'}
          </button>
        </form>
      </div>
    </div>
  )
}
