import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Suspense, startTransition, type ReactNode } from 'react'
import type { CatalogTable } from '../types/api'

const mocks = vi.hoisted(() => ({
  tablesPage: vi.fn(), facets: vi.fn(), catalogTree: vi.fn(), searchCatalog: vi.fn(),
  registerFile: vi.fn(), registerDataset: vi.fn(), lineage: vi.fn(), sample: vi.fn(), table: vi.fn(), tableByRegistration: vi.fn(),
  datasetRevisions: vi.fn(), datasetRevision: vi.fn(), datasetRevisionCapabilities: vi.fn(), resolveDatasetRevision: vi.fn(),
  setTableMetadata: vi.fn(), saveTableEdit: vi.fn(), unregisterTable: vi.fn(), unregisterTables: vi.fn(),
  catalogFolders: vi.fn(), createFolder: vi.fn(), renameFolder: vi.fn(), deleteFolder: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: mocks,
  KernelError: class KernelError extends Error {
    status: number
    constructor(status = 0, message = '') { super(message); this.status = status }
  },
}))

const store = vi.hoisted(() => ({
  addToCanvas: vi.fn(), rememberTables: vi.fn(), uploadDataset: vi.fn(), pushToast: vi.fn(),
  kernelInfo: { capabilities: ['catalog.folder_mutation', 'catalog.atomic_metadata_edit', 'catalog.cas_unregister'] },  // catalog mutation UI is capability-gated
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

import { AddDataModal, CatalogDetail, CatalogDiscovery } from './CatalogDiscovery'

const TABLE: CatalogTable = {
  id: 't1', registrationId: 'registration-orders', name: 'orders', uri: 'mem://orders', rowCount: 2, version: 'v1', folder: 'sales',
  metadataRevision: 'm1_orders',
  columns: [{ name: 'order_id', type: 'int', capabilities: ['key'] }],
}
const TABLE_2: CatalogTable = {
  id: 't2', registrationId: 'registration-customers', name: 'customers', uri: 'mem://customers', rowCount: 1, version: 'v1',
  metadataRevision: 'm1_customers',
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

function CatalogDiscoveryFixture() {
  return <CatalogDiscovery sourceIdentity={store.kernelInfo} foldersMutable
    onUseTables={vi.fn()} onUploadDataset={store.uploadDataset} />
}

function openCatalogDetails() {
  fireEvent.click(screen.getByText('Edit catalog details'))
}

describe('Catalog discovery request and mutation truth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.tablesPage.mockResolvedValue({ items: [TABLE], total: 1, hasMore: false })
    mocks.facets.mockResolvedValue(FACETS)
    mocks.catalogTree.mockResolvedValue({ prefix: '', folders: [], tables: [] })
    mocks.searchCatalog.mockResolvedValue([])
    mocks.lineage.mockResolvedValue({ rootUri: TABLE.uri, nodes: [], edges: [] })
    mocks.datasetRevisions.mockRejectedValue(Object.assign(new Error('history absent'), { status: 501 }))
    mocks.datasetRevisionCapabilities.mockResolvedValue({
      selectors: [], asOfOrdering: null, timezone: null, datasetViewSave: false,
    })
    mocks.resolveDatasetRevision.mockRejectedValue(Object.assign(new Error('revision resolution absent'), { status: 501 }))
    mocks.sample.mockResolvedValue({
      columns: TABLE.columns, rows: [{ order_id: 1 }], rowCount: 2,
      hasMore: true, truncated: true, completeness: 'page',
      notPreviewable: false, wire: 'dataset',
    })
    mocks.table.mockResolvedValue(TABLE)
    mocks.tableByRegistration.mockResolvedValue(TABLE)
    mocks.setTableMetadata.mockResolvedValue(TABLE)
    mocks.saveTableEdit.mockResolvedValue({ ...TABLE, metadataRevision: 'm1_test' })
    mocks.unregisterTable.mockResolvedValue({ ok: true })
    mocks.catalogFolders.mockResolvedValue([])
    mocks.createFolder.mockResolvedValue({ path: 'archive' })
    mocks.renameFolder.mockResolvedValue({ ok: true })
    mocks.deleteFolder.mockResolvedValue({ ok: true })
    store.uploadDataset.mockResolvedValue(null)
  })

  it('uses the selected registration normally when no complete exact revision pair is present', async () => {
    mocks.tableByRegistration.mockResolvedValue({ ...TABLE, id: 'tbl_orders', registrationId: 'registration-path' })
    render(<CatalogDiscovery sourceIdentity={store.kernelInfo} foldersMutable
      selectedRegistrationId="registration-path" onUseTables={vi.fn()} onUploadDataset={store.uploadDataset} />)
    await screen.findByRole('dialog', { name: 'orders' })
    expect(mocks.tableByRegistration).toHaveBeenCalledWith('registration-path')
    expect(mocks.table).not.toHaveBeenCalled()
  })

  it('labels browser upload and kernel-visible registration as distinct add-data actions', async () => {
    const onUploadDataset = vi.fn().mockResolvedValue(TABLE)
    const onCompleted = vi.fn()
    const { container } = render(<AddDataModal onClose={vi.fn()} onUploadDataset={onUploadDataset} onCompleted={onCompleted} />)

    expect(screen.getByText(/bytes are uploaded to Data Playground/i)).toBeVisible()
    expect(screen.getByText(/Lance datasets are directories and are not supported here/i)).toBeVisible()
    expect(screen.getByText(/kernel.server can already read/i)).toBeVisible()
    fireEvent.change(container.querySelector('input[type="file"]')!, {
      target: { files: [new File(['id\n1\n'], 'local.csv', { type: 'text/csv' })] },
    })
    await waitFor(() => expect(onUploadDataset).toHaveBeenCalledWith(expect.objectContaining({ name: 'local.csv' })))
    expect(onCompleted).toHaveBeenCalled()
  })

  it('states the kernel path convention and keeps an unreachable registration actionable', async () => {
    mocks.registerDataset.mockRejectedValue(new Error('HTTP 400: cannot read /mounted/missing.parquet'))
    render(<AddDataModal onClose={vi.fn()} onUploadDataset={vi.fn()} onCompleted={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Register path or URI' }))
    expect(screen.getByText(/Absolute paths start on the kernel host/i)).toBeVisible()
    expect(screen.getByText(/relative paths resolve from the kernel working directory/i)).toBeVisible()
    fireEvent.change(screen.getByTestId('register-uri'), { target: { value: '/mounted/missing.parquet' } })
    fireEvent.click(screen.getByTestId('register-submit'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Confirm it exists and is readable from the kernel host')
    expect(mocks.registerDataset).toHaveBeenCalledWith(expect.objectContaining({ uri: '/mounted/missing.parquet' }))
  })

  it('uses the exact receipt identity rather than a mismatched path registration and canonicalizes the path', async () => {
    const onSelectedTableChange = vi.fn()
    mocks.tableByRegistration.mockResolvedValue({ ...TABLE, id: 'tbl_orders', registrationId: 'registration-current' })
    render(<CatalogDiscovery sourceIdentity={store.kernelInfo} foldersMutable
      selectedRegistrationId="registration-other" initialRevisionId="rev-receipt" initialRevisionDatasetId="logical-receipt-id"
      onSelectedTableChange={onSelectedTableChange} onUseTables={vi.fn()} onUploadDataset={store.uploadDataset} />)
    await screen.findByRole('dialog', { name: 'orders' })
    expect(mocks.tableByRegistration).toHaveBeenCalledTimes(1)
    expect(mocks.tableByRegistration).toHaveBeenCalledWith('logical-receipt-id')
    expect(mocks.tableByRegistration).not.toHaveBeenCalledWith('registration-other')
    expect(onSelectedTableChange).toHaveBeenCalledWith(
      expect.objectContaining({ registrationId: 'registration-current' }), 'route',
    )
  })

  it('renders exact revision rows as the full-page primary view without reading latest rows', async () => {
    const onUseTables = vi.fn()
    mocks.datasetRevision.mockResolvedValue({
      datasetId: 'logical-receipt-id', revisionId: 'rev-receipt',
      committedAt: '2026-07-30T12:00:00Z', retentionOwner: 'core',
      parentRevisionId: null, producerOperation: 'write',
      summary: { rowCount: 2, totalBytes: 128 },
      preview: {
        columns: [
          { name: 'order_id', type: 'int', capabilities: ['key'] },
          { name: 'status', type: 'string', capabilities: [] },
        ],
        rows: [{ order_id: 7, status: 'exact-only-row' }],
        hasMore: true,
        rowLimit: 100,
      },
    })
    render(<CatalogDiscovery sourceIdentity={store.kernelInfo} foldersMutable
      selectedRegistrationId="registration-other" initialRevisionId="rev-receipt"
      initialRevisionDatasetId="logical-receipt-id"
      onUseTables={onUseTables} onUploadDataset={store.uploadDataset} />)

    const viewer = await screen.findByTestId('dataset-viewer')
    expect(viewer).toHaveClass('absolute', 'inset-0')
    expect(viewer).not.toHaveClass('w-[420px]')
    expect(await screen.findByRole('cell', { name: 'exact-only-row' })).toBeVisible()
    expect(screen.getByLabelText('Dataset preview scope')).toHaveTextContent(
      'exact revision logical-receipt-id@rev-receipt')
    expect(screen.getByLabelText('Dataset preview scope')).toHaveTextContent('capped at 100')
    expect(mocks.datasetRevision).toHaveBeenCalledWith('logical-receipt-id', 'rev-receipt')
    expect(mocks.datasetRevision).toHaveBeenCalledTimes(1)
    expect(mocks.sample).not.toHaveBeenCalled()
    expect(mocks.resolveDatasetRevision).not.toHaveBeenCalled()
    expect(screen.getByTestId('detail-use-unavailable')).toHaveTextContent('Exact revision is view-only')
    expect(screen.queryByTestId('detail-use')).not.toBeInTheDocument()
    expect(onUseTables).not.toHaveBeenCalled()
  })

  it('preserves the exact route when catalog metadata is saved from the viewer', async () => {
    const onSelectedTableChange = vi.fn()
    const saved = { ...TABLE, name: 'renamed orders', metadataRevision: 'm2_orders' }
    mocks.datasetRevision.mockResolvedValue({
      datasetId: 'logical-receipt-id', revisionId: 'rev-receipt',
      committedAt: '2026-07-30T12:00:00Z', retentionOwner: 'core',
      parentRevisionId: null, producerOperation: 'write',
      summary: { rowCount: 2, totalBytes: 128 },
      preview: {
        columns: TABLE.columns, rows: [{ order_id: 7 }], hasMore: false, rowLimit: 100,
      },
    })
    mocks.saveTableEdit.mockResolvedValue(saved)
    render(<CatalogDiscovery sourceIdentity={store.kernelInfo} foldersMutable
      selectedRegistrationId="registration-other" initialRevisionId="rev-receipt"
      initialRevisionDatasetId="logical-receipt-id"
      onSelectedTableChange={onSelectedTableChange}
      onUseTables={vi.fn()} onUploadDataset={store.uploadDataset} />)

    await screen.findByTestId('detail-use-unavailable')
    onSelectedTableChange.mockClear()
    openCatalogDetails()
    fireEvent.change(screen.getByTestId('detail-name'), { target: { value: saved.name } })
    fireEvent.click(screen.getByTestId('detail-save'))

    await waitFor(() => expect(mocks.saveTableEdit).toHaveBeenCalled())
    expect(onSelectedTableChange).toHaveBeenCalledWith(saved, 'route')
  })

  it('fails an unavailable exact viewer closed without substituting latest rows', async () => {
    mocks.datasetRevision.mockRejectedValue(Object.assign(new Error('compacted'), { status: 410 }))
    render(<CatalogDiscovery sourceIdentity={store.kernelInfo} foldersMutable
      selectedRegistrationId="registration-other" initialRevisionId="rev-receipt"
      initialRevisionDatasetId="logical-receipt-id"
      onUseTables={vi.fn()} onUploadDataset={store.uploadDataset} />)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This exact revision is unavailable or no longer retained. Latest was not substituted.')
    expect(screen.queryByTestId('detail-preview-scroll')).not.toBeInTheDocument()
    expect(mocks.sample).not.toHaveBeenCalled()
    expect(mocks.resolveDatasetRevision).not.toHaveBeenCalled()
  })

  it('re-resolves an already selected path when a same-page exact receipt pair names another dataset', async () => {
    mocks.tableByRegistration.mockImplementation(async (registrationId: string) => registrationId === 'registration-path'
      ? TABLE_2
      : { ...TABLE, id: 'tbl_orders', registrationId: 'registration-current' })
    const props = { sourceIdentity: store.kernelInfo, foldersMutable: true,
      selectedRegistrationId: 'registration-path', onUseTables: vi.fn(), onUploadDataset: store.uploadDataset }
    const { rerender } = render(<CatalogDiscovery {...props} />)
    await screen.findByRole('dialog', { name: 'customers' })
    expect(mocks.tableByRegistration).toHaveBeenLastCalledWith('registration-path')

    rerender(<CatalogDiscovery {...props} initialRevisionId="rev-receipt" initialRevisionDatasetId="logical-receipt-id" />)
    await screen.findByRole('dialog', { name: 'orders' })
    expect(mocks.tableByRegistration).toHaveBeenLastCalledWith('logical-receipt-id')
    expect(mocks.tableByRegistration).toHaveBeenCalledTimes(2)
  })

  it.each([403, 404])('keeps exact receipt lookup failures (%i) visible without trying the path registration', async (status) => {
    mocks.tableByRegistration.mockRejectedValue(Object.assign(new Error(`receipt ${status} unavailable`), { status }))
    render(<CatalogDiscovery sourceIdentity={store.kernelInfo} foldersMutable
      selectedRegistrationId="registration-other" initialRevisionId="rev-receipt" initialRevisionDatasetId="logical-receipt-id"
      onUseTables={vi.fn()} onUploadDataset={store.uploadDataset} />)
    expect(await screen.findByRole('alert')).toHaveTextContent(`receipt ${status} unavailable`)
    expect(mocks.tableByRegistration).toHaveBeenCalledTimes(1)
    expect(mocks.tableByRegistration).toHaveBeenCalledWith('logical-receipt-id')
    expect(mocks.tableByRegistration).not.toHaveBeenCalledWith('registration-other')
  })

  it('re-resolves a later exact receipt link after its earlier route was canonicalized', async () => {
    const logical = 'logical-receipt-id'
    const onSelectedTableChange = vi.fn()
    mocks.tableByRegistration.mockImplementation(async (registrationId: string) => registrationId === 'registration-customers'
      ? TABLE_2
      : { ...TABLE, id: 'tbl_orders', registrationId: 'registration-current' })
    const props = {
      sourceIdentity: store.kernelInfo, foldersMutable: true,
      onUseTables: vi.fn(), onUploadDataset: store.uploadDataset, onSelectedTableChange,
    }
    const exact = { initialRevisionId: 'rev-receipt', initialRevisionDatasetId: logical }
    const { rerender } = render(<CatalogDiscovery {...props} {...exact} selectedRegistrationId={logical} />)
    await screen.findByRole('dialog', { name: 'orders' })
    expect(mocks.tableByRegistration).toHaveBeenCalledTimes(1)

    // Workspace writes the canonical registration after the exact logical lookup succeeds.
    rerender(<CatalogDiscovery {...props} {...exact} selectedRegistrationId="registration-current" />)
    await waitFor(() => expect(onSelectedTableChange).toHaveBeenCalledWith(
      expect.objectContaining({ registrationId: 'registration-current' }), 'route',
    ))
    expect(mocks.tableByRegistration).toHaveBeenCalledTimes(1)

    rerender(<CatalogDiscovery {...props} selectedRegistrationId="registration-customers" />)
    await screen.findByRole('dialog', { name: 'customers' })
    expect(mocks.tableByRegistration).toHaveBeenCalledTimes(2)

    rerender(<CatalogDiscovery {...props} {...exact} selectedRegistrationId={logical} />)
    await screen.findByRole('dialog', { name: 'orders' })
    expect(mocks.tableByRegistration).toHaveBeenCalledTimes(3)
    expect(mocks.tableByRegistration).toHaveBeenLastCalledWith(logical)
  })
  afterEach(() => cleanup())

  it('delegates Use to the supplied destination without assuming the current canvas', async () => {
    const onUseTables = vi.fn()
    render(<CatalogDiscovery sourceIdentity={store.kernelInfo} foldersMutable
      onUseTables={onUseTables} onUploadDataset={store.uploadDataset} />)

    expect(await screen.findByText(/^1 dataset$/)).toBeInTheDocument()
    expect(screen.queryByText(/^1 datasets$/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'All datasets' })).toBeInTheDocument()
    expect(screen.getByLabelText('Search datasets')).toBeInTheDocument()
    expect(screen.getByLabelText('Sort datasets')).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Use dataset orders' }))
    expect(onUseTables).toHaveBeenCalledWith([TABLE])
    expect(store.addToCanvas).not.toHaveBeenCalled()
  })

  it('keeps an unavailable Workspace location disabled and exposes only an explicit retry', async () => {
    const onOpenInWorkspace = vi.fn()
    const onRetryWorkspaceLocation = vi.fn()
    const props = {
      sourceIdentity: store.kernelInfo, foldersMutable: true,
      onUseTables: vi.fn(), onUploadDataset: store.uploadDataset,
      onOpenInWorkspace, onRetryWorkspaceLocation,
    }
    const { rerender } = render(<CatalogDiscovery {...props} workspaceLocation={{
      state: 'unavailable', retryable: true,
      reason: 'This dataset is not currently available in Workspace. catalog temporarily offline',
    }} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open dataset orders' }))

    const open = screen.getByRole('button', { name: /Open in Workspace/ })
    expect(open).toBeDisabled()
    expect(open).toHaveAttribute(
      'title', 'This dataset is not currently available in Workspace. catalog temporarily offline',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetryWorkspaceLocation).toHaveBeenCalledTimes(1)
    expect(onOpenInWorkspace).not.toHaveBeenCalled()

    rerender(<CatalogDiscovery {...props} workspaceLocation={{ state: 'available' }} />)
    expect(open).toBeEnabled()
    fireEvent.click(open)
    expect(onOpenInWorkspace).toHaveBeenCalledWith(TABLE)
  })

  it('shows Open in Workspace for a root dataset without inventing a folder label', async () => {
    const rootTable = { ...TABLE_2, folder: undefined }
    const onOpenInWorkspace = vi.fn()
    mocks.tablesPage.mockResolvedValue({ items: [rootTable], total: 1, hasMore: false })
    render(<CatalogDiscovery sourceIdentity={store.kernelInfo} foldersMutable
      onUseTables={vi.fn()} onUploadDataset={store.uploadDataset}
      onOpenInWorkspace={onOpenInWorkspace} workspaceLocation={{ state: 'available' }} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Open dataset customers' }))
    const open = screen.getByRole('button', { name: 'Open in Workspace →' })
    expect(open).toBeEnabled()
    fireEvent.click(open)
    expect(onOpenInWorkspace).toHaveBeenCalledWith(rootTable)
  })

  it('keeps a 5,000-dataset discovery path bounded to pages, facets, and the lazy tree', async () => {
    mocks.tablesPage
      .mockResolvedValueOnce({ items: [TABLE], total: 5_000, hasMore: true })
      .mockResolvedValueOnce({ items: [TABLE_2], total: 5_000, hasMore: true })
    render(<CatalogDiscovery sourceIdentity={store.kernelInfo} foldersMutable />)

    expect(await screen.findByText('orders')).toBeInTheDocument()
    expect(mocks.tablesPage).toHaveBeenNthCalledWith(1, expect.objectContaining({ limit: 50, offset: 0 }))
    expect(mocks.facets).toHaveBeenCalledTimes(1)
    expect(mocks.catalogTree).toHaveBeenCalledWith('')
    expect(mocks.catalogFolders).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('request-next-page'))
    expect(await screen.findByText('customers')).toBeInTheDocument()
    expect(mocks.tablesPage).toHaveBeenNthCalledWith(2, expect.objectContaining({ limit: 50, offset: 1 }))
    expect(mocks.catalogFolders).not.toHaveBeenCalled()
  })

  it('shows a 5xx folder-tree failure as an error and retries instead of claiming there are no folders', async () => {
    mocks.catalogTree
      .mockRejectedValueOnce(new Error('HTTP 500: catalog backend failed'))
      .mockResolvedValueOnce({ prefix: '', folders: [{ name: 'sales', path: 'sales', tableCount: 1 }], tables: [] })
      .mockRejectedValueOnce(new Error('HTTP 502: branch failed'))
      .mockResolvedValueOnce({ prefix: 'sales', folders: [{ name: 'daily', path: 'sales/daily', tableCount: 1 }], tables: [] })
    render(<CatalogDiscoveryFixture />)

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
    render(<CatalogDiscoveryFixture />)
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

    render(<CatalogDiscoveryFixture />)
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
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByText('orders'))

    expect(await screen.findByText(/Couldn't load lineage: HTTP 503/i)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('detail-lineage-retry'))
    expect(await screen.findAllByText('No related datasets yet.')).toHaveLength(1)
    expect(screen.queryByText('Parents')).not.toBeInTheDocument()
    expect(screen.queryByText('Children')).not.toBeInTheDocument()

    expect(await screen.findByText(/Couldn't load latest preview: Failed to fetch/i)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('detail-preview-retry'))
    expect(await screen.findByRole('cell', { name: '1' })).toBeInTheDocument()
    expect(screen.getByText('Showing 1 preview row.')).toBeInTheDocument()

    openCatalogDetails()
    const folder = screen.getByTestId('detail-folder') as HTMLInputElement
    fireEvent.change(folder, { target: { value: 'curated/sales' } })
    fireEvent.click(screen.getByTestId('detail-save'))
    await waitFor(() => expect(store.pushToast).toHaveBeenCalledWith('HTTP 409: concurrent edit', 'error'))
    expect(folder.value).toBe('curated/sales')
    expect(mocks.catalogTree).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByTestId('detail-save'))
    await waitFor(() => expect(mocks.catalogTree).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByTestId('detail-unregister'))
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('not underlying data'))
    await waitFor(() => expect(mocks.catalogTree).toHaveBeenCalledTimes(3))
  })

  it('shows the full bounded first page as the primary dataset view', async () => {
    const rows = Array.from({ length: 50 }, (_, order_id) => ({ order_id }))
    mocks.sample.mockResolvedValue({
      columns: TABLE.columns, rows, rowCount: 50,
      hasMore: false, truncated: false, completeness: 'complete',
      notPreviewable: false, wire: 'dataset',
    })
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByText('orders'))
    expect(await screen.findByText('Showing 50 preview rows.')).toBeInTheDocument()
    expect(screen.queryByText('rows 1–50')).not.toBeInTheDocument()
    expect(screen.getAllByRole('cell')).toHaveLength(50)
    expect(screen.getAllByText('Showing 50 preview rows.')).toHaveLength(1)
    expect(screen.getByTestId('detail-preview-scroll').querySelector('th')).toHaveClass('sticky', 'top-0')
  })

  it('keeps default schema evidence and scrollable preview inspection keyboard reachable', async () => {
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByText('orders'))

    expect(await screen.findByRole('button', { name: 'Inspect evidence for order_id' })).toBeVisible()
    const details = screen.getByTestId('detail-dataset-details')
    expect(details).not.toHaveAttribute('open')
    expect(details).toHaveTextContent('Dataset details')
    expect(screen.getByTestId('dataset-location')).not.toBeVisible()
    fireEvent.click(screen.getByText('Dataset details'))
    expect(details).toHaveAttribute('open')
    expect(screen.getByTestId('dataset-location')).toHaveTextContent(TABLE.uri)
    expect(screen.getByRole('button', { name: 'Copy dataset location' })).toBeVisible()
    expect(screen.getByText('Edit catalog details').parentElement).not.toHaveAttribute('open')
    expect(screen.getByTestId('dataset-detail-content')).toHaveAttribute('tabindex', '0')
    expect(await screen.findByTestId('detail-preview-scroll')).toHaveAttribute('tabindex', '0')
  })

  it('keeps every wide-schema field evidence action in the default Schema inspection', async () => {
    const columns = Array.from({ length: 12 }, (_, index) => ({
      name: `column_${index + 1}`, type: 'int64', capabilities: [],
    }))
    render(<CatalogDetail table={{ ...TABLE, columns }} onClose={vi.fn()} onUse={vi.fn()}
      onChanged={vi.fn()} onFolder={vi.fn()} onDeleted={vi.fn()} onOpenTable={vi.fn()}
      onColumn={vi.fn()} />)

    const schema = screen.getByTestId('detail-schema-scroll')
    expect(schema).toHaveAttribute('tabindex', '0')
    expect(await within(schema).findByRole('button', { name: 'Inspect evidence for column_12' })).toBeInTheDocument()
    expect(screen.queryByText(/more columns in Catalog maintenance/)).not.toBeInTheDocument()
    await waitFor(() => expect(mocks.sample).toHaveBeenCalled())
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
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByText('orders'))
    expect(await screen.findByText('Showing 1 preview row.')).toBeInTheDocument()
    expect(screen.getByTestId('preview-details')).not.toHaveAttribute('open')
    fireEvent.click(screen.getByText('Preview details'))
    expect(screen.getByText(/Requested 50 rows.*scanned unknown.*returned 1.*total 2/i)).toBeInTheDocument()
    expect(screen.getByText(`Input ${TABLE.uri} · revision revision-1.`)).toBeInTheDocument()
    expect(screen.getByText('This is a prefix preview, not representative or random.')).toBeInTheDocument()
  })

  it('does not infer an empty dataset from an empty bounded preview batch', async () => {
    mocks.sample.mockResolvedValue({
      columns: TABLE.columns, rows: [], rowCount: null,
      hasMore: null, truncated: true, completeness: 'unknown',
      notPreviewable: false, wire: 'dataset',
    })
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByText('orders'))
    expect(await screen.findByText('No rows returned by this preview; dataset size is unknown.')).toBeInTheDocument()
    expect(screen.queryByText('No rows in this dataset')).not.toBeInTheDocument()
  })

  it('keeps a known nonzero dataset distinct from an empty preview batch', async () => {
    mocks.sample.mockResolvedValue({
      columns: TABLE.columns, rows: [], rowCount: 120,
      hasMore: true, truncated: true, completeness: 'page',
      notPreviewable: false, wire: 'dataset',
    })
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByText('orders'))
    expect(await screen.findByText(
      'No rows returned by this preview; the dataset contains 120 rows.',
    )).toBeInTheDocument()
  })

  it('keeps v3 facts revision-bound while preview advances, then refreshes exact v4 facts', async () => {
    const v3Columns = [{ name: 'legacy_code', type: 'string' }]
    const v4Columns = [{ name: 'order_id', type: 'int' }, { name: 'status', type: 'string' }]
    const cachedV3 = { ...TABLE, rowCount: 3, version: 'catalog-v3', columns: v3Columns }
    const v3 = {
      datasetId: 'orders-dataset', revisionId: '3', committedAt: '2026-07-24T12:00:00Z',
      retentionOwner: 'provider', selector: 'latest',
    }
    const v4 = {
      datasetId: 'orders-dataset', revisionId: '4', committedAt: '2026-07-25T12:00:00Z',
      retentionOwner: 'provider', selector: 'latest',
    }
    mocks.tablesPage.mockResolvedValue({ items: [cachedV3], total: 1, hasMore: false })
    mocks.lineage.mockResolvedValue({ rootUri: cachedV3.uri, nodes: [], edges: [] })
    mocks.resolveDatasetRevision
      .mockResolvedValueOnce(v3)
      .mockResolvedValueOnce(v3)
      .mockResolvedValueOnce(v4)
      .mockResolvedValue(v4)
    mocks.datasetRevision
      .mockResolvedValueOnce({
        ...v3, parentRevisionId: '2', producerOperation: null,
        summary: { rowCount: 3 },
        preview: { columns: v3Columns, rows: [{ legacy_code: 'old' }], hasMore: false, rowLimit: 100 },
      })
      .mockRejectedValueOnce(new Error('provider offline'))
      .mockResolvedValueOnce({
        ...v4, parentRevisionId: '3', producerOperation: null,
        summary: { rowCount: 4 },
        preview: { columns: v4Columns, rows: [{ order_id: 1, status: 'ready' }], hasMore: false, rowLimit: 100 },
      })
    mocks.sample.mockResolvedValue({
      columns: v4Columns, rows: [{ order_id: 1, status: 'ready' }], rowCount: 4,
      hasMore: true, truncated: true, completeness: 'page', notPreviewable: false, wire: 'dataset',
      sampleProvenance: {
        strategy: 'prefix', seed: null, requestedRows: 30, scannedRows: null, returnedRows: 1,
        totalRows: 4, datasetIdentity: cachedV3.uri, datasetRevision: 'lance-v4',
        identity: 'b'.repeat(64), limitations: [],
      },
    })

    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByText('orders'))

    expect(await screen.findByText(/not bound to latest head orders-dataset@3/i)).toBeInTheDocument()
    expect(screen.queryByTestId('dataset-facts-source')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('refresh-dataset-facts'))
    expect(await screen.findByTestId('dataset-facts-source')).toHaveTextContent('Exact revision orders-dataset@3')
    expect(screen.getByText('3 rows')).toBeInTheDocument()
    expect(screen.getByText('· 1 cols')).toBeInTheDocument()
    expect(screen.getAllByText('legacy_code')).not.toHaveLength(0)

    expect(await screen.findByText('Input mem://orders · revision lance-v4.')).toBeInTheDocument()
    expect(await screen.findByText(/latest head is orders-dataset@4/i)).toBeInTheDocument()
    expect(screen.getByTestId('dataset-facts-source')).toHaveTextContent('Exact revision orders-dataset@3')
    expect(screen.getByText('3 rows')).toBeInTheDocument()
    expect(screen.getByText('· 1 cols')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('refresh-dataset-facts'))
    expect(await screen.findByText("Couldn't refresh exact head facts: provider offline")).toBeInTheDocument()
    expect(screen.getByTestId('dataset-facts-source')).toHaveTextContent('Exact revision orders-dataset@3')
    expect(screen.getByTestId('dataset-facts-stale')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('refresh-dataset-facts'))
    await waitFor(() => expect(screen.getByTestId('dataset-facts-source')).toHaveTextContent('Exact revision orders-dataset@4'))
    expect(screen.getByText('4 rows')).toBeInTheDocument()
    expect(screen.getByText('· 2 cols')).toBeInTheDocument()
    expect(screen.queryByText('legacy_code')).not.toBeInTheDocument()
    expect(screen.getByText('· verified latest head')).toBeInTheDocument()
    expect(screen.queryByTestId('dataset-facts-stale')).not.toBeInTheDocument()
    expect(mocks.datasetRevision).toHaveBeenNthCalledWith(1, 'orders-dataset', '3')
    expect(mocks.datasetRevision).toHaveBeenNthCalledWith(2, 'orders-dataset', '4')
    expect(mocks.datasetRevision).toHaveBeenNthCalledWith(3, 'orders-dataset', '4')
  })
})

describe('Catalog discovery selection, register modal, and rename', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.tablesPage.mockResolvedValue({ items: [TABLE, TABLE_2], total: 2, hasMore: false })
    mocks.facets.mockResolvedValue(FACETS)
    mocks.catalogTree.mockResolvedValue({ prefix: '', folders: [], tables: [] })
    mocks.searchCatalog.mockResolvedValue([])
    mocks.lineage.mockResolvedValue({ rootUri: TABLE.uri, nodes: [], edges: [] })
    mocks.datasetRevisions.mockRejectedValue(Object.assign(new Error('history absent'), { status: 501 }))
    mocks.datasetRevisionCapabilities.mockResolvedValue({
      selectors: [], asOfOrdering: null, timezone: null, datasetViewSave: false,
    })
    mocks.resolveDatasetRevision.mockRejectedValue(Object.assign(new Error('revision resolution absent'), { status: 501 }))
    mocks.sample.mockResolvedValue({
      columns: TABLE.columns, rows: [{ order_id: 1 }], rowCount: 2,
      hasMore: true, truncated: true, completeness: 'page',
      notPreviewable: false, wire: 'dataset',
    })
    mocks.saveTableEdit.mockResolvedValue(TABLE)
    mocks.unregisterTables.mockResolvedValue({
      mode: 'best_effort', limit: 50,
      results: [
        { id: 't1', status: 'unregistered', detail: null },
        { id: 't2', status: 'unregistered', detail: null },
      ],
    })
    mocks.registerDataset.mockResolvedValue(TABLE)
    mocks.catalogFolders.mockResolvedValue([])
    mocks.createFolder.mockResolvedValue({ path: 'archive' })
    mocks.renameFolder.mockResolvedValue({ ok: true })
    mocks.deleteFolder.mockResolvedValue({ ok: true })
    store.uploadDataset.mockResolvedValue(null)
  })
  afterEach(() => cleanup())

  it('multi-selects rows and batch-unregisters them without implying data deletion', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByLabelText('Select orders'))
    fireEvent.click(screen.getByLabelText('Select customers'))
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Unregister' })).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('catalog-delete-selected'))
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('not underlying data'))
    await waitFor(() => expect(mocks.unregisterTables).toHaveBeenCalledWith([
      { id: 't1', expectedRegistrationId: 'registration-orders', expectedRevision: 'm1_orders' },
      { id: 't2', expectedRegistrationId: 'registration-customers', expectedRevision: 'm1_customers' },
    ]))
    const result = await screen.findByTestId('catalog-unregister-result')
    expect(result).toHaveTextContent('Best-effort unregister result')
    expect(result).toHaveTextContent('orders: unregistered')
    expect(result).toHaveTextContent('customers: unregistered')
  })

  it('registers a dataset through the modal with the full payload', async () => {
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByTestId('register-dataset'))
    fireEvent.change(screen.getByTestId('register-uri'), { target: { value: '/data/events.parquet' } })
    fireEvent.click(screen.getByTestId('register-submit'))
    await waitFor(() => expect(mocks.registerDataset).toHaveBeenCalledWith(
      expect.objectContaining({ uri: '/data/events.parquet' })))
  })

  it('renames a dataset from the detail drawer', async () => {
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByText('orders'))
    openCatalogDetails()
    fireEvent.change(screen.getByTestId('detail-name'), { target: { value: 'daily orders' } })
    fireEvent.click(screen.getByTestId('detail-save'))
    await waitFor(() => expect(mocks.saveTableEdit).toHaveBeenCalledWith('t1',
      expect.objectContaining({ name: 'daily orders' })))
  })

  it('renders an unselected column as an available key action, not a key', async () => {
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByText('orders'))
    openCatalogDetails()

    expect(screen.getByText('No saved key')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Mark order_id as a key' })).toBeVisible()
    expect(screen.queryByTestId('detail-key-state-order_id')).toBeNull()
  })

  it('keeps a one-column key as an explicit saved state and stages changes until Save', async () => {
    const onChanged = vi.fn()
    const saved = { ...TABLE, keys: [{ columns: ['order_id'], confidence: 'declared' as const }] }
    mocks.saveTableEdit.mockResolvedValue(saved)
    render(<CatalogDetail table={TABLE} onClose={vi.fn()} onUse={vi.fn()} onChanged={onChanged} onFolder={vi.fn()}
      onDeleted={vi.fn()} onOpenTable={vi.fn()} onColumn={vi.fn()} />)
    openCatalogDetails()

    fireEvent.click(screen.getByRole('button', { name: 'Mark order_id as a key' }))
    expect(screen.getByTestId('detail-key-state-order_id')).toHaveTextContent('Will be a key on Save')
    expect(onChanged).not.toHaveBeenCalled()
    expect(mocks.saveTableEdit).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('detail-save'))
    await waitFor(() => expect(mocks.saveTableEdit).toHaveBeenCalledWith('t1', expect.objectContaining({ declaredKey: ['order_id'] })))
    expect(screen.getByTestId('detail-key-state-order_id')).toHaveTextContent('Key')
    expect(onChanged).toHaveBeenCalledWith(saved)
  })

  it('labels multiple persisted columns as one composite key', async () => {
    const composite = { ...TABLE, columns: [...TABLE.columns, { name: 'customer_id', type: 'int' }],
      keys: [{ columns: ['order_id', 'customer_id'], confidence: 'declared' as const }] }
    render(<CatalogDetail table={composite} onClose={vi.fn()} onUse={vi.fn()} onChanged={vi.fn()} onFolder={vi.fn()}
      onDeleted={vi.fn()} onOpenTable={vi.fn()} onColumn={vi.fn()} />)
    openCatalogDetails()

    expect(screen.getByText('Saved composite key')).toBeVisible()
    expect(screen.getByTestId('detail-key-state-order_id')).toHaveTextContent('Composite key')
    expect(screen.getByTestId('detail-key-state-customer_id')).toHaveTextContent('Composite key')
  })

  it('stages keys with metadata and offers reload or reapply after a conflict', async () => {
    const conflict = Object.assign(new Error('catalog metadata changed'), { status: 409 })
    mocks.saveTableEdit.mockRejectedValueOnce(conflict).mockResolvedValueOnce({ ...TABLE, name: 'reapplied', metadataRevision: 'm1_new' })
    mocks.table.mockResolvedValue({ ...TABLE, name: 'other editor', metadataRevision: 'm1_other' })
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByText('orders'))
    openCatalogDetails()
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
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByText('orders'))
    openCatalogDetails()
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
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByTestId('folder-new'))
    await waitFor(() => expect(mocks.createFolder).toHaveBeenCalledWith('archive'))
  })

  it('renames a folder from the tree and moves the selected filter with it', async () => {
    mocks.catalogTree.mockResolvedValue({ prefix: '', folders: [{ name: 'sales', path: 'sales', tableCount: 1 }], tables: [] })
    vi.spyOn(window, 'prompt').mockReturnValue('revenue')
    render(<CatalogDiscoveryFixture />)
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
    render(<CatalogDiscoveryFixture />)

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
    render(<CatalogDiscoveryFixture />)
    fireEvent.click(await screen.findByTestId('folder-delete-sales'))
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('the top level'))
    await waitFor(() => expect(mocks.deleteFolder).toHaveBeenCalledWith('sales'))
  })
})

describe('Catalog discovery folder child request identity', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.clearAllMocks()
    store.kernelInfo = { capabilities: ['catalog.folder_mutation', 'catalog.atomic_metadata_edit'] }
    mocks.tablesPage.mockResolvedValue({ items: [], total: 0, hasMore: false })
    mocks.facets.mockResolvedValue({ folders: [], tags: [], owners: [] })
    mocks.searchCatalog.mockResolvedValue([])
    mocks.datasetRevisions.mockRejectedValue(Object.assign(new Error('history absent'), { status: 501 }))
    mocks.resolveDatasetRevision.mockRejectedValue(Object.assign(new Error('revision resolution absent'), { status: 501 }))
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
    render(<CatalogDiscoveryFixture />)

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
    render(<CatalogDiscoveryFixture />)

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
    render(<CatalogDiscoveryFixture />)

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
    render(<CatalogDiscoveryFixture />)

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
    const view = render(<CatalogDiscoveryFixture />)

    fireEvent.click(await screen.findByRole('button', { name: 'Expand folder A' }))
    await waitFor(() => expect(oldSignal).toBeDefined())
    store.kernelInfo = { capabilities: ['catalog.folder_mutation', 'catalog.atomic_metadata_edit'] }
    view.rerender(<CatalogDiscoveryFixture />)

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
        <CatalogDiscoveryFixture />
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
    render(<CatalogDiscoveryFixture />)

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
    render(<CatalogDiscoveryFixture />)

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
    const view = render(<CatalogDiscoveryFixture />)

    fireEvent.click(await screen.findByRole('button', { name: 'Expand folder A' }))
    await waitFor(() => expect(signal).toBeDefined())
    view.unmount()

    expect(signal?.aborted).toBe(true)
    await act(async () => { pending.resolve(tree('A', ['A/stale'])); await pending.promise })
    expect(store.pushToast).not.toHaveBeenCalled()
  })
})
