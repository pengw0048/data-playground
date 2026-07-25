import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Position, type EdgeProps } from '@xyflow/react'

const state = { doc: { nodes: [] as Array<{ id: string; data: { status?: string } }> } }

vi.mock('../store/graph', () => ({
  useStore: (selector: (store: typeof state) => unknown) => selector(state),
}))

vi.mock('../nodes/registry', () => ({ nodeOutputs: () => [] }))

import { WireEdge } from './WireEdge'

describe('WireEdge', () => {
  it('renders a visible, generous hit target for a persisted degenerate self-loop', () => {
    const props = {
      id: 'self-loop', source: 'node', target: 'node',
      // Match a standard 232px card: output on the right, input on the left.
      sourceX: 232, sourceY: 80, targetX: 0, targetY: 80,
      sourcePosition: Position.Right, targetPosition: Position.Left,
      sourceHandleId: null, targetHandleId: null, selected: false, markerEnd: 'dp-arrow', data: {},
    } as EdgeProps
    const { container } = render(<svg><WireEdge {...props} /></svg>)

    expect(container.querySelector('.react-flow__edge-path')).toHaveAttribute(
      'd', 'M232,80 C325,-59 -93,-59 0,80',
    )
    expect(container.querySelector('.react-flow__edge-interaction')).toHaveAttribute('stroke-width', '28')
  })
})
