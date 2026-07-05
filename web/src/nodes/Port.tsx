import { useState } from 'react'
import { Handle, Position } from '@xyflow/react'
import { wire as wireTokens, type WireType } from '../theme/tokens'
import { useStore } from '../store/graph'
import type { PortSpec } from '../types/graph'

// A typed port. Shape + tint encode the wire type (design — wire types). Incompatible
// types can't connect — validity is enforced by the canvas onConnect check.
// Affordance: an UNCONNECTED port is a hollow (outline) shape; a connected one is filled. On hover the
// port grows and (output side) shows a "+" — the visible affordance, instead of only a cursor change.
// Output port UX: drag connects; a plain CLICK opens the add-node menu (React Flow doesn't fire
// onConnectEnd on a no-move click, so we drive the menu off a real click event here).
export function Port({ spec, side, index, count, nodeId }: {
  spec: PortSpec; side: 'input' | 'output'; index: number; count: number; nodeId?: string
}) {
  const w: WireType = (spec.wire as WireType) ?? 'dataset'
  const tok = wireTokens[w] ?? wireTokens.dataset
  const isSource = side === 'output'
  const top = count === 1 ? '50%' : `${((index + 1) / (count + 1)) * 100}%`
  const [hover, setHover] = useState(false)
  const connected = useStore((s) => s.doc.edges.some((e) => isSource
    ? e.source === nodeId && (e.sourceHandle == null || e.sourceHandle === spec.id)
    : e.target === nodeId && (e.targetHandle == null || e.targetHandle === spec.id)))
  const round = tok.shape !== 'square' && tok.shape !== 'diamond'

  const base: React.CSSProperties = {
    width: hover ? 15 : 11,
    height: hover ? 15 : 11,
    // hollow when nothing is wired; filled once connected (or while hovering, to preview the target)
    background: connected || hover ? tok.color : '#fff',
    border: `1.5px solid ${tok.color}`,
    top,
    [isSource ? 'right' : 'left']: hover ? -8 : -6,
    transform: tok.shape === 'diamond' ? 'translateY(-50%) rotate(45deg)' : 'translateY(-50%)',
    borderRadius: round ? '50%' : 2,
    zIndex: 3,
    display: 'grid',
    placeItems: 'center',
    transition: 'width .1s, height .1s, background .1s',
    cursor: isSource ? 'copy' : 'crosshair',
  }

  return (
    <Handle
      id={spec.id}
      type={isSource ? 'source' : 'target'}
      position={isSource ? Position.Right : Position.Left}
      style={base}
      isConnectable
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={isSource && nodeId ? (e) => {
        e.stopPropagation()
        window.dispatchEvent(new CustomEvent('dp-port-click', {
          detail: { nodeId, handleId: spec.id, x: e.clientX, y: e.clientY },
        }))
      } : undefined}
    >
      {/* the "+" add-affordance on an output port, shown on hover (counter-rotated for diamonds) */}
      {isSource && hover && (
        <span style={{ color: '#fff', fontSize: 11, lineHeight: 1, fontWeight: 700, pointerEvents: 'none',
          transform: tok.shape === 'diamond' ? 'rotate(-45deg)' : undefined }}>+</span>
      )}
    </Handle>
  )
}
