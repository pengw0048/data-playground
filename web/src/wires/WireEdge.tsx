import { BaseEdge, getBezierPath, type EdgeProps } from '@xyflow/react'
import { color, wire, type WireType } from '../theme/tokens'
import { useStore } from '../store/graph'
import { nodeOutputs } from '../nodes/registry'

// A typed wire: tinted by its wire type (a dataset / selection / sample / metric edge reads at a
// glance, not only at the ports); the active run path renders blue (P4, FR-E5).
export function WireEdge(props: EdgeProps) {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, source, target, sourceHandleId, selected, markerEnd, data } = props
  // A persisted self-loop can have coincident handle coordinates. getBezierPath then produces a
  // zero-length path, which makes the otherwise valid edge impossible to select and delete.
  // Draw it outside the node instead; new loops are rejected before this renderer is reached.
  const [path] = source === target
    ? (() => {
        // Standard nodes place outputs on the right and inputs on the left, so a self-loop's
        // endpoints can be a full card width apart. Route beyond both sides and above the card;
        // a small coincident-endpoint loop uses the same bounded minimum clearance.
        const span = Math.abs(sourceX - targetX)
        const horizontalPad = Math.max(72, Math.round(span * 0.4))
        const verticalPad = Math.max(96, Math.round(span * 0.6))
        const loopY = Math.min(sourceY, targetY) - verticalPad
        return [`M${sourceX},${sourceY} C${sourceX + horizontalPad},${loopY} ${targetX - horizontalPad},${loopY} ${targetX},${targetY}`]
      })()
    : getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition })
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
      data-source={source}
      data-target={target}
      path={path}
      markerEnd={active ? 'url(#dp-arrow-active)' : selected ? 'url(#dp-arrow-sel)' : markerEnd}
      interactionWidth={28}
      style={{ stroke, strokeWidth: active ? 2.2 : 1.5, strokeDasharray: warned && !active && !selected ? '5 3' : undefined, transition: 'stroke .15s' }}
    />
  )
}
