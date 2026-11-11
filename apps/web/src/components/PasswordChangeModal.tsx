import { useEffect, useState } from 'react'
import { Modal } from './Modal'
import { useApp } from '../store'

interface Props {
  open: boolean
  userId: string
  title?: string
  onClose: () => void
}

export function PasswordChangeModal({ open, userId, title = '修改密码', onClose }: Props) {
  const changePassword = useApp((state) => state.changePassword)
  const currentUser = useApp((state) => state.user)
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open) {
      setPassword('')
      setError('')
    }
  }, [open, userId])

  const submit = async () => {
    if (password.length < 8 || password.length > 128) {
      setError('新密码需为 8–128 个字符')
      return
    }
    setBusy(true)
    setError('')
    try {
      await changePassword(userId, password)
      setPassword('')
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} title={title} width={400} onClose={onClose} footer={
      <>
        <button type="button" className="btn ghost" onClick={onClose}>取消</button>
        <button type="button" className="btn primary" disabled={busy || !password} onClick={() => void submit()}>
          {busy ? '保存中…' : '确认'}
        </button>
      </>
    }>
      <label className="field">
        <span>新密码</span>
        <input
          id="password-change-input"
          type="password"
          minLength={8}
          maxLength={128}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              void submit()
            }
          }}
          autoFocus
        />
        <small className="hint">
          {userId === currentUser?.id
            ? '8–128 个字符。保存后旧登录 Cookie 会失效。'
            : '8–128 个字符。保存后该用户的旧登录 Cookie 会失效。'}
        </small>
      </label>
      {error ? <p className="login-error">{error}</p> : null}
    </Modal>
  )
}
