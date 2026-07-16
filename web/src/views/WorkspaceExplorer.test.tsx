import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  workspaceBrowse: vi.fn(), workspaceResource: vi.fn(), table: vi.fn(),
}))
const store = vi.hoisted(() => ({
  workspaceResourceId: null as string | null,
  setWorkspaceResource: vi.fn(), openFile: vi.fn(), addToCanvas: vi.fn(),
  rememberTables: vi.fn(), pushToast: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mocks }))
vi.mock('../store/graph', () => ({ useStore: (select: (state: typeof store) => unknown) => select(store) }))
vi.mock('./CatalogView', () => ({
  CatalogDetail: ({ table, onClose }: { table: { name: string }; onClose: () => void }) =>
    <div data-testid="catalog-detail">{table.name}<button onClick={onClose}>close detail</button></div>,
}))

import { WorkspaceExplorer } from './WorkspaceExplorer'

const ROOT = { id: 'container:workspace-local-root', kind: 'container' as const, name: 'Workspace', detached: false }
const FOLDER = { id: 'container:folder-1', kind: 'container' as const, name: 'Research', detached: false }
const DATASET = { id: 'dataset:dataset-1', kind: 'dataset' as const, name: 'observations', parentId: FOLDER.id, detached: false }

describe('WorkspaceExplorer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    store.workspaceResourceId = null
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [FOLDER], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceResource.mockResolvedValue({ resource: DATASET, ancestors: [ROOT, FOLDER] })
    mocks.table.mockResolvedValue({ id: 'dataset-1', name: 'observations', uri: 'file:///observations.parquet', columns: [] })
  })
  afterEach(() => cleanup())

  it('resolves a stable dataset URL into server-provided breadcrumbs and the existing detail surface', async () => {
    store.workspaceResourceId = DATASET.id
    mocks.workspaceBrowse.mockResolvedValue({ container: FOLDER, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    expect(await screen.findByTestId('catalog-detail')).toHaveTextContent('observations')
    expect(screen.getByRole('navigation', { name: 'Workspace path' })).toHaveTextContent('Workspace/Research')
    expect(mocks.workspaceBrowse).toHaveBeenCalledWith('folder-1', { limit: 50, cursor: undefined })
  })

  it('continues a bounded page only when the user requests more', async () => {
    mocks.workspaceBrowse
      .mockResolvedValueOnce({ container: ROOT, items: [FOLDER], nextCursor: 'cursor-2', hasMore: true, completeness: 'page' })
      .mockResolvedValueOnce({ container: ROOT, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByTestId('workspace-load-more'))
    await waitFor(() => expect(mocks.workspaceBrowse).toHaveBeenLastCalledWith('workspace-local-root', { limit: 50, cursor: 'cursor-2' }))
    expect(await screen.findByText('observations')).toBeInTheDocument()
  })

  it('shows future creation and placement entry points without claiming they work', async () => {
    render(<WorkspaceExplorer />)
    expect(await screen.findByRole('button', { name: 'New canvas' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Add dataset' })).toBeDisabled()
  })

  it('keeps an honest error and offers an explicit retry', async () => {
    mocks.workspaceBrowse.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ container: ROOT, items: [], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    expect(await screen.findByRole('alert')).toHaveTextContent('offline')
    fireEvent.click(screen.getByText('Retry'))
    expect(await screen.findByText(/This local container is empty/)).toBeInTheDocument()
  })

  it('keeps the loaded page visible when loading the next page fails', async () => {
    mocks.workspaceBrowse
      .mockResolvedValueOnce({ container: ROOT, items: [FOLDER], nextCursor: 'cursor-2', hasMore: true, completeness: 'page' })
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockResolvedValueOnce({ container: ROOT, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByTestId('workspace-load-more'))
    expect(await screen.findByRole('alert')).toHaveTextContent('temporary failure')
    expect(screen.getByText('Research')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Retry load more'))
    expect(await screen.findByText('observations')).toBeInTheDocument()
  })
})
