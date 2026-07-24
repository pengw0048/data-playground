import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DatasetViewDefinition } from '../types/api'

const mocks = vi.hoisted(() => ({
  workspaceBrowse: vi.fn(), workspaceResource: vi.fn(), workspaceSearch: vi.fn(), tablesPage: vi.fn(), tableByRegistration: vi.fn(),
  workspaceCanonicalDataset: vi.fn(),
  workspaceCreateCanvas: vi.fn(), workspaceCreateFolder: vi.fn(), workspaceRenameFolder: vi.fn(), workspaceDeleteFolder: vi.fn(), workspaceAddDatasets: vi.fn(), workspaceMoveCanvas: vi.fn(), workspaceRelink: vi.fn(),
  getCanvas: vi.fn(), saveCanvas: vi.fn(), deleteCanvas: vi.fn(),
  datasetView: vi.fn(), previewDatasetView: vi.fn(), deleteDatasetView: vi.fn(),
}))
const store = vi.hoisted(() => ({
  workspaceResourceId: null as string | null,
  workspaceSearchQuery: '', setWorkspaceSearchQuery: vi.fn(),
  workspaceScope: 'all' as 'all' | 'datasets', setWorkspaceScope: vi.fn(), switchWorkspaceScope: vi.fn(),
  workspaceDatasetQuery: '', setWorkspaceDatasetQuery: vi.fn(),
  setWorkspaceResource: vi.fn(), openFile: vi.fn(), rememberTables: vi.fn(), pushToast: vi.fn(),
  kernelInfo: { capabilities: ['catalog.folder_mutation', 'catalog.atomic_metadata_edit', 'catalog.cas_unregister'] },
  uploadDataset: vi.fn(),
  doc: { id: '', version: 0 },
  files: [] as { id: string; name: string; version: number; role: 'owner' | 'editor' | 'viewer' }[],
  refreshFiles: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mocks }))
vi.mock('../store/graph', () => ({ useStore: (select: (state: typeof store) => unknown) => select(store) }))
vi.mock('./CatalogDiscovery', () => ({
  CATALOG_BATCH_LIMIT: 50,
  emptyCatalogDiscoveryQuery: () => ({ q: '', folder: '', tags: [], owner: '', hasColumns: [], sort: 'name', order: 'asc', match: 'text' }),
  CatalogDiscovery: ({ onUseTables, onQueryStateChange, onSelectedTableChange, selectedRegistrationId,
    initialRevisionId, initialRevisionDatasetId,
    onOpenInWorkspace, workspaceLocation, onRetryWorkspaceLocation }: {
    onUseTables: (tables: { id: string; registrationId: string; name: string; uri: string; columns: never[] }[]) => void
    onQueryStateChange: (query: object) => void
    onSelectedTableChange: (table: { id: string; registrationId: string; name: string; uri: string; folder?: string; columns: never[] } | null, origin?: 'user' | 'route') => void
    selectedRegistrationId?: string | null
    initialRevisionId?: string
    initialRevisionDatasetId?: string
    onOpenInWorkspace?: (table: { id: string; registrationId: string; name: string; uri: string; folder?: string; columns: never[] }) => void
    workspaceLocation?: { state: 'resolving' | 'available' | 'unavailable'; reason?: string; retryable?: boolean }
    onRetryWorkspaceLocation?: () => void
  }) => <div data-testid="catalog-discovery">
    <span>Selected registration: {selectedRegistrationId ?? 'none'}</span>
    <span>Exact deep link: {initialRevisionDatasetId ?? 'none'}@{initialRevisionId ?? 'none'}</span>
    <button onClick={() => onUseTables([
      { id: 't1', registrationId: 'dataset-1', name: 'observations', uri: 'file:///observations.parquet', columns: [] },
      { id: 't2', registrationId: 'dataset-2', name: 'actions', uri: 'file:///actions.parquet', columns: [] },
    ])}>Use selected datasets</button>
    <button onClick={() => onQueryStateChange({ q: 'robot hands', folder: 'robotics', tags: ['gold'], owner: '', hasColumns: ['frame_id'], sort: 'updated', order: 'desc', match: 'meaning' })}>Change dataset query</button>
    <button onClick={() => onSelectedTableChange({ id: 't1', registrationId: 'dataset-1', name: 'observations', uri: 'file:///observations.parquet', folder: 'robotics', columns: [] })}>Open dataset</button>
    <button onClick={() => onSelectedTableChange(null)}>Close dataset</button>
    <button onClick={() => onSelectedTableChange({ id: 'tbl-receipt', registrationId: 'registration-current', name: 'receipt dataset', uri: 'file:///receipt.parquet', folder: 'robotics', columns: [] }, 'route')}>Open receipt dataset</button>
    <button onClick={() => onSelectedTableChange({ id: 'root-table', registrationId: 'root-dataset', name: 'root observations', uri: 'file:///root.parquet', columns: [] })}>Open root dataset</button>
    {onOpenInWorkspace && <button
      disabled={workspaceLocation?.state !== 'available'}
      title={workspaceLocation?.state === 'resolving' ? 'Resolving this dataset’s Workspace location…' : workspaceLocation?.reason}
      onClick={() => onOpenInWorkspace({ id: 't1', registrationId: 'dataset-1', name: 'observations', uri: 'file:///observations.parquet', folder: 'robotics', columns: [] })}>Open in Workspace</button>}
    {onOpenInWorkspace && <button
      disabled={workspaceLocation?.state !== 'available'}
      onClick={() => onOpenInWorkspace({ id: 'root-table', registrationId: 'root-dataset', name: 'root observations', uri: 'file:///root.parquet', columns: [] })}>Open root in Workspace</button>}
    {workspaceLocation?.state === 'unavailable' && workspaceLocation.retryable
      && <button onClick={onRetryWorkspaceLocation}>Retry</button>}
  </div>,
  CatalogDetail: ({ table, onClose, onUse }: { table: { name: string }; onClose: () => void; onUse: (table: { name: string }) => void }) =>
    <div data-testid="catalog-detail">{table.name}<button onClick={() => onUse(table)}>Use</button><button onClick={onClose}>close detail</button></div>,
}))

import { WorkspaceExplorer } from './WorkspaceExplorer'

const ROOT = { id: 'container:workspace-local-root', kind: 'container' as const, name: 'Workspace', version: 1, detached: false }
const FOLDER = { id: 'container:folder-1', kind: 'container' as const, name: 'Research', parentId: ROOT.id, version: 1, detached: false }
const CATALOG_FOLDER = { ...FOLDER, id: 'container:catalog-robotics', name: 'robotics', catalogFolderId: 'folder-stable-robotics', catalogFolderState: 'current' as const, catalogFolderPath: 'robotics' }
const DATASET = { id: 'dataset:dataset-1', kind: 'dataset' as const, name: 'observations', parentId: FOLDER.id, placementId: 'dataset-placement', version: 1, detached: false }
const CANVAS = { id: 'canvas:canvas-1', kind: 'canvas' as const, name: 'Analysis', parentId: ROOT.id, placementId: 'canvas-placement', version: 3, detached: false }
const DATASET_VIEW = { id: 'dataset_view:view-1', kind: 'dataset_view' as const, name: 'robot interactions', parentId: FOLDER.id, placementId: 'view-placement', version: 1, detached: false }
const VIEW_DEFINITION: DatasetViewDefinition = {
  schemaVersion: 1, id: 'view-1', creatorId: 'local', name: 'robot interactions',
  datasetRef: { kind: 'exact', datasetId: 'dataset-stable', revisionId: 'rev-7', lastKnown: { committedAt: '2026-07-17T12:00:00Z' } },
  placement: { containerId: 'folder-1', placementId: 'view-placement', sourceRegistrationId: 'dataset-1' },
  selectedColumns: ['frame_id'], predicate: null, sampling: { kind: 'all' }, sampleProvenance: null,
  retentionOwner: 'provider', createdAt: '2026-07-18T12:00:00Z', semanticSha256: 'a'.repeat(64), definitionSha256: 'b'.repeat(64),
}
const EXTERNAL_LOCAL_PLACEMENT = { writable: true, canCreateCanvas: true, canMoveCanvas: true, containerId: 'local-overlay-anchor', containerVersion: 7, recoveryState: 'ready' as const }
const EXTERNAL_FOLDER = { id: 'container:external.mount-folder', kind: 'container' as const, name: 'Remote', parentId: ROOT.id, detached: false, source: 'provider' as const, mountId: 'warehouse', provider: 'fixture', resourceId: 'remote-folder', providerPlacementId: 'remote-folder', localPlacement: EXTERNAL_LOCAL_PLACEMENT, providerMutation: false }
const EXTERNAL_DATASET = { id: 'dataset:external.mount-dataset', kind: 'dataset' as const, name: 'observations', parentId: EXTERNAL_FOLDER.id, detached: false, source: 'provider' as const, mountId: 'warehouse', provider: 'fixture', resourceId: 'remote-dataset', providerPlacementId: 'remote-dataset', parentProviderPlacementId: 'remote-folder', providerDatasetId: 'canonical-observations', referenceState: 'current' as const, canonicalReferenceState: 'current' as const }
const PROVIDER_COMPLETE = { id: 'mount:warehouse', kind: 'provider' as const, mountId: 'warehouse', provider: 'fixture', completeness: 'complete' as const, error: null }
const CANONICAL_SOURCE_BINDING = { mountId: 'warehouse', sourceBindingId: 'a'.repeat(32) }
const CANONICAL_DATASET_CONTEXT = {
  ...CANONICAL_SOURCE_BINDING,
  providerDatasetId: 'canonical-observations',
  datasetIdentity: 'workspace-provider:canonical-source',
  readMode: 'exact' as const,
  revisionId: 'revision-7',
  committedAt: '2026-07-23T12:00:00Z',
  columns: [{ name: 'value', type: 'int64', provenance: 'provider' as const, capabilities: [], annotations: [] }],
}

describe('WorkspaceExplorer', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    store.workspaceResourceId = null
    store.workspaceSearchQuery = ''
    store.workspaceScope = 'all'
    store.workspaceDatasetQuery = ''
    store.doc = { id: 'canvas-1', version: 3 }
    store.files = [{ id: 'canvas-1', name: 'Analysis', version: 3, role: 'owner' }]
    store.refreshFiles.mockResolvedValue(true)
    store.openFile.mockResolvedValue(true)
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [FOLDER], nextCursor: null, hasMore: false, completeness: 'complete', sources: [{ id: 'local', kind: 'local', completeness: 'complete' }] })
    mocks.workspaceResource.mockResolvedValue({ resource: DATASET, ancestors: [ROOT, FOLDER], source: { id: 'local', kind: 'local', completeness: 'complete' } })
    mocks.workspaceSearch.mockResolvedValue({ query: 'observations', groups: [], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceCanonicalDataset.mockResolvedValue(CANONICAL_DATASET_CONTEXT)
    mocks.tablesPage.mockResolvedValue({ items: [{ id: 'dataset-1', registrationId: 'dataset-1', name: 'observations', uri: 'file:///observations.parquet', folder: 'robotics', columns: [] }], total: 1, hasMore: false })
    mocks.tableByRegistration.mockResolvedValue({ id: 'dataset-1', name: 'observations', uri: 'file:///observations.parquet', columns: [] })
    mocks.datasetView.mockResolvedValue(VIEW_DEFINITION)
    mocks.previewDatasetView.mockResolvedValue({
      columns: [{ fieldId: 'frame_id', name: 'frame_id', type: 'bigint', nullable: false, provenance: 'provider', capabilities: [] }],
      rows: [{ frame_id: 9 }], rowCount: 1, hasMore: false, rowLimit: 100, sampleProvenance: null,
    })
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

  it('resolves a stable DatasetView URL beside its Catalog source and replays its exact revision', async () => {
    store.workspaceResourceId = DATASET_VIEW.id
    mocks.workspaceResource.mockResolvedValue({
      resource: DATASET_VIEW, ancestors: [ROOT, FOLDER],
      source: { id: 'local', kind: 'local', completeness: 'complete' },
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: FOLDER, items: [DATASET_VIEW], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [{ id: 'local', kind: 'local', completeness: 'complete' }],
    })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('dialog', { name: 'robot interactions' })
    expect(detail).toHaveTextContent('revision:rev-7')
    expect(screen.getByRole('navigation', { name: 'Workspace path' })).toHaveTextContent('Workspace/Research')
    expect(screen.getByRole('button', { name: 'Open datasetview robot interactions' }).parentElement)
      .toHaveTextContent('DatasetView · Local exact view')
    expect(mocks.datasetView).toHaveBeenCalledWith('view-1')
    await waitFor(() => expect(mocks.previewDatasetView).toHaveBeenCalledWith('view-1'))
  })

  it('preserves a receipt logical revision identity while canonicalizing its Workspace registration', async () => {
    store.workspaceScope = 'datasets'
    store.workspaceResourceId = 'dataset:logical-receipt'
    store.workspaceDatasetQuery = 'revision=rev-receipt&revisionDataset=logical-receipt'
    render(<WorkspaceExplorer />)

    expect(screen.getByText('Exact deep link: logical-receipt@rev-receipt')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Open receipt dataset' }))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith('dataset:registration-current')
    expect(store.setWorkspaceDatasetQuery).not.toHaveBeenCalledWith(expect.not.stringContaining('revision=rev-receipt'))
  })

  it('clears both exact revision fields when the user selects another dataset', async () => {
    store.workspaceScope = 'datasets'
    store.workspaceResourceId = 'dataset:registration-current'
    store.workspaceDatasetQuery = 'revision=rev-receipt&revisionDataset=logical-receipt'
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('button', { name: 'Open dataset' }))
    await waitFor(() => expect(store.setWorkspaceDatasetQuery).toHaveBeenCalledWith(''))
  })

  it('clears both exact revision fields when the user closes the exact dataset', async () => {
    store.workspaceScope = 'datasets'
    store.workspaceResourceId = 'dataset:registration-current'
    store.workspaceDatasetQuery = 'revision=rev-receipt&revisionDataset=logical-receipt'
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('button', { name: 'Close dataset' }))
    await waitFor(() => expect(store.setWorkspaceDatasetQuery).toHaveBeenCalledWith(''))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(null)
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

  it('keeps folder names readable while distinguishing Catalog authority without a second hierarchy', async () => {
    const catalogFolder = { ...FOLDER, id: 'container:catalog-research', catalogFolderId: 'folder-stable-1', catalogFolderPath: 'research' }
    const catalogDataset = { ...DATASET, name: 'Research' }
    const overlayCanvas = { ...CANVAS, name: 'Research' }
    const localContainer = { ...FOLDER, id: 'container:local-research' }
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [catalogFolder, catalogDataset, overlayCanvas, localContainer],
      nextCursor: null, hasMore: false, completeness: 'complete',
    })
    render(<WorkspaceExplorer />)

    expect((await screen.findAllByRole('button', { name: 'Open folder Research' }))[0].parentElement)
      .toHaveTextContent('Folder · Catalog organization')
    expect(screen.getByRole('button', { name: 'Open dataset Research' }).parentElement)
      .toHaveTextContent('Dataset · Catalog')
    expect(screen.getByRole('button', { name: 'Open canvas Research' }).parentElement)
      .toHaveTextContent('Canvas · Local')
    expect((await screen.findAllByRole('button', { name: 'Open folder Research' }))[1].parentElement)
      .toHaveTextContent('Folder · Local')
  })

  it('derives one Folder overflow menu from local capabilities and creates with the exact parent CAS token', async () => {
    const localFolder = { ...FOLDER, canCreateFolder: true, canRenameFolder: true, canDeleteFolder: true }
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [localFolder], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceCreateFolder.mockResolvedValue({ ok: true, resource: { ...localFolder, id: 'container:child', name: 'Child' } })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Research' }), { button: 0, ctrlKey: false })
    expect(screen.getByRole('menu', { name: 'More actions for Research' })).toHaveTextContent('OpenNew folderRenameDelete')
    fireEvent.click(screen.getByRole('menuitem', { name: 'New folder' }))
    const dialog = screen.getByRole('dialog', { name: 'New folder' })
    expect(dialog).toHaveTextContent('Parent: Workspace / Research')
    fireEvent.change(screen.getByLabelText('Folder name'), { target: { value: 'Child' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(mocks.workspaceCreateFolder).toHaveBeenCalledWith(expect.objectContaining({
      parentId: 'folder-1', expectedParentVersion: 1, name: 'Child', requestId: expect.any(String),
    })))
  })

  it('keeps non-empty local Folder deletion non-destructive and offers opening the Folder instead', async () => {
    const nonEmpty = { ...FOLDER, canCreateFolder: true, canRenameFolder: true, canDeleteFolder: false,
      folderMutationUnavailableReason: "Move or remove this Folder's contents before deleting it." }
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [nonEmpty], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Research' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete' }))
    const dialog = screen.getByRole('dialog', { name: 'Delete Research' })
    expect(dialog).toHaveTextContent('This folder must be empty before it can be deleted.')
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open folder' }))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(nonEmpty.id)
    expect(mocks.workspaceDeleteFolder).not.toHaveBeenCalled()
  })

  it('does not advertise configured mount deletion, but keeps the explicit detached recovery cleanup action', async () => {
    const mount = { ...FOLDER, id: 'container:mount-point', name: 'Mount point', canCreateFolder: true, canRenameFolder: true,
      canDeleteFolder: false, folderMutationUnavailableReason: 'This Folder is configured as a provider mount point and cannot be deleted.' }
    const cleanupFolder = { ...FOLDER, id: 'container:cleanup-folder', name: 'Recovered local Folder', detached: true,
      canCreateFolder: false, canRenameFolder: false, canDeleteFolder: true,
      folderMutationUnavailableReason: 'This Folder is below a detached Catalog folder; only empty local Folder recovery cleanup is available.' }
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [mount, cleanupFolder], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceDeleteFolder.mockResolvedValue({ ok: true })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Mount point' }), { button: 0, ctrlKey: false })
    expect(screen.queryByRole('menuitem', { name: 'Delete' })).not.toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Recovered local Folder' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(mocks.workspaceDeleteFolder).toHaveBeenCalledWith('cleanup-folder', { expectedVersion: 1 }))
  })

  it('renames and deletes only an owned local Canvas through confirmation dialogs with the Canvas document CAS token', async () => {
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [CANVAS], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.getCanvas.mockResolvedValue({ id: 'canvas-1', name: 'Analysis', version: 17, nodes: [], edges: [] })
    mocks.saveCanvas.mockResolvedValue({ ok: true, id: 'canvas-1', version: 18 })
    mocks.deleteCanvas.mockResolvedValue({ ok: true })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Rename' }))
    fireEvent.change(screen.getByLabelText('Canvas name'), { target: { value: 'Renamed Analysis' } })
    fireEvent.click(screen.getByRole('button', { name: 'Rename' }))
    await waitFor(() => expect(mocks.saveCanvas).toHaveBeenCalledWith({
      id: 'canvas-1', name: 'Renamed Analysis', version: 17, nodes: [], edges: [],
    }, false, 17))

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(mocks.deleteCanvas).not.toHaveBeenCalled()
    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete' }))
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Delete Analysis' })).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(mocks.deleteCanvas).toHaveBeenCalledWith('canvas-1'))
  })

  it('fences a closed Canvas Rename fetch so an old row cannot save or close a newer dialog', async () => {
    const secondCanvas = { ...CANVAS, id: 'canvas:canvas-2', name: 'Second analysis', placementId: 'canvas-placement-2', version: 4 }
    let resolveFirst: ((value: { id: string; name: string; version: number; nodes: never[]; edges: never[] }) => void) | undefined
    store.files = [
      { id: 'canvas-1', name: 'Analysis', version: 3, role: 'owner' },
      { id: 'canvas-2', name: 'Second analysis', version: 4, role: 'owner' },
    ]
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [CANVAS, secondCanvas], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.getCanvas.mockReturnValueOnce(new Promise((resolve) => { resolveFirst = resolve }))
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Rename' }))
    fireEvent.change(screen.getByLabelText('Canvas name'), { target: { value: 'Old rename' } })
    fireEvent.click(screen.getByRole('button', { name: 'Rename' }))
    await waitFor(() => expect(mocks.getCanvas).toHaveBeenCalledWith('canvas-1'))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    fireEvent.pointerDown(screen.getByRole('button', { name: 'More actions for Second analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Rename' }))
    expect(screen.getByRole('dialog', { name: 'Rename Second analysis' })).toBeVisible()
    await act(async () => { resolveFirst?.({ id: 'canvas-1', name: 'Analysis', version: 17, nodes: [], edges: [] }) })

    expect(mocks.saveCanvas).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog', { name: 'Rename Second analysis' })).toBeVisible()
  })

  it('does not expose local Canvas mutations to a viewer', async () => {
    store.files = [{ id: 'canvas-1', name: 'Analysis', version: 3, role: 'viewer' }]
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [CANVAS], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    expect(screen.queryByRole('menuitem', { name: 'Rename' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Move' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Delete' })).not.toBeInTheDocument()
  })

  it('does not expose Canvas mutations for a detached placement', async () => {
    const detachedCanvas = { ...CANVAS, detached: true }
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [detachedCanvas], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    expect(screen.getByRole('menu')).toHaveTextContent('Open')
    expect(screen.queryByRole('menuitem', { name: 'Rename' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Move' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Delete' })).not.toBeInTheDocument()
  })

  it('keeps a source-only provider Folder free of Folder writes while retaining local Canvas creation', async () => {
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [EXTERNAL_FOLDER], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Remote' }), { button: 0, ctrlKey: false })
    expect(screen.getByRole('menu')).toHaveTextContent('Open')
    expect(screen.queryByRole('menuitem', { name: 'New folder' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Rename' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Delete' })).not.toBeInTheDocument()
    expect(screen.getByText('This catalog manages its folders. You can still create a local Canvas here. Folder rename, move, and delete are unavailable here; this does not change the connected catalog.')).toBeVisible()
    expect(mocks.workspaceCreateFolder).not.toHaveBeenCalled()
    expect(mocks.workspaceRenameFolder).not.toHaveBeenCalled()
    expect(mocks.workspaceDeleteFolder).not.toHaveBeenCalled()
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

  it('keeps capability-driven Folder and Canvas actions available from search results without leaving search context', async () => {
    const searchableFolder = { ...FOLDER, canCreateFolder: true, canRenameFolder: true, canDeleteFolder: true }
    const searchableCanvas = { ...CANVAS, parentId: FOLDER.id }
    store.workspaceSearchQuery = 'analysis'
    mocks.workspaceSearch.mockResolvedValue({
      query: 'analysis', completeness: 'complete', hasMore: false, nextCursor: null,
      groups: [{ source: { id: 'local', kind: 'local', completeness: 'complete', freshness: 'current', searchMode: 'native' }, items: [searchableFolder, searchableCanvas] }],
    })
    mocks.workspaceResource.mockImplementation(async (id: string) => id === searchableFolder.id
      ? { resource: searchableFolder, ancestors: [ROOT], source: { id: 'local', kind: 'local', completeness: 'complete' } }
      : { resource: searchableCanvas, ancestors: [ROOT, FOLDER], source: { id: 'local', kind: 'local', completeness: 'complete' } })
    mocks.workspaceRenameFolder.mockResolvedValue({ ok: true, resource: { ...searchableFolder, name: 'Renamed research' } })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Research' }), { button: 0, ctrlKey: false })
    expect(screen.getByRole('menu', { name: 'More actions for Research' })).toHaveTextContent('OpenNew folderRenameDelete')
    fireEvent.click(screen.getByRole('menuitem', { name: 'New folder' }))
    await waitFor(() => expect(mocks.workspaceResource).toHaveBeenCalledWith(searchableFolder.id))
    expect(screen.getByRole('dialog', { name: 'New folder' })).toHaveTextContent('Parent: Workspace / Research')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.pointerDown(screen.getByRole('button', { name: 'More actions for Research' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Rename' }))
    await waitFor(() => expect(mocks.workspaceResource).toHaveBeenCalledWith(searchableFolder.id))
    fireEvent.change(screen.getByLabelText('Folder name'), { target: { value: 'Renamed research' } })
    fireEvent.click(screen.getByRole('button', { name: 'Rename' }))
    await waitFor(() => expect(mocks.workspaceRenameFolder).toHaveBeenCalledWith('folder-1', { expectedVersion: 1, name: 'Renamed research' }))
    expect(screen.getByTestId('workspace-search-results')).toBeVisible()
    expect(store.setWorkspaceResource).not.toHaveBeenCalledWith(searchableFolder.id)

    fireEvent.pointerDown(screen.getByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    expect(screen.getByRole('menu', { name: 'More actions for Analysis' })).toHaveTextContent('OpenRenameMoveDelete')
  })

  it('keeps detached Canvas search results read-only', async () => {
    const detachedCanvas = { ...CANVAS, detached: true }
    store.workspaceSearchQuery = 'analysis'
    mocks.workspaceSearch.mockResolvedValue({
      query: 'analysis', completeness: 'complete', hasMore: false, nextCursor: null,
      groups: [{ source: { id: 'local', kind: 'local', completeness: 'complete', freshness: 'current', searchMode: 'native' }, items: [detachedCanvas] }],
    })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    expect(screen.getByRole('menu')).toHaveTextContent('Open')
    expect(screen.queryByRole('menuitem', { name: 'Rename' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Move' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Delete' })).not.toBeInTheDocument()
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

  it('creates a locally owned Canvas in a source-only provider folder and reuses its request id on retry', async () => {
    store.workspaceResourceId = EXTERNAL_FOLDER.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_FOLDER, ancestors: [ROOT], source: PROVIDER_COMPLETE })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    mocks.workspaceCreateCanvas
      .mockRejectedValueOnce(new Error('connection interrupted after submission'))
      .mockResolvedValueOnce({ ok: true, id: 'external-created', created: true, resource: CANVAS })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'New canvas here' }))
    const dialog = screen.getByRole('dialog', { name: 'New canvas here' })
    expect(dialog).toHaveTextContent('locally owned Canvas overlay')
    expect(dialog).toHaveTextContent('does not change the connected catalog')
    fireEvent.change(screen.getByLabelText('Canvas name'), { target: { value: 'Hand tracking review' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create canvas' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('connection interrupted')
    fireEvent.click(screen.getByRole('button', { name: 'Create canvas' }))

    await waitFor(() => expect(mocks.workspaceCreateCanvas).toHaveBeenCalledTimes(2))
    const [first, second] = mocks.workspaceCreateCanvas.mock.calls.map(([body]) => body)
    expect(first).toMatchObject({
      containerId: 'local-overlay-anchor', expectedContainerVersion: 7,
      name: 'Hand tracking review', requestId: expect.any(String),
    })
    expect(second).toEqual(first)
    expect(store.openFile).toHaveBeenCalledWith('external-created')
  })

  it('resets the external create replay identity when the Canvas intent changes', async () => {
    store.workspaceResourceId = EXTERNAL_FOLDER.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_FOLDER, ancestors: [ROOT], source: PROVIDER_COMPLETE })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    mocks.workspaceCreateCanvas.mockRejectedValue(new Error('retry later'))
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'New canvas here' }))
    fireEvent.change(screen.getByLabelText('Canvas name'), { target: { value: 'first intent' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create canvas' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('retry later')
    fireEvent.change(screen.getByLabelText('Canvas name'), { target: { value: 'second intent' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create canvas' }))

    await waitFor(() => expect(mocks.workspaceCreateCanvas).toHaveBeenCalledTimes(2))
    expect(mocks.workspaceCreateCanvas.mock.calls[1][0]).toMatchObject({ name: 'second intent' })
    expect(mocks.workspaceCreateCanvas.mock.calls[1][0].requestId)
      .not.toBe(mocks.workspaceCreateCanvas.mock.calls[0][0].requestId)
  })

  it('keeps the create action unavailable when an external local overlay cannot be recovered', async () => {
    const unavailable = { ...EXTERNAL_FOLDER, localPlacement: { ...EXTERNAL_LOCAL_PLACEMENT, recoveryState: 'unavailable' as const } }
    store.workspaceResourceId = unavailable.id
    mocks.workspaceResource.mockResolvedValue({ resource: unavailable, ancestors: [ROOT], source: { ...PROVIDER_COMPLETE, completeness: 'partial', error: 'provider offline' } })
    mocks.workspaceBrowse.mockResolvedValue({ container: unavailable, items: [], nextCursor: null, hasMore: false, completeness: 'partial', sources: [{ ...PROVIDER_COMPLETE, completeness: 'unavailable', error: 'provider offline' }] })
    render(<WorkspaceExplorer />)

    const button = await screen.findByRole('button', { name: 'New canvas here' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', 'The local Canvas overlay is unavailable; retry after this source recovers')
    expect(screen.getByText('This source-only provider location is empty.')).toBeVisible()
  })

  it('explains that a detached provider location must be relinked instead of calling it a local tombstone', async () => {
    const detached = { ...EXTERNAL_FOLDER, detached: true, referenceState: 'detached' as const }
    store.workspaceResourceId = detached.id
    mocks.workspaceResource.mockResolvedValue({ resource: detached, ancestors: [ROOT], source: { ...PROVIDER_COMPLETE, completeness: 'unavailable', error: 'resource detached' } })
    mocks.workspaceBrowse.mockResolvedValue({ container: detached, items: [], nextCursor: null, hasMore: false, completeness: 'partial', sources: [{ ...PROVIDER_COMPLETE, completeness: 'unavailable', error: 'resource detached' }] })
    render(<WorkspaceExplorer />)

    const button = await screen.findByRole('button', { name: 'New canvas here' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', 'This source-only provider location is detached; relink or recover it before using its local Canvas overlay')
  })

  it('explores a stable dataset in a new canvas at its visible container', async () => {
    store.workspaceResourceId = DATASET.id
    mocks.workspaceBrowse.mockResolvedValue({ container: FOLDER, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceCreateCanvas.mockResolvedValue({ ok: true, id: 'explore-1', created: true, resource: CANVAS })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use' }))
    expect(screen.getByRole('dialog', { name: 'Use observations' })).toHaveTextContent('observations')
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
    expect(screen.getByRole('button', { name: /^Explore in a new Canvas/ })).toBeVisible()
    expect(screen.getByRole('button', { name: /^Add to this Canvas/ })).toBeVisible()
    await waitFor(() => expect(screen.getByRole('button', { name: /^Choose a Canvas/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /^Choose a Canvas/ }))
    await waitFor(() => expect(screen.getByLabelText('Target canvas')).toHaveValue('target-canvas'))
    expect(screen.queryByRole('option', { name: /Read only/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    await waitFor(() => expect(mocks.workspaceAddDatasets).toHaveBeenCalledWith('target-canvas', expect.objectContaining({
      datasetIds: ['dataset-1'], expectedCanvasVersion: 9, requestId: expect.any(String),
    })))
    expect(store.openFile).toHaveBeenCalledWith('target-canvas')
  })

  it('adds a local dataset to the exact editable current Canvas only after its list refresh completes', async () => {
    store.workspaceResourceId = DATASET.id
    store.doc = { id: 'current-canvas', version: 12 }
    let finishRefresh!: () => void
    store.refreshFiles.mockImplementationOnce(() => new Promise<boolean>((resolve) => {
      finishRefresh = () => {
        store.files = [{ id: 'current-canvas', name: 'Current analysis', version: 12, role: 'editor' }]
        resolve(true)
      }
    }))
    mocks.workspaceBrowse.mockResolvedValue({ container: FOLDER, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceAddDatasets.mockResolvedValue({ ok: true, id: 'current-canvas', version: 13 })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use' }))
    expect(screen.getByRole('button', { name: /^Add to this Canvas/ })).toBeDisabled()
    act(() => finishRefresh())
    await waitFor(() => expect(screen.getByRole('button', { name: /^Add to this Canvas/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /^Add to this Canvas/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    await waitFor(() => expect(mocks.workspaceAddDatasets).toHaveBeenCalledWith('current-canvas', expect.objectContaining({
      datasetIds: ['dataset-1'], expectedCanvasVersion: 12, requestId: expect.any(String),
    })))
  })

  it('fails closed instead of offering stale Canvas targets when the list refresh fails', async () => {
    store.workspaceResourceId = DATASET.id
    store.doc = { id: 'stale-current', version: 4 }
    store.files = [{ id: 'stale-current', name: 'Stale target', version: 4, role: 'owner' }]
    store.refreshFiles.mockResolvedValueOnce(false)
    mocks.workspaceBrowse.mockResolvedValue({ container: FOLDER, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use' }))
    await waitFor(() => expect(store.refreshFiles).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: /^Add to this Canvas/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /^Choose a Canvas/ })).toBeDisabled()
    expect(mocks.workspaceAddDatasets).not.toHaveBeenCalled()
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

  it('translates a nested Datasets folder into its exact opaque Catalog projection', async () => {
    store.workspaceScope = 'datasets'
    store.workspaceDatasetQuery = 'folder=robotics'
    mocks.workspaceResource.mockResolvedValue({
      resource: DATASET, ancestors: [ROOT, CATALOG_FOLDER],
      source: { id: 'local', kind: 'local', completeness: 'complete' },
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('tab', { name: 'All Workspace' }))
    await waitFor(() => expect(mocks.workspaceResource).toHaveBeenCalledWith('dataset:dataset-1'))
    expect(store.switchWorkspaceScope).toHaveBeenCalledWith('all', { resourceId: CATALOG_FOLDER.id })
  })

  it('switches the root Datasets scope back to the Workspace root without inventing a folder identity', async () => {
    store.workspaceScope = 'datasets'
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('tab', { name: 'All Workspace' }))
    expect(store.switchWorkspaceScope).toHaveBeenCalledWith('all')
    expect(mocks.workspaceResource).not.toHaveBeenCalled()
  })

  it('keeps a current projected folder when switching from All Workspace to Datasets', async () => {
    mocks.workspaceBrowse.mockResolvedValue({
      container: CATALOG_FOLDER, items: [], nextCursor: null, hasMore: false, completeness: 'complete',
      sources: [{ id: 'local', kind: 'local', completeness: 'complete' }],
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('tab', { name: 'Datasets' }))
    expect(store.switchWorkspaceScope).toHaveBeenCalledWith('datasets', {
      resourceId: CATALOG_FOLDER.id, datasetQuery: 'folder=robotics',
    })
  })

  it('does not fall back to a same-named local folder when a Catalog folder cannot be resolved', async () => {
    store.workspaceScope = 'datasets'
    store.workspaceDatasetQuery = 'folder=robotics'
    mocks.workspaceResource.mockResolvedValue({
      resource: DATASET, ancestors: [ROOT, { ...FOLDER, name: 'robotics' }],
      source: { id: 'local', kind: 'local', completeness: 'complete' },
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('tab', { name: 'All Workspace' }))
    await waitFor(() => expect(screen.getByRole('tab', { name: 'All Workspace' })).toBeDisabled())
    expect(screen.getByRole('tab', { name: 'All Workspace' })).toHaveAttribute(
      'title', 'This folder is not currently available in Workspace.',
    )
    expect(store.switchWorkspaceScope).not.toHaveBeenCalledWith('all', expect.anything())
  })

  it('offers an explicit retry for a partial folder resolution', async () => {
    store.workspaceScope = 'datasets'
    store.workspaceDatasetQuery = 'folder=robotics'
    mocks.workspaceResource.mockResolvedValueOnce({
      resource: DATASET, ancestors: [ROOT, CATALOG_FOLDER],
      source: { id: 'local', kind: 'local', completeness: 'partial', error: 'catalog temporarily offline' },
    }).mockResolvedValue({
      resource: DATASET, ancestors: [ROOT, CATALOG_FOLDER],
      source: { id: 'local', kind: 'local', completeness: 'complete' },
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('tab', { name: 'All Workspace' }))
    const tab = screen.getByRole('tab', { name: 'All Workspace' })
    await waitFor(() => expect(tab).toBeDisabled())
    expect(tab.getAttribute('title')).toContain('This folder is not currently available in Workspace.')
    fireEvent.click(screen.getByRole('button', { name: 'Retry Workspace location' }))

    await waitFor(() => expect(store.switchWorkspaceScope).toHaveBeenCalledWith('all', {
      resourceId: CATALOG_FOLDER.id,
    }))
  })

  it('opens a dataset detail in the exact resolved Workspace folder', async () => {
    store.workspaceScope = 'datasets'
    mocks.workspaceResource.mockResolvedValue({
      resource: DATASET, ancestors: [ROOT, CATALOG_FOLDER],
      source: { id: 'local', kind: 'local', completeness: 'complete' },
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('button', { name: 'Open dataset' }))
    const open = screen.getByRole('button', { name: 'Open in Workspace' })
    await waitFor(() => expect(open).toBeEnabled())
    fireEvent.click(open)
    await waitFor(() => expect(store.switchWorkspaceScope).toHaveBeenCalledWith('all', { resourceId: CATALOG_FOLDER.id }))
  })

  it('opens a root dataset detail at the exact Workspace root', async () => {
    store.workspaceScope = 'datasets'
    mocks.workspaceResource.mockResolvedValue({
      resource: { ...DATASET, id: 'dataset:root-dataset', parentId: ROOT.id }, ancestors: [ROOT],
      source: { id: 'local', kind: 'local', completeness: 'complete' },
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('button', { name: 'Open root dataset' }))
    const open = screen.getByRole('button', { name: 'Open root in Workspace' })
    await waitFor(() => expect(open).toBeEnabled())
    fireEvent.click(open)

    expect(store.switchWorkspaceScope).toHaveBeenCalledWith('all')
  })

  it('disables an unresolvable dataset location without offering a false retry', async () => {
    store.workspaceScope = 'datasets'
    mocks.workspaceResource.mockResolvedValue({
      resource: DATASET, ancestors: [ROOT, { ...FOLDER, name: 'robotics' }],
      source: { id: 'local', kind: 'local', completeness: 'complete' },
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('button', { name: 'Open dataset' }))
    const open = screen.getByRole('button', { name: 'Open in Workspace' })
    await waitFor(() => expect(open).toBeDisabled())
    expect(open).toHaveAttribute('title', 'This dataset is not currently available in Workspace.')
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(store.switchWorkspaceScope).not.toHaveBeenCalled()
  })

  it('does not borrow another dataset projection when the selected registration is detached', async () => {
    store.workspaceScope = 'datasets'
    mocks.workspaceResource.mockResolvedValue({
      resource: DATASET, ancestors: [ROOT, { ...FOLDER, name: 'robotics' }],
      source: { id: 'local', kind: 'local', completeness: 'complete' },
    })
    mocks.tablesPage.mockResolvedValue({
      items: [{ id: 'other', registrationId: 'other-registration', name: 'other', uri: 'file:///other.parquet', folder: 'robotics', columns: [] }],
      total: 1, hasMore: false,
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('button', { name: 'Open dataset' }))
    const open = screen.getByRole('button', { name: 'Open in Workspace' })
    await waitFor(() => expect(open).toHaveAttribute(
      'title', 'This dataset is not currently available in Workspace.',
    ))
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(mocks.tablesPage).not.toHaveBeenCalled()
  })

  it('does not offer retry for a stable dataset-resolution error', async () => {
    store.workspaceScope = 'datasets'
    mocks.workspaceResource.mockRejectedValue(Object.assign(new Error('dataset missing'), { status: 404 }))
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('button', { name: 'Open dataset' }))
    const open = screen.getByRole('button', { name: 'Open in Workspace' })
    await waitFor(() => expect(open.getAttribute('title')).toContain('dataset missing'))
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('offers retry for a transient dataset-resolution error', async () => {
    store.workspaceScope = 'datasets'
    mocks.workspaceResource.mockRejectedValue(Object.assign(new Error('catalog unavailable'), { status: 503 }))
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('button', { name: 'Open dataset' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeVisible())
  })

  it('retries a partial dataset location and opens only the recovered opaque folder', async () => {
    store.workspaceScope = 'datasets'
    mocks.workspaceResource.mockResolvedValueOnce({
      resource: DATASET, ancestors: [ROOT, CATALOG_FOLDER],
      source: { id: 'local', kind: 'local', completeness: 'partial', error: 'catalog temporarily offline' },
    }).mockResolvedValue({
      resource: DATASET, ancestors: [ROOT, CATALOG_FOLDER],
      source: { id: 'local', kind: 'local', completeness: 'complete' },
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('button', { name: 'Open dataset' }))
    const open = screen.getByRole('button', { name: 'Open in Workspace' })
    await waitFor(() => expect(open).toBeDisabled())
    expect(open.getAttribute('title')).toContain('This dataset is not currently available in Workspace.')
    expect(open.getAttribute('title')).toContain('catalog temporarily offline')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(open).toBeEnabled())
    fireEvent.click(open)

    expect(store.switchWorkspaceScope).toHaveBeenCalledWith('all', { resourceId: CATALOG_FOLDER.id })
  })

  it('ignores an obsolete folder resolution after the Datasets route changes', async () => {
    store.workspaceScope = 'datasets'
    store.workspaceDatasetQuery = 'folder=robotics'
    store.workspaceResourceId = 'dataset:dataset-1'
    let finish!: (value: unknown) => void
    const pending = new Promise<unknown>((resolve) => { finish = resolve })
    mocks.workspaceResource.mockReturnValueOnce(pending)
    const { rerender } = render(<WorkspaceExplorer />)

    fireEvent.click(screen.getByRole('tab', { name: 'All Workspace' }))
    store.workspaceDatasetQuery = 'folder=other'
    store.workspaceResourceId = 'dataset:dataset-2'
    rerender(<WorkspaceExplorer />)
    await act(async () => {
      finish({
        resource: DATASET, ancestors: [ROOT, CATALOG_FOLDER],
        source: { id: 'local', kind: 'local', completeness: 'complete' },
      })
      await pending
    })

    expect(store.switchWorkspaceScope).not.toHaveBeenCalled()
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

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Move' }))
    expect(await screen.findByRole('dialog', { name: 'Move Analysis' })).toHaveTextContent('Current location: Workspace')
    fireEvent.click(await screen.findByRole('button', { name: 'Research' }))
    await waitFor(() => expect(screen.getByText(/Destination:/)).toHaveTextContent('Destination: Workspace / Research'))
    fireEvent.click(await screen.findByRole('button', { name: 'Move to Research' }))
    await waitFor(() => expect(mocks.workspaceMoveCanvas).toHaveBeenNthCalledWith(1, 'canvas-placement', {
      containerId: 'folder-1', expectedContainerVersion: 1, expectedVersion: 3,
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Undo move' }))
    await waitFor(() => expect(mocks.workspaceMoveCanvas).toHaveBeenNthCalledWith(2, 'canvas-placement', {
      containerId: 'workspace-local-root', expectedContainerVersion: 1, expectedVersion: 4,
    }))
  })

  it('moves a Canvas into an external local overlay and uses its local destination again for undo', async () => {
    mocks.workspaceBrowse.mockImplementation((containerId: string) => Promise.resolve(containerId === 'external.mount-folder'
      ? { container: EXTERNAL_FOLDER, items: [], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] }
      : { container: ROOT, items: [EXTERNAL_FOLDER, CANVAS], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] }))
    mocks.workspaceMoveCanvas
      .mockResolvedValueOnce({ ok: true, resource: { ...CANVAS, parentId: EXTERNAL_FOLDER.id, version: 4 }, previousContainer: ROOT, container: EXTERNAL_FOLDER })
      .mockResolvedValueOnce({ ok: true, resource: { ...CANVAS, version: 5 }, previousContainer: EXTERNAL_FOLDER, container: ROOT })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Move' }))
    fireEvent.click(await screen.findByRole('button', { name: /Remote.*local overlay/ }))
    const move = await screen.findByRole('button', { name: 'Move to Remote' })
    expect(screen.getByText(/Destination:/)).toHaveTextContent('locally owned Canvas overlay')
    fireEvent.click(move)
    await waitFor(() => expect(mocks.workspaceMoveCanvas).toHaveBeenNthCalledWith(1, 'canvas-placement', {
      containerId: 'local-overlay-anchor', expectedContainerVersion: 7, expectedVersion: 3,
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Undo move' }))
    await waitFor(() => expect(mocks.workspaceMoveCanvas).toHaveBeenNthCalledWith(2, 'canvas-placement', {
      containerId: 'workspace-local-root', expectedContainerVersion: 1, expectedVersion: 4,
    }))
  })

  it('uses the hidden previous external overlay destination when undoing a move out of it', async () => {
    const overlayCanvas = { ...CANVAS, parentId: EXTERNAL_FOLDER.id }
    store.workspaceResourceId = EXTERNAL_FOLDER.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_FOLDER, ancestors: [ROOT], source: PROVIDER_COMPLETE })
    mocks.workspaceBrowse.mockImplementation((containerId: string) => Promise.resolve(containerId === 'external.mount-folder'
      ? { container: EXTERNAL_FOLDER, items: [overlayCanvas], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] }
      : { container: ROOT, items: [], nextCursor: null, hasMore: false, completeness: 'complete' }))
    mocks.workspaceMoveCanvas
      .mockResolvedValueOnce({ ok: true, resource: { ...overlayCanvas, parentId: ROOT.id, version: 4 }, previousContainer: EXTERNAL_FOLDER, container: ROOT })
      .mockResolvedValueOnce({ ok: true, resource: { ...overlayCanvas, parentId: EXTERNAL_FOLDER.id, version: 5 }, previousContainer: ROOT, container: EXTERNAL_FOLDER })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Move' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Move to Workspace' }))
    await waitFor(() => expect(mocks.workspaceMoveCanvas).toHaveBeenNthCalledWith(1, 'canvas-placement', {
      containerId: 'workspace-local-root', expectedContainerVersion: 1, expectedVersion: 3,
    }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Moved “Analysis”'))
    fireEvent.click(await screen.findByRole('button', { name: 'Undo move' }))
    await waitFor(() => expect(mocks.workspaceMoveCanvas).toHaveBeenNthCalledWith(2, 'canvas-placement', {
      containerId: 'local-overlay-anchor', expectedContainerVersion: 7, expectedVersion: 4,
    }))
  })

  it('disables undo when a previous external local overlay is unavailable', async () => {
    const overlayCanvas = { ...CANVAS, parentId: EXTERNAL_FOLDER.id }
    const unavailable = { ...EXTERNAL_FOLDER, localPlacement: { ...EXTERNAL_LOCAL_PLACEMENT, recoveryState: 'unavailable' as const } }
    store.workspaceResourceId = EXTERNAL_FOLDER.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_FOLDER, ancestors: [ROOT], source: PROVIDER_COMPLETE })
    mocks.workspaceBrowse.mockImplementation((containerId: string) => Promise.resolve(containerId === 'external.mount-folder'
      ? { container: EXTERNAL_FOLDER, items: [overlayCanvas], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] }
      : { container: ROOT, items: [], nextCursor: null, hasMore: false, completeness: 'complete' }))
    mocks.workspaceMoveCanvas.mockResolvedValueOnce({
      ok: true, resource: { ...overlayCanvas, parentId: ROOT.id, version: 4 }, previousContainer: unavailable, container: ROOT,
    })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Move' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Move to Workspace' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Moved “Analysis”'))
    const undo = await screen.findByRole('button', { name: 'Undo unavailable' })
    expect(undo).toBeDisabled()
    expect(undo).toHaveAttribute('title', 'The local Canvas overlay is unavailable; retry after this source recovers')
    expect(screen.getByRole('status')).toHaveTextContent('recover or relink it before undoing')
    expect(mocks.workspaceMoveCanvas).toHaveBeenCalledTimes(1)
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

    const archive = await screen.findByRole('button', { name: 'Open dataset observations from Source-only mount archive · fixture' })
    expect(screen.getByRole('button', { name: 'Open dataset observations from Source-only mount warehouse · fixture' })).toBeVisible()
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

  it('uses an external dataset by stable reference without catalog lookup or provider writes', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    store.files = [{ id: 'target-canvas', name: 'Exact target', version: 9, role: 'editor' }]
    mocks.workspaceAddDatasets.mockResolvedValue({ ok: true, id: 'target-canvas', version: 10 })
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('dialog', { name: 'observations' })
    expect(detail).toHaveTextContent('Source-only mount warehouse · fixture')
    expect(detail).toHaveTextContent('Using the dataset creates only a local Source; it never writes to the provider')
    expect(screen.getByRole('button', { name: 'Create a local Canvas here' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Use in canvas' }))
    expect(screen.getByRole('dialog', { name: 'Use observations' })).toHaveTextContent(
      'Only the stable provider identity and display metadata are stored locally',
    )
    await waitFor(() => expect(screen.getByRole('button', { name: /^Choose a Canvas/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /^Choose a Canvas/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add and open' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    await waitFor(() => expect(mocks.workspaceAddDatasets).toHaveBeenCalledWith('target-canvas', expect.objectContaining({
      providerDatasetRefs: [EXTERNAL_DATASET.id], expectedCanvasVersion: 9, requestId: expect.any(String),
    })))
    expect(store.openFile).toHaveBeenCalledWith('target-canvas')
    expect(mocks.tableByRegistration).not.toHaveBeenCalled()
    expect(mocks.workspaceCreateCanvas).not.toHaveBeenCalled()
    expect(mocks.workspaceMoveCanvas).not.toHaveBeenCalled()
  })

  it('adds a provider reference to the exact editable current Canvas without provider mutation', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    store.doc = { id: 'current-provider-canvas', version: 9 }
    store.files = [{ id: 'current-provider-canvas', name: 'Current provider analysis', version: 9, role: 'owner' }]
    mocks.workspaceAddDatasets.mockResolvedValue({ ok: true, id: 'current-provider-canvas', version: 10 })
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use in canvas' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /^Add to this Canvas/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /^Add to this Canvas/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    await waitFor(() => expect(mocks.workspaceAddDatasets).toHaveBeenCalledWith('current-provider-canvas', expect.objectContaining({
      providerDatasetRefs: [EXTERNAL_DATASET.id], expectedCanvasVersion: 9, requestId: expect.any(String),
    })))
    expect(mocks.workspaceCreateCanvas).not.toHaveBeenCalled()
    expect(mocks.workspaceMoveCanvas).not.toHaveBeenCalled()
    expect(mocks.tableByRegistration).not.toHaveBeenCalled()
  })

  it('reuses the provider add request ID on retry and reports an already-present Source', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    store.doc = { id: 'current-provider-canvas', version: 9 }
    store.files = [{ id: 'current-provider-canvas', name: 'Current provider analysis', version: 9, role: 'owner' }]
    mocks.workspaceAddDatasets
      .mockRejectedValueOnce(new Error('provider temporarily unavailable'))
      .mockResolvedValueOnce({
        ok: true, id: 'current-provider-canvas', version: 9,
        changed: false, alreadyPresent: true, addedCount: 0,
      })
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use in canvas' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /^Add to this Canvas/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /^Add to this Canvas/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    expect(await screen.findByText('provider temporarily unavailable')).toBeVisible()
    const firstPayload = mocks.workspaceAddDatasets.mock.calls[0]?.[1] as {
      requestId: string
    }
    expect(firstPayload.requestId).toEqual(expect.any(String))

    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    await waitFor(() => expect(mocks.workspaceAddDatasets).toHaveBeenCalledTimes(2))
    const secondPayload = mocks.workspaceAddDatasets.mock.calls[1]?.[1] as {
      requestId: string
    }
    expect(secondPayload.requestId).toBe(firstPayload.requestId)
    expect(store.pushToast).toHaveBeenCalledWith(
      'This provider dataset is already present in the selected Canvas.',
      'info',
    )
    expect(store.openFile).toHaveBeenCalledWith('current-provider-canvas')
    expect(mocks.workspaceCreateCanvas).not.toHaveBeenCalled()
    expect(mocks.workspaceMoveCanvas).not.toHaveBeenCalled()
    expect(mocks.tableByRegistration).not.toHaveBeenCalled()
  })


  it('explores a provider dataset in the surrounding external local overlay without mutating the provider', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    mocks.workspaceCreateCanvas.mockResolvedValue({ ok: true, id: 'provider-explore', created: true, resource: CANVAS })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use in canvas' }))
    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    await waitFor(() => expect(mocks.workspaceCreateCanvas).toHaveBeenCalledWith(expect.objectContaining({
      containerId: 'local-overlay-anchor', expectedContainerVersion: 7,
      name: 'observations exploration', providerDatasetRefs: [EXTERNAL_DATASET.id], requestId: expect.any(String),
    })))
    expect(store.openFile).toHaveBeenCalledWith('provider-explore')
    expect(mocks.workspaceAddDatasets).not.toHaveBeenCalled()
    expect(mocks.tableByRegistration).not.toHaveBeenCalled()
  })

  it('disables provider dataset exploration when its external local overlay is unavailable', async () => {
    const unavailable = { ...EXTERNAL_FOLDER, localPlacement: { ...EXTERNAL_LOCAL_PLACEMENT, recoveryState: 'unavailable' as const } }
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_DATASET, ancestors: [ROOT, unavailable], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: unavailable, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use in canvas' }))
    const create = screen.getByRole('button', { name: 'Create and open' })
    expect(create).toBeDisabled()
    expect(create).toHaveAttribute('title', 'The local Canvas overlay is unavailable; retry after this source recovers')
    expect(screen.getByRole('status')).toHaveTextContent('local Canvas overlay is unavailable')
    expect(mocks.workspaceCreateCanvas).not.toHaveBeenCalled()
  })

  it('keeps a placement deep link distinct from its canonical dataset and shows only observed aliases', async () => {
    const alternateFolder = {
      ...EXTERNAL_FOLDER, id: 'container:external.remote-b', name: 'Remote B',
      resourceId: 'remote-folder-b', providerPlacementId: 'remote-folder-b',
    }
    const alternate = {
      ...EXTERNAL_DATASET, id: 'dataset:external.mount-dataset-b', parentId: alternateFolder.id,
      resourceId: 'remote-dataset-b', providerPlacementId: 'remote-dataset-b',
      parentProviderPlacementId: 'remote-folder-b',
    }
    store.workspaceResourceId = alternate.id
    mocks.workspaceResource.mockResolvedValue({ resource: alternate, ancestors: [ROOT, alternateFolder], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: alternateFolder, items: [alternate], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    const view = render(<WorkspaceExplorer />)
    await screen.findByRole('dialog', { name: 'observations' })

    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    view.rerender(<WorkspaceExplorer />)

    const detail = await screen.findByRole('dialog', { name: 'observations' })
    expect(detail).toHaveTextContent('Workspace placementremote-dataset')
    expect(detail).toHaveTextContent('Canonical datasetMountwarehouseDataset IDcanonical-observations')
    expect(within(detail).getByTestId('canonical-provider-dataset-context')).toHaveTextContent(
      'Source dataset identityworkspace-provider:canonical-source',
    )
    expect(within(detail).getByTestId('canonical-provider-dataset-context')).toHaveTextContent(
      'Read modeExact revision · revision-7',
    )
    expect(within(detail).getByTestId('canonical-provider-dataset-context')).toHaveTextContent(
      'Canonical columnsvalue · int64',
    )
    expect(detail).toHaveTextContent('Also observed atRemote B / observations')
    expect(detail).toHaveTextContent('Only placements already loaded in this Workspace session are shown.')
    expect(mocks.workspaceResource).toHaveBeenLastCalledWith(EXTERNAL_DATASET.id)
    expect(mocks.workspaceCanonicalDataset).toHaveBeenCalledWith(
      alternate.id, { signal: expect.any(AbortSignal) },
    )
    expect(mocks.workspaceCanonicalDataset).toHaveBeenCalledWith(
      EXTERNAL_DATASET.id, { signal: expect.any(AbortSignal) },
    )
  })

  it('keeps the full named ancestor chain for a nested provider placement', async () => {
    const top = {
      ...EXTERNAL_FOLDER, id: 'container:external.top', name: 'Top collection',
      resourceId: 'top', providerPlacementId: 'top',
    }
    const nested = {
      ...EXTERNAL_FOLDER, id: 'container:external.nested', name: 'Nested collection', parentId: top.id,
      resourceId: 'nested', providerPlacementId: 'nested', parentProviderPlacementId: 'top',
    }
    const dataset = {
      ...EXTERNAL_DATASET, id: 'dataset:external.nested-dataset', parentId: nested.id,
      resourceId: 'nested-dataset', providerPlacementId: 'nested-dataset', parentProviderPlacementId: 'nested',
    }
    store.workspaceResourceId = dataset.id
    mocks.workspaceResource.mockResolvedValue({ resource: dataset, ancestors: [ROOT, top, nested], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: nested, items: [dataset], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('dialog', { name: 'observations' })
    expect(detail).toHaveTextContent('Top collection / Nested collection / observations')
  })

  it('uses a top-level provider dataset name as its truthful placement path', async () => {
    const topLevel = {
      ...EXTERNAL_DATASET, id: 'dataset:external.top-level', parentId: ROOT.id,
      resourceId: 'top-level', providerPlacementId: 'top-level',
      parentProviderPlacementId: undefined,
    }
    store.workspaceResourceId = topLevel.id
    mocks.workspaceResource.mockResolvedValue({
      resource: topLevel, ancestors: [ROOT], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [topLevel], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('dialog', { name: 'observations' })
    expect(detail).toHaveTextContent('Workspace placementtop-levelobservations')
    expect(detail).not.toHaveTextContent('remote-folder')
  })

  it('labels mutable canonical provider detail as current instead of implying an exact revision', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    mocks.workspaceCanonicalDataset.mockResolvedValue({
      ...CANONICAL_DATASET_CONTEXT,
      readMode: 'current',
      revisionId: null,
      committedAt: null,
    })
    render(<WorkspaceExplorer />)

    const context = await screen.findByTestId('canonical-provider-dataset-context')
    expect(context).toHaveTextContent('Current/latest provider state · not an exact revision')
    expect(context).not.toHaveTextContent('Exact revision')
  })

  it('bounds canonical column rendering while retaining the reported total', async () => {
    const columns = Array.from({ length: 27 }, (_, index) => ({
      name: `column-${index}`, type: 'string', provenance: 'provider' as const,
      capabilities: [], annotations: [],
    }))
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    mocks.workspaceCanonicalDataset.mockResolvedValue({ ...CANONICAL_DATASET_CONTEXT, columns })
    render(<WorkspaceExplorer />)

    const context = await screen.findByTestId('canonical-provider-dataset-context')
    expect(within(context).getByText('column-0')).toBeVisible()
    expect(within(context).getByText('column-24')).toBeVisible()
    expect(within(context).queryByText('column-25')).not.toBeInTheDocument()
    expect(context).toHaveTextContent('2 more columns')
  })

  it('retries canonical provider detail without changing the placement', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    mocks.workspaceCanonicalDataset
      .mockRejectedValueOnce(new Error('canonical detail timed out'))
      .mockResolvedValueOnce(CANONICAL_DATASET_CONTEXT)
    render(<WorkspaceExplorer />)

    expect(await screen.findByText(/Canonical dataset context is unavailable/)).toHaveTextContent(
      'canonical detail timed out',
    )
    expect(screen.getByRole('button', { name: 'Use in canvas' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry canonical detail' }))
    expect(await screen.findByTestId('canonical-provider-dataset-context')).toHaveTextContent(
      'Exact revision · revision-7',
    )
    expect(mocks.workspaceCanonicalDataset).toHaveBeenCalledTimes(2)
    expect(store.workspaceResourceId).toBe(EXTERNAL_DATASET.id)
  })

  it('bounded-resolves fresh same-named provider search occurrences into truthful paths', async () => {
    const alternateFolder = {
      ...EXTERNAL_FOLDER, id: 'container:external.other-folder', name: 'Other Remote',
      resourceId: 'other-folder', providerPlacementId: 'other-folder',
    }
    const alternate = {
      ...EXTERNAL_DATASET, id: 'dataset:external.search-alias', resourceId: 'search-alias',
      providerPlacementId: 'search-alias', parentProviderPlacementId: 'other-folder',
    }
    store.workspaceSearchQuery = 'observations'
    mocks.workspaceSearch.mockResolvedValue({
      query: 'observations', completeness: 'complete', hasMore: false, nextCursor: null,
      groups: [{ source: { ...PROVIDER_COMPLETE, freshness: 'current', searchMode: 'native' }, items: [EXTERNAL_DATASET, alternate] }],
    })
    mocks.workspaceResource.mockImplementation((resourceId: string) => Promise.resolve(
      resourceId === EXTERNAL_DATASET.id
        ? { resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING }
        : { resource: alternate, ancestors: [ROOT, alternateFolder], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING },
    ))
    render(<WorkspaceExplorer />)

    const results = await screen.findByTestId('workspace-search-results')
    expect(within(results).getAllByText('observations', { exact: true })).toHaveLength(2)
    expect(results).toHaveTextContent('Placement path · Remote / observations')
    expect(results).toHaveTextContent('Placement path · Other Remote / observations')
    expect(mocks.workspaceResource).toHaveBeenCalledTimes(2)
    expect(mocks.workspaceResource.mock.calls.every(([, options]) => options.signal instanceof AbortSignal)).toBe(true)
  })

  it('detects same-named provider occurrences across loaded search pages', async () => {
    const alternateFolder = {
      ...EXTERNAL_FOLDER, id: 'container:external.page-two-folder', name: 'Page Two',
      resourceId: 'page-two-folder', providerPlacementId: 'page-two-folder',
    }
    const alternate = {
      ...EXTERNAL_DATASET, id: 'dataset:external.page-two-dataset',
      resourceId: 'page-two-dataset', providerPlacementId: 'page-two-dataset',
      parentProviderPlacementId: 'page-two-folder',
    }
    store.workspaceSearchQuery = 'observations'
    mocks.workspaceSearch
      .mockResolvedValueOnce({
        query: 'observations', completeness: 'page', hasMore: true, nextCursor: 'provider-page-2',
        groups: [{
          source: { ...PROVIDER_COMPLETE, completeness: 'page', freshness: 'current', searchMode: 'native' },
          items: [EXTERNAL_DATASET],
        }],
      })
      .mockResolvedValueOnce({
        query: 'observations', completeness: 'complete', hasMore: false, nextCursor: null,
        groups: [{
          source: { ...PROVIDER_COMPLETE, freshness: 'current', searchMode: 'native' },
          items: [alternate],
        }],
      })
    mocks.workspaceResource.mockImplementation((resourceId: string) => Promise.resolve(
      resourceId === EXTERNAL_DATASET.id
        ? {
            resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER],
            source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
          }
        : {
            resource: alternate, ancestors: [ROOT, alternateFolder],
            source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
          },
    ))
    render(<WorkspaceExplorer />)

    const initial = await screen.findByTestId('workspace-search-results')
    expect(within(initial).getAllByText('observations', { exact: true })).toHaveLength(1)
    expect(mocks.workspaceResource).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Load more results' }))

    await waitFor(() => expect(mocks.workspaceResource).toHaveBeenCalledTimes(2))
    const merged = screen.getByTestId('workspace-search-results')
    expect(within(merged).getAllByText('observations', { exact: true })).toHaveLength(2)
    expect(merged).toHaveTextContent('Placement path · Remote / observations')
    expect(merged).toHaveTextContent('Placement path · Page Two / observations')
  })

  it('caps automatic provider search enrichment at 25 placements per query', async () => {
    const datasets = Array.from({ length: 30 }, (_, index) => ({
      ...EXTERNAL_DATASET,
      id: `dataset:external.search-cap-${index}`,
      resourceId: `search-cap-${index}`,
      providerPlacementId: `search-cap-${index}`,
      parentProviderPlacementId: `search-cap-folder-${index}`,
    }))
    store.workspaceSearchQuery = 'observations'
    mocks.workspaceSearch
      .mockResolvedValueOnce({
        query: 'observations', completeness: 'page', hasMore: true, nextCursor: 'cap-page-2',
        groups: [{
          source: { ...PROVIDER_COMPLETE, completeness: 'page', freshness: 'current', searchMode: 'native' },
          items: datasets.slice(0, 20),
        }],
      })
      .mockResolvedValueOnce({
        query: 'observations', completeness: 'complete', hasMore: false, nextCursor: null,
        groups: [{
          source: { ...PROVIDER_COMPLETE, freshness: 'current', searchMode: 'native' },
          items: datasets.slice(20),
        }],
      })
    mocks.workspaceResource.mockImplementation((resourceId: string) => {
      const index = datasets.findIndex((resource) => resource.id === resourceId)
      const folder = {
        ...EXTERNAL_FOLDER,
        id: `container:external.search-cap-folder-${index}`,
        name: `Search folder ${index}`,
        resourceId: `search-cap-folder-${index}`,
        providerPlacementId: `search-cap-folder-${index}`,
      }
      return Promise.resolve({
        resource: datasets[index], ancestors: [ROOT, folder],
        source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
      })
    })
    render(<WorkspaceExplorer />)

    await screen.findByTestId('workspace-search-results')
    expect(mocks.workspaceResource).toHaveBeenCalledTimes(20)
    fireEvent.click(screen.getByRole('button', { name: 'Load more results' }))
    await waitFor(() => expect(mocks.workspaceSearch).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(mocks.workspaceResource).toHaveBeenCalledTimes(25))
  })

  it('aborts superseded search enrichment without polluting later placement observations', async () => {
    const alternateFolder = {
      ...EXTERNAL_FOLDER, id: 'container:external.superseded-folder', name: 'Superseded Remote',
      resourceId: 'superseded-folder', providerPlacementId: 'superseded-folder',
    }
    const alternate = {
      ...EXTERNAL_DATASET, id: 'dataset:external.superseded-dataset',
      resourceId: 'superseded-dataset', providerPlacementId: 'superseded-dataset',
      parentProviderPlacementId: 'superseded-folder',
    }
    const pending: Array<(value: unknown) => void> = []
    store.workspaceSearchQuery = 'first'
    mocks.workspaceSearch
      .mockResolvedValueOnce({
        query: 'first', completeness: 'complete', hasMore: false, nextCursor: null,
        groups: [{
          source: { ...PROVIDER_COMPLETE, freshness: 'current', searchMode: 'native' },
          items: [EXTERNAL_DATASET, alternate],
        }],
      })
      .mockResolvedValueOnce({
        query: 'second', completeness: 'complete', hasMore: false, nextCursor: null,
        groups: [],
      })
    mocks.workspaceResource.mockImplementation(() => new Promise((resolve) => pending.push(resolve)))
    const view = render(<WorkspaceExplorer />)
    await waitFor(() => expect(mocks.workspaceResource).toHaveBeenCalledTimes(2))

    store.workspaceSearchQuery = 'second'
    view.rerender(<WorkspaceExplorer />)
    expect(await screen.findByTestId('workspace-search-results')).toHaveTextContent('for “second”')

    await act(async () => {
      pending[0]?.({
        resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER],
        source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
      })
      pending[1]?.({
        resource: alternate, ancestors: [ROOT, alternateFolder],
        source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
      })
    })

    store.workspaceSearchQuery = ''
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    view.rerender(<WorkspaceExplorer />)

    const detail = await screen.findByRole('dialog', { name: 'observations' })
    expect(detail).not.toHaveTextContent('Also observed at')
    expect(detail).not.toHaveTextContent('Superseded Remote / observations')
  })

  it('does not invent a search path from opaque provider identities', async () => {
    store.workspaceSearchQuery = 'observations'
    mocks.workspaceSearch.mockResolvedValue({
      query: 'observations', completeness: 'complete', hasMore: false, nextCursor: null,
      groups: [{ source: { ...PROVIDER_COMPLETE, freshness: 'current', searchMode: 'native' }, items: [EXTERNAL_DATASET] }],
    })
    render(<WorkspaceExplorer />)

    const results = await screen.findByTestId('workspace-search-results')
    expect(results).not.toHaveTextContent('Placement path ·')
    expect(results).not.toHaveTextContent('remote-folder')
    expect(mocks.workspaceResource).not.toHaveBeenCalled()
  })

  it('keeps stale search paths visible but excludes them from current alternate placements', async () => {
    const alternateFolder = {
      ...EXTERNAL_FOLDER, id: 'container:external.stale-folder', name: 'Stale Remote',
      resourceId: 'stale-folder', providerPlacementId: 'stale-folder',
    }
    const alternate = {
      ...EXTERNAL_DATASET, id: 'dataset:external.stale-alternate',
      resourceId: 'stale-alternate', providerPlacementId: 'stale-alternate',
      parentProviderPlacementId: 'stale-folder',
    }
    store.workspaceResourceId = alternate.id
    mocks.workspaceResource.mockResolvedValue({
      resource: alternate, ancestors: [ROOT, alternateFolder], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: alternateFolder, items: [alternate], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    const view = render(<WorkspaceExplorer />)
    await screen.findByRole('dialog', { name: 'observations' })

    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    view.rerender(<WorkspaceExplorer />)
    expect(await screen.findByRole('dialog', { name: 'observations' })).toHaveTextContent(
      'Also observed atStale Remote / observations',
    )

    store.workspaceSearchQuery = 'observations'
    mocks.workspaceSearch.mockResolvedValue({
      query: 'observations', completeness: 'complete', hasMore: false, nextCursor: null,
      groups: [{
        source: { ...PROVIDER_COMPLETE, freshness: 'stale', searchMode: 'native' },
        items: [alternate],
      }],
    })
    view.rerender(<WorkspaceExplorer />)
    const results = await screen.findByTestId('workspace-search-results')
    expect(results).toHaveTextContent('Placement path · Stale Remote / observations')

    store.workspaceSearchQuery = ''
    view.rerender(<WorkspaceExplorer />)
    const detail = await screen.findByRole('dialog', { name: 'observations' })
    expect(detail).not.toHaveTextContent('Also observed at')
  })

  it('bounds observed alternate placements within one Workspace session', async () => {
    const entries = Array.from({ length: 7 }, (_, index) => {
      const folder = {
        ...EXTERNAL_FOLDER, id: `container:external.folder-${index}`, name: `Remote ${index}`,
        resourceId: `folder-${index}`, providerPlacementId: `folder-${index}`,
      }
      const dataset = {
        ...EXTERNAL_DATASET, id: `dataset:external.dataset-${index}`, parentId: folder.id,
        resourceId: `dataset-${index}`, providerPlacementId: `dataset-${index}`,
        parentProviderPlacementId: `folder-${index}`,
      }
      return { folder, dataset }
    })
    const byResource = new Map(entries.map(({ folder, dataset }) => [dataset.id, { resource: dataset, ancestors: [ROOT, folder], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING }]))
    mocks.workspaceResource.mockImplementation((resourceId: string) => Promise.resolve(byResource.get(resourceId)))
    mocks.workspaceBrowse.mockImplementation((containerId: string) => {
      const entry = entries.find(({ folder }) => folder.id === `container:${containerId}`)
      return Promise.resolve(entry && { container: entry.folder, items: [entry.dataset], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    })

    store.workspaceResourceId = entries[0].dataset.id
    const view = render(<WorkspaceExplorer />)
    for (const entry of entries) {
      store.workspaceResourceId = entry.dataset.id
      view.rerender(<WorkspaceExplorer />)
      await waitFor(() => expect(mocks.workspaceResource).toHaveBeenLastCalledWith(entry.dataset.id))
    }

    const detail = await screen.findByRole('dialog', { name: 'observations' })
    expect(detail.querySelectorAll('[title^="Remote "]')).toHaveLength(5)
    expect(detail).not.toHaveTextContent('Remote 0 / observations')
    expect(detail).toHaveTextContent('Remote 1 / observations')
  })

  it('separates a detached placement from canonical dataset unavailability', async () => {
    const detachedPlacement = { ...EXTERNAL_DATASET, detached: true, referenceState: 'detached' as const, lastKnown: true }
    store.workspaceResourceId = detachedPlacement.id
    mocks.workspaceResource.mockResolvedValue({ resource: detachedPlacement, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [detachedPlacement], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    const first = render(<WorkspaceExplorer />)
    expect(await screen.findByRole('dialog', { name: 'observations' })).toHaveTextContent('Placement state · detached · canonical dataset is current')
    first.unmount()
    mocks.workspaceResource.mockClear()

    const unavailableCanonical = { ...EXTERNAL_DATASET, canonicalReferenceState: 'offline' as const, lastKnown: true }
    mocks.workspaceResource.mockResolvedValue({ resource: unavailableCanonical, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [unavailableCanonical], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    const second = render(<WorkspaceExplorer />)
    const detail = await screen.findByRole('dialog', { name: 'observations' })
    expect(detail).toHaveTextContent('Canonical dataset state · offline')
    expect(detail).toHaveTextContent('Placement state · current')
    expect(screen.getByRole('button', { name: 'Use in canvas' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry canonical dataset' }))
    await waitFor(() => expect(mocks.workspaceResource).toHaveBeenCalledTimes(2))
    second.unmount()
    mocks.workspaceResource.mockClear()

    const bothUnavailable = {
      ...EXTERNAL_DATASET, referenceState: 'offline' as const,
      canonicalReferenceState: 'offline' as const, lastKnown: true,
    }
    mocks.workspaceResource.mockResolvedValue({
      resource: bothUnavailable, ancestors: [ROOT, EXTERNAL_FOLDER],
      source: { ...PROVIDER_COMPLETE, completeness: 'unavailable', referenceState: 'offline' },
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [bothUnavailable], nextCursor: null, hasMore: false,
      completeness: 'partial',
      sources: [{ ...PROVIDER_COMPLETE, completeness: 'unavailable', referenceState: 'offline' }],
    })
    render(<WorkspaceExplorer />)
    const both = await screen.findByRole('dialog', { name: 'observations' })
    expect(both).toHaveTextContent('Placement state · offline')
    expect(both).toHaveTextContent('Canonical dataset state · offline')
    expect(screen.getByRole('button', { name: 'Use in canvas' })).toBeDisabled()
  })

  it('preserves an external selection and ancestors when its refresh becomes unavailable', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource
      .mockResolvedValueOnce({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
      .mockResolvedValueOnce({ resource: EXTERNAL_DATASET, ancestors: [ROOT], source: { ...PROVIDER_COMPLETE, completeness: 'partial', error: 'ancestor read interrupted' }, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
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
    expect(detail).toHaveTextContent('Placement state · offline')
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
