import { useRef, type CSSProperties, type PointerEvent, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  className?: string
  onClick?: () => void
  as?: 'div' | 'button'
}

/** Card with a cursor-following highlight (react-bits "spotlight" pattern). */
export function SpotlightCard({ children, className = '', onClick, as = 'div' }: Props) {
  const frame = useRef(0)
  const ref = useRef<HTMLDivElement>(null)

  const handleMove = (event: PointerEvent<HTMLDivElement>) => {
    const target = ref.current
    if (!target || frame.current) return
    const { clientX, clientY } = event
    frame.current = window.requestAnimationFrame(() => {
      frame.current = 0
      const rect = target.getBoundingClientRect()
      target.style.setProperty('--mx', `${clientX - rect.left}px`)
      target.style.setProperty('--my', `${clientY - rect.top}px`)
    })
  }

  const style = { cursor: onClick ? 'pointer' : undefined } as CSSProperties
  return (
    <div
      ref={ref}
      className={`spotlight ${className}`}
      style={style}
      onPointerMove={handleMove}
      onClick={onClick}
      role={as === 'button' ? 'button' : undefined}
      tabIndex={as === 'button' ? 0 : undefined}
      onKeyDown={
        as === 'button'
          ? (event) => {
              if (event.key === 'Enter' || event.key === ' ') onClick?.()
            }
          : undefined
      }
    >
      {children}
    </div>
  )
}
