import { Handle, Position } from '@xyflow/react'
import { wire as wireTokens, type WireType } from '../theme/tokens'
import type { PortSpec } from '../types/graph'

// A typed port. Shape + tint encode the wire type (design — wire types). Incompatible
// types can't connect — validity is enforced by the canvas onConnect check.
// Output port UX: drag connects; a plain CLICK opens the add-node menu (React Flow doesn't fire
// onConnectEnd on a no-move click, so we drive the menu off a real click event here — a drag to
// another node/pane doesn't fire a click on this handle, so it never pops the menu).
export function Port({ spec, side, index, count, nodeId }: {
  spec: PortSpec; side: 'input' | 'output'; index: number; count: number; nodeId?: string
}) {
  const w: WireType = (spec.wire as WireType) ?? 'dataset'
  const tok = wireTokens[w] ?? wireTokens.dataset
  const isSource = side === 'output'
  const top = count === 1 ? '50%' : `${((index + 1) / (count + 1)) * 100}%`

  const base: React.CSSProperties = {
    width: 11,
    height: 11,
    background: tok.shape === 'ring' ? '#fff' : tok.color,
    border: `1.5px solid ${tok.color}`,
    top,
    [isSource ? 'right' : 'left']: -6,
    transform: tok.shape === 'diamond'
      ? 'translateY(-50%) rotate(45deg)'
      : 'translateY(-50%)',
    borderRadius: tok.shape === 'square' || tok.shape === 'diamond' ? 2 : '50%',
    zIndex: 3,
    // output port affords "add": click opens the node menu, drag connects (Canvas onConnectEnd)
    cursor: isSource ? 'copy' : 'crosshair',
  }

  return (
    <Handle
      id={spec.id}
      type={isSource ? 'source' : 'target'}
      position={isSource ? Position.Right : Position.Left}
      style={base}
      isConnectable
      onClick={isSource && nodeId ? (e) => {
        e.stopPropagation()
        window.dispatchEvent(new CustomEvent('dp-port-click', {
          detail: { nodeId, handleId: spec.id, x: e.clientX, y: e.clientY },
        }))
      } : undefined}
    />
  )
}
