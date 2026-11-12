import { useEffect, useState } from 'react'
import { Modal } from './Modal'
import { Icon } from './Icon'
import { PasswordChangeModal } from './PasswordChangeModal'
import { useConfirm } from './ConfirmModal'
import { useApp } from '../store'
import type { User } from '../types'

export function UserManageModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const users = useApp((state) => state.users)
  const loadUsers = useApp((state) => state.loadUsers)
  const createUser = useApp((state) => state.createUser)
  const deleteUser = useApp((state) => state.deleteUser)
  const currentUser = useApp((state) => state.user)
  const confirm = useConfirm()

  const [newUsername, setNewUsername] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [changingFor, setChangingFor] = useState<User | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open) {
      void loadUsers()
      setNewUsername('')
      setShowAdd(false)
    }
  }, [open, loadUsers])

  const handleCreate = async () => {
    if (!newUsername.trim()) return
    setBusy(true)
    try {
      await createUser(newUsername.trim())
      setNewUsername('')
      setShowAdd(false)
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async (user: User) => {
    const ok = await confirm({
      title: '删除用户',
      message: `确定删除用户「${user.username}」？`,
      confirmText: '删除',
      danger: true,
    })
    if (!ok) return
    setBusy(true)
    try {
      await deleteUser(user.id)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Modal open={open} onClose={onClose} title="用户管理" width={560}>
        <div className="user-list">
          {users.map((user) => (
            <div key={user.id} className="user-row">
              <div className="user-info">
                <span className="user-name">{user.username}</span>
                {user.is_admin && <span className="user-badge admin">管理员</span>}
                {user.must_change_password && (
                  <span className="user-badge warn">默认密码未改</span>
                )}
              </div>
              <div className="user-actions">
                <button
                  type="button"
                  className="icon-btn"
                  onClick={() => setChangingFor(user)}
                  title="修改密码"
                  disabled={busy}
                >
                  <Icon name="key" size={16} />
                </button>
                {currentUser?.id !== user.id && (
                  <button
                    type="button"
                    className="icon-btn danger"
                    onClick={() => handleDelete(user)}
                    title="删除用户"
                    disabled={busy}
                  >
                    <Icon name="trash" size={16} />
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
        <div className="modal-foot-row">
          {showAdd ? (
            <div className="inline-form">
              <input
                type="text"
                placeholder="用户名"
                value={newUsername}
                onChange={(e) => setNewUsername(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
                autoFocus
              />
              <button type="button" className="btn" onClick={handleCreate} disabled={busy}>
                创建
              </button>
              <button type="button" className="btn ghost" onClick={() => setShowAdd(false)}>
                取消
              </button>
            </div>
          ) : (
            <button type="button" className="btn primary" onClick={() => setShowAdd(true)}>
              添加用户
            </button>
          )}
        </div>
      </Modal>

      <PasswordChangeModal
        open={!!changingFor}
        userId={changingFor?.id ?? ''}
        title={`修改密码：${changingFor?.username ?? ''}`}
        onClose={() => setChangingFor(null)}
      />
    </>
  )
}
