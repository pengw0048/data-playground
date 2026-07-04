import { useStore } from '../store/graph'
import { color, shadow } from '../theme/tokens'
import { Icon } from '../ui/Icon'

export function TopBar() {
  const doc = useStore((s) => s.doc)
  const kernelUp = useStore((s) => s.kernelUp)
  const kernelInfo = useStore((s) => s.kernelInfo)
  const saved = useStore((s) => s.saved)
  const rerunAll = useStore((s) => s.rerunAll)

  return (
    <>
      <div style={{ position: 'absolute', top: 16, left: 20, zIndex: 15, display: 'flex', alignItems: 'center', gap: 10 }}>
        <button style={iconGhost}><Icon name="chevronLeft" size={15} /></button>
        <span style={{ fontSize: 13.5, color: color.text3 }}>Data Playground</span>
        <span style={{ fontSize: 13.5, color: color.text3 }}>/</span>
        <span style={{ fontSize: 13.5, fontWeight: 600, color: color.ink }}>{doc.name ?? 'untitled'}</span>
        {/* auto-save: no button — just a quiet status */}
        <span data-testid="autosave" style={{ fontSize: 11, color: color.text3, marginLeft: 2 }}>· {saved ? 'saved' : 'saving…'}</span>
      </div>

      <div style={{ position: 'absolute', top: 16, right: 20, zIndex: 15, display: 'flex', alignItems: 'center', gap: 10 }}>
        <div
          title={kernelInfo ? `${kernelInfo.backend} · ${kernelInfo.runners.join(', ')}` : 'kernel offline'}
          style={{
            display: 'flex', alignItems: 'center', gap: 7, padding: '6px 12px', background: '#fff',
            border: `1px solid ${color.border}`, borderRadius: 20, boxShadow: shadow.card, fontSize: 12, color: color.text2,
          }}
        >
          <span style={{ width: 8, height: 8, borderRadius: '50%', background: kernelUp ? color.latest : color.failed }} />
          kernel · {kernelUp ? 'warm' : 'offline'}
        </div>
        <button onClick={rerunAll} title="Re-run the whole graph" style={{ ...pill, background: color.ink, color: '#fff', border: 'none' }}>
          <Icon name="refresh" size={13} /> Rerun all
        </button>
      </div>
    </>
  )
}

const iconGhost: React.CSSProperties = {
  width: 28, height: 28, display: 'grid', placeItems: 'center', border: 'none',
  borderRadius: 8, background: 'transparent', color: color.text3,
}
const pill: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 14px', background: '#fff',
  border: `1px solid ${color.border}`, borderRadius: 20, boxShadow: shadow.card, fontSize: 12.5, fontWeight: 600,
}
