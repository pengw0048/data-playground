import { useStore } from '../store/graph'
import { color, radius, shadow } from '../theme/tokens'
import { Icon } from '../ui/Icon'

// Bottom-right toasts for errors/info so failures aren't silent, plus a slim top banner when the
// kernel is offline (with a Retry that re-bootstraps). Rendered once in App, above every view.
export function Toaster() {
  const toasts = useStore((s) => s.toasts)
  const dismiss = useStore((s) => s.dismissToast)
  const kernelUp = useStore((s) => s.kernelUp)
  const accessDenied = useStore((s) => s.accessDenied)
  const bootstrap = useStore((s) => s.bootstrap)

  const tone = { error: { bg: '#fdecec', fg: '#b3261e', icon: 'close' as const }, success: { bg: '#e6f6ec', fg: '#1f7a45', icon: 'check' as const }, info: { bg: '#eef1f6', fg: color.ink, icon: 'note' as const } }

  return (
    <>
      {!kernelUp && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, zIndex: 70, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 12, padding: '7px 12px', background: '#3a3f4b', color: '#fff', fontSize: 12.5 }}>
          <span style={{ width: 8, height: 8, borderRadius: '50%', background: color.failed }} />
          Kernel offline — your work is cached locally.
          <button onClick={() => bootstrap()} style={{ border: '1px solid rgba(255,255,255,0.3)', background: 'transparent', color: '#fff', borderRadius: 6, padding: '2px 10px', fontSize: 12, cursor: 'pointer' }}>Retry</button>
        </div>
      )}
      {kernelUp && accessDenied && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, zIndex: 70, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10, padding: '7px 12px', background: '#8a6d0b', color: '#fff', fontSize: 12.5 }}>
          <Icon name="mute" size={13} />
          View-only access — your edits are kept in this browser but aren’t being saved to the server.
        </div>
      )}
      <div style={{ position: 'fixed', right: 16, bottom: 16, zIndex: 70, display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 380 }}>
        {toasts.map((t) => {
          const s = tone[t.kind]
          return (
            <div key={t.id} data-testid="toast" style={{ display: 'flex', alignItems: 'flex-start', gap: 9, background: s.bg, color: s.fg, border: `1px solid ${color.border}`, borderRadius: radius.button, boxShadow: shadow.panel, padding: '9px 11px', fontSize: 12.5, lineHeight: 1.4 }}>
              <Icon name={s.icon} size={13} style={{ marginTop: 1, flex: '0 0 auto' }} />
              <span style={{ flex: 1, wordBreak: 'break-word' }}>{t.msg}</span>
              <button onClick={() => dismiss(t.id)} aria-label="Dismiss" style={{ border: 'none', background: 'transparent', color: s.fg, opacity: 0.7, cursor: 'pointer', flex: '0 0 auto' }}><Icon name="close" size={12} /></button>
            </div>
          )
        })}
      </div>
    </>
  )
}
