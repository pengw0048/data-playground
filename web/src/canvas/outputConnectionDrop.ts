import type { FinalConnectionState } from '@xyflow/react'

/** A missing target is not necessarily blank Canvas: node bodies and overlay controls also have
 * no compatible handle. Only a new output gesture released on the actual pane offers a new step. */
export function outputConnectionDrop(
  state: FinalConnectionState,
  target: Element | null,
  canEdit: boolean,
  reconnectingEdgeId: string | null,
): { nodeId: string; handleId: string | null } | null {
  if (!canEdit || reconnectingEdgeId || state.isValid === true
      || !state.fromNode || state.fromHandle?.type !== 'source'
      || state.toNode || state.toHandle
      || !target?.classList.contains('react-flow__pane')) return null
  return { nodeId: state.fromNode.id, handleId: state.fromHandle.id ?? null }
}
