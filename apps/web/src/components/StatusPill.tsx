import type { ServerStatus } from '../types'

const LABELS: Record<ServerStatus, string> = {
  running: '运行中',
  starting: '启动中',
  error: '异常',
  stopped: '已停止',
}

export function StatusPill({
  status,
  error,
  enabled = true,
}: {
  status: ServerStatus
  error?: string | null
  /** `enabled=false` is a deliberate stop; it is not the same as a crash. */
  enabled?: boolean
}) {
  const effective = enabled ? status : 'stopped'
  const label = enabled ? LABELS[status] : '已停用'
  return (
    <span
      className={`status status-${effective}`}
      title={enabled ? (error ?? LABELS[status]) : '已停用，启用后恢复运行'}
    >
      <i aria-hidden="true" />
      {label}
    </span>
  )
}
