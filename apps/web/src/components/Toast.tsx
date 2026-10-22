import { useEffect } from 'react'
import { useApp } from '../store'
import { Icon } from './Icon'

export function Toast() {
  const toast = useApp((state) => state.toast)
  const clear = useApp((state) => state.clearToast)

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => clear(), 3600)
    return () => window.clearTimeout(timer)
  }, [toast, clear])

  if (!toast) return null
  return (
    <div className={`toast toast-${toast.kind}`} role="status">
      <Icon name={toast.kind === 'ok' ? 'check' : toast.kind === 'error' ? 'alert' : 'bolt'} size={16} />
      <span>{toast.text}</span>
      <button type="button" className="icon-btn tiny" onClick={clear} aria-label="关闭提示">
        <Icon name="x" size={14} />
      </button>
    </div>
  )
}
