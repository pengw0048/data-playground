import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ReactFlowProvider } from '@xyflow/react'

// importing the store triggers autosave side-effects → stub the api client
const mocks = vi.hoisted(() => ({
  tablesPage: vi.fn(), destinations: vi.fn(), browseDestination: vi.fn(),
  registerFile: vi.fn(), mkdirDestination: vi.fn(), datasetRevisions: vi.fn(), datasetRevision: vi.fn(),
  datasetRevisionCapabilities: vi.fn(), resolveDatasetRevision: vi.fn(),
}))
vi.mock('../../api/client', () => ({ api: mocks }))

import './source'                          // registers the Source card via register()
import { getComponent } from '../registry'
import { useStore } from '../../store/graph'

const Source = getComponent('source')!
const render1 = (data: object) =>
  render(<ReactFlowProvider><Source id="s1" data={data as never} /></ReactFlowProvider>)

describe('Source card — honest counts + empty/offline (UX-14)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.tablesPage.mockResolvedValue({ items: [], total: 0, hasMore: false })
    mocks.destinations.mockResolvedValue({ destinations: [{ id: 'local', name: 'Workspace', backend: 'local', root: '/data' }], backends: ['local'] })
    mocks.browseDestination.mockResolvedValue({ path: '', entries: [{ name: 'new.csv', kind: 'file', uri: 'file:///data/new.csv' }], writable: true })
    mocks.mkdirDestination.mockResolvedValue({ ok: true })
    mocks.datasetRevisions.mockResolvedValue({ items: [], nextCursor: null, hasMore: false })
    mocks.datasetRevisionCapabilities.mockResolvedValue({
      selectors: ['exact', 'latest'], asOfOrdering: null, timezone: null,
    })
    mocks.datasetRevision.mockResolvedValue({
      datasetId: 'dataset-1', revisionId: '1', retentionOwner: 'provider', summary: { rowCount: 1 },
      preview: { columns: [], rows: [], hasMore: false, rowLimit: 100 },
    })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({
      kernelUp: true,
      canvasRole: 'owner',
      doc: { id: 'c', name: 'test', version: 1, nodes: [], edges: [] },
      catalog: [{ id: 't1', name: 'orders', uri: 'mem://orders', rowCount: null, version: 'v1', columns: [{ name: 'a', type: 'int', capabilities: [] }] }],
      past: [], future: [], selectedIds: [],
    } as any)
  })
  afterEach(() => cleanup())

  it('shows "—" for an unknown row count, not a fake "0 rows"', () => {
    render1({ title: 'source', status: 'draft', config: { tableId: 't1' } })
    expect(screen.getByText(/—\s*rows/)).toBeInTheDocument()
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

  it('does not change the source until a browsed file has been registered successfully', async () => {
    const oldConfig = { uri: 'mem://orders', tableId: 't1' }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({
      doc: { id: 'c', name: 'test', version: 1, nodes: [{ id: 's1', type: 'source', position: { x: 0, y: 0 }, data: { title: 'orders source', status: 'draft', config: oldConfig } }], edges: [] },
    } as any)
    mocks.registerFile
      .mockRejectedValueOnce(new Error('HTTP 422: unsupported dataset'))
      .mockResolvedValueOnce({ id: 't2', name: 'new', uri: 'file:///data/new.csv', rowCount: 1, columns: [{ name: 'x', type: 'int', capabilities: [] }] })
    render1({ title: 'orders source', status: 'draft', config: oldConfig })
    fireEvent.click(screen.getByText('orders'))
    fireEvent.click(screen.getByText(/Browse files/i))
    fireEvent.click(await screen.findByText('new.csv'))

    expect(await screen.findByText(/Couldn't open file: HTTP 422/i)).toBeInTheDocument()
    expect(useStore.getState().doc.nodes[0].data.config).toEqual(oldConfig)
    expect(useStore.getState().doc.nodes[0].data.title).toBe('orders source')

    fireEvent.click(screen.getByText('new.csv'))
    await waitFor(() => expect(useStore.getState().doc.nodes[0].data.config).toMatchObject({ uri: 'file:///data/new.csv', tableId: 't2' }))
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

    fireEvent.click(await screen.findByRole('button', { name: /Pin exact revision/i }))
    fireEvent.click(screen.getAllByRole('button').find((button) => button.textContent?.startsWith('1'))!)

    expect(useStore.getState().doc.nodes[0].data.config.datasetRef).toEqual({
      kind: 'exact', datasetId: 'dataset-1', revisionId: '1',
      lastKnown: { committedAt: '2026-07-15T12:00:00Z' },
    })
    expect(useStore.getState().doc.nodes[0].data.status).toBe('stale')
    expect(useStore.getState().doc.nodes[1].data.status).toBe('stale')
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

    expect(await screen.findByText(/revision missing.*missing or compacted.*Selection preserved.*latest was not substituted/i)).toBeInTheDocument()
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

    expect(await screen.findByText(/revision missing.*missing or compacted.*Selection preserved/i)).toBeInTheDocument()
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

    expect(await screen.findByText(/revision 3.*registration is missing or compacted.*Selection preserved/i)).toBeInTheDocument()
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

    expect(await screen.findByText(/Permission to open exact revision 7 was lost.*latest was not substituted/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry exact revision' }))
    expect(await screen.findByText(/Pinned exact revision 7.*1 rows/i)).toBeInTheDocument()
    expect(mocks.datasetRevision).toHaveBeenNthCalledWith(2, 'dataset-1', '7')
    expect(useStore.getState().doc.nodes[0].data.config.datasetRef).toEqual(selected)
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

    fireEvent.click(await screen.findByRole('button', { name: 'Choose exact or as-of revision' }))
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

    fireEvent.click(await screen.findByRole('button', { name: 'Choose revision as of a time' }))
    expect(mocks.datasetRevisions).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('As-of UTC date and time'), { target: { value: localIntent } })
    fireEvent.click(screen.getByRole('button', { name: 'Resolve once' }))

    await waitFor(() => expect(mocks.resolveDatasetRevision).toHaveBeenCalledWith('t1', utcIntent))
    expect(useStore.getState().doc.nodes[0].data.config.datasetRef).toEqual({
      kind: 'as_of', asOf: utcIntent, resolved,
    })
  })
})
