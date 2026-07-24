import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, type CanvasFile } from '../api/client'
import { useStore } from '../store/graph'
import type {
  CatalogTable, DatasetViewDefinition, WorkspaceMoveCanvasResult, WorkspaceResource, WorkspaceSearchGroup,
  WorkspaceCanonicalDatasetContext, WorkspaceSourceStatus,
} from '../types/api'
import { Icon } from '../ui/Icon'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import {
  CATALOG_BATCH_LIMIT, CatalogDetail, CatalogDiscovery, emptyCatalogDiscoveryQuery,
  type CatalogDiscoveryQueryState,
} from './CatalogDiscovery'
import { WorkspaceLocalDrafts } from '../canvas/LocalDrafts'
import { DatasetViewDetail } from './DatasetViewDetail'
import { examples } from '../examples'

const LOCAL_ROOT_ID = 'workspace-local-root'
const PAGE_SIZE = 50
const WORKSPACE_SEARCH_PAGE_SIZE = 25
const WORKSPACE_SEARCH_ENRICHMENT_MAX_OBSERVATIONS = 100
const CANONICAL_CONTEXT_COLUMN_LIMIT = 25
const WORKSPACE_ROOT_BREADCRUMB: WorkspaceResource = {
  id: `container:${LOCAL_ROOT_ID}`, kind: 'container', name: 'Workspace', detached: false, source: 'local',
}
const NON_EMPTY_LOCAL_FOLDER_REASON = "Move or remove this Folder's contents before deleting it."
const PROVIDER_PLACEMENT_CACHE_MAX_DATASETS = 64
const PROVIDER_PLACEMENT_CACHE_MAX_PLACEMENTS = 6
const PROVIDER_PLACEMENT_CACHE_MAX_PATHS = 256

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
const identity = (resource: WorkspaceResource) => resource.id.slice(resource.id.indexOf(':') + 1)
const isExternal = (resource: WorkspaceResource | null) => resource?.source === 'provider'
const isCatalogFolder = (resource: WorkspaceResource | null) => !!resource?.catalogFolderId
const isCurrentCatalogLocation = (resource: WorkspaceResource | null) => !!resource && !resource.detached
  && (identity(resource) === LOCAL_ROOT_ID || resource.catalogFolderState === 'current')
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
    ? 'This source-only provider location is detached; relink or recover it before using its local Canvas overlay'
    : 'Deleted Catalog folder tombstones do not accept new canvases'
  if (!isExternal(resource)) return resource.version == null ? 'Load an exact Workspace destination first' : `Create in ${resource.name}`
  if (canvasDestination(resource, action)) return `Create a locally owned Canvas beside ${resource.name}`
  if (resource.localPlacement?.recoveryState === 'unavailable') return 'The local Canvas overlay is unavailable; retry after this source recovers'
  return 'This source-only provider location has no writable local Canvas overlay'
}

function newRequestId(): string {
  return globalThis.crypto.randomUUID()
}
const statusMessage = (status: WorkspaceSourceStatus) => status.error
  ?? (status.completeness === 'unavailable' ? 'source is offline'
    : status.completeness === 'unsupported' ? 'browse is not supported'
      : status.completeness === 'partial' ? 'source returned partial results' : null)

const DATASET_SORTS = new Set<CatalogDiscoveryQueryState['sort']>(['name', 'rows', 'updated', 'usage', 'folder'])

type WorkspaceFolderContext = {
  resource: WorkspaceResource | null
  reason?: string
  retryable?: boolean
}

// Folder paths are only a Catalog filter. Navigation is always to the opaque projected container
// returned while resolving a stable dataset identity, so a same-named local container cannot win.
function projectedFolderFromResolution(folder: string, resolved: { resource: WorkspaceResource | null; ancestors: WorkspaceResource[]; source: WorkspaceSourceStatus }): WorkspaceFolderContext {
  if (resolved.source.completeness !== 'complete') {
    return {
      resource: null,
      reason: statusMessage(resolved.source) ?? 'Workspace is only partially available',
      retryable: resolved.source.completeness !== 'unsupported'
        && resolved.source.referenceState !== 'detached'
        && resolved.source.referenceState !== 'permission_lost',
    }
  }
  const candidate = resolved.resource?.kind === 'container'
    ? resolved.resource : resolved.ancestors[resolved.ancestors.length - 1] ?? null
  const exactRoot = !folder && candidate?.id === `container:${LOCAL_ROOT_ID}`
  const exactProjection = !!folder && candidate?.catalogFolderState === 'current'
    && candidate.catalogFolderPath === folder
  if (!candidate || candidate.kind !== 'container' || candidate.detached
      || (!exactRoot && !exactProjection)) {
    return { resource: null, reason: 'This dataset is not currently available in Workspace.' }
  }
  return { resource: candidate }
}

function retryableResolutionError(caught: unknown): boolean {
  if (!caught || typeof caught !== 'object') return true
  const error = caught as { status?: unknown; retryable?: unknown }
  if (typeof error.retryable === 'boolean') return error.retryable
  if (typeof error.status !== 'number') return true
  return error.status === 429 || error.status >= 500
}

async function resolveSelectedDatasetFolder(table: CatalogTable): Promise<WorkspaceFolderContext> {
  if (!table.registrationId) {
    return { resource: null, reason: 'This dataset is not currently available in Workspace.' }
  }
  try {
    return projectedFolderFromResolution(
      table.folder ?? '', await api.workspaceResource(`dataset:${table.registrationId}`),
    )
  } catch (caught) {
    return {
      resource: null, reason: errorMessage(caught), retryable: retryableResolutionError(caught),
    }
  }
}

async function resolveProjectedFolder(folder: string, knownResourceId?: string | null): Promise<WorkspaceFolderContext> {
  if (!folder) return { resource: null }
  let knownError: string | undefined
  let knownRetryable = false
  if (knownResourceId) {
    try {
      const known = projectedFolderFromResolution(folder, await api.workspaceResource(knownResourceId))
      if (known.resource || known.retryable) return known
    } catch (caught) {
      knownError = errorMessage(caught)
      knownRetryable = retryableResolutionError(caught)
      // The route may point at a dataset that was removed while this query was open. Fall through
      // to one bounded Catalog lookup; it still has to resolve an opaque Workspace resource below.
    }
  }
  try {
    const page = await api.tablesPage({ folder, limit: 1 })
    const table = page.items[0]
    if (!table?.registrationId || table.folder !== folder) {
      return knownError
        ? { resource: null, reason: knownError, retryable: knownRetryable }
        : { resource: null, reason: 'This folder is not currently available in Workspace.' }
    }
    return projectedFolderFromResolution(folder, await api.workspaceResource(`dataset:${table.registrationId}`))
  } catch (caught) {
    return {
      resource: null, reason: errorMessage(caught), retryable: retryableResolutionError(caught),
    }
  }
}

function unavailableWorkspaceLocation(subject: 'dataset' | 'folder', reason?: string): string {
  const message = `This ${subject} is not currently available in Workspace.`
  return !reason || /^This (dataset|folder) is not currently available in Workspace\.$/.test(reason)
    ? message : `${message} ${reason}`
}

export function parseWorkspaceDatasetQuery(value: string): CatalogDiscoveryQueryState {
  const params = new URLSearchParams(value)
  const state = emptyCatalogDiscoveryQuery()
  state.q = params.get('dq')?.trim() ?? ''
  state.folder = params.get('folder')?.trim().replace(/^\/+|\/+$/g, '') ?? ''
  state.tags = (params.get('tags') ?? '').split(',').map((item) => item.trim()).filter(Boolean).slice(0, 50)
  state.owner = params.get('owner')?.trim() ?? ''
  state.hasColumns = (params.get('columns') ?? '').split(',').map((item) => item.trim()).filter(Boolean).slice(0, 50)
  const sort = params.get('sort') as CatalogDiscoveryQueryState['sort'] | null
  if (sort && DATASET_SORTS.has(sort)) state.sort = sort
  if (params.get('order') === 'desc') state.order = 'desc'
  if (params.get('match') === 'meaning') state.match = 'meaning'
  return state
}

export function serializeWorkspaceDatasetQuery(state: CatalogDiscoveryQueryState): string {
  const params = new URLSearchParams()
  if (state.q) params.set('dq', state.q)
  if (state.folder) params.set('folder', state.folder)
  if (state.tags.length) params.set('tags', state.tags.join(','))
  if (state.owner) params.set('owner', state.owner)
  if (state.hasColumns.length) params.set('columns', state.hasColumns.join(','))
  if (state.sort !== 'name') params.set('sort', state.sort)
  if (state.order !== 'asc') params.set('order', state.order)
  if (state.match !== 'text') params.set('match', state.match)
  return params.toString()
}

export function WorkspaceExplorer() {
  const scope = useStore((state) => state.workspaceScope) ?? 'all'
  const firstRunChoice = useStore((state) => state.firstRunChoice)
  const providerPlacementObservations = useProviderPlacementObservations()
  const previousScope = useRef(scope)
  useEffect(() => {
    if (previousScope.current !== scope) providerPlacementObservations.reset()
    previousScope.current = scope
  }, [scope, providerPlacementObservations])
  return <ProviderPlacementObservationsContext.Provider value={providerPlacementObservations}><WorkspaceOverflowMenuProvider><div className="flex h-full min-h-0 flex-col">
    {firstRunChoice && <FirstRunCanvasChoice />}
    <WorkspaceLocalDrafts />
    <div className="min-h-0 flex-1">{scope === 'datasets' ? <WorkspaceDatasets /> : <WorkspaceMixedExplorer />}</div>
  </div></WorkspaceOverflowMenuProvider></ProviderPlacementObservationsContext.Provider>
}

// A first-run choice belongs beside the Workspace, not in a separate tutorial surface: datasets
// remain discoverable and the two actions create exactly the Canvas the researcher selected.
function FirstRunCanvasChoice() {
  const newFile = useStore((state) => state.newFile)
  const newFromExample = useStore((state) => state.newFromExample)
  return (
    <section data-testid="first-run-canvas-choice" aria-labelledby="first-run-canvas-title"
      className="border-b border-border bg-card px-7 py-5">
      <div className="mx-auto max-w-5xl">
        <h2 id="first-run-canvas-title" className="text-[15px] font-semibold text-foreground">Create your first Canvas</h2>
        <p className="mt-1 max-w-2xl text-[12.5px] leading-relaxed text-muted-foreground">
          Start with an empty graph, or open a runnable example using the seeded sample data.
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" onClick={() => { void newFile() }}
            className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background">Start a blank Canvas</button>
        </div>
        <div className="mt-4 grid max-w-4xl gap-2 sm:grid-cols-3" role="group" aria-label="Runnable examples">
          {examples.map((example) => <button key={example.key} type="button"
            onClick={() => { void newFromExample(example.key) }} aria-label={`Open example ${example.name}`}
            className="rounded-md border border-border bg-background px-3 py-2.5 text-left transition-colors hover:border-primary/50 hover:bg-accent">
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
  const files = useStore((s) => s.files)
  const currentCanvasId = useStore((s) => s.doc?.id ?? '')
  const refreshFiles = useStore((s) => s.refreshFiles)
  const rememberTables = useStore((s) => s.rememberTables)
  const pushToast = useStore((s) => s.pushToast)
  const switchWorkspaceScope = useStore((s) => s.switchWorkspaceScope)
  const workspaceDatasetQuery = useStore((s) => s.workspaceDatasetQuery)
  const [containerId, setContainerId] = useState(LOCAL_ROOT_ID)
  const [container, setContainer] = useState<WorkspaceResource | null>(null)
  const [crumbs, setCrumbs] = useState<WorkspaceResource[]>([])
  const [items, setItems] = useState<WorkspaceResource[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
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
  const [folderCreateParent, setFolderCreateParent] = useState<{ resource: WorkspaceResource; path: WorkspaceResource[] } | null>(null)
  const [folderRenameResource, setFolderRenameResource] = useState<{ resource: WorkspaceResource; path: WorkspaceResource[]; fromSearch?: boolean } | null>(null)
  const [folderDeleteResource, setFolderDeleteResource] = useState<{ resource: WorkspaceResource; path: WorkspaceResource[]; fromSearch?: boolean } | null>(null)
  const [canvasRenameResource, setCanvasRenameResource] = useState<WorkspaceResource | null>(null)
  const [canvasDeleteResource, setCanvasDeleteResource] = useState<WorkspaceResource | null>(null)
  const [datasetAction, setDatasetAction] = useState<{ tables: CatalogTable[] } | null>(null)
  const [providerDatasetAction, setProviderDatasetAction] = useState<WorkspaceResource | null>(null)
  const [canvasTargetState, setCanvasTargetState] = useState<CanvasTargetState>('loading')
  const canvasTargetRequest = useRef(0)
  const [moveResource, setMoveResource] = useState<{
    resource: WorkspaceResource; sourceContainer: WorkspaceResource; sourcePath: WorkspaceResource[]
  } | null>(null)
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

  const load = useCallback(async (targetId: string, nextCursor?: string | null) => {
    const sequence = ++request.current
    const more = !!nextCursor
    if (more) { setLoadingMore(true); setLoadMoreError(null) }
    else {
      setLoading(true); setError(null); setLoadMoreError(null)
      // Keep a resolved location visible while it refreshes. Provider refreshes may return an honest
      // partial/offline page with no container; clearing here would hide the selected resource and
      // its ancestors even though their stable identity has not changed.
      if (targetId !== loadedContainer.current) { setItems([]); setCursor(null); setHasMore(false); setSources([]) }
    }
    try {
      const page = await api.workspaceBrowse(targetId, { limit: PAGE_SIZE, cursor: nextCursor ?? undefined })
      if (sequence !== request.current) return
      setCompleteness(page.completeness)
      setSources(page.sources ?? [])
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
      if (!more) setCrumbs((current) => current.length && current[current.length - 1].id === page.container!.id ? current : [...current, page.container!])
      setItems((current) => {
        const next = more ? current : []
        const seen = new Set(next.map((item) => item.id))
        return [...next, ...page.items.filter((item) => !seen.has(item.id))]
      })
      setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
    } catch (caught) {
      if (sequence !== request.current) return
      if (more) setLoadMoreError(errorMessage(caught))
      else setError(errorMessage(caught))
    } finally {
      if (sequence === request.current) { setLoading(false); setLoadingMore(false) }
    }
  }, [providerPlacementObservations])

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
    if (resource.kind === 'canvas') { void openFile(identity(resource)); return }
    setWorkspaceResource(resource.id)
  }
  const closeDetail = () => setWorkspaceResource(`container:${containerId}`)
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
  const startSearchAction = async (resource: WorkspaceResource, action: 'new-folder' | 'rename-folder' | 'delete-folder' | 'rename-canvas' | 'move-canvas' | 'delete-canvas') => {
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
        if (sourceContainer) setMoveResource({ resource: exact, sourceContainer, sourcePath: path })
      } else if (action === 'delete-canvas' && exact.kind === 'canvas'
        && files.find((file) => file.id === identity(exact))?.role === 'owner') {
        setCanvasDeleteResource(exact)
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
      await api.workspaceMoveCanvas(undoMove.resource.placementId, {
        containerId: destination.containerId,
        expectedContainerVersion: destination.expectedContainerVersion,
        expectedVersion: undoMove.resource.version,
      })
      setUndoMove(null)
      pushToast('Canvas move undone', 'success')
      reload()
    } catch (caught) {
      pushToast(`Could not undo move: ${errorMessage(caught)}`, 'error')
    } finally { setUndoBusy(false) }
  }
  const undoDestination = undoMove ? canvasDestination(undoMove.previousContainer, 'move') : null
  const switchToDatasets = () => {
    // Only a current built-in Catalog projection has a stable counterpart in the Datasets lens.
    // Local folders and provider mounts intentionally remain in All Workspace rather than being
    // guessed from their display name.
    const mappedFolder = container?.catalogFolderState === 'current' && container.catalogFolderPath != null
      ? container.catalogFolderPath : null
    if (mappedFolder == null) {
      switchWorkspaceScope('datasets')
      return
    }
    const next = parseWorkspaceDatasetQuery(workspaceDatasetQuery)
    next.folder = mappedFolder
    switchWorkspaceScope('datasets', {
      resourceId: container?.id ?? null,
      datasetQuery: serializeWorkspaceDatasetQuery(next),
    })
  }

  return (
    <div className="flex h-full min-w-0 flex-col">
      <header className="flex min-h-[68px] items-center gap-3 border-b border-border px-7 py-3">
        <div className="min-w-0">
          <h1 className="text-[20px] font-bold text-foreground">Workspace</h1>
          <nav aria-label="Workspace path" className="mt-0.5 flex min-w-0 items-center gap-1 overflow-hidden text-[11.5px] text-muted-foreground">
            <button onClick={() => setWorkspaceResource(null)} className="shrink-0 hover:text-foreground">Workspace</button>
            {crumbs.slice(1).map((crumb) => <span key={crumb.id} className="flex min-w-0 items-center gap-1"><span>/</span><button onClick={() => setWorkspaceResource(crumb.id)} className="truncate hover:text-foreground">{crumb.name}</button></span>)}
          </nav>
        </div>
        <WorkspaceScopeTabs active="all" onChange={(next) => {
          if (next === 'datasets') switchToDatasets()
        }} />
        <span className="flex-1" />
        <form aria-label="Workspace search" onSubmit={(event) => {
          event.preventDefault()
          setWorkspaceSearchQuery(searchDraft)
        }} className="flex min-w-[220px] max-w-sm flex-1 items-center gap-1 rounded-md border border-border bg-card px-2">
          <Icon name="search" size={13} />
          <input aria-label="Search views, datasets, canvases, and containers" value={searchDraft}
            onChange={(event) => setSearchDraft(event.target.value)} placeholder="Search Workspace"
            className="min-w-0 flex-1 bg-transparent py-1.5 text-[12px] outline-none" />
          {searchDraft && <button type="button" aria-label="Clear Workspace search" onClick={() => {
            setSearchDraft(''); setWorkspaceSearchQuery('')
          }}><Icon name="close" size={12} /></button>}
        </form>
        <div className="hidden items-center gap-2 sm:flex" aria-label="Workspace actions">
          {container?.canCreateFolder && <button onClick={() => setFolderCreateParent({ resource: container, path: crumbs })} disabled={loading}
            className="rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-semibold text-foreground disabled:text-muted-foreground disabled:opacity-65">New folder</button>}
          <button onClick={() => setCreateOpen(true)} disabled={!canvasDestination(container, 'create') || loading}
            title={canvasDestinationTitle(container, 'create')}
            className="rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-semibold text-foreground disabled:text-muted-foreground disabled:opacity-65">{isExternal(container) ? 'Create a local Canvas here' : 'New canvas here'}</button>
        </div>
        <button onClick={reload} disabled={loading || loadingMore} data-testid="workspace-reload" className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-semibold text-foreground disabled:opacity-50">
          <Icon name="refresh" size={13} /> Reload
        </button>
      </header>

      {undoMove && <div role="status" className="flex items-center gap-2 border-b border-border bg-primary/5 px-7 py-2 text-[12px] text-foreground">
        <span className="flex-1">Moved “{undoMove.resource.name}” to {breadcrumb(undoMove.destinationPath)}.{!undoDestination && ' Its previous source-only location is unavailable; recover or relink it before undoing.'}</span>
        <button onClick={() => void undoLastMove()} disabled={undoBusy || !undoDestination}
          title={!undoDestination ? canvasDestinationTitle(undoMove.previousContainer, 'move') : undefined}
          className="font-semibold text-primary underline disabled:opacity-50">{undoBusy ? 'Undoing…' : undoDestination ? 'Undo move' : 'Undo unavailable'}</button>
        <button onClick={() => setUndoMove(null)} aria-label="Dismiss move confirmation"><Icon name="close" size={13} /></button>
      </div>}

      {!searchQuery && (sources.some((source) => source.kind !== 'local') || completeness === 'partial')
        && <SourceStatusBar sources={sources} completeness={completeness} />}
      {container?.catalogFolderState === 'current' && !container.detached && <div className="border-b border-border bg-muted/30 px-7 py-1.5 text-[11.5px] text-muted-foreground">
        Folder organization comes from this catalog. Canvases stored here are local to Data Playground.
      </div>}
      {resolutionError && <div role="alert" className="flex items-center gap-3 border-b border-amber-300/50 bg-amber-50 px-7 py-2 text-[12px] text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
        <span className="min-w-0 flex-1 truncate">This selection could not be fully refreshed: {resolutionError}</span>
        <button onClick={reload} disabled={loading} className="shrink-0 font-semibold underline disabled:opacity-50">Retry</button>
        {selectedProviderResource && <button onClick={() => setRelinkResource(selectedProviderResource)} className="shrink-0 font-semibold underline">Relink</button>}
      </div>}

      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        {searchQuery ? <WorkspaceSearchResults query={searchQuery} revision={revision} onOpen={open}
          onAction={startSearchAction} files={files} /> : error ? <div role="alert" className="mx-auto flex max-w-md flex-col items-center gap-2 rounded-lg border border-destructive/30 p-5 text-center text-[13px] text-destructive">
          <span>Couldn't load this Workspace location: {error}</span>
          <button onClick={reload} className="font-semibold underline">Retry</button>
        </div> : loading ? <div className="grid h-full place-items-center text-[13px] text-muted-foreground">Loading Workspace…</div> : items.length ? <div className="mx-auto grid max-w-5xl gap-2">
          {items.map((resource) => <ResourceRow key={resource.id} resource={resource} onOpen={() => open(resource)}
            onNewFolder={resource.kind === 'container' && resource.canCreateFolder
              ? () => setFolderCreateParent({ resource, path: [...crumbs, resource] }) : undefined}
            onRenameFolder={resource.kind === 'container' && resource.canRenameFolder
              ? () => setFolderRenameResource({ resource, path: [...crumbs, resource] }) : undefined}
            onDeleteFolder={resource.kind === 'container' && folderDeleteMode(resource)
              ? () => setFolderDeleteResource({ resource, path: [...crumbs, resource] }) : undefined}
            onMove={resource.kind === 'canvas' && !isExternal(resource) && !resource.detached && ['owner', 'editor'].includes(files.find((file) => file.id === identity(resource))?.role ?? '')
              ? () => container && setMoveResource({ resource, sourceContainer: container, sourcePath: crumbs }) : undefined}
            onRenameCanvas={resource.kind === 'canvas' && !isExternal(resource) && !resource.detached && ['owner', 'editor'].includes(files.find((file) => file.id === identity(resource))?.role ?? '')
              ? () => setCanvasRenameResource(resource) : undefined}
            onDeleteCanvas={resource.kind === 'canvas' && !isExternal(resource) && !resource.detached && files.find((file) => file.id === identity(resource))?.role === 'owner'
              ? () => setCanvasDeleteResource(resource) : undefined} />)}
          {loadMoreError && <div role="alert" className="mx-auto mt-2 text-[12px] text-destructive">Couldn't load more: {loadMoreError}</div>}
          {hasMore && <button onClick={() => void load(containerId, cursor)} disabled={loadingMore} data-testid="workspace-load-more" className="mx-auto mt-2 rounded-md border border-border bg-card px-3 py-1.5 text-[12px] font-semibold text-foreground disabled:opacity-50">
            {loadingMore ? 'Loading…' : loadMoreError ? 'Retry load more' : 'Load more'}
          </button>}
        </div> : <div className="grid h-full place-items-center px-4 text-center text-[13px] text-muted-foreground"><span>{!container
          ? 'This Workspace location is unavailable.'
          : isExternal(container) ? canvasDestination(container, 'create')
            ? 'This source-only provider location is empty. Create a locally owned Canvas here to get started.'
            : 'This source-only provider location is empty.'
            : 'This local container is empty. Create a canvas here to get started.'}</span></div>}
      </div>

      {selectedTable && <CatalogDetail table={selectedTable} onClose={closeDetail} onUse={useTable}
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
        onColumn={() => pushToast('Column filters are available from the dataset detail only.', 'info')} />}
      {selectedView && <DatasetViewDetail definition={selectedView} onClose={closeDetail} onDeleted={() => {
        setSelectedView(null)
        pushToast('DatasetView deleted', 'success')
        setWorkspaceResource(`container:${containerId}`)
      }} />}
      {selectedDataset && isExternal(selectedDataset) && <ExternalDatasetDetail resource={selectedDataset} source={selectedSource}
        canonicalSourceBinding={selectedCanonicalSourceBinding} onClose={closeDetail} onRetry={reload}
        onRelink={() => setRelinkResource(selectedDataset)} onUse={() => useProviderDataset(selectedDataset)} />}
      {selectedDetached && <DetachedResource resource={selectedDetached} onClose={closeDetail} />}
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
        onDeleted={() => { setCanvasDeleteResource(null); void refreshFiles(); reload() }} />}
      {datasetAction && container?.version != null && <DatasetActionDialog action={datasetAction} container={container}
        files={files} currentCanvasId={currentCanvasId} targetState={canvasTargetState} onClose={() => setDatasetAction(null)}
        onOpened={(canvasId) => { setDatasetAction(null); setSelectedTable(null); setSelectedDataset(null); void openFile(canvasId) }} />}
      {providerDatasetAction && <ProviderDatasetActionDialog resource={providerDatasetAction}
        container={container} files={files} currentCanvasId={currentCanvasId} targetState={canvasTargetState} onClose={() => setProviderDatasetAction(null)}
        onOpened={(canvasId) => {
          setProviderDatasetAction(null); setSelectedDataset(null); void openFile(canvasId)
        }} />}
      {moveResource && <MoveCanvasDialog resource={moveResource.resource} sourceContainer={moveResource.sourceContainer} sourcePath={moveResource.sourcePath} onClose={() => setMoveResource(null)}
        onMoved={(result, destinationPath) => {
          setMoveResource(null)
          setUndoMove({ resource: result.resource, previousContainer: result.previousContainer, destination: result.container,
            destinationPath })
          reload()
        }} />}
      {relinkResource && <RelinkResourceDialog resource={relinkResource} onClose={() => setRelinkResource(null)}
        onRelinked={(resource) => {
          setRelinkResource(null)
          pushToast(`Relinked to ${resource.name}`, 'success')
          setWorkspaceResource(resource.id)
        }} />}
    </div>
  )
}

function WorkspaceScopeTabs({ active, onChange, disabled = false, disabledTitle }: {
  active: 'all' | 'datasets'; onChange: (scope: 'all' | 'datasets') => void
  disabled?: boolean; disabledTitle?: string
}) {
  return <div role="tablist" aria-label="Workspace scope" className="flex shrink-0 items-center rounded-lg border border-border bg-card p-0.5 text-[11.5px]">
    {([['all', 'All Workspace'], ['datasets', 'Datasets']] as const).map(([scope, label]) => (
      <button key={scope} role="tab" aria-selected={active === scope}
        disabled={scope !== active && disabled} title={scope !== active ? disabledTitle : undefined}
        onClick={() => onChange(scope)}
        className={`rounded-md px-2.5 py-1 disabled:cursor-not-allowed disabled:opacity-45 ${active === scope ? 'bg-accent font-semibold text-accent-foreground' : 'text-muted-foreground hover:text-foreground'}`}>
        {label}
      </button>
    ))}
  </div>
}

function WorkspaceDatasets() {
  const catalogSource = useStore((state) => state.kernelInfo)
  const foldersMutable = catalogSource?.capabilities?.includes('catalog.folder_mutation') ?? false
  const uploadDataset = useStore((state) => state.uploadDataset)
  const rememberTables = useStore((state) => state.rememberTables)
  const files = useStore((state) => state.files)
  const currentCanvasId = useStore((state) => state.doc?.id ?? '')
  const refreshFiles = useStore((state) => state.refreshFiles)
  const openFile = useStore((state) => state.openFile)
  const pushToast = useStore((state) => state.pushToast)
  const requestedResourceId = useStore((state) => state.workspaceResourceId)
  const setWorkspaceResource = useStore((state) => state.setWorkspaceResource)
  const switchWorkspaceScope = useStore((state) => state.switchWorkspaceScope)
  const encodedQuery = useStore((state) => state.workspaceDatasetQuery)
  const setEncodedQuery = useStore((state) => state.setWorkspaceDatasetQuery)
  const query = useMemo(() => parseWorkspaceDatasetQuery(encodedQuery), [encodedQuery])
  // An exact revision deep link is navigation state, not a Catalog filter. Keep it opaque here;
  // DatasetRevisionHistory verifies/read-opens the revision instead of substituting the current head.
  const exactRevision = useMemo(() => {
    const params = new URLSearchParams(encodedQuery)
    const revisionId = params.get('revision') || undefined
    const datasetId = params.get('revisionDataset') || undefined
    return revisionId && datasetId ? { revisionId, datasetId } : undefined
  }, [encodedQuery])
  const initialRevisionId = exactRevision?.revisionId
  const initialRevisionDatasetId = exactRevision?.datasetId
  const hasExactRevision = !!initialRevisionId && !!initialRevisionDatasetId
  const selectedRegistrationId = requestedResourceId?.startsWith('dataset:')
    ? requestedResourceId.slice('dataset:'.length) : null
  const [datasetAction, setDatasetAction] = useState<{ tables: CatalogTable[] } | null>(null)
  const [canvasTargetState, setCanvasTargetState] = useState<CanvasTargetState>('loading')
  const canvasTargetRequest = useRef(0)
  const [rootContainer, setRootContainer] = useState<WorkspaceResource | null>(null)
  const [destinationError, setDestinationError] = useState<string | null>(null)
  const [destinationRevision, setDestinationRevision] = useState(0)
  const [selectedWorkspaceTable, setSelectedWorkspaceTable] = useState<CatalogTable | null>(null)
  const [detailResolutionRevision, setDetailResolutionRevision] = useState(0)
  const detailResolutionSeq = useRef(0)
  const selectedWorkspaceKey = selectedWorkspaceTable?.registrationId
    ? `${selectedWorkspaceTable.registrationId}\u0000${selectedWorkspaceTable.folder ?? ''}` : null
  const [detailContext, setDetailContext] = useState<{
    key: string
    state: 'resolving' | 'available' | 'unavailable'
    resourceId?: string
    reason?: string
    retryable?: boolean
  } | null>(null)
  const folderResolutionKey = `${query.folder}\u0000${requestedResourceId ?? ''}`
  const folderResolutionKeyRef = useRef(folderResolutionKey)
  folderResolutionKeyRef.current = folderResolutionKey
  const folderResolutionSeq = useRef(0)
  const [folderContext, setFolderContext] = useState<{
    key: string
    state: 'ready' | 'resolving' | 'unavailable'
    reason?: string
    retryable?: boolean
  }>({ key: folderResolutionKey, state: 'ready' })

  // A failed resolution belongs only to this exact filter/deep-link pair. Selecting another folder
  // must immediately make its own resolution attempt possible rather than leaving a stale disabled tab.
  useEffect(() => {
    folderResolutionSeq.current += 1
    setFolderContext({ key: folderResolutionKey, state: 'ready' })
  }, [folderResolutionKey])

  useEffect(() => {
    const request = ++detailResolutionSeq.current
    if (!selectedWorkspaceTable) {
      setDetailContext(null)
      return
    }
    const key = selectedWorkspaceKey
    if (!key || !selectedWorkspaceTable.registrationId) {
      setDetailContext({
        key: key ?? '', state: 'unavailable',
        reason: 'This dataset is not currently available in Workspace.',
      })
      return
    }
    setDetailContext({ key, state: 'resolving' })
    void resolveSelectedDatasetFolder(selectedWorkspaceTable).then((context) => {
      if (request !== detailResolutionSeq.current) return
      if (context.resource) {
        setDetailContext({ key, state: 'available', resourceId: context.resource.id })
      } else {
        setDetailContext({
          key, state: 'unavailable', retryable: context.retryable,
          reason: unavailableWorkspaceLocation('dataset', context.reason),
        })
      }
    })
    return () => { detailResolutionSeq.current += 1 }
  }, [selectedWorkspaceKey, detailResolutionRevision])

  useEffect(() => {
    let cancelled = false
    setDestinationError(null)
    api.workspaceBrowse(LOCAL_ROOT_ID, { limit: 1 }).then((page) => {
      if (cancelled) return
      if (!page.container || page.container.version == null) throw new Error('Workspace root is unavailable')
      setRootContainer(page.container)
    }).catch((caught) => {
      if (!cancelled) { setRootContainer(null); setDestinationError(errorMessage(caught)) }
    })
    return () => { cancelled = true }
  }, [destinationRevision])

  const useTables = (tables: CatalogTable[]) => {
    if (!tables.length) return
    if (tables.length > CATALOG_BATCH_LIMIT) {
      pushToast(`Use is limited to ${CATALOG_BATCH_LIMIT} datasets`, 'error')
      return
    }
    if (tables.some((table) => !table.registrationId)) {
      pushToast('Reload before using: a dataset has no stable Workspace identity', 'error')
      return
    }
    rememberTables(tables)
    const request = ++canvasTargetRequest.current
    setCanvasTargetState('loading')
    setDatasetAction({ tables })
    void refreshFiles().then((refreshed) => {
      if (canvasTargetRequest.current === request) setCanvasTargetState(refreshed ? 'ready' : 'unavailable')
    })
  }

  const openTableInWorkspace = (table: CatalogTable) => {
    const key = table.registrationId ? `${table.registrationId}\u0000${table.folder ?? ''}` : null
    if (!key || detailContext?.key !== key || detailContext.state !== 'available'
        || !detailContext.resourceId) return
    if (detailContext.resourceId === `container:${LOCAL_ROOT_ID}`) switchWorkspaceScope('all')
    else switchWorkspaceScope('all', { resourceId: detailContext.resourceId })
  }

  const switchToAll = async () => {
    if (!query.folder) {
      switchWorkspaceScope('all')
      return
    }
    const key = folderResolutionKey
    const request = ++folderResolutionSeq.current
    setFolderContext({ key, state: 'resolving' })
    const context = await resolveProjectedFolder(query.folder, requestedResourceId)
    if (request !== folderResolutionSeq.current || folderResolutionKeyRef.current !== key) return
    if (!context.resource) {
      setFolderContext({
        key, state: 'unavailable', retryable: context.retryable,
        reason: unavailableWorkspaceLocation('folder', context.reason),
      })
      return
    }
    setFolderContext({ key, state: 'ready' })
    switchWorkspaceScope('all', { resourceId: context.resource.id })
  }

  const activeFolderContext = folderContext.key === folderResolutionKey
    ? folderContext : { key: folderResolutionKey, state: 'ready' as const }
  const activeDetailContext = selectedWorkspaceKey && detailContext?.key === selectedWorkspaceKey
    ? detailContext : selectedWorkspaceKey ? { key: selectedWorkspaceKey, state: 'resolving' as const } : undefined

  return <div className="flex h-full min-w-0 flex-col">
    <div className="flex items-center gap-3 border-b border-border px-7 py-2">
      <span className="text-[13px] font-bold text-foreground">Workspace</span>
      <WorkspaceScopeTabs active="datasets" onChange={(scope) => { if (scope === 'all') void switchToAll() }}
        disabled={activeFolderContext.state === 'resolving' || (activeFolderContext.state === 'unavailable' && !!query.folder)}
        disabledTitle={activeFolderContext.state === 'resolving' ? 'Resolving this folder in Workspace…'
          : activeFolderContext.reason ?? 'This folder is not currently available in Workspace.'} />
      {activeFolderContext.state === 'unavailable' && activeFolderContext.retryable
        && <button type="button" onClick={() => { void switchToAll() }}
          className="text-[11px] font-semibold text-primary hover:underline">Retry Workspace location</button>}
      <span className="ml-auto text-[11px] text-muted-foreground">Open a dataset’s folder in All Workspace to work beside its local Canvases.</span>
    </div>
    <div className="min-h-0 flex-1">
      <CatalogDiscovery sourceIdentity={catalogSource} foldersMutable={foldersMutable}
        title="Datasets" queryState={query}
        initialRevisionId={initialRevisionId}
        initialRevisionDatasetId={initialRevisionDatasetId}
        onQueryStateChange={(next) => {
          const params = new URLSearchParams(serializeWorkspaceDatasetQuery(next))
          if (hasExactRevision) {
            params.set('revision', initialRevisionId)
            params.set('revisionDataset', initialRevisionDatasetId)
          }
          setEncodedQuery(params.toString())
        }}
        selectedRegistrationId={selectedRegistrationId}
        onSelectedTableChange={(table, origin = 'user') => {
          setSelectedWorkspaceTable(table)
          // Exact revision navigation belongs to the selected route as an atomic pair. A user
          // close or user-selected replacement leaves that route, while route resolution itself
          // (including a transient null) must preserve it.
          if (hasExactRevision && origin === 'user') {
            const params = new URLSearchParams(serializeWorkspaceDatasetQuery(query))
            params.delete('revision')
            params.delete('revisionDataset')
            setEncodedQuery(params.toString())
          }
          if (!table) setWorkspaceResource(null)
          else if (table.registrationId) {
            // A route may canonicalize an old registration or receipt logical id to the current
            // registration. Only an explicit user selection starts a different dataset journey.
            setWorkspaceResource(`dataset:${table.registrationId}`)
          }
          else pushToast('This dataset has no stable Workspace identity', 'error')
        }}
        onUseTables={useTables} onUploadDataset={uploadDataset}
        onOpenInWorkspace={openTableInWorkspace}
        workspaceLocation={activeDetailContext}
        onRetryWorkspaceLocation={() => setDetailResolutionRevision((current) => current + 1)} />
    </div>
    {datasetAction && <DatasetActionDialog action={datasetAction} container={rootContainer}
      destinationError={destinationError} files={files} currentCanvasId={currentCanvasId} targetState={canvasTargetState} onClose={() => setDatasetAction(null)}
      onRetryDestination={() => setDestinationRevision((current) => current + 1)}
      onOpened={(canvasId) => { setDatasetAction(null); void openFile(canvasId) }} />}
  </div>
}

function WorkspaceSearchResults({ query, revision, onOpen, onAction, files }: {
  query: string; revision: number; onOpen: (resource: WorkspaceResource) => void
  onAction: (resource: WorkspaceResource, action: 'new-folder' | 'rename-folder' | 'delete-folder' | 'rename-canvas' | 'move-canvas' | 'delete-canvas') => void
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
      {completeness === 'partial'
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
  onAction: (resource: WorkspaceResource, action: 'new-folder' | 'rename-folder' | 'delete-folder' | 'rename-canvas' | 'move-canvas' | 'delete-canvas') => void
  files: CanvasFile[]
}) {
  const source = group.source
  const name = source.kind === 'local' ? 'Local Workspace'
    : source.kind === 'provider' ? `Mount ${source.mountId ?? source.id}` : 'Mount configuration'
  const error = statusMessage(source)
  const detail = [
    source.provider,
    source.searchMode === 'native' ? 'native search' : source.searchMode,
    source.freshness,
    source.completeness,
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
        ? () => onAction(resource, 'delete-canvas') : undefined} />)}
    {!group.items.length && <div className="rounded-md border border-dashed border-border px-3 py-2 text-[11px] text-muted-foreground">
      {source.completeness === 'complete' ? 'No matches from this source.'
        : error ?? `This source is ${source.completeness}.`}
    </div>}
  </section>
}

function SourceStatusBar({ sources, completeness }: {
  sources: WorkspaceSourceStatus[]; completeness: 'complete' | 'page' | 'partial'
}) {
  return <section aria-label="Workspace source status" className={`flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-7 py-2 text-[11px] ${completeness === 'partial'
    ? 'border-amber-300/50 bg-amber-50 text-amber-950 dark:bg-amber-950/30 dark:text-amber-100'
    : 'border-border bg-muted/25 text-muted-foreground'}`}>
    <span className="font-semibold">{completeness === 'partial' ? 'Some sources are incomplete' : 'Sources'}</span>
    {sources.map((source) => {
      const name = source.kind === 'local' ? 'Local'
        : source.kind === 'provider' ? `Mount ${source.mountId ?? source.id}`
          : 'Mount configuration'
      const detail = source.provider ? ` · ${source.provider}` : ''
      const message = statusMessage(source)
      return <span key={source.id} title={message ?? undefined} className="min-w-0 max-w-full truncate">
        {name}{detail} · <strong>{source.completeness}</strong>{message ? ` — ${message}` : ''}
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
  return <Modal label="New canvas here" onClose={onClose}>
    <p className="text-[12px] text-muted-foreground">Destination: <strong className="text-foreground">{container.name}</strong>{isExternal(container) && <span> · locally owned Canvas overlay</span>}</p>
    {isExternal(container) && <p className="text-[11px] leading-5 text-muted-foreground">This does not change the connected catalog. The Canvas is local to Data Playground.</p>}
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

function CanvasDeleteDialog({ resource, onClose, onDeleted }: {
  resource: WorkspaceResource; onClose: () => void; onDeleted: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = async () => {
    if (busy) return
    setBusy(true); setError(null)
    try { await api.deleteCanvas(identity(resource)); onDeleted() }
    catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label={`Delete ${resource.name}`} onClose={onClose}>
    <p className="text-[12px] text-muted-foreground">Delete this local Canvas? This cannot be undone.</p>
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void submit()} disabled={busy} className="rounded-md bg-destructive px-3 py-1.5 text-[12px] font-semibold text-destructive-foreground disabled:opacity-50">{busy ? 'Deleting…' : 'Delete'}</button></div>
  </Modal>
}

type CanvasTargetState = 'loading' | 'ready' | 'unavailable'

function DatasetActionDialog({ action, container, destinationError, files, currentCanvasId, targetState, onClose, onOpened, onRetryDestination }: {
  action: { tables: CatalogTable[] }; container: WorkspaceResource | null; destinationError?: string | null
  files: CanvasFile[]; currentCanvasId: string; targetState: CanvasTargetState; onClose: () => void; onOpened: (canvasId: string) => void
  onRetryDestination?: () => void
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
  const pushToast = useStore((state) => state.pushToast)
  const addReplay = useRef<{ intent: string; requestId: string } | null>(null)
  const currentCanvas = editable.find((file) => file.id === currentCanvasId)
  useEffect(() => {
    if (!editable.some((file) => file.id === canvasId)) setCanvasId(editable[0]?.id ?? '')
  }, [canvasId, files, targetState])
  const submit = async () => {
    if (busy) return
    setBusy(true); setError(null)
    try {
      if (datasetIds.length !== action.tables.length || !datasetIds.length) {
        setError('Reload the selection before using it; a stable dataset identity is missing')
        return
      }
      if (mode === 'explore') {
        if (!container || container.version == null) { setError('Load an exact Workspace destination first'); return }
        if (!name.trim()) return
        const created = await api.workspaceCreateCanvas({
          containerId: identity(container), expectedContainerVersion: container.version,
          name: name.trim(), datasetIds,
        })
        onOpened(created.id)
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
    } catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label={`Use ${label}`} onClose={onClose}>
    <div className="max-h-24 overflow-y-auto rounded-md border border-border bg-muted/25 px-2 py-1 text-[10.5px] text-muted-foreground">
      {action.tables.map((table) => <div key={table.id} className="truncate">{table.name}</div>)}
    </div>
    <p className="text-[11px] text-muted-foreground">Bounded to {CATALOG_BATCH_LIMIT} datasets. The selected sources are applied atomically under one Canvas version precondition.</p>
    <div className="grid gap-2 sm:grid-cols-3">
      <button onClick={() => setMode('explore')} aria-pressed={mode === 'explore'} className={`rounded-lg border p-3 text-left ${mode === 'explore' ? 'border-primary bg-primary/5' : 'border-border'}`}>
        <span className="block text-[12px] font-semibold">Explore in a new Canvas</span><span className="text-[10.5px] text-muted-foreground">{container ? `Create in ${container.name}` : 'Loading exact destination…'}</span>
      </button>
      <button onClick={() => setMode('current')} disabled={targetState !== 'ready' || !currentCanvas} aria-pressed={mode === 'current'} className={`rounded-lg border p-3 text-left disabled:opacity-50 ${mode === 'current' ? 'border-primary bg-primary/5' : 'border-border'}`}>
        <span className="block text-[12px] font-semibold">Add to this Canvas</span><span className="text-[10.5px] text-muted-foreground">{currentCanvas ? currentCanvas.name : 'No editable current Canvas'}</span>
      </button>
      <button onClick={() => setMode('choose')} disabled={targetState !== 'ready'} aria-pressed={mode === 'choose'} className={`rounded-lg border p-3 text-left disabled:opacity-50 ${mode === 'choose' ? 'border-primary bg-primary/5' : 'border-border'}`}>
        <span className="block text-[12px] font-semibold">Choose a Canvas</span><span className="text-[10.5px] text-muted-foreground">Select an editable destination</span>
      </button>
    </div>
    {mode === 'explore' ? <label className="grid gap-1 text-[11px] text-muted-foreground">New canvas name
      <input value={name} onChange={(event) => setName(event.target.value)} className="dp-input" />
    </label> : targetState !== 'ready' ? <div role="status" className="text-[12px] text-muted-foreground">{targetState === 'loading' ? 'Refreshing editable Canvases…' : 'Editable Canvases could not be refreshed. Close and try again.'}</div>
      : mode === 'current' && currentCanvas ? <div className="text-[11px] text-muted-foreground">Selected Canvas: <strong className="text-foreground">{currentCanvas.name}</strong></div>
      : editable.length ? <label className="grid gap-1 text-[11px] text-muted-foreground">Choose a Canvas
      <select aria-label="Target canvas" value={canvasId} onChange={(event) => setCanvasId(event.target.value)} className="dp-input">
        {editable.map((file) => <option key={file.id} value={file.id}>{file.name} · {file.id}</option>)}
      </select>
    </label> : <div role="status" className="text-[12px] text-muted-foreground">No editable canvas is available. Explore in a new canvas instead.</div>}
    {destinationError && mode === 'explore' && <div role="alert" className="flex items-center justify-between gap-2 text-[12px] text-destructive"><span>Couldn't load the Workspace destination: {destinationError}</span>{onRetryDestination && <button onClick={onRetryDestination} className="font-semibold underline">Retry</button>}</div>}
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void submit()} disabled={busy || (mode === 'explore' ? !name.trim() || !container : targetState !== 'ready' || (mode === 'current' ? !currentCanvas : !canvasId))} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Applying…' : mode === 'explore' ? 'Create and open' : 'Add and open'}</button></div>
  </Modal>
}

function MoveCanvasDialog({ resource, sourceContainer, sourcePath, onClose, onMoved }: {
  resource: WorkspaceResource; sourceContainer: WorkspaceResource; sourcePath: WorkspaceResource[]; onClose: () => void
  onMoved: (result: WorkspaceMoveCanvasResult, destinationPath: WorkspaceResource[]) => void
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
      const page = await api.workspaceBrowse(targetId, { limit: PAGE_SIZE, cursor: nextCursor ?? undefined })
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
      const destinations = page.items.filter((item) => item.kind === 'container' && !!canvasDestination(item, 'move'))
      setChildren((current) => nextCursor ? [...current, ...destinations] : destinations)
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
    if (!resource.placementId || resource.version == null || !destination || busy) return
    setBusy(true); setError(null)
    try {
      onMoved(await api.workspaceMoveCanvas(resource.placementId, {
        containerId: destination.containerId, expectedContainerVersion: destination.expectedContainerVersion,
        expectedVersion: resource.version,
      }), path)
    } catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label={`Move ${resource.name}`} onClose={onClose}>
    <p className="text-[11px] text-muted-foreground">Current location: <strong className="text-foreground">{breadcrumb(sourcePath)}</strong></p>
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
        className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] hover:bg-accent"><Icon name="chevronRight" size={12} /> <span className="min-w-0 flex-1 truncate">{child.name}</span>{isExternal(child) && <span className="text-[10px] text-muted-foreground">local overlay</span>}</button>)}
      {!loading && !children.length && <div className="p-3 text-[11px] text-muted-foreground">No child containers.</div>}
      {hasMore && <button onClick={() => void load(identity(container!), cursor)} disabled={loading} className="p-2 text-[11px] font-semibold text-primary">Load more containers</button>}
    </div>
    {container && <p className="text-[12px]">Destination: <strong>{breadcrumb(path)}</strong>{isExternal(container) && ' · locally owned Canvas overlay'}</p>}
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

function ResourceRow({ resource, onOpen, onNewFolder, onRenameFolder, onDeleteFolder, onMove, onRenameCanvas, onDeleteCanvas }: {
  resource: WorkspaceResource; onOpen: () => void; onNewFolder?: () => void; onRenameFolder?: () => void; onDeleteFolder?: () => void
  onMove?: () => void; onRenameCanvas?: () => void; onDeleteCanvas?: () => void
}) {
  const { openId, setOpenId } = useContext(WorkspaceOverflowMenuContext)
  const providerPlacementObservations = useContext(ProviderPlacementObservationsContext)
  const menuOpen = openId === resource.id
  const icon = resource.kind === 'dataset' ? 'db' : resource.kind === 'dataset_view' ? 'sample' : resource.kind === 'canvas' ? 'grid' : 'chevronRight'
  const kind = resource.kind === 'container' ? 'Folder' : resource.kind === 'canvas' ? 'Canvas' : resource.kind === 'dataset_view' ? 'DatasetView' : 'Dataset'
  const source = isExternal(resource) ? `Source-only mount ${resource.mountId ?? 'external'}${resource.provider ? ` · ${resource.provider}` : ''}`
    : isCatalogFolder(resource) ? 'Catalog organization'
      : resource.kind === 'dataset' ? 'Catalog'
        : resource.kind === 'dataset_view' ? 'Local exact view'
        : resource.kind === 'canvas' ? 'Local'
          : 'Local'
  const openLabel = `Open ${kind.toLowerCase()} ${resource.name}${isExternal(resource) ? ` from ${source}` : ''}`
  const providerFolderExplanation = resource.kind === 'container' && isExternal(resource)
    && !resource.canCreateFolder && !resource.canRenameFolder && !folderDeleteMode(resource)
    ? canvasDestination(resource, 'create')
      ? 'This catalog manages its folders. You can still create a local Canvas here. Folder rename, move, and delete are unavailable here; this does not change the connected catalog.'
      : 'This catalog manages its folders. Folder rename, move, and delete are unavailable here. No local Canvas placement is available here.'
    : null
  return <div className="flex min-w-0 items-center rounded-lg border border-border bg-card hover:border-primary/40 hover:bg-accent">
    <button type="button" onClick={onOpen} aria-label={openLabel}
      className="flex min-w-0 flex-1 items-center gap-3 px-3 py-3 text-left">
      <Icon name={icon} size={16} style={{ color: 'hsl(var(--muted-foreground))' }} />
      <span className="min-w-0 flex-1"><span title={resource.name} className="block truncate text-[13px] font-semibold text-foreground">{resource.name}</span><span className="block truncate text-[11px] text-muted-foreground">{kind} · {source}{resource.detached ? ' · detached' : ''}</span>{isExternal(resource) && resource.kind === 'dataset' && providerPlacementObservations.placementPath(resource) && <span className="block truncate text-[11px] text-muted-foreground">Placement path · {providerPlacementObservations.placementPath(resource)}</span>}{providerFolderExplanation && <span className="block text-[11px] leading-4 text-muted-foreground">{providerFolderExplanation}</span>}</span>
      {resource.kind === 'container' && <Icon name="chevronRight" size={14} style={{ color: 'hsl(var(--muted-foreground))' }} />}
    </button>
    <DropdownMenu open={menuOpen} onOpenChange={(open) => setOpenId(open ? resource.id : null)} modal={false}>
      <DropdownMenuTrigger asChild>
        <button type="button" aria-label={`More actions for ${resource.name}`}
          onPointerDown={(event) => {
            if (!menuOpen && event.button === 0 && !event.ctrlKey) setOpenId(resource.id)
          }}
          className="mr-2 shrink-0 rounded-md border border-border bg-card px-2 py-1 text-[13px] font-semibold text-muted-foreground hover:text-foreground">•••</button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" aria-label={`Actions for ${resource.name}`} className="min-w-40">
        <DropdownMenuItem onSelect={onOpen}>{resource.kind === 'dataset' ? 'Open in Workspace' : 'Open'}</DropdownMenuItem>
        {onNewFolder && <DropdownMenuItem onSelect={onNewFolder}>New folder</DropdownMenuItem>}
        {onRenameFolder && <DropdownMenuItem onSelect={onRenameFolder}>Rename</DropdownMenuItem>}
        {onDeleteFolder && <DropdownMenuItem onSelect={onDeleteFolder} className="text-destructive focus:text-destructive">Delete</DropdownMenuItem>}
        {onRenameCanvas && <DropdownMenuItem onSelect={onRenameCanvas}>Rename</DropdownMenuItem>}
        {onMove && <DropdownMenuItem onSelect={onMove}>Move</DropdownMenuItem>}
        {onDeleteCanvas && <DropdownMenuItem onSelect={onDeleteCanvas} className="text-destructive focus:text-destructive">Delete</DropdownMenuItem>}
      </DropdownMenuContent>
    </DropdownMenu>
  </div>
}

function ExternalDatasetDetail({ resource, source, canonicalSourceBinding, onClose, onRetry, onRelink, onUse }: {
  resource: WorkspaceResource; source: WorkspaceSourceStatus | null; onClose: () => void
  canonicalSourceBinding: { mountId: string; sourceBindingId: string } | null
  onRetry: () => void; onRelink: () => void; onUse: () => void
}) {
  const providerPlacementObservations = useContext(ProviderPlacementObservationsContext)
  const [canonicalContext, setCanonicalContext] = useState<WorkspaceCanonicalDatasetContext | null>(null)
  const [canonicalContextError, setCanonicalContextError] = useState<string | null>(null)
  const [canonicalContextRevision, setCanonicalContextRevision] = useState(0)
  const placementId = providerPlacementId(resource)
  const placementPath = providerPlacementObservations.placementPath(resource)
  const alternatePlacements = providerPlacementObservations.alternatePlacements(resource)
  const placementState = resource.referenceState ?? (resource.detached ? 'detached' : 'current')
  const canonicalState = resource.canonicalReferenceState
  const canonicalUnavailable = canonicalState != null && canonicalState !== 'current'
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
        setCanonicalContextError('The canonical Source generation changed; retry this placement.')
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
  return <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}>
    <div role="dialog" aria-modal="true" aria-label={resource.name} onClick={(event) => event.stopPropagation()} className="flex h-full w-[420px] max-w-full flex-col border-l border-border bg-card p-5 shadow-xl">
      <div className="flex items-center gap-2"><Icon name="db" size={16} /><div title={resource.name} className="min-w-0 flex-1 truncate text-[14px] font-bold">{resource.name}</div><button onClick={onClose} aria-label="Close"><Icon name="close" size={15} /></button></div>
      <div className="mt-5 grid gap-3 text-[12px]">
        <div><div className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Source</div><div>Source-only mount <strong>{resource.mountId ?? 'external'}</strong>{resource.provider ? ` · ${resource.provider}` : ''}</div></div>
        <div><div className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Workspace placement</div><div className="break-all font-mono text-[11px]">{placementId ?? resource.id}</div>{placementPath && <div className="mt-0.5 text-[11px] text-muted-foreground">{placementPath}</div>}</div>
        {resource.providerDatasetId && <div><div className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Canonical dataset</div>
          <div className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 text-[11px]"><span className="text-muted-foreground">Mount</span><span className="break-all font-mono">{resource.mountId}</span>
            <span className="text-muted-foreground">Dataset ID</span><span className="break-all font-mono">{resource.providerDatasetId}</span></div>
        </div>}
        {resource.providerDatasetId && placementState === 'current' && !canonicalUnavailable && !resource.lastKnown
          && canonicalSourceBinding && !canonicalContext && !canonicalContextError && <div role="status" className="text-[11px] text-muted-foreground">Loading canonical dataset context…</div>}
        {resource.providerDatasetId && placementState === 'current' && !canonicalUnavailable && !resource.lastKnown
          && !canonicalSourceBinding && <div role="status" className="rounded-md border border-amber-300/50 bg-amber-50 p-2 text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
            Canonical Source binding is unavailable.
            <button type="button" onClick={onRetry} className="ml-2 font-semibold underline">Retry canonical dataset</button>
          </div>}
        {canonicalContext && <div data-testid="canonical-provider-dataset-context" className="rounded-md border border-border p-2 text-[11px]">
          <div><span className="text-muted-foreground">Source dataset identity</span><div className="break-all font-mono">{canonicalContext.datasetIdentity}</div></div>
          <div className="mt-1"><span className="text-muted-foreground">Read mode</span><div>{canonicalContext.readMode === 'exact'
            ? <>Exact revision · <span className="font-mono">{canonicalContext.revisionId}</span>{canonicalContext.committedAt ? ` · committed ${new Date(canonicalContext.committedAt).toLocaleString()}` : ''}</>
            : 'Current/latest provider state · not an exact revision'}</div></div>
          <div className="mt-1"><span className="text-muted-foreground">Canonical columns</span>
            {canonicalContext.columns.length
              ? <div className="mt-0.5 grid gap-0.5">{canonicalContext.columns.slice(0, CANONICAL_CONTEXT_COLUMN_LIMIT).map((column) => <div key={column.fieldId ?? column.name}><span className="font-mono">{column.name}</span> · {column.type}</div>)}
                {canonicalContext.columns.length > CANONICAL_CONTEXT_COLUMN_LIMIT
                  && <div className="text-muted-foreground">{canonicalContext.columns.length - CANONICAL_CONTEXT_COLUMN_LIMIT} more columns</div>}
              </div>
              : <div>No canonical columns were reported.</div>}
          </div>
        </div>}
        {canonicalContextError && <div role="status" className="rounded-md border border-amber-300/50 bg-amber-50 p-2 text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          Canonical dataset context is unavailable. {canonicalContextError}
          <button type="button" onClick={() => setCanonicalContextRevision((current) => current + 1)} className="ml-2 font-semibold underline">Retry canonical detail</button>
        </div>}
        <div className="text-[11px] text-muted-foreground">Placement state · {placementState.replace('_', ' ')}</div>
        {resource.providerDatasetId && canonicalState && <div className="text-[11px] text-muted-foreground">Canonical dataset state · {canonicalState.replace('_', ' ')}</div>}
        {placementState !== 'current' && <div role="status" className="rounded-md border border-amber-300/50 bg-amber-50 p-2 text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          Placement state · {placementState.replace('_', ' ')}{canonicalState === 'current' ? ' · canonical dataset is current' : ''}{resource.lastResolvedAt ? ` · last resolved ${new Date(resource.lastResolvedAt).toLocaleString()}` : ''}
          <div className="mt-2 flex gap-3"><button onClick={onRetry} className="font-semibold underline">Retry</button><button onClick={onRelink} className="font-semibold underline">Relink</button></div>
        </div>}
        {canonicalUnavailable && <div role="status" className="rounded-md border border-amber-300/50 bg-amber-50 p-2 text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">Canonical dataset state · {canonicalState.replace('_', ' ')}. This placement remains distinct for navigation and recovery, but its Source action is unavailable.
          <button type="button" onClick={onRetry} className="ml-2 font-semibold underline">Retry canonical dataset</button>
        </div>}
        {resource.lastKnown && placementState === 'current' && !canonicalUnavailable && <div role="status" className="rounded-md border border-amber-300/50 bg-amber-50 p-2 text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">Last-known placement metadata{resource.lastResolvedAt ? ` · last resolved ${new Date(resource.lastResolvedAt).toLocaleString()}` : ''}</div>}
        {alternatePlacements.length > 0 && <div className="rounded-md border border-border bg-muted/25 p-2 text-[11px] text-muted-foreground"><div className="font-semibold text-foreground">Also observed at</div><div className="mt-1 grid gap-1">{alternatePlacements.map((placement) => <div key={placement.placementId} className="truncate" title={placement.path}>{placement.path}</div>)}</div><div className="mt-1">Only placements already loaded in this Workspace session are shown.</div></div>}
        {source && source.completeness !== 'complete' && <div role="status" className="rounded-md border border-amber-300/50 bg-amber-50 p-2 text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">Source state: {source.completeness}{statusMessage(source) ? ` — ${statusMessage(source)}` : ''}</div>}
      </div>
      <div className="mt-auto rounded-lg border border-border bg-muted/35 p-3 text-[11.5px] leading-5 text-muted-foreground">
        This provider placement is source-only. Using the dataset creates only a local Source; it never writes to the provider. Other Workspace placements use that same canonical Source.
        <button onClick={onUse} disabled={source?.completeness !== 'complete' || resource.lastKnown || placementState !== 'current' || canonicalUnavailable}
          className="mt-3 block w-full rounded-md bg-foreground px-3 py-2 font-semibold text-background disabled:opacity-50">Use in canvas</button>
      </div>
    </div>
  </div>
}

function ProviderDatasetActionDialog({ resource, container, files, currentCanvasId, targetState, onClose, onOpened }: {
  resource: WorkspaceResource; container: WorkspaceResource | null; files: CanvasFile[]; currentCanvasId: string; targetState: CanvasTargetState
  onClose: () => void; onOpened: (canvasId: string) => void
}) {
  const editable = targetState === 'ready'
    ? files.filter((file) => file.role === 'owner' || file.role === 'editor') : []
  const [mode, setMode] = useState<'explore' | 'current' | 'choose'>('explore')
  const [name, setName] = useState(`${resource.name} exploration`)
  const [canvasId, setCanvasId] = useState(editable[0]?.id ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
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
    setBusy(true); setError(null)
    try {
      if (mode === 'explore') {
        if (!destination) throw new Error('Load an exact writable local Canvas destination first')
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
        onOpened(created.id)
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
    } catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label={`Use ${resource.name}`} onClose={onClose}>
    <p className="text-[11px] leading-5 text-muted-foreground">Only the stable provider identity and display metadata are stored locally; data and credentials are not copied, and the provider is never mutated. {isExternal(container) && destination && 'The new Canvas is a locally owned overlay beside this source-only provider resource.'}</p>
    <div className="grid gap-2 sm:grid-cols-3">
      <button onClick={() => setMode('explore')} aria-pressed={mode === 'explore'} className={`rounded-lg border p-3 text-left ${mode === 'explore' ? 'border-primary bg-primary/5' : 'border-border'}`}><span className="block text-[12px] font-semibold">Explore in a new Canvas</span></button>
      <button onClick={() => setMode('current')} disabled={targetState !== 'ready' || !currentCanvas} aria-pressed={mode === 'current'} className={`rounded-lg border p-3 text-left disabled:opacity-50 ${mode === 'current' ? 'border-primary bg-primary/5' : 'border-border'}`}><span className="block text-[12px] font-semibold">Add to this Canvas</span><span className="text-[10.5px] text-muted-foreground">{currentCanvas ? currentCanvas.name : 'No editable current Canvas'}</span></button>
      <button onClick={() => setMode('choose')} disabled={targetState !== 'ready'} aria-pressed={mode === 'choose'} className={`rounded-lg border p-3 text-left disabled:opacity-50 ${mode === 'choose' ? 'border-primary bg-primary/5' : 'border-border'}`}><span className="block text-[12px] font-semibold">Choose a Canvas</span></button>
    </div>
    {mode === 'explore' ? <label className="grid gap-1 text-[11px] text-muted-foreground">New canvas name<input value={name} onChange={(event) => setName(event.target.value)} className="dp-input" /></label>
      : targetState !== 'ready' ? <div role="status" className="text-[12px] text-muted-foreground">{targetState === 'loading' ? 'Refreshing editable Canvases…' : 'Editable Canvases could not be refreshed. Close and try again.'}</div>
      : mode === 'current' && currentCanvas ? <div className="text-[11px] text-muted-foreground">Selected Canvas: <strong className="text-foreground">{currentCanvas.name}</strong></div>
      : editable.length ? <label className="grid gap-1 text-[11px] text-muted-foreground">Choose a Canvas<select aria-label="Target canvas" value={canvasId} onChange={(event) => setCanvasId(event.target.value)} className="dp-input">{editable.map((file) => <option key={file.id} value={file.id}>{file.name}</option>)}</select></label>
        : <div role="status" className="text-[12px] text-muted-foreground">No editable canvas is available.</div>}
    {mode === 'explore' && !destination && <div role="status" className="text-[12px] text-muted-foreground">{canvasDestinationTitle(container, 'create')}</div>}
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
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
    <p className="text-[12px] leading-5 text-muted-foreground">Choose the exact provider identity. Names are never used to repair a binding, and this action creates a new auditable Workspace reference.</p>
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
