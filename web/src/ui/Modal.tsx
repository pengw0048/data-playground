import * as DialogPrimitive from '@radix-ui/react-dialog'
import { useEffect, useRef, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { Icon } from './Icon'

/** Centred application dialog. Radix owns focus containment, Escape and scroll locking. */
export function Modal({ label, onClose, children, className, testId, dismissible = true }: {
  label: ReactNode
  onClose: () => void
  children: ReactNode
  className?: string
  testId?: string
  /** false keeps Escape and outside clicks from closing; the Close button still works. */
  dismissible?: boolean
}) {
  const content = useRef<HTMLDivElement>(null)
  const opener = useRef(typeof document === 'undefined' ? null : document.activeElement as HTMLElement | null)
  useEffect(() => () => {
    const target = opener.current
    requestAnimationFrame(() => { if (target?.isConnected) target.focus() })
  }, [])
  return (
    <DialogPrimitive.Root open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/30" />
        <DialogPrimitive.Content ref={content} data-testid={testId} aria-describedby={undefined}
          onEscapeKeyDown={dismissible ? undefined : (event) => event.preventDefault()}
          onPointerDownOutside={dismissible ? undefined : (event) => event.preventDefault()}
          // Radix would otherwise land on the close button; keep today's behaviour of starting in the form.
          onOpenAutoFocus={(event) => {
            const field = content.current?.querySelector<HTMLElement>('input, textarea, select')
            if (!field) return
            event.preventDefault()
            field.focus()
          }}
          className={cn('fixed left-1/2 top-1/2 z-50 grid w-[460px] max-w-[calc(100vw-32px)] max-h-[calc(100vh-32px)] -translate-x-1/2 -translate-y-1/2 gap-3 overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl', className)}>
          <div className="flex items-center gap-2">
            <DialogPrimitive.Title className="flex-1 text-[15px] font-bold text-foreground">{label}</DialogPrimitive.Title>
            <DialogPrimitive.Close aria-label="Close" className="text-muted-foreground hover:text-foreground">
              <Icon name="close" size={15} />
            </DialogPrimitive.Close>
          </div>
          {children}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}
