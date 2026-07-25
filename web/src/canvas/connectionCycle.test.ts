import { describe, expect, it } from 'vitest'
import {
  cycleConnectionReason, cycleGestureReason, graphConnectionFromGesture, graphHasCycle,
} from './connectionCycle'

const edge = (id: string, source: string, target: string) => ({
  id, source, target, data: { wire: 'dataset' as const },
})

describe('connection cycle diagnostics', () => {
  it('rejects self-loops and edges that close an indirect cycle', () => {
    const edges = [edge('a-b', 'a', 'b'), edge('b-c', 'b', 'c')]

    expect(cycleConnectionReason(edges, { source: 'a', target: 'a' })).toBe(
      'A node cannot connect to itself.',
    )
    expect(cycleConnectionReason(edges, { source: 'c', target: 'a' })).toBe(
      'This connection would create a cycle. Use a Section for control flow.',
    )
    expect(cycleConnectionReason(edges, { source: 'c', target: 'd' })).toBeNull()
  })

  it('ignores the edge being rerouted but still detects a newly-created cycle', () => {
    const edges = [edge('a-b', 'a', 'b'), edge('b-c', 'b', 'c')]

    expect(cycleConnectionReason(edges, { source: 'a', target: 'b' }, 'a-b')).toBeNull()
    expect(cycleConnectionReason(edges, { source: 'c', target: 'b' }, 'a-b')).toBe(
      'This connection would create a cycle. Use a Section for control flow.',
    )
  })

  it('normalizes source- and target-handle gestures into graph direction', () => {
    expect(graphConnectionFromGesture({
      fromNode: { id: 'a' }, toNode: { id: 'b' }, fromHandle: { type: 'source' },
    })).toEqual({ source: 'a', target: 'b' })
    expect(graphConnectionFromGesture({
      fromNode: { id: 'b' }, toNode: { id: 'c' }, fromHandle: { type: 'target' },
    })).toEqual({ source: 'c', target: 'b' })
  })

  it('explains a rejected source-end reconnect using graph direction, not gesture direction', () => {
    const edges = [edge('a-b', 'a', 'b'), edge('b-c', 'b', 'c')]
    const sourceEndReconnect = {
      // Moving the source end of A→B leaves B's target fixed, so React Flow starts the gesture at B.
      fromNode: { id: 'b' }, toNode: { id: 'c' }, fromHandle: { type: 'target' as const },
    }

    expect(cycleGestureReason(edges, sourceEndReconnect, 'a-b')).toBe(
      'This connection would create a cycle. Use a Section for control flow.',
    )
  })

  it('detects cycles in persisted graphs so callers can explain why no terminal run exists', () => {
    expect(graphHasCycle([edge('a-b', 'a', 'b'), edge('b-a', 'b', 'a')])).toBe(true)
    expect(graphHasCycle([edge('a-b', 'a', 'b')])).toBe(false)
  })
})
