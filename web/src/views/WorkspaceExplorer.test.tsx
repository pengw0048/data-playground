import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DatasetViewDefinition } from '../types/api'
import { KernelError } from '../api/client'

const mocks = vi.hoisted(() => ({
  workspaceBrowse: vi.fn(), workspaceResource: vi.fn(), workspaceSearch: vi.fn(), tablesPage: vi.fn(), tableByRegistration: vi.fn(),
  workspaceCanonicalDataset: vi.fn(), datasetRevision: vi.fn(), lineage: vi.fn(), table: vi.fn(),
  unregisterTable: vi.fn(),
  workspaceCreateCanvas: vi.fn(), workspaceCreateFolder: vi.fn(), workspaceRenameFolder: vi.fn(), workspaceDeleteFolder: vi.fn(), workspaceAddDatasets: vi.fn(), workspaceMoveCanvas: vi.fn(), workspaceRemoveDetachedDataset: vi.fn(), workspaceBatch: vi.fn(), workspaceRelink: vi.fn(), removeProviderDataset: vi.fn(),
  getCanvas: vi.fn(), saveCanvas: vi.fn(), deleteCanvas: vi.fn(),
  datasetView: vi.fn(), previewDatasetView: vi.fn(), deleteDatasetView: vi.fn(),
}))
const store = vi.hoisted(() => ({
  workspaceResourceId: null as string | null,
  workspaceSearchQuery: '', setWorkspaceSearchQuery: vi.fn(),
  workspaceScope: 'all' as 'all' | 'datasets', setWorkspaceScope: vi.fn(), switchWorkspaceScope: vi.fn(),
  clearWorkspaceDatasetViewerState: vi.fn(),
  returnFromWorkspaceDatasetViewer: vi.fn(),
  workspaceDatasetQuery: '', setWorkspaceDatasetQuery: vi.fn(),
  setWorkspaceResource: vi.fn(), openFile: vi.fn(), select: vi.fn(), activateLoadedCanvasRoute: vi.fn(),
  openRelationships: vi.fn(),
  rememberTables: vi.fn(), pushToast: vi.fn(),
  kernelInfo: { capabilities: ['catalog.folder_mutation', 'catalog.atomic_metadata_edit', 'catalog.cas_unregister'] },
  uploadDataset: vi.fn(),
  firstRunChoice: false,
  localDrafts: [] as never[],
  draftStorageErrors: [] as string[],
  doc: { id: '', version: 0 },
  files: [] as { id: string; name: string; version: number; role: 'owner' | 'editor' | 'viewer' }[],
  refreshFiles: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: mocks,
  KernelError: class KernelError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))
vi.mock('../store/graph', () => ({ useStore: (select: (state: typeof store) => unknown) => select(store) }))
vi.mock('./CatalogDiscovery', () => ({
  CATALOG_BATCH_LIMIT: 50,
  emptyCatalogDiscoveryQuery: () => ({ q: '', folder: '', tags: [], owner: '', hasColumns: [], sort: 'name', order: 'asc', match: 'text' }),
  AddDataModal: ({ onClose }: { onClose: () => void }) => <div role="dialog" aria-label="Add data"><span>Upload a local file</span><span>Register an accessible path or URI</span><button onClick={onClose}>Close</button></div>,
  CatalogDiscovery: ({ title, onUseTables, onQueryStateChange, onSelectedTableChange, selectedRegistrationId,
    initialRevisionId, initialRevisionDatasetId, detailBackLabel,
    onOpenInWorkspace, workspaceLocation, onRetryWorkspaceLocation }: {
    title: string
    onUseTables: (tables: { id: string; registrationId: string; name: string; uri: string; columns: never[] }[]) => void
    onQueryStateChange: (query: object) => void
    onSelectedTableChange: (table: { id: string; registrationId: string; name: string; uri: string; folder?: string; columns: never[] } | null, origin?: 'user' | 'route') => void
    selectedRegistrationId?: string | null
    initialRevisionId?: string
    initialRevisionDatasetId?: string
    detailBackLabel?: string
    onOpenInWorkspace?: (table: { id: string; registrationId: string; name: string; uri: string; folder?: string; columns: never[] }) => void
    workspaceLocation?: { state: 'resolving' | 'available' | 'unavailable'; reason?: string; retryable?: boolean }
    onRetryWorkspaceLocation?: () => void
  }) => <div data-testid="catalog-discovery">
    <span>Catalog title: {title}</span>
    <span>Selected registration: {selectedRegistrationId ?? 'none'}</span>
    <span>Exact deep link: {initialRevisionDatasetId ?? 'none'}@{initialRevisionId ?? 'none'}</span>
    <span>Detail back: {detailBackLabel ?? 'default'}</span>
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
  CatalogDetail: ({ table, onClose, onUse, initialRevisionId, initialRevisionDatasetId, backLabel }: {
    table: { name: string }; onClose: () => void; onUse: (table: { name: string }) => void
    initialRevisionId?: string; initialRevisionDatasetId?: string; backLabel?: string
  }) => <div data-testid="catalog-detail">
    {table.name}
    <span>Exact deep link: {initialRevisionDatasetId ?? 'none'}@{initialRevisionId ?? 'none'}</span>
    <span>Detail back: {backLabel ?? 'default'}</span>
    <button onClick={() => onUse(table)}>Use</button><button onClick={onClose}>Close dataset</button>
  </div>,
}))

import { WorkspaceExplorer, workspaceTimestampLabel } from './WorkspaceExplorer'

const ROOT = { id: 'container:workspace-local-root', kind: 'container' as const, name: 'Workspace', version: 1, detached: false }
const FOLDER = { id: 'container:folder-1', kind: 'container' as const, name: 'Research', parentId: ROOT.id, version: 1, detached: false }
const CATALOG_FOLDER = { ...FOLDER, id: 'container:catalog-robotics', name: 'robotics', catalogFolderId: 'folder-stable-robotics', catalogFolderState: 'current' as const, catalogFolderPath: 'robotics' }
const DATASET = { id: 'dataset:dataset-1', kind: 'dataset' as const, name: 'observations', parentId: FOLDER.id, placementId: 'dataset-placement', version: 1, detached: false }
const CANVAS = { id: 'canvas:canvas-1', kind: 'canvas' as const, name: 'Analysis', parentId: ROOT.id, placementId: 'canvas-placement', version: 3, canvasVersion: 17, detached: false }
const DATASET_VIEW = { id: 'dataset_view:view-1', kind: 'dataset_view' as const, name: 'robot interactions', parentId: FOLDER.id, placementId: 'view-placement', version: 1, detached: false }
const VIEW_DEFINITION: DatasetViewDefinition = {
  schemaVersion: 1, id: 'view-1', creatorId: 'local', name: 'robot interactions',
  datasetRef: { kind: 'exact', datasetId: 'dataset-stable', revisionId: 'rev-7', lastKnown: { committedAt: '2026-07-17T12:00:00Z' } },
  placement: { containerId: 'folder-1', placementId: 'view-placement', sourceRegistrationId: 'dataset-1' },
  selectedColumns: ['frame_id'], predicate: null, sampling: { kind: 'all' }, sampleProvenance: null,
  retentionOwner: 'provider', createdAt: '2026-07-18T12:00:00Z', semanticSha256: 'a'.repeat(64), definitionSha256: 'b'.repeat(64),
}
const EXTERNAL_LOCAL_PLACEMENT = { writable: true, canCreateCanvas: true, canMoveCanvas: true, containerId: 'local-overlay-anchor', containerVersion: 7, recoveryState: 'ready' as const }
const CONNECTED_SOURCE = { id: 'container:mount.d2FyZWhvdXNl', kind: 'container' as const, name: 'warehouse', parentId: ROOT.id, detached: false, source: 'provider' as const, mountId: 'warehouse', provider: 'fixture', resourceId: null, providerPlacementId: null, localPlacement: null }
const EXTERNAL_FOLDER = { id: 'container:external.mount-folder', kind: 'container' as const, name: 'Remote', parentId: ROOT.id, detached: false, source: 'provider' as const, mountId: 'warehouse', provider: 'fixture', resourceId: 'remote-folder', providerPlacementId: 'remote-folder', localPlacement: EXTERNAL_LOCAL_PLACEMENT, providerMutation: false }
const EXTERNAL_DATASET = { id: 'dataset:external.mount-dataset', kind: 'dataset' as const, name: 'observations', parentId: EXTERNAL_FOLDER.id, detached: false, source: 'provider' as const, mountId: 'warehouse', provider: 'fixture', resourceId: 'remote-dataset', providerPlacementId: 'remote-dataset', parentProviderPlacementId: 'remote-folder', providerDatasetId: 'canonical-observations', referenceState: 'current' as const, canonicalReferenceState: 'current' as const }
const PROVIDER_COMPLETE = { id: 'mount:warehouse', kind: 'provider' as const, mountId: 'warehouse', provider: 'fixture', completeness: 'complete' as const, error: null }
const CANONICAL_SOURCE_BINDING = { mountId: 'warehouse', sourceBindingId: 'a'.repeat(32) }
const CANONICAL_DATASET_CONTEXT = {
  ...CANONICAL_SOURCE_BINDING,
  providerDatasetId: 'canonical-observations',
  datasetIdentity: 'workspace-provider:canonical-source',
  sourceUri: 'workspace-provider://canonical-source',
  readMode: 'exact' as const,
  revisionId: 'revision-7',
  committedAt: '2026-07-23T12:00:00Z',
  columns: [{ name: 'value', type: 'int64', provenance: 'provider' as const, capabilities: [], annotations: [] }],
}

describe('workspaceTimestampLabel', () => {
  it('uses compact relative times and handles missing metadata', () => {
    const now = Date.parse('2026-08-01T12:00:00Z')
    expect(workspaceTimestampLabel('2026-08-01T11:58:00Z', now)).toBe('2m ago')
    expect(workspaceTimestampLabel('2026-07-29T12:00:00Z', now)).toBe('3d ago')
    expect(workspaceTimestampLabel(null, now)).toBe('—')
  })
})

describe('WorkspaceExplorer', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    store.workspaceResourceId = null
    store.workspaceSearchQuery = ''
    store.workspaceScope = 'all'
    store.workspaceDatasetQuery = ''
    store.firstRunChoice = false
    store.localDrafts = []
    store.draftStorageErrors = []
    store.doc = { id: 'canvas-1', version: 3 }
    store.files = [{ id: 'canvas-1', name: 'Analysis', version: 3, role: 'owner' }]
    store.refreshFiles.mockResolvedValue(true)
    store.openFile.mockResolvedValue(true)
    store.activateLoadedCanvasRoute.mockImplementation((canvasId: string) => store.doc.id === canvasId)
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [FOLDER], nextCursor: null, hasMore: false, completeness: 'complete', sources: [{ id: 'local', kind: 'local', completeness: 'complete' }] })
    mocks.workspaceResource.mockResolvedValue({ resource: DATASET, ancestors: [ROOT, FOLDER], source: { id: 'local', kind: 'local', completeness: 'complete' } })
    mocks.workspaceSearch.mockResolvedValue({ query: 'observations', groups: [], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceCanonicalDataset.mockResolvedValue(CANONICAL_DATASET_CONTEXT)
    mocks.datasetRevision.mockResolvedValue({
      datasetId: CANONICAL_DATASET_CONTEXT.datasetIdentity, revisionId: 'revision-7',
      summary: { rowCount: 2, dataFileCount: null, totalBytes: null, fragmentCount: null },
      preview: { columns: CANONICAL_DATASET_CONTEXT.columns, rows: [{ value: 1 }, { value: 2 }], hasMore: false, rowLimit: 100 },
    })
    mocks.lineage.mockResolvedValue({
      rootUri: CANONICAL_DATASET_CONTEXT.sourceUri,
      nodes: [{ id: 'canonical-observations', name: 'observations', uri: CANONICAL_DATASET_CONTEXT.sourceUri, kind: 'dataset' }],
      edges: [], truncated: false,
    })
    mocks.table.mockResolvedValue({ id: 'dataset-1', registrationId: 'dataset-1', name: 'events', uri: 'file:///events.parquet', columns: [] })
    mocks.tablesPage.mockResolvedValue({ items: [{ id: 'dataset-1', registrationId: 'dataset-1', name: 'observations', uri: 'file:///observations.parquet', folder: 'robotics', columns: [] }], total: 1, hasMore: false })
    mocks.tableByRegistration.mockResolvedValue({ id: 'dataset-1', name: 'observations', uri: 'file:///observations.parquet', columns: [] })
    mocks.unregisterTable.mockResolvedValue({ ok: true })
    mocks.workspaceRemoveDetachedDataset.mockResolvedValue({ ok: true, placementId: 'dataset-placement' })
    mocks.removeProviderDataset.mockResolvedValue({ ok: true, removedFrom: 'warehouse' })
    mocks.datasetView.mockResolvedValue(VIEW_DEFINITION)
    mocks.previewDatasetView.mockResolvedValue({
      columns: [{ fieldId: 'frame_id', name: 'frame_id', type: 'bigint', nullable: false, provenance: 'provider', capabilities: [] }],
      rows: [{ frame_id: 9 }], rowCount: 1, hasMore: false, rowLimit: 100, sampleProvenance: null,
    })
  })
  afterEach(() => {
    cleanup()
    window.location.hash = ''
  })

  it('replaces All Workspace with the local dataset detail route', async () => {
    store.workspaceResourceId = DATASET.id
    store.firstRunChoice = true
    store.draftStorageErrors = ['local draft warning']
    mocks.workspaceBrowse.mockResolvedValue({ container: FOLDER, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    expect(await screen.findByTestId('catalog-detail')).toHaveTextContent('observations')
    expect(screen.queryByRole('form', { name: 'Workspace search' })).not.toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Workspace path' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open dataset observations' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('first-run-canvas-choice')).not.toBeInTheDocument()
    expect(screen.queryByText('local draft warning')).not.toBeInTheDocument()
    expect(mocks.workspaceBrowse).toHaveBeenCalledWith('folder-1', { limit: 50, cursor: undefined })
  })

  it('exposes both add-data choices directly from All Workspace', async () => {
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByTestId('workspace-add-data'))
    const dialog = screen.getByRole('dialog', { name: 'Add data' })
    expect(dialog).toHaveTextContent('Upload a local file')
    expect(dialog).toHaveTextContent('Register an accessible path or URI')
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
    expect(detail).toHaveTextContent('keeps using the saved version')
    expect(within(detail).queryByText('Diagnostics')).not.toBeInTheDocument()
    expect(within(detail).queryByText('rev-7')).not.toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Workspace path' })).toHaveTextContent('Workspace/Research')
    expect(screen.getByRole('button', { name: 'Open saved view robot interactions' }).parentElement)
      .toHaveTextContent('Saved view')
    expect(mocks.datasetView).toHaveBeenCalledWith('view-1')
    await waitFor(() => expect(mocks.previewDatasetView).toHaveBeenCalledWith('view-1'))
  })

  it('preserves a receipt logical revision identity and route while resolving its current registration', async () => {
    store.workspaceResourceId = DATASET.id
    store.workspaceDatasetQuery = 'revision=rev-receipt&revisionDataset=logical-receipt'
    render(<WorkspaceExplorer />)

    expect(await screen.findByText('Exact deep link: logical-receipt@rev-receipt')).toBeVisible()
    expect(mocks.tableByRegistration).toHaveBeenCalledWith('dataset-1')
    expect(store.setWorkspaceDatasetQuery).not.toHaveBeenCalledWith(expect.not.stringContaining('revision=rev-receipt'))
  })

  it('clears both exact revision fields when the user selects another dataset', async () => {
    store.workspaceDatasetQuery = 'revision=rev-receipt&revisionDataset=logical-receipt'
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete',
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Open dataset observations' }))
    await waitFor(() => expect(store.setWorkspaceDatasetQuery).toHaveBeenCalledWith(''))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(DATASET.id)
  })

  it('clears both exact revision fields when the user closes the exact dataset', async () => {
    store.workspaceResourceId = DATASET.id
    store.workspaceDatasetQuery = 'revision=rev-receipt&revisionDataset=logical-receipt'
    render(<WorkspaceExplorer />)

    expect(await screen.findByText('Detail back: Back to Workspace')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Close dataset' }))
    await waitFor(() => expect(store.switchWorkspaceScope).toHaveBeenCalledWith('all', {
      resourceId: 'container:workspace-local-root', datasetQuery: '',
    }))
  })

  it('returns a Canvas-origin exact viewer to its original selected node', async () => {
    store.workspaceResourceId = DATASET.id
    store.workspaceDatasetQuery = 'revision=rev-receipt&revisionDataset=logical-receipt&returnCanvas=canvas-1&returnNode=write'
    render(<WorkspaceExplorer />)

    expect(await screen.findByText('Detail back: Back to Canvas')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Close dataset' }))

    expect(store.activateLoadedCanvasRoute).toHaveBeenCalledWith('canvas-1', 'write')
    expect(store.clearWorkspaceDatasetViewerState).toHaveBeenCalledWith('')
    expect(store.switchWorkspaceScope).not.toHaveBeenCalled()
    expect(store.openFile).not.toHaveBeenCalled()
    expect(store.setWorkspaceResource).not.toHaveBeenCalledWith(null)
  })

  it.each([
    ['jobs', 'status=failed&run=run-7', 'Back to Jobs'],
    ['inbox', 'filter=unread', 'Back to Inbox'],
  ] as const)('returns an exact viewer to its originating %s context', async (view, returnQuery, label) => {
    store.workspaceResourceId = DATASET.id
    store.workspaceDatasetQuery = new URLSearchParams({
      dq: 'published',
      revision: 'rev-receipt',
      revisionDataset: 'logical-receipt',
      returnView: view,
      returnQuery,
    }).toString()
    render(<WorkspaceExplorer />)

    expect(await screen.findByText(`Detail back: ${label}`)).toBeVisible()
    fireEvent.click(await screen.findByRole('button', { name: 'Close dataset' }))

    expect(store.returnFromWorkspaceDatasetViewer).toHaveBeenCalledWith(view, returnQuery, '')
    expect(store.activateLoadedCanvasRoute).not.toHaveBeenCalled()
    expect(store.setWorkspaceResource).not.toHaveBeenCalledWith(null)
  })

  it('loads the return Canvas only when it is no longer the live in-memory document', async () => {
    store.doc = { id: 'different-canvas', version: 3 }
    store.workspaceResourceId = DATASET.id
    store.workspaceDatasetQuery = 'revision=rev-receipt&revisionDataset=logical-receipt&returnCanvas=canvas-1&returnNode=write'
    render(<WorkspaceExplorer />)

    store.activateLoadedCanvasRoute.mockReturnValue(true)
    fireEvent.click(await screen.findByRole('button', { name: 'Close dataset' }))

    await waitFor(() => expect(store.openFile).toHaveBeenCalledWith('canvas-1', { skipViewportFit: true }))
    expect(store.activateLoadedCanvasRoute).toHaveBeenLastCalledWith('canvas-1', 'write')
    await waitFor(() => expect(store.clearWorkspaceDatasetViewerState).toHaveBeenCalledWith(''))
  })

  it('moves between bounded Workspace pages only when the user asks', async () => {
    mocks.workspaceBrowse
      .mockResolvedValueOnce({ container: ROOT, items: [FOLDER], nextCursor: 'cursor-2', hasMore: true, completeness: 'page' })
      .mockResolvedValueOnce({ container: ROOT, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByTestId('workspace-next-page'))
    await waitFor(() => expect(mocks.workspaceBrowse).toHaveBeenLastCalledWith('workspace-local-root', { limit: 50, cursor: 'cursor-2' }))
    expect(await screen.findByText('observations')).toBeInTheDocument()
    expect(screen.queryByText('Research')).not.toBeInTheDocument()
  })

  it('continues an empty Workspace browse page without presenting it as a final empty location', async () => {
    mocks.workspaceBrowse
      .mockResolvedValueOnce({ container: ROOT, items: [], nextCursor: 'sparse-page-2', hasMore: true, completeness: 'page' })
      .mockResolvedValueOnce({ container: ROOT, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    expect(await screen.findByText('This page has no items. Continue to the next page.')).toBeVisible()
    fireEvent.click(screen.getByTestId('workspace-next-page'))

    expect(await screen.findByText('observations')).toBeVisible()
    expect(mocks.workspaceBrowse).toHaveBeenLastCalledWith('workspace-local-root', {
      limit: 50, cursor: 'sparse-page-2',
    })
  })

  it('keeps degraded provider rows visible while disabling Open and bounded retry actions', async () => {
    const unavailable = {
      ...EXTERNAL_DATASET,
      name: 'cold observations',
      canonicalReferenceState: 'provider_error' as const,
      lastKnown: true,
      unavailableReason: 'Unavailable: Metadata is still indexing',
    }
    const unsupported = {
      ...EXTERNAL_FOLDER,
      id: 'container:external.unsupported-folder',
      name: 'archived folder',
      referenceState: 'provider_error' as const,
      lastKnown: true,
      localPlacement: null,
      unavailableReason: 'Unsupported: Archived folders cannot be browsed',
    }
    const healthy = { ...EXTERNAL_DATASET, id: 'dataset:external.healthy', name: 'healthy observations' }
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT,
      items: [unavailable, unsupported, healthy],
      nextCursor: 'next-page',
      hasMore: true,
      completeness: 'page',
      sources: [{ ...PROVIDER_COMPLETE, completeness: 'page' }],
    })
    render(<WorkspaceExplorer />)

    const unavailableOpen = await screen.findByRole('button', {
      name: 'Open dataset cold observations from Connected source warehouse · fixture',
    })
    const unsupportedOpen = screen.getByRole('button', {
      name: 'Open folder archived folder from Connected source warehouse · fixture',
    })
    expect(unavailableOpen).toBeDisabled()
    expect(unsupportedOpen).toBeDisabled()
    expect(within(unavailableOpen.parentElement!).getByText('Unavailable')).toBeVisible()
    expect(unavailableOpen.parentElement).toHaveTextContent('Metadata is still indexing')
    expect(within(unsupportedOpen.parentElement!).getByText('Unsupported')).toBeVisible()
    expect(unsupportedOpen.parentElement).toHaveTextContent('Archived folders cannot be browsed')
    expect(screen.queryByText('Some Workspace sources are unavailable')).not.toBeInTheDocument()
    expect(screen.getByTestId('workspace-next-page')).toBeEnabled()
    expect(screen.getAllByRole('button', { name: 'Retry' })).toHaveLength(1)

    fireEvent.click(unavailableOpen)
    fireEvent.click(unsupportedOpen)
    expect(store.setWorkspaceResource).not.toHaveBeenCalledWith(unavailable.id)
    expect(store.setWorkspaceResource).not.toHaveBeenCalledWith(unsupported.id)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(mocks.workspaceBrowse).toHaveBeenCalledTimes(2))

    const healthyOpen = screen.getByRole('button', {
      name: 'Open dataset healthy observations from Connected source warehouse · fixture',
    })
    expect(healthyOpen).toBeEnabled()
    fireEvent.click(healthyOpen)
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(healthy.id)
  })

  it('keeps recovery opening available and exposes truthful dataset removal menus', async () => {
    const missing = {
      ...DATASET,
      id: 'dataset:missing-dataset',
      placementId: 'missing-dataset-placement',
      detached: true,
    }
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [DATASET, missing], nextCursor: null, hasMore: false, completeness: 'complete',
    })
    render(<WorkspaceExplorer />)

    const rows = await screen.findAllByRole('button', { name: 'Open dataset observations' })
    expect(rows).toHaveLength(2)
    expect(rows[0].parentElement).not.toHaveTextContent('Unavailable')
    expect(rows[1]).toBeEnabled()
    expect(rows[1].parentElement).toHaveTextContent('Unavailable')
    expect(rows[1].parentElement).toHaveTextContent(
      'The local dataset is no longer available. Open it to view recovery details.',
    )
    expect(rows[1].parentElement).not.toHaveTextContent('detached')
    expect(screen.getAllByRole('button', { name: /More actions for observations/ })).toHaveLength(2)

    fireEvent.click(rows[1])
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(missing.id)
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
      .toHaveTextContent('Folder')
    expect(screen.getByRole('button', { name: 'Open dataset Research' }).parentElement)
      .toHaveTextContent('Dataset')
    expect(screen.getByRole('button', { name: 'Open canvas Research' }).parentElement)
      .toHaveTextContent('Canvas')
    expect((await screen.findAllByRole('button', { name: 'Open folder Research' }))[1].parentElement)
      .toHaveTextContent('Folder')
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

  it('opens item and folder actions from the right-click location', async () => {
    const root = { ...ROOT, canCreateFolder: true }
    const localFolder = { ...FOLDER, canCreateFolder: true, canRenameFolder: true, canDeleteFolder: true }
    mocks.workspaceBrowse.mockResolvedValue({ container: root, items: [localFolder, CANVAS], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByLabelText('Select Analysis'))
    fireEvent.contextMenu(await screen.findByRole('button', { name: 'Open folder Research' }), {
      clientX: 120, clientY: 180,
    })
    const itemMenu = await screen.findByRole('menu', { name: 'Actions for Research' })
    expect(itemMenu).toHaveTextContent('OpenNew folderRenameDelete')
    expect(screen.getByLabelText('Select Research')).toBeChecked()
    expect(screen.getByLabelText('Select Analysis')).not.toBeChecked()
    fireEvent.keyDown(document, { key: 'Escape' })

    fireEvent.contextMenu(screen.getByTestId('workspace-scroll-surface'), { clientX: 700, clientY: 500 })
    const folderMenu = await screen.findByRole('menu', { name: 'Folder actions' })
    for (const name of ['Add data…', 'New folder', 'Create canvas', 'Reload']) {
      expect(within(folderMenu).getByRole('menuitem', { name })).toBeVisible()
    }
  })

  it('removes a local dataset from its right-click menu without claiming to delete the source file', async () => {
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.tableByRegistration.mockResolvedValue({
      id: 'table-1', registrationId: 'dataset-1', metadataRevision: 'metadata-7',
      name: 'observations', uri: 'file:///observations.parquet', columns: [], sourceDeleteAllowed: false,
    })
    render(<WorkspaceExplorer />)

    fireEvent.contextMenu(await screen.findByRole('button', { name: 'Open dataset observations' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Remove dataset…' }))
    const dialog = screen.getByRole('dialog', { name: 'Remove observations' })
    expect(dialog).toHaveTextContent('Keep the source file so it can be registered again.')
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Remove dataset' }))

    await waitFor(() => expect(mocks.unregisterTable).toHaveBeenCalledWith(
      'table-1', 'dataset-1', 'metadata-7',
    ))
    expect(store.pushToast).toHaveBeenCalledWith('Dataset removed from Workspace', 'success')
  })

  it('offers source-file deletion only when the server authorizes that exact local registration', async () => {
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.tableByRegistration.mockResolvedValue({
      id: 'table-1', registrationId: 'dataset-1', metadataRevision: 'metadata-7',
      name: 'observations', uri: 'file:///data/observations.parquet', columns: [], sourceDeleteAllowed: true,
    })
    mocks.unregisterTable.mockResolvedValue({ ok: true, sourceDeleted: true })
    render(<WorkspaceExplorer />)

    fireEvent.contextMenu(await screen.findByRole('button', { name: 'Open dataset observations' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Remove dataset…' }))
    const dialog = screen.getByRole('dialog', { name: 'Remove observations' })
    const deleteOption = await within(dialog).findByRole('radio', { name: /Delete the source file too/ })
    expect(dialog).toHaveTextContent('/data/observations.parquet')
    fireEvent.click(deleteOption)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete file and remove' }))

    await waitFor(() => expect(mocks.unregisterTable).toHaveBeenCalledWith(
      'table-1', 'dataset-1', 'metadata-7', true,
    ))
  })

  it('removes a provider dataset through an explicitly writable connected source', async () => {
    const removable = { ...EXTERNAL_DATASET, providerMutation: true }
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [removable], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    store.workspaceResourceId = EXTERNAL_FOLDER.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_FOLDER, ancestors: [ROOT], source: PROVIDER_COMPLETE })
    render(<WorkspaceExplorer />)

    fireEvent.contextMenu(await screen.findByRole('button', { name: /Open dataset observations from Connected source/ }))
    expect(screen.queryByRole('menuitem', { name: /Remove unavailable/ })).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Remove dataset…' }))
    const dialog = screen.getByRole('dialog', { name: 'Remove observations from warehouse' })
    expect(dialog).toHaveTextContent('underlying data stays in its storage')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove from source' }))

    await waitFor(() => expect(mocks.removeProviderDataset).toHaveBeenCalledWith(removable.id))
    expect(store.pushToast).toHaveBeenCalledWith('Dataset removed from its connected source', 'success')
  })

  it('explains when a connected source does not expose dataset removal', async () => {
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    store.workspaceResourceId = EXTERNAL_FOLDER.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_FOLDER, ancestors: [ROOT], source: PROVIDER_COMPLETE })
    render(<WorkspaceExplorer />)

    fireEvent.contextMenu(await screen.findByRole('button', { name: /Open dataset observations from Connected source/ }))
    const unavailable = await screen.findByRole('menuitem', { name: /Remove unavailable/ })
    expect(unavailable).toHaveAttribute('data-disabled')
    expect(unavailable).toHaveTextContent('warehouse did not expose dataset removal')
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
    mocks.workspaceBatch.mockResolvedValue({ ok: true, action: 'delete_canvases', items: [], deletedCanvasIds: ['canvas-1'] })
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
    expect(screen.getByRole('dialog', { name: 'Delete Analysis' })).toHaveTextContent(
      'This permanently deletes its version history, run and Job history, Inbox outcomes, and saved intermediate results.')
    expect(screen.getByRole('dialog', { name: 'Delete Analysis' })).toHaveTextContent(
      'Published or managed datasets remain available.')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(mocks.workspaceBatch).not.toHaveBeenCalled()
    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete' }))
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Delete Analysis' })).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(mocks.workspaceBatch).toHaveBeenCalledWith({
      action: 'delete_canvases',
      items: [{ placementId: 'canvas-placement', expectedVersion: 3, expectedCanvasVersion: 17 }],
    }))
  })

  it('fences a closed Canvas Rename fetch so an old row cannot save or close a newer dialog', async () => {
    const secondCanvas = { ...CANVAS, id: 'canvas:canvas-2', name: 'Second analysis', placementId: 'canvas-placement-2', version: 4, canvasVersion: 23 }
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

    expect(await screen.findByRole('button', { name: 'Open canvas Analysis' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'More actions for Analysis' })).not.toBeInTheDocument()
  })

  it('defaults to a compact list and exposes selected Canvas actions in grid view', async () => {
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [CANVAS], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.getCanvas.mockResolvedValue({ id: 'canvas-1', name: 'Analysis', version: 3, nodes: [], edges: [] })
    render(<WorkspaceExplorer />)

    const views = await screen.findByRole('group', { name: 'Workspace view' })
    expect(within(views).getByRole('button', { name: 'list' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('Last modified')).toBeVisible()
    expect(screen.getByText('Opened here')).toBeVisible()
    fireEvent.click(within(views).getByRole('button', { name: 'grid' }))
    expect(within(views).getByRole('button', { name: 'grid' })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByLabelText('Select Analysis'))
    expect(screen.getByText('1 selected')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Duplicate' }))
    expect(await screen.findByRole('dialog', { name: 'Duplicate canvas' })).toBeVisible()
    expect(mocks.getCanvas).toHaveBeenCalledWith('canvas-1')
  })

  it('requests server-wide sorting and real resource type filtering', async () => {
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [FOLDER, CANVAS], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [{ id: 'local', kind: 'local', completeness: 'complete' }],
    })
    render(<WorkspaceExplorer />)

    const sort = await screen.findByRole('combobox', { name: 'Sort Workspace' })
    mocks.workspaceBrowse.mockClear()
    fireEvent.change(sort, { target: { value: 'name-desc' } })
    await waitFor(() => expect(mocks.workspaceBrowse).toHaveBeenCalledWith(
      'workspace-local-root',
      { limit: 50, cursor: undefined, sort: 'name', order: 'desc' },
    ))

    fireEvent.change(screen.getByRole('combobox', { name: 'Filter Workspace by type' }), {
      target: { value: 'canvas' },
    })
    await waitFor(() => expect(mocks.workspaceBrowse).toHaveBeenCalledWith(
      'workspace-local-root',
      { limit: 50, cursor: undefined, sort: 'name', order: 'desc', kinds: ['canvas'] },
    ))
  })

  it('hides unsupported query controls before browsing a connected source', async () => {
    const providerRoot = {
      ...EXTERNAL_FOLDER,
      id: 'container:mount.bHVtYS1zdGFnaW5n',
      name: 'luma-staging',
      mountId: 'luma-staging',
      resourceId: null,
      bindingId: null,
      localPlacement: null,
    }
    store.workspaceResourceId = providerRoot.id
    mocks.workspaceResource.mockResolvedValue({
      resource: providerRoot,
      ancestors: [ROOT],
      source: { ...PROVIDER_COMPLETE, id: 'mount:luma-staging', mountId: 'luma-staging' },
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: providerRoot, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE], connectedSources: [],
      queryCapabilities: {
        sort: [], kindFilter: false,
        reason: "Sorting and type filters aren't available for this source.",
      },
    })
    render(<WorkspaceExplorer />)

    expect(await screen.findByRole('button', {
      name: 'Open dataset observations from Connected source warehouse · fixture',
    })).toBeVisible()
    expect(screen.queryByRole('combobox', { name: 'Sort Workspace' })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Filter Workspace by type' })).not.toBeInTheDocument()
    expect(screen.queryByText("Sorting and type filters aren't available for this source.")).not.toBeInTheDocument()
    expect(mocks.workspaceBrowse).toHaveBeenCalledWith(
      'mount.bHVtYS1zdGFnaW5n', { limit: 50, cursor: undefined },
    )
  })

  it('mixes connected-source roots beside local items without a second Workspace section', async () => {
    const providerRoot = {
      ...EXTERNAL_FOLDER,
      id: 'container:mount.bHVtYS1zdGFnaW5n',
      name: 'luma-staging',
      mountId: 'luma-staging',
      resourceId: null,
      bindingId: null,
      localPlacement: null,
    }
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [providerRoot, CANVAS], connectedSources: [],
      nextCursor: null, hasMore: false, completeness: 'complete',
      sources: [{ id: 'local', kind: 'local', completeness: 'complete' }, PROVIDER_COMPLETE],
      queryCapabilities: {
        sort: [], kindFilter: false,
        reason: "Sorting and type filters aren't available in this view.",
      },
    })
    render(<WorkspaceExplorer />)

    expect(screen.queryByRole('region', { name: 'Connected sources' })).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Open folder luma-staging from Connected source luma-staging · fixture' }))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(providerRoot.id)
    expect(screen.queryByRole('combobox', { name: 'Sort Workspace' })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Filter Workspace by type' })).not.toBeInTheDocument()
    expect(screen.queryByText("Sorting and type filters aren't available in this view.")).not.toBeInTheDocument()
  })

  it('moves multiple selected Canvases with one atomic Workspace request', async () => {
    const secondCanvas = { ...CANVAS, id: 'canvas:canvas-2', name: 'Second analysis', placementId: 'canvas-placement-2', version: 4 }
    store.files = [
      { id: 'canvas-1', name: 'Analysis', version: 3, role: 'owner' },
      { id: 'canvas-2', name: 'Second analysis', version: 4, role: 'editor' },
    ]
    mocks.workspaceBrowse.mockImplementation((containerId: string) => Promise.resolve(containerId === 'folder-1'
      ? { container: FOLDER, items: [], nextCursor: null, hasMore: false, completeness: 'complete' }
      : { container: ROOT, items: [FOLDER, CANVAS, secondCanvas], nextCursor: null, hasMore: false, completeness: 'complete' }))
    mocks.workspaceBatch.mockResolvedValue({
      ok: true, action: 'move',
      items: [
        { ...CANVAS, parentId: FOLDER.id, version: 4 },
        { ...secondCanvas, parentId: FOLDER.id, version: 5 },
      ],
      container: FOLDER,
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByLabelText('Select Analysis'))
    fireEvent.click(screen.getByLabelText('Select Second analysis'))
    fireEvent.contextMenu(screen.getByRole('button', { name: 'Open canvas Analysis' }))
    const menu = await screen.findByRole('menu', { name: 'Actions for Analysis' })
    expect(menu).toHaveTextContent('2 selected')
    expect(within(menu).queryByRole('menuitem', { name: 'Open' })).not.toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: 'Rename' })).not.toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: 'Duplicate' })).not.toBeInTheDocument()
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Move' }))
    expect(await screen.findByRole('dialog', { name: 'Move 2 Canvases' })).toHaveTextContent(
      'If any one changed or cannot be moved, none are moved.',
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Research' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Move to Research' }))

    await waitFor(() => expect(mocks.workspaceBatch).toHaveBeenCalledTimes(1))
    expect(mocks.workspaceBatch).toHaveBeenCalledWith({
      action: 'move',
      items: [
        { placementId: 'canvas-placement', expectedVersion: 3 },
        { placementId: 'canvas-placement-2', expectedVersion: 4 },
      ],
      containerId: 'folder-1', expectedContainerVersion: 1,
    })
    expect(store.pushToast).toHaveBeenCalledWith('Moved 2 Canvases.', 'success')
  })

  it('shows a Folder drop target and reports a failed Canvas drag move', async () => {
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [FOLDER, CANVAS], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [{ id: 'local', kind: 'local', completeness: 'complete' }],
    })
    mocks.workspaceBatch.mockRejectedValue(new Error('destination changed'))
    render(<WorkspaceExplorer />)

    const transfer = { effectAllowed: '', dropEffect: '', setData: vi.fn() }
    const canvasRow = (await screen.findByRole('button', { name: 'Open canvas Analysis' })).parentElement!
    fireEvent.dragStart(canvasRow, { dataTransfer: transfer })
    const folderRow = screen.getByRole('button', { name: 'Open folder Research' }).parentElement!
    fireEvent.dragOver(folderRow, { dataTransfer: transfer })
    expect(within(folderRow).getByRole('status')).toHaveTextContent('Move here')
    fireEvent.drop(folderRow, { dataTransfer: transfer })

    await waitFor(() => expect(mocks.workspaceBatch).toHaveBeenCalledWith({
      action: 'move', items: [{ placementId: 'canvas-placement', expectedVersion: 3 }],
      containerId: 'folder-1', expectedContainerVersion: 1,
    }))
    expect(store.pushToast).toHaveBeenCalledWith(
      'Could not move “Analysis”: destination changed', 'error',
    )
  })

  it('drags the selected Canvas group as one atomic move', async () => {
    const secondCanvas = {
      ...CANVAS, id: 'canvas:canvas-2', name: 'Second analysis',
      placementId: 'canvas-placement-2', version: 4,
    }
    store.files = [
      { id: 'canvas-1', name: 'Analysis', version: 3, role: 'owner' },
      { id: 'canvas-2', name: 'Second analysis', version: 4, role: 'editor' },
    ]
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [FOLDER, CANVAS, secondCanvas], nextCursor: null,
      hasMore: false, completeness: 'complete',
    })
    mocks.workspaceBatch.mockResolvedValue({
      ok: true, action: 'move', container: FOLDER,
      items: [
        { ...CANVAS, parentId: FOLDER.id, version: 4 },
        { ...secondCanvas, parentId: FOLDER.id, version: 5 },
      ],
    })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByLabelText('Select Analysis'))
    fireEvent.click(screen.getByLabelText('Select Second analysis'))
    const transfer = { effectAllowed: '', dropEffect: '', setData: vi.fn() }
    const canvasRow = screen.getByRole('button', { name: 'Open canvas Analysis' }).parentElement!
    fireEvent.dragStart(canvasRow, { dataTransfer: transfer })
    const folderRow = screen.getByRole('button', { name: 'Open folder Research' }).parentElement!
    fireEvent.dragOver(folderRow, { dataTransfer: transfer })
    expect(within(folderRow).getByRole('status')).toHaveTextContent('Move 2 here')
    fireEvent.drop(folderRow, { dataTransfer: transfer })

    await waitFor(() => expect(mocks.workspaceBatch).toHaveBeenCalledWith({
      action: 'move',
      items: [
        { placementId: 'canvas-placement', expectedVersion: 3 },
        { placementId: 'canvas-placement-2', expectedVersion: 4 },
      ],
      containerId: 'folder-1', expectedContainerVersion: 1,
    }))
    expect(store.pushToast).toHaveBeenCalledWith(
      'Moved 2 Canvases to “Research”.', 'success',
    )
  })

  it('deletes multiple selected owned Canvases through one explicit confirmation', async () => {
    const secondCanvas = { ...CANVAS, id: 'canvas:canvas-2', name: 'Second analysis', placementId: 'canvas-placement-2', version: 4, canvasVersion: 23 }
    store.files = [
      { id: 'canvas-1', name: 'Analysis', version: 3, role: 'owner' },
      { id: 'canvas-2', name: 'Second analysis', version: 4, role: 'owner' },
    ]
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [CANVAS, secondCanvas], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceBatch.mockResolvedValue({ ok: true, action: 'delete_canvases', items: [], deletedCanvasIds: ['canvas-1', 'canvas-2'] })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByLabelText('Select Analysis'))
    fireEvent.click(screen.getByLabelText('Select Second analysis'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('dialog', { name: 'Delete 2 Canvases' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete selected' }))

    await waitFor(() => expect(mocks.workspaceBatch).toHaveBeenCalledTimes(1))
    expect(mocks.workspaceBatch).toHaveBeenCalledWith({
      action: 'delete_canvases',
      items: [
        { placementId: 'canvas-placement', expectedVersion: 3, expectedCanvasVersion: 17 },
        { placementId: 'canvas-placement-2', expectedVersion: 4, expectedCanvasVersion: 23 },
      ],
    })
    expect(dialog).toHaveTextContent('If any Canvas changed or cannot be deleted, nothing is deleted.')
  })

  it('does not expose Canvas mutations for a detached placement', async () => {
    const detachedCanvas = { ...CANVAS, detached: true }
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [detachedCanvas], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    const row = await screen.findByRole('button', { name: 'Open canvas Analysis' })
    expect(row).toBeDisabled()
    expect(row.parentElement).toHaveTextContent('Unavailable')
    expect(row.parentElement).toHaveTextContent('This Canvas is no longer available.')
    expect(screen.queryByRole('button', { name: 'More actions for Analysis' })).not.toBeInTheDocument()
  })

  it('keeps a source-only provider Folder free of Folder writes while retaining local Canvas creation', async () => {
    mocks.workspaceBrowse.mockResolvedValue({ container: ROOT, items: [EXTERNAL_FOLDER], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    expect(await screen.findByRole('button', { name: 'Open folder Remote from Connected source warehouse · fixture' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'More actions for Remote' })).not.toBeInTheDocument()
    expect(screen.queryByText('Connected catalogs manage their folders. Canvases created here stay local to Data Playground.')).not.toBeInTheDocument()
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
    expect(screen.getByRole('region', { name: 'Search source Connected source warehouse' })).toHaveTextContent('deadline exceeded')
    fireEvent.click(screen.getByRole('button', { name: 'Open dataset observations' }))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(DATASET.id)
    expect(mocks.workspaceSearch).toHaveBeenCalledWith('observations', { limit: 25, cursor: undefined })
  })

  it('keeps submitted results distinct from an unsubmitted Workspace search draft', async () => {
    store.workspaceSearchQuery = 'observations'
    mocks.workspaceSearch
      .mockResolvedValueOnce({
        query: 'observations', completeness: 'complete', hasMore: false, nextCursor: null,
        groups: [{ source: { id: 'local', kind: 'local', completeness: 'complete', freshness: 'current', searchMode: 'native' }, items: [DATASET] }],
      })
      .mockResolvedValueOnce({
        query: 'zzzz-no-match', completeness: 'complete', hasMore: false, nextCursor: null,
        groups: [],
      })
    const view = render(<WorkspaceExplorer />)

    expect(await screen.findByRole('button', { name: 'Open dataset observations' })).toBeVisible()
    fireEvent.change(screen.getByRole('textbox', { name: 'Search views, datasets, canvases, and containers' }), {
      target: { value: 'zzzz-no-match' },
    })
    expect(screen.getByRole('status')).toHaveTextContent('Results are still for “observations”. Select Search to update.')
    expect(screen.getByRole('button', { name: 'Open dataset observations' })).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Search Workspace' }))
    expect(store.setWorkspaceSearchQuery).toHaveBeenCalledWith('zzzz-no-match')
    store.workspaceSearchQuery = 'zzzz-no-match'
    view.rerender(<WorkspaceExplorer />)

    expect(await screen.findByText('No views, datasets, canvases, or containers match this query.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Open dataset observations' })).not.toBeInTheDocument()
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

    const row = await screen.findByRole('button', { name: 'Open canvas Analysis' })
    expect(row).toBeDisabled()
    expect(row.parentElement).toHaveTextContent('Unavailable')
    expect(screen.queryByRole('button', { name: 'More actions for Analysis' })).not.toBeInTheDocument()
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

  it('continues an empty Workspace search page without presenting it as a final empty result', async () => {
    store.workspaceSearchQuery = 'observations'
    mocks.workspaceSearch
      .mockResolvedValueOnce({
        query: 'observations', completeness: 'page', hasMore: true, nextCursor: 'sparse-page-2',
        groups: [{
          source: { id: 'local', kind: 'local', completeness: 'page', freshness: 'current', searchMode: 'native' },
          items: [],
        }],
      })
      .mockResolvedValueOnce({
        query: 'observations', completeness: 'complete', hasMore: false, nextCursor: null,
        groups: [{
          source: { id: 'local', kind: 'local', completeness: 'complete', freshness: 'current', searchMode: 'native' },
          items: [DATASET],
        }],
      })
    render(<WorkspaceExplorer />)

    expect(await screen.findByText('This page has no matches yet. Load more results to continue searching.')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Load more results' }))

    expect(await screen.findByRole('button', { name: 'Open dataset observations' })).toBeVisible()
    expect(mocks.workspaceSearch).toHaveBeenLastCalledWith('observations', {
      limit: 25, cursor: 'sparse-page-2',
    })
  })

  it('creates a canvas in the exact visible destination', async () => {
    mocks.workspaceCreateCanvas.mockResolvedValue({ ok: true, id: 'created-1', created: true, resource: CANVAS })
    render(<WorkspaceExplorer />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create canvas' }))
    expect(screen.getByRole('dialog', { name: 'Create canvas' })).toHaveTextContent('Folder: Workspace')
    fireEvent.change(screen.getByLabelText('Canvas name'), { target: { value: 'Exact destination' } })
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Create canvas' })).getByRole('button', { name: 'Create canvas' }))
    await waitFor(() => expect(mocks.workspaceCreateCanvas).toHaveBeenCalledWith({
      containerId: 'workspace-local-root', expectedContainerVersion: 1, name: 'Exact destination',
    }))
    expect(store.openFile).toHaveBeenCalledWith('created-1')
  })

  it('closes a Workspace dialog with Escape', async () => {
    render(<WorkspaceExplorer />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create canvas' }))
    expect(screen.getByRole('dialog', { name: 'Create canvas' })).toBeVisible()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('dialog', { name: 'Create canvas' })).not.toBeInTheDocument()
  })

  it('creates a locally owned Canvas in a source-only provider folder and reuses its request id on retry', async () => {
    store.workspaceResourceId = EXTERNAL_FOLDER.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_FOLDER, ancestors: [ROOT], source: PROVIDER_COMPLETE })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    mocks.workspaceCreateCanvas
      .mockRejectedValueOnce(new Error('connection interrupted after submission'))
      .mockResolvedValueOnce({ ok: true, id: 'external-created', created: true, resource: CANVAS })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Create canvas' }))
    const dialog = screen.getByRole('dialog', { name: 'Create canvas' })
    expect(dialog).toHaveTextContent('Folder: Remote')
    expect(dialog).not.toHaveTextContent('overlay')
    fireEvent.change(screen.getByLabelText('Canvas name'), { target: { value: 'Hand tracking review' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create canvas' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('connection interrupted')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create canvas' }))

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

    fireEvent.click(await screen.findByRole('button', { name: 'Create canvas' }))
    const dialog = screen.getByRole('dialog', { name: 'Create canvas' })
    fireEvent.change(screen.getByLabelText('Canvas name'), { target: { value: 'first intent' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create canvas' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('retry later')
    fireEvent.change(screen.getByLabelText('Canvas name'), { target: { value: 'second intent' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create canvas' }))

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

    const button = await screen.findByRole('button', { name: 'Create canvas' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', 'This Canvas folder is unavailable; retry after the connected source recovers')
    expect(screen.getByText('This connected source folder is empty.')).toBeVisible()
  })

  it('explains that a detached provider location must be relinked instead of calling it a local tombstone', async () => {
    const detached = { ...EXTERNAL_FOLDER, detached: true, referenceState: 'detached' as const }
    store.workspaceResourceId = detached.id
    mocks.workspaceResource.mockResolvedValue({ resource: detached, ancestors: [ROOT], source: { ...PROVIDER_COMPLETE, completeness: 'unavailable', error: 'resource detached' } })
    mocks.workspaceBrowse.mockResolvedValue({ container: detached, items: [], nextCursor: null, hasMore: false, completeness: 'partial', sources: [{ ...PROVIDER_COMPLETE, completeness: 'unavailable', error: 'resource detached' }] })
    render(<WorkspaceExplorer />)

    const button = await screen.findByRole('button', { name: 'Create canvas' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', 'This connected source is unavailable. Relink it before creating or moving a Canvas here')
  })

  it('explores a stable dataset in a new canvas at its visible container', async () => {
    store.workspaceResourceId = DATASET.id
    mocks.workspaceBrowse.mockResolvedValue({ container: FOLDER, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceCreateCanvas.mockResolvedValue({ ok: true, id: 'explore-1', created: true, nodeId: 'created-source', resource: CANVAS })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use' }))
    expect(screen.getByRole('dialog', { name: 'Use observations' })).toHaveTextContent('observations')
    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    await waitFor(() => expect(mocks.workspaceCreateCanvas).toHaveBeenCalledWith({
      containerId: 'folder-1', expectedContainerVersion: 1,
      name: 'observations exploration', datasetIds: ['dataset-1'],
    }))
    expect(store.openFile).toHaveBeenCalledWith('explore-1')
    await waitFor(() => expect(store.select).toHaveBeenCalledWith('created-source'))
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
    expect(screen.getByRole('button', { name: /^Add to a recent Canvas/ })).toBeVisible()
    await waitFor(() => expect(screen.getByRole('button', { name: /^Choose another Canvas/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /^Choose another Canvas/ }))
    await waitFor(() => expect(screen.getByLabelText('Target canvas')).toHaveValue('target-canvas'))
    expect(screen.queryByRole('option', { name: /Read only/ })).not.toBeInTheDocument()
    expect(screen.getByText('Source nodes will be added; your data is not copied or modified.')).toBeVisible()
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
    expect(screen.getByRole('button', { name: /^Add to a recent Canvas/ })).toBeDisabled()
    act(() => finishRefresh())
    await waitFor(() => expect(screen.getByRole('button', { name: /^Add to a recent Canvas/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /^Add to a recent Canvas/ }))
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
    expect(screen.getByRole('button', { name: /^Add to a recent Canvas/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /^Choose another Canvas/ })).toBeDisabled()
    expect(mocks.workspaceAddDatasets).not.toHaveBeenCalled()
  })

  it('keeps a Canvas version conflict fail-closed and offers one refresh-and-retry path', async () => {
    store.workspaceResourceId = DATASET.id
    store.doc = { id: 'current-canvas', version: 12 }
    store.files = [{ id: 'current-canvas', name: 'Current analysis', version: 12, role: 'editor' }]
    mocks.workspaceBrowse.mockResolvedValue({ container: FOLDER, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    mocks.workspaceAddDatasets.mockRejectedValueOnce(new KernelError(409, 'version changed'))
    store.refreshFiles
      .mockResolvedValueOnce(true)
      .mockImplementationOnce(async () => {
        store.files = [{ id: 'current-canvas', name: 'Current analysis', version: 13, role: 'editor' }]
        return true
      })
    const { rerender } = render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /^Add to a recent Canvas/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /^Add to a recent Canvas/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('That Canvas changed')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh Canvases' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Canvases refreshed. Try adding the Source again.'))
    expect(mocks.workspaceAddDatasets).toHaveBeenCalledTimes(1)
    expect(store.openFile).not.toHaveBeenCalled()
    rerender(<WorkspaceExplorer />)
    mocks.workspaceAddDatasets.mockResolvedValueOnce({ ok: true, id: 'current-canvas', version: 14 })
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    await waitFor(() => expect(mocks.workspaceAddDatasets).toHaveBeenCalledTimes(2))
    expect(mocks.workspaceAddDatasets).toHaveBeenLastCalledWith('current-canvas', expect.objectContaining({
      datasetIds: ['dataset-1'], expectedCanvasVersion: 13, requestId: expect.any(String),
    }))
    const firstRequest = mocks.workspaceAddDatasets.mock.calls[0]?.[1] as { requestId: string }
    const retryRequest = mocks.workspaceAddDatasets.mock.calls[1]?.[1] as { requestId: string }
    expect(retryRequest.requestId).not.toBe(firstRequest.requestId)
    expect(store.openFile).toHaveBeenCalledTimes(1)
    expect(store.openFile).toHaveBeenCalledWith('current-canvas')
  })

  it('confirms a placement-only canvas move and offers a versioned undo', async () => {
    mocks.workspaceBrowse.mockImplementation((containerId: string) => Promise.resolve(containerId === 'folder-1'
      ? { container: FOLDER, items: [], nextCursor: null, hasMore: false, completeness: 'complete' }
      : { container: ROOT, items: [FOLDER, CANVAS], nextCursor: null, hasMore: false, completeness: 'complete' }))
    mocks.workspaceBatch
      .mockResolvedValueOnce({ ok: true, action: 'move', items: [{ ...CANVAS, parentId: FOLDER.id, version: 4 }], container: FOLDER })
      .mockResolvedValueOnce({ ok: true, action: 'move', items: [{ ...CANVAS, version: 5 }], container: ROOT })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Move' }))
    expect(await screen.findByRole('dialog', { name: 'Move Analysis' })).toHaveTextContent('Current location: Workspace')
    fireEvent.click(await screen.findByRole('button', { name: 'Research' }))
    await waitFor(() => expect(screen.getByText(/Destination:/)).toHaveTextContent('Destination: Workspace / Research'))
    fireEvent.click(await screen.findByRole('button', { name: 'Move to Research' }))
    await waitFor(() => expect(mocks.workspaceBatch).toHaveBeenNthCalledWith(1, {
      action: 'move', items: [{ placementId: 'canvas-placement', expectedVersion: 3 }],
      containerId: 'folder-1', expectedContainerVersion: 1,
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Undo move' }))
    await waitFor(() => expect(mocks.workspaceBatch).toHaveBeenNthCalledWith(2, {
      action: 'move', items: [{ placementId: 'canvas-placement', expectedVersion: 4 }],
      containerId: 'workspace-local-root', expectedContainerVersion: 1,
    }))
  })

  it('moves a Canvas into an external local overlay and uses its local destination again for undo', async () => {
    mocks.workspaceBrowse.mockImplementation((containerId: string) => Promise.resolve(containerId === 'external.mount-folder'
      ? { container: EXTERNAL_FOLDER, items: [], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] }
      : containerId === 'mount.d2FyZWhvdXNl'
        ? { container: CONNECTED_SOURCE, items: [EXTERNAL_FOLDER], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] }
        : { container: ROOT, items: [CANVAS], connectedSources: [CONNECTED_SOURCE], nextCursor: null, hasMore: false, completeness: 'complete', sources: [{ id: 'local', kind: 'local', completeness: 'complete' }] }))
    mocks.workspaceBatch
      .mockResolvedValueOnce({ ok: true, action: 'move', items: [{ ...CANVAS, parentId: EXTERNAL_FOLDER.id, version: 4 }], container: EXTERNAL_FOLDER })
      .mockResolvedValueOnce({ ok: true, action: 'move', items: [{ ...CANVAS, version: 5 }], container: ROOT })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Move' }))
    const dialog = await screen.findByRole('dialog', { name: 'Move Analysis' })
    fireEvent.click(await within(dialog).findByRole('button', { name: /warehouse.*connected source/ }))
    expect(mocks.workspaceBrowse).toHaveBeenCalledWith('mount.d2FyZWhvdXNl', { limit: 50, cursor: undefined })
    fireEvent.click(await within(dialog).findByRole('button', { name: /Remote.*Canvas folder/ }))
    const move = await within(dialog).findByRole('button', { name: 'Move to Remote' })
    expect(screen.getByText(/Destination:/)).toHaveTextContent('Canvases stay in this Workspace')
    fireEvent.click(move)
    await waitFor(() => expect(mocks.workspaceBatch).toHaveBeenNthCalledWith(1, {
      action: 'move', items: [{ placementId: 'canvas-placement', expectedVersion: 3 }],
      containerId: 'local-overlay-anchor', expectedContainerVersion: 7,
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Undo move' }))
    await waitFor(() => expect(mocks.workspaceBatch).toHaveBeenNthCalledWith(2, {
      action: 'move', items: [{ placementId: 'canvas-placement', expectedVersion: 4 }],
      containerId: 'workspace-local-root', expectedContainerVersion: 1,
    }))
  })

  it('uses the hidden previous external overlay destination when undoing a move out of it', async () => {
    const overlayCanvas = { ...CANVAS, parentId: EXTERNAL_FOLDER.id }
    store.workspaceResourceId = EXTERNAL_FOLDER.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_FOLDER, ancestors: [ROOT], source: PROVIDER_COMPLETE })
    mocks.workspaceBrowse.mockImplementation((containerId: string) => Promise.resolve(containerId === 'external.mount-folder'
      ? { container: EXTERNAL_FOLDER, items: [overlayCanvas], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] }
      : { container: ROOT, items: [], nextCursor: null, hasMore: false, completeness: 'complete' }))
    mocks.workspaceBatch
      .mockResolvedValueOnce({ ok: true, action: 'move', items: [{ ...overlayCanvas, parentId: ROOT.id, version: 4 }], container: ROOT })
      .mockResolvedValueOnce({ ok: true, action: 'move', items: [{ ...overlayCanvas, parentId: EXTERNAL_FOLDER.id, version: 5 }], container: EXTERNAL_FOLDER })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Move' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Move to Workspace' }))
    await waitFor(() => expect(mocks.workspaceBatch).toHaveBeenNthCalledWith(1, {
      action: 'move', items: [{ placementId: 'canvas-placement', expectedVersion: 3 }],
      containerId: 'workspace-local-root', expectedContainerVersion: 1,
    }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Moved “Analysis”'))
    fireEvent.click(await screen.findByRole('button', { name: 'Undo move' }))
    await waitFor(() => expect(mocks.workspaceBatch).toHaveBeenNthCalledWith(2, {
      action: 'move', items: [{ placementId: 'canvas-placement', expectedVersion: 4 }],
      containerId: 'local-overlay-anchor', expectedContainerVersion: 7,
    }))
  })

  it('disables undo when a previous external local overlay is unavailable', async () => {
    const overlayCanvas = { ...CANVAS, parentId: EXTERNAL_FOLDER.id }
    const unavailable = { ...EXTERNAL_FOLDER, localPlacement: { ...EXTERNAL_LOCAL_PLACEMENT, recoveryState: 'unavailable' as const } }
    store.workspaceResourceId = EXTERNAL_FOLDER.id
    mocks.workspaceResource.mockResolvedValue({ resource: unavailable, ancestors: [ROOT], source: PROVIDER_COMPLETE })
    mocks.workspaceBrowse.mockImplementation((containerId: string) => Promise.resolve(containerId === 'external.mount-folder'
      ? { container: unavailable, items: [overlayCanvas], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] }
      : { container: ROOT, items: [], nextCursor: null, hasMore: false, completeness: 'complete' }))
    mocks.workspaceBatch.mockResolvedValueOnce({
      ok: true, action: 'move', items: [{ ...overlayCanvas, parentId: ROOT.id, version: 4 }], container: ROOT,
    })
    render(<WorkspaceExplorer />)

    fireEvent.pointerDown(await screen.findByRole('button', { name: 'More actions for Analysis' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Move' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Move to Workspace' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Moved “Analysis”'))
    const undo = await screen.findByRole('button', { name: 'Undo unavailable' })
    expect(undo).toBeDisabled()
    expect(undo).toHaveAttribute('title', 'This Canvas folder is unavailable; retry after the connected source recovers')
    expect(screen.getByRole('status')).toHaveTextContent('recover or relink it before undoing')
    expect(mocks.workspaceBatch).toHaveBeenCalledTimes(1)
  })

  it('keeps an honest error and offers an explicit retry', async () => {
    mocks.workspaceBrowse.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ container: ROOT, items: [], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    expect(await screen.findByRole('alert')).toHaveTextContent('offline')
    fireEvent.click(screen.getByText('Retry'))
    expect(await screen.findByText('This folder is empty. Create a Canvas here to get started.')).toBeInTheDocument()
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

    expect(await screen.findByText(/The file behind this dataset is no longer available/)).toBeVisible()
    expect(screen.queryByText(/placement is detached/i)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove from Workspace…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove', exact: true }))
    await waitFor(() => expect(mocks.workspaceRemoveDetachedDataset).toHaveBeenCalledWith(
      DATASET.placementId, { expectedVersion: DATASET.version },
    ))
  })

  it('keeps the loaded page visible when loading the next page fails', async () => {
    mocks.workspaceBrowse
      .mockResolvedValueOnce({ container: ROOT, items: [FOLDER], nextCursor: 'cursor-2', hasMore: true, completeness: 'page' })
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockResolvedValueOnce({ container: ROOT, items: [DATASET], nextCursor: null, hasMore: false, completeness: 'complete' })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByTestId('workspace-next-page'))
    expect(await screen.findByRole('alert')).toHaveTextContent('temporary failure')
    expect(screen.getByText('Research')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('workspace-next-page'))
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

    const archive = await screen.findByRole('button', { name: 'Open dataset observations from Connected source archive · fixture' })
    expect(screen.getByRole('button', { name: 'Open dataset observations from Connected source warehouse · fixture' })).toBeVisible()
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
    expect(screen.getByRole('region', { name: 'Workspace source status' })).toHaveTextContent('Some Workspace sources are unavailable')
    expect(screen.getByRole('region', { name: 'Workspace source status' })).toHaveTextContent('Connected source warehouse · fixture · Unavailable — deadline exceeded')
  })

  it('translates every provider completeness state without treating a healthy page as unavailable', async () => {
    mocks.workspaceBrowse.mockResolvedValue({
      container: ROOT, items: [FOLDER], nextCursor: null, hasMore: false, completeness: 'partial',
      sources: [
        { ...PROVIDER_COMPLETE, id: 'complete', mountId: 'complete', completeness: 'complete' },
        { ...PROVIDER_COMPLETE, id: 'page', mountId: 'page', completeness: 'page' },
        { ...PROVIDER_COMPLETE, id: 'partial', mountId: 'partial', completeness: 'partial' },
        { ...PROVIDER_COMPLETE, id: 'unavailable', mountId: 'unavailable', completeness: 'unavailable' },
        { ...PROVIDER_COMPLETE, id: 'unsupported', mountId: 'unsupported', completeness: 'unsupported' },
      ],
    })
    render(<WorkspaceExplorer />)

    const status = await screen.findByRole('region', { name: 'Workspace source status' })
    expect(status).not.toHaveTextContent('Connected source complete')
    expect(status).not.toHaveTextContent('Connected source page')
    expect(status).toHaveTextContent('Some results unavailable')
    expect(status).toHaveTextContent('Unavailable')
    expect(status).toHaveTextContent('Browse unavailable')
    expect(status).not.toHaveTextContent('· page')
  })

  it('uses an external dataset by stable reference without catalog lookup or provider writes', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    store.files = [{ id: 'target-canvas', name: 'Exact target', version: 9, role: 'editor' }]
    mocks.workspaceAddDatasets.mockResolvedValue({ ok: true, id: 'target-canvas', version: 10 })
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(detail).toHaveTextContent('LocationConnected source warehouse / Remote / observationsfixture')
    expect(detail).toHaveTextContent('2 rows· 1 data column')
    expect(detail).toHaveTextContent('Preview')
    await waitFor(() => expect(mocks.datasetRevision).toHaveBeenCalledWith(
      CANONICAL_DATASET_CONTEXT.datasetIdentity, CANONICAL_DATASET_CONTEXT.revisionId,
    ))
    fireEvent.click(screen.getByRole('button', { name: 'Use in Canvas' }))
    expect(screen.getByRole('dialog', { name: 'Use observations' })).toHaveTextContent(
      'Data Playground saves only the connection and display details',
    )
    await waitFor(() => expect(screen.getByRole('button', { name: /^Choose another Canvas/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /^Choose another Canvas/ }))
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

  it('separates canonical data columns from exact-preview system columns in Workspace', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    const dataColumns = [
      { name: 'image', type: 'binary', provenance: 'provider' as const, capabilities: [], annotations: [] },
      { name: 'source_rowid', type: 'int', provenance: 'provider' as const, capabilities: [], annotations: [] },
    ]
    const rowId = { name: '_rowid', type: 'uint64', provenance: 'inferred' as const, capabilities: [], annotations: [] }
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceCanonicalDataset.mockResolvedValue({ ...CANONICAL_DATASET_CONTEXT, columns: dataColumns })
    mocks.datasetRevision.mockResolvedValue({
      datasetId: CANONICAL_DATASET_CONTEXT.datasetIdentity, revisionId: CANONICAL_DATASET_CONTEXT.revisionId,
      summary: { rowCount: 1, dataFileCount: null, totalBytes: null, fragmentCount: null },
      preview: {
        columns: [dataColumns[0], rowId, dataColumns[1]],
        rows: [{ image: '<123 bytes>', _rowid: 7, source_rowid: 42 }], hasMore: false, rowLimit: 100,
      },
    })

    render(<WorkspaceExplorer />)

    const context = await screen.findByTestId('canonical-provider-dataset-context')
    await waitFor(() => expect(within(context).getByTestId('provider-column-summary')).toHaveTextContent(
      '1 row· 2 data columns· 1 system column',
    ))
    const systemLabels = within(context).getAllByText('System row ID')
    expect(systemLabels).toHaveLength(2)
    for (const label of systemLabels) {
      expect(label).toHaveAttribute('title', expect.stringContaining('not a data column'))
    }
    expect(within(context).getAllByTestId('provider-preview-column-name').map((column) => column.textContent)).toEqual([
      'image', '_rowid', 'source_rowid',
    ])
  })

  it.each([
    ['the current Workspace route', ''],
    ['the exact current-version route', new URLSearchParams({
      revision: CANONICAL_DATASET_CONTEXT.revisionId,
      revisionDataset: CANONICAL_DATASET_CONTEXT.datasetIdentity,
    }).toString()],
  ])('keeps a provider-declared _rowid in the canonical data schema on %s', async (_route, query) => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    store.workspaceDatasetQuery = query
    const declaredRowId = {
      name: '_rowid', type: 'string', provenance: 'provider' as const, capabilities: [], annotations: [],
    }
    const inferredPreviewRowId = { ...declaredRowId, provenance: 'inferred' as const }
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceCanonicalDataset.mockResolvedValue({
      ...CANONICAL_DATASET_CONTEXT, columns: [declaredRowId],
    })
    mocks.datasetRevision.mockResolvedValue({
      datasetId: CANONICAL_DATASET_CONTEXT.datasetIdentity, revisionId: CANONICAL_DATASET_CONTEXT.revisionId,
      summary: { rowCount: 1, dataFileCount: null, totalBytes: null, fragmentCount: null },
      preview: { columns: [inferredPreviewRowId], rows: [{ _rowid: 'business-key' }], hasMore: false, rowLimit: 100 },
    })

    render(<WorkspaceExplorer />)

    const context = await screen.findByTestId('canonical-provider-dataset-context')
    await waitFor(() => expect(within(context).getByTestId('provider-column-summary')).toHaveTextContent(
      '1 row· 1 data column',
    ))
    expect(within(context).getByTestId('provider-column-summary')).not.toHaveTextContent('system')
    expect(within(context).queryByText('System row ID')).not.toBeInTheDocument()
  })

  it('opens a provider search result in the full-page dataset route while preserving search context', async () => {
    store.workspaceSearchQuery = 'will_demo'
    mocks.workspaceSearch.mockResolvedValue({
      query: 'will_demo', completeness: 'complete', hasMore: false, nextCursor: null,
      groups: [{
        source: { ...PROVIDER_COMPLETE, freshness: 'current', searchMode: 'native' },
        items: [EXTERNAL_DATASET],
      }],
    })
    mocks.workspaceResource.mockImplementation((resourceId: string) => Promise.resolve(
      resourceId === EXTERNAL_FOLDER.id
        ? { resource: EXTERNAL_FOLDER, ancestors: [ROOT], source: PROVIDER_COMPLETE }
        : {
            resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
            canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
          },
    ))
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    const view = render(<WorkspaceExplorer />)

    const results = await screen.findByTestId('workspace-search-results')
    fireEvent.click(within(results).getByRole('button', {
      name: 'Open dataset observations from Connected source warehouse · fixture',
    }))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(EXTERNAL_DATASET.id)

    store.workspaceResourceId = EXTERNAL_DATASET.id
    view.rerender(<WorkspaceExplorer />)

    const viewer = await screen.findByTestId('provider-dataset-viewer')
    expect(viewer).toHaveClass('h-full', 'min-w-0', 'flex-1', 'bg-background')
    const detail = screen.getByRole('region', { name: 'observations' })
    expect(detail).not.toHaveClass('w-[420px]')
    expect(await within(detail).findByText('Published version')).toBeVisible()
    expect(within(detail).queryByText('Diagnostics')).not.toBeInTheDocument()
    expect(within(detail).queryByText('revision-7')).not.toBeInTheDocument()
    expect(screen.queryByTestId('workspace-search-results')).not.toBeInTheDocument()

    fireEvent.click(within(detail).getByRole('button', { name: 'Back to Workspace' }))
    expect(store.setWorkspaceResource).toHaveBeenLastCalledWith(EXTERNAL_FOLDER.id)
    expect(store.setWorkspaceSearchQuery).not.toHaveBeenCalled()
    store.workspaceResourceId = EXTERNAL_FOLDER.id
    view.rerender(<WorkspaceExplorer />)
    const restored = await screen.findByTestId('workspace-search-results')
    expect(restored).toHaveTextContent('for “will_demo”')
    expect(within(restored).getByRole('button', {
      name: 'Open dataset observations from Connected source warehouse · fixture',
    })).toBeVisible()
  })

  it('restores an exact provider Source viewer from its route and returns to the live Canvas node', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    store.workspaceDatasetQuery = new URLSearchParams({
      revision: 'retained-revision-3',
      revisionDataset: 'workspace-provider:retained-source',
      returnCanvas: 'canvas-1',
      returnNode: 'source-1',
    }).toString()
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    mocks.workspaceCanonicalDataset.mockRejectedValue(new Error('current provider head unavailable'))
    mocks.datasetRevision.mockResolvedValue({
      datasetId: 'workspace-provider:retained-source', revisionId: 'retained-revision-3',
      committedAt: '2026-07-20T12:00:00Z', retentionOwner: 'provider', summary: { rowCount: 1 },
      preview: {
        columns: [
          { name: 'historical_value', type: 'string', capabilities: [] },
          { name: '_rowid', type: 'uint64', provenance: 'inferred', capabilities: [] },
        ],
        rows: [{ historical_value: 'retained row', _rowid: 9 }], hasMore: false, rowLimit: 100,
      },
    })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(detail).toHaveTextContent('Selected version')
    const context = within(detail).getByTestId('canonical-provider-dataset-context')
    expect(context).toHaveTextContent('1 row')
    expect(context).toHaveTextContent('1 data column')
    expect(context).toHaveTextContent('1 system column')
    expect(context).toHaveTextContent('historical_value')
    expect(context).toHaveTextContent('Text')
    expect(within(context).getAllByText('System row ID')).toHaveLength(2)
    expect(within(context).getAllByTestId('provider-preview-column-name').map((column) => column.textContent)).toEqual([
      'historical_value', '_rowid',
    ])
    expect(context).toHaveTextContent('retained row')
    expect(context).not.toHaveTextContent('value · int64')
    expect(detail).not.toHaveTextContent("Couldn't load provider details")
    expect(within(detail).queryByRole('button', { name: 'Use in Canvas' })).not.toBeInTheDocument()
    expect(mocks.datasetRevision).toHaveBeenCalledWith(
      'workspace-provider:retained-source', 'retained-revision-3',
    )
    expect(mocks.datasetRevision).not.toHaveBeenCalledWith(
      CANONICAL_DATASET_CONTEXT.datasetIdentity, CANONICAL_DATASET_CONTEXT.revisionId,
    )

    fireEvent.click(within(detail).getByRole('button', { name: 'Back to Canvas' }))
    expect(store.activateLoadedCanvasRoute).toHaveBeenCalledWith('canvas-1', 'source-1')
    expect(store.clearWorkspaceDatasetViewerState).toHaveBeenCalledWith('')
    expect(store.openFile).not.toHaveBeenCalled()
  })

  it('rejects a mismatched exact provider revision response instead of showing it as selected', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    store.workspaceDatasetQuery = new URLSearchParams({
      revision: 'retained-revision-3',
      revisionDataset: 'workspace-provider:retained-source',
    }).toString()
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    mocks.datasetRevision.mockResolvedValue({
      datasetId: 'workspace-provider:other-source', revisionId: 'other-revision',
      retentionOwner: 'provider', summary: { rowCount: 1 },
      preview: { columns: [{ name: 'wrong', type: 'string', capabilities: [] }], rows: [{ wrong: 'row' }], hasMore: false, rowLimit: 100 },
    })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(await within(detail).findByText(/returned a different dataset version/)).toBeVisible()
    expect(within(detail).queryByText('wrong · string')).not.toBeInTheDocument()
    expect(within(detail).queryByText('row', { exact: true })).not.toBeInTheDocument()
    fireEvent.click(within(detail).getByRole('button', { name: 'Back to Workspace' }))
    expect(store.switchWorkspaceScope).toHaveBeenCalledWith('all', {
      resourceId: EXTERNAL_FOLDER.id,
      datasetQuery: '',
    })
  })

  it('keeps source pagination out of dataset details while leaving the dataset usable', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    const pagedSource = { ...PROVIDER_COMPLETE, completeness: 'page' as const }
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: pagedSource,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: 'next', hasMore: true,
      completeness: 'page', sources: [pagedSource],
    })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(within(detail).queryByRole('status', { name: 'Dataset source status' })).not.toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: 'Use in Canvas' })).toBeEnabled()
  })

  it('shows one actionable provider error while keeping protocol state collapsed', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    const partialSource = { ...PROVIDER_COMPLETE, completeness: 'partial' as const, error: 'provider timed out' }
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: partialSource,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'partial', sources: [partialSource],
    })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(within(detail).getByRole('status')).toHaveTextContent('provider timed out')
    expect(within(detail).getAllByRole('button', { name: 'Retry' })).toHaveLength(1)
    expect(within(detail).queryByText('Diagnostics')).not.toBeInTheDocument()
    expect(within(detail).queryByText('Provider result state · partial')).not.toBeInTheDocument()
  })

  it('adds a provider reference to the exact editable current Canvas without provider mutation', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    store.doc = { id: 'current-provider-canvas', version: 9 }
    store.files = [{ id: 'current-provider-canvas', name: 'Current provider analysis', version: 9, role: 'owner' }]
    mocks.workspaceAddDatasets.mockResolvedValue({ ok: true, id: 'current-provider-canvas', version: 10 })
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use in Canvas' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /^Add to a recent Canvas/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /^Add to a recent Canvas/ }))
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

    fireEvent.click(await screen.findByRole('button', { name: 'Use in Canvas' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /^Add to a recent Canvas/ })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /^Add to a recent Canvas/ }))
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

    fireEvent.click(await screen.findByRole('button', { name: 'Use in Canvas' }))
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

    fireEvent.click(await screen.findByRole('button', { name: 'Use in Canvas' }))
    const create = screen.getByRole('button', { name: 'Create and open' })
    expect(create).toBeDisabled()
    expect(create).toHaveAttribute('title', 'This Canvas folder is unavailable; retry after the connected source recovers')
    expect(screen.getByRole('status')).toHaveTextContent('Canvas folder is unavailable')
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
    await screen.findByRole('region', { name: 'observations' })

    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    view.rerender(<WorkspaceExplorer />)

    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(detail).toHaveTextContent('LocationConnected source warehouse / Remote / observations')
    expect(detail).not.toHaveTextContent('remote-dataset')
    expect(detail).not.toHaveTextContent('canonical-observations')
    expect(detail).not.toHaveTextContent('workspace-provider:canonical-source')
    expect(detail).not.toHaveTextContent('revision-7')
    const context = within(detail).getByTestId('canonical-provider-dataset-context')
    expect(within(context).getAllByText('value', { exact: true })[0]).toBeVisible()
    expect(within(context).getByText('Integer', { exact: true })).toBeVisible()
    expect(detail).toHaveTextContent('Other locationsRemote B / observations')
    expect(mocks.workspaceResource).toHaveBeenLastCalledWith(EXTERNAL_DATASET.id)
    expect(mocks.workspaceCanonicalDataset).toHaveBeenCalledWith(
      alternate.id, { signal: expect.any(AbortSignal) },
    )
    expect(mocks.workspaceCanonicalDataset).toHaveBeenCalledWith(
      EXTERNAL_DATASET.id, { signal: expect.any(AbortSignal) },
    )
  })

  it('embeds recorded provider lineage and opens a linked catalog dataset', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource.mockResolvedValue({
      resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
      canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
    })
    mocks.workspaceBrowse.mockResolvedValue({
      container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false,
      completeness: 'complete', sources: [PROVIDER_COMPLETE],
    })
    mocks.lineage.mockResolvedValue({
      rootUri: CANONICAL_DATASET_CONTEXT.sourceUri,
      nodes: [
        { id: 'canonical-observations', name: 'observations', uri: CANONICAL_DATASET_CONTEXT.sourceUri, kind: 'dataset' },
        { id: 'dataset-1', name: 'events', uri: 'file:///events.parquet', kind: 'dataset' },
      ],
      edges: [{ parent: CANONICAL_DATASET_CONTEXT.sourceUri, child: 'file:///events.parquet', factCount: 1 }],
      truncated: false,
    })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(await within(detail).findByTestId('dataset-lineage-summary')).toBeVisible()
    expect(mocks.lineage).toHaveBeenCalledWith(CANONICAL_DATASET_CONTEXT.sourceUri, 1, 16)
    fireEvent.click(within(detail).getByRole('button', { name: 'Lineage' }))
    expect(store.openRelationships).toHaveBeenCalledWith(
      CANONICAL_DATASET_CONTEXT.sourceUri,
      expect.objectContaining({ mode: 'lineage' }),
    )
    fireEvent.click(within(detail).getByRole('button', { name: 'events' }))

    await waitFor(() => expect(mocks.table).toHaveBeenCalledWith('dataset-1'))
    expect(store.rememberTables).toHaveBeenCalledWith([expect.objectContaining({ registrationId: 'dataset-1' })])
    expect(store.setWorkspaceResource).toHaveBeenCalledWith('dataset:dataset-1')
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

    const detail = await screen.findByRole('region', { name: 'observations' })
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

    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(detail).toHaveTextContent('LocationConnected source warehouse / observations')
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
    expect(screen.getByRole('region', { name: 'observations' })).toHaveTextContent('Latest provider version')
    expect(context).not.toHaveTextContent('Exact revision')
  })

  it('opens lineage-only dataset details without offering an unusable Canvas Source', async () => {
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
      readMode: 'lineage',
      revisionId: null,
      committedAt: null,
      columns: [{
        name: 'original_row_id', type: 'unknown', provenance: 'provider' as const,
        capabilities: [], annotations: [],
      }],
    })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(detail).toHaveTextContent('Lineage record')
    expect(detail).toHaveTextContent('original_row_id')
    expect(within(detail).getByRole('button', { name: 'Use in Canvas' })).toBeDisabled()
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
    mocks.datasetRevision.mockResolvedValue({
      datasetId: CANONICAL_DATASET_CONTEXT.datasetIdentity, revisionId: CANONICAL_DATASET_CONTEXT.revisionId,
      summary: { rowCount: 2, dataFileCount: null, totalBytes: null, fragmentCount: null },
      preview: {
        columns: columns.slice(0, 2),
        rows: [{ 'column-0': 'first', 'column-1': 'second' }], hasMore: false, rowLimit: 100,
      },
    })
    render(<WorkspaceExplorer />)

    const context = await screen.findByTestId('canonical-provider-dataset-context')
    expect(within(context).getAllByText('column-0')[0]).toBeVisible()
    expect(within(context).getByText('column-5')).toBeVisible()
    expect(within(context).getByText('column-6')).toBeVisible()
    expect(within(context).getByText('column-26')).toBeVisible()
    expect(context).not.toHaveTextContent('more data columns')
  })

  it('keeps a wide provider detail scrollable while its close and use actions stay reachable', async () => {
    const columns = Array.from({ length: 100 }, (_, index) => ({
      name: `provider-column-${index}`, type: 'string', provenance: 'provider' as const,
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
    mocks.workspaceCanonicalDataset.mockResolvedValue({
      ...CANONICAL_DATASET_CONTEXT,
      datasetIdentity: 'provider://warehouse/' + 'long-revision-identity/'.repeat(20),
      revisionId: 'revision-' + 'a'.repeat(200),
      columns,
    })
    render(<WorkspaceExplorer />)

    const detail = await screen.findByRole('region', { name: 'observations' })
    const content = within(detail).getByTestId('provider-dataset-detail-content')
    expect(content).toHaveClass('min-h-0', 'flex-1', 'overflow-y-auto', 'overscroll-contain')
    expect(content).toHaveAttribute('tabindex', '0')
    expect(within(detail).queryByLabelText('Provider dataset schema')).not.toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: 'Back to Workspace' })).toBeVisible()
    expect(within(detail).getByRole('button', { name: 'Use in Canvas' })).toBeVisible()
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

    const failure = await screen.findByText("Couldn't load dataset details.")
    expect(failure).not.toHaveTextContent('canonical detail timed out')
    expect(screen.getByRole('button', { name: 'Use in Canvas' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByTestId('canonical-provider-dataset-context')
    expect(screen.getByRole('region', { name: 'observations' })).toHaveTextContent('Published version')
    expect(mocks.workspaceCanonicalDataset.mock.calls.length).toBeGreaterThanOrEqual(2)
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
    expect(results).toHaveTextContent('Remote / observations')
    expect(results).toHaveTextContent('Other Remote / observations')
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
    expect(merged).toHaveTextContent('Remote / observations')
    expect(merged).toHaveTextContent('Page Two / observations')
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

    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(detail).not.toHaveTextContent('Other locations')
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
    await screen.findByRole('region', { name: 'observations' })

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
    expect(await screen.findByRole('region', { name: 'observations' })).toHaveTextContent(
      'Other locationsStale Remote / observations',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Back to Workspace' }))
    store.workspaceResourceId = EXTERNAL_FOLDER.id
    store.workspaceSearchQuery = 'observations'
    mocks.workspaceSearch.mockResolvedValue({
      query: 'observations', completeness: 'complete', hasMore: false, nextCursor: null,
      groups: [{
        source: { ...PROVIDER_COMPLETE, freshness: 'stale', searchMode: 'native' },
        items: [alternate],
      }],
    })
    mocks.workspaceResource.mockImplementation((resourceId: string) => Promise.resolve(
      resourceId === EXTERNAL_FOLDER.id
        ? { resource: EXTERNAL_FOLDER, ancestors: [ROOT], source: PROVIDER_COMPLETE }
        : resourceId === alternate.id
          ? {
              resource: alternate, ancestors: [ROOT, alternateFolder], source: PROVIDER_COMPLETE,
              canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
            }
          : {
              resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE,
              canonicalSourceBinding: CANONICAL_SOURCE_BINDING,
            },
    ))
    view.rerender(<WorkspaceExplorer />)
    const results = await screen.findByTestId('workspace-search-results')
    expect(results).toHaveTextContent('Stale Remote / observations')

    store.workspaceSearchQuery = ''
    store.workspaceResourceId = EXTERNAL_DATASET.id
    view.rerender(<WorkspaceExplorer />)
    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(detail).not.toHaveTextContent('Other locations')
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

    const detail = await screen.findByRole('region', { name: 'observations' })
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
    const detachedDetail = await screen.findByRole('region', { name: 'observations' })
    expect(detachedDetail).toHaveTextContent('This provider location is not available right now.')
    expect(detachedDetail).not.toHaveTextContent('Placement state · detached')
    expect(detachedDetail).not.toHaveTextContent('Diagnostics')
    first.unmount()
    mocks.workspaceResource.mockClear()

    const unavailableCanonical = { ...EXTERNAL_DATASET, canonicalReferenceState: 'offline' as const, lastKnown: true }
    mocks.workspaceResource.mockResolvedValue({ resource: unavailableCanonical, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [unavailableCanonical], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    const second = render(<WorkspaceExplorer />)
    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(within(detail).getByRole('status')).toHaveTextContent(/not available|offline/i)
    expect(detail).not.toHaveTextContent('Dataset status · offline')
    expect(detail).not.toHaveTextContent('Placement state · current')
    expect(screen.getByRole('button', { name: 'Use in Canvas' })).toBeDisabled()
    fireEvent.click(within(detail).getByRole('button', { name: 'Retry' }))
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
    const both = await screen.findByRole('region', { name: 'observations' })
    expect(within(both).getByRole('status')).toHaveTextContent(/not available|offline/i)
    expect(both).not.toHaveTextContent('Placement state · offline')
    expect(both).not.toHaveTextContent('Dataset status · offline')
    expect(screen.getByRole('button', { name: 'Use in Canvas' })).toBeDisabled()
  })

  it('preserves an external selection and ancestors when its refresh becomes unavailable', async () => {
    store.workspaceResourceId = EXTERNAL_DATASET.id
    mocks.workspaceResource
      .mockResolvedValueOnce({ resource: EXTERNAL_DATASET, ancestors: [ROOT, EXTERNAL_FOLDER], source: PROVIDER_COMPLETE, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
      .mockResolvedValueOnce({ resource: EXTERNAL_DATASET, ancestors: [ROOT], source: { ...PROVIDER_COMPLETE, completeness: 'partial', error: 'ancestor read interrupted' }, canonicalSourceBinding: CANONICAL_SOURCE_BINDING })
    mocks.workspaceBrowse.mockResolvedValue({ container: EXTERNAL_FOLDER, items: [EXTERNAL_DATASET], nextCursor: null, hasMore: false, completeness: 'complete', sources: [PROVIDER_COMPLETE] })
    render(<WorkspaceExplorer />)

    expect(await screen.findByRole('region', { name: 'observations' })).toBeVisible()
    expect(screen.queryByRole('navigation', { name: 'Workspace path' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reload dataset' }))
    expect(await screen.findByText(/ancestor read interrupted/)).toBeVisible()
    expect(screen.getByRole('region', { name: 'observations' })).toBeVisible()
    expect(screen.queryByRole('navigation', { name: 'Workspace path' })).not.toBeInTheDocument()
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

    const detail = await screen.findByRole('region', { name: 'observations' })
    expect(within(detail).getByRole('status')).toHaveTextContent('provider offline')
    expect(detail).not.toHaveTextContent('Placement state · offline')
    fireEvent.click(screen.getAllByRole('button', { name: 'Relink' })[0])
    const dialog = screen.getByRole('dialog', { name: 'Relink observations' })
    expect(dialog).toHaveTextContent('Names alone are not used to repair a connection')
    expect(screen.getByLabelText('Replacement mount ID')).toHaveValue('warehouse')
    expect(screen.getByLabelText('Replacement provider resource ID')).toHaveValue('remote-dataset')
    fireEvent.click(screen.getAllByRole('button', { name: 'Relink' }).at(-1)!)

    await waitFor(() => expect(mocks.workspaceRelink).toHaveBeenCalledWith(stale.id, {
      mountId: 'warehouse', resourceId: 'remote-dataset',
    }))
    expect(store.setWorkspaceResource).toHaveBeenCalledWith(fresh.id)
  })
})
