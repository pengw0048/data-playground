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
      sourceX: 100, sourceY: 100, targetX: 100, targetY: 100,
      sourcePosition: Position.Right, targetPosition: Position.Left,
      sourceHandleId: null, targetHandleId: null, selected: false, markerEnd: 'dp-arrow', data: {},
    } as EdgeProps
    const { container } = render(<svg><WireEdge {...props} /></svg>)

    expect(container.querySelector('.react-flow__edge-path')).toHaveAttribute(
      'd', 'M100,100 C148,44 148,156 100,100',
    )
    expect(container.querySelector('.react-flow__edge-interaction')).toHaveAttribute('stroke-width', '28')
  })
})
