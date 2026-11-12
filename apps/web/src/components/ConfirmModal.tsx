import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { Icon } from './Icon'
import { Modal } from './Modal'

export interface ConfirmOptions {
  title: string
  message: ReactNode
  confirmText?: string
  cancelText?: string
  /** Danger actions use the red button and focus 取消 by default. */
  danger?: boolean
}

type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>

const ConfirmContext = createContext<ConfirmFn | null>(null)

/**
 * App-wide confirmation dialog. Call `const confirm = useConfirm()` and
 * `await confirm({ title, message, danger: true })` instead of window.confirm:
 * same boolean result, but a styled modal that matches the rest of the UI.
 *
 * One provider at the root renders a single dialog, so any screen can ask for
 * confirmation without each call site owning modal state.
 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [options, setOptions] = useState<ConfirmOptions | null>(null)
  const resolveRef = useRef<((value: boolean) => void) | null>(null)

  const confirm = useCallback<ConfirmFn>((next) => {
    return new Promise<boolean>((resolve) => {
      resolveRef.current = resolve
      setOptions(next)
    })
  }, [])

  const settle = useCallback((result: boolean) => {
    setOptions(null)
    resolveRef.current?.(result)
    resolveRef.current = null
  }, [])

  const value = useMemo(() => confirm, [confirm])

  return (
    <ConfirmContext.Provider value={value}>
      {children}
      <Modal
        open={options !== null}
        title={options?.title ?? ''}
        onClose={() => settle(false)}
        width={420}
        footer={
          <>
            <button
              type="button"
              className="btn ghost"
              autoFocus={options?.danger}
              onClick={() => settle(false)}
            >
              {options?.cancelText ?? '取消'}
            </button>
            <button
              type="button"
              className={options?.danger ? 'btn danger' : 'btn primary'}
              autoFocus={!options?.danger}
              onClick={() => settle(true)}
            >
              {options?.confirmText ?? '确认'}
            </button>
          </>
        }
      >
        {options ? (
          <div className="confirm-body">
            <span className={`confirm-icon${options.danger ? ' danger' : ''}`}>
              <Icon name={options.danger ? 'alert' : 'info'} size={20} />
            </span>
            <p className="confirm-message">{options.message}</p>
          </div>
        ) : null}
      </Modal>
    </ConfirmContext.Provider>
  )
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext)
  if (ctx === null) {
    throw new Error('useConfirm 必须在 <ConfirmProvider> 内使用')
  }
  return ctx
}
