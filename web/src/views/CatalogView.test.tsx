import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import type { CatalogTable } from '../types/api'

const mocks = vi.hoisted(() => ({
  tablesPage: vi.fn(), facets: vi.fn(), catalogTree: vi.fn(), catalogFolders: vi.fn(), searchCatalog: vi.fn(),
  registerFile: vi.fn(), registerDataset: vi.fn(), lineage: vi.fn(), sample: vi.fn(), table: vi.fn(),
  setTableMetadata: vi.fn(), unregisterTable: vi.fn(), unregisterTables: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks }))

const store = vi.hoisted(() => ({
  addToCanvas: vi.fn(), rememberTables: vi.fn(), uploadDataset: vi.fn(), pushToast: vi.fn(),
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
  columns: [{ name: 'order_id', type: 'int', capabilities: ['key'] }],
}
const TABLE_2: CatalogTable = {
  id: 't2', name: 'customers', uri: 'mem://customers', rowCount: 1, version: 'v1',
  columns: [{ name: 'customer_id', type: 'int', capabilities: ['key'] }],
}
const FACETS = { folders: [{ value: 'sales', count: 1 }], tags: [], owners: [] }

describe('CatalogView request and mutation truth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.tablesPage.mockResolvedValue({ items: [TABLE], total: 1, hasMore: false })
    mocks.facets.mockResolvedValue(FACETS)
    mocks.catalogTree.mockResolvedValue({ prefix: '', folders: [], tables: [] })
    mocks.catalogFolders.mockResolvedValue([])
    mocks.searchCatalog.mockResolvedValue([])
    mocks.lineage.mockResolvedValue({ nodes: [], edges: [] })
    mocks.sample.mockResolvedValue({ columns: TABLE.columns, rows: [{ order_id: 1 }], truncated: false, notPreviewable: false, wire: 'dataset' })
    mocks.table.mockResolvedValue(TABLE)
    mocks.setTableMetadata.mockResolvedValue(TABLE)
    mocks.unregisterTable.mockResolvedValue({ ok: true })
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

  it('surfaces detail failures, preserves edits after a failed save, and refreshes the tree after save and delete', async () => {
    mocks.lineage
      .mockRejectedValueOnce(new Error('HTTP 503: lineage unavailable'))
      .mockResolvedValueOnce({ nodes: [], edges: [] })
    mocks.sample
      .mockRejectedValueOnce(new Error('Failed to fetch'))
      .mockResolvedValueOnce({ columns: TABLE.columns, rows: [{ order_id: 1 }], truncated: false, notPreviewable: false, wire: 'dataset' })
    mocks.setTableMetadata
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
})

describe('CatalogView selection, register modal, and rename', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.tablesPage.mockResolvedValue({ items: [TABLE, TABLE_2], total: 2, hasMore: false })
    mocks.facets.mockResolvedValue(FACETS)
    mocks.catalogTree.mockResolvedValue({ prefix: '', folders: [], tables: [] })
    mocks.catalogFolders.mockResolvedValue([])
    mocks.searchCatalog.mockResolvedValue([])
    mocks.lineage.mockResolvedValue({ nodes: [], edges: [] })
    mocks.setTableMetadata.mockResolvedValue(TABLE)
    mocks.unregisterTables.mockResolvedValue({ deleted: ['t1', 't2'], missing: [] })
    mocks.registerDataset.mockResolvedValue(TABLE)
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
    await waitFor(() => expect(mocks.setTableMetadata).toHaveBeenCalledWith('t1',
      expect.objectContaining({ name: 'daily orders' })))
  })
})
