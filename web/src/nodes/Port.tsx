import { Handle, Position } from '@xyflow/react'
import { wire as wireTokens, type WireType } from '../theme/tokens'
import type { PortSpec } from '../types/graph'

// A typed port. Shape + tint encode the wire type (design — wire types). Incompatible
// types can't connect — validity is enforced by the canvas onConnect check.
export function Port({ spec, side, index, count }: {
  spec: PortSpec; side: 'input' | 'output'; index: number; count: number
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
  }

  return (
    <Handle
      id={spec.id}
      type={isSource ? 'source' : 'target'}
      position={isSource ? Position.Right : Position.Left}
      style={base}
      isConnectable
    />
  )
}
