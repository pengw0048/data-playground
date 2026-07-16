import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ lineage: vi.fn() }))
vi.mock('../api/client', () => ({ api: mocks }))

const store = vi.hoisted(() => ({
  doc: {
    id: 'canvas', name: 'Canvas', version: 1, requirements: [], edges: [],
    nodes: [{
      id: 'source', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'orders', status: 'latest', config: { uri: 'mem://orders' } },
    }],
  },
}))
vi.mock('../store/graph', () => ({ useStore: (select: (state: typeof store) => unknown) => select(store) }))

import { LineagePanel } from './LineagePanel'

describe('LineagePanel', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    store.doc.nodes[0].data.config = { uri: 'mem://orders' }
  })

  it('labels aggregated lineage edges with singular and plural fact counts', async () => {
    mocks.lineage.mockResolvedValue({
      rootUri: 'mem://orders-current',
      nodes: [
        { id: 'raw', name: 'raw_orders', uri: 'mem://raw-orders', kind: 'table' },
        { id: 'orders', name: 'orders', uri: 'mem://orders-current', kind: 'table' },
        { id: 'daily', name: 'daily_orders', uri: 'mem://daily-orders', kind: 'table' },
      ],
      edges: [
        { parent: 'mem://raw-orders', child: 'mem://orders-current', factCount: 1 },
        { parent: 'mem://orders-current', child: 'mem://daily-orders', factCount: 2 },
      ],
    })

    render(<LineagePanel nodeId="source" />)

    expect(await screen.findByText('raw_orders')).toBeInTheDocument()
    expect(screen.getByText(/1 fact$/)).toBeInTheDocument()
    expect(screen.getByText(/2 facts$/)).toBeInTheDocument()
    expect(mocks.lineage).toHaveBeenCalledWith('mem://orders', 6, 200)
  })

  it('clears a missing-dataset error when the node later gets a URI', async () => {
    store.doc.nodes[0].data.config = {}
    mocks.lineage.mockResolvedValue({
      rootUri: 'mem://new',
      nodes: [{ id: 'new', name: 'new_orders', uri: 'mem://new', kind: 'table' }],
      edges: [],
    })
    const view = render(<LineagePanel nodeId="source" />)
    expect(screen.getByText(/no registered dataset yet/i)).toBeInTheDocument()

    store.doc.nodes[0].data.config = { uri: 'mem://new' }
    view.rerender(<LineagePanel nodeId="source" />)

    expect(await screen.findByText('new_orders')).toBeInTheDocument()
    expect(screen.queryByText(/no registered dataset yet/i)).toBeNull()
  })

  it('does not let a slow stale request replace a newer URI result', async () => {
    let resolveOld!: (value: unknown) => void
    const oldRequest = new Promise((resolve) => { resolveOld = resolve })
    mocks.lineage
      .mockReturnValueOnce(oldRequest)
      .mockResolvedValueOnce({
        rootUri: 'mem://new',
        nodes: [{ id: 'new', name: 'new_orders', uri: 'mem://new', kind: 'table' }],
        edges: [],
      })
    const view = render(<LineagePanel nodeId="source" />)

    store.doc.nodes[0].data.config = { uri: 'mem://new' }
    view.rerender(<LineagePanel nodeId="source" />)
    expect(await screen.findByText('new_orders')).toBeInTheDocument()

    await act(async () => resolveOld({
      rootUri: 'mem://orders',
      nodes: [{ id: 'old', name: 'stale_orders', uri: 'mem://orders', kind: 'table' }],
      edges: [],
    }))
    expect(screen.queryByText('stale_orders')).toBeNull()
    expect(screen.getByText('new_orders')).toBeInTheDocument()
  })
})
