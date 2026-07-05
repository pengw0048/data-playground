import { useStore } from '../store/graph'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Icon } from '../ui/Icon'

// Bottom-right toasts for errors/info so failures aren't silent, plus a slim top banner when the
// kernel is offline (with a Retry that re-bootstraps). Rendered once in App, above every view.
export function Toaster() {
  const toasts = useStore((s) => s.toasts)
  const dismiss = useStore((s) => s.dismissToast)
  const kernelUp = useStore((s) => s.kernelUp)
  const accessDenied = useStore((s) => s.accessDenied)
  const bootstrap = useStore((s) => s.bootstrap)

  // Token-based tones: error reads as destructive; success/info share the neutral popover surface
  // (the check/note icon distinguishes them). No hardcoded hex — see index.css design tokens.
  const tone = {
    error: { cls: 'border-destructive/40 bg-destructive/10 text-destructive', icon: 'close' as const },
    success: { cls: 'border-border bg-popover text-popover-foreground', icon: 'check' as const },
    info: { cls: 'border-border bg-popover text-popover-foreground', icon: 'note' as const },
  }

  return (
    <>
      {!kernelUp && (
        <div className="fixed left-0 right-0 top-0 z-[70] flex items-center justify-center gap-3 bg-foreground px-3 py-[7px] text-xs text-background">
          <span className="h-2 w-2 rounded-full bg-destructive" />
          Kernel offline — your work is cached locally.
          <Button variant="outline" size="sm" className="h-6 px-2.5 text-foreground" onClick={() => bootstrap()}>Retry</Button>
        </div>
      )}
      {kernelUp && accessDenied && (
        <div className="fixed left-0 right-0 top-0 z-[70] flex items-center justify-center gap-2.5 border-b border-destructive/20 bg-destructive/10 px-3 py-[7px] text-xs text-destructive">
          <Icon name="mute" size={13} />
          View-only access — your edits are kept in this browser but aren’t being saved to the server.
        </div>
      )}
      <div className="fixed bottom-4 right-4 z-[70] flex max-w-[380px] flex-col gap-2">
        {toasts.map((t) => {
          const s = tone[t.kind]
          return (
            <div key={t.id} data-testid="toast"
              className={cn('flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-xs leading-snug shadow-lg', s.cls)}>
              <Icon name={s.icon} size={13} style={{ marginTop: 1, flexShrink: 0 }} />
              <span className="flex-1 break-words">{t.msg}</span>
              <button onClick={() => dismiss(t.id)} aria-label="Dismiss"
                className="shrink-0 border-0 bg-transparent text-inherit opacity-70 transition-opacity hover:opacity-100">
                <Icon name="close" size={12} />
              </button>
            </div>
          )
        })}
      </div>
    </>
  )
}
