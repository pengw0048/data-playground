import { BaseEdge, getBezierPath, type EdgeProps } from '@xyflow/react'
import { color, wire, type WireType } from '../theme/tokens'
import { useStore } from '../store/graph'
import { nodeOutputs } from '../nodes/registry'

// A typed wire: tinted by its wire type (a dataset / selection / sample / metric edge reads at a
// glance, not only at the ports); the active run path renders blue (P4, FR-E5).
export function WireEdge(props: EdgeProps) {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, source, target, sourceHandleId, selected, markerEnd } = props
  const [path] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition })

  // two primitive selectors (not a new object) so an edge doesn't re-render on every unrelated change
  const active = useStore((s) => {
    const src = s.doc.nodes.find((n) => n.id === source)
    const tgt = s.doc.nodes.find((n) => n.id === target)
    return src?.data.status === 'running' || tgt?.data.status === 'running'
  })
  const wt = useStore((s) => {
    const src = s.doc.nodes.find((n) => n.id === source)
    const outs = src ? nodeOutputs(src) : []
    return ((outs.find((p) => p.id === sourceHandleId) ?? outs[0])?.wire) as WireType | undefined
  })

  const typed = (wt && wire[wt]?.color) || color.wire
  const stroke = active ? color.wireActive : selected ? '#7f8792' : typed
  return (
    <BaseEdge
      id={id}
      path={path}
      markerEnd={active ? 'url(#dp-arrow-active)' : selected ? 'url(#dp-arrow-sel)' : markerEnd}
      style={{ stroke, strokeWidth: active ? 2.2 : 1.5, transition: 'stroke .15s' }}
    />
  )
}
