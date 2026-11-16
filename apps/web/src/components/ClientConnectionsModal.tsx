import { useState } from 'react'
import { api } from '../api'
import { useConfirm } from './ConfirmModal'
import { Icon } from './Icon'
import { Modal } from './Modal'
import { usePolling } from '../hooks/usePolling'
import type { ClientConnection } from '../types'

interface Props {
  open: boolean
  serverId: string
  onClose: () => void
}

function formatTime(value: number): string {
  return new Date(value * 1000).toLocaleTimeString()
}

export function ClientConnectionsModal({ open, serverId, onClose }: Props) {
  const confirm = useConfirm()
  const [connections, setConnections] = useState<ClientConnection[]>([])
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState<string | null>(null)

  usePolling(
    () => {
      if (!open) return
      void (async () => {
        try {
          const data = await api.serverConnections(serverId)
          setConnections(data.connections)
          setError('')
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err))
        }
      })()
    },
    1000,
    open,
  )

  const kick = async (conn: ClientConnection) => {
    const ok = await confirm({
      title: '断开客户端',
      message: `确定断开 ${conn.remote}？客户端可能会自动重连。`,
      confirmText: '断开',
      danger: true,
    })
    if (!ok) return
    setBusyId(conn.id)
    try {
      await api.kickServerConnection(serverId, conn.id)
      setConnections((list) => list.filter((item) => item.id !== conn.id))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <Modal open={open} title="客户端连接" subtitle="Modbus 自建实例的实时连接" width={640} onClose={onClose}>
      {error ? <p className="login-error">{error}</p> : null}
      <div className="conn-table">
        <div className="conn-row conn-head">
          <span>远端地址</span>
          <span>连接时间</span>
          <span>最近活动</span>
          <span>unit</span>
          <span>请求数</span>
          <span />
        </div>
        {connections.length === 0 ? (
          <p className="muted">当前没有客户端连接</p>
        ) : (
          connections.map((conn) => (
            <div key={conn.id} className="conn-row">
              <span className="mono">{conn.remote}</span>
              <span>{formatTime(conn.connected_at)}</span>
              <span>{formatTime(conn.last_activity)}</span>
              <span>{conn.unit_id ?? '—'}</span>
              <span>{conn.requests}</span>
              <button
                type="button"
                className="btn tiny danger"
                disabled={busyId === conn.id}
                onClick={() => void kick(conn)}
              >
                <Icon name="x" size={13} /> 断开
              </button>
            </div>
          ))
        )}
      </div>
    </Modal>
  )
}
