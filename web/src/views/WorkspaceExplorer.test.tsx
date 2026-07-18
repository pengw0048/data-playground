import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  workspaceBrowse: vi.fn(), workspaceResource: vi.fn(), workspaceSearch: vi.fn(), tableByRegistration: vi.fn(),
  workspaceCreateCanvas: vi.fn(), workspaceAddDatasets: vi.fn(), workspaceMoveCanvas: vi.fn(), workspaceRelink: vi.fn(),
}))
const store = vi.hoisted(() => ({
  workspaceResourceId: null as string | null,
  workspaceSearchQuery: '', setWorkspaceSearchQuery: vi.fn(),
  workspaceScope: 'all' as 'all' | 'datasets', setWorkspaceScope: vi.fn(), switchWorkspaceScope: vi.fn(),
  workspaceDatasetQuery: '', setWorkspaceDatasetQuery: vi.fn(),
  setWorkspaceResource: vi.fn(), openFile: vi.fn(), rememberTables: vi.fn(), pushToast: vi.fn(),
  kernelInfo: { capabilities: ['catalog.folder_mutation', 'catalog.atomic_metadata_edit', 'catalog.cas_unregister'] },
  uploadDataset: vi.fn(),
  files: [] as { id: string; name: string; version: number; role: 'owner' | 'editor' | 'viewer' }[],
  refreshFiles: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mocks }))
vi.mock('../store/graph', () => ({ useStore: (select: (state: typeof store) => unknown) => select(store) }))
vi.mock('./CatalogDiscovery', () => ({
  CATALOG_BATCH_LIMIT: 50,
  emptyCatalogDiscoveryQuery: () => ({ q: '', folder: '', tags: [], owner: '', hasColumns: [], sort: 'name', order: 'asc', match: 'text' }),
  CatalogDiscovery: ({ onUseTables, onQueryStateChange, onSelectedTableChange, selectedRegistrationId }: {
    onUseTables: (tables: { id: string; registrationId: string; name: string; uri: string; columns: never[] }[]) => void
    onQueryStateChange: (query: object) => void
    onSelectedTableChange: (table: { id: string; registrationId: string; name: string; uri: string; columns: never[] } | null) => void
    selectedRegistrationId?: string | null
  }) => <div data-testid="catalog-discovery">
    <span>Selected registration: {selectedRegistrationId ?? 'none'}</span>
    <button onClick={() => onUseTables([
      { id: 't1', registrationId: 'dataset-1', name: 'observations', uri: 'file:///observations.parquet', columns: [] },
      { id: 't2', registrationId: 'dataset-2', name: 'actions', uri: 'file:///actions.parquet', columns: [] },
    ])}>Use selected datasets</button>
    <button onClick={() => onQueryStateChange({ q: 'robot hands', folder: 'robotics', tags: ['gold'], owner: '', hasColumns: ['frame_id'], sort: 'updated', order: 'desc', match: 'meaning' })}>Change dataset query</button>
    <button onClick={() => onSelectedTableChange({ id: 't1', registrationId: 'dataset-1', name: 'observations', uri: 'file:///observations.parquet', columns: [] })}>Open dataset</button>
  </div>,
  CatalogDetail: ({ table, onClose, onUse }: { table: { name: string }; onClose: () => void; onUse: (table: { name: string }) => void }) =>
    <div data-testid="catalog-detail">{table.name}<button onClick={() => onUse(table)}>Use</button><button onClick={onClose}>close detail</button></div>,
}))

import { WorkspaceExplorer } from './WorkspaceExplorer'

const ROOT = { id: 'container:workspace-local-root', kind: 'container' as const, name: 'Workspace', version: 1, detached: false }
const FOLDER = { id: 'container:folder-1', kind: 'container' as const, name: 'Research', parentId: ROOT.id, version: 1, detached: false }
const DATASET = { id: 'dataset:dataset-1', kind: 'dataset' as const, name: 'observations', parentId: FOLDER.id, placementId: 'dataset-placement', version: 1, detached: false }
const CANVAS = { id: 'canvas:canvas-1', kind: 'canvas' as const, name: 'Analysis', parentId: ROOT.id, placementId: 'canvas-placement', version: 3, detached: false }
const EXTERNAL_FOLDER = { id: 'container:external.mount-folder', kind: 'container' as const, name: 'Remote', parentId: ROOT.id, detached: false, source: 'provider' as const, mountId: 'warehouse', provider: 'fixture', resourceId: 'remote-folder' }
const EXTERNAL_DATASET = { id: 'dataset:external.mount-dataset', kind: 'dataset' as const, name: 'observations', parentId: EXTERNAL_FOLDER.id, detached: false, source: 'provider' as const, mountId: 'warehouse', provider: 'fixture', resourceId: 'remote-dataset' }
const PROVIDER_COMPLETE = { id: 'mount:warehouse', kind: 'provider' as const, mountId: 'warehouse', provider: 'fixture', completeness: 'complete' as const, error: null }

describe('WorkspaceExplorer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    store.workspaceResourceId = null
    store.workspaceSearchQuery = ''
    store.workspaceScope = 'all'
    store.workspaceDatasetQuery = ''
    store.files = []
    store.refreshFiles.mockResolvedValue(true)
    store.openFile.mockResolvedValue(true)
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [FOLDER], nextCursor: null, hasMore: false, completeness: 'complete', sources: [{ id: 'local', kind: 'local', completeness: 'complete' }] })
    mocks.workspaceResource.mockResolvedValue({ resource: DATASET, ancestors: [ROOT, FOLDER], source: { id: 'local', kind: 'local', completeness: 'complete' } })
    mocks.workspaceSearch.mockResolvedValue({ query: 'observations', groups: [], nextCursor: null, hasMore: false, completeness: 'complete' })
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

  it('labels same-name local resources by their Catalog or overlay authority', async () => {
    const catalogFolder = { ...FOLDER, id: 'container:catalog-research', catalogFolderId: 'folder-stable-1', catalogFolderPath: 'research' }
    const catalogDataset = { ...DATASET, name: 'Research' }
    const overlayCanvas = { ...CANVAS, name: 'Research' }
    const localContainer = { ...FOLDER, id: 'container:local-research' }
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [catalogFolder, catalogDataset, overlayCanvas, localContainer],
      nextCursor: null, hasMore: false, completeness: 'complete',
    })
    render(<WorkspaceExplorer />)

    expect((await screen.findByRole('button', { name: 'Open catalog folder Research' })).parentElement)
      .toHaveTextContent('Catalog folder · Local Catalog projection')
    expect(screen.getByRole('button', { name: 'Open dataset Research' }).parentElement)
      .toHaveTextContent('Dataset · Local Catalog')
    expect(screen.getByRole('button', { name: 'Open canvas Research' }).parentElement)
      .toHaveTextContent('Canvas · Local overlay')
    expect(screen.getByRole('button', { name: 'Open container Research' }).parentElement)
      .toHaveTextContent('Container · Local container')
  })

  it('shows source-grouped partial search results and opens stable identities', async () => {
    store.workspaceSearchQuery = 'observations'
    mocks.workspaceSearch.mockResolvedValue({
      query: 'observations', completeness: 'partial', hasMore: false, nextCursor: null,
      groups: [
        { source: { id: 'local', kind: 'local', completeness: 'complete', freshness: 'current', searchMode: 'native' }, items: [DATASET] },
        { source: { id: 'mount:warehouse', kind: 'provider', mountId: 'warehouse', provider: 'fixture', completeness: 'unavailable', error: 'deadline exceeded', freshness: 'unknown', searchMode: 'native' }, items: [] },
      ],
    })
    render(<WorkspaceExplorer />)

    expect(await screen.findByText('Partial search results')).toBeVisible()
    expect(screen.getByRole('region', { name: 'Search source Mount warehouse' })).toHaveTextContent('deadline exceeded')
    fireEvent.click(screen.getByRole('button', { name: 'Open dataset observations' }))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(DATASET.id)
    expect(mocks.workspaceSearch).toHaveBeenCalledWith('observations', { limit: 25, cursor: undefined })
  })

  it('keeps completed search pages visible when loading the continuation fails', async () => {
    store.workspaceSearchQuery = 'observations'
    mocks.workspaceSearch.mockResolvedValueOnce({
      query: 'observations', completeness: 'page', hasMore: true, nextCursor: 'next',
      groups: [{
        source: { id: 'local', kind: 'local', completeness: 'page', freshness: 'current', searchMode: 'native' },
        items: [DATASET],
      }],
    }).mockRejectedValueOnce(new Error('network unavailable'))
    render(<WorkspaceExplorer />)

    const result = await screen.findByRole('button', { name: 'Open dataset observations' })
    fireEvent.click(screen.getByRole('button', { name: 'Load more results' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      "Couldn't load more search results: network unavailable",
    )
    expect(result).toBeVisible()
    expect(screen.getByRole('button', { name: 'Retry load more' })).toBeVisible()
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
    expect(screen.getByRole('dialog', { name: 'Use observations' })).toHaveTextContent('observations · dataset:dataset-1')
    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    await waitFor(() => expect(mocks.workspaceCreateCanvas).toHaveBeenCalledWith({
      containerId: 'folder-1', expectedContainerVersion: 1,
      name: 'observations exploration', datasetIds: ['dataset-1'],
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
    mocks.workspaceAddDatasets.mockResolvedValue({ ok: true, id: 'target-canvas', version: 10 })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use' }))
    fireEvent.click(screen.getByRole('button', { name: /^Add to canvas/ }))
    expect(screen.getByLabelText('Target canvas')).toHaveValue('target-canvas')
    expect(screen.queryByRole('option', { name: /Read only/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    await waitFor(() => expect(mocks.workspaceAddDatasets).toHaveBeenCalledWith('target-canvas', {
      datasetIds: ['dataset-1'], expectedCanvasVersion: 9,
    }))
    expect(store.openFile).toHaveBeenCalledWith('target-canvas')
  })

  it('renders the shared bounded Catalog inside the Datasets scope and preserves independent URL state', async () => {
    store.workspaceScope = 'datasets'
    store.workspaceResourceId = 'dataset:dataset-1'
    render(<WorkspaceExplorer />)

    expect(await screen.findByTestId('catalog-discovery')).toHaveTextContent('Selected registration: dataset-1')
    expect(screen.queryByLabelText('Workspace search')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Change dataset query' }))
    expect(store.setWorkspaceDatasetQuery).toHaveBeenCalledWith(
      'dq=robot+hands&folder=robotics&tags=gold&columns=frame_id&sort=updated&order=desc&match=meaning',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Open dataset' }))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith('dataset:dataset-1')
  })

  it('uses a bounded dataset selection atomically in one exact new Canvas destination', async () => {
    store.workspaceScope = 'datasets'
    mocks.workspaceCreateCanvas.mockResolvedValue({ ok: true, id: 'batch-canvas', created: true, resource: CANVAS })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use selected datasets' }))
    const dialog = await screen.findByRole('dialog', { name: 'Use 2 datasets' })
    expect(dialog).toHaveTextContent('Bounded to 50 datasets')
    expect(dialog).toHaveTextContent('applied atomically under one Canvas version precondition')
    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    await waitFor(() => expect(mocks.workspaceCreateCanvas).toHaveBeenCalledWith({
      containerId: 'workspace-local-root', expectedContainerVersion: 1,
      name: '2 datasets exploration', datasetIds: ['dataset-1', 'dataset-2'],
    }))
    expect(store.openFile).toHaveBeenCalledWith('batch-canvas')
    expect(mocks.workspaceBrowse).toHaveBeenCalledWith('workspace-local-root', { limit: 1 })
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

  it('labels duplicate external names by mount and opens the exact stable identity', async () => {
    const duplicate = { ...EXTERNAL_DATASET, id: 'dataset:external.other-dataset', mountId: 'archive', resourceId: 'same-provider-id' }
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [EXTERNAL_DATASET, duplicate], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [
        { id: 'local', kind: 'local', completeness: 'complete' },
        PROVIDER_COMPLETE,
        { ...PROVIDER_COMPLETE, id: 'mount:archive', mountId: 'archive' },
      ],
    })
    render(<WorkspaceExplorer />)

    const archive = await screen.findByRole('button', { name: 'Open dataset observations from Mount archive · fixture' })
    expect(screen.getByRole('button', { name: 'Open dataset observations from Mount warehouse · fixture' })).toBeVisible()
    fireEvent.click(archive)
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(duplicate.id)
  })

  it('keeps local content visible and reports an offline mount as partial', async () => {
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [FOLDER], nextCursor: null, hasMore: false, completeness: 'partial',
      sources: [
        { id: 'local', kind: 'local', completeness: 'complete' },
        { id: 'mount:warehouse', kind: 'provider', mountId: 'warehouse', provider: 'fixture', completeness: 'unavailable', error: 'deadline exceeded' },
      ],
    })
    render(<WorkspaceExplorer />)

    expect(await screen.findByText('Research')).toBeVisible()
    expect(screen.getByRole('region', { name: 'Workspace source status' })).toHaveTextContent('Some sources are incomplete')
    expect(screen.getByRole('region', { name: 'Workspace source status' })).toHaveTextContent('Mount warehouse · fixture · unavailable — deadline exceeded')
  })

  it('opens external dataset detail without catalog lookup or provider writes', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('dialog', { name: 'observations' })
    expect(detail).toHaveTextContent('Read-only mount warehouse · fixture')
    expect(detail).toHaveTextContent('Create, move, delete, and dataset-use actions are unavailable')
    expect(screen.getByRole('button', { name: 'New canvas here' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'New canvas here' })).toHaveAttribute('title', 'Read-only external mounts do not support creating canvases')
    expect(mocks.tableByRegistration).not.toHaveBeenCalled()
    expect(mocks.workspaceCreateCanvas).not.toHaveBeenCalled()
    expect(mocks.workspaceAddDatasets).not.toHaveBeenCalled()
    expect(mocks.workspaceMoveCanvas).not.toHaveBeenCalled()
  })

  it('preserves an external selection and ancestors when its refresh becomes unavailable', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource
      .mockResolvedValueOnce({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE })
      .mockResolvedValueOnce({ resource: EXTERNAL_DATASET, ancestors: [ROOT], source: { ...PROVIDER_COMPLETE, completeness: 'partial', error: 'ancestor read interrupted' } })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    expect(await screen.findByRole('dialog', { name: 'observations' })).toBeVisible()
    expect(screen.getByRole('navigation', { name: 'Workspace path' })).toHaveTextContent('Workspace/Remote')
    fireEvent.click(screen.getByTestId('workspace-reload'))
    expect(await screen.findByRole('alert')).toHaveTextContent('ancestor read interrupted')
    expect(screen.getByRole('dialog', { name: 'observations' })).toBeVisible()
    expect(screen.getByRole('navigation', { name: 'Workspace path' })).toHaveTextContent('Workspace/Remote')
    expect(mocks.workspaceBrowse).toHaveBeenLastCalledWith('external.mount-folder', { limit: 50, cursor: undefined })
  })

  it('allows an initially unavailable external deep link to retry instead of loading forever', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({
      resource: null, ancestors: [],
      source: { ...PROVIDER_COMPLETE, completeness: 'unavailable', error: 'provider offline' },
    })
    render(<WorkspaceExplorer />)

    expect(await screen.findByRole('alert')).toHaveTextContent('provider offline')
    expect(screen.queryByText('Loading Workspace…')).not.toBeInTheDocument()
    expect(screen.getByText('This Workspace location is unavailable.')).toBeVisible()
    const retry = screen.getByRole('button', { name: 'Retry' })
    expect(retry).toBeEnabled()
    fireEvent.click(retry)
    await waitFor(() => expect(mocks.workspaceResource).toHaveBeenCalledTimes(2))
  })

  it('shows last-known external state and relinks only to an explicit provider identity', async () => {
    const stale = { ...EXTERNAL_DATASET, bindingId: 'old-binding', referenceState: 'offline' as const, lastKnown: true, lastResolvedAt: '2026-07-17T00:00:00Z' }
    const fresh = { ...EXTERNAL_DATASET, id: 'dataset:external.fresh-binding', bindingId: 'fresh-binding', referenceState: 'current' as const, lastKnown: false }
    store.workspaceResourceId = stale.id
    mocks.workspaceResource.mockResolvedValue({
      resource: stale, ancestors: [ROOT, EXTERNAL_FOLDER],
      source: { ...PROVIDER_COMPLETE, completeness: 'unavailable', error: 'provider offline', referenceState: 'offline' },
    })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [stale], nextCursor: null, hasMore: false, completeness: 'partial', sources: [{ ...PROVIDER_COMPLETE, completeness: 'unavailable', error: 'provider offline', referenceState: 'offline' }] })
    mocks.workspaceRelink.mockResolvedValue({ ok: true, resource: fresh, previousResource: { ...stale, referenceState: 'detached' } })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('dialog', { name: 'observations' })
    expect(detail).toHaveTextContent('Last-known metadata · offline')
    fireEvent.click(screen.getAllByRole('button', { name: 'Relink' })[0])
    const dialog = screen.getByRole('dialog', { name: 'Relink observations' })
    expect(dialog).toHaveTextContent('Names are never used to repair a binding')
    expect(screen.getByLabelText('Replacement mount ID')).toHaveValue('warehouse')
    expect(screen.getByLabelText('Replacement provider resource ID')).toHaveValue('remote-dataset')
    fireEvent.click(screen.getAllByRole('button', { name: 'Relink' }).at(-1)!)

    await waitFor(() => expect(mocks.workspaceRelink).toHaveBeenCalledWith(stale.id, {
      mountId: 'warehouse', resourceId: 'remote-dataset',
    }))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(fresh.id)
  })
})
