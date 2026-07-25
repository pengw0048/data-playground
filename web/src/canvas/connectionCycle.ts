import type { CanvasEdge } from '../types/graph'

type CandidateConnection = {
  source: string | null | undefined
  target: string | null | undefined
}

export type ConnectionGesture = {
  fromNode: { id: string } | null
  toNode: { id: string } | null
  fromHandle: { type: 'source' | 'target' } | null
}

/**
 * React Flow reports the fixed end of a reconnect as `fromNode`, so these values describe the
 * gesture rather than graph direction. A gesture that starts from a target handle runs backwards:
 * the node reached by the pointer becomes the graph source.
 */
export function graphConnectionFromGesture(
  gesture: ConnectionGesture,
): CandidateConnection | null {
  const { fromNode, toNode, fromHandle } = gesture
  if (!fromNode || !toNode || !fromHandle) return null
  return fromHandle.type === 'target'
    ? { source: toNode.id, target: fromNode.id }
    : { source: fromNode.id, target: toNode.id }
}

/**
 * Returns the user-facing reason when adding a directed edge would make the canvas cyclic.
 *
 * This deliberately only diagnoses the client graph. The kernel remains authoritative when a
 * graph is executed, including for documents created by older clients or imported externally.
 */
export function cycleConnectionReason(
  edges: readonly CanvasEdge[],
  connection: CandidateConnection,
  ignoredEdgeId?: string | null,
): string | null {
  const { source, target } = connection
  if (!source || !target) return null
  if (source === target) return 'A node cannot connect to itself.'

  // source → target closes a cycle exactly when target can already reach source. Ignore the edge
  // currently being rerouted: otherwise re-dropping it on its existing target looks cyclic.
  const outgoing = new Map<string, string[]>()
  for (const edge of edges) {
    if (edge.id === ignoredEdgeId) continue
    outgoing.set(edge.source, [...(outgoing.get(edge.source) ?? []), edge.target])
  }
  const pending = [target]
  const seen = new Set<string>()
  while (pending.length) {
    const nodeId = pending.pop()!
    if (nodeId === source) {
      return 'This connection would create a cycle. Use a Section for control flow.'
    }
    if (seen.has(nodeId)) continue
    seen.add(nodeId)
    pending.push(...(outgoing.get(nodeId) ?? []))
  }
  return null
}

export function cycleGestureReason(
  edges: readonly CanvasEdge[],
  gesture: ConnectionGesture,
  ignoredEdgeId?: string | null,
): string | null {
  const connection = graphConnectionFromGesture(gesture)
  return connection ? cycleConnectionReason(edges, connection, ignoredEdgeId) : null
}

/** True when a persisted/imported graph contains any directed cycle. */
export function graphHasCycle(edges: readonly CanvasEdge[]): boolean {
  const outgoing = new Map<string, string[]>()
  const nodes = new Set<string>()
  for (const edge of edges) {
    nodes.add(edge.source)
    nodes.add(edge.target)
    outgoing.set(edge.source, [...(outgoing.get(edge.source) ?? []), edge.target])
  }
  const visiting = new Set<string>()
  const visited = new Set<string>()
  const visit = (nodeId: string): boolean => {
    if (visiting.has(nodeId)) return true
    if (visited.has(nodeId)) return false
    visiting.add(nodeId)
    for (const next of outgoing.get(nodeId) ?? []) if (visit(next)) return true
    visiting.delete(nodeId)
    visited.add(nodeId)
    return false
  }
  return [...nodes].some(visit)
}
