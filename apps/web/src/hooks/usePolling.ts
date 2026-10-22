import { useEffect, useRef } from 'react'

/** Interval that pauses while the tab is hidden and refreshes on return. */
export function usePolling(fn: () => void, delay: number, enabled = true) {
  const ref = useRef(fn)
  ref.current = fn

  useEffect(() => {
    if (!enabled) return
    const tick = () => {
      if (!document.hidden) ref.current()
    }
    tick()
    const timer = window.setInterval(tick, delay)
    const onVisible = () => {
      if (!document.hidden) ref.current()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [delay, enabled])
}
