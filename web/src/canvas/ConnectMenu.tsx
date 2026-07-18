import { useEffect, useRef } from 'react'
import { kindsAcceptingWire } from '../nodes/registry'
import { color, kindAccent, radius, shadow, wire as wireTok, type WireType } from '../theme/tokens'

// Drag from an output port → a menu filtered to nodes whose first input accepts this type
// (FR-C2). You can only build valid graphs.
export function ConnectMenu({ x, y, wire, onPick, onFind, onClose }: {
  x: number; y: number; wire: WireType; onPick: (kind: string) => void; onFind: () => void; onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const candidates = kindsAcceptingWire(wire)

  useEffect(() => {
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onClose() }
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    // defer so the same click that opened doesn't immediately close
    const t = setTimeout(() => window.addEventListener('mousedown', onDoc), 0)
    window.addEventListener('keydown', onEsc)
    return () => { clearTimeout(t); window.removeEventListener('mousedown', onDoc); window.removeEventListener('keydown', onEsc) }
  }, [onClose])

  const tok = wireTok[wire] ?? wireTok.dataset

  return (
    <div
      ref={ref}
      className="dp-panel"
      style={{
        position: 'fixed', left: x, top: y, zIndex: 60, minWidth: 176,
        background: 'hsl(var(--popover))', border: `1px solid ${color.border}`, borderRadius: 12, boxShadow: shadow.panel,
        padding: 5, maxHeight: 320, overflowY: 'auto',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '6px 8px 8px' }}>
        <span style={{ width: 9, height: 9, borderRadius: tok.shape === 'square' ? 2 : '50%', background: tok.shape === 'ring' ? '#fff' : tok.color, border: `1.5px solid ${tok.color}`, transform: tok.shape === 'diamond' ? 'rotate(45deg)' : undefined }} />
        <span style={{ fontSize: 10, fontWeight: 600, letterSpacing: 0.4, textTransform: 'uppercase', color: color.text3 }}>accepts {wire}</span>
      </div>
      {candidates.map((s) => (
        <button
          key={s.kind}
          onClick={(e) => { e.stopPropagation(); onPick(s.kind) }}
          style={{ display: 'flex', alignItems: 'center', gap: 9, width: '100%', textAlign: 'left', padding: '7px 8px', border: 'none', background: 'transparent', borderRadius: 7 }}
          onMouseEnter={(e) => (e.currentTarget.style.background = 'hsl(var(--accent))')}
          onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
        >
          <span style={{ width: 4, height: 14, borderRadius: 2, background: kindAccent[s.kind] ?? color.text3 }} />
          <span style={{ display: 'flex', flexDirection: 'column' }}>
            <span style={{ fontSize: 12.5, fontWeight: 600, color: color.ink }}>{s.title}</span>
            <span style={{ fontSize: 10, color: color.text3 }}>{s.blurb}</span>
          </span>
        </button>
      ))}
      {candidates.length === 0 && <div style={{ padding: 10, fontSize: 11.5, color: color.text3 }}>no compatible node</div>}
      <button onClick={(e) => { e.stopPropagation(); onFind() }}
        style={{ width: '100%', marginTop: 3, padding: '7px 8px', border: 'none', borderTop: `1px solid ${color.border}`, background: 'transparent', color: color.text2, fontSize: 11, textAlign: 'left' }}>
        Search all nodes…
      </button>
    </div>
  )
}
