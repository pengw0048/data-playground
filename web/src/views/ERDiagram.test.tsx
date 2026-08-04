import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import type { CatalogTable } from '../types/api'

const mocks = vi.hoisted(() => ({
  tablesPage: vi.fn(), relationships: vi.fn(), facets: vi.fn(), joinSuggestions: vi.fn(),
  declareKey: vi.fn(), deleteRelationship: vi.fn(), addRelationship: vi.fn(), lineage: vi.fn(),
  tableByRegistration: vi.fn(), workspaceResource: vi.fn(), workspaceCanonicalDataset: vi.fn(),
  workspaceLineageResource: vi.fn(), fitView: vi.fn(),
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
  setWorkspaceResource: vi.fn(),
}))
vi.mock('../store/graph', () => ({ useStore: (select: (state: typeof store) => unknown) => select(store) }))
vi.mock('../theme/mode', () => ({ resolvedTheme: () => 'light' }))

// React Flow's canvas geometry is irrelevant here; expose connection and Fit View deterministically.
vi.mock('@xyflow/react', () => ({
  ReactFlow: ({ nodes, edges, onConnect, onEdgeClick, onNodesChange, onMove, nodesDraggable, children }: {
    nodes: { id: string; data: {
      table: CatalogTable; fields: Array<{ name: string; role: string }>
      focused: boolean; lineage: boolean; onFocus: () => void; onOpen: () => void
    }; position: { x: number; y: number } }[]
    edges: { id: string; data?: { rel?: unknown }; sourceHandle?: string; targetHandle?: string; label?: string; style?: { stroke?: string } }[]
    onConnect: (connection: { source: string; target: string }) => void
    onEdgeClick: (event: unknown, edge: { id: string; data?: { rel?: unknown } }) => void
    onNodesChange?: (changes: Array<{ id: string; type: 'position'; position: { x: number; y: number } }>) => void
    onMove?: (event: unknown, viewport: { zoom: number }) => void
    nodesDraggable?: boolean
    children?: ReactNode
  }) => <div data-testid="flow" data-nodes-draggable={String(nodesDraggable)}>
    {nodes.map((node) => <button key={node.id} data-testid={`node-${node.id}`}
      data-focused={String(node.data.focused)} data-x={node.position.x} data-y={node.position.y}
      data-fields={node.data.fields.map((field) => `${field.role}:${field.name}`).join(',')}
      onClick={node.data.lineage ? node.data.onOpen : node.data.onFocus}>{node.data.table.name}</button>)}
    <button onClick={() => onMove?.({}, { zoom: 1 })}>zoom graph</button>
    <button disabled={!nodes.length || !onNodesChange}
      onClick={() => onNodesChange?.([{ id: nodes[0].id, type: 'position', position: { x: 777, y: 888 } }])}>
      move first node
    </button>
    <button disabled={nodes.length < 2} onClick={() => onConnect({ source: nodes[0].id, target: nodes[1].id })}>connect tables</button>
    {edges.filter((edge) => edge.data?.rel).map((edge) => (
      <button key={edge.id} data-testid={`edge-${edge.id}`} onClick={(event) => onEdgeClick(event, edge)}>relationship edge</button>
    ))}
    {edges.map((edge) => <span key={`shape-${edge.id}`} data-testid={`edge-shape-${edge.id}`}
      data-source-handle={edge.sourceHandle} data-target-handle={edge.targetHandle}
      data-stroke={edge.style?.stroke}>{edge.label}</span>)}
    {children}
  </div>,
  Background: () => null,
  Controls: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  ControlButton: ({ children, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) => <button {...props}>{children}</button>,
  useReactFlow: () => ({ fitView: mocks.fitView }),
  useViewport: () => ({ x: 0, y: 0, zoom: 0.5 }),
  Handle: () => null,
  Position: { Left: 'left', Right: 'right' },
  MarkerType: { ArrowClosed: 'arrow-closed' },
  BackgroundVariant: { Dots: 'dots' },
}))

import { ERDiagram, EntityNode } from './ERDiagram'

const storedPositions = new Map<string, string>()
const localStorageMock = {
  getItem: (key: string) => storedPositions.get(key) ?? null,
  setItem: (key: string, value: string) => { storedPositions.set(key, value) },
  removeItem: (key: string) => { storedPositions.delete(key) },
  clear: () => { storedPositions.clear() },
  key: (index: number) => [...storedPositions.keys()][index] ?? null,
  get length() { return storedPositions.size },
}

const ORDERS: CatalogTable = {
  id: 'orders', registrationId: 'registration-orders', name: 'orders', uri: 'mem://orders',
  columns: [{ name: 'customer_id', type: 'int', capabilities: [] }],
  keys: [{ columns: ['customer_id'], confidence: 'inferred' }],
}
const CUSTOMERS: CatalogTable = {
  id: 'customers', registrationId: 'registration-customers', name: 'customers', uri: 'mem://customers',
  columns: [{ name: 'id', type: 'int', capabilities: ['key'] }],
}
const PAGE = { items: [ORDERS, CUSTOMERS], total: 2, hasMore: false }

describe('ERDiagram request truth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorageMock.clear()
    vi.stubGlobal('localStorage', localStorageMock)
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
    mocks.workspaceResource.mockResolvedValue({
      resource: { id: 'dataset:resolved', kind: 'dataset', name: 'resolved' },
      ancestors: [],
      source: { id: 'local', kind: 'local', completeness: 'complete' },
    })
    mocks.workspaceCanonicalDataset.mockResolvedValue({ sourceUri: ORDERS.uri, columns: ORDERS.columns })
    mocks.workspaceLineageResource.mockResolvedValue({ id: 'dataset:resolved', kind: 'dataset', name: 'resolved', source: 'provider', detached: false })
    mocks.fitView.mockReset()
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

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

  it('keeps overview fitting compact while preserving the same safe insets', async () => {
    render(<ERDiagram />)

    await screen.findByText('orders')
    fireEvent.click(screen.getByRole('button', { name: 'Fit view' }))

    expect(mocks.fitView).toHaveBeenCalledWith({
      padding: { top: '164px', right: '16px', bottom: '16px', left: '344px' }, maxZoom: 0.8,
    })
  })

  it('allows a focused relationship graph to fit at detail zoom', async () => {
    store.erFocusUri = ORDERS.uri
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
    // The graph renders the canonical root returned by the service, while refreshes continue to
    // query from the stable route focus so a physical generation is never fed back as a new root.
    await waitFor(() => expect(mocks.lineage).toHaveBeenLastCalledWith(
      ORDERS.uri, 1, 60))
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

  it('keeps a connected-source focus human-readable when switching to ER', async () => {
    const providerDatasetId = 'dataset:external.raw-video'
    const providerUri = `workspace-provider:${'a'.repeat(64)}`
    const canonicalLineageUri = `workspace-provider-lineage:${'b'.repeat(64)}`
    store.erFocusUri = providerUri
    store.erFocusDatasetId = providerDatasetId
    store.erMode = 'lineage'
    // Real provider navigation already has a runtime URI. Optional Workspace enrichment may fail,
    // and lineage can still canonicalize that URI to a different graph root.
    mocks.workspaceResource.mockRejectedValue(new Error('provider details timed out'))
    mocks.lineage.mockResolvedValue({
      rootUri: canonicalLineageUri,
      nodes: [{ id: 'provider-root', name: 'raw_video_v2', uri: canonicalLineageUri, kind: 'table' }],
      edges: [],
    })
    mocks.tablesPage.mockResolvedValue({ items: [], total: 0, hasMore: false })
    render(<ERDiagram />)

    expect(await within(screen.getByTestId('er-focus-bar')).findByText('raw_video_v2')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('er-mode-joins'))

    expect(await within(screen.getByTestId('er-focus-bar')).findByText('Focused: raw_video_v2')).toBeInTheDocument()
    expect(screen.getByTestId(`node-lineage:${canonicalLineageUri}`)).toHaveTextContent('raw_video_v2')
    expect(screen.queryByText(providerUri)).toBeNull()
  })

  it('opens a clicked provider-lineage neighbour as a Workspace dataset', async () => {
    const providerRoot = 'workspace-provider://opaque-root'
    const providerChild = `workspace-provider-lineage://${'a'.repeat(64)}`
    store.erFocusUri = providerRoot
    store.erMode = 'lineage'
    mocks.lineage.mockResolvedValue({
      rootUri: providerRoot,
      nodes: [
        { id: 'root', name: 'raw_video_v2', uri: providerRoot, kind: 'table' },
        { id: 'child', name: 'raw_video_v4', uri: providerChild, kind: 'table' },
      ],
      edges: [{ parent: providerRoot, child: providerChild, factCount: 1 }],
    })
    mocks.tablesPage.mockResolvedValue({ items: [], total: 0, hasMore: false })
    render(<ERDiagram />)

    fireEvent.click(await screen.findByTestId(`node-lineage:${providerChild}`))

    await waitFor(() => expect(mocks.workspaceLineageResource).toHaveBeenCalledWith({
      rootUri: providerRoot,
      nodeUri: providerChild,
      name: 'raw_video_v4',
    }))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith('dataset:resolved')
  })

  it('explains when a lineage node has no registered dataset details', async () => {
    const providerRoot = 'workspace-provider://opaque-root'
    const providerChild = `workspace-provider-lineage://${'b'.repeat(64)}`
    store.erFocusUri = providerRoot
    store.erMode = 'lineage'
    mocks.lineage.mockResolvedValue({
      rootUri: providerRoot,
      nodes: [
        { id: 'root', name: 'raw_video_v2', uri: providerRoot, kind: 'table' },
        { id: 'child', name: 'scratch_output', uri: providerChild, kind: 'table' },
      ],
      edges: [{ parent: providerRoot, child: providerChild, factCount: 1 }],
    })
    mocks.tablesPage.mockResolvedValue({ items: [], total: 0, hasMore: false })
    mocks.workspaceLineageResource.mockRejectedValueOnce(
      new Error('HTTP 404: lineage dataset is not registered in this connected source'),
    )
    render(<ERDiagram />)

    fireEvent.click(await screen.findByTestId(`node-lineage:${providerChild}`))

    await waitFor(() => expect(store.pushToast).toHaveBeenCalledWith(
      'No dataset details are available for scratch_output.', 'error',
    ))
  })

  it('opens a clicked registered-lineage neighbour in its normal Dataset detail page', async () => {
    store.erFocusUri = ORDERS.uri
    store.erMode = 'lineage'
    mocks.lineage.mockResolvedValue({
      rootUri: ORDERS.uri,
      nodes: [
        { id: ORDERS.id, name: ORDERS.name, uri: ORDERS.uri, kind: 'table' },
        { id: CUSTOMERS.id, name: CUSTOMERS.name, uri: CUSTOMERS.uri, kind: 'table' },
      ],
      edges: [{ parent: ORDERS.uri, child: CUSTOMERS.uri, factCount: 1 }],
    })
    render(<ERDiagram />)

    fireEvent.click(await screen.findByTestId('node-customers'))

    expect(store.setWorkspaceResource).toHaveBeenCalledWith('dataset:registration-customers')
    expect(mocks.workspaceLineageResource).not.toHaveBeenCalled()
  })

  it('reveals relationship and lineage column endpoints at detail zoom', async () => {
    mocks.relationships.mockResolvedValue([{
      leftUri: ORDERS.uri, leftColumns: ['customer_id'],
      rightUri: CUSTOMERS.uri, rightColumns: ['id'],
      cardinality: 'N:1', confidence: 'declared',
    }])
    const { unmount } = render(<ERDiagram />)

    const compactJoin = await screen.findByTestId('edge-shape-d0')
    expect(compactJoin).toHaveTextContent('N:1')
    expect(compactJoin).toHaveAttribute('data-stroke', 'hsl(var(--primary))')
    expect(compactJoin).toHaveAttribute('data-source-handle', 'node-source')
    fireEvent.click(screen.getByRole('button', { name: 'zoom graph' }))
    await waitFor(() => expect(screen.getByTestId('edge-shape-d0')).toHaveAttribute(
      'data-source-handle', 'column-out:customer_id',
    ))
    expect(screen.getByTestId('edge-shape-d0')).toHaveAttribute('data-target-handle', 'column-in:id')

    unmount()
    cleanup()
    vi.clearAllMocks()
    store.erFocusUri = ORDERS.uri
    store.erMode = 'lineage'
    mocks.tablesPage.mockResolvedValue(PAGE)
    mocks.relationships.mockResolvedValue([])
    const archive = {
      id: 'customers-archive', name: 'customers_archive', uri: 'mem://customers-archive', kind: 'table',
    }
    mocks.lineage.mockResolvedValue({
      rootUri: ORDERS.uri,
      nodes: [
        { id: ORDERS.id, name: ORDERS.name, uri: ORDERS.uri, kind: 'table' },
        { id: CUSTOMERS.id, name: CUSTOMERS.name, uri: CUSTOMERS.uri, kind: 'table' },
        archive,
      ],
      edges: [
        {
          parent: ORDERS.uri,
          child: CUSTOMERS.uri,
          factCount: 1,
          columns: ['id'],
          pipelineNames: ['publish_customers'],
          cardinality: { value: '1:N', evidence: 'measured' },
        },
        {
          parent: ORDERS.uri,
          child: archive.uri,
          factCount: 1,
          columns: ['id'],
          pipelineNames: ['publish_customers_with_a_generated_identifier'],
        },
      ],
    })
    render(<ERDiagram />)

    expect(await screen.findByTestId('edge-shape-l0')).not.toHaveTextContent('publish_customers')
    expect(screen.getByTestId('edge-shape-l0')).toHaveAttribute('data-stroke', 'hsl(var(--muted-foreground))')
    fireEvent.click(screen.getByRole('button', { name: 'zoom graph' }))
    await waitFor(() => expect(screen.getByTestId('edge-shape-l0')).toHaveAttribute(
      'data-target-handle', 'column-in:id',
    ))
    expect(screen.getByTestId('edge-shape-l0')).toHaveTextContent('1:N')
    expect(screen.getByTestId('edge-shape-l0')).not.toHaveTextContent('publish_customers')
    expect(screen.getByTestId('edge-shape-l1')).toBeEmptyDOMElement()
  })

  it('reserves enough space for the longest semantic field role', () => {
    render(<EntityNode data={{
      table: CUSTOMERS,
      fields: [{ name: 'id', role: 'mapped', column: CUSTOMERS.columns[0], type: 'int' }],
      focused: false,
      lineage: true,
      expanded: true,
      opening: false,
      onFocus: vi.fn(),
      onOpen: vi.fn(),
    }} />)

    const badge = screen.getByTestId('er-field-role:customers:id')
    expect(badge).toHaveTextContent('mapped')
    expect(badge).toHaveClass('w-12')
    expect(screen.getByRole('button', { name: 'Open dataset customers' })).toBeInTheDocument()
  })

  it('shows a key-capable column as a join key without claiming it is a primary key', async () => {
    store.erFocusUri = CUSTOMERS.uri
    store.erMode = 'lineage'
    mocks.tablesPage.mockResolvedValue({ items: [CUSTOMERS], total: 1, hasMore: false })
    mocks.lineage.mockResolvedValue({
      rootUri: CUSTOMERS.uri,
      nodes: [{ id: CUSTOMERS.id, name: CUSTOMERS.name, uri: CUSTOMERS.uri, kind: 'table' }],
      edges: [],
    })

    render(<ERDiagram />)

    expect(await screen.findByTestId('node-customers')).toHaveAttribute('data-fields', 'KEY:id')
  })

  it('shows an inferred catalog key without presenting it as a declared PK', async () => {
    store.erFocusUri = ORDERS.uri
    store.erMode = 'lineage'
    mocks.tablesPage.mockResolvedValue({ items: [ORDERS], total: 1, hasMore: false })
    mocks.lineage.mockResolvedValue({
      rootUri: ORDERS.uri,
      nodes: [{ id: ORDERS.id, name: ORDERS.name, uri: ORDERS.uri, kind: 'table' }],
      edges: [],
    })

    render(<ERDiagram />)

    expect(await screen.findByTestId('node-orders')).toHaveAttribute('data-fields', 'KEY:customer_id')
  })

  it('omits unrelated schema columns and keeps long key lists locally scrollable', () => {
    const fields = Array.from({ length: 8 }, (_, index) => ({
      name: `key_${index}`, role: 'KEY' as const, type: 'string',
    }))
    render(<EntityNode data={{
      table: { ...CUSTOMERS, columns: [
        ...fields.map((field) => ({ name: field.name, type: field.type, capabilities: ['key'] })),
        { name: 'description', type: 'string', capabilities: [] },
      ] },
      fields,
      focused: true,
      lineage: true,
      expanded: true,
      opening: false,
      onFocus: vi.fn(),
      onOpen: vi.fn(),
    }} />)

    const fieldList = screen.getByTestId('er-entity-fields')
    expect(fieldList).toHaveClass('nowheel', 'nodrag', 'overflow-y-auto')
    expect(fieldList).not.toHaveTextContent('description')
    expect(screen.getByRole('button', { name: 'Back to dataset customers' })).toHaveClass('entity-drag-handle')
  })

  it('lets a lineage entity keep a manually dragged position independently of its auto layout', async () => {
    store.erFocusUri = ORDERS.uri
    store.erMode = 'lineage'
    mocks.tablesPage.mockResolvedValue({ items: [ORDERS], total: 1, hasMore: false })
    mocks.lineage.mockResolvedValue({
      rootUri: ORDERS.uri,
      nodes: [{ id: ORDERS.id, name: ORDERS.name, uri: ORDERS.uri, kind: 'table' }],
      edges: [],
    })
    const { unmount } = render(<ERDiagram />)

    expect(await screen.findByTestId('flow')).toHaveAttribute('data-nodes-draggable', 'true')
    fireEvent.click(screen.getByRole('button', { name: 'move first node' }))
    await waitFor(() => expect(screen.getByTestId('node-orders')).toHaveAttribute('data-x', '777'))
    expect(screen.getByTestId('node-orders')).toHaveAttribute('data-y', '888')

    unmount()
    render(<ERDiagram />)
    expect(await screen.findByTestId('node-orders')).toHaveAttribute('data-x', '777')
    expect(screen.getByTestId('node-orders')).toHaveAttribute('data-y', '888')
  })

  it('opens a high-fan-out lineage as a readable subset and expands it on demand', async () => {
    const children = Array.from({ length: 20 }, (_, index) => ({
      id: `child-${index + 1}`,
      name: `child_${String(index + 1).padStart(2, '0')}`,
      uri: `mem://child-${index + 1}`,
      kind: 'table',
    }))
    store.erFocusUri = ORDERS.uri
    store.erMode = 'lineage'
    mocks.lineage.mockResolvedValue({
      rootUri: ORDERS.uri,
      nodes: [
        { id: ORDERS.id, name: ORDERS.name, uri: ORDERS.uri, kind: 'table' },
        ...children,
      ],
      edges: children.map((child) => ({
        parent: ORDERS.uri, child: child.uri, factCount: 1,
      })),
      truncated: false,
    })
    render(<ERDiagram />)

    await screen.findByTestId('er-lineage-show-more')
    expect(screen.getByTestId('er-connection-count')).toHaveTextContent('8 of 20 connections')
    expect(screen.getAllByTestId(/^node-/)).toHaveLength(9)
    const childNodes = screen.getAllByTestId(/^node-lineage:mem:\/\/child-/)
    expect(new Set(childNodes.map((node) => node.getAttribute('data-x')))).toEqual(
      new Set(['340', '640']),
    )
    const rowsAt = (x: number) => childNodes
      .filter((node) => Number(node.getAttribute('data-x')) === x)
      .map((node) => Number(node.getAttribute('data-y')))
      .sort((left, right) => left - right)
    expect(rowsAt(340)).toEqual([-330, -110, 110, 330])
    expect(rowsAt(640)).toEqual([-440, -220, 220, 440])

    fireEvent.click(screen.getByTestId('er-lineage-show-more'))

    await waitFor(() => expect(screen.getByTestId('er-connection-count')).toHaveTextContent('16 of 20 connections'))
    fireEvent.click(screen.getByTestId('er-lineage-show-more'))
    await waitFor(() => expect(screen.getByTestId('er-connection-count')).toHaveTextContent('20 connections'))
    expect(screen.getAllByTestId(/^node-/)).toHaveLength(21)
    expect(screen.getByTestId('er-lineage-show-fewer')).toBeVisible()
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
    expect(screen.getByTestId('er-mode-lineage')).toBeVisible()
    expect(mocks.tableByRegistration).toHaveBeenCalledWith(ORDERS.registrationId)
    await waitFor(() => expect(mocks.lineage).toHaveBeenCalledWith(ORDERS.uri, 1, 60))

    fireEvent.click(screen.getByTestId('node-orders'))
    expect(store.returnFromRelationships).toHaveBeenCalledOnce()

    fireEvent.click(screen.getByRole('button', { name: 'Back to dataset' }))
    expect(store.returnFromRelationships).toHaveBeenCalledTimes(2)
  })
})
