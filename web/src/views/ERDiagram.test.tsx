import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import type { CatalogTable } from '../types/api'

const mocks = vi.hoisted(() => ({
  tablesPage: vi.fn(), relationships: vi.fn(), facets: vi.fn(), joinSuggestions: vi.fn(),
  declareKey: vi.fn(), deleteRelationship: vi.fn(), addRelationship: vi.fn(), lineage: vi.fn(),
  tableByRegistration: vi.fn(), fitView: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks }))

const store = vi.hoisted(() => ({
  pushToast: vi.fn(),
  erFocusUri: null as string | null,
  erFocusDatasetId: null as string | null,
  erMode: 'joins' as 'joins' | 'lineage',
  erReturn: null as null | { resourceId: string; scope: 'all' | 'datasets'; datasetQuery?: string },
  setRelationshipsFocus: vi.fn(),
  setRelationshipsMode: vi.fn(),
  returnFromRelationships: vi.fn(),
  setView: vi.fn(),
}))
vi.mock('../store/graph', () => ({ useStore: (select: (state: typeof store) => unknown) => select(store) }))
vi.mock('../theme/mode', () => ({ resolvedTheme: () => 'light' }))

// React Flow's canvas geometry is irrelevant here; expose connection and Fit View deterministically.
vi.mock('@xyflow/react', () => ({
  ReactFlow: ({ nodes, edges, onConnect, onEdgeClick, children }: {
    nodes: { id: string; data: {
      table: CatalogTable; focused: boolean; onFocus: () => void
    } }[]
    edges: { id: string; data?: { rel?: unknown } }[]
    onConnect: (connection: { source: string; target: string }) => void
    onEdgeClick: (event: unknown, edge: { id: string; data?: { rel?: unknown } }) => void
    children?: ReactNode
  }) => <div data-testid="flow">
    {nodes.map((node) => <button key={node.id} data-testid={`node-${node.id}`}
      data-focused={String(node.data.focused)} onClick={node.data.onFocus}>{node.data.table.name}</button>)}
    <button disabled={nodes.length < 2} onClick={() => onConnect({ source: nodes[0].id, target: nodes[1].id })}>connect tables</button>
    {edges.filter((edge) => edge.data?.rel).map((edge) => (
      <button key={edge.id} data-testid={`edge-${edge.id}`} onClick={(event) => onEdgeClick(event, edge)}>relationship edge</button>
    ))}
    {children}
  </div>,
  Background: () => null,
  Controls: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  ControlButton: ({ children, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) => <button {...props}>{children}</button>,
  useReactFlow: () => ({ fitView: mocks.fitView }),
  Handle: () => null,
  Position: { Left: 'left', Right: 'right' },
  MarkerType: { ArrowClosed: 'arrow-closed' },
  BackgroundVariant: { Dots: 'dots' },
}))

import { ERDiagram } from './ERDiagram'

const ORDERS: CatalogTable = {
  id: 'orders', registrationId: 'registration-orders', name: 'orders', uri: 'mem://orders',
  columns: [{ name: 'customer_id', type: 'int', capabilities: ['key'] }],
}
const CUSTOMERS: CatalogTable = {
  id: 'customers', registrationId: 'registration-customers', name: 'customers', uri: 'mem://customers',
  columns: [{ name: 'id', type: 'int', capabilities: ['key'] }],
}
const PAGE = { items: [ORDERS, CUSTOMERS], total: 2, hasMore: false }

describe('ERDiagram request truth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    store.erFocusUri = null
    store.erFocusDatasetId = null
    store.erMode = 'joins'
    store.erReturn = null
    mocks.tablesPage.mockResolvedValue(PAGE)
    mocks.tableByRegistration.mockResolvedValue(ORDERS)
    mocks.relationships.mockResolvedValue([])
    mocks.facets.mockResolvedValue({ folders: [{ value: 'sales', count: 2 }], tags: [], owners: [] })
    mocks.joinSuggestions.mockResolvedValue([])
    mocks.declareKey.mockResolvedValue(ORDERS)
    mocks.deleteRelationship.mockResolvedValue([])
    mocks.addRelationship.mockResolvedValue([])
    mocks.lineage.mockResolvedValue({ rootUri: ORDERS.uri, nodes: [], edges: [] })
    mocks.fitView.mockReset()
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

  it('explains how to edit the relationship graph', async () => {
    render(<ERDiagram />)

    fireEvent.click(screen.getByRole('button', { name: 'How this works' }))

    expect(await screen.findByText(/Drag from one entity to another to declare a join/)).toBeVisible()
    expect(screen.getByText(/Click a solid edge to remove it/)).toBeVisible()
  })

  it('requires an in-app confirmation before removing a declared relationship', async () => {
    const relationship = {
      leftUri: ORDERS.uri, leftColumns: ['customer_id'],
      rightUri: CUSTOMERS.uri, rightColumns: ['id'],
      cardinality: 'N:1' as const, confidence: 'declared' as const,
    }
    mocks.relationships.mockResolvedValue([relationship])
    render(<ERDiagram />)

    fireEvent.click(await screen.findByTestId('edge-d0'))
    const dialog = screen.getByRole('dialog', { name: 'Remove relationship?' })
    expect(dialog).toHaveTextContent('customer_id = id')
    expect(mocks.deleteRelationship).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(mocks.deleteRelationship).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('edge-d0'))
    fireEvent.click(screen.getByRole('button', { name: 'Remove relationship' }))

    await waitFor(() => expect(mocks.deleteRelationship).toHaveBeenCalledWith(relationship))
  })

  it('uses the same safe insets for the React Flow Fit View control', async () => {
    render(<ERDiagram />)

    await screen.findByText('orders')
    fireEvent.click(screen.getByRole('button', { name: 'Fit view' }))

    expect(mocks.fitView).toHaveBeenCalledWith({
      padding: { top: '164px', right: '16px', bottom: '16px', left: '344px' }, maxZoom: 1,
    })
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

    expect(await within(screen.getByTestId('er-focus-bar')).findByText('orders-current')).toBeInTheDocument()
    expect(screen.getByTestId('node-orders')).toHaveAttribute('data-focused', 'true')
    await waitFor(() => expect(mocks.lineage).toHaveBeenLastCalledWith(
      currentOrders.uri, 1, 60))
  })

  it('keeps a provider dataset visible when it is not registered in the local Catalog', async () => {
    const providerUri = 'luma-data-exact://table/1437/revision/451'
    store.erFocusUri = providerUri
    store.erMode = 'lineage'
    mocks.lineage.mockResolvedValue({
      rootUri: providerUri,
      nodes: [
        { id: 'provider-1437', name: 'raw_video_v2', uri: providerUri, kind: 'table' },
        { id: CUSTOMERS.id, name: CUSTOMERS.name, uri: CUSTOMERS.uri, kind: 'table' },
      ],
      edges: [{ parent: providerUri, child: CUSTOMERS.uri, factCount: 1 }],
    })
    mocks.tablesPage.mockResolvedValue({ items: [CUSTOMERS], total: 1, hasMore: false })
    render(<ERDiagram />)

    expect(await screen.findByText('Current dataset')).toBeInTheDocument()
    expect(within(screen.getByTestId('er-focus-bar')).getByText('raw_video_v2')).toBeInTheDocument()
    expect(screen.getByTestId(`node-lineage:${providerUri}`)).toHaveAttribute('data-focused', 'true')
    expect(screen.getByText('customers')).toBeInTheDocument()
    expect(mocks.tablesPage).toHaveBeenCalledWith({ uris: [providerUri, CUSTOMERS.uri], limit: 60 })
  })

  it('restores a routed stable focus in lineage mode and returns to its Dataset', async () => {
    store.erFocusDatasetId = ORDERS.registrationId!
    store.erMode = 'lineage'
    store.erReturn = {
      resourceId: `dataset:${ORDERS.registrationId}`,
      scope: 'datasets',
      datasetQuery: 'revision=revision-1&revisionDataset=logical-orders',
    }
    mocks.lineage.mockResolvedValue({
      rootUri: ORDERS.uri,
      nodes: [{ id: ORDERS.id, name: ORDERS.name, uri: ORDERS.uri, kind: 'table' }],
      edges: [],
    })
    render(<ERDiagram />)

    expect(await screen.findByText('Current dataset')).toBeInTheDocument()
    expect(screen.queryByTestId('er-mode-lineage')).not.toBeInTheDocument()
    expect(mocks.tableByRegistration).toHaveBeenCalledWith(ORDERS.registrationId)
    await waitFor(() => expect(mocks.lineage).toHaveBeenCalledWith(ORDERS.uri, 1, 60))

    fireEvent.click(screen.getByRole('button', { name: 'Back to dataset' }))
    expect(store.returnFromRelationships).toHaveBeenCalledOnce()
  })
})
