import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ReactFlowProvider } from '@xyflow/react'

// importing the store triggers autosave side-effects → stub the api client
const mocks = vi.hoisted(() => ({
  tablesPage: vi.fn(), destinations: vi.fn(), browseDestination: vi.fn(),
  registerFile: vi.fn(), mkdirDestination: vi.fn(),
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
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({
      kernelUp: true,
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
})
