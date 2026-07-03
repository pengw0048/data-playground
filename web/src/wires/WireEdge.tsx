import { BaseEdge, getBezierPath, type EdgeProps } from '@xyflow/react'
import { color } from '../theme/tokens'
import { useStore } from '../store/graph'

// A typed wire: neutral gray by default; the active run path renders blue (P4, FR-E5).
export function WireEdge(props: EdgeProps) {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, source, target, selected, markerEnd } = props
  const [path] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition })

  const active = useStore((s) => {
    const src = s.doc.nodes.find((n) => n.id === source)
    const tgt = s.doc.nodes.find((n) => n.id === target)
    return src?.data.status === 'running' || tgt?.data.status === 'running'
  })

  const stroke = active ? color.wireActive : selected ? '#7f8792' : color.wire
  return (
    <BaseEdge
      id={id}
      path={path}
      markerEnd={active ? 'url(#dp-arrow-active)' : selected ? 'url(#dp-arrow-sel)' : markerEnd}
      style={{ stroke, strokeWidth: active ? 2.2 : 1.5, transition: 'stroke .15s' }}
    />
  )
}
