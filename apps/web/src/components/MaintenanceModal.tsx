import { useEffect, useState } from 'react'
import { api } from '../api'
import { useConfirm } from './ConfirmModal'
import { Icon } from './Icon'
import { Modal } from './Modal'
import { useApp } from '../store'
import type { RuntimeProcess } from '../types'

interface Props {
  open: boolean
  onClose: () => void
}

/** One row's type label: only test leftovers are selectable. */
function typeText(proc: RuntimeProcess): string {
  if (proc.owned) return '业务实例（运行中）'
  if (proc.is_test) return '测试残留'
  return '业务实例（其他控制面）'
}

/**
 * Maintenance view of every poly_sim_runtime process this machine currently
 * has. `owned` rows belong to a running instance and `!is_test` rows belong
 * to another control plane - both are disabled, so only test leftovers can be
 * killed from here. No polling: the list is a snapshot taken on open, and the
 * refresh button re-reads it, so a half-forgotten dialog cannot keep firing
 * requests while the user is deciding.
 */
export function MaintenanceModal({ open, onClose }: Props) {
  const confirm = useConfirm()
  const notify = useApp((state) => state.notify)
  const [processes, setProcesses] = useState<RuntimeProcess[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = () => {
    setBusy(true)
    api
      .maintenanceRuntimes()
      .then((data) => {
        setProcesses(data.runtimes)
        setError('')
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setBusy(false))
  }

  useEffect(() => {
    if (open) refresh()
  }, [open])

  const selected = processes.filter((proc) => proc.is_test && !proc.owned)

  const killSelected = async () => {
    const pids = selected.map((proc) => proc.pid)
    const ok = await confirm({
      title: '清理测试残留',
      message: `将强制结束所选的 ${pids.length} 个测试残留进程，确定？`,
      confirmText: '清理',
      danger: true,
    })
    if (!ok) return
    setBusy(true)
    try {
      await api.killRuntimes(pids)
      notify('ok', `已结束 ${pids.length} 个测试残留进程`)
      refresh()
    } catch (err) {
      notify('error', err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="清理残留"
      subtitle="本机所有 poly_sim_runtime 进程；只有测试残留可以结束"
      width={720}
      onClose={onClose}
    >
      {error ? <p className="login-error">{error}</p> : null}
      <div className="conn-table maintenance-table">
        <div className="conn-row conn-head maintenance-row">
          <span>PID</span>
          <span>配置路径</span>
          <span>控制端口</span>
          <span>类型</span>
          <span>选择</span>
        </div>
        {processes.length === 0 ? (
          <p className="muted">没有发现 poly_sim_runtime 进程</p>
        ) : (
          processes.map((proc) => {
            const selectable = proc.is_test && !proc.owned
            return (
              <div key={proc.pid} className="conn-row maintenance-row">
                <span className="mono">{proc.pid}</span>
                <span className="mono maintenance-config" title={proc.config}>
                  {proc.config}
                </span>
                <span className="mono">{proc.control_port}</span>
                <span>{typeText(proc)}</span>
                <input
                  type="checkbox"
                  aria-label={`选择 PID ${proc.pid}`}
                  disabled={!selectable}
                  defaultChecked={selectable}
                  data-pid={proc.pid}
                  className="maintenance-check"
                />
              </div>
            )
          })
        )}
      </div>
      <div className="maintenance-foot">
        <button type="button" className="btn ghost" onClick={refresh} disabled={busy}>
          <Icon name="restart" size={15} /> 刷新
        </button>
        <button
          type="button"
          className="btn danger"
          disabled={busy || selected.length === 0}
          onClick={() => void killSelected()}
        >
          <Icon name="alert" size={15} /> 清理所选{selected.length ? `（${selected.length}）` : ''}
        </button>
      </div>
    </Modal>
  )
}
