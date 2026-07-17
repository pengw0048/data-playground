import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  workspaceBrowse: vi.fn(), workspaceResource: vi.fn(), tableByRegistration: vi.fn(),
  workspaceCreateCanvas: vi.fn(), workspaceAddDataset: vi.fn(), workspaceMoveCanvas: vi.fn(),
}))
const store = vi.hoisted(() => ({
  workspaceResourceId: null as string | null,
  setWorkspaceResource: vi.fn(), openFile: vi.fn(), rememberTables: vi.fn(), pushToast: vi.fn(),
  files: [] as { id: string; name: string; version: number; role: 'owner' | 'editor' | 'viewer' }[],
  refreshFiles: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mocks }))
vi.mock('../store/graph', () => ({ useStore: (select: (state: typeof store) => unknown) => select(store) }))
vi.mock('./CatalogView', () => ({
  CatalogDetail: ({ table, onClose, onUse }: { table: { name: string }; onClose: () => void; onUse: (table: { name: string }) => void }) =>
    <div data-testid="catalog-detail">{table.name}<button onClick={() => onUse(table)}>Use</button><button onClick={onClose}>close detail</button></div>,
}))

import { WorkspaceExplorer } from './WorkspaceExplorer'

const ROOT = { id: 'container:workspace-local-root', kind: 'container' as const, name: 'Workspace', version: 1, detached: false }
const FOLDER = { id: 'container:folder-1', kind: 'container' as const, name: 'Research', parentId: ROOT.id, version: 1, detached: false }
const DATASET = { id: 'dataset:dataset-1', kind: 'dataset' as const, name: 'observations', parentId: FOLDER.id, placementId: 'dataset-placement', version: 1, detached: false }
const CANVAS = { id: 'canvas:canvas-1', kind: 'canvas' as const, name: 'Analysis', parentId: ROOT.id, placementId: 'canvas-placement', version: 3, detached: false }

describe('WorkspaceExplorer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    store.workspaceResourceId = null
    store.files = []
    store.refreshFiles.mockResolvedValue(true)
    store.openFile.mockResolvedValue(true)
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [FOLDER], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceResource.mockResolvedValue({ resource: DATASET, ancestors: [ROOT, FOLDER] })
    mocks.tableByRegistration.mockResolvedValue({ id: 'dataset-1', name: 'observations', uri: 'file:///observations.parquet', columns: [] })
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

  it('creates a canvas in the exact visible destination', async () => {
    mocks.workspaceCreateCanvas.mockResolvedValue({ ok: true, id: 'created-1', created: true, resource: CANVAS })
    render(<WorkspaceExplorer />)
    fireEvent.click(await screen.findByRole('button', { name: 'New canvas here' }))
    expect(screen.getByRole('dialog', { name: 'New canvas here' })).toHaveTextContent('Destination: Workspace')
    fireEvent.change(screen.getByLabelText('Canvas name'), { target: { value: 'Exact destination' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create canvas' }))
    await waitFor(() => expect(mocks.workspaceCreateCanvas).toHaveBeenCalledWith({
      containerId: 'workspace-local-root', expectedContainerVersion: 1, name: 'Exact destination',
    }))
    expect(store.openFile).toHaveBeenCalledWith('created-1')
  })

  it('explores a stable dataset in a new canvas at its visible container', async () => {
    store.workspaceResourceId = DATASET.id
    mocks.workspaceBrowse.mockResolvedValue({ container: FOLDER, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceCreateCanvas.mockResolvedValue({ ok: true, id: 'explore-1', created: true, resource: CANVAS })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use' }))
    expect(screen.getByRole('dialog', { name: 'Use observations' })).toHaveTextContent('Stable dataset: dataset:dataset-1')
    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    await waitFor(() => expect(mocks.workspaceCreateCanvas).toHaveBeenCalledWith({
      containerId: 'folder-1', expectedContainerVersion: 1,
      name: 'observations exploration', datasetId: 'dataset-1',
    }))
    expect(store.openFile).toHaveBeenCalledWith('explore-1')
  })

  it('adds a stable dataset only to the explicitly selected editable canvas', async () => {
    store.workspaceResourceId = DATASET.id
    store.files = [
      { id: 'viewer-canvas', name: 'Read only', version: 4, role: 'viewer' },
      { id: 'target-canvas', name: 'Exact target', version: 9, role: 'editor' },
    ]
    mocks.workspaceBrowse.mockResolvedValue({ container: FOLDER, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceAddDataset.mockResolvedValue({ ok: true, id: 'target-canvas', version: 10 })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use' }))
    fireEvent.click(screen.getByRole('button', { name: /^Add to canvas/ }))
    expect(screen.getByLabelText('Target canvas')).toHaveValue('target-canvas')
    expect(screen.queryByRole('option', { name: /Read only/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    await waitFor(() => expect(mocks.workspaceAddDataset).toHaveBeenCalledWith('target-canvas', {
      datasetId: 'dataset-1', expectedCanvasVersion: 9,
    }))
    expect(store.openFile).toHaveBeenCalledWith('target-canvas')
  })

  it('confirms a placement-only canvas move and offers a versioned undo', async () => {
    mocks.workspaceBrowse.mockImplementation((containerId: string) => Promise.resolve(containerId === 'folder-1'
      ? { container: FOLDER, items: [], nextCursor: null, hasMore: false, completeness: 'complete' }
      : { container: ROOT, items: [FOLDER, CANVAS], nextCursor: null, hasMore: false, completeness: 'complete' }))
    mocks.workspaceMoveCanvas
      .mockResolvedValueOnce({ ok: true, resource: { ...CANVAS, parentId: FOLDER.id, version: 4 }, previousContainer: ROOT, container: FOLDER })
      .mockResolvedValueOnce({ ok: true, resource: { ...CANVAS, version: 5 }, previousContainer: FOLDER, container: ROOT })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Move canvas Analysis' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Research' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Move to Research' }))
    await waitFor(() => expect(mocks.workspaceMoveCanvas).toHaveBeenNthCalledWith(1, 'canvas-placement', {
      containerId: 'folder-1', expectedContainerVersion: 1, expectedVersion: 3,
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Undo move' }))
    await waitFor(() => expect(mocks.workspaceMoveCanvas).toHaveBeenNthCalledWith(2, 'canvas-placement', {
      containerId: 'workspace-local-root', expectedContainerVersion: 1, expectedVersion: 4,
    }))
  })

  it('keeps an honest error and offers an explicit retry', async () => {
    mocks.workspaceBrowse.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ container: ROOT, items: [], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    expect(await screen.findByRole('alert')).toHaveTextContent('offline')
    fireEvent.click(screen.getByText('Retry'))
    expect(await screen.findByText(/This local container is empty/)).toBeInTheDocument()
  })

  it('does not misreport a transient detail failure as a detached dataset', async () => {
    store.workspaceResourceId = DATASET.id
    mocks.workspaceBrowse.mockResolvedValue({ container: FOLDER, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.tableByRegistration.mockRejectedValueOnce(Object.assign(new Error('service unavailable'), { status: 503 }))
    render(<WorkspaceExplorer />)

    expect(await screen.findByRole('alert')).toHaveTextContent('service unavailable')
    expect(screen.queryByText(/detached/i)).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Retry'))
    expect(await screen.findByTestId('catalog-detail')).toHaveTextContent('observations')
  })

  it('shows a dataset detached when it disappears between resolve and detail fetch', async () => {
    store.workspaceResourceId = DATASET.id
    mocks.workspaceBrowse.mockResolvedValue({ container: FOLDER, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.tableByRegistration.mockRejectedValueOnce(Object.assign(new Error('not found'), { status: 404 }))
    render(<WorkspaceExplorer />)

    expect(await screen.findByRole('dialog', { name: 'observations' })).toHaveTextContent('detached')
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
