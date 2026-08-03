import { render, screen, fireEvent, cleanup, waitFor, act, within } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ReactFlowProvider } from '@xyflow/react'
import { TooltipProvider } from '@/components/ui/tooltip'

// importing the store triggers autosave side-effects → stub the api client
const mocks = vi.hoisted(() => ({
  tablesPage: vi.fn(), destinations: vi.fn(), browseDestination: vi.fn(),
  registerFile: vi.fn(), mkdirDestination: vi.fn(), datasetRevisions: vi.fn(), datasetRevision: vi.fn(),
  datasetRevisionCapabilities: vi.fn(), resolveDatasetRevision: vi.fn(), workspaceSearch: vi.fn(), workspaceProviderSource: vi.fn(),
}))
vi.mock('../../api/client', () => ({ api: mocks }))

import { requestSourceEntryAction } from './source' // also registers the Source card via register()
import { getComponent } from '../registry'
import { useStore } from '../../store/graph'

const Source = getComponent('source')!
const render1 = (data: object) =>
  render(<TooltipProvider><ReactFlowProvider><Source id="s1" data={data as never} /></ReactFlowProvider></TooltipProvider>)

describe('Source card — honest counts + empty/offline (UX-14)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.tablesPage.mockResolvedValue({ items: [], total: 0, hasMore: false })
    mocks.destinations.mockResolvedValue({ destinations: [{ id: 'local', name: 'Workspace', backend: 'local', root: '/data' }], backends: ['local'] })
    mocks.browseDestination.mockResolvedValue({ path: '', entries: [{ name: 'new.csv', kind: 'file', uri: 'file:///data/new.csv' }], writable: true })
    mocks.registerFile.mockReset()
    mocks.mkdirDestination.mockResolvedValue({ ok: true })
    mocks.datasetRevisions.mockResolvedValue({ items: [], nextCursor: null, hasMore: false })
    mocks.datasetRevisionCapabilities.mockResolvedValue({
      selectors: ['exact', 'latest'], asOfOrdering: null, timezone: null,
    })
    mocks.datasetRevision.mockResolvedValue({
      datasetId: 'dataset-1', revisionId: '1', retentionOwner: 'provider', summary: { rowCount: 1 },
      preview: { columns: [], rows: [], hasMore: false, rowLimit: 100 },
    })
    mocks.workspaceSearch.mockReset().mockResolvedValue({ query: 'remote', groups: [], nextCursor: null, hasMore: false })
    mocks.workspaceProviderSource.mockReset()
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({
      kernelUp: true,
      canvasRole: 'owner',
      doc: { id: 'c', name: 'test', version: 1, nodes: [], edges: [] },
      catalog: [{ id: 't1', name: 'orders', uri: 'mem://orders', rowCount: null, version: 'v1', columns: [{ name: 'a', type: 'int', capabilities: [] }] }],
      past: [], future: [], selectedIds: [],
    } as any)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllEnvs()
  })

  it('labels an unknown row count without inventing a fake zero', () => {
    render1({ title: 'source', status: 'draft', config: { tableId: 't1' } })
    expect(screen.getByText(/Rows unknown/i)).toBeInTheDocument()
    expect(screen.queryByText(/\b0\s*rows/)).toBeNull()
  })

  it('still shows "0 rows" for a genuinely empty table', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ kernelUp: true, catalog: [
      { id: 't1', name: 'orders', uri: 'mem://orders', rowCount: 0, version: 'v1', columns: [{ name: 'a', type: 'int', capabilities: [] }] },
    ] } as any)
    render1({ title: 'source', status: 'draft', config: { tableId: 't1' } })
    expect(screen.getByText(/\b0\s*rows/)).toBeInTheDocument()
  })

  it('selects the Source when its dataset control receives the click', () => {
    render1({ title: 'source', status: 'draft', config: { tableId: 't1' } })

    const selector = screen.getByRole('button', { name: 'Change dataset' })
    expect(selector).toHaveAttribute('title', expect.stringContaining('Click to change dataset'))
    fireEvent.click(selector)

    expect(useStore.getState().selectedIds).toEqual(['s1'])
  })

  it('routes Inspector entry actions to the matching Source and focuses its picker', async () => {
    render1({ title: 'source', status: 'draft', config: {} })

    requestSourceEntryAction('another-source', 'select')
    expect(screen.queryByTestId('source-search')).not.toBeInTheDocument()

    requestSourceEntryAction('s1', 'select')
    const search = await screen.findByTestId('source-search')
    await waitFor(() => expect(search).toHaveFocus())
  })

  it('opens an entry action requested while a new Source is still mounting', async () => {
    requestSourceEntryAction('s1', 'select')
    render1({ title: 'source', status: 'draft', config: {} })

    const search = await screen.findByTestId('source-search')
    await waitFor(() => expect(search).toHaveFocus())
  })

  it('does not route an Inspector entry action to a different Source', async () => {
    const sourceData = (title: string) => ({ title, status: 'draft', config: {} })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ doc: {
      id: 'c', name: 'test', version: 1, edges: [],
      nodes: [
        { id: 's1', type: 'source', position: { x: 0, y: 0 }, data: sourceData('first') },
        { id: 's2', type: 'source', position: { x: 200, y: 0 }, data: sourceData('second') },
      ],
    } } as any)
    render(<TooltipProvider><ReactFlowProvider>
      <Source id="s1" data={sourceData('first') as never} />
      <Source id="s2" data={sourceData('second') as never} />
    </ReactFlowProvider></TooltipProvider>)

    requestSourceEntryAction('s2', 'select')
    fireEvent.click(await screen.findByText('orders'))

    const [first, second] = useStore.getState().doc.nodes
    expect(first.data.config.tableId).toBeUndefined()
    expect(second.data.config.tableId).toBe('t1')
  })

  it('registers a pasted path or URL from the direct Source entry action', async () => {
    const data = { title: 'source', status: 'draft', config: {} }
    mocks.registerFile.mockResolvedValue({
      id: 'registered', name: 'events', uri: 's3://datasets/events.parquet', rowCount: 1,
      columns: [{ name: 'event', type: 'string', capabilities: [] }],
    })
    useStore.setState({ doc: {
      id: 'c', name: 'test', version: 1, edges: [],
      nodes: [{ id: 's1', type: 'source', position: { x: 0, y: 0 }, data }],
    } } as any)
    render1(data)

    requestSourceEntryAction('s1', 'browse')
    const dialog = await screen.findByRole('dialog', { name: 'Register path or URL' })
    expect(dialog.parentElement?.parentElement).toBe(document.body)
    fireEvent.change(within(dialog).getByLabelText('Dataset path or URL'), {
      target: { value: 's3://datasets/events.parquet' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Register' }))

    await waitFor(() => expect(mocks.registerFile).toHaveBeenCalledWith('s3://datasets/events.parquet'))
    expect(useStore.getState().doc.nodes[0].data.config).toEqual({
      uri: 's3://datasets/events.parquet', tableId: 'registered',
    })
  })

  it('closes the Source registration dialog with Escape', async () => {
    const data = { title: 'source', status: 'draft', config: {} }
    render1(data)

    requestSourceEntryAction('s1', 'browse')
    expect(await screen.findByRole('dialog', { name: 'Register path or URL' })).toBeVisible()
    fireEvent.keyDown(window, { key: 'Escape' })

    expect(screen.queryByRole('dialog', { name: 'Register path or URL' })).not.toBeInTheDocument()
  })

  it('ignores Inspector entry actions when the Source is read-only', () => {
    const fileClick = vi.spyOn(HTMLInputElement.prototype, 'click')
    render1({ title: 'source', status: 'draft', config: {} })
    // Exercise the listener replacement as permission changes, not only its initial closure.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    act(() => useStore.setState({ canvasRole: 'viewer' } as any))

    requestSourceEntryAction('s1', 'select')
    requestSourceEntryAction('s1', 'upload')
    requestSourceEntryAction('s1', 'browse')

    expect(screen.queryByTestId('source-search')).not.toBeInTheDocument()
    expect(screen.queryByText('Open a dataset')).not.toBeInTheDocument()
    expect(fileClick).not.toHaveBeenCalled()
  })

  it('removes its Inspector entry listener when the Source unmounts', () => {
    const remove = vi.spyOn(window, 'removeEventListener')
    const view = render1({ title: 'source', status: 'draft', config: {} })
    view.unmount()

    expect(remove).toHaveBeenCalledWith(
      'dataplay:source-entry:s1',
      expect.any(Function),
    )
  })

  it('keeps a selected provider exact summary on the card without field evidence or navigation clutter', async () => {
    mocks.datasetRevision.mockResolvedValueOnce({
      datasetId: 'provider-orders', revisionId: 'empty-r7', retentionOwner: 'provider', summary: { rowCount: 0 },
      preview: {
        columns: [{ name: 'customer_id', type: 'int64', physicalType: 'INT64', nullable: false, hasDefault: null,
          fieldId: 'provider.customer_id', provenance: 'provider', capabilities: [],
          annotations: [{ key: 'provider.note', value: 'selected exact schema', encoding: 'utf8', provenance: 'provider' }] }],
        rows: [], hasMore: false, rowLimit: 100,
      },
    })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ catalog: [], doc: { id: 'c', name: 'test', version: 1, nodes: [], edges: [] } } as any)
    render1({ title: 'provider orders', status: 'latest', config: {
      providerResourceRef: 'dataset:provider-placement-orders', providerName: 'fixture', providerReadMode: 'exact',
      datasetRef: { kind: 'exact', datasetId: 'provider-orders', revisionId: 'empty-r7' },
    } })

    expect(await screen.findByText('fixture · Saved version · 0 rows · 1 column')).toBeInTheDocument()
    expect(screen.queryByText(/Field evidence/i)).not.toBeInTheDocument()
    expect(mocks.datasetRevision).toHaveBeenCalledTimes(1)
    expect(mocks.datasetRevision).toHaveBeenCalledWith('provider-orders', 'empty-r7')
    expect(screen.queryByRole('link', { name: 'Open dataset' })).not.toBeInTheDocument()
  })

  it('does not misroute an incomplete provider binding into the local catalog', async () => {
    mocks.datasetRevision.mockResolvedValueOnce({
      datasetId: 'provider-orders', revisionId: 'empty-r7', retentionOwner: 'provider', summary: { rowCount: 0 },
      preview: { columns: [], rows: [], hasMore: false, rowLimit: 100 },
    })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ catalog: [], doc: { id: 'c', name: 'test', version: 1, nodes: [], edges: [] } } as any)
    render1({ title: 'provider orders', status: 'latest', config: {
      uri: 'workspace-provider://missing-placement', providerName: 'fixture', providerReadMode: 'exact',
      datasetRef: { kind: 'exact', datasetId: 'provider-orders', revisionId: 'empty-r7' },
    } })

    expect(await screen.findByText('fixture · Saved version · 0 rows · 0 columns')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Open dataset' })).not.toBeInTheDocument()
  })

  it('keeps a long exact identity out of the Source card summary', async () => {
    const revisionId = 'revision:an-intentionally-long-opaque-identity'
    mocks.datasetRevision.mockResolvedValueOnce({
      datasetId: 'provider-orders', revisionId, retentionOwner: 'provider', summary: { rowCount: 12 },
      preview: {
        columns: [{ name: 'customer_id', type: 'int64', capabilities: [] }],
        rows: [], hasMore: false, rowLimit: 100,
      },
    })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ catalog: [], doc: { id: 'c', name: 'test', version: 1, nodes: [], edges: [] } } as any)
    render1({ title: 'provider orders', status: 'latest', config: {
      providerResourceRef: 'dataset:provider-orders', providerName: 'fixture', providerReadMode: 'exact',
      datasetRef: { kind: 'exact', datasetId: 'provider-orders', revisionId },
    } })

    expect(await screen.findByText('fixture · Saved version · 12 rows · 1 column')).toBeInTheDocument()
    expect(screen.queryByText(/intentionally-long-opaque/i)).not.toBeInTheDocument()
  })

  it('cold start: kernel up + no recents fetches a server page, then says the catalog is empty (not "offline")', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ kernelUp: true, catalog: [] } as any)
    render1({ title: 'source', status: 'draft', config: {} })
    fireEvent.click(screen.getByText(/select dataset/i))
    // the stubbed api resolves the top-usage page to an empty list → the honest "empty catalog" copy
    expect(await screen.findByText(/Catalog is empty/i)).toBeInTheDocument()
    expect(screen.queryByText(/offline/i)).toBeNull()
  })

  it('prefers the friendly offline state over a redundant raw request error', async () => {
    mocks.tablesPage.mockRejectedValueOnce(new Error('Failed to fetch'))
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ kernelUp: false, catalog: [] } as any)
    render1({ title: 'source', status: 'draft', config: {} })
    fireEvent.click(screen.getByText(/select dataset/i))
    expect(await screen.findByText(/Kernel offline/i)).toBeInTheDocument()
    await waitFor(() => expect(mocks.tablesPage).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText(/Failed to fetch/i)).toBeNull()
  })

  it('surfaces a catalog search failure and retries instead of reporting no matches', async () => {
    mocks.tablesPage
      .mockRejectedValueOnce(new Error('HTTP 502: catalog unavailable'))
      .mockResolvedValueOnce({ items: [], total: 0, hasMore: false })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ kernelUp: true, catalog: [] } as any)
    render1({ title: 'source', status: 'draft', config: {} })
    fireEvent.click(screen.getByText(/select dataset/i))

    expect(await screen.findByText(/Couldn't load catalog: HTTP 502/i)).toBeInTheDocument()
    expect(screen.queryByText('No matches')).toBeNull()
    fireEvent.click(screen.getByTestId('source-search-retry'))
    expect(await screen.findByText(/Catalog is empty/i)).toBeInTheDocument()
    expect(mocks.tablesPage).toHaveBeenCalledTimes(2)
  })

  it('replaces a Source with the canonical exact binding selected from Workspace providers', async () => {
    const oldConfig = {
      uri: 'workspace-provider://old-binding', providerResourceRef: 'dataset:old-placement',
      providerMountId: 'mount-a', providerSourceBindingId: 'a'.repeat(32),
      providerName: 'fixture', providerReadMode: 'exact' as const,
      datasetRef: { kind: 'exact' as const, datasetId: 'workspace-provider:old', revisionId: 'old-revision' },
      tableId: 'stale-local-table', registrationId: 'stale-registration',
      providerLegacyOption: 'must not survive replacement',
    }
    mocks.workspaceSearch.mockResolvedValue({
      query: 'remote', nextCursor: null, hasMore: false,
      groups: [{ source: { id: 'mount-b', kind: 'provider', completeness: 'complete', provider: 'fixture-b' }, items: [{
        id: 'dataset:provider-placement-b', kind: 'dataset', name: 'remote orders', detached: false,
        source: 'provider', mountId: 'mount-b', provider: 'fixture-b', providerDatasetId: 'orders',
        referenceState: 'current', canonicalReferenceState: 'current', lastKnown: false,
      }] }],
    })
    const canonicalConfig = {
      uri: 'workspace-provider://canonical-binding', providerResourceRef: 'dataset:provider-placement-b',
      providerMountId: 'mount-b', providerSourceBindingId: 'b'.repeat(32), providerName: 'fixture-b',
      providerReadMode: 'exact' as const,
      datasetRef: { kind: 'exact' as const, datasetId: 'workspace-provider:canonical', revisionId: 'provider-r2' },
    }
    mocks.workspaceProviderSource.mockResolvedValue({ name: 'remote orders', config: canonicalConfig })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ doc: { id: 'c', name: 'test', version: 1, edges: [], nodes: [{
      id: 's1', type: 'source', position: { x: 0, y: 0 }, data: { title: 'old orders', status: 'latest', config: oldConfig },
    }] } } as any)
    render1({ title: 'old orders', status: 'latest', config: oldConfig })

    fireEvent.click(screen.getByRole('button', { name: 'Change dataset' }))
    fireEvent.click(screen.getByRole('button', { name: 'Browse Workspace catalog…' }))
    const search = await screen.findByTestId('workspace-source-search')
    expect(search).toHaveFocus()
    fireEvent.change(search, { target: { value: 'remote' } })
    const result = await screen.findByRole('button', { name: /remote orders/i })
    fireEvent.click(result)

    await waitFor(() => expect(mocks.workspaceProviderSource).toHaveBeenCalledWith('dataset:provider-placement-b'))
    const config = useStore.getState().doc.nodes[0].data.config
    expect(config).toEqual(canonicalConfig)
    expect(config).not.toHaveProperty('tableId')
    expect(config).not.toHaveProperty('registrationId')
    expect(config).not.toHaveProperty('providerLegacyOption')
    expect(config.uri).toBe('workspace-provider://canonical-binding')
    expect(config.providerSourceBindingId).toBe('b'.repeat(32))
    expect(config.datasetRef).toEqual({ kind: 'exact', datasetId: 'workspace-provider:canonical', revisionId: 'provider-r2' })
    expect(useStore.getState().doc.nodes[0].data.title).toBe('remote orders')
    expect(useStore.getState().doc.nodes[0].data.status).toBe('stale')
    expect(useStore.getState().past).toHaveLength(1)

    act(() => useStore.getState().undo())
    expect(useStore.getState().doc.nodes[0].data).toMatchObject({
      title: 'old orders', config: oldConfig,
    })
    act(() => useStore.getState().redo())
    expect(useStore.getState().doc.nodes[0].data).toMatchObject({
      title: 'remote orders', config: canonicalConfig,
    })
  })

  it('keeps the original Source when a provider dataset cannot supply an exact replacement', async () => {
    const oldConfig = {
      uri: 'workspace-provider://old-binding',
      providerResourceRef: 'dataset:old-placement',
      providerMountId: 'mount-a',
      providerSourceBindingId: 'a'.repeat(32),
      providerName: 'fixture',
      providerReadMode: 'exact' as const,
      datasetRef: {
        kind: 'exact' as const,
        datasetId: 'workspace-provider:old',
        revisionId: 'old-revision',
      },
    }
    mocks.workspaceSearch.mockResolvedValue({
      query: 'mutable', nextCursor: null, hasMore: false,
      groups: [{
        source: {
          id: 'mount-b', kind: 'provider', completeness: 'complete', provider: 'fixture-b',
        },
        items: [{
          id: 'dataset:mutable-placement', kind: 'dataset', name: 'mutable observations',
          detached: false, source: 'provider', mountId: 'mount-b', provider: 'fixture-b',
          providerDatasetId: 'observations', referenceState: 'current',
          canonicalReferenceState: 'current', lastKnown: false,
        }],
      }],
    })
    mocks.workspaceProviderSource.mockRejectedValue(new Error(
      'This dataset cannot be pinned to a version, so it cannot replace a runnable Source.',
    ))
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ doc: { id: 'c', name: 'test', version: 1, edges: [], nodes: [{
      id: 's1', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'old observations', status: 'latest', config: oldConfig },
    }] } } as any)
    render1({ title: 'old observations', status: 'latest', config: oldConfig })

    fireEvent.click(screen.getByRole('button', { name: 'Change dataset' }))
    fireEvent.click(screen.getByRole('button', { name: 'Browse Workspace catalog…' }))
    fireEvent.change(await screen.findByTestId('workspace-source-search'), {
      target: { value: 'mutable' },
    })
    fireEvent.click(await screen.findByRole('button', { name: /mutable observations/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This dataset cannot be pinned to a version, so it cannot replace a runnable Source.',
    )
    expect(useStore.getState().doc.nodes[0].data).toMatchObject({
      title: 'old observations', config: oldConfig,
    })
    expect(useStore.getState().past).toHaveLength(0)
  })

  it('shows an unavailable provider without also claiming that the search had no matches', async () => {
    mocks.workspaceSearch.mockResolvedValue({
      query: 'remote', nextCursor: null, hasMore: false,
      groups: [{
        source: {
          id: 'mount-offline', kind: 'provider', completeness: 'unavailable',
          provider: 'fixture', error: 'catalog offline',
        },
        items: [],
      }],
    })
    render1({ title: 'source', status: 'draft', config: {} })

    fireEvent.click(screen.getByRole('button', { name: 'Select dataset' }))
    fireEvent.click(screen.getByRole('button', { name: 'Browse Workspace catalog…' }))
    expect(screen.getByText('Search datasets from connected catalogs.')).toBeInTheDocument()
    expect(screen.queryByText(/canonical binding and exact revision admission/i)).toBeNull()
    fireEvent.change(await screen.findByTestId('workspace-source-search'), {
      target: { value: 'remote' },
    })

    expect(await screen.findByText('fixture unavailable: catalog offline')).toBeInTheDocument()
    expect(screen.queryByText(/No mounted provider datasets match/i)).toBeNull()
  })

  it('does not change the source until a browsed file has been registered successfully', async () => {
    const oldConfig = {
      uri: 'workspace-provider://binding', tableId: 't1',
      providerResourceRef: 'dataset:external.binding', providerMountId: 'mount-a',
      providerSourceBindingId: 'a'.repeat(32),
      providerName: 'fixture', providerReadMode: 'exact' as const,
      registrationId: 'old-registration',
      datasetRef: {
        kind: 'exact' as const,
        datasetId: 'workspace-provider:old',
        revisionId: 'old-revision',
      },
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({
      doc: { id: 'c', name: 'test', version: 1, nodes: [{ id: 's1', type: 'source', position: { x: 0, y: 0 }, data: { title: 'orders source', status: 'draft', config: oldConfig } }], edges: [] },
    } as any)
    mocks.registerFile
      .mockRejectedValueOnce(new Error('HTTP 422: unsupported dataset'))
      .mockResolvedValueOnce({ id: 't2', name: 'new', uri: 'file:///data/new.csv', rowCount: 1, columns: [{ name: 'x', type: 'int', capabilities: [] }] })
    render1({ title: 'orders source', status: 'draft', config: oldConfig })
    fireEvent.click(screen.getByText('orders'))
    fireEvent.click(screen.getByText(/Register accessible path/i))
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Register path or URL' })).getByRole('button', { name: 'Browse storage' }))
    fireEvent.click(await screen.findByText('new.csv'))

    expect(await screen.findByText(/Couldn't open file: HTTP 422/i)).toBeInTheDocument()
    expect(useStore.getState().doc.nodes[0].data.config).toEqual(oldConfig)
    expect(useStore.getState().doc.nodes[0].data.title).toBe('orders source')

    fireEvent.click(screen.getByText('new.csv'))
    await waitFor(() => expect(useStore.getState().doc.nodes[0].data.config).toEqual({
      uri: 'file:///data/new.csv', tableId: 't2',
    }))
    const config = useStore.getState().doc.nodes[0].data.config
    for (const field of [
      'registrationId', 'datasetRef', 'providerResourceRef', 'providerMountId',
      'providerSourceBindingId', 'providerName', 'providerReadMode',
    ]) expect(config).not.toHaveProperty(field)
    expect(useStore.getState().doc.nodes[0].data.title).toBe('new')
    expect(screen.queryByText(/Couldn't open file/i)).toBeNull()
  })

  it('pins one bounded managed-local Parquet revision and invalidates downstream state', async () => {
    const source = { id: 's1', type: 'source', position: { x: 0, y: 0 }, data: {
      title: 'orders', status: 'latest', config: { uri: '/data/orders.parquet', tableId: 't1' },
    } }
    const target = { id: 'out', type: 'write', position: { x: 100, y: 0 }, data: {
      title: 'output', status: 'latest', config: {},
    } }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ catalog: [{ ...useStore.getState().catalog[0], uri: '/data/orders.parquet' }],
      doc: { id: 'c', name: 'test', version: 1, nodes: [source, target], edges: [{ id: 'e', source: 's1', target: 'out' }] } } as any)
    mocks.datasetRevisions.mockResolvedValue({ items: [
      { datasetId: 'dataset-1', revisionId: '2', committedAt: '2026-07-16T12:00:00Z', retentionOwner: 'provider' },
      { datasetId: 'dataset-1', revisionId: '1', committedAt: '2026-07-15T12:00:00Z', retentionOwner: 'provider' },
    ], nextCursor: null, hasMore: false })
    render1(source.data)

    fireEvent.click(await screen.findByRole('button', { name: /Pin a version/i }))
    fireEvent.click(screen.getAllByRole('button').find((button) => button.textContent?.startsWith('1'))!)

    expect(useStore.getState().doc.nodes[0].data.config.datasetRef).toEqual({
      kind: 'exact', datasetId: 'dataset-1', revisionId: '1',
      lastKnown: { committedAt: '2026-07-15T12:00:00Z' },
    })
    expect(useStore.getState().doc.nodes[0].data.status).toBe('stale')
    expect(useStore.getState().doc.nodes[1].data.status).toBe('stale')
  })

  it('uses only the selected exact version facts on the card, not current-head or field detail', async () => {
    const selected = { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'rev-1' }
    const data = { title: 'orders', status: 'latest', config: {
      uri: '/data/orders.lance', tableId: 't1', datasetRef: selected,
    } }
    const headColumns = Array.from({ length: 5 }, (_, index) => ({
      name: `head_${index}`, type: 'int', capabilities: [],
    }))
    const pinnedColumns = Array.from({ length: 4 }, (_, index) => ({
      name: `pin_${index}`, type: 'int', capabilities: [],
    }))
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({
      catalog: [{
        id: 't1', name: 'orders', uri: '/data/orders.lance',
        rowCount: 1_500, version: 'v4', columns: headColumns,
      }],
      doc: { id: 'c', name: 'test', version: 1,
        nodes: [{ id: 's1', type: 'source', position: { x: 0, y: 0 }, data }], edges: [] },
    } as any)
    mocks.datasetRevision.mockResolvedValueOnce({
      datasetId: 'dataset-1', revisionId: 'rev-1', retentionOwner: 'provider',
      summary: { rowCount: 1_000 },
      preview: { columns: pinnedColumns, rows: [], hasMore: false, rowLimit: 100 },
    })

    render1(data)

    expect(await screen.findByText('Datasets · Saved version · 1,000 rows · 4 columns')).toBeInTheDocument()
    expect(screen.queryByText(/Current head/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Field evidence/i)).not.toBeInTheDocument()
  })

  it('omits revision controls once the provider proves it has no selector capability', async () => {
    const source = { id: 's1', type: 'source', position: { x: 0, y: 0 }, data: {
      title: 'orders', status: 'latest', config: { uri: 'mem://orders', tableId: 't1' },
    } }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ doc: { id: 'c', name: 'test', version: 1, nodes: [source], edges: [] } } as any)
    mocks.datasetRevisionCapabilities.mockResolvedValue({
      selectors: ['latest'], asOfOrdering: null, timezone: null,
    })
    render1(source.data)

    await waitFor(() => expect(screen.queryByRole('button', { name: 'Revision selection unavailable' })).not.toBeInTheDocument())
    expect(mocks.datasetRevisions).not.toHaveBeenCalled()
  })

  it('keeps the capability check visible while unresolved, then removes it when no selector is advertised', async () => {
    let resolveCapabilities!: (value: { selectors: Array<'latest'>; asOfOrdering: null; timezone: null }) => void
    mocks.datasetRevisionCapabilities.mockImplementationOnce(() => new Promise((resolve) => { resolveCapabilities = resolve }))
    const source = { id: 's1', type: 'source', position: { x: 0, y: 0 }, data: {
      title: 'orders', status: 'latest', config: { uri: 'mem://orders', tableId: 't1' },
    } }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ doc: { id: 'c', name: 'test', version: 1, nodes: [source], edges: [] } } as any)
    render1(source.data)

    expect(await screen.findByRole('button', { name: 'Checking revision capabilities…' })).toBeDisabled()
    resolveCapabilities({ selectors: ['latest'], asOfOrdering: null, timezone: null })
    await waitFor(() => expect(screen.queryByRole('button', { name: /revision/i })).not.toBeInTheDocument())
  })

  it('keeps an unknown capability failure visible and retries it instead of treating it as unsupported', async () => {
    mocks.datasetRevisionCapabilities
      .mockRejectedValueOnce(new Error('network unavailable'))
      .mockResolvedValueOnce({ selectors: ['latest'], asOfOrdering: null, timezone: null })
    const source = { id: 's1', type: 'source', position: { x: 0, y: 0 }, data: {
      title: 'orders', status: 'latest', config: { uri: 'mem://orders', tableId: 't1' },
    } }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ doc: { id: 'c', name: 'test', version: 1, nodes: [source], edges: [] } } as any)
    render1(source.data)

    expect(await screen.findByText(/Couldn't check revision capabilities: network unavailable/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Revision selection unavailable' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(mocks.datasetRevisionCapabilities).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('button', { name: /revision/i })).not.toBeInTheDocument())
  })

  it('preserves an unavailable pinned selection with a recoverable explanation', async () => {
    const selected = { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'missing',
      lastKnown: { committedAt: '2026-07-15T12:00:00Z' } }
    const data = { title: 'orders', status: 'stale', config: {
      uri: '/data/orders.lance', tableId: 't1', datasetRef: selected,
    } }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ catalog: [{ ...useStore.getState().catalog[0], uri: '/data/orders.lance' }],
      doc: { id: 'c', name: 'test', version: 1, nodes: [{ id: 's1', type: 'source', position: { x: 0, y: 0 }, data }], edges: [] } } as any)
    mocks.datasetRevision.mockRejectedValueOnce(Object.assign(
      new Error('dataset_revision_unavailable'),
      { status: 410, code: 'resource_gone', retryable: false },
    ))
    mocks.datasetRevisionCapabilities.mockResolvedValueOnce({
      selectors: ['latest'], asOfOrdering: null, timezone: null,
    })
    render1(data)

    expect(await screen.findByText(/Selected version is missing or compacted.*Selection preserved.*latest was not substituted/i)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/Last known provider commit.*stale/i)
    expect(screen.queryByRole('button', { name: 'Revision selection unavailable' })).not.toBeInTheDocument()
    expect(useStore.getState().doc.nodes[0].data.config.datasetRef).toEqual(selected)
  })

  it('preserves an unavailable as-of binding when the current provider has no selector', async () => {
    const selected = {
      kind: 'as_of' as const, asOf: '2026-07-15T12:00:00.000Z',
      resolved: {
        datasetId: 'dataset-1', revisionId: 'missing', committedAt: '2026-07-15T11:00:00Z',
        retentionOwner: 'provider', selector: 'as_of' as const,
      },
    }
    const data = { title: 'orders', status: 'stale', config: {
      uri: '/data/orders.parquet', tableId: 't1', datasetRef: selected,
    } }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ catalog: [{ ...useStore.getState().catalog[0], uri: '/data/orders.parquet' }],
      doc: { id: 'c', name: 'test', version: 1, nodes: [{ id: 's1', type: 'source', position: { x: 0, y: 0 }, data }], edges: [] } } as any)
    mocks.datasetRevisionCapabilities.mockResolvedValueOnce({
      selectors: ['latest'], asOfOrdering: null, timezone: null,
    })
    mocks.datasetRevision.mockRejectedValueOnce(Object.assign(
      new Error('dataset_revision_unavailable'), { status: 410, code: 'resource_gone', retryable: false },
    ))
    render1(data)

    expect(await screen.findByText(/Selected version is missing or compacted.*Selection preserved/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /revision/i })).not.toBeInTheDocument()
    expect(useStore.getState().doc.nodes[0].data.config.datasetRef).toEqual(selected)
  })

  it('keeps pinned recovery visible after the dataset registration disappears', async () => {
    const selected = { kind: 'exact' as const, datasetId: 'removed-dataset', revisionId: '3',
      lastKnown: { committedAt: '2026-07-15T12:00:00Z' } }
    const data = { title: 'removed source', status: 'stale', config: {
      uri: '/data/removed.lance', tableId: 'removed-table', datasetRef: selected,
    } }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ catalog: [], doc: { id: 'c', name: 'test', version: 1,
      nodes: [{ id: 's1', type: 'source', position: { x: 0, y: 0 }, data }], edges: [] } } as any)
    mocks.datasetRevision.mockRejectedValueOnce(Object.assign(
      new Error('dataset_revision_unavailable'),
      { status: 410, code: 'resource_gone', retryable: false },
    ))
    render1(data)

    expect(await screen.findByText(/Selected version is missing or compacted.*Selection preserved/i)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/Choose a new dataset above to create a new binding/i)
    expect(screen.queryByRole('button', { name: /follow current latest explicitly/i })).not.toBeInTheDocument()
    expect(useStore.getState().doc.nodes[0].data.config.datasetRef).toEqual(selected)
  })

  it('distinguishes permission loss and retries the same exact identity', async () => {
    const selected = { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: '7' }
    const data = { title: 'orders', status: 'stale', config: {
      uri: '/data/orders.lance', tableId: 't1', datasetRef: selected,
    } }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ catalog: [{ ...useStore.getState().catalog[0], uri: '/data/orders.lance' }],
      doc: { id: 'c', name: 'test', version: 1, nodes: [{ id: 's1', type: 'source', position: { x: 0, y: 0 }, data }], edges: [] } } as any)
    mocks.datasetRevision
      .mockRejectedValueOnce(Object.assign(new Error('dataset_revision_permission_lost'), {
        status: 403, code: 'permission_denied', retryable: false,
      }))
      .mockResolvedValueOnce({
        datasetId: 'dataset-1', revisionId: '7', retentionOwner: 'provider', summary: { rowCount: 1 },
        preview: { columns: [], rows: [], hasMore: false, rowLimit: 100 },
      })
    render1(data)

    expect(await screen.findByText(/Permission to open the selected version was lost.*latest was not substituted/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry selected version' }))
    expect(await screen.findByText('Datasets · Saved version · 1 row · 0 columns')).toBeInTheDocument()
    expect(mocks.datasetRevision).toHaveBeenNthCalledWith(2, 'dataset-1', '7')
    expect(useStore.getState().doc.nodes[0].data.config.datasetRef).toEqual(selected)
  })

  it('renders adjacent as-of and revision timestamps in explicit UTC under a non-UTC timezone', async () => {
    vi.stubEnv('TZ', 'America/Los_Angeles')
    const selected = {
      kind: 'as_of' as const, asOf: '2026-07-16T12:38:00Z',
      resolved: {
        datasetId: 'dataset-1', revisionId: 'rev-pin', committedAt: '2026-07-16T11:38:00Z',
        retentionOwner: 'provider', selector: 'as_of' as const,
      },
    }
    const data = { title: 'orders', status: 'latest', config: {
      uri: 'mem://orders', tableId: 't1', datasetRef: selected,
    } }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ doc: { id: 'c', name: 'test', version: 1,
      nodes: [{ id: 's1', type: 'source', position: { x: 0, y: 0 }, data }], edges: [] } } as any)
    mocks.datasetRevisionCapabilities.mockResolvedValue({
      selectors: ['exact', 'latest', 'as_of'],
      asOfOrdering: 'latest_committed_at_at_or_before', timezone: 'UTC',
    })
    mocks.datasetRevisions.mockResolvedValue({
      items: [{
        datasetId: 'dataset-1', revisionId: 'rev-head', committedAt: '2026-07-16T15:38:00Z',
        retentionOwner: 'provider',
      }],
      nextCursor: null, hasMore: false,
    })
    mocks.datasetRevision.mockResolvedValueOnce({
      datasetId: 'dataset-1', revisionId: 'rev-pin', committedAt: '2026-07-16T11:38:00Z',
      retentionOwner: 'provider', summary: { rowCount: 1 },
      preview: { columns: [], rows: [], hasMore: false, rowLimit: 100 },
    })

    render1(data)

    const control = await screen.findByRole('button', {
      name: 'Change version selected as of Jul 16, 2026, 12:38:00 UTC',
    })
    fireEvent.click(control)
    expect(screen.getByText('Jul 16, 2026, 15:38:00 UTC')).toBeInTheDocument()
    expect(screen.getByLabelText('As-of UTC date and time')).toBeInTheDocument()
    expect(await screen.findByText('Datasets · Saved version · 1 row · 0 columns')).toBeInTheDocument()
  })

  it('stores UTC as-of intent with exact and as-of capabilities after history is ready', async () => {
    const source = { id: 's1', type: 'source', position: { x: 0, y: 0 }, data: {
      title: 'orders', status: 'latest', config: { uri: 'mem://orders', tableId: 't1' },
    } }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ doc: { id: 'c', name: 'test', version: 1, nodes: [source], edges: [] } } as any)
    mocks.datasetRevisionCapabilities.mockResolvedValue({
      selectors: ['exact', 'latest', 'as_of'],
      asOfOrdering: 'latest_committed_at_at_or_before', timezone: 'UTC',
    })
    const localIntent = '2026-07-16T12:30'
    const utcIntent = new Date(`${localIntent}Z`).toISOString()
    const resolved = {
      datasetId: 'dataset-1', revisionId: '7', committedAt: '2026-07-16T15:00:00Z',
      retentionOwner: 'provider', selector: 'as_of',
    }
    mocks.resolveDatasetRevision.mockResolvedValue(resolved)
    render1(source.data)

    fireEvent.click(await screen.findByRole('button', { name: 'Choose a saved or as-of version' }))
    expect(screen.getByText(/latest provider commit at or before this UTC instant \(inclusive\)/i)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('As-of UTC date and time'), { target: { value: localIntent } })
    fireEvent.click(screen.getByRole('button', { name: 'Resolve once' }))

    await waitFor(() => expect(mocks.resolveDatasetRevision).toHaveBeenCalledWith('t1', utcIntent))
    expect(useStore.getState().doc.nodes[0].data.config.datasetRef).toEqual({
      kind: 'as_of', asOf: utcIntent, resolved,
    })
  })

  it('offers as-of-only resolution without requesting exact history', async () => {
    const source = { id: 's1', type: 'source', position: { x: 0, y: 0 }, data: {
      title: 'orders', status: 'latest', config: { uri: 'mem://orders', tableId: 't1' },
    } }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ doc: { id: 'c', name: 'test', version: 1, nodes: [source], edges: [] } } as any)
    mocks.datasetRevisionCapabilities.mockResolvedValue({
      selectors: ['as_of'], asOfOrdering: 'latest_committed_at_at_or_before', timezone: 'UTC',
    })
    const localIntent = '2026-07-16T12:30'
    const utcIntent = new Date(`${localIntent}Z`).toISOString()
    const resolved = {
      datasetId: 'dataset-1', revisionId: '7', committedAt: '2026-07-16T15:00:00Z',
      retentionOwner: 'provider', selector: 'as_of',
    }
    mocks.resolveDatasetRevision.mockResolvedValue(resolved)
    render1(source.data)

    fireEvent.click(await screen.findByRole('button', { name: 'Choose version by time' }))
    expect(mocks.datasetRevisions).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('As-of UTC date and time'), { target: { value: localIntent } })
    fireEvent.click(screen.getByRole('button', { name: 'Resolve once' }))

    await waitFor(() => expect(mocks.resolveDatasetRevision).toHaveBeenCalledWith('t1', utcIntent))
    expect(useStore.getState().doc.nodes[0].data.config.datasetRef).toEqual({
      kind: 'as_of', asOf: utcIntent, resolved,
    })
  })
})
