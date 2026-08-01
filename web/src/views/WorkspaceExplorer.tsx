import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type DragEvent, type ReactNode,
} from 'react'
import { api, KernelError, type CanvasFile } from '../api/client'
import { useStore } from '../store/graph'
import type { ColumnSchema } from '../types/graph'
import type {
  CatalogTable, DatasetRevisionDetail, DatasetViewDefinition, WorkspaceResource, WorkspaceSearchGroup,
  WorkspaceCanonicalDatasetContext, WorkspaceQueryCapabilities, WorkspaceSourceStatus,
} from '../types/api'
import { Icon } from '../ui/Icon'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import {
  ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuLabel,
  ContextMenuSeparator, ContextMenuTrigger,
} from '../components/ui/context-menu'
import { AddDataModal, CatalogDetail } from './CatalogDiscovery'
import { WorkspaceLocalDrafts } from '../canvas/LocalDrafts'
import { DatasetViewDetail } from './DatasetViewDetail'
import { examples } from '../examples'
import { parseDatasetViewerReturn, type ParsedDatasetViewerReturn } from '../router'
import { CanvasCopyModal, type CanvasCopySource } from '../panels/CanvasCopyModal'
import { DatasetLineageSummary } from '../components/DatasetLineageSummary'

const LOCAL_ROOT_ID = 'workspace-local-root'
const PAGE_SIZE = 50
const WORKSPACE_SEARCH_PAGE_SIZE = 25
const WORKSPACE_SEARCH_ENRICHMENT_MAX_OBSERVATIONS = 100
const WORKSPACE_ROOT_BREADCRUMB: WorkspaceResource = {
  id: `container:${LOCAL_ROOT_ID}`, kind: 'container', name: 'Workspace', detached: false, source: 'local',
}
const NON_EMPTY_LOCAL_FOLDER_REASON = "Move or remove this Folder's contents before deleting it."
const PROVIDER_PLACEMENT_CACHE_MAX_DATASETS = 64
const PROVIDER_PLACEMENT_CACHE_MAX_PLACEMENTS = 6
const PROVIDER_PLACEMENT_CACHE_MAX_PATHS = 256
const SYSTEM_ROW_ID_DESCRIPTION = 'System row ID supplied for this run; it is not a data column.'
const LOCAL_QUERY_CAPABILITIES: WorkspaceQueryCapabilities = {
  sort: ['name', 'updated'], kindFilter: true,
}

function isProviderBrowseIdentity(identity: string): boolean {
  return identity.startsWith('external.') || identity.startsWith('mount.')
}

function isConnectedSourceRoot(resource: WorkspaceResource): boolean {
  return resource.id.startsWith('container:mount.')
}

function datasetViewerBackLabel(returnTo?: ParsedDatasetViewerReturn): 'Back to Workspace' | 'Back to Canvas' | 'Back to Jobs' | 'Back to Inbox' {
  if (returnTo?.view === 'canvas') return 'Back to Canvas'
  if (returnTo?.view === 'jobs') return 'Back to Jobs'
  if (returnTo?.view === 'inbox') return 'Back to Inbox'
  return 'Back to Workspace'
}

function preserveDatasetViewerReturn(params: URLSearchParams, returnTo?: ParsedDatasetViewerReturn) {
  if (returnTo?.view === 'canvas') {
    params.set('returnCanvas', returnTo.canvasId)
    if (returnTo.nodeId) params.set('returnNode', returnTo.nodeId)
  } else if (returnTo) {
    params.set('returnView', returnTo.view)
    if (returnTo.query) params.set('returnQuery', returnTo.query)
  }
}

type ProviderSystemColumnPresentation = {
  column: ColumnSchema
  label: 'System row ID' | 'System column'
  description: string
}

function providerColumnCount(count: number, kind: 'data' | 'system'): string {
  return `${count.toLocaleString()} ${kind} ${count === 1 ? 'column' : 'columns'}`
}

function providerRowCount(count: number | null | undefined): string {
  if (count == null) return 'Rows not reported'
  return `${count.toLocaleString()} ${count === 1 ? 'row' : 'rows'}`
}

function friendlyProviderColumnType(type: string): string {
  const normalized = type.trim().toLowerCase()
  if (/^(u?int|integer|bigint|smallint|tinyint)/.test(normalized)) return 'Integer'
  if (/^(float|double|decimal|numeric)/.test(normalized)) return 'Number'
  if (/^(utf8|string|varchar|text)/.test(normalized)) return 'Text'
  if (/^(bool|boolean)/.test(normalized)) return 'Boolean'
  if (/^(timestamp|datetime)/.test(normalized)) return 'Timestamp'
  if (/^date/.test(normalized)) return 'Date'
  if (/^(binary|blob)/.test(normalized)) return 'Binary'
  if (/^(list|array|fixed_size_list)/.test(normalized)) return 'List'
  if (/^(struct|map)/.test(normalized)) return 'Object'
  return type
}

function providerSystemColumn(
  column: ColumnSchema,
  canonicalNames: Set<string>,
  compareWithCanonical: boolean,
): ProviderSystemColumnPresentation | null {
  const absentFromCanonical = compareWithCanonical && !canonicalNames.has(column.name)
  if (column.name === '_rowid') {
    if (compareWithCanonical && canonicalNames.has(column.name)) return null
    if (column.provenance === 'inferred' || absentFromCanonical) {
      return { column, label: 'System row ID', description: SYSTEM_ROW_ID_DESCRIPTION }
    }
  }
  if (!absentFromCanonical) return null
  return {
    column,
    label: 'System column',
    description: 'Supplied by the run preview; it is not part of the dataset schema.',
  }
}

type ProviderPlacementObservation = {
  placementId: string
  path: string
}

type ProviderPlacementObservations = {
  observe: (
    resources: WorkspaceResource[], ancestors?: WorkspaceResource[],
    evidence?: { current: boolean },
  ) => void
  alternatePlacements: (resource: WorkspaceResource) => ProviderPlacementObservation[]
  placementPath: (resource: WorkspaceResource) => string | null
  reset: () => void
}

const ProviderPlacementObservationsContext = createContext<ProviderPlacementObservations>({
  observe: () => undefined,
  alternatePlacements: () => [],
  placementPath: () => null,
  reset: () => undefined,
})

function providerPlacementId(resource: WorkspaceResource): string | null {
  return resource.providerPlacementId ?? null
}

function providerPlacementPathKey(resource: WorkspaceResource): string | null {
  const placementId = providerPlacementId(resource)
  return resource.mountId && placementId ? `${resource.mountId}\u0000${placementId}` : null
}

function providerCanonicalKey(resource: WorkspaceResource): string | null {
  return resource.mountId && resource.providerDatasetId
    ? `${resource.mountId}\u0000${resource.providerDatasetId}` : null
}

function useProviderPlacementObservations(): ProviderPlacementObservations {
  // This state belongs to this mounted Workspace only. It records returned browse/search/resolve
  // observations; it neither persists across Workspace lifetimes nor asks a provider for aliases.
  const canonicalObservations = useRef(new Map<string, Map<string, ProviderPlacementObservation>>())
  const placementPaths = useRef(new Map<string, string>())
  const [, setVersion] = useState(0)

  const placementPath = useCallback((resource: WorkspaceResource): string | null => (
    placementPaths.current.get(providerPlacementPathKey(resource) ?? '') ?? null
  ), [])

  const observe = useCallback((
    resources: WorkspaceResource[], ancestors: WorkspaceResource[] = [],
    evidence: { current: boolean } = { current: false },
  ) => {
    let changed = false
    // A resolution supplies the full ancestor chain. Observe it in order so a nested child can
    // reuse the complete path of its real parent instead of truncating to the current page name.
    const observed = [
      ...ancestors.map((ancestor, index) => ({ resource: ancestor, ancestors: ancestors.slice(0, index) })),
      ...resources.map((resource) => ({ resource, ancestors })),
    ]
    for (const { resource, ancestors: resourceAncestors } of observed) {
      if (!isExternal(resource)) continue
      const placementId = providerPlacementId(resource)
      const placementKey = providerPlacementPathKey(resource)
      if (!placementId || !placementKey) continue
      const canonicalKey = resource.kind === 'dataset' ? providerCanonicalKey(resource) : null
      const currentEvidence = evidence.current
        && resource.referenceState === 'current'
        && resource.canonicalReferenceState === 'current'
        && !resource.detached && !resource.lastKnown
      if (canonicalKey && !currentEvidence) {
        const existing = canonicalObservations.current.get(canonicalKey)
        if (existing?.delete(placementId)) {
          if (!existing.size) canonicalObservations.current.delete(canonicalKey)
          changed = true
        }
      }
      const directParent = resourceAncestors[resourceAncestors.length - 1]
      const parentPath = directParent && isExternal(directParent)
        ? placementPaths.current.get(providerPlacementPathKey(directParent) ?? '')
        : resource.mountId && resource.parentProviderPlacementId
        ? placementPaths.current.get(`${resource.mountId}\u0000${resource.parentProviderPlacementId}`)
        : undefined
      const visibleAncestors = resourceAncestors.filter(isExternal).map((item) => item.name)
      // A path is usable only when it came with named ancestors, from an already observed named
      // parent, or from a top-level provider placement. Search rows alone never invent one from
      // opaque placement/parent ids.
      const path = parentPath ? `${parentPath} / ${resource.name}`
        : visibleAncestors.length ? [...visibleAncestors, resource.name].join(' / ')
          : !resource.parentProviderPlacementId ? resource.name : null
      if (!path) continue
      placementPaths.current.delete(placementKey)
      placementPaths.current.set(placementKey, path)
      while (placementPaths.current.size > PROVIDER_PLACEMENT_CACHE_MAX_PATHS) {
        placementPaths.current.delete(placementPaths.current.keys().next().value!)
      }
      if (!canonicalKey || !currentEvidence) { changed = true; continue }
      let placements = canonicalObservations.current.get(canonicalKey)
      if (!placements) {
        if (canonicalObservations.current.size >= PROVIDER_PLACEMENT_CACHE_MAX_DATASETS) {
          canonicalObservations.current.delete(canonicalObservations.current.keys().next().value!)
        }
        placements = new Map()
      } else canonicalObservations.current.delete(canonicalKey)
      placements.delete(placementId)
      placements.set(placementId, { placementId, path })
      while (placements.size > PROVIDER_PLACEMENT_CACHE_MAX_PLACEMENTS) {
        placements.delete(placements.keys().next().value!)
      }
      canonicalObservations.current.set(canonicalKey, placements)
      changed = true
    }
    if (changed) setVersion((current) => current + 1)
  }, [])

  const alternatePlacements = useCallback((resource: WorkspaceResource) => {
    const canonicalKey = providerCanonicalKey(resource)
    const currentPlacement = providerPlacementId(resource)
    if (!canonicalKey || !currentPlacement) return []
    return [...(canonicalObservations.current.get(canonicalKey)?.values() ?? [])]
      .filter((placement) => placement.placementId !== currentPlacement)
  }, [])

  const reset = useCallback(() => {
    canonicalObservations.current.clear()
    placementPaths.current.clear()
  }, [])

  useEffect(() => reset, [reset])

  return useMemo(() => ({ observe, alternatePlacements, placementPath, reset }), [observe, alternatePlacements, placementPath, reset])
}

const WorkspaceOverflowMenuContext = createContext<{ openId: string | null; setOpenId: (id: string | null) => void }>({
  openId: null, setOpenId: () => undefined,
})

function WorkspaceOverflowMenuProvider({ children }: { children: ReactNode }) {
  const [openId, setOpenId] = useState<string | null>(null)
  return <WorkspaceOverflowMenuContext.Provider value={{ openId, setOpenId }}>{children}</WorkspaceOverflowMenuContext.Provider>
}

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)
const previewCell = (value: unknown) => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value)
const identity = (resource: WorkspaceResource) => resource.id.slice(resource.id.indexOf(':') + 1)
function ordinaryLocalSourcePath(uri: string): string | null {
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(uri) && !uri.toLowerCase().startsWith('file://')) return null
  try {
    const path = uri.toLowerCase().startsWith('file://') ? decodeURIComponent(new URL(uri).pathname) : uri
    return path && !path.toLowerCase().endsWith('.lance') ? path : null
  } catch {
    return null
  }
}
const isExternal = (resource: WorkspaceResource | null) => resource?.source === 'provider'
const isCatalogFolder = (resource: WorkspaceResource | null) => !!resource?.catalogFolderId
const isCurrentCatalogLocation = (resource: WorkspaceResource | null) => !!resource && !resource.detached
  && (identity(resource) === LOCAL_ROOT_ID || resource.catalogFolderState === 'current')
const isDetachedLocalPlacement = (resource: WorkspaceResource) => resource.detached && !isExternal(resource)
const hasDetachedDatasetRecovery = (resource: WorkspaceResource) => (
  isDetachedLocalPlacement(resource) && resource.kind === 'dataset'
)
function itemAvailability(resource: WorkspaceResource): {
  state: 'unavailable' | 'unsupported'; label: 'Unavailable' | 'Unsupported'; reason: string
} | null {
  const raw = resource.unavailableReason
  if (!raw) {
    if (!isDetachedLocalPlacement(resource)) return null
    const reason = resource.kind === 'dataset'
      ? 'The local dataset is no longer available. Open it to view recovery details.'
      : resource.kind === 'canvas'
        ? 'This Canvas is no longer available.'
        : resource.kind === 'container'
          ? 'This local folder is no longer available.'
          : 'This local item is no longer available.'
    return { state: 'unavailable', label: 'Unavailable', reason }
  }
  if (raw.startsWith('Unsupported: ')) {
    return { state: 'unsupported', label: 'Unsupported', reason: raw.slice('Unsupported: '.length) }
  }
  return {
    state: 'unavailable',
    label: 'Unavailable',
    reason: raw.startsWith('Unavailable: ') ? raw.slice('Unavailable: '.length) : raw,
  }
}
function folderDeleteMode(resource: WorkspaceResource): 'delete' | 'explain' | null {
  if (resource.canDeleteFolder) return 'delete'
  return resource.folderMutationUnavailableReason === NON_EMPTY_LOCAL_FOLDER_REASON ? 'explain' : null
}
type CanvasDestination = { containerId: string; expectedContainerVersion: number; externalOverlay: boolean }

// Provider containers expose a localPlacement capability rather than mutation authority.  This
// converts it to the exact opaque local destination required by the mutation API; public provider
// ids are deliberately never used for a Canvas create or move.
function canvasDestination(resource: WorkspaceResource | null, action: 'create' | 'move'): CanvasDestination | null {
  if (!resource || resource.detached) return null
  if (!isExternal(resource)) {
    return resource.version == null ? null : {
      containerId: identity(resource), expectedContainerVersion: resource.version, externalOverlay: false,
    }
  }
  const placement = resource.localPlacement
  const allowed = action === 'create' ? placement?.canCreateCanvas : placement?.canMoveCanvas
  if (!placement?.writable || !allowed || placement.recoveryState !== 'ready'
      || !placement.containerId || placement.containerVersion == null) return null
  return {
    containerId: placement.containerId,
    expectedContainerVersion: placement.containerVersion,
    externalOverlay: true,
  }
}

function canvasDestinationTitle(resource: WorkspaceResource | null, action: 'create' | 'move'): string {
  if (!resource) return 'Load a Workspace destination first'
  if (resource.detached) return isExternal(resource)
    ? 'This connected source is unavailable. Relink it before creating or moving a Canvas here'
    : 'Deleted Catalog folder tombstones do not accept new canvases'
  if (!isExternal(resource)) return resource.version == null ? 'Reload this Workspace destination first' : `Create in ${resource.name}`
  if (canvasDestination(resource, action)) return `Create a Canvas in ${resource.name}`
  if (resource.localPlacement?.recoveryState === 'unavailable') return 'This Canvas folder is unavailable; retry after the connected source recovers'
  return 'This connected source folder cannot contain Canvases'
}

function newRequestId(): string {
  return globalThis.crypto.randomUUID()
}
const statusMessage = (status: WorkspaceSourceStatus) => status.error
  ?? (status.completeness === 'unavailable' ? 'source is offline'
    : status.completeness === 'unsupported' ? 'browse is not supported'
      : status.completeness === 'partial' ? 'source returned partial results' : null)

function sourceCompletenessLabel(completeness: WorkspaceSourceStatus['completeness']): string {
  switch (completeness) {
    case 'complete': return 'Available'
    case 'page': return 'More available'
    case 'pending': return 'Loading results'
    case 'partial': return 'Some results unavailable'
    case 'unavailable': return 'Unavailable'
    case 'unsupported': return 'Browse unavailable'
  }
}

function sourceNeedsAttention(source: WorkspaceSourceStatus): boolean {
  return !!source.error || ['partial', 'unavailable', 'unsupported'].includes(source.completeness)
}

function sourceIsUsable(status: WorkspaceSourceStatus | null): boolean {
  return status?.completeness === 'complete' || status?.completeness === 'page'
}

export function WorkspaceExplorer() {
  const scope = useStore((state) => state.workspaceScope) ?? 'all'
  const setWorkspaceScope = useStore((state) => state.setWorkspaceScope)
  const firstRunChoice = useStore((state) => state.firstRunChoice)
  const requestedResourceId = useStore((state) => state.workspaceResourceId)
  const fullPageResource = requestedResourceId?.startsWith('dataset:')
    || requestedResourceId?.startsWith('dataset_view:')
  const providerPlacementObservations = useProviderPlacementObservations()
  const previousScope = useRef(scope)
  useEffect(() => {
    if (previousScope.current !== scope) providerPlacementObservations.reset()
    previousScope.current = scope
  }, [scope, providerPlacementObservations])
  // `scope=datasets` was an older second Workspace lens. Keep old links readable, but fold them
  // into the one file-browser surface instead of making people choose between two partial roots.
  useEffect(() => {
    if (scope === 'datasets') setWorkspaceScope('all')
  }, [scope, setWorkspaceScope])
  return <ProviderPlacementObservationsContext.Provider value={providerPlacementObservations}><WorkspaceOverflowMenuProvider><div className="relative flex h-full min-h-0 flex-col overflow-hidden">
    {!fullPageResource && firstRunChoice && <FirstRunCanvasChoice />}
    {!fullPageResource && <WorkspaceLocalDrafts />}
    <div className="min-h-0 flex-1"><WorkspaceMixedExplorer /></div>
  </div></WorkspaceOverflowMenuProvider></ProviderPlacementObservationsContext.Provider>
}

// A first-run choice belongs beside the Workspace, not in a separate tutorial surface: datasets
// remain discoverable and the two actions create exactly the Canvas the researcher selected.
function FirstRunCanvasChoice() {
  const newFile = useStore((state) => state.newFile)
  const newFromExample = useStore((state) => state.newFromExample)
  return (
    <section data-testid="first-run-canvas-choice" aria-labelledby="first-run-canvas-title"
      className="border-b border-border bg-card px-7 py-3">
      <div className="mx-auto max-w-5xl">
        <h2 id="first-run-canvas-title" className="text-[15px] font-semibold text-foreground">Create your first Canvas</h2>
        <p className="mt-0.5 max-w-2xl text-[12.5px] leading-snug text-muted-foreground">
          Start with an empty graph, or open a runnable example using the seeded sample data.
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          <button type="button" onClick={() => { void newFile() }}
            className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background">Start a blank Canvas</button>
        </div>
        <div className="mt-2 grid max-w-4xl gap-2 sm:grid-cols-3" role="group" aria-label="Runnable examples">
          {examples.map((example) => <button key={example.key} type="button"
            onClick={() => { void newFromExample(example.key) }} aria-label={`Open example ${example.name}`}
            className="rounded-md border border-border bg-background px-3 py-2 text-left transition-colors hover:border-primary/50 hover:bg-accent">
            <span className="block text-[12px] font-semibold text-foreground">{example.name}</span>
            <span className="mt-0.5 block text-[11px] leading-snug text-muted-foreground">{example.blurb}</span>
          </button>)}
        </div>
      </div>
    </section>
  )
}

// The explorer deliberately consumes the bounded Workspace API rather than composing a canvas list
// and catalog page in the browser. A resource URL is opaque and remains valid when its display name
// or placement changes; only containers are expanded locally, one page at a time.
function WorkspaceMixedExplorer() {
  const providerPlacementObservations = useContext(ProviderPlacementObservationsContext)
  const requestedResourceId = useStore((s) => s.workspaceResourceId)
  const setWorkspaceResource = useStore((s) => s.setWorkspaceResource)
  const searchQuery = useStore((s) => s.workspaceSearchQuery)
  const setWorkspaceSearchQuery = useStore((s) => s.setWorkspaceSearchQuery)
  const openFile = useStore((s) => s.openFile)
  const select = useStore((s) => s.select)
  const activateLoadedCanvasRoute = useStore((s) => s.activateLoadedCanvasRoute)
  const clearWorkspaceDatasetViewerState = useStore((s) => s.clearWorkspaceDatasetViewerState)
  const returnFromWorkspaceDatasetViewer = useStore((s) => s.returnFromWorkspaceDatasetViewer)
  const files = useStore((s) => s.files)
  const currentCanvasId = useStore((s) => s.doc?.id ?? '')
  const refreshFiles = useStore((s) => s.refreshFiles)
  const rememberTables = useStore((s) => s.rememberTables)
  const uploadDataset = useStore((s) => s.uploadDataset)
  const pushToast = useStore((s) => s.pushToast)
  const switchWorkspaceScope = useStore((s) => s.switchWorkspaceScope)
  const workspaceDatasetQuery = useStore((s) => s.workspaceDatasetQuery)
  const setWorkspaceDatasetQuery = useStore((s) => s.setWorkspaceDatasetQuery)
  const providerViewerRoute = useMemo(() => {
    const params = new URLSearchParams(workspaceDatasetQuery)
    const revisionId = params.get('revision') || undefined
    const datasetId = params.get('revisionDataset') || undefined
    return {
      exactRevision: revisionId && datasetId ? { revisionId, datasetId } : undefined,
      viewerReturn: parseDatasetViewerReturn(workspaceDatasetQuery),
    }
  }, [workspaceDatasetQuery])

  // A create response is the sole authority for this short-lived selection. Opening an existing
  // Canvas remains selection-neutral, and a multi-dataset create intentionally has no node id.
  const openCreatedSourceCanvas = async (canvasId: string, nodeId?: string | null) => {
    if (await openFile(canvasId) && nodeId) select(nodeId)
  }
  const [containerId, setContainerId] = useState(LOCAL_ROOT_ID)
  const [container, setContainer] = useState<WorkspaceResource | null>(null)
  const [crumbs, setCrumbs] = useState<WorkspaceResource[]>([])
  const [items, setItems] = useState<WorkspaceResource[]>([])
  const [connectedSources, setConnectedSources] = useState<WorkspaceResource[]>([])
  const [queryCapabilities, setQueryCapabilities] = useState<WorkspaceQueryCapabilities>(LOCAL_QUERY_CAPABILITIES)
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [pageCursors, setPageCursors] = useState<(string | null)[]>([null])
  const [pageIndex, setPageIndex] = useState(0)
  const [viewMode, setViewMode] = useState<'list' | 'grid'>('list')
  const [sortMode, setSortMode] = useState<'source' | 'name-asc' | 'name-desc' | 'updated-desc' | 'updated-asc'>('source')
  const [kindFilter, setKindFilter] = useState<'all' | 'container' | 'canvas' | 'dataset' | 'dataset_view'>('all')
  const [selectedResourceIds, setSelectedResourceIds] = useState<Set<string>>(new Set())
  const [completeness, setCompleteness] = useState<'complete' | 'page' | 'partial'>('complete')
  const [sources, setSources] = useState<WorkspaceSourceStatus[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null)
  const [selectedTable, setSelectedTable] = useState<CatalogTable | null>(null)
  const [selectedView, setSelectedView] = useState<DatasetViewDefinition | null>(null)
  const [selectedDataset, setSelectedDataset] = useState<WorkspaceResource | null>(null)
  const [selectedSource, setSelectedSource] = useState<WorkspaceSourceStatus | null>(null)
  const [selectedCanonicalSourceBinding, setSelectedCanonicalSourceBinding] = useState<{
    mountId: string; sourceBindingId: string
  } | null>(null)
  const [selectedProviderResource, setSelectedProviderResource] = useState<WorkspaceResource | null>(null)
  const [selectedDetached, setSelectedDetached] = useState<WorkspaceResource | null>(null)
  const [resolutionError, setResolutionError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [addDataOpen, setAddDataOpen] = useState(false)
  const [folderCreateParent, setFolderCreateParent] = useState<{ resource: WorkspaceResource; path: WorkspaceResource[] } | null>(null)
  const [folderRenameResource, setFolderRenameResource] = useState<{ resource: WorkspaceResource; path: WorkspaceResource[]; fromSearch?: boolean } | null>(null)
  const [folderDeleteResource, setFolderDeleteResource] = useState<{ resource: WorkspaceResource; path: WorkspaceResource[]; fromSearch?: boolean } | null>(null)
  const [canvasRenameResource, setCanvasRenameResource] = useState<WorkspaceResource | null>(null)
  const [canvasDeleteResource, setCanvasDeleteResource] = useState<WorkspaceResource | null>(null)
  const [canvasBatchDeleteResources, setCanvasBatchDeleteResources] = useState<WorkspaceResource[] | null>(null)
  const [datasetRemoveResource, setDatasetRemoveResource] = useState<WorkspaceResource | null>(null)
  const [canvasCopySource, setCanvasCopySource] = useState<CanvasCopySource | null>(null)
  const [datasetAction, setDatasetAction] = useState<{ tables: CatalogTable[] } | null>(null)
  const [providerDatasetAction, setProviderDatasetAction] = useState<WorkspaceResource | null>(null)
  const [canvasTargetState, setCanvasTargetState] = useState<CanvasTargetState>('loading')
  const canvasTargetRequest = useRef(0)
  const [moveResource, setMoveResource] = useState<{
    resources: WorkspaceResource[]; sourceContainer: WorkspaceResource; sourcePath: WorkspaceResource[]
  } | null>(null)
  const [draggedCanvases, setDraggedCanvases] = useState<WorkspaceResource[] | null>(null)
  const [dropTargetId, setDropTargetId] = useState<string | null>(null)
  const [dropBusy, setDropBusy] = useState(false)
  const [relinkResource, setRelinkResource] = useState<WorkspaceResource | null>(null)
  const [undoMove, setUndoMove] = useState<{
    resource: WorkspaceResource; previousContainer: WorkspaceResource; destination: WorkspaceResource
    destinationPath: WorkspaceResource[]
  } | null>(null)
  const [undoBusy, setUndoBusy] = useState(false)
  const [revision, setRevision] = useState(0)
  const [searchDraft, setSearchDraft] = useState(searchQuery)
  const request = useRef(0)
  const loadedContainer = useRef<string | null>(null)
  const selectionRequest = useRef<string | null>(null)
  const selectionContainer = useRef<WorkspaceResource | null>(null)

  useEffect(() => { setSearchDraft(searchQuery) }, [searchQuery])

  // Search is an explicit, bounded action. Keep the text being edited separate from the query
  // whose results are currently on screen so an old result set is never implied to match a new
  // draft merely because the input changed.
  const normalizedSearchDraft = searchDraft.trim().replace(/\s+/g, ' ')
  const searchDraftPending = normalizedSearchDraft !== searchQuery

  const load = useCallback(async (
    targetId: string, pageCursor: string | null = null, targetPage = 0,
  ) => {
    const sequence = ++request.current
    const paging = pageCursor !== null || targetPage > 0
    const sameContainer = targetId === loadedContainer.current
    if (paging) { setLoadingMore(true); setLoadMoreError(null) }
    else {
      setLoading(true); setError(null); setLoadMoreError(null)
      // Keep a resolved location visible while it refreshes. Provider refreshes may return an honest
      // partial/offline page with no container; clearing here would hide the selected resource and
      // its ancestors even though their stable identity has not changed.
      if (targetId !== loadedContainer.current) {
        setItems([]); setConnectedSources([]); setCursor(null); setHasMore(false); setSources([])
      }
    }
    try {
      const localSource = !isProviderBrowseIdentity(targetId)
      const [sort, order] = sortMode === 'source' ? [undefined, undefined]
        : sortMode.split('-') as ['name' | 'updated', 'asc' | 'desc']
      const params: Parameters<typeof api.workspaceBrowse>[1] = {
        limit: PAGE_SIZE,
        cursor: pageCursor ?? undefined,
      }
      if (localSource && sort && order) {
        params.sort = sort
        params.order = order
      }
      if (localSource && kindFilter !== 'all') params.kinds = [kindFilter]
      const page = await api.workspaceBrowse(targetId, params)
      if (sequence !== request.current) return
      setCompleteness(page.completeness)
      setSources(page.sources ?? [])
      setConnectedSources(page.connectedSources ?? [])
      const capabilities = page.queryCapabilities ?? (localSource
        ? LOCAL_QUERY_CAPABILITIES
        : { sort: [], kindFilter: false, reason: "This source controls the order of its results. Sorting and type filters aren't available here." })
      setQueryCapabilities(capabilities)
      if (!capabilities.sort.length && sortMode !== 'source') setSortMode('source')
      if (!capabilities.kindFilter && kindFilter !== 'all') setKindFilter('all')
      if (!page.container) {
        const unavailable = page.sources?.map(statusMessage).find(Boolean)
          ?? 'Workspace source is unavailable'
        if (targetId !== loadedContainer.current) setError(unavailable)
        return
      }
      providerPlacementObservations.observe(page.items, [page.container], { current: true })
      setContainerId(identity(page.container))
      loadedContainer.current = identity(page.container)
      setContainer(page.container)
      if (!paging) setCrumbs((current) => current.length && current[current.length - 1].id === page.container!.id ? current : [...current, page.container!])
      setItems(page.items)
      setSelectedResourceIds(new Set())
      setPageIndex(targetPage)
      setPageCursors((current) => {
        const next = sameContainer && targetPage > 0 ? current.slice(0, targetPage + 1) : [null]
        next[targetPage] = pageCursor
        if (page.nextCursor) next[targetPage + 1] = page.nextCursor
        return next
      })
      setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
    } catch (caught) {
      if (sequence !== request.current) return
      if (paging) setLoadMoreError(errorMessage(caught))
      else setError(errorMessage(caught))
    } finally {
      if (sequence === request.current) { setLoading(false); setLoadingMore(false) }
    }
  }, [providerPlacementObservations, sortMode, kindFilter])

  useEffect(() => {
    let cancelled = false
    const resolve = async () => {
      setResolutionError(null)
      const refreshingSelection = selectionRequest.current === requestedResourceId
      if (!refreshingSelection) {
        selectionRequest.current = requestedResourceId
        selectionContainer.current = null
        setSelectedTable(null); setSelectedView(null); setSelectedDataset(null); setSelectedSource(null); setSelectedCanonicalSourceBinding(null); setSelectedDetached(null); setSelectedProviderResource(null)
      }
      if (!requestedResourceId) {
        selectionContainer.current = null
        setCrumbs([])
        if (searchQuery) { setLoading(false); return }
        await load(LOCAL_ROOT_ID)
        return
      }
      try {
        const resolved = await api.workspaceResource(requestedResourceId)
        if (cancelled) return
        if (!resolved.resource) {
          setResolutionError(statusMessage(resolved.source) ?? 'Workspace resource is unavailable')
          setLoading(false)
          return
        }
        providerPlacementObservations.observe(
          [resolved.resource], resolved.ancestors,
          { current: resolved.source.completeness === 'complete' },
        )
        const resolvedContainer = resolved.resource.kind === 'container'
          ? resolved.resource
          : resolved.ancestors[resolved.ancestors.length - 1]
        if (!resolvedContainer) throw new Error('Workspace resource has no container')
        const preserveNavigation = refreshingSelection && resolved.source.completeness !== 'complete'
        const container = preserveNavigation ? selectionContainer.current ?? resolvedContainer : resolvedContainer
        if (!preserveNavigation) selectionContainer.current = resolvedContainer
        setSelectedProviderResource(isExternal(resolved.resource) ? resolved.resource : null)
        setSelectedCanonicalSourceBinding(resolved.canonicalSourceBinding ?? null)
        const resolvedCrumbs = resolved.resource.kind === 'container'
          ? [...resolved.ancestors, resolved.resource]
          : resolved.ancestors
        if (resolved.source.completeness === 'complete' || !refreshingSelection) setCrumbs(resolvedCrumbs)
        else setCrumbs((current) => current.length ? current : resolvedCrumbs)
        if (searchQuery) {
          setContainerId(identity(container))
          loadedContainer.current = identity(container)
          setContainer(container)
          setLoading(false)
        } else await load(identity(container))
        if (cancelled) return
        if (resolved.resource.kind === 'dataset_view') {
          setSelectedTable(null); setSelectedDataset(null); setSelectedSource(null); setSelectedDetached(null)
          try {
            const view = await api.datasetView(identity(resolved.resource))
            if (!cancelled) setSelectedView(view)
          } catch (caught) {
            if (!cancelled) setResolutionError(errorMessage(caught))
          }
          return
        }
        setSelectedView(null)
        if (resolved.resource.kind !== 'dataset') {
          setSelectedTable(null); setSelectedDataset(null); setSelectedSource(null); setSelectedDetached(null)
          if (resolved.source.completeness !== 'complete') {
            setResolutionError(statusMessage(resolved.source) ?? 'Workspace path is partial')
          }
          return
        }
        setSelectedDataset(resolved.resource)
        setSelectedSource(resolved.source)
        if (isExternal(resolved.resource)) {
          setSelectedTable(null); setSelectedDetached(null)
          if (resolved.source.completeness !== 'complete') {
            setResolutionError(statusMessage(resolved.source) ?? 'Workspace path is partial')
          }
          return
        }
        if (resolved.resource.detached) { setSelectedTable(null); setSelectedDetached(resolved.resource); return }
        try {
          setSelectedTable(null); setSelectedDetached(null)
          setSelectedTable(await api.tableByRegistration(identity(resolved.resource)))
        }
        catch (caught) {
          if (cancelled) return
          const status = typeof caught === 'object' && caught !== null
            ? (caught as { status?: unknown }).status
            : undefined
          if (status === 404) setSelectedDetached({ ...resolved.resource, detached: true })
          else { setError(errorMessage(caught)); setItems([]); setHasMore(false) }
        }
      } catch (caught) {
        if (!cancelled) {
          setResolutionError(errorMessage(caught))
          setLoading(false)
        }
      }
    }
    void resolve()
    return () => { cancelled = true; request.current += 1 }
  }, [requestedResourceId, searchQuery, load, revision, providerPlacementObservations])

  const open = (resource: WorkspaceResource) => {
    if (itemAvailability(resource) && !hasDetachedDatasetRecovery(resource)) return
    if (resource.kind === 'canvas') { void openFile(identity(resource)); return }
    if (resource.kind === 'dataset'
        && (providerViewerRoute.exactRevision || providerViewerRoute.viewerReturn)) {
      setWorkspaceDatasetQuery('')
    }
    setWorkspaceResource(resource.id)
  }
  const closeDetail = () => {
    const viewerReturn = providerViewerRoute.viewerReturn
    if (viewerReturn && viewerReturn.view !== 'canvas') {
      returnFromWorkspaceDatasetViewer(viewerReturn.view, viewerReturn.query ?? '', '')
      return
    }
    if (!viewerReturn) {
      if (providerViewerRoute.exactRevision) {
        switchWorkspaceScope('all', { resourceId: `container:${containerId}`, datasetQuery: '' })
        return
      }
      setWorkspaceResource(`container:${containerId}`)
      return
    }
    const canvasReturn = viewerReturn
    if (currentCanvasId === canvasReturn.canvasId
        && activateLoadedCanvasRoute(canvasReturn.canvasId, canvasReturn.nodeId)) {
      clearWorkspaceDatasetViewerState('')
      return
    }
    void openFile(canvasReturn.canvasId, { skipViewportFit: true }).then((opened) => {
      if (!opened || !activateLoadedCanvasRoute(canvasReturn.canvasId, canvasReturn.nodeId)) return
      clearWorkspaceDatasetViewerState('')
    })
  }
  const useTable = (table: CatalogTable) => {
    if (!selectedDataset) {
      pushToast('Could not resolve the stable Workspace dataset identity', 'error')
      return
    }
    rememberTables([table])
    const request = ++canvasTargetRequest.current
    setCanvasTargetState('loading')
    void refreshFiles().then((refreshed) => {
      if (canvasTargetRequest.current === request) setCanvasTargetState(refreshed ? 'ready' : 'unavailable')
    })
    setDatasetAction({ tables: [{ ...table, registrationId: identity(selectedDataset) }] })
  }
  const useProviderDataset = (resource: WorkspaceResource) => {
    const request = ++canvasTargetRequest.current
    setCanvasTargetState('loading')
    setProviderDatasetAction(resource)
    void refreshFiles().then((refreshed) => {
      if (canvasTargetRequest.current === request) setCanvasTargetState(refreshed ? 'ready' : 'unavailable')
    })
  }
  // Re-resolve the stable resource before reloading. This keeps rename/move refreshes truthful and
  // retries the same deep link rather than silently falling back to a different container.
  const reload = () => setRevision((current) => current + 1)
  const searchActionRequest = useRef(0)
  useEffect(() => () => { searchActionRequest.current += 1 }, [searchQuery])
  const startSearchAction = async (resource: WorkspaceResource, action: 'new-folder' | 'rename-folder' | 'delete-folder' | 'rename-canvas' | 'move-canvas' | 'delete-canvas' | 'remove-dataset') => {
    const sequence = ++searchActionRequest.current
    try {
      const resolved = await api.workspaceResource(resource.id)
      if (sequence !== searchActionRequest.current || !resolved.resource) return
      const exact = resolved.resource
      const path = exact.kind === 'container' ? [...resolved.ancestors, exact] : resolved.ancestors
      const editableCanvas = exact.kind === 'canvas' && !exact.detached
        && ['owner', 'editor'].includes(files.find((file) => file.id === identity(exact))?.role ?? '')
      if (action === 'new-folder' && exact.kind === 'container' && exact.canCreateFolder) {
        setFolderCreateParent({ resource: exact, path })
      } else if (action === 'rename-folder' && exact.kind === 'container' && exact.canRenameFolder) {
        setFolderRenameResource({ resource: exact, path, fromSearch: true })
      } else if (action === 'delete-folder' && exact.kind === 'container' && folderDeleteMode(exact)) {
        setFolderDeleteResource({ resource: exact, path, fromSearch: true })
      } else if (action === 'rename-canvas' && editableCanvas) {
        setCanvasRenameResource(exact)
      } else if (action === 'move-canvas' && editableCanvas) {
        const sourceContainer = resolved.ancestors[resolved.ancestors.length - 1]
        if (sourceContainer) setMoveResource({ resources: [exact], sourceContainer, sourcePath: path })
      } else if (action === 'delete-canvas' && exact.kind === 'canvas'
        && files.find((file) => file.id === identity(exact))?.role === 'owner') {
        setCanvasDeleteResource(exact)
      } else if (action === 'remove-dataset' && exact.kind === 'dataset'
        && !exact.detached && (!isExternal(exact) || exact.providerMutation)) {
        setDatasetRemoveResource(exact)
      }
    } catch (caught) {
      if (sequence === searchActionRequest.current) pushToast(`Could not load this search result's actions: ${errorMessage(caught)}`, 'error')
    }
  }
  const undoLastMove = async () => {
    const destination = canvasDestination(undoMove?.previousContainer ?? null, 'move')
    if (!undoMove?.resource.placementId || undoMove.resource.version == null || !destination) return
    setUndoBusy(true)
    try {
      await api.workspaceBatch({
        action: 'move',
        items: [{ placementId: undoMove.resource.placementId, expectedVersion: undoMove.resource.version }],
        containerId: destination.containerId,
        expectedContainerVersion: destination.expectedContainerVersion,
      })
      setUndoMove(null)
      pushToast('Canvas move undone', 'success')
      reload()
    } catch (caught) {
      pushToast(`Could not undo move: ${errorMessage(caught)}`, 'error')
    } finally { setUndoBusy(false) }
  }
  const undoDestination = undoMove ? canvasDestination(undoMove.previousContainer, 'move') : null
  const selectedResources = items.filter((resource) => selectedResourceIds.has(resource.id))
  const sortSupported = queryCapabilities.sort.length > 0
  const kindFilterSupported = queryCapabilities.kindFilter
  const visibleConnectedSources = kindFilter === 'all' || kindFilter === 'container'
    ? connectedSources : []
  const singleSelectedResource = selectedResources.length === 1 ? selectedResources[0] ?? null : null
  const canvasRole = (resource: WorkspaceResource) => (
    files.find((file) => file.id === identity(resource))?.role ?? ''
  )
  const editableCanvas = (resource: WorkspaceResource) => resource.kind === 'canvas'
    && !isExternal(resource) && !resource.detached && ['owner', 'editor'].includes(canvasRole(resource))
  const ownedCanvas = (resource: WorkspaceResource) => editableCanvas(resource) && canvasRole(resource) === 'owner'
  const selectedEditableCanvases = selectedResources.length > 0 && selectedResources.every(editableCanvas)
    ? selectedResources : []
  const selectedOwnedCanvases = selectedResources.length > 0 && selectedResources.every(ownedCanvas)
    ? selectedResources : []
  const singleRemovableDataset = singleSelectedResource?.kind === 'dataset'
    && !singleSelectedResource.detached
    && (!isExternal(singleSelectedResource) || singleSelectedResource.providerMutation)
    ? singleSelectedResource : null
  const toggleResourceSelection = (resourceId: string) => setSelectedResourceIds((current) => {
    const next = new Set(current)
    if (next.has(resourceId)) next.delete(resourceId)
    else next.add(resourceId)
    return next
  })
  const startDuplicate = async (resource: WorkspaceResource) => {
    try {
      const doc = await api.getCanvas(identity(resource))
      setCanvasCopySource({ canvasId: doc.id, version: doc.version, name: doc.name || resource.name })
    } catch (caught) {
      pushToast(`Could not prepare this Canvas copy: ${errorMessage(caught)}`, 'error')
    }
  }
  const deleteSelection = () => {
    if (selectedOwnedCanvases.length === 1) setCanvasDeleteResource(selectedOwnedCanvases[0] ?? null)
    else if (selectedOwnedCanvases.length > 1) setCanvasBatchDeleteResources(selectedOwnedCanvases)
  }
  const dropCanvasInto = async (destinationResource: WorkspaceResource) => {
    const resources = draggedCanvases ?? []
    const destination = canvasDestination(destinationResource, 'move')
    setDropTargetId(null)
    setDraggedCanvases(null)
    if (!resources.length || !destination || dropBusy
        || resources.some((resource) => !resource.placementId || resource.version == null
          || resource.parentId === destinationResource.id)) return
    setDropBusy(true)
    try {
      const result = await api.workspaceBatch({
        action: 'move',
        items: resources.map((resource) => ({
          placementId: resource.placementId!, expectedVersion: resource.version!,
        })),
        containerId: destination.containerId,
        expectedContainerVersion: destination.expectedContainerVersion,
      })
      if (result.items.length !== resources.length || !container) {
        throw new Error('Workspace move did not return every moved Canvas')
      }
      const moved = result.items[0]
      setUndoMove(resources.length === 1 && moved ? {
        resource: moved,
        previousContainer: container,
        destination: result.container ?? destinationResource,
        destinationPath: [...crumbs, destinationResource],
      } : null)
      setSelectedResourceIds(new Set())
      pushToast(resources.length === 1
        ? `Moved “${resources[0]?.name}” to “${destinationResource.name}”.`
        : `Moved ${resources.length} Canvases to “${destinationResource.name}”.`, 'success')
      reload()
    } catch (caught) {
      pushToast(resources.length === 1
        ? `Could not move “${resources[0]?.name}”: ${errorMessage(caught)}`
        : `Could not move ${resources.length} Canvases: ${errorMessage(caught)}`, 'error')
    } finally {
      setDropBusy(false)
    }
  }
  const openLineageDataset = async (catalogId: string) => {
    try {
      const table = await api.table(catalogId)
      rememberTables([table])
      setWorkspaceResource(`dataset:${table.registrationId ?? table.id}`)
    } catch (caught) {
      pushToast(`Couldn't open linked dataset: ${errorMessage(caught)}`, 'error')
    }
  }
  const providerActionDialog = providerDatasetAction ? <ProviderDatasetActionDialog
    resource={providerDatasetAction}
    container={container}
    files={files}
    currentCanvasId={currentCanvasId}
    targetState={canvasTargetState}
    onClose={() => setProviderDatasetAction(null)}
    onRefreshCanvases={refreshFiles}
    onOpened={(canvasId, nodeId) => {
      setProviderDatasetAction(null); setSelectedDataset(null); void openCreatedSourceCanvas(canvasId, nodeId)
    }} /> : null
  const relinkDialog = relinkResource ? <RelinkResourceDialog
    resource={relinkResource}
    onClose={() => setRelinkResource(null)}
    onRelinked={(resource) => {
      setRelinkResource(null)
      pushToast(`Relinked to ${resource.name}`, 'success')
      setWorkspaceResource(resource.id)
    }} /> : null
  const datasetActionDialog = datasetAction && container?.version != null ? <DatasetActionDialog
    action={datasetAction} container={container}
    files={files} currentCanvasId={currentCanvasId} targetState={canvasTargetState}
    onClose={() => setDatasetAction(null)}
    onRefreshCanvases={refreshFiles}
    onOpened={(canvasId, nodeId) => {
      setDatasetAction(null); setSelectedTable(null); setSelectedDataset(null)
      void openCreatedSourceCanvas(canvasId, nodeId)
    }} /> : null

  if (selectedDataset && isExternal(selectedDataset)) return <>
    <ExternalDatasetDetail resource={selectedDataset} source={selectedSource}
      canonicalSourceBinding={selectedCanonicalSourceBinding} onClose={closeDetail} onRetry={reload}
      exactRevision={providerViewerRoute.exactRevision}
      backLabel={datasetViewerBackLabel(providerViewerRoute.viewerReturn)}
      onUse={() => useProviderDataset(selectedDataset)}
      onOpenLineageDataset={(catalogId) => void openLineageDataset(catalogId)}
      onRelink={() => setRelinkResource(selectedDataset)}
      onRemove={selectedDataset.providerMutation ? () => setDatasetRemoveResource(selectedDataset) : undefined} />
    {providerActionDialog}
    {relinkDialog}
    {datasetRemoveResource && <ProviderDatasetRemoveDialog resource={datasetRemoveResource}
      onClose={() => setDatasetRemoveResource(null)} onRemoved={() => {
        setDatasetRemoveResource(null); setSelectedDataset(null); setWorkspaceResource(null); reload()
        pushToast('Dataset removed from its connected source', 'success')
      }} />}
  </>

  if (selectedTable) return <>
    <CatalogDetail table={selectedTable} onClose={closeDetail} onUse={useTable}
      initialRevisionId={providerViewerRoute.exactRevision?.revisionId}
      initialRevisionDatasetId={providerViewerRoute.exactRevision?.datasetId}
      backLabel={datasetViewerBackLabel(providerViewerRoute.viewerReturn)}
      onChanged={(table) => { setSelectedTable(table); void load(containerId) }} onDeleted={closeDetail}
      folderActionLabel="Open in Workspace"
      folderActionVisible
      folderActionDisabled={!isCurrentCatalogLocation(container)}
      folderActionTitle={!isCurrentCatalogLocation(container)
        ? 'This dataset is not currently available in Workspace.' : undefined}
      onOpenTable={setSelectedTable} onFolder={() => {
        if (container?.kind === 'container' && isCurrentCatalogLocation(container)) {
          setWorkspaceResource(identity(container) === LOCAL_ROOT_ID ? null : container.id)
        } else pushToast('This dataset is not currently available in Workspace.', 'error')
      }}
      onColumn={() => pushToast('Column filters are available from the dataset detail only.', 'info')} />
    {datasetActionDialog}
  </>

  return (
    <div className="flex h-full min-w-0 flex-col">
      <header className="flex min-h-[60px] items-center gap-3 border-b border-border px-7 py-2.5">
        <nav aria-label="Workspace path" className="flex min-w-0 items-center gap-1.5 overflow-hidden text-muted-foreground">
          <button onClick={() => setWorkspaceResource(null)} className="shrink-0 text-[20px] font-bold text-foreground hover:text-primary">Workspace</button>
          {crumbs.slice(1).map((crumb) => <span key={crumb.id} className="flex min-w-0 items-center gap-1.5 text-[12px]"><span>/</span><button disabled={!!itemAvailability(crumb)} onClick={() => setWorkspaceResource(crumb.id)} className="truncate hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60">{crumb.name}</button></span>)}
        </nav>
        <span className="flex-1" />
        <form aria-label="Workspace search" onSubmit={(event) => {
          event.preventDefault()
          if (searchDraftPending) setWorkspaceSearchQuery(searchDraft)
        }} className="flex min-w-[220px] max-w-sm flex-1 items-center gap-1 rounded-md border border-border bg-card px-2">
          <Icon name="search" size={13} />
          <input aria-label="Search views, datasets, canvases, and containers" value={searchDraft}
            onChange={(event) => setSearchDraft(event.target.value)} placeholder="Search Workspace"
            className="min-w-0 flex-1 bg-transparent py-1.5 text-[12px] outline-none" />
          {searchDraft && <button type="button" aria-label="Clear Workspace search" onClick={() => {
            setSearchDraft(''); setWorkspaceSearchQuery('')
          }}><Icon name="close" size={12} /></button>}
          <button type="submit" disabled={!searchDraftPending} aria-label="Search Workspace"
            className="rounded px-1.5 py-1 text-[11.5px] font-semibold text-primary hover:bg-primary/10 disabled:cursor-not-allowed disabled:text-muted-foreground disabled:hover:bg-transparent">Search</button>
        </form>
        <button type="button" onClick={() => setAddDataOpen(true)} data-testid="workspace-add-data"
          className="rounded-md bg-foreground px-2.5 py-1.5 text-[12px] font-semibold text-background">Add data</button>
        <div className="hidden items-center gap-2 sm:flex" aria-label="Workspace actions">
          {container?.canCreateFolder && <button onClick={() => setFolderCreateParent({ resource: container, path: crumbs })} disabled={loading}
            className="rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-semibold text-foreground disabled:text-muted-foreground disabled:opacity-65">New folder</button>}
          <button onClick={() => setCreateOpen(true)} disabled={!canvasDestination(container, 'create') || loading}
            title={canvasDestinationTitle(container, 'create')}
            className="rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-semibold text-foreground disabled:text-muted-foreground disabled:opacity-65">Create canvas</button>
        </div>
        <button onClick={reload} disabled={loading || loadingMore} data-testid="workspace-reload" className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-semibold text-foreground disabled:opacity-50">
          <Icon name="refresh" size={13} /> Reload
        </button>
      </header>

      {undoMove && <div role="status" className="flex items-center gap-2 border-b border-border bg-primary/5 px-7 py-2 text-[12px] text-foreground">
        <span className="flex-1">Moved “{undoMove.resource.name}” to {breadcrumb(undoMove.destinationPath)}.{!undoDestination && ' Its previous connected source folder is unavailable; recover or relink it before undoing.'}</span>
        <button onClick={() => void undoLastMove()} disabled={undoBusy || !undoDestination}
          title={!undoDestination ? canvasDestinationTitle(undoMove.previousContainer, 'move') : undefined}
          className="font-semibold text-primary underline disabled:opacity-50">{undoBusy ? 'Undoing…' : undoDestination ? 'Undo move' : 'Undo unavailable'}</button>
        <button onClick={() => setUndoMove(null)} aria-label="Dismiss move confirmation"><Icon name="close" size={13} /></button>
      </div>}

      {searchDraftPending && <div role="status" className="border-b border-border bg-muted/30 px-7 py-1.5 text-[11.5px] text-muted-foreground">
        {searchQuery
          ? <>Results are still for “{searchQuery}”. Select Search to update.</>
          : <>Select Search to look for “{normalizedSearchDraft}”.</>}
      </div>}

      {!searchQuery && (sources.some(sourceNeedsAttention) || completeness === 'partial')
        && <SourceStatusBar sources={sources} completeness={completeness} />}
      {resolutionError && <div role="alert" className="flex items-center gap-3 border-b border-amber-300/50 bg-amber-50 px-7 py-2 text-[12px] text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
        <span className="min-w-0 flex-1 truncate">This selection could not be fully refreshed: {resolutionError}</span>
        <button onClick={reload} disabled={loading} className="shrink-0 font-semibold underline disabled:opacity-50">Retry</button>
        {selectedProviderResource && <button onClick={() => setRelinkResource(selectedProviderResource)} className="shrink-0 font-semibold underline">Relink</button>}
      </div>}

      {!searchQuery && !loading && <div className="flex min-h-10 flex-wrap items-center gap-2 border-b border-border bg-card px-7 py-1.5 text-[12px]">
        <select aria-label="Sort Workspace" value={sortMode} onChange={(event) => {
          setSortMode(event.target.value as typeof sortMode)
          setSelectedResourceIds(new Set())
        }} disabled={!sortSupported} title={!sortSupported ? queryCapabilities.reason ?? undefined : undefined}
          className="rounded-md border border-border bg-background px-2 py-1 text-[11px] text-foreground disabled:cursor-not-allowed disabled:opacity-55">
          <option value="source">{sortSupported ? 'Default order' : 'Source order'}</option>
          <option value="name-asc">Name A–Z</option>
          <option value="name-desc">Name Z–A</option>
          <option value="updated-desc">Recently updated</option>
          <option value="updated-asc">Least recently updated</option>
        </select>
        <select aria-label="Filter Workspace by type" value={kindFilter} onChange={(event) => {
          setKindFilter(event.target.value as typeof kindFilter)
          setSelectedResourceIds(new Set())
        }} disabled={!kindFilterSupported} title={!kindFilterSupported ? queryCapabilities.reason ?? undefined : undefined}
          className="rounded-md border border-border bg-background px-2 py-1 text-[11px] text-foreground disabled:cursor-not-allowed disabled:opacity-55">
          <option value="all">All types</option>
          <option value="container">Folders</option>
          <option value="canvas">Canvases</option>
          <option value="dataset">Datasets</option>
          <option value="dataset_view">Saved views</option>
        </select>
        {(!sortSupported || !kindFilterSupported) && queryCapabilities.reason
          && <span data-testid="workspace-query-capability-note"
            className="min-w-[240px] max-w-[560px] flex-1 text-[11px] leading-snug text-muted-foreground">{queryCapabilities.reason}</span>}
        {error && (sortMode !== 'source' || kindFilter !== 'all') && <button type="button" onClick={() => {
          setSortMode('source')
          setKindFilter('all')
          setSelectedResourceIds(new Set())
        }} className="rounded-md border border-border px-2 py-1 font-semibold text-primary hover:bg-accent">Reset view</button>}
        {!error && <>
        <label className="inline-flex cursor-pointer items-center gap-2 text-muted-foreground">
          <input type="checkbox" aria-label="Select this page"
            checked={items.length > 0 && selectedResourceIds.size === items.length}
            onChange={(event) => setSelectedResourceIds(event.target.checked ? new Set(items.map((item) => item.id)) : new Set())}
            className="h-3.5 w-3.5 accent-primary" />
          {selectedResources.length ? `${selectedResources.length} selected` : 'Select page'}
        </label>
        {selectedResources.length > 0 && <>
          {singleSelectedResource && <button type="button" onClick={() => open(singleSelectedResource)}
            className="rounded-md border border-border px-2 py-1 font-semibold text-foreground hover:bg-accent">Open</button>}
          {singleSelectedResource && editableCanvas(singleSelectedResource) && <>
            <button type="button" onClick={() => setCanvasRenameResource(singleSelectedResource)} className="rounded-md border border-border px-2 py-1 font-semibold text-foreground hover:bg-accent">Rename</button>
            <button type="button" onClick={() => void startDuplicate(singleSelectedResource)} className="rounded-md border border-border px-2 py-1 font-semibold text-foreground hover:bg-accent">Duplicate</button>
          </>}
          {selectedEditableCanvases.length > 0 && <button type="button"
            onClick={() => container && setMoveResource({ resources: selectedEditableCanvases, sourceContainer: container, sourcePath: crumbs })}
            className="rounded-md border border-border px-2 py-1 font-semibold text-foreground hover:bg-accent">Move</button>}
          {selectedOwnedCanvases.length > 0 && <button type="button" onClick={deleteSelection}
            className="rounded-md border border-border px-2 py-1 font-semibold text-destructive hover:bg-destructive/5">Delete</button>}
          {singleRemovableDataset && <button type="button" onClick={() => setDatasetRemoveResource(singleRemovableDataset)}
            className="rounded-md border border-border px-2 py-1 font-semibold text-destructive hover:bg-destructive/5">Remove dataset</button>}
          <button type="button" onClick={() => setSelectedResourceIds(new Set())} className="px-2 py-1 text-muted-foreground hover:text-foreground">Clear</button>
        </>}
        </>}
        <span className="flex-1" />
        <div role="group" aria-label="Workspace view" className="flex rounded-md border border-border bg-background p-0.5">
          {(['list', 'grid'] as const).map((mode) => <button key={mode} type="button" aria-pressed={viewMode === mode}
            onClick={() => setViewMode(mode)} className={`rounded px-2 py-1 text-[11px] font-semibold capitalize ${viewMode === mode ? 'bg-accent text-foreground' : 'text-muted-foreground hover:text-foreground'}`}>{mode}</button>)}
        </div>
      </div>}

      <ContextMenu>
      <ContextMenuTrigger asChild>
      <div data-testid="workspace-scroll-surface" className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        {searchQuery ? <WorkspaceSearchResults query={searchQuery} revision={revision} onOpen={open}
          onAction={startSearchAction} files={files} /> : error ? <div role="alert" className="mx-auto flex max-w-md flex-col items-center gap-2 rounded-lg border border-destructive/30 p-5 text-center text-[13px] text-destructive">
          <span>Couldn't load this Workspace location: {error}</span>
          <button onClick={reload} className="font-semibold underline">Retry</button>
        </div> : loading ? <div className="grid h-full place-items-center text-[13px] text-muted-foreground">Loading Workspace…</div> : <div className="mx-auto flex min-h-full w-full max-w-[1600px] flex-col">
          {visibleConnectedSources.length > 0 && <section aria-label="Connected sources" className="mb-4 border-b border-border pb-4">
            <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Connected sources</h2>
            <div className={viewMode === 'grid' ? 'grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3' : 'grid gap-1'}>
              {visibleConnectedSources.map((resource) => <ResourceRow key={resource.id} resource={resource}
                onOpen={() => open(resource)} onRetry={reload} viewMode={viewMode} />)}
            </div>
          </section>}
          {items.length ? <div className={viewMode === 'grid' ? 'grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3' : 'grid gap-1'}>
            {items.map((resource) => {
              const rowSelected = selectedResourceIds.has(resource.id)
              const groupSelected = rowSelected && selectedResources.length > 1
              return <ResourceRow key={resource.id} resource={resource} onOpen={() => open(resource)}
              viewMode={viewMode} selected={rowSelected} contextSelectionCount={groupSelected ? selectedResources.length : 1}
              onToggleSelect={() => toggleResourceSelection(resource.id)}
              onContextSelect={() => setSelectedResourceIds(new Set([resource.id]))}
              draggable={editableCanvas(resource) && !dropBusy}
              onDragStart={(event) => {
                setDraggedCanvases(selectedResourceIds.has(resource.id)
                  && selectedEditableCanvases.length > 0 ? selectedEditableCanvases : [resource])
                event.dataTransfer.effectAllowed = 'move'
                event.dataTransfer.setData('application/x-data-playground-workspace-canvas', resource.id)
              }}
              onDragEnd={() => { setDraggedCanvases(null); setDropTargetId(null) }}
              dropTarget={resource.kind === 'container' && dropTargetId === resource.id}
              dropTargetLabel={draggedCanvases && draggedCanvases.length > 1
                ? `Move ${draggedCanvases.length} here` : 'Move here'}
              onDragOver={resource.kind === 'container' && draggedCanvases?.length && canvasDestination(resource, 'move')
                && draggedCanvases.every((canvas) => canvas.parentId !== resource.id) ? (event) => {
                  event.preventDefault()
                  event.dataTransfer.dropEffect = 'move'
                  setDropTargetId(resource.id)
                } : undefined}
              onDragLeave={resource.kind === 'container' ? (event) => {
                if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDropTargetId(null)
              } : undefined}
              onDrop={resource.kind === 'container' ? (event) => {
                event.preventDefault()
                void dropCanvasInto(resource)
              } : undefined}
              onRetry={reload}
              onNewFolder={!groupSelected && resource.kind === 'container' && resource.canCreateFolder
                ? () => setFolderCreateParent({ resource, path: [...crumbs, resource] }) : undefined}
              onRenameFolder={!groupSelected && resource.kind === 'container' && resource.canRenameFolder
                ? () => setFolderRenameResource({ resource, path: [...crumbs, resource] }) : undefined}
              onDeleteFolder={!groupSelected && resource.kind === 'container' && folderDeleteMode(resource)
                ? () => setFolderDeleteResource({ resource, path: [...crumbs, resource] }) : undefined}
              onMove={groupSelected && selectedEditableCanvases.length === selectedResources.length
                ? () => container && setMoveResource({ resources: selectedEditableCanvases, sourceContainer: container, sourcePath: crumbs })
                : !groupSelected && editableCanvas(resource)
                  ? () => container && setMoveResource({ resources: [resource], sourceContainer: container, sourcePath: crumbs }) : undefined}
              onRenameCanvas={!groupSelected && editableCanvas(resource)
                ? () => setCanvasRenameResource(resource) : undefined}
              onDuplicateCanvas={!groupSelected && editableCanvas(resource) ? () => void startDuplicate(resource) : undefined}
              onDeleteCanvas={groupSelected && selectedOwnedCanvases.length === selectedResources.length
                ? deleteSelection : !groupSelected && ownedCanvas(resource) ? () => setCanvasDeleteResource(resource) : undefined}
              onRemoveDataset={!groupSelected && resource.kind === 'dataset' && !resource.detached
                && (!isExternal(resource) || resource.providerMutation)
                ? () => setDatasetRemoveResource(resource) : undefined} />
            })}
          </div> : visibleConnectedSources.length ? null : <div className="grid flex-1 place-items-center px-4 text-center text-[13px] text-muted-foreground"><span>{!container
            ? 'This Workspace location is unavailable.'
            : hasMore ? 'This page has no items. Continue to the next page.'
            : isExternal(container) ? canvasDestination(container, 'create')
              ? 'This connected source folder is empty. Create a Canvas here to get started.'
              : 'This connected source folder is empty.'
              : 'This folder is empty. Create a Canvas here to get started.'}</span></div>}
          {loadMoreError && <div role="alert" className="mt-3 self-center text-[12px] text-destructive">Couldn't load this page: {loadMoreError}</div>}
          {(pageIndex > 0 || hasMore) && <nav aria-label="Workspace pages" className="mt-3 flex items-center justify-center gap-2 text-[12px]">
            <button type="button" onClick={() => void load(containerId, pageCursors[pageIndex - 1] ?? null, pageIndex - 1)}
              disabled={pageIndex === 0 || loadingMore} data-testid="workspace-previous-page"
              className="rounded-md border border-border bg-card px-3 py-1.5 font-semibold disabled:opacity-50">Previous</button>
            <span className="min-w-16 text-center text-muted-foreground">Page {pageIndex + 1}</span>
            <button type="button" onClick={() => void load(containerId, cursor, pageIndex + 1)}
              disabled={!hasMore || !cursor || loadingMore} data-testid="workspace-next-page"
              className="rounded-md border border-border bg-card px-3 py-1.5 font-semibold disabled:opacity-50">{loadingMore ? 'Loading…' : loadMoreError ? 'Retry' : 'Next'}</button>
          </nav>}
        </div>}
      </div>
      </ContextMenuTrigger>
      <ContextMenuContent aria-label="Folder actions" className="w-56">
        <ContextMenuLabel className="truncate">{container?.name ?? 'Workspace'}</ContextMenuLabel>
        <ContextMenuItem onSelect={() => setAddDataOpen(true)}><Icon name="plus" size={13} /> Add data…</ContextMenuItem>
        <ContextMenuItem disabled={!container?.canCreateFolder}
          title={!container?.canCreateFolder ? container?.folderMutationUnavailableReason ?? 'New folders are unavailable here.' : undefined}
          onSelect={() => container && setFolderCreateParent({ resource: container, path: crumbs })}>
          <Icon name="plus" size={13} /> {container?.canCreateFolder ? 'New folder' : 'New folder unavailable'}
        </ContextMenuItem>
        <ContextMenuItem disabled={!canvasDestination(container, 'create')}
          title={!canvasDestination(container, 'create') ? canvasDestinationTitle(container, 'create') : undefined}
          onSelect={() => setCreateOpen(true)}><Icon name="grid" size={13} /> Create canvas</ContextMenuItem>
        <ContextMenuSeparator />
        <ContextMenuItem onSelect={reload}><Icon name="refresh" size={13} /> Reload</ContextMenuItem>
      </ContextMenuContent>
      </ContextMenu>

      {selectedView && <DatasetViewDetail definition={selectedView} onClose={closeDetail} onDeleted={() => {
        setSelectedView(null)
        pushToast('DatasetView deleted', 'success')
        setWorkspaceResource(`container:${containerId}`)
      }} />}
      {selectedDetached && <DetachedResource resource={selectedDetached} onClose={closeDetail} />}
      {addDataOpen && <AddDataModal onClose={() => setAddDataOpen(false)} onUploadDataset={uploadDataset}
        onCompleted={reload} />}
      {createOpen && canvasDestination(container, 'create') && <NewCanvasDialog container={container!} onClose={() => setCreateOpen(false)}
        onCreated={(canvasId) => { setCreateOpen(false); void openFile(canvasId) }} />}
      {folderCreateParent && <FolderCreateDialog parent={folderCreateParent.resource} path={folderCreateParent.path}
        onClose={() => setFolderCreateParent(null)} onCreated={(resource) => {
          setFolderCreateParent(null); reload(); setWorkspaceResource(resource.id)
        }} />}
      {folderRenameResource && <FolderRenameDialog resource={folderRenameResource.resource} path={folderRenameResource.path}
        onClose={() => setFolderRenameResource(null)} onRenamed={(resource) => {
          const fromSearch = folderRenameResource.fromSearch
          setFolderRenameResource(null); reload()
          if (!fromSearch) setWorkspaceResource(resource.id)
        }} />}
      {folderDeleteResource && <FolderDeleteDialog resource={folderDeleteResource.resource} path={folderDeleteResource.path}
        onClose={() => setFolderDeleteResource(null)} onDeleted={() => {
          setFolderDeleteResource(null); reload()
        }} onOpenFolder={() => { setFolderDeleteResource(null); setWorkspaceResource(folderDeleteResource.resource.id) }} />}
      {canvasRenameResource && <CanvasRenameDialog resource={canvasRenameResource} onClose={() => setCanvasRenameResource(null)}
        onRenamed={() => { setCanvasRenameResource(null); void refreshFiles(); reload() }} />}
      {canvasDeleteResource && <CanvasDeleteDialog resource={canvasDeleteResource} onClose={() => setCanvasDeleteResource(null)}
        onDeleted={() => { setCanvasDeleteResource(null); setSelectedResourceIds(new Set()); void refreshFiles(); reload() }} />}
      {canvasBatchDeleteResources && <CanvasBatchDeleteDialog resources={canvasBatchDeleteResources}
        onClose={() => setCanvasBatchDeleteResources(null)} onCompleted={(deleted) => {
          setCanvasBatchDeleteResources(null); setSelectedResourceIds(new Set()); void refreshFiles(); reload()
          pushToast(`Deleted ${deleted} Canvases.`, 'success')
        }} />}
      {datasetRemoveResource && (isExternal(datasetRemoveResource)
        ? <ProviderDatasetRemoveDialog resource={datasetRemoveResource}
          onClose={() => setDatasetRemoveResource(null)} onRemoved={() => {
            setDatasetRemoveResource(null); setSelectedResourceIds(new Set()); reload()
            pushToast('Dataset removed from its connected source', 'success')
          }} />
        : <DatasetRemoveDialog resource={datasetRemoveResource}
        onClose={() => setDatasetRemoveResource(null)} onRemoved={(warning) => {
          setDatasetRemoveResource(null); setSelectedResourceIds(new Set()); reload()
          pushToast(warning ?? 'Dataset removed from Workspace', warning ? 'info' : 'success')
        }} />)}
      {canvasCopySource && <CanvasCopyModal source={canvasCopySource}
        initialDestination={container && !isExternal(container)
          ? { containerId: identity(container), path: crumbs }
          : undefined}
        onClose={() => setCanvasCopySource(null)}
        onCreated={() => { setSelectedResourceIds(new Set()); reload() }} />}
      {datasetActionDialog}
      {providerActionDialog}
      {moveResource && <MoveCanvasDialog resources={moveResource.resources} sourceContainer={moveResource.sourceContainer} sourcePath={moveResource.sourcePath} onClose={() => setMoveResource(null)}
        onMoved={(result, destinationPath) => {
          const moved = result.items[0]
          const destination = result.container
          if (moveResource.resources.length === 1 && moved && destination) {
            setUndoMove({ resource: moved, previousContainer: moveResource.sourceContainer,
              destination, destinationPath })
          } else {
            setUndoMove(null)
          }
          pushToast(moveResource.resources.length === 1
            ? `Moved “${moveResource.resources[0]?.name}”.`
            : `Moved ${moveResource.resources.length} Canvases.`, 'success')
          setMoveResource(null)
          setSelectedResourceIds(new Set())
          reload()
        }} />}
      {relinkDialog}
    </div>
  )
}

function WorkspaceSearchResults({ query, revision, onOpen, onAction, files }: {
  query: string; revision: number; onOpen: (resource: WorkspaceResource) => void
  onAction: (resource: WorkspaceResource, action: 'new-folder' | 'rename-folder' | 'delete-folder' | 'rename-canvas' | 'move-canvas' | 'delete-canvas' | 'remove-dataset') => void
  files: CanvasFile[]
}) {
  const providerPlacementObservations = useContext(ProviderPlacementObservationsContext)
  const [groups, setGroups] = useState<WorkspaceSearchGroup[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [completeness, setCompleteness] = useState<'complete' | 'page' | 'partial'>('complete')
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null)
  const request = useRef(0)
  const enrichment = useRef<AbortController | null>(null)
  const enrichmentAttempts = useRef(new Set<string>())
  const loadedProviderOccurrences = useRef(new Map<string, {
    resource: WorkspaceResource
    freshness: 'current' | 'stale' | 'unknown'
  }>())

  const load = useCallback(async (nextCursor?: string | null) => {
    const sequence = ++request.current
    enrichment.current?.abort()
    const controller = new AbortController()
    enrichment.current = controller
    const more = !!nextCursor
    if (more) { setLoadingMore(true); setLoadMoreError(null) }
    else {
      setLoading(true); setGroups([]); setError(null); setLoadMoreError(null)
      enrichmentAttempts.current.clear()
      loadedProviderOccurrences.current.clear()
    }
    try {
      const page = await api.workspaceSearch(query, {
        limit: WORKSPACE_SEARCH_PAGE_SIZE, cursor: nextCursor ?? undefined,
      })
      if (sequence !== request.current) return
      page.groups.forEach((group) => providerPlacementObservations.observe(
        group.items, [], { current: group.source.freshness === 'current' },
      ))
      setCompleteness(page.completeness)
      setGroups((current) => {
        if (!more) return page.groups
        const merged = new Map(current.map((group) => [group.source.id, group]))
        for (const group of page.groups) {
          const previous = merged.get(group.source.id)
          const items = previous ? [...previous.items] : []
          const seen = new Set(items.map((item) => item.id))
          items.push(...group.items.filter((item) => !seen.has(item.id)))
          merged.set(group.source.id, { source: group.source, items })
        }
        return [...merged.values()]
      })
      setCursor(page.nextCursor ?? null)
      setHasMore(page.hasMore)
      const pageOccurrences = page.groups.flatMap((group) => group.items
        .filter((resource) => isExternal(resource) && resource.kind === 'dataset'
          && resource.mountId && resource.providerPlacementId)
        .map((resource) => ({ resource, freshness: group.source.freshness })))
      for (const occurrence of pageOccurrences) {
        loadedProviderOccurrences.current.delete(occurrence.resource.id)
        loadedProviderOccurrences.current.set(occurrence.resource.id, occurrence)
      }
      while (loadedProviderOccurrences.current.size > WORKSPACE_SEARCH_ENRICHMENT_MAX_OBSERVATIONS) {
        loadedProviderOccurrences.current.delete(loadedProviderOccurrences.current.keys().next().value!)
      }
      const occurrences = [...loadedProviderOccurrences.current.values()]
      const nameCounts = new Map<string, number>()
      for (const { resource } of occurrences) {
        const key = `${resource.mountId}\u0000${resource.name.toLowerCase()}`
        nameCounts.set(key, (nameCounts.get(key) ?? 0) + 1)
      }
      const duplicateOccurrences = occurrences.filter(({ resource }) => (
        (nameCounts.get(`${resource.mountId}\u0000${resource.name.toLowerCase()}`) ?? 0) > 1
        && !providerPlacementObservations.placementPath(resource)
        && !enrichmentAttempts.current.has(resource.id)
      )).slice(0, Math.max(0, WORKSPACE_SEARCH_PAGE_SIZE - enrichmentAttempts.current.size))
      duplicateOccurrences.forEach(({ resource }) => enrichmentAttempts.current.add(resource.id))
      const resolved = await Promise.all(duplicateOccurrences.map(async (occurrence) => {
        try {
          return {
            occurrence,
            resolution: await api.workspaceResource(
              occurrence.resource.id, { signal: controller.signal },
            ),
          }
        } catch {
          return null
        }
      }))
      if (sequence !== request.current || controller.signal.aborted) return
      for (const item of resolved) {
        if (!item?.resolution.resource) continue
        providerPlacementObservations.observe(
          [item.resolution.resource], item.resolution.ancestors,
          {
            current: item.occurrence.freshness === 'current'
              && item.resolution.source.completeness === 'complete',
          },
        )
      }
    } catch (caught) {
      if (controller.signal.aborted) return
      if (sequence === request.current) {
        if (more) setLoadMoreError(errorMessage(caught))
        else setError(errorMessage(caught))
      }
    } finally {
      if (sequence === request.current) { setLoading(false); setLoadingMore(false) }
    }
  }, [query, providerPlacementObservations])

  useEffect(() => {
    void load()
    return () => {
      request.current += 1
      enrichment.current?.abort()
    }
  }, [load, revision])

  const resultCount = groups.reduce((count, group) => count + group.items.length, 0)
  if (loading) return <div className="grid h-full place-items-center text-[13px] text-muted-foreground">Searching Workspace…</div>
  if (error) return <div role="alert" className="mx-auto flex max-w-md flex-col items-center gap-2 rounded-lg border border-destructive/30 p-5 text-center text-[13px] text-destructive">
    <span>Couldn't search Workspace: {error}</span>
    <button onClick={() => void load()} className="font-semibold underline">Retry</button>
  </div>
  return <div className="mx-auto grid max-w-5xl gap-4" data-testid="workspace-search-results">
    <div className={`rounded-lg border px-3 py-2 text-[12px] ${completeness === 'partial'
      ? 'border-amber-300/50 bg-amber-50 text-amber-950 dark:bg-amber-950/30 dark:text-amber-100'
      : 'border-border bg-muted/25 text-muted-foreground'}`}>
      <strong>{completeness === 'partial' ? 'Partial search results' : `${resultCount} result${resultCount === 1 ? '' : 's'}`}</strong>
      <span> for “{query}”</span>
      {completeness === 'partial' && <span> — unavailable, stale, or unsupported sources are labeled below.</span>}
    </div>
    {groups.map((group) => <SearchSourceGroup key={group.source.id} group={group} onOpen={onOpen} onAction={onAction} files={files} />)}
    {!resultCount && <div className="rounded-lg border border-dashed border-border p-8 text-center text-[13px] text-muted-foreground">
      {hasMore ? 'This page has no matches yet. Load more results to continue searching.'
        : completeness === 'partial'
        ? 'No matches were returned by the available sources. This is not a complete empty result.'
        : 'No views, datasets, canvases, or containers match this query.'}
    </div>}
    {loadMoreError && <div role="alert" className="mx-auto text-[12px] text-destructive">
      Couldn't load more search results: {loadMoreError}
    </div>}
    {hasMore && <button onClick={() => void load(cursor)} disabled={loadingMore}
      data-testid="workspace-search-load-more"
      className="mx-auto rounded-md border border-border bg-card px-3 py-1.5 text-[12px] font-semibold text-foreground disabled:opacity-50">
      {loadingMore ? 'Loading…' : loadMoreError ? 'Retry load more' : 'Load more results'}
    </button>}
  </div>
}

function SearchSourceGroup({ group, onOpen, onAction, files }: {
  group: WorkspaceSearchGroup; onOpen: (resource: WorkspaceResource) => void
  onAction: (resource: WorkspaceResource, action: 'new-folder' | 'rename-folder' | 'delete-folder' | 'rename-canvas' | 'move-canvas' | 'delete-canvas' | 'remove-dataset') => void
  files: CanvasFile[]
}) {
  const source = group.source
  const name = source.kind === 'local' ? 'Workspace'
    : source.kind === 'provider' ? `Connected source ${source.mountId ?? source.id}` : 'Connected source configuration'
  const error = statusMessage(source)
  const detail = [
    source.provider,
  ].filter(Boolean).join(' · ')
  return <section aria-label={`Search source ${name}`} className="grid gap-2">
    <div className="flex min-w-0 flex-wrap items-center gap-x-2 text-[11px] text-muted-foreground">
      <h2 className="text-[12px] font-bold text-foreground">{name}</h2>
      <span>{detail}</span>
      {error && <span className="text-amber-700 dark:text-amber-300">— {error}</span>}
    </div>
    {group.items.map((resource) => <ResourceRow key={resource.id} resource={resource} onOpen={() => onOpen(resource)}
      onNewFolder={resource.kind === 'container' && resource.canCreateFolder ? () => onAction(resource, 'new-folder') : undefined}
      onRenameFolder={resource.kind === 'container' && resource.canRenameFolder ? () => onAction(resource, 'rename-folder') : undefined}
      onDeleteFolder={resource.kind === 'container' && folderDeleteMode(resource) ? () => onAction(resource, 'delete-folder') : undefined}
      onRenameCanvas={resource.kind === 'canvas' && !isExternal(resource) && !resource.detached && ['owner', 'editor'].includes(files.find((file) => file.id === identity(resource))?.role ?? '')
        ? () => onAction(resource, 'rename-canvas') : undefined}
      onMove={resource.kind === 'canvas' && !isExternal(resource) && !resource.detached && ['owner', 'editor'].includes(files.find((file) => file.id === identity(resource))?.role ?? '')
        ? () => onAction(resource, 'move-canvas') : undefined}
      onDeleteCanvas={resource.kind === 'canvas' && !isExternal(resource) && !resource.detached && files.find((file) => file.id === identity(resource))?.role === 'owner'
        ? () => onAction(resource, 'delete-canvas') : undefined}
      onRemoveDataset={resource.kind === 'dataset' && !resource.detached
        && (!isExternal(resource) || resource.providerMutation)
        ? () => onAction(resource, 'remove-dataset') : undefined} />)}
    {!group.items.length && <div className="rounded-md border border-dashed border-border px-3 py-2 text-[11px] text-muted-foreground">
      {source.completeness === 'complete' ? 'No matches from this source.'
        : error ?? sourceCompletenessLabel(source.completeness)}
    </div>}
  </section>
}

function SourceStatusBar({ sources, completeness }: {
  sources: WorkspaceSourceStatus[]; completeness: 'complete' | 'page' | 'partial'
}) {
  const issues = sources.filter(sourceNeedsAttention)
  if (!issues.length && completeness !== 'partial') return null
  return <section aria-label="Workspace source status" className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-amber-300/50 bg-amber-50 px-7 py-2 text-[11px] text-amber-950 dark:bg-amber-950/30 dark:text-amber-100">
    <span className="font-semibold">Some Workspace sources are unavailable</span>
    {issues.map((source) => {
      const name = source.kind === 'local' ? 'Workspace'
        : source.kind === 'provider' ? `Connected source ${source.mountId ?? source.id}`
          : 'Connected source configuration'
      const detail = source.provider ? ` · ${source.provider}` : ''
      const message = statusMessage(source)
      return <span key={source.id} title={message ?? undefined} className="min-w-0 max-w-full truncate">
        {name}{detail} · <strong>{sourceCompletenessLabel(source.completeness)}</strong>{message ? ` — ${message}` : ''}
      </span>
    })}
  </section>
}

function NewCanvasDialog({ container, onClose, onCreated }: {
  container: WorkspaceResource; onClose: () => void; onCreated: (canvasId: string) => void
}) {
  const [name, setName] = useState('untitled')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const replay = useRef<{ intent: string; requestId: string } | null>(null)
  const submit = async () => {
    const destination = canvasDestination(container, 'create')
    if (!name.trim() || !destination || busy) return
    setBusy(true); setError(null)
    try {
      const intent = JSON.stringify({ containerId: destination.containerId,
        expectedContainerVersion: destination.expectedContainerVersion, name: name.trim() })
      if (destination.externalOverlay && replay.current?.intent !== intent) {
        replay.current = { intent, requestId: newRequestId() }
      }
      const created = await api.workspaceCreateCanvas({
        containerId: destination.containerId, expectedContainerVersion: destination.expectedContainerVersion,
        name: name.trim(), ...(destination.externalOverlay ? { requestId: replay.current!.requestId } : {}),
      })
      onCreated(created.id)
    } catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label="Create canvas" onClose={onClose}>
    <p className="text-[12px] text-muted-foreground">Folder: <strong className="text-foreground">{container.name}</strong></p>
    <label className="grid gap-1 text-[11px] text-muted-foreground">Canvas name
      <input autoFocus value={name} onChange={(event) => setName(event.target.value)} className="dp-input" />
    </label>
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void submit()} disabled={!name.trim() || busy} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Creating…' : 'Create canvas'}</button></div>
  </Modal>
}

function breadcrumb(path: WorkspaceResource[]): string {
  const names = path.map((item) => item.name).filter(Boolean)
  return names[0] === 'Workspace' ? names.join(' / ') : ['Workspace', ...names].join(' / ')
}

function FolderCreateDialog({ parent, path, onClose, onCreated }: {
  parent: WorkspaceResource; path: WorkspaceResource[]; onClose: () => void
  onCreated: (resource: WorkspaceResource) => void
}) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const replay = useRef<{ intent: string; requestId: string } | null>(null)
  const submit = async () => {
    if (!name.trim() || parent.version == null || busy) return
    setBusy(true); setError(null)
    try {
      const intent = JSON.stringify({ parentId: identity(parent), expectedParentVersion: parent.version, name: name.trim() })
      if (replay.current?.intent !== intent) replay.current = { intent, requestId: newRequestId() }
      const result = await api.workspaceCreateFolder({
        parentId: identity(parent), expectedParentVersion: parent.version, name: name.trim(), requestId: replay.current.requestId,
      })
      onCreated(result.resource)
    } catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label="New folder" onClose={onClose}>
    <p className="text-[12px] text-muted-foreground">Parent: <strong className="text-foreground">{breadcrumb(path)}</strong></p>
    <label className="grid gap-1 text-[11px] text-muted-foreground">Folder name
      <input autoFocus aria-label="Folder name" value={name} onChange={(event) => setName(event.target.value)} className="dp-input" />
    </label>
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void submit()} disabled={!name.trim() || busy} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Creating…' : 'Create'}</button></div>
  </Modal>
}

function FolderRenameDialog({ resource, path, onClose, onRenamed }: {
  resource: WorkspaceResource; path: WorkspaceResource[]; onClose: () => void
  onRenamed: (resource: WorkspaceResource) => void
}) {
  const [name, setName] = useState(resource.name)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = async () => {
    if (!name.trim() || resource.version == null || busy) return
    setBusy(true); setError(null)
    try { onRenamed((await api.workspaceRenameFolder(identity(resource), { expectedVersion: resource.version, name: name.trim() })).resource) }
    catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label={`Rename ${resource.name}`} onClose={onClose}>
    <p className="text-[12px] text-muted-foreground">Location: <strong className="text-foreground">{breadcrumb(path.slice(0, -1))}</strong></p>
    <label className="grid gap-1 text-[11px] text-muted-foreground">Folder name
      <input autoFocus aria-label="Folder name" value={name} onChange={(event) => setName(event.target.value)} className="dp-input" />
    </label>
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void submit()} disabled={!name.trim() || name.trim() === resource.name || busy} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Renaming…' : 'Rename'}</button></div>
  </Modal>
}

function FolderDeleteDialog({ resource, path, onClose, onDeleted, onOpenFolder }: {
  resource: WorkspaceResource; path: WorkspaceResource[]; onClose: () => void; onDeleted: () => void; onOpenFolder: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const empty = resource.canDeleteFolder
  const submit = async () => {
    if (!empty || resource.version == null || busy) return
    setBusy(true); setError(null)
    try { await api.workspaceDeleteFolder(identity(resource), { expectedVersion: resource.version }); onDeleted() }
    catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label={`Delete ${resource.name}`} onClose={onClose}>
    <p className="text-[12px] text-muted-foreground">Location: <strong className="text-foreground">{breadcrumb(path.slice(0, -1))}</strong></p>
    {!empty ? <p role="status" className="text-[12px] leading-5 text-muted-foreground">This folder must be empty before it can be deleted.</p>
      : <p className="text-[12px] leading-5 text-muted-foreground">Delete this empty local Folder? This cannot be undone.</p>}
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      {!empty && <button onClick={onOpenFolder} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background">Open folder</button>}
      {empty && <button onClick={() => void submit()} disabled={busy} className="rounded-md bg-destructive px-3 py-1.5 text-[12px] font-semibold text-destructive-foreground disabled:opacity-50">{busy ? 'Deleting…' : 'Delete'}</button>}</div>
  </Modal>
}

function CanvasRenameDialog({ resource, onClose, onRenamed }: {
  resource: WorkspaceResource; onClose: () => void; onRenamed: () => void
}) {
  const [name, setName] = useState(resource.name)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const active = useRef(true)
  useEffect(() => () => { active.current = false }, [])
  const close = () => { active.current = false; onClose() }
  const submit = async () => {
    if (!name.trim() || busy) return
    setBusy(true); setError(null)
    try {
      const doc = await api.getCanvas(identity(resource))
      // Workspace placement versions protect placement moves, not the Canvas document. Read the
      // exact document first and use its own CAS token for this document mutation.
      if (!active.current) return
      await api.saveCanvas({ ...doc, name: name.trim() }, false, doc.version)
      if (!active.current) return
      onRenamed()
    } catch (caught) { if (active.current) setError(errorMessage(caught)) }
    finally { if (active.current) setBusy(false) }
  }
  return <Modal label={`Rename ${resource.name}`} onClose={close}>
    <label className="grid gap-1 text-[11px] text-muted-foreground">Canvas name
      <input autoFocus aria-label="Canvas name" value={name} onChange={(event) => setName(event.target.value)} className="dp-input" />
    </label>
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={close} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void submit()} disabled={!name.trim() || name.trim() === resource.name || busy} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Renaming…' : 'Rename'}</button></div>
  </Modal>
}

function DatasetRemoveDialog({ resource, onClose, onRemoved }: {
  resource: WorkspaceResource; onClose: () => void; onRemoved: (warning?: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [table, setTable] = useState<CatalogTable | null>(null)
  const [loading, setLoading] = useState(true)
  const [deleteSource, setDeleteSource] = useState(false)
  useEffect(() => {
    let active = true
    void api.tableByRegistration(identity(resource)).then((next) => {
      if (active) setTable(next)
    }).catch((caught) => {
      if (active) setError(errorMessage(caught))
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [resource])
  const sourcePath = table?.sourceDeleteAllowed ? ordinaryLocalSourcePath(table.uri) : null
  const remove = async () => {
    if (busy || !table) return
    setBusy(true); setError(null)
    try {
      if (!table.registrationId || !table.metadataRevision) {
        throw new Error('Reload this dataset before removing it')
      }
      const result = deleteSource
        ? await api.unregisterTable(table.id, table.registrationId, table.metadataRevision, true)
        : await api.unregisterTable(table.id, table.registrationId, table.metadataRevision)
      onRemoved(result.warning ?? undefined)
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }
  return <Modal label={`Remove ${resource.name}`} onClose={busy ? () => undefined : onClose}>
    <div className="grid gap-2 text-[12px]">
      <label className={`flex cursor-pointer gap-3 rounded-md border p-3 ${!deleteSource ? 'border-primary bg-primary/5' : 'border-border'}`}>
        <input type="radio" name="remove-mode" checked={!deleteSource} onChange={() => setDeleteSource(false)} className="mt-0.5 accent-primary" />
        <span><span className="block font-semibold text-foreground">Remove from Workspace</span>
          <span className="mt-0.5 block text-muted-foreground">Keep the source file so it can be registered again.</span></span>
      </label>
      {sourcePath && <label className={`flex cursor-pointer gap-3 rounded-md border p-3 ${deleteSource ? 'border-destructive/70 bg-destructive/5' : 'border-border'}`}>
        <input type="radio" name="remove-mode" checked={deleteSource} onChange={() => setDeleteSource(true)} className="mt-0.5 accent-destructive" />
        <span className="min-w-0"><span className="block font-semibold text-foreground">Delete the source file too</span>
          <span className="mt-0.5 block text-muted-foreground">This cannot be undone. Canvases using this dataset will no longer be able to read it.</span>
          <span className="mt-1 block break-all font-mono text-[10.5px] text-muted-foreground">{sourcePath}</span></span>
      </label>}
      {!loading && table && !sourcePath && <p className="text-muted-foreground">This source is remote, managed, or folder-backed, so only its Workspace entry can be removed here.</p>}
    </div>
    {error && <div role="alert" className="text-[12px] text-destructive">Couldn't remove this dataset: {error}</div>}
    <div className="flex justify-end gap-2">
      <button onClick={onClose} disabled={busy} className="rounded-md border border-border px-3 py-1.5 text-[12px] disabled:opacity-50">Cancel</button>
      <button onClick={() => void remove()} disabled={busy || loading || !table}
        className="rounded-md bg-destructive px-3 py-1.5 text-[12px] font-semibold text-destructive-foreground disabled:opacity-50">
        {busy ? 'Removing…' : deleteSource ? 'Delete file and remove' : 'Remove dataset'}
      </button>
    </div>
  </Modal>
}

function ProviderDatasetRemoveDialog({ resource, onClose, onRemoved }: {
  resource: WorkspaceResource; onClose: () => void; onRemoved: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const remove = async () => {
    if (busy) return
    setBusy(true); setError(null)
    try {
      await api.removeProviderDataset(resource.id)
      onRemoved()
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }
  return <Modal label={`Remove ${resource.name} from ${resource.mountId ?? 'connected source'}`} onClose={busy ? () => undefined : onClose}>
    <div className="space-y-2 text-[12px] leading-5 text-muted-foreground">
      <p>This removes the table registration from {resource.mountId ?? 'the connected source'}.</p>
      <p>The underlying data stays in its storage. Canvases using this table will show it as unavailable.</p>
    </div>
    {error && <div role="alert" className="text-[12px] text-destructive">Couldn't remove this dataset: {error}</div>}
    <div className="flex justify-end gap-2">
      <button onClick={onClose} disabled={busy} className="rounded-md border border-border px-3 py-1.5 text-[12px] disabled:opacity-50">Cancel</button>
      <button onClick={() => void remove()} disabled={busy}
        className="rounded-md bg-destructive px-3 py-1.5 text-[12px] font-semibold text-destructive-foreground disabled:opacity-50">
        {busy ? 'Removing…' : 'Remove from source'}
      </button>
    </div>
  </Modal>
}

function CanvasDeleteDialog({ resource, onClose, onDeleted }: {
  resource: WorkspaceResource; onClose: () => void; onDeleted: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = async () => {
    if (busy) return
    setBusy(true); setError(null)
    try {
      if (!resource.placementId || resource.version == null || resource.canvasVersion == null) {
        throw new Error('Reload this Canvas before deleting it')
      }
      await api.workspaceBatch({
        action: 'delete_canvases',
        items: [{
          placementId: resource.placementId,
          expectedVersion: resource.version,
          expectedCanvasVersion: resource.canvasVersion,
        }],
      })
      onDeleted()
    }
    catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label={`Delete ${resource.name}`} onClose={onClose}>
    <div className="space-y-2 text-[12px] text-muted-foreground">
      <p>Delete this local Canvas? This cannot be undone.</p>
      <p>This permanently deletes its version history, run and Job history, Inbox outcomes, and saved intermediate results.</p>
      <p>Published or managed datasets remain available.</p>
    </div>
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void submit()} disabled={busy} className="rounded-md bg-destructive px-3 py-1.5 text-[12px] font-semibold text-destructive-foreground disabled:opacity-50">{busy ? 'Deleting…' : 'Delete'}</button></div>
  </Modal>
}

function CanvasBatchDeleteDialog({ resources, onClose, onCompleted }: {
  resources: WorkspaceResource[]; onClose: () => void
  onCompleted: (deleted: number) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const remove = async () => {
    if (busy) return
    setBusy(true); setError(null)
    try {
      const items = resources.flatMap((resource) => (
        resource.placementId && resource.version != null && resource.canvasVersion != null
      )
        ? [{
            placementId: resource.placementId,
            expectedVersion: resource.version,
            expectedCanvasVersion: resource.canvasVersion,
          }]
        : [])
      if (items.length !== resources.length) {
        throw new Error('Reload the selection before deleting it')
      }
      await api.workspaceBatch({ action: 'delete_canvases', items })
      onCompleted(resources.length)
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }
  return <Modal label={`Delete ${resources.length} Canvases`} onClose={busy ? () => undefined : onClose}>
    <div className="space-y-2 text-[12px] text-muted-foreground">
      <p>Delete the selected Canvases? This cannot be undone.</p>
      <ul className="max-h-32 list-disc overflow-y-auto pl-5 text-foreground">
        {resources.map((resource) => <li key={resource.id}>{resource.name}</li>)}
      </ul>
      <p>The selection is deleted together. If any Canvas changed or cannot be deleted, nothing is deleted.</p>
    </div>
    {error && <div role="alert" className="text-[12px] text-destructive">Couldn't delete the selection: {error}</div>}
    <div className="flex justify-end gap-2">
      <button onClick={onClose} disabled={busy} className="rounded-md border border-border px-3 py-1.5 text-[12px] disabled:opacity-50">Cancel</button>
      <button onClick={() => void remove()} disabled={busy} className="rounded-md bg-destructive px-3 py-1.5 text-[12px] font-semibold text-destructive-foreground disabled:opacity-50">{busy ? 'Deleting…' : 'Delete selected'}</button>
    </div>
  </Modal>
}

type CanvasTargetState = 'loading' | 'ready' | 'unavailable'

function DatasetActionDialog({ action, container, destinationError, files, currentCanvasId, targetState, onClose, onOpened, onRetryDestination, onRefreshCanvases }: {
  action: { tables: CatalogTable[] }; container: WorkspaceResource | null; destinationError?: string | null
  files: CanvasFile[]; currentCanvasId: string; targetState: CanvasTargetState; onClose: () => void; onOpened: (canvasId: string, nodeId?: string | null) => void
  onRetryDestination?: () => void; onRefreshCanvases: () => Promise<boolean>
}) {
  const editable = targetState === 'ready'
    ? files.filter((file) => file.role === 'owner' || file.role === 'editor') : []
  const datasetIds = action.tables.flatMap((table) => table.registrationId ? [table.registrationId] : [])
  const label = action.tables.length === 1 ? action.tables[0].name : `${action.tables.length} datasets`
  const [mode, setMode] = useState<'explore' | 'current' | 'choose'>('explore')
  const [name, setName] = useState(`${label} exploration`)
  const [canvasId, setCanvasId] = useState(editable[0]?.id ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const pushToast = useStore((state) => state.pushToast)
  const addReplay = useRef<{ intent: string; requestId: string } | null>(null)
  const currentCanvas = editable.find((file) => file.id === currentCanvasId)
  useEffect(() => {
    if (!editable.some((file) => file.id === canvasId)) setCanvasId(editable[0]?.id ?? '')
  }, [canvasId, files, targetState])
  const submit = async () => {
    if (busy) return
    setBusy(true); setError(null); setConflict(false)
    try {
      if (datasetIds.length !== action.tables.length || !datasetIds.length) {
        setError('Reload the selection before using it; a stable dataset identity is missing')
        return
      }
      if (mode === 'explore') {
        if (!container || container.version == null) { setError('Reload this Workspace destination first'); return }
        if (!name.trim()) return
        const created = await api.workspaceCreateCanvas({
          containerId: identity(container), expectedContainerVersion: container.version,
          name: name.trim(), datasetIds,
        })
        onOpened(created.id, created.nodeId)
      } else {
        const target = mode === 'current' ? currentCanvas : editable.find((file) => file.id === canvasId)
        if (!target) { setError('Choose an editable target canvas'); return }
        const intent = JSON.stringify({ canvasId: target.id, expectedCanvasVersion: target.version, datasetIds })
        if (addReplay.current?.intent !== intent) addReplay.current = { intent, requestId: newRequestId() }
        const result = await api.workspaceAddDatasets(target.id, {
          datasetIds, expectedCanvasVersion: target.version, requestId: addReplay.current.requestId,
        })
        if (result.alreadyPresent) pushToast('This dataset is already present in the selected Canvas.', 'info')
        onOpened(target.id)
      }
    } catch (caught) {
      if (caught instanceof KernelError && caught.status === 409 && mode !== 'explore') {
        setConflict(true)
        setError('That Canvas changed. Refresh the Canvas list, then try adding the Source again.')
      } else setError(errorMessage(caught))
    }
    finally { setBusy(false) }
  }
  const refreshAfterConflict = async () => {
    if (busy) return
    setBusy(true)
    const refreshed = await onRefreshCanvases()
    setBusy(false)
    if (refreshed) { setConflict(false); setError('Canvases refreshed. Try adding the Source again.') }
  }
  return <Modal label={`Use ${label}`} onClose={onClose}>
    <div className="max-h-24 overflow-y-auto rounded-md border border-border bg-muted/25 px-2 py-1 text-[10.5px] text-muted-foreground">
      {action.tables.map((table) => <div key={table.id} className="truncate">{table.name}</div>)}
    </div>
    <div className="grid gap-2 sm:grid-cols-3">
      <button onClick={() => setMode('explore')} aria-pressed={mode === 'explore'} className={`rounded-lg border p-3 text-left ${mode === 'explore' ? 'border-primary bg-primary/5' : 'border-border'}`}>
        <span className="block text-[12px] font-semibold">Explore in a new Canvas</span><span className="text-[10.5px] text-muted-foreground">{container ? `Create in ${container.name}` : 'Loading destination…'}</span>
      </button>
      <button onClick={() => setMode('current')} disabled={targetState !== 'ready' || !currentCanvas} aria-pressed={mode === 'current'} className={`rounded-lg border p-3 text-left disabled:opacity-50 ${mode === 'current' ? 'border-primary bg-primary/5' : 'border-border'}`}>
        <span className="block text-[12px] font-semibold">Add to a recent Canvas</span><span className="text-[10.5px] text-muted-foreground">{currentCanvas ? currentCanvas.name : 'No editable recent Canvas'}</span>
      </button>
      <button onClick={() => setMode('choose')} disabled={targetState !== 'ready'} aria-pressed={mode === 'choose'} className={`rounded-lg border p-3 text-left disabled:opacity-50 ${mode === 'choose' ? 'border-primary bg-primary/5' : 'border-border'}`}>
        <span className="block text-[12px] font-semibold">Choose another Canvas</span><span className="text-[10.5px] text-muted-foreground">Select an editable destination</span>
      </button>
    </div>
    {mode === 'explore' ? <label className="grid gap-1 text-[11px] text-muted-foreground">New canvas name
      <input aria-label="New canvas name" value={name} onChange={(event) => setName(event.target.value)} className="dp-input" />
    </label> : targetState !== 'ready' ? <div role="status" className="text-[12px] text-muted-foreground">{targetState === 'loading' ? 'Refreshing editable Canvases…' : 'Editable Canvases could not be refreshed. Close and try again.'}</div>
      : mode === 'current' && currentCanvas ? <div className="text-[11px] text-muted-foreground">Selected Canvas: <strong className="text-foreground">{currentCanvas.name}</strong>. Source nodes will be added; your data is not copied or modified.</div>
      : editable.length ? <label className="grid gap-1 text-[11px] text-muted-foreground">Choose another Canvas
      <select aria-label="Target canvas" value={canvasId} onChange={(event) => setCanvasId(event.target.value)} className="dp-input">
        {editable.map((file) => <option key={file.id} value={file.id}>{file.name} · {file.id}</option>)}
      </select>
      <span className="text-[11px] text-muted-foreground">Source nodes will be added; your data is not copied or modified.</span>
    </label> : <div role="status" className="text-[12px] text-muted-foreground">No editable canvas is available. Explore in a new canvas instead.</div>}
    {destinationError && mode === 'explore' && <div role="alert" className="flex items-center justify-between gap-2 text-[12px] text-destructive"><span>Couldn't load the Workspace destination: {destinationError}</span>{onRetryDestination && <button onClick={onRetryDestination} className="font-semibold underline">Retry</button>}</div>}
    {error && <div role="alert" className="flex items-center justify-between gap-2 text-[12px] text-destructive"><span>{error}</span>{conflict && <button onClick={() => void refreshAfterConflict()} disabled={busy} className="font-semibold underline">Refresh Canvases</button>}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void submit()} disabled={busy || (mode === 'explore' ? !name.trim() || !container : targetState !== 'ready' || (mode === 'current' ? !currentCanvas : !canvasId))} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Applying…' : mode === 'explore' ? 'Create and open' : 'Add and open'}</button></div>
  </Modal>
}

function MoveCanvasDialog({ resources, sourceContainer, sourcePath, onClose, onMoved }: {
  resources: WorkspaceResource[]; sourceContainer: WorkspaceResource; sourcePath: WorkspaceResource[]; onClose: () => void
  onMoved: (result: Awaited<ReturnType<typeof api.workspaceBatch>>, destinationPath: WorkspaceResource[]) => void
}) {
  const [path, setPath] = useState<WorkspaceResource[]>([])
  const [container, setContainer] = useState<WorkspaceResource | null>(null)
  const [children, setChildren] = useState<WorkspaceResource[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const loadRequest = useRef(0)
  const load = useCallback(async (targetId: string, nextCursor?: string | null, nextPath?: WorkspaceResource[]) => {
    const request = ++loadRequest.current
    setLoading(true); setError(null)
    try {
      const params: Parameters<typeof api.workspaceBrowse>[1] = {
        limit: PAGE_SIZE, cursor: nextCursor ?? undefined,
      }
      if (!isProviderBrowseIdentity(targetId)) params.source = 'local'
      const page = await api.workspaceBrowse(targetId, params)
      if (request !== loadRequest.current) return
      if (!page.container) throw new Error(page.sources.map(statusMessage).find(Boolean) ?? 'Workspace destination is unavailable')
      setContainer(page.container)
      if (!nextCursor) {
        const next = nextPath ?? [page.container]
        // The first picker page can resolve before React commits its path state. Its children still
        // carry the root parent identity, so restore that display-only ancestor rather than showing
        // an ambiguous bare name in the move confirmation.
        setPath(next.length === 1 && next[0].parentId === WORKSPACE_ROOT_BREADCRUMB.id
          ? [WORKSPACE_ROOT_BREADCRUMB, ...next] : next)
      }
      const destinations = [
        ...page.items.filter((item) => item.kind === 'container'),
        ...(page.connectedSources ?? []),
      ]
      setChildren((current) => {
        if (!nextCursor) return destinations
        const known = new Set(current.map((item) => item.id))
        return [...current, ...destinations.filter((item) => !known.has(item.id))]
      })
      setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
    } catch (caught) {
      if (request === loadRequest.current) setError(errorMessage(caught))
    } finally {
      if (request === loadRequest.current) setLoading(false)
    }
  }, [])
  useEffect(() => { void load(LOCAL_ROOT_ID) }, [load])
  const move = async () => {
    const destination = canvasDestination(container, 'move')
    if (!destination || busy) return
    setBusy(true); setError(null)
    try {
      const items = resources.flatMap((resource) => resource.placementId && resource.version != null
        ? [{ placementId: resource.placementId, expectedVersion: resource.version }]
        : [])
      if (items.length !== resources.length) {
        throw new Error('One or more Canvases do not have a writable Workspace placement')
      }
      const result = await api.workspaceBatch({
        action: 'move', items,
        containerId: destination.containerId,
        expectedContainerVersion: destination.expectedContainerVersion,
      })
      if (!result.container || result.items.length !== resources.length) {
        throw new Error('Workspace move returned an incomplete result')
      }
      onMoved(result, path)
    } catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  const label = resources.length === 1 ? `Move ${resources[0]?.name ?? 'Canvas'}` : `Move ${resources.length} Canvases`
  return <Modal label={label} onClose={onClose}>
    <p className="text-[11px] text-muted-foreground">Current location: <strong className="text-foreground">{breadcrumb(sourcePath)}</strong></p>
    {resources.length > 1 && <p className="text-[11px] text-muted-foreground">
      All selected Canvases move together. If any one changed or cannot be moved, none are moved.
    </p>}
    <nav aria-label="Choose destination path" className="flex flex-wrap gap-1 text-[11px]">
      {path.map((item, index) => <button key={item.id} onClick={() => void load(identity(item), null, path.slice(0, index + 1))} className="text-primary underline">{item.name}</button>)}
    </nav>
    <div className="max-h-[220px] overflow-y-auto rounded-lg border border-border p-1">
      {loading && !children.length ? <div className="p-3 text-[11px] text-muted-foreground">Loading containers…</div> : children.map((child) => <button key={child.id} onClick={() => {
        // `children` can paint one render before React commits the paired path state. Prefer the
        // current loaded container in that narrow window so destination identity never loses its root.
        const prefix = path.length ? path : container ? [container] : []
        void load(identity(child), null, [...prefix, child])
      }}
        className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] hover:bg-accent"><Icon name="chevronRight" size={12} /> <span className="min-w-0 flex-1 truncate">{child.name}</span>{isConnectedSourceRoot(child)
          ? <span className="text-[10px] text-muted-foreground">connected source</span>
          : isExternal(child) ? <span className="text-[10px] text-muted-foreground">{canvasDestination(child, 'move') ? 'Canvas folder' : 'browse only'}</span> : null}</button>)}
      {!loading && !children.length && <div className="p-3 text-[11px] text-muted-foreground">No child containers.</div>}
      {hasMore && <button onClick={() => void load(identity(container!), cursor)} disabled={loading} className="p-2 text-[11px] font-semibold text-primary">Load more containers</button>}
    </div>
    {container && <p className="text-[12px]">Destination: <strong>{breadcrumb(path)}</strong>{isExternal(container) && canvasDestination(container, 'move') ? ' · Canvases stay in this Workspace' : null}</p>}
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void move()} disabled={busy || !canvasDestination(container, 'move') || container?.id === sourceContainer.id} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Moving…' : `Move to ${container?.name ?? 'destination'}`}</button></div>
  </Modal>
}

function Modal({ label, onClose, children }: { label: string; onClose: () => void; children: ReactNode }) {
  return <div className="fixed inset-0 z-50 grid place-items-center bg-black/30 p-4" onClick={onClose}>
    <div role="dialog" aria-modal="true" aria-label={label} className="grid w-[460px] max-w-full gap-3 rounded-xl border border-border bg-card p-5 shadow-xl" onClick={(event) => event.stopPropagation()}>
      <div className="flex items-center gap-2"><h2 className="flex-1 text-[15px] font-bold">{label}</h2><button onClick={onClose} aria-label="Close"><Icon name="close" size={15} /></button></div>
      {children}
    </div>
  </div>
}

function WorkspaceResourceGlyph({ resource, size }: { resource: WorkspaceResource; size: number }) {
  if (resource.kind === 'container') return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 16 16"
    fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
    <path d="M1.8 4.2h4.4l1.3 1.5h6.7v6.8H1.8z" /><path d="M1.8 4.2V3h4l1.2 1.2" />
  </svg>
  const icon = resource.kind === 'dataset' ? 'db' : resource.kind === 'dataset_view' ? 'sample' : 'grid'
  return <Icon name={icon} size={size} />
}

type ResourceMenuAction = {
  label: string
  onSelect?: () => void
  disabled?: boolean
  danger?: boolean
  hint?: string
}

function ResourceActionLabel({ action }: { action: ResourceMenuAction }) {
  return <span className="flex min-w-0 flex-col">
    <span>{action.label}</span>
    {action.hint ? <span className="max-w-64 whitespace-normal text-[10px] font-normal leading-snug text-muted-foreground">{action.hint}</span> : null}
  </span>
}

function ResourceRow({ resource, viewMode = 'list', selected = false, contextSelectionCount = 1, onToggleSelect, onContextSelect, onOpen, onRetry, onNewFolder, onRenameFolder, onDeleteFolder, onMove, onRenameCanvas, onDuplicateCanvas, onDeleteCanvas, onRemoveDataset, draggable = false, dropTarget = false, dropTargetLabel = 'Move here', onDragStart, onDragEnd, onDragOver, onDragLeave, onDrop }: {
  resource: WorkspaceResource; viewMode?: 'list' | 'grid'; selected?: boolean; onToggleSelect?: () => void
  contextSelectionCount?: number; onContextSelect?: () => void
  onOpen: () => void; onNewFolder?: () => void; onRenameFolder?: () => void; onDeleteFolder?: () => void
  onRetry?: () => void; onMove?: () => void; onRenameCanvas?: () => void; onDuplicateCanvas?: () => void; onDeleteCanvas?: () => void
  onRemoveDataset?: () => void
  draggable?: boolean; dropTarget?: boolean; dropTargetLabel?: string
  onDragStart?: (event: DragEvent<HTMLDivElement>) => void
  onDragEnd?: (event: DragEvent<HTMLDivElement>) => void
  onDragOver?: (event: DragEvent<HTMLDivElement>) => void
  onDragLeave?: (event: DragEvent<HTMLDivElement>) => void
  onDrop?: (event: DragEvent<HTMLDivElement>) => void
}) {
  const { openId, setOpenId } = useContext(WorkspaceOverflowMenuContext)
  const providerPlacementObservations = useContext(ProviderPlacementObservationsContext)
  const menuOpen = openId === resource.id
  const unavailable = itemAvailability(resource)
  const canOpen = !unavailable || hasDetachedDatasetRecovery(resource)
  const kind = resource.kind === 'container' ? 'Folder' : resource.kind === 'canvas' ? 'Canvas' : resource.kind === 'dataset_view' ? 'Saved view' : 'Dataset'
  const source = isExternal(resource) ? `Connected source ${resource.mountId ?? 'external'}${resource.provider ? ` · ${resource.provider}` : ''}`
    : isCatalogFolder(resource) ? 'Catalog organization'
      : resource.kind === 'dataset' ? 'Catalog'
        : resource.kind === 'dataset_view' ? 'Saved dataset view'
        : resource.kind === 'canvas' ? 'Local'
          : 'Local'
  const openLabel = `Open ${kind.toLowerCase()} ${resource.name}${isExternal(resource) ? ` from ${source}` : ''}`
  const grid = viewMode === 'grid'
  const actions: ResourceMenuAction[] = [
    ...(canOpen && contextSelectionCount === 1 ? [{ label: resource.kind === 'dataset' ? 'Open in Workspace' : 'Open', onSelect: onOpen }] : []),
    ...(onNewFolder ? [{ label: 'New folder', onSelect: onNewFolder }] : []),
    ...(onRenameFolder ? [{ label: 'Rename', onSelect: onRenameFolder }] : []),
    ...(onDeleteFolder ? [{ label: 'Delete', onSelect: onDeleteFolder, danger: true }] : []),
    ...(onRenameCanvas ? [{ label: 'Rename', onSelect: onRenameCanvas }] : []),
    ...(onMove ? [{ label: 'Move', onSelect: onMove }] : []),
    ...(onDuplicateCanvas ? [{ label: 'Duplicate', onSelect: onDuplicateCanvas }] : []),
    ...(onDeleteCanvas ? [{ label: 'Delete', onSelect: onDeleteCanvas, danger: true }] : []),
    ...(onRemoveDataset ? [{ label: 'Remove dataset…', onSelect: onRemoveDataset, danger: true }] : []),
  ]
  if (contextSelectionCount > 1 && !actions.length) {
    actions.push({ label: 'No bulk actions available', disabled: true })
  } else if (contextSelectionCount === 1 && resource.kind === 'container' && !onDeleteFolder && resource.folderMutationUnavailableReason) {
    actions.push({ label: 'Delete unavailable', disabled: true, hint: resource.folderMutationUnavailableReason })
  } else if (contextSelectionCount === 1 && resource.kind === 'dataset' && isExternal(resource)) {
    actions.push({
      label: 'Remove unavailable', disabled: true,
      hint: `${resource.mountId ?? 'This connected source'} did not expose dataset removal.`,
    })
  } else if (contextSelectionCount === 1 && resource.kind === 'dataset' && !onRemoveDataset) {
    actions.push({ label: 'Remove unavailable', disabled: true, hint: 'Open this dataset to review its recovery options.' })
  }
  const dropdownItems = actions.map((action, index) => <DropdownMenuItem
    key={`${action.label}:${index}`} disabled={action.disabled} title={action.hint}
    onSelect={action.onSelect} className={action.danger ? 'text-destructive focus:text-destructive' : undefined}>
    <ResourceActionLabel action={action} />
  </DropdownMenuItem>)
  const contextItems = actions.map((action, index) => <ContextMenuItem
    key={`${action.label}:${index}`} disabled={action.disabled} title={action.hint}
    onSelect={action.onSelect} className={action.danger ? 'text-destructive focus:text-destructive' : undefined}>
    <ResourceActionLabel action={action} />
  </ContextMenuItem>)
  const hasOverflowMenu = actions.some((action) => action.label !== 'Open' && action.label !== 'Open in Workspace')
  return <ContextMenu>
    <ContextMenuTrigger asChild>
    <div draggable={draggable} onDragStart={onDragStart} onDragEnd={onDragEnd}
    onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}
    onContextMenu={() => { if (!selected) onContextSelect?.() }}
    className={`relative min-w-0 rounded-lg border bg-card ${grid ? 'flex min-h-[132px] flex-col' : 'flex items-center'} ${dropTarget ? 'border-primary bg-primary/10 ring-2 ring-primary/30' : selected ? 'border-primary/70 bg-primary/5' : 'border-border'} ${canOpen ? 'hover:border-primary/40 hover:bg-accent' : ''}`}>
    {dropTarget && <span role="status" className="pointer-events-none absolute right-2 top-2 z-20 rounded bg-primary px-2 py-1 text-[10px] font-semibold text-primary-foreground shadow-sm">{dropTargetLabel}</span>}
    {onToggleSelect && <label className={grid ? 'absolute left-2 top-2 z-10 grid h-6 w-6 place-items-center' : 'grid h-full shrink-0 place-items-center pl-3'}>
      <input type="checkbox" checked={selected} onChange={onToggleSelect} aria-label={`Select ${resource.name}`}
        className="h-3.5 w-3.5 cursor-pointer accent-primary" />
    </label>}
    <button type="button" onClick={onOpen} aria-label={openLabel} disabled={!canOpen}
      title={unavailable?.reason}
      className={grid
        ? 'flex min-h-0 min-w-0 max-w-full flex-1 flex-col items-center justify-center gap-2 overflow-hidden px-4 pb-3 pt-8 text-center disabled:cursor-not-allowed'
        : 'flex min-w-0 max-w-full flex-1 items-center gap-2.5 overflow-hidden px-3 py-1.5 text-left disabled:cursor-not-allowed'}>
      <span className="shrink-0 text-muted-foreground"><WorkspaceResourceGlyph resource={resource} size={grid ? 28 : 16} /></span>
      <span className="min-w-0 max-w-full flex-1 overflow-hidden"><span title={resource.name} className={`flex w-full min-w-0 max-w-full items-center gap-2 overflow-hidden text-[13px] font-semibold text-foreground ${grid ? 'justify-center' : ''}`}><span className="min-w-0 flex-1 truncate">{resource.name}</span>{unavailable && <span className="shrink-0 rounded-full border border-amber-300/70 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800 dark:bg-amber-950/30 dark:text-amber-200">{unavailable.label}</span>}</span><span className="block truncate text-[11px] text-muted-foreground">{kind}{isExternal(resource) ? ` · ${resource.mountId ?? resource.provider ?? 'Connected source'}` : ''}</span>{unavailable && <span className="block truncate text-[11px] text-amber-700 dark:text-amber-300">{unavailable.reason}</span>}{!grid && isExternal(resource) && resource.kind === 'dataset' && providerPlacementObservations.placementPath(resource) && <span className="block truncate text-[11px] text-muted-foreground">{providerPlacementObservations.placementPath(resource)}</span>}</span>
      {!grid && resource.kind === 'container' && canOpen && <Icon name="chevronRight" size={14} style={{ color: 'hsl(var(--muted-foreground))' }} />}
    </button>
    {resource.unavailableReason && unavailable?.state === 'unavailable' && onRetry && <button type="button" onClick={onRetry}
      className={grid ? 'pb-2 text-[11px] font-semibold text-primary underline' : 'mr-2 shrink-0 font-semibold text-primary underline'}>Retry</button>}
    {hasOverflowMenu && <DropdownMenu open={menuOpen} onOpenChange={(open) => setOpenId(open ? resource.id : null)} modal={false}>
      <DropdownMenuTrigger asChild>
        <button type="button" aria-label={`More actions for ${resource.name}`}
          onPointerDown={(event) => {
            if (!menuOpen && event.button === 0 && !event.ctrlKey) setOpenId(resource.id)
          }}
          className={grid ? 'absolute right-2 top-2 z-10 shrink-0 rounded-md border border-border bg-card px-2 py-1 text-[13px] font-semibold text-muted-foreground hover:text-foreground' : 'mr-2 shrink-0 rounded-md border border-border bg-card px-2 py-1 text-[13px] font-semibold text-muted-foreground hover:text-foreground'}>•••</button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" aria-label={`Actions for ${resource.name}`} className="min-w-40">
        {dropdownItems}
      </DropdownMenuContent>
    </DropdownMenu>}
  </div>
  </ContextMenuTrigger>
  <ContextMenuContent aria-label={`Actions for ${resource.name}`} className="min-w-52 max-w-72">
    <ContextMenuLabel className="truncate">{contextSelectionCount > 1 ? `${contextSelectionCount} selected` : resource.name}</ContextMenuLabel>
    <ContextMenuSeparator />
    {contextItems}
  </ContextMenuContent>
  </ContextMenu>
}

function ExternalDatasetDetail({ resource, source, canonicalSourceBinding, exactRevision, backLabel,
  onClose, onRetry, onUse, onOpenLineageDataset, onRelink, onRemove }: {
  resource: WorkspaceResource; source: WorkspaceSourceStatus | null; onClose: () => void
  canonicalSourceBinding: { mountId: string; sourceBindingId: string } | null
  exactRevision?: { datasetId: string; revisionId: string }
  backLabel: 'Back to Workspace' | 'Back to Canvas' | 'Back to Jobs' | 'Back to Inbox'
  onRetry: () => void; onUse: () => void; onOpenLineageDataset: (catalogId: string) => void; onRelink: () => void
  onRemove?: () => void
}) {
  const providerPlacementObservations = useContext(ProviderPlacementObservationsContext)
  const openRelationships = useStore((state) => state.openRelationships)
  const workspaceScope = useStore((state) => state.workspaceScope)
  const workspaceSearchQuery = useStore((state) => state.workspaceSearchQuery)
  const workspaceDatasetQuery = useStore((state) => state.workspaceDatasetQuery)
  const [canonicalContext, setCanonicalContext] = useState<WorkspaceCanonicalDatasetContext | null>(null)
  const [canonicalContextError, setCanonicalContextError] = useState<string | null>(null)
  const [canonicalContextRevision, setCanonicalContextRevision] = useState(0)
  const [preview, setPreview] = useState<DatasetRevisionDetail | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [previewRevision, setPreviewRevision] = useState(0)
  const placementPath = providerPlacementObservations.placementPath(resource)
  const alternatePlacements = providerPlacementObservations.alternatePlacements(resource)
  const placementState = resource.referenceState ?? (resource.detached ? 'detached' : 'current')
  const canonicalState = resource.canonicalReferenceState
  const canonicalUnavailable = canonicalState != null && canonicalState !== 'current'
  const unavailable = itemAvailability(resource)
  const selectedDatasetId = exactRevision?.datasetId ?? canonicalContext?.datasetIdentity
  const selectedRevisionId = exactRevision?.revisionId
    ?? (canonicalContext?.readMode === 'exact' ? canonicalContext.revisionId ?? undefined : undefined)
  const canonicalColumns = canonicalContext?.columns ?? []
  const previewColumns = preview?.preview.columns ?? []
  const canonicalNames = new Set(canonicalColumns.map((column) => column.name))
  const canonicalMatchesSelectedRevision = canonicalContext?.datasetIdentity === selectedDatasetId
    && canonicalContext?.revisionId === selectedRevisionId
  const compareWithCanonical = canonicalColumns.length > 0
    && (!exactRevision || canonicalMatchesSelectedRevision)
  const systemColumns = previewColumns.map((column) => (
    providerSystemColumn(column, canonicalNames, compareWithCanonical)
  )).filter((column): column is ProviderSystemColumnPresentation => column !== null)
  const systemColumnNames = new Set(systemColumns.map(({ column }) => column.name))
  const selectedColumns = exactRevision ? previewColumns : canonicalColumns
  const dataColumns = selectedColumns.filter((column) => !systemColumnNames.has(column.name))
  const systemColumnByName = new Map(systemColumns.map((column) => [column.column.name, column]))
  const selectedCommittedAt = exactRevision ? preview?.committedAt : canonicalContext?.committedAt
  const providerIssue = previewError
    ? `Couldn't load the selected version preview: ${previewError}`
    : !exactRevision && canonicalContextError
      ? `Couldn't load provider details: ${canonicalContextError}`
    : source && !sourceIsUsable(source)
      ? statusMessage(source) ?? 'This provider is not available right now.'
      : placementState !== 'current'
        ? 'This provider location is not available right now.'
        : canonicalUnavailable
          ? unavailable?.reason ?? 'This provider dataset is not available right now.'
          : resource.lastKnown
            ? 'This provider has only last-known dataset information right now.'
            : !canonicalSourceBinding && resource.providerDatasetId
              ? 'This provider could not verify the dataset connection.'
              : null
  const retryProviderDetails = () => {
    setCanonicalContextRevision((current) => current + 1)
    setPreviewRevision((current) => current + 1)
    onRetry()
  }
  const openLineageGraph = () => {
    if (!canonicalContext?.sourceUri) return
    openRelationships(canonicalContext.sourceUri, {
      mode: 'lineage',
      returnTo: {
        resourceId: resource.id,
        scope: workspaceScope,
        workspaceQuery: workspaceSearchQuery,
        datasetQuery: workspaceDatasetQuery,
      },
    })
  }
  useEffect(() => {
    const controller = new AbortController()
    setCanonicalContext(null)
    setCanonicalContextError(null)
    if (!resource.providerDatasetId || !canonicalSourceBinding || placementState !== 'current' || canonicalUnavailable
        || resource.lastKnown) return () => controller.abort()
    void api.workspaceCanonicalDataset(resource.id, { signal: controller.signal }).then((context) => {
      if (controller.signal.aborted) return
      if (canonicalSourceBinding && (
        context.mountId !== canonicalSourceBinding.mountId
        || context.sourceBindingId !== canonicalSourceBinding.sourceBindingId
        || context.providerDatasetId !== resource.providerDatasetId
      )) {
        setCanonicalContextError('The source changed while these details were loading. Retry.')
        return
      }
      setCanonicalContext(context)
    }).catch((caught) => {
      if (!controller.signal.aborted) setCanonicalContextError(errorMessage(caught))
    })
    return () => controller.abort()
  }, [
    resource.id, resource.providerDatasetId, resource.lastKnown, placementState,
    canonicalUnavailable, canonicalSourceBinding, canonicalContextRevision,
  ])
  useEffect(() => {
    const controller = new AbortController()
    setPreview(null)
    setPreviewError(null)
    if (!selectedDatasetId || !selectedRevisionId) {
      return () => controller.abort()
    }
    void api.datasetRevision(selectedDatasetId, selectedRevisionId).then((detail) => {
      if (controller.signal.aborted) return
      if (exactRevision
          && (detail.datasetId !== selectedDatasetId || detail.revisionId !== selectedRevisionId)) {
        setPreviewError('The provider returned a different dataset version than the one requested.')
        return
      }
      setPreview(detail)
    }).catch((caught) => {
      if (!controller.signal.aborted) setPreviewError(errorMessage(caught))
    })
    return () => controller.abort()
  }, [exactRevision, selectedDatasetId, selectedRevisionId, previewRevision])
  return <section aria-label={resource.name}
    className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-background"
    data-testid="provider-dataset-viewer">
      <div className="flex shrink-0 flex-wrap items-center gap-3 border-b border-border bg-card px-5 py-3">
        <button onClick={onClose} aria-label={backLabel}
          className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11.5px] font-semibold text-muted-foreground hover:bg-accent hover:text-foreground">
          <Icon name="chevronLeft" size={14} /> Back
        </button>
        <Icon name="db" size={16} />
        <div className="min-w-0 flex-1">
          <div title={resource.name} className="truncate text-[15px] font-bold text-foreground">{resource.name}</div>
          <div className="truncate text-[10.5px] text-muted-foreground">Dataset · {resource.provider ?? resource.mountId ?? 'connected source'}</div>
        </div>
        <button type="button" onClick={openLineageGraph} disabled={!canonicalContext?.sourceUri}
          title={!canonicalContext?.sourceUri ? 'Lineage becomes available after this dataset connection is verified.' : undefined}
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-card px-2.5 py-1 text-[11.5px] font-semibold text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-45">
          <Icon name="lineage" size={12} /> Lineage
        </button>
        <button onClick={onRetry} aria-label="Reload dataset"
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-card px-2.5 py-1 text-[11.5px] font-semibold text-foreground hover:bg-accent">
          <Icon name="refresh" size={12} /> Reload
        </button>
        {!exactRevision && <button onClick={onUse} disabled={!sourceIsUsable(source) || resource.lastKnown || placementState !== 'current' || canonicalUnavailable}
          className="shrink-0 rounded-md bg-primary/10 px-2.5 py-1 text-[11.5px] font-semibold text-primary disabled:opacity-50">Use in Canvas</button>}
        {!exactRevision && onRemove && <button onClick={onRemove}
          className="shrink-0 rounded-md border border-destructive/40 bg-card px-2.5 py-1 text-[11.5px] font-semibold text-destructive hover:bg-destructive/5">
          Remove…
        </button>}
      </div>
      <div tabIndex={0} aria-label="Provider dataset detail content" data-testid="provider-dataset-detail-content"
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-5 text-[12px] focus:outline-none focus:ring-2 focus:ring-inset focus:ring-ring">
        <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-5">
        <section className="grid gap-1"><div className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Location</div>
          <div className="break-words">Connected source <strong>{resource.mountId ?? 'external'}</strong>{placementPath ? ` / ${placementPath}` : ''}</div>
          {resource.provider && <div className="text-[11px] text-muted-foreground">{resource.provider}</div>}
          {alternatePlacements.length > 0 && <div className="mt-1 grid gap-1 text-[11px] text-muted-foreground">
            <div className="font-semibold text-foreground">Other locations</div>
            {alternatePlacements.map((placement) => <div key={placement.placementId} className="truncate" title={placement.path}>{placement.path}</div>)}
          </div>}
        </section>
        <section className="grid gap-1"><div className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Version</div>
          <div className="text-[11px] text-muted-foreground">{selectedRevisionId
            ? <><span className="block">{exactRevision ? 'Selected version' : 'Published version'}</span>{selectedCommittedAt && <span>Committed {new Date(selectedCommittedAt).toLocaleString()}</span>}</>
            : canonicalContext ? 'Latest provider version' : 'Checking provider version…'}</div>
        </section>
        {resource.providerDatasetId && placementState === 'current' && !canonicalUnavailable && !resource.lastKnown
          && canonicalSourceBinding && !canonicalContext && !canonicalContextError && <div role="status" className="text-[11px] text-muted-foreground">Loading dataset details…</div>}
        {(canonicalContext || exactRevision) && <section data-testid="canonical-provider-dataset-context" className="grid gap-2">
          <div data-testid="provider-column-summary" className="flex flex-wrap gap-2 text-[11px] text-muted-foreground">
            <span>{providerRowCount(preview?.summary.rowCount)}</span>
            <span>· {providerColumnCount(dataColumns.length, 'data')}</span>
            {systemColumns.length > 0 && <span>· {providerColumnCount(systemColumns.length, 'system')}</span>}
          </div>
          <div className="order-2"><div className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Schema</div>
            <div className="mt-0.5 text-[10px] text-muted-foreground">Types are reported by {resource.provider ?? resource.mountId ?? 'this source'} and are read-only here.</div>
            {exactRevision && !preview && !previewError
              ? <div className="mt-1 text-[11px] text-muted-foreground">Loading selected schema…</div>
              : dataColumns.length || systemColumns.length
              ? <div className="mt-1 max-h-[320px] overflow-y-auto rounded-md border border-border">
                <div className="sticky top-0 grid grid-cols-[minmax(0,1fr)_140px_auto] gap-3 border-b border-border bg-muted px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                  <span>Column</span><span>Type</span><span className="sr-only">Role</span>
                </div>
                {dataColumns.map((column) => <div key={column.fieldId ?? column.name}
                  className="grid grid-cols-[minmax(0,1fr)_140px_auto] items-center gap-3 border-b border-border/50 px-3 py-1.5 last:border-0">
                  <span className="truncate font-mono">{column.name}</span>
                  <span title={column.type} className="truncate text-muted-foreground">{friendlyProviderColumnType(column.type)}</span>
                  <span />
                </div>)}
                {systemColumns.map(({ column, label, description }) => <div key={column.fieldId ?? column.name}
                  className="grid grid-cols-[minmax(0,1fr)_140px_auto] items-center gap-3 border-b border-border/50 px-3 py-1.5 last:border-0">
                  <span className="truncate font-mono">{column.name}</span>
                  <span title={column.type} className="truncate text-muted-foreground">{friendlyProviderColumnType(column.type)}</span>
                  <span aria-label={`${column.name}: ${description}`} title={description}
                    className="rounded bg-muted px-1 py-px text-[9.5px] font-semibold text-muted-foreground">{label}</span>
                </div>)}
              </div>
              : <div>This source did not report any data columns.</div>}
          </div>
          {selectedRevisionId && <div className="order-1"><div className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Preview</div>
            {!preview && !previewError && <div className="mt-1 text-[11px] text-muted-foreground">Loading preview…</div>}
            {preview && (preview.preview.rows.length
              ? <div data-testid="provider-dataset-preview-scroll" tabIndex={0}
                  className="mt-1 max-h-[420px] overflow-auto rounded-md border border-border"><table className="dp-mono w-max min-w-full text-[10.5px]"><thead><tr>{preview.preview.columns.map((column) => {
                    const systemColumn = systemColumnByName.get(column.name)
                    return <th key={column.name} className="sticky top-0 border-b border-border bg-muted px-2 py-1 text-left font-semibold">
                      <span className="inline-flex items-center gap-1"><span data-testid="provider-preview-column-name">{column.name}</span>{systemColumn && <span
                        aria-label={`${column.name}: ${systemColumn.description}`} title={systemColumn.description}
                        className="rounded bg-background/80 px-1 py-px font-sans text-[9px] font-semibold text-muted-foreground">{systemColumn.label}</span>}</span>
                    </th>
                  })}</tr></thead><tbody>{preview.preview.rows.map((row, index) => <tr key={index}>{preview.preview.columns.map((column) => <td key={column.name} className="max-w-[280px] truncate whitespace-nowrap border-b border-border/40 px-2 py-0.5 last:border-0">{previewCell(row[column.name])}</td>)}</tr>)}</tbody></table></div>
              : <div className="mt-1 rounded-md border border-border px-2 py-1.5 text-[11px] text-muted-foreground">No rows in this version.</div>)}</div>}
          <div className="order-3">
            <DatasetLineageSummary uri={canonicalContext?.sourceUri} name={resource.name}
              onOpenDataset={onOpenLineageDataset} />
          </div>
        </section>}
        {source && (source.completeness === 'pending' || sourceNeedsAttention(source)) && !providerIssue
          && <div role="status" aria-label="Dataset source status"
            className="rounded-md border border-border bg-muted/30 p-2 text-muted-foreground">
            {sourceCompletenessLabel(source.completeness)}
          </div>}
        {providerIssue && <div role="status" className="rounded-md border border-amber-300/50 bg-amber-50 p-2 text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          {providerIssue}<button type="button" onClick={retryProviderDetails} className="ml-2 font-semibold underline">Retry</button>
          {(resource.lastKnown || placementState !== 'current' || canonicalUnavailable)
            && <button type="button" onClick={onRelink} className="ml-2 font-semibold underline">Relink</button>}
        </div>}
        </div>
      </div>
  </section>
}

function ProviderDatasetActionDialog({ resource, container, files, currentCanvasId, targetState, onClose, onOpened, onRefreshCanvases }: {
  resource: WorkspaceResource; container: WorkspaceResource | null; files: CanvasFile[]; currentCanvasId: string; targetState: CanvasTargetState
  onClose: () => void; onOpened: (canvasId: string, nodeId?: string | null) => void; onRefreshCanvases: () => Promise<boolean>
}) {
  const editable = targetState === 'ready'
    ? files.filter((file) => file.role === 'owner' || file.role === 'editor') : []
  const [mode, setMode] = useState<'explore' | 'current' | 'choose'>('explore')
  const [name, setName] = useState(`${resource.name} exploration`)
  const [canvasId, setCanvasId] = useState(editable[0]?.id ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const pushToast = useStore((state) => state.pushToast)
  const replay = useRef<{ intent: string; requestId: string } | null>(null)
  const addReplay = useRef<{ intent: string; requestId: string } | null>(null)
  const destination = canvasDestination(container, 'create')
  const currentCanvas = editable.find((file) => file.id === currentCanvasId)
  useEffect(() => {
    if (!editable.some((file) => file.id === canvasId)) setCanvasId(editable[0]?.id ?? '')
  }, [canvasId, files, targetState])
  const submit = async () => {
    if (busy) return
    setBusy(true); setError(null); setConflict(false)
    try {
      if (mode === 'explore') {
        if (!destination) throw new Error('Reload the writable Canvas destination first')
        if (!name.trim()) return
        const intent = JSON.stringify({ containerId: destination.containerId,
          expectedContainerVersion: destination.expectedContainerVersion, name: name.trim(), providerDatasetRefs: [resource.id] })
        if (destination.externalOverlay && replay.current?.intent !== intent) {
          replay.current = { intent, requestId: newRequestId() }
        }
        const created = await api.workspaceCreateCanvas({
          containerId: destination.containerId, expectedContainerVersion: destination.expectedContainerVersion,
          name: name.trim(), providerDatasetRefs: [resource.id],
          ...(destination.externalOverlay ? { requestId: replay.current!.requestId } : {}),
        })
        onOpened(created.id, created.nodeId)
      } else {
        const target = mode === 'current' ? currentCanvas : editable.find((file) => file.id === canvasId)
        if (!target) throw new Error('Choose an editable target canvas')
        const intent = JSON.stringify({ canvasId: target.id,
          expectedCanvasVersion: target.version, providerDatasetRefs: [resource.id] })
        if (addReplay.current?.intent !== intent) addReplay.current = { intent, requestId: newRequestId() }
        const result = await api.workspaceAddDatasets(target.id, {
          providerDatasetRefs: [resource.id], expectedCanvasVersion: target.version,
          requestId: addReplay.current.requestId,
        })
        if (result.alreadyPresent) pushToast('This provider dataset is already present in the selected Canvas.', 'info')
        onOpened(target.id)
      }
    } catch (caught) {
      if (caught instanceof KernelError && caught.status === 409 && mode !== 'explore') {
        setConflict(true)
        setError('That Canvas changed. Refresh the Canvas list, then try adding the Source again.')
      } else setError(errorMessage(caught))
    }
    finally { setBusy(false) }
  }
  const refreshAfterConflict = async () => {
    if (busy) return
    setBusy(true)
    const refreshed = await onRefreshCanvases()
    setBusy(false)
    if (refreshed) { setConflict(false); setError('Canvases refreshed. Try adding the Source again.') }
  }
  return <Modal label={`Use ${resource.name}`} onClose={onClose}>
    <p className="text-[11px] leading-5 text-muted-foreground">Data Playground saves only the connection and display details. The source data stays where it is, and creating a Canvas does not change it. {isExternal(container) && destination && 'The new Canvas stays in this Workspace.'}</p>
    <div className="grid gap-2 sm:grid-cols-3">
      <button onClick={() => setMode('explore')} aria-pressed={mode === 'explore'} className={`rounded-lg border p-3 text-left ${mode === 'explore' ? 'border-primary bg-primary/5' : 'border-border'}`}><span className="block text-[12px] font-semibold">Explore in a new Canvas</span></button>
      <button onClick={() => setMode('current')} disabled={targetState !== 'ready' || !currentCanvas} aria-pressed={mode === 'current'} className={`rounded-lg border p-3 text-left disabled:opacity-50 ${mode === 'current' ? 'border-primary bg-primary/5' : 'border-border'}`}><span className="block text-[12px] font-semibold">Add to a recent Canvas</span><span className="text-[10.5px] text-muted-foreground">{currentCanvas ? currentCanvas.name : 'No editable recent Canvas'}</span></button>
      <button onClick={() => setMode('choose')} disabled={targetState !== 'ready'} aria-pressed={mode === 'choose'} className={`rounded-lg border p-3 text-left disabled:opacity-50 ${mode === 'choose' ? 'border-primary bg-primary/5' : 'border-border'}`}><span className="block text-[12px] font-semibold">Choose another Canvas</span></button>
    </div>
    {mode === 'explore' ? <label className="grid gap-1 text-[11px] text-muted-foreground">New canvas name<input aria-label="New canvas name" value={name} onChange={(event) => setName(event.target.value)} className="dp-input" /></label>
      : targetState !== 'ready' ? <div role="status" className="text-[12px] text-muted-foreground">{targetState === 'loading' ? 'Refreshing editable Canvases…' : 'Editable Canvases could not be refreshed. Close and try again.'}</div>
      : mode === 'current' && currentCanvas ? <div className="text-[11px] text-muted-foreground">Selected Canvas: <strong className="text-foreground">{currentCanvas.name}</strong>. Source nodes will be added; your data is not copied or modified.</div>
      : editable.length ? <label className="grid gap-1 text-[11px] text-muted-foreground">Choose another Canvas<select aria-label="Target canvas" value={canvasId} onChange={(event) => setCanvasId(event.target.value)} className="dp-input">{editable.map((file) => <option key={file.id} value={file.id}>{file.name}</option>)}</select><span className="text-[11px] text-muted-foreground">Source nodes will be added; your data is not copied or modified.</span></label>
        : <div role="status" className="text-[12px] text-muted-foreground">No editable canvas is available.</div>}
    {mode === 'explore' && !destination && <div role="status" className="text-[12px] text-muted-foreground">{canvasDestinationTitle(container, 'create')}</div>}
    {error && <div role="alert" className="flex items-center justify-between gap-2 text-[12px] text-destructive"><span>{error}</span>{conflict && <button onClick={() => void refreshAfterConflict()} disabled={busy} className="font-semibold underline">Refresh Canvases</button>}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button><button onClick={() => void submit()} disabled={busy || (mode === 'explore' ? !name.trim() || !destination : targetState !== 'ready' || (mode === 'current' ? !currentCanvas : !canvasId))}
      title={mode === 'explore' && !destination ? canvasDestinationTitle(container, 'create') : undefined}
      className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Applying…' : mode === 'explore' ? 'Create and open' : 'Add and open'}</button></div>
  </Modal>
}

function RelinkResourceDialog({ resource, onClose, onRelinked }: {
  resource: WorkspaceResource; onClose: () => void; onRelinked: (resource: WorkspaceResource) => void
}) {
  const [mountId, setMountId] = useState(resource.mountId ?? '')
  const [resourceId, setResourceId] = useState(resource.resourceId ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = async () => {
    if (!mountId.trim() || !resourceId.trim() || busy) return
    setBusy(true); setError(null)
    try {
      const result = await api.workspaceRelink(resource.id, {
        mountId: mountId.trim(), resourceId: resourceId.trim(),
      })
      onRelinked(result.resource)
    } catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label={`Relink ${resource.name}`} onClose={onClose}>
    <p className="text-[12px] leading-5 text-muted-foreground">Choose the provider dataset to reconnect. Names alone are not used to repair a connection; this creates a new auditable Workspace reference.</p>
    <label className="grid gap-1 text-[11px] font-semibold">Mount ID<input aria-label="Replacement mount ID" value={mountId} onChange={(event) => setMountId(event.target.value)} className="rounded-md border border-border bg-background px-2 py-1.5 font-mono text-[12px] font-normal" /></label>
    <label className="grid gap-1 text-[11px] font-semibold">Provider resource ID<input aria-label="Replacement provider resource ID" value={resourceId} onChange={(event) => setResourceId(event.target.value)} className="rounded-md border border-border bg-background px-2 py-1.5 font-mono text-[12px] font-normal" /></label>
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button><button onClick={() => void submit()} disabled={busy || !mountId.trim() || !resourceId.trim()} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Relinking…' : 'Relink'}</button></div>
  </Modal>
}

function DetachedResource({ resource, onClose }: { resource: WorkspaceResource; onClose: () => void }) {
  return <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}>
    <div role="dialog" aria-modal="true" aria-label={resource.name} onClick={(event) => event.stopPropagation()} className="flex h-full w-[420px] flex-col border-l border-border bg-card p-5 shadow-xl">
      <div className="flex items-center gap-2"><Icon name="db" size={16} /><div className="min-w-0 flex-1 truncate text-[14px] font-bold">{resource.name}</div><button onClick={onClose} aria-label="Close"><Icon name="close" size={15} /></button></div>
      <p className="mt-5 text-[13px] leading-6 text-muted-foreground">This Workspace placement is detached: its local dataset is no longer available. Its stable placement remains visible, but there is no dataset detail to show.</p>
    </div>
  </div>
}
