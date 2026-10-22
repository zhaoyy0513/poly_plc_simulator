import type { ServerStatus } from '../types'

const LABELS: Record<ServerStatus, string> = {
  running: '运行中',
  starting: '启动中',
  error: '异常',
  stopped: '已停止',
}

export function StatusPill({ status, error }: { status: ServerStatus; error?: string | null }) {
  return (
    <span className={`status status-${status}`} title={error ?? LABELS[status]}>
      <i aria-hidden="true" />
      {LABELS[status] ?? status}
    </span>
  )
}
