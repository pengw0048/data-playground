import { useReactFlow, useViewport } from '@xyflow/react'
import { useStore } from '../store/graph'

// Live cursors of other people on this canvas. Each peer broadcasts a flow-coordinate cursor; we map
// it to THIS client's screen via the current viewport (so it tracks correctly regardless of each
// person's pan/zoom). Re-renders on viewport change via useViewport.
export function PeerCursors() {
  const peers = useStore((s) => s.peers)
  const { flowToScreenPosition } = useReactFlow()
  useViewport() // re-render on pan/zoom so cursors stay anchored to canvas coordinates

  return (
    <div className="pointer-events-none absolute inset-0 z-20 overflow-hidden">
      {Object.entries(peers).map(([id, p]) => {
        if (!p.cursor) return null
        const s = flowToScreenPosition({ x: p.cursor.x, y: p.cursor.y })
        return (
          <div key={id} className="absolute" style={{ left: s.x, top: s.y, transform: 'translate(-2px,-2px)', transition: 'left .08s linear, top .08s linear' }}>
            <svg width="18" height="18" viewBox="0 0 18 18" className="block">
              <path d="M2 2 L2 14 L6 10 L9 16 L11 15 L8 9 L14 9 Z" fill={p.color} stroke="#fff" strokeWidth="1" />
            </svg>
            <span className="absolute left-[14px] top-3 whitespace-nowrap rounded-md px-1.5 py-px text-[10px] font-semibold text-white" style={{ background: p.color }}>{p.name}</span>
          </div>
        )
      })}
    </div>
  )
}
