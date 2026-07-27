import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { kindsAcceptingWire } from '../nodes/registry'
import { color, kindAccent, radius, shadow, wire as wireTok, type WireType } from '../theme/tokens'

// Drag from an output port → a menu filtered to nodes whose first input accepts this type
// (FR-C2). You can only build valid graphs.
const MENU_WIDTH = 300
const MENU_MAX_HEIGHT = 320
const VIEWPORT_GUTTER = 12

function clampMenuPosition(value: number, viewport: number, size: number) {
  const max = Math.max(VIEWPORT_GUTTER, viewport - VIEWPORT_GUTTER - size)
  return Math.max(VIEWPORT_GUTTER, Math.min(value, max))
}

export function ConnectMenu({ x, y, wire, onPick, onFind, onClose }: {
  x: number; y: number; wire: WireType; onPick: (kind: string) => void; onFind: () => void; onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const candidates = kindsAcceptingWire(wire)
  // Start with the bounded box so the first paint is on-screen. Once laid out, use its real
  // height — short menus then stay adjacent to a low output port instead of jumping up by 320px.
  const [menuSize, setMenuSize] = useState({ width: MENU_WIDTH, height: MENU_MAX_HEIGHT })
  const [viewportVersion, setViewportVersion] = useState(0)

  useEffect(() => {
    const onResize = () => setViewportVersion((version) => version + 1)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  useLayoutEffect(() => {
    const rect = ref.current?.getBoundingClientRect()
    if (rect && rect.width > 0 && rect.height > 0) {
      setMenuSize({ width: rect.width, height: rect.height })
    }
  }, [candidates.length, viewportVersion])

  useEffect(() => {
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onClose() }
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    // defer so the same click that opened doesn't immediately close
    const t = setTimeout(() => window.addEventListener('mousedown', onDoc), 0)
    window.addEventListener('keydown', onEsc)
    return () => { clearTimeout(t); window.removeEventListener('mousedown', onDoc); window.removeEventListener('keydown', onEsc) }
  }, [onClose])

  const tok = wireTok[wire] ?? wireTok.dataset
  const availableWidth = Math.max(1, window.innerWidth - VIEWPORT_GUTTER * 2)
  const availableHeight = Math.max(1, window.innerHeight - VIEWPORT_GUTTER * 2)
  const width = Math.min(MENU_WIDTH, availableWidth)
  const height = Math.min(menuSize.height, Math.min(MENU_MAX_HEIGHT, availableHeight))
  const left = clampMenuPosition(x, window.innerWidth, Math.min(menuSize.width, width))
  const top = clampMenuPosition(y, window.innerHeight, height)

  return (
    <div
      ref={ref}
      className="dp-panel"
      style={{
        position: 'fixed', left, top, zIndex: 60, width: MENU_WIDTH,
        maxWidth: `calc(100vw - ${VIEWPORT_GUTTER * 2}px)`, boxSizing: 'border-box',
        background: 'hsl(var(--popover))', border: `1px solid ${color.border}`, borderRadius: 12, boxShadow: shadow.panel,
        padding: 5, maxHeight: `min(${MENU_MAX_HEIGHT}px, calc(100vh - ${VIEWPORT_GUTTER * 2}px))`, overflowY: 'auto',
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
          <span style={{ display: 'flex', minWidth: 0, flexDirection: 'column' }}>
            <span style={{ overflowWrap: 'anywhere', fontSize: 12.5, fontWeight: 600, color: color.ink }}>{s.title}</span>
            <span style={{ overflowWrap: 'anywhere', fontSize: 10, color: color.text3 }}>{s.blurb}</span>
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
