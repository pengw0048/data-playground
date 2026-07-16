import { BaseEdge, getBezierPath, type EdgeProps } from '@xyflow/react'
import { color, wire, type WireType } from '../theme/tokens'
import { useStore } from '../store/graph'
import { nodeOutputs } from '../nodes/registry'

// A typed wire: tinted by its wire type (a dataset / selection / sample / metric edge reads at a
// glance, not only at the ports); the active run path renders blue (P4, FR-E5).
export function WireEdge(props: EdgeProps) {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, source, target, sourceHandleId, selected, markerEnd, data } = props
  const [path] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition })
  const warned = !!(data as { warned?: boolean } | undefined)?.warned  // target references a missing column

  // two primitive selectors (not a new object) so an edge doesn't re-render on every unrelated change
  const active = useStore((s) => {
    const src = s.doc.nodes.find((n) => n.id === source)
    const tgt = s.doc.nodes.find((n) => n.id === target)
    return src?.data.status === 'running' || tgt?.data.status === 'running'
  })
  const wt = useStore((s) => {
    const src = s.doc.nodes.find((n) => n.id === source)
    const outs = src ? nodeOutputs(src) : []
    const output = sourceHandleId
      ? outs.find((port) => port.id === sourceHandleId)
      : outs.length === 1 ? outs[0] : undefined
    return output?.wire as WireType | undefined
  })

  const typed = (wt && wire[wt]?.color) || color.wire
  // amber when the downstream node references a column its input doesn't have — a "connects, but check it"
  // cue (priority below the active run path + selection). Literal color: var() doesn't resolve in SVG stroke.
  const stroke = active ? color.wireActive : selected ? '#7f8792' : warned ? '#d97706' : typed
  return (
    <BaseEdge
      id={id}
      path={path}
      markerEnd={active ? 'url(#dp-arrow-active)' : selected ? 'url(#dp-arrow-sel)' : markerEnd}
      style={{ stroke, strokeWidth: active ? 2.2 : 1.5, strokeDasharray: warned && !active && !selected ? '5 3' : undefined, transition: 'stroke .15s' }}
    />
  )
}
