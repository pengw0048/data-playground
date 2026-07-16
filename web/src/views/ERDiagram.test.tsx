import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import type { CatalogTable } from '../types/api'

const mocks = vi.hoisted(() => ({
  tablesPage: vi.fn(), relationships: vi.fn(), facets: vi.fn(), joinSuggestions: vi.fn(),
  declareKey: vi.fn(), deleteRelationship: vi.fn(), addRelationship: vi.fn(), lineage: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks }))

const store = vi.hoisted(() => ({
  pushToast: vi.fn(), erFocusUri: null as string | null, setView: vi.fn(),
}))
vi.mock('../store/graph', () => ({ useStore: (select: (state: typeof store) => unknown) => select(store) }))
vi.mock('../theme/mode', () => ({ resolvedTheme: () => 'light' }))

// React Flow's canvas geometry is irrelevant here; expose connection as a deterministic button.
vi.mock('@xyflow/react', () => ({
  ReactFlow: ({ nodes, onConnect, children }: {
    nodes: { id: string; data: {
      table: CatalogTable; focused: boolean; onFocus: () => void
    } }[]
    onConnect: (connection: { source: string; target: string }) => void
    children?: ReactNode
  }) => <div data-testid="flow">
    {nodes.map((node) => <button key={node.id} data-testid={`node-${node.id}`}
      data-focused={String(node.data.focused)} onClick={node.data.onFocus}>{node.data.table.name}</button>)}
    <button disabled={nodes.length < 2} onClick={() => onConnect({ source: nodes[0].id, target: nodes[1].id })}>connect tables</button>
    {children}
  </div>,
  Background: () => null,
  Controls: () => null,
  Handle: () => null,
  Position: { Left: 'left', Right: 'right' },
  MarkerType: { ArrowClosed: 'arrow-closed' },
  BackgroundVariant: { Dots: 'dots' },
}))

import { ERDiagram } from './ERDiagram'

const ORDERS: CatalogTable = {
  id: 'orders', name: 'orders', uri: 'mem://orders', columns: [{ name: 'customer_id', type: 'int', capabilities: ['key'] }],
}
const CUSTOMERS: CatalogTable = {
  id: 'customers', name: 'customers', uri: 'mem://customers', columns: [{ name: 'id', type: 'int', capabilities: ['key'] }],
}
const PAGE = { items: [ORDERS, CUSTOMERS], total: 2, hasMore: false }

describe('ERDiagram request truth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    store.erFocusUri = null
    mocks.tablesPage.mockResolvedValue(PAGE)
    mocks.relationships.mockResolvedValue([])
    mocks.facets.mockResolvedValue({ folders: [{ value: 'sales', count: 2 }], tags: [], owners: [] })
    mocks.joinSuggestions.mockResolvedValue([])
    mocks.declareKey.mockResolvedValue(ORDERS)
    mocks.deleteRelationship.mockResolvedValue([])
    mocks.addRelationship.mockResolvedValue([])
    mocks.lineage.mockResolvedValue({ rootUri: ORDERS.uri, nodes: [], edges: [] })
  })
  afterEach(() => cleanup())

  it('shows catalog and relationship load failures with independent retries', async () => {
    mocks.tablesPage.mockRejectedValueOnce(new Error('Failed to fetch')).mockResolvedValueOnce(PAGE)
    mocks.relationships.mockRejectedValueOnce(new Error('HTTP 401: relationships denied')).mockResolvedValueOnce([])
    render(<ERDiagram />)

    expect(await screen.findByText(/Couldn't load: Failed to fetch/i)).toBeInTheDocument()
    expect(screen.getByText(/Couldn't load declared relationships: HTTP 401/i)).toBeInTheDocument()
    expect(screen.queryByText(/No datasets registered/i)).toBeNull()

    fireEvent.click(screen.getByTestId('er-catalog-retry'))
    fireEvent.click(screen.getByTestId('er-relationships-retry'))
    expect(await screen.findByText('orders')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryAllByRole('alert')).toHaveLength(0))

    // switching folder must not leave the previous folder's rows under the new filter
    mocks.tablesPage.mockRejectedValueOnce(new Error('HTTP 503: sales folder unavailable'))
    fireEvent.change(screen.getByTestId('er-folder'), { target: { value: 'sales' } })
    expect(await screen.findByText(/Couldn't load: HTTP 503/i)).toBeInTheDocument()
    expect(screen.queryByText('orders')).toBeNull()
    fireEvent.click(screen.getByTestId('er-catalog-retry'))
    expect(await screen.findByText('orders')).toBeInTheDocument()
  })

  it('labels join-suggestion failure, preserves manual editing, and retries without pretending there are no suggestions', async () => {
    mocks.joinSuggestions
      .mockRejectedValueOnce(new Error('HTTP 502: suggestion engine unavailable'))
      .mockResolvedValueOnce([{ leftColumns: ['customer_id'], rightColumns: ['id'], cardinality: 'N:1', confidence: 'verified', score: 1, reason: 'key match' }])
    render(<ERDiagram />)
    await screen.findByText('orders')
    fireEvent.click(screen.getByText('connect tables'))

    expect(await screen.findByText(/Join suggestions unavailable: HTTP 502/i)).toBeInTheDocument()
    expect(screen.getByText(/still choose keys manually/i)).toBeInTheDocument()
    expect(screen.getAllByText('customer_id').length).toBeGreaterThan(0)

    fireEvent.click(screen.getByTestId('er-suggestions-retry'))
    expect(await screen.findByText(/customer_id = id/i)).toBeInTheDocument()
    expect(screen.queryByText(/suggestions unavailable/i)).toBeNull()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Declare' })).toBeEnabled())
  })

  it('uses the canonical lineage root when a focused physical generation advances', async () => {
    const currentOrders = { ...ORDERS, name: 'orders-current', uri: 'mem://orders-current' }
    store.erFocusUri = ORDERS.uri
    mocks.tablesPage.mockResolvedValue({
      items: [currentOrders, CUSTOMERS], total: 2, hasMore: false,
    })
    mocks.lineage.mockResolvedValue({
      rootUri: currentOrders.uri,
      nodes: [
        { id: currentOrders.id, name: currentOrders.name, uri: currentOrders.uri, kind: 'table' },
        { id: CUSTOMERS.id, name: CUSTOMERS.name, uri: CUSTOMERS.uri, kind: 'table' },
      ],
      edges: [{ parent: currentOrders.uri, child: CUSTOMERS.uri, factCount: 1 }],
    })
    render(<ERDiagram />)

    fireEvent.click(await screen.findByTestId('er-mode-lineage'))

    expect(await screen.findByText('Focused: orders-current')).toBeInTheDocument()
    expect(screen.getByTestId('node-orders')).toHaveAttribute('data-focused', 'true')
    await waitFor(() => expect(mocks.lineage).toHaveBeenLastCalledWith(
      currentOrders.uri, 1, 60))
  })
})
