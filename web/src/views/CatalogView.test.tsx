import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Suspense, startTransition, type ReactNode } from 'react'
import type { CatalogTable } from '../types/api'

const mocks = vi.hoisted(() => ({
  tablesPage: vi.fn(), facets: vi.fn(), catalogTree: vi.fn(), searchCatalog: vi.fn(),
  registerFile: vi.fn(), registerDataset: vi.fn(), lineage: vi.fn(), sample: vi.fn(), table: vi.fn(),
  datasetRevisions: vi.fn(), datasetRevision: vi.fn(),
  setTableMetadata: vi.fn(), saveTableEdit: vi.fn(), unregisterTable: vi.fn(), unregisterTables: vi.fn(),
  catalogFolders: vi.fn(), createFolder: vi.fn(), renameFolder: vi.fn(), deleteFolder: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: mocks,
  KernelError: class KernelError extends Error { status = 0 },
}))

const store = vi.hoisted(() => ({
  addToCanvas: vi.fn(), rememberTables: vi.fn(), uploadDataset: vi.fn(), pushToast: vi.fn(),
  kernelInfo: { capabilities: ['catalog.folder_mutation', 'catalog.atomic_metadata_edit'] },  // catalog mutation UI is capability-gated
}))
vi.mock('../store/graph', () => ({ useStore: (select: (state: typeof store) => unknown) => select(store) }))

// Make infinite-scroll deterministic: tests explicitly ask the list to request its next page.
vi.mock('../ui/VirtualList', () => ({
  VirtualList: ({ items, renderRow, onEndReached, emptyNote }: {
    items: CatalogTable[]; renderRow: (item: CatalogTable) => ReactNode
    onEndReached?: () => void; emptyNote?: ReactNode
  }) => <div>
    {items.length ? items.map((item) => <div key={item.id}>{renderRow(item)}</div>) : emptyNote}
    <button data-testid="request-next-page" disabled={!onEndReached} onClick={() => onEndReached?.()}>next page</button>
  </div>,
}))

import { CatalogView } from './CatalogView'

const TABLE: CatalogTable = {
  id: 't1', name: 'orders', uri: 'mem://orders', rowCount: 2, version: 'v1', folder: 'sales',
  metadataRevision: 'm1_orders',
  columns: [{ name: 'order_id', type: 'int', capabilities: ['key'] }],
}
const TABLE_2: CatalogTable = {
  id: 't2', name: 'customers', uri: 'mem://customers', rowCount: 1, version: 'v1',
  columns: [{ name: 'customer_id', type: 'int', capabilities: ['key'] }],
}
const FACETS = { folders: [{ value: 'sales', count: 1 }], tags: [], owners: [] }

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const folder = (path: string) => ({ name: path.split('/').pop()!, path, tableCount: 0 })
const tree = (prefix: string, paths: string[]) => ({ prefix, folders: paths.map(folder), tables: [] })

describe('CatalogView request and mutation truth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.tablesPage.mockResolvedValue({ items: [TABLE], total: 1, hasMore: false })
    mocks.facets.mockResolvedValue(FACETS)
    mocks.catalogTree.mockResolvedValue({ prefix: '', folders: [], tables: [] })
    mocks.searchCatalog.mockResolvedValue([])
    mocks.lineage.mockResolvedValue({ rootUri: TABLE.uri, nodes: [], edges: [] })
    mocks.datasetRevisions.mockRejectedValue(Object.assign(new Error('history absent'), { status: 501 }))
    mocks.sample.mockResolvedValue({
      columns: TABLE.columns, rows: [{ order_id: 1 }], rowCount: 2,
      hasMore: true, truncated: true, completeness: 'page',
      notPreviewable: false, wire: 'dataset',
    })
    mocks.table.mockResolvedValue(TABLE)
    mocks.setTableMetadata.mockResolvedValue(TABLE)
    mocks.saveTableEdit.mockResolvedValue({ ...TABLE, metadataRevision: 'm1_test' })
    mocks.unregisterTable.mockResolvedValue({ ok: true })
    mocks.catalogFolders.mockResolvedValue([])
    mocks.createFolder.mockResolvedValue({ path: 'archive' })
    mocks.renameFolder.mockResolvedValue({ ok: true })
    mocks.deleteFolder.mockResolvedValue({ ok: true })
    store.uploadDataset.mockResolvedValue(null)
  })
  afterEach(() => cleanup())

  it('shows a 5xx folder-tree failure as an error and retries instead of claiming there are no folders', async () => {
    mocks.catalogTree
      .mockRejectedValueOnce(new Error('HTTP 500: catalog backend failed'))
      .mockResolvedValueOnce({ prefix: '', folders: [{ name: 'sales', path: 'sales', tableCount: 1 }], tables: [] })
      .mockRejectedValueOnce(new Error('HTTP 502: branch failed'))
      .mockResolvedValueOnce({ prefix: 'sales', folders: [{ name: 'daily', path: 'sales/daily', tableCount: 1 }], tables: [] })
    render(<CatalogView />)

    expect(await screen.findByText(/Couldn't load folders: HTTP 500/i)).toBeInTheDocument()
    expect(screen.queryByText('No folders yet')).toBeNull()
    fireEvent.click(screen.getByTestId('folder-tree-retry'))
    await waitFor(() => expect(mocks.catalogTree).toHaveBeenCalledTimes(2))

    fireEvent.click(screen.getByRole('button', { name: 'Expand folder sales' }))
    expect(await screen.findByText(/Couldn't load: HTTP 502/i)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('folder-branch-retry-sales'))
    expect(await screen.findByText(/daily/)).toBeInTheDocument()
  })

  it('keeps the first page when load-more is offline and exposes an explicit retry', async () => {
    mocks.tablesPage
      .mockResolvedValueOnce({ items: [TABLE], total: 2, hasMore: true })
      .mockRejectedValueOnce(new Error('Failed to fetch'))
      .mockResolvedValueOnce({ items: [TABLE_2], total: 2, hasMore: false })
    render(<CatalogView />)
    expect(await screen.findByText('orders')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('request-next-page'))
    expect(await screen.findByText(/Couldn't load more: Failed to fetch/i)).toBeInTheDocument()
    expect(screen.getByText('orders')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('catalog-load-more-retry'))
    expect(await screen.findByText('customers')).toBeInTheDocument()
  })

  it('uses the canonical lineage root and shows aggregated fact counts', async () => {
    const currentRoot = 'mem://orders-current'
    mocks.lineage.mockResolvedValue({
      rootUri: currentRoot,
      nodes: [
        { id: 'upstream', name: 'raw_orders', uri: 'mem://raw-orders', kind: 'table' },
        { id: TABLE.id, name: TABLE.name, uri: currentRoot, kind: 'table' },
        { id: 'downstream', name: 'daily_orders', uri: 'mem://daily-orders', kind: 'table' },
      ],
      edges: [
        { parent: 'mem://raw-orders', child: currentRoot, factCount: 1 },
        { parent: currentRoot, child: 'mem://daily-orders', factCount: 3 },
      ],
    })

    render(<CatalogView />)
    fireEvent.click(await screen.findByText('orders'))

    expect(await screen.findByText('raw_orders')).toBeInTheDocument()
    expect(screen.getByText(/1 fact$/)).toBeInTheDocument()
    expect(screen.getByText(/3 facts$/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('raw_orders'))
    await waitFor(() => expect(mocks.table).toHaveBeenCalledWith('upstream'))
    fireEvent.click(screen.getByText('daily_orders'))
    await waitFor(() => expect(mocks.table).toHaveBeenCalledWith('downstream'))
  })

  it('surfaces detail failures, preserves edits after a failed save, and refreshes the tree after save and delete', async () => {
    mocks.lineage
      .mockRejectedValueOnce(new Error('HTTP 503: lineage unavailable'))
      .mockResolvedValueOnce({ rootUri: TABLE.uri, nodes: [], edges: [] })
    mocks.sample
      .mockRejectedValueOnce(new Error('Failed to fetch'))
      .mockResolvedValueOnce({
        columns: TABLE.columns, rows: [{ order_id: 1 }], rowCount: 2,
        hasMore: true, truncated: true, completeness: 'page',
        notPreviewable: false, wire: 'dataset',
      })
    mocks.saveTableEdit
      .mockRejectedValueOnce(new Error('HTTP 409: concurrent edit'))
      .mockResolvedValueOnce({ ...TABLE, folder: 'curated/sales' })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<CatalogView />)
    fireEvent.click(await screen.findByText('orders'))

    expect(await screen.findByText(/Couldn't load lineage: HTTP 503/i)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('detail-lineage-retry'))
    expect(await screen.findByText('no upstream datasets')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('detail-preview'))
    expect(await screen.findByText(/Couldn't load preview: Failed to fetch/i)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('detail-preview-retry'))
    expect(await screen.findByRole('cell', { name: '1' })).toBeInTheDocument()
    expect(screen.getByText('Dataset preview · showing 1 of 2 rows')).toBeInTheDocument()

    const folder = screen.getByTestId('detail-folder') as HTMLInputElement
    fireEvent.change(folder, { target: { value: 'curated/sales' } })
    fireEvent.click(screen.getByTestId('detail-save'))
    await waitFor(() => expect(store.pushToast).toHaveBeenCalledWith('HTTP 409: concurrent edit', 'error'))
    expect(folder.value).toBe('curated/sales')
    expect(mocks.catalogTree).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByTestId('detail-save'))
    await waitFor(() => expect(mocks.catalogTree).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByTestId('detail-unregister'))
    await waitFor(() => expect(mocks.catalogTree).toHaveBeenCalledTimes(3))
  })

  it('renders every row counted by the catalog preview scope label', async () => {
    const rows = Array.from({ length: 50 }, (_, order_id) => ({ order_id }))
    mocks.sample.mockResolvedValue({
      columns: TABLE.columns, rows, rowCount: 50,
      hasMore: false, truncated: false, completeness: 'complete',
      notPreviewable: false, wire: 'dataset',
    })
    render(<CatalogView />)
    fireEvent.click(await screen.findByText('orders'))
    fireEvent.click(screen.getByTestId('detail-preview'))

    expect(await screen.findByText('Complete dataset · 50 rows')).toBeInTheDocument()
    expect(screen.getAllByRole('cell')).toHaveLength(50)
  })

  it('labels a catalog prefix preview as non-random and exposes its input revision', async () => {
    mocks.sample.mockResolvedValue({
      columns: TABLE.columns, rows: [{ order_id: 1 }], rowCount: 2,
      hasMore: true, truncated: true, completeness: 'page', notPreviewable: false, wire: 'dataset',
      sampleProvenance: {
        strategy: 'prefix', seed: null, requestedRows: 50, scannedRows: null, returnedRows: 1,
        totalRows: 2, datasetIdentity: TABLE.uri, datasetRevision: 'revision-1',
        identity: 'a'.repeat(64), limitations: ['This is a prefix preview, not representative or random.'],
      },
    })
    render(<CatalogView />)
    fireEvent.click(await screen.findByText('orders'))
    fireEvent.click(screen.getByTestId('detail-preview'))

    expect(await screen.findByText(/Prefix preview.*Requested 50 rows.*scanned unknown.*returned 1.*total 2/i)).toBeInTheDocument()
    expect(screen.getByText(`Input ${TABLE.uri} · revision revision-1.`)).toBeInTheDocument()
    expect(screen.getByText('This is a prefix preview, not representative or random.')).toBeInTheDocument()
  })

  it('does not infer an empty dataset from an empty bounded preview batch', async () => {
    mocks.sample.mockResolvedValue({
      columns: TABLE.columns, rows: [], rowCount: null,
      hasMore: null, truncated: true, completeness: 'unknown',
      notPreviewable: false, wire: 'dataset',
    })
    render(<CatalogView />)
    fireEvent.click(await screen.findByText('orders'))
    fireEvent.click(screen.getByTestId('detail-preview'))

    expect(await screen.findByText('No rows returned by this preview; dataset size is unknown.')).toBeInTheDocument()
    expect(screen.queryByText('No rows in this dataset')).not.toBeInTheDocument()
  })

  it('keeps a known nonzero dataset distinct from an empty preview batch', async () => {
    mocks.sample.mockResolvedValue({
      columns: TABLE.columns, rows: [], rowCount: 120,
      hasMore: true, truncated: true, completeness: 'page',
      notPreviewable: false, wire: 'dataset',
    })
    render(<CatalogView />)
    fireEvent.click(await screen.findByText('orders'))
    fireEvent.click(screen.getByTestId('detail-preview'))

    expect(await screen.findByText(
      'No rows returned by this preview; the dataset contains 120 rows.',
    )).toBeInTheDocument()
  })
})

describe('CatalogView selection, register modal, and rename', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.tablesPage.mockResolvedValue({ items: [TABLE, TABLE_2], total: 2, hasMore: false })
    mocks.facets.mockResolvedValue(FACETS)
    mocks.catalogTree.mockResolvedValue({ prefix: '', folders: [], tables: [] })
    mocks.searchCatalog.mockResolvedValue([])
    mocks.lineage.mockResolvedValue({ rootUri: TABLE.uri, nodes: [], edges: [] })
    mocks.datasetRevisions.mockRejectedValue(Object.assign(new Error('history absent'), { status: 501 }))
    mocks.saveTableEdit.mockResolvedValue(TABLE)
    mocks.unregisterTables.mockResolvedValue({ deleted: ['t1', 't2'], missing: [] })
    mocks.registerDataset.mockResolvedValue(TABLE)
    mocks.catalogFolders.mockResolvedValue([])
    mocks.createFolder.mockResolvedValue({ path: 'archive' })
    mocks.renameFolder.mockResolvedValue({ ok: true })
    mocks.deleteFolder.mockResolvedValue({ ok: true })
    store.uploadDataset.mockResolvedValue(null)
  })
  afterEach(() => cleanup())

  it('multi-selects rows and batch-deletes them in one request', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<CatalogView />)
    fireEvent.click(await screen.findByLabelText('Select orders'))
    fireEvent.click(screen.getByLabelText('Select customers'))
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('catalog-delete-selected'))
    await waitFor(() => expect(mocks.unregisterTables).toHaveBeenCalledWith(['t1', 't2']))
  })

  it('registers a dataset through the modal with the full payload', async () => {
    render(<CatalogView />)
    fireEvent.click(await screen.findByTestId('register-dataset'))
    fireEvent.change(screen.getByTestId('register-uri'), { target: { value: '/data/events.parquet' } })
    fireEvent.click(screen.getByTestId('register-submit'))
    await waitFor(() => expect(mocks.registerDataset).toHaveBeenCalledWith(
      expect.objectContaining({ uri: '/data/events.parquet' })))
  })

  it('renames a dataset from the detail drawer', async () => {
    render(<CatalogView />)
    fireEvent.click(await screen.findByText('orders'))
    fireEvent.change(screen.getByTestId('detail-name'), { target: { value: 'daily orders' } })
    fireEvent.click(screen.getByTestId('detail-save'))
    await waitFor(() => expect(mocks.saveTableEdit).toHaveBeenCalledWith('t1',
      expect.objectContaining({ name: 'daily orders' })))
  })

  it('stages keys with metadata and offers reload or reapply after a conflict', async () => {
    const conflict = Object.assign(new Error('catalog metadata changed'), { status: 409 })
    mocks.saveTableEdit.mockRejectedValueOnce(conflict).mockResolvedValueOnce({ ...TABLE, name: 'reapplied', metadataRevision: 'm1_new' })
    mocks.table.mockResolvedValue({ ...TABLE, name: 'other editor', metadataRevision: 'm1_other' })
    render(<CatalogView />)
    fireEvent.click(await screen.findByText('orders'))
    fireEvent.change(screen.getByTestId('detail-name'), { target: { value: 'reapplied' } })
    fireEvent.click(screen.getByTestId('detail-pk-order_id'))
    fireEvent.click(screen.getByTestId('detail-save'))
    expect(await screen.findByText('Another editor saved changes first.')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Reapply'))
    await waitFor(() => expect(mocks.saveTableEdit).toHaveBeenLastCalledWith('t1', expect.objectContaining({
      expectedRevision: 'm1_other', name: 'reapplied', declaredKey: ['order_id'],
    })))
  })

  it('keeps recovery available when conflict refresh fails and protects Escape dismissal', async () => {
    const conflict = Object.assign(new Error('catalog metadata changed'), { status: 409 })
    mocks.saveTableEdit.mockRejectedValueOnce(conflict)
    mocks.table.mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce({ ...TABLE, name: 'other editor', metadataRevision: 'm1_other' })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<CatalogView />)
    fireEvent.click(await screen.findByText('orders'))
    fireEvent.change(screen.getByTestId('detail-name'), { target: { value: 'my draft' } })

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(confirm).toHaveBeenCalledWith('Discard unsaved catalog edits?')
    expect(screen.getByRole('dialog', { name: 'orders' })).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('detail-save'))
    expect(await screen.findByText('Another editor saved changes first.')).toBeInTheDocument()
    expect(screen.getByText('Reload')).toBeInTheDocument()
    expect(screen.queryByText('Reapply')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Reload'))
    await waitFor(() => expect(screen.getByTestId('detail-name')).toHaveValue('other editor'))
  })

  it('creates an empty folder from the tree', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('archive')
    render(<CatalogView />)
    fireEvent.click(await screen.findByTestId('folder-new'))
    await waitFor(() => expect(mocks.createFolder).toHaveBeenCalledWith('archive'))
  })

  it('renames a folder from the tree and moves the selected filter with it', async () => {
    mocks.catalogTree.mockResolvedValue({ prefix: '', folders: [{ name: 'sales', path: 'sales', tableCount: 1 }], tables: [] })
    vi.spyOn(window, 'prompt').mockReturnValue('revenue')
    render(<CatalogView />)
    // select the folder first, then rename it — the filter must follow the rename, not strand
    fireEvent.click(await screen.findByText('📁 sales'))
    fireEvent.click(screen.getByTestId('folder-rename-sales'))
    await waitFor(() => expect(mocks.renameFolder).toHaveBeenCalledWith('sales', 'revenue'))
    expect(await screen.findByText('📁 revenue')).toBeInTheDocument()
  })

  it('rehydrates an expanded branch after rename remounts it under the new path', async () => {
    let renamed = false
    mocks.catalogTree.mockImplementation(async (prefix: string) => {
      if (!prefix) {
        const path = renamed ? 'revenue' : 'sales'
        return { prefix: '', folders: [{ name: path, path, tableCount: 1 }], tables: [] }
      }
      const path = `${prefix}/daily`
      return { prefix, folders: [{ name: 'daily', path, tableCount: 1 }], tables: [] }
    })
    mocks.renameFolder.mockImplementation(async () => { renamed = true; return { ok: true } })
    vi.spyOn(window, 'prompt').mockReturnValue('revenue')
    render(<CatalogView />)

    fireEvent.click(await screen.findByRole('button', { name: 'Expand folder sales' }))
    expect(await screen.findByTestId('folder-rename-sales/daily')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('folder-rename-sales'))

    await waitFor(() => expect(mocks.catalogTree).toHaveBeenCalledWith('revenue', expect.anything()))
    expect(await screen.findByRole('button', { name: 'Collapse folder revenue' })).toBeInTheDocument()
    expect(await screen.findByTestId('folder-rename-revenue/daily')).toBeInTheDocument()
  })

  it('deletes a folder from the tree after confirming where its datasets go', async () => {
    mocks.catalogTree.mockResolvedValue({ prefix: '', folders: [{ name: 'sales', path: 'sales', tableCount: 1 }], tables: [] })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<CatalogView />)
    fireEvent.click(await screen.findByTestId('folder-delete-sales'))
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('the top level'))
    await waitFor(() => expect(mocks.deleteFolder).toHaveBeenCalledWith('sales'))
  })
})

describe('CatalogView folder child request identity', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.clearAllMocks()
    store.kernelInfo = { capabilities: ['catalog.folder_mutation', 'catalog.atomic_metadata_edit'] }
    mocks.tablesPage.mockResolvedValue({ items: [], total: 0, hasMore: false })
    mocks.facets.mockResolvedValue({ folders: [], tags: [], owners: [] })
    mocks.searchCatalog.mockResolvedValue([])
    mocks.datasetRevisions.mockRejectedValue(Object.assign(new Error('history absent'), { status: 501 }))
    mocks.catalogFolders.mockResolvedValue([])
    mocks.createFolder.mockResolvedValue({ path: 'created' })
    mocks.renameFolder.mockResolvedValue({ ok: true })
    mocks.deleteFolder.mockResolvedValue({ ok: true })
    store.uploadDataset.mockResolvedValue(null)
  })
  afterEach(() => cleanup())

  it('keeps reversed A and B responses bound to their own expanded branches', async () => {
    const a = deferred<ReturnType<typeof tree>>()
    const b = deferred<ReturnType<typeof tree>>()
    mocks.catalogTree.mockImplementation((prefix: string) => {
      if (!prefix) return Promise.resolve(tree('', ['A', 'B']))
      return prefix === 'A' ? a.promise : b.promise
    })
    render(<CatalogView />)

    fireEvent.click(await screen.findByRole('button', { name: 'Expand folder A' }))
    fireEvent.click(screen.getByRole('button', { name: 'Expand folder B' }))
    await waitFor(() => expect(mocks.catalogTree).toHaveBeenCalledWith('A', expect.anything()))
    await waitFor(() => expect(mocks.catalogTree).toHaveBeenCalledWith('B', expect.anything()))

    await act(async () => { b.resolve(tree('B', ['B/b-current'])); await b.promise })
    expect(await screen.findByText('📁 b-current')).toBeInTheDocument()
    await act(async () => { a.resolve(tree('A', ['A/a-current'])); await a.promise })

    expect(await screen.findByText('📁 a-current')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Collapse folder A' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Collapse folder B' })).toBeInTheDocument()
  })

  it('makes A→B→A re-expansion supersede the first A generation without losing focus', async () => {
    const firstA = deferred<ReturnType<typeof tree>>()
    const secondA = deferred<ReturnType<typeof tree>>()
    let aCalls = 0
    let firstSignal: AbortSignal | undefined
    mocks.catalogTree.mockImplementation((prefix: string, options?: { signal?: AbortSignal }) => {
      if (!prefix) return Promise.resolve(tree('', ['A', 'B']))
      if (prefix === 'B') return Promise.resolve(tree('B', ['B/b-current']))
      aCalls += 1
      if (aCalls === 1) {
        firstSignal = options?.signal
        return firstA.promise
      }
      return secondA.promise
    })
    render(<CatalogView />)

    fireEvent.click(await screen.findByRole('button', { name: 'Expand folder A' }))
    await waitFor(() => expect(firstSignal).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: 'Collapse folder A' }))
    expect(firstSignal?.aborted).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Expand folder B' }))
    expect(await screen.findByText('📁 b-current')).toBeInTheDocument()
    fireEvent.click(screen.getByText('📁 B'))
    fireEvent.click(screen.getByRole('button', { name: 'Expand folder A' }))
    await waitFor(() => expect(aCalls).toBe(2))

    await act(async () => { secondA.resolve(tree('A', ['A/a-current'])); await secondA.promise })
    expect(await screen.findByText('📁 a-current')).toBeInTheDocument()
    await act(async () => { firstA.resolve(tree('A', ['A/a-stale'])); await firstA.promise })

    expect(screen.queryByText('📁 a-stale')).toBeNull()
    expect(screen.getByText('📁 a-current')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove filter 📁 B' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Collapse folder A' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Collapse folder B' })).toBeInTheDocument()
  })

  it('does not let an aborted error clear the newer loading state or replace its retry error', async () => {
    const first = deferred<ReturnType<typeof tree>>()
    const second = deferred<ReturnType<typeof tree>>()
    let calls = 0
    mocks.catalogTree.mockImplementation((prefix: string) => {
      if (!prefix) return Promise.resolve(tree('', ['A']))
      calls += 1
      if (calls === 1) return first.promise
      if (calls === 2) return second.promise
      return Promise.resolve(tree('A', ['A/recovered']))
    })
    render(<CatalogView />)

    fireEvent.click(await screen.findByRole('button', { name: 'Expand folder A' }))
    expect(await screen.findByText('Loading…')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Collapse folder A' }))
    fireEvent.click(screen.getByRole('button', { name: 'Expand folder A' }))
    await waitFor(() => expect(calls).toBe(2))

    await act(async () => { first.reject(new Error('stale failure')); await first.promise.catch(() => {}) })
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByText(/stale failure/)).toBeNull()
    await act(async () => { second.reject(new Error('latest failure')); await second.promise.catch(() => {}) })

    expect(await screen.findByText(/Couldn't load: latest failure/)).toBeInTheDocument()
    expect(screen.queryByText('Loading…')).toBeNull()
    fireEvent.click(screen.getByTestId('folder-branch-retry-A'))
    expect(await screen.findByText('📁 recovered')).toBeInTheDocument()
    expect(screen.queryByText(/latest failure/)).toBeNull()
  })

  it('binds background revision refreshes and loaded children to the latest revision', async () => {
    const revisionOne = deferred<ReturnType<typeof tree>>()
    const revisionTwo = deferred<ReturnType<typeof tree>>()
    const revisionSignals: AbortSignal[] = []
    let branchCalls = 0
    mocks.catalogTree.mockImplementation((prefix: string, options?: { signal?: AbortSignal }) => {
      if (!prefix) return Promise.resolve(tree('', ['A']))
      branchCalls += 1
      if (branchCalls === 1) return Promise.resolve(tree('A', ['A/initial']))
      if (options?.signal) revisionSignals.push(options.signal)
      return branchCalls === 2 ? revisionOne.promise : revisionTwo.promise
    })
    vi.spyOn(window, 'prompt').mockReturnValue('created')
    render(<CatalogView />)

    fireEvent.click(await screen.findByRole('button', { name: 'Expand folder A' }))
    expect(await screen.findByText('📁 initial')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('folder-new'))
    await waitFor(() => expect(branchCalls).toBe(2))
    expect(screen.getByText('Refreshing…')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('folder-new'))
    await waitFor(() => expect(branchCalls).toBe(3))
    expect(revisionSignals[0].aborted).toBe(true)

    await act(async () => { revisionTwo.resolve(tree('A', ['A/revision-two'])); await revisionTwo.promise })
    expect(await screen.findByText('📁 revision-two')).toBeInTheDocument()
    await act(async () => { revisionOne.resolve(tree('A', ['A/revision-one-stale'])); await revisionOne.promise })

    expect(screen.queryByText('📁 revision-one-stale')).toBeNull()
    expect(screen.getByText('📁 revision-two')).toBeInTheDocument()
    expect(screen.queryByText('Refreshing…')).toBeNull()
  })

  it('invalidates branch children when the catalog provider snapshot changes', async () => {
    const oldProvider = deferred<ReturnType<typeof tree>>()
    let branchCalls = 0
    let oldSignal: AbortSignal | undefined
    mocks.catalogTree.mockImplementation((prefix: string, options?: { signal?: AbortSignal }) => {
      if (!prefix) return Promise.resolve(tree('', ['A']))
      branchCalls += 1
      if (branchCalls === 1) {
        oldSignal = options?.signal
        return oldProvider.promise
      }
      return Promise.resolve(tree('A', ['A/new-provider']))
    })
    const view = render(<CatalogView />)

    fireEvent.click(await screen.findByRole('button', { name: 'Expand folder A' }))
    await waitFor(() => expect(oldSignal).toBeDefined())
    store.kernelInfo = { capabilities: ['catalog.folder_mutation', 'catalog.atomic_metadata_edit'] }
    view.rerender(<CatalogView />)

    await waitFor(() => expect(oldSignal?.aborted).toBe(true))
    expect(await screen.findByText('📁 new-provider')).toBeInTheDocument()
    await act(async () => { oldProvider.resolve(tree('A', ['A/old-provider'])); await oldProvider.promise })
    expect(screen.queryByText('📁 old-provider')).toBeNull()
    expect(screen.getByText('📁 new-provider')).toBeInTheDocument()
  })

  it('lets the committed provider request finish when a newer provider render is abandoned', async () => {
    const committedBranch = deferred<ReturnType<typeof tree>>()
    const blockedRender = deferred<void>()
    let blockCommit = false
    mocks.catalogTree.mockImplementation((prefix: string) => {
      if (!prefix) return Promise.resolve(tree('', ['A']))
      return committedBranch.promise
    })
    function BlockAfterCatalog() {
      if (blockCommit) throw blockedRender.promise
      return null
    }
    function Shell({ version }: { version: number }) {
      return <Suspense fallback={<div data-testid="blocked-provider-render">blocked {version}</div>}>
        <CatalogView />
        <BlockAfterCatalog />
      </Suspense>
    }
    const view = render(<Shell version={0} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Expand folder A' }))
    await waitFor(() => expect(mocks.catalogTree).toHaveBeenCalledWith('A', expect.anything()))

    blockCommit = true
    store.kernelInfo = { capabilities: ['catalog.folder_mutation', 'catalog.atomic_metadata_edit'] }
    await act(async () => {
      startTransition(() => view.rerender(<Shell version={1} />))
    })
    expect(screen.queryByTestId('blocked-provider-render')).toBeNull()

    await act(async () => {
      committedBranch.resolve(tree('A', ['A/committed-provider']))
      await committedBranch.promise
    })
    expect(await screen.findByText('📁 committed-provider')).toBeInTheDocument()
  })

  it('aborts an old-path request after rename and hydrates the remapped expanded branch', async () => {
    const oldPath = deferred<ReturnType<typeof tree>>()
    let renamed = false
    let oldSignal: AbortSignal | undefined
    mocks.catalogTree.mockImplementation((prefix: string, options?: { signal?: AbortSignal }) => {
      if (!prefix) return Promise.resolve(tree('', [renamed ? 'B' : 'A']))
      if (prefix === 'A') {
        oldSignal = options?.signal
        return oldPath.promise
      }
      return Promise.resolve(tree('B', ['B/current']))
    })
    mocks.renameFolder.mockImplementation(async () => { renamed = true; return { ok: true } })
    vi.spyOn(window, 'prompt').mockReturnValue('B')
    render(<CatalogView />)

    fireEvent.click(await screen.findByText('📁 A'))
    fireEvent.click(screen.getByRole('button', { name: 'Expand folder A' }))
    await waitFor(() => expect(oldSignal).toBeDefined())
    fireEvent.click(screen.getByTestId('folder-rename-A'))

    await waitFor(() => expect(oldSignal?.aborted).toBe(true))
    expect(await screen.findByText('📁 current')).toBeInTheDocument()
    await act(async () => { oldPath.resolve(tree('A', ['A/stale'])); await oldPath.promise })
    expect(screen.queryByText('📁 stale')).toBeNull()
    expect(screen.getByRole('button', { name: 'Collapse folder B' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove filter 📁 B' })).toBeInTheDocument()
  })

  it('aborts a branch request after delete and ignores its late result', async () => {
    const pending = deferred<ReturnType<typeof tree>>()
    let deleted = false
    let signal: AbortSignal | undefined
    mocks.catalogTree.mockImplementation((prefix: string, options?: { signal?: AbortSignal }) => {
      if (!prefix) return Promise.resolve(tree('', deleted ? [] : ['A']))
      signal = options?.signal
      return pending.promise
    })
    mocks.deleteFolder.mockImplementation(async () => { deleted = true; return { ok: true } })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<CatalogView />)

    fireEvent.click(await screen.findByRole('button', { name: 'Expand folder A' }))
    await waitFor(() => expect(signal).toBeDefined())
    fireEvent.click(screen.getByTestId('folder-delete-A'))

    await waitFor(() => expect(signal?.aborted).toBe(true))
    expect(await screen.findByText('No folders yet')).toBeInTheDocument()
    await act(async () => { pending.resolve(tree('A', ['A/stale'])); await pending.promise })
    expect(screen.queryByText('📁 stale')).toBeNull()
  })

  it('aborts a branch request when navigation unmounts the catalog view', async () => {
    const pending = deferred<ReturnType<typeof tree>>()
    let signal: AbortSignal | undefined
    mocks.catalogTree.mockImplementation((prefix: string, options?: { signal?: AbortSignal }) => {
      if (!prefix) return Promise.resolve(tree('', ['A']))
      signal = options?.signal
      return pending.promise
    })
    const view = render(<CatalogView />)

    fireEvent.click(await screen.findByRole('button', { name: 'Expand folder A' }))
    await waitFor(() => expect(signal).toBeDefined())
    view.unmount()

    expect(signal?.aborted).toBe(true)
    await act(async () => { pending.resolve(tree('A', ['A/stale'])); await pending.promise })
    expect(store.pushToast).not.toHaveBeenCalled()
  })
})
