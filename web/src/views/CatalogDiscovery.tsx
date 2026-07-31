import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useStore } from '../store/graph'
import { api, KernelError } from '../api/client'
import { color, radius } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { VirtualList } from '../ui/VirtualList'
import { FileDialog } from '../ui/FileDialog'
import { DatasetRevisionHistory } from './DatasetRevisionHistory'
import { FieldEvidenceButton } from '../components/FieldEvidenceDetail'
import { MediaCellRenderer } from '../components/MediaCellRenderer'
import { PreviewDetails, PreviewSummary } from '../components/PreviewPresentation'
import type {
  CatalogQueryParams, CatalogTable, CatalogUnregisterResult, DatasetRevisionDetail,
  DatasetRevisionResolution, Facets, FolderNode, KernelInfo, LineageResult, SampleResult,
} from '../types/api'

// The Workspace dataset discovery surface is built to browse thousands of datasets. Nothing is loaded up front: a left
// FOLDER TREE (lazy), a center VIRTUALIZED list fed by a server-side filtered/sorted/paginated query
// (infinite scroll), and a right FACET RAIL (tags/owners with counts). A search box (debounced) and a
// sort control drive the same query; clicking a row opens the shared dataset viewer to inspect rows,
// columns, revisions, and lineage
// and curate the dataset's folder/tags/owner/description.

const PAGE = 50
export const CATALOG_BATCH_LIMIT = 50
export const UPLOAD_FILE_ACCEPT = '.parquet,.pq,.csv,.tsv,.json,.ndjson,.arrow,.feather,.ipc'
const ROW_H = 58
type Sort = NonNullable<CatalogQueryParams['sort']>
const errorMessage = (e: unknown) => e instanceof Error ? e.message : String(e)
const statusOf = (e: unknown) => e instanceof KernelError ? e.status
  : typeof e === 'object' && e !== null ? (e as { status?: unknown }).status : undefined
const sameRevision = (
  left: { datasetId: string; revisionId: string } | null,
  right: { datasetId: string; revisionId: string } | null,
) => left !== null && right !== null
  && left.datasetId === right.datasetId && left.revisionId === right.revisionId
const revisionLabel = (revision: { datasetId: string; revisionId: string }) =>
  `${revision.datasetId}@${revision.revisionId}`

/**
 * The bounded catalog browser is deliberately independent from the destination of a `Use` action.
 * Workspace supplies an explicit target without copying its query, paging, selection, or curation behavior.
 */
export interface CatalogDiscoveryProps {
  sourceIdentity: KernelInfo | null
  foldersMutable: boolean
  onUseTables: (tables: CatalogTable[]) => void
  onUploadDataset: (file: File) => Promise<CatalogTable | null>
  title?: string
  queryState?: CatalogDiscoveryQueryState
  onQueryStateChange?: (state: CatalogDiscoveryQueryState) => void
  selectedRegistrationId?: string | null
  onSelectedTableChange?: (table: CatalogTable | null, origin?: 'user' | 'route') => void
  /** Optional Workspace bridge; Catalog remains independently reusable without it. */
  onOpenInWorkspace?: (table: CatalogTable) => void
  workspaceLocation?: {
    state: 'resolving' | 'available' | 'unavailable'
    reason?: string
    retryable?: boolean
  }
  onRetryWorkspaceLocation?: () => void
  /** Exact revision requested by a Catalog deep link; it is not a moving-head selection. */
  initialRevisionId?: string
  /** Authoritative logical dataset identity paired with the requested revision. */
  initialRevisionDatasetId?: string
  /** Accessible Back destination for a routed full-page detail. */
  detailBackLabel?: string
}

export interface CatalogDiscoveryQueryState {
  q: string
  folder: string
  tags: string[]
  owner: string
  hasColumns: string[]
  sort: Sort
  order: 'asc' | 'desc'
  match: 'text' | 'meaning'
}

export const emptyCatalogDiscoveryQuery = (): CatalogDiscoveryQueryState => ({
  q: '', folder: '', tags: [], owner: '', hasColumns: [], sort: 'name', order: 'asc', match: 'text',
})

export function CatalogDiscovery({
  sourceIdentity: catalogSource, foldersMutable, onUseTables, onUploadDataset, title = 'Datasets',
  queryState, onQueryStateChange, selectedRegistrationId, onSelectedTableChange, onOpenInWorkspace,
  workspaceLocation, onRetryWorkspaceLocation,
  initialRevisionId, initialRevisionDatasetId, detailBackLabel,
}: CatalogDiscoveryProps) {
  const pushToast = useStore((s) => s.pushToast)
  const fileRef = useRef<HTMLInputElement>(null)
  const selectedTableChangeRef = useRef(onSelectedTableChange)
  selectedTableChangeRef.current = onSelectedTableChange

  // query state
  const [localQuery, setLocalQuery] = useState<CatalogDiscoveryQueryState>(emptyCatalogDiscoveryQuery)
  const query = queryState ?? localQuery
  const { q, folder, tags, owner, hasColumns, sort, order, match } = query
  const commitQuery = (next: CatalogDiscoveryQueryState) => {
    if (queryState && onQueryStateChange) onQueryStateChange(next)
    else setLocalQuery(next)
  }
  const setQueryField = <K extends keyof CatalogDiscoveryQueryState>(
    key: K, value: CatalogDiscoveryQueryState[K] | ((current: CatalogDiscoveryQueryState[K]) => CatalogDiscoveryQueryState[K]),
  ) => {
    const next = typeof value === 'function'
      ? (value as (current: CatalogDiscoveryQueryState[K]) => CatalogDiscoveryQueryState[K])(query[key])
      : value
    commitQuery({ ...query, [key]: next })
  }
  const setQ = (value: string) => setQueryField('q', value)
  const setFolder = (value: string | ((current: string) => string)) => setQueryField('folder', value)
  const setTags = (value: string[] | ((current: string[]) => string[])) => setQueryField('tags', value)
  const setOwner = (value: string) => setQueryField('owner', value)
  const setHasColumns = (value: string[] | ((current: string[]) => string[])) => setQueryField('hasColumns', value)
  const setMatch = (value: 'text' | 'meaning') => setQueryField('match', value)
  const [rawQ, setRawQ] = useState(q)
  useEffect(() => { setRawQ(q) }, [q])

  // results + facets
  const [items, setItems] = useState<CatalogTable[]>([])
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loadingMoreState, setLoadingMoreState] = useState(false)
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null)
  const [facets, setFacets] = useState<Facets>({ folders: [], tags: [], owners: [] })
  const [selected, setSelected] = useState<CatalogTable | null>(null)
  const [selectionError, setSelectionError] = useState<string | null>(null)
  const [selectionRevision, setSelectionRevision] = useState(0)
  const [registerOpen, setRegisterOpen] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [unregisterResult, setUnregisterResult] = useState<{
    response: CatalogUnregisterResult; names: Record<string, string>
  } | null>(null)
  const [catalogRevision, setCatalogRevision] = useState(0)
  const seq = useRef(0)
  const loadingMore = useRef(false)
  const selectionSeq = useRef(0)
  // The displayed table and the identity used to resolve it are distinct during exact-revision
  // navigation: a receipt logical id can resolve to a current registration id.
  const selectedLookupId = useRef<string | null>(null)
  // A route may temporarily retain a retired registration while Workspace canonicalizes it after
  // resolution. Remembering that route selection avoids immediately clearing the resolved table
  // and issuing the same failed lookup again during that hand-off.
  const resolvedRouteSelection = useRef<string | null>(null)

  // debounce the search box into the query
  useEffect(() => {
    const next = rawQ.trim()
    if (next === q) return
    const timer = setTimeout(() => setQ(next), 250)
    return () => clearTimeout(timer)
  }, [rawQ, q])

  const params = useMemo<CatalogQueryParams>(
    () => ({ q: q || undefined, folder: folder || undefined, tags, owner: owner || undefined, hasColumns, sort, order, limit: PAGE }),
    [q, folder, tags, owner, hasColumns, sort, order])
  const semantic = match === 'meaning' && !!q  // "meaning" mode: ranked hybrid search instead of paging

  const loadFirst = useCallback(async () => {
    const s = ++seq.current
    loadingMore.current = false
    setLoading(true); setError(null); setLoadingMoreState(false); setLoadMoreError(null)
    // A changed filter must not leave the previous query's rows/facets visible while the new
    // request is in flight. The loading state below is the only claim we can make until it returns.
    setItems([]); setTotal(0); setHasMore(false); setSelectedIds(new Set())  // a new query invalidates the old selection
    setFacets((cur) => ({ folders: [], tags: [], owners: [], semanticAvailable: cur.semanticAvailable }))
    try {
      let page: { items: CatalogTable[]; total: number; hasMore: boolean }
      let fc: Facets
      if (semantic) {
        const hits = await api.searchCatalog({
          q, folder: params.folder, tags: params.tags, owner: params.owner,
          hasColumns: params.hasColumns, limit: 100,
        }, 'hybrid')
        page = { items: hits, total: hits.length, hasMore: false }
        // Server facets are lexical and therefore describe a different result set. Counts shown in
        // meaning mode are intentionally computed from the bounded ranked results the user can see.
        fc = rankedResultFacets(hits)
      } else {
        [page, fc] = await Promise.all([
          api.tablesPage({ ...params, offset: 0 }),
          api.facets(params),
        ])
      }
      if (s !== seq.current) return  // a newer query superseded this one
      setItems(page.items); setTotal(page.total); setHasMore(page.hasMore); setFacets(fc)
    } catch (e) {
      if (s !== seq.current) return
      setItems([]); setTotal(0); setHasMore(false); setError((e as Error).message)  // never show stale results under new filters
    } finally {
      if (s === seq.current) setLoading(false)
    }
  }, [params, semantic, q])

  useEffect(() => { void loadFirst() }, [loadFirst])

  const selectTable = useCallback((table: CatalogTable | null, origin: 'user' | 'route' = 'user') => {
    selectedLookupId.current = table?.registrationId ?? null
    setSelected(table)
    onSelectedTableChange?.(table, origin)
  }, [onSelectedTableChange])
  useEffect(() => {
    if (selectedRegistrationId === undefined) return
    const requestId = ++selectionSeq.current
    setSelectionError(null)
    const hasExactRevision = !!initialRevisionId && !!initialRevisionDatasetId
    // A complete exact revision pair names its authoritative logical dataset. The path resource
    // is only a stale/canonicalizable Workspace projection and must never select a different
    // table before the revision view opens.
    const lookupId = hasExactRevision ? initialRevisionDatasetId : selectedRegistrationId
    const routeProjection = selectedRegistrationId ?? lookupId
    const routeLookupKey = `${routeProjection}\u0000${lookupId}`
    if (!lookupId) {
      resolvedRouteSelection.current = null
      selectedLookupId.current = null
      setSelected(null)
      selectedTableChangeRef.current?.(null, 'route')
      return
    }
    if (selected?.registrationId === selectedRegistrationId
        && selectedLookupId.current === lookupId) {
      // The parent has consumed the transient route selection and canonicalized its URL. Clear
      // only the transient hand-off guard; retain selectedLookupId as evidence that this table
      // was resolved from the current authoritative lookup identity.
      resolvedRouteSelection.current = null
      return
    }
    if (resolvedRouteSelection.current === routeLookupKey) return
    setSelected(null)
    void api.tableByRegistration(lookupId).then((table) => {
      if (requestId === selectionSeq.current) {
        resolvedRouteSelection.current = routeLookupKey
        selectedLookupId.current = lookupId
        setSelected(table)
        selectedTableChangeRef.current?.(table, 'route')
      }
    }).catch((caught) => {
      if (requestId === selectionSeq.current) setSelectionError(errorMessage(caught))
    })
    return () => { selectionSeq.current += 1 }
  }, [selectedRegistrationId, initialRevisionId, initialRevisionDatasetId, selectionRevision])

  const loadMore = useCallback(async () => {
    if (!hasMore || loading || loadingMore.current) return
    loadingMore.current = true
    setLoadingMoreState(true); setLoadMoreError(null)
    const s = seq.current
    try {
      const page = await api.tablesPage({ ...params, offset: items.length })
      if (s !== seq.current) return
      // dedupe by id: offsets drift when the catalog changes between pages
      setItems((cur) => {
        const seen = new Set(cur.map((t) => t.id))
        const fresh = page.items.filter((t) => !seen.has(t.id))
        return fresh.length ? [...cur, ...fresh] : cur
      })
      setHasMore(page.hasMore)
    } catch (e) {
      if (s === seq.current) setLoadMoreError(errorMessage(e))
    } finally {
      if (s === seq.current) {
        loadingMore.current = false
        setLoadingMoreState(false)
      }
    }
  }, [hasMore, loading, params, items.length])

  const toggleTag = (t: string) => setTags((cur) => cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t])
  const clearFilters = () => {
    setRawQ('')
    commitQuery({ ...emptyCatalogDiscoveryQuery(), sort, order, match })
  }
  const hasFilters = !!(folder || tags.length || owner || hasColumns.length || q)

  const onRegistered = (t: CatalogTable) => {
    setRegisterOpen(false)
    setCatalogRevision((v) => v + 1)
    pushToast(`Registered “${t.name}”`, 'success')
    void loadFirst()
  }
  // folder-tree mutations: reload the tree (via the key bump) + the row list, and keep the selected
  // folder filter pointing at where its datasets went so a rename/delete can't strand the view
  const onFolderCreated = () => { setCatalogRevision((v) => v + 1) }
  const onFolderRenamed = (oldPath: string, newPath: string) => {
    setFolder((cur) => cur === oldPath ? newPath : cur.startsWith(`${oldPath}/`) ? newPath + cur.slice(oldPath.length) : cur)
    setCatalogRevision((v) => v + 1); void loadFirst()
  }
  const onFolderDeleted = (path: string) => {
    const parent = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : ''
    setFolder((cur) => cur === path || cur.startsWith(`${path}/`) ? parent : cur)
    setCatalogRevision((v) => v + 1); void loadFirst()
  }
  const toggleSelect = (id: string) => setSelectedIds((cur) => {
    const next = new Set(cur)
    if (next.has(id)) next.delete(id)
    else if (next.size < CATALOG_BATCH_LIMIT) next.add(id)
    else pushToast(`A dataset action is limited to ${CATALOG_BATCH_LIMIT} items`, 'info')
    return next
  })
  const clearSelection = () => setSelectedIds(new Set())
  const selectAllLoaded = () => setSelectedIds(new Set(items.slice(0, CATALOG_BATCH_LIMIT).map((t) => t.id)))
  const useSelected = () => {
    const ts = items.filter((t) => selectedIds.has(t.id)); if (!ts.length) return
    onUseTables(ts)
    clearSelection()
  }
  const deleteSelected = async () => {
    const tables = items.filter((table) => selectedIds.has(table.id)); if (!tables.length) return
    const targets = tables.flatMap((table) => table.metadataRevision && table.registrationId
      ? [{ id: table.id, expectedRegistrationId: table.registrationId, expectedRevision: table.metadataRevision }] : [])
    if (targets.length !== tables.length) {
      pushToast('Reload before removing: at least one dataset has no version precondition', 'error')
      return
    }
    if (!window.confirm(
      `Unregister ${targets.length} dataset${targets.length === 1 ? '' : 's'}? `
      + 'This removes catalog registrations, not underlying data. '
      + 'The operation is best effort: each item is version-checked and the result may be partial.',
    )) return
    try {
      const result = await api.unregisterTables(targets)
      setUnregisterResult({
        response: result,
        names: Object.fromEntries(tables.map((table) => [table.id, table.name])),
      })
      const counts = result.results.reduce<Record<string, number>>((current, item) => {
        current[item.status] = (current[item.status] ?? 0) + 1
        return current
      }, {})
      const failures = (counts.conflict ?? 0) + (counts.failed ?? 0)
      pushToast(
        `Unregister result: ${counts.unregistered ?? 0} unregistered, ${counts.missing ?? 0} already gone`
        + (failures ? `, ${failures} need review` : ''),
        failures ? 'info' : 'success',
      )
    } catch (e) { pushToast(errorMessage(e), 'error') }
    clearSelection(); setCatalogRevision((v) => v + 1); await loadFirst()
  }
  const onUpload = async (f?: File) => {
    if (!f) return
    if (await onUploadDataset(f)) {
      setCatalogRevision((v) => v + 1)
      await loadFirst()
    }
  }
  // warm the working set first, or the new source node can't resolve its table and shows "Select dataset"
  const use = (t: CatalogTable) => onUseTables([t])

  if (selected) {
    return (
      <div className="relative h-full">
        <CatalogDetail key={selected.id} table={selected} onClose={() => selectTable(null)} onUse={use}
          backLabel={detailBackLabel}
          initialRevisionId={initialRevisionId}
          initialRevisionDatasetId={initialRevisionDatasetId}
          onChanged={(t) => {
            // Saving catalog metadata refreshes the selected registration; it does not navigate
            // away from an exact-revision route.
            selectTable(t, initialRevisionId && initialRevisionDatasetId ? 'route' : 'user')
            setCatalogRevision((v) => v + 1)
            void loadFirst()
          }}
          onFolder={(folder) => {
            if (onOpenInWorkspace) void onOpenInWorkspace(selected)
            else { setFolder(folder); selectTable(null) }
          }}
          folderActionLabel={onOpenInWorkspace ? 'Open in Workspace' : undefined}
          folderActionVisible={!!onOpenInWorkspace || !!selected.folder}
          folderActionDisabled={!!onOpenInWorkspace && (!selected.registrationId
            || workspaceLocation?.state === 'resolving' || workspaceLocation?.state === 'unavailable')}
          folderActionTitle={!selected.registrationId ? 'This dataset is not currently available in Workspace.'
            : workspaceLocation?.state === 'resolving' ? 'Resolving this dataset’s Workspace location…'
              : workspaceLocation?.state === 'unavailable'
                ? workspaceLocation.reason ?? 'This dataset is not currently available in Workspace.' : undefined}
          onFolderRetry={workspaceLocation?.state === 'unavailable' && workspaceLocation.retryable
            ? onRetryWorkspaceLocation : undefined}
          onDeleted={() => { selectTable(null); setCatalogRevision((v) => v + 1); void loadFirst() }}
          onOpenTable={selectTable}
          onColumn={(c) => { setHasColumns((cur) => cur.includes(c) ? cur : [...cur, c]); selectTable(null) }} />
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      {/* header: title + register / upload */}
      <div className="flex items-center gap-3 px-7 pb-3 pt-5">
        <h1 className="text-[20px] font-bold text-foreground">{title}</h1>
        <span className="text-[12px] text-muted-foreground">{total.toLocaleString()} {total === 1 ? 'dataset' : 'datasets'}</span>
        <span className="flex-1" />
        <button onClick={() => setRegisterOpen(true)} data-testid="register-dataset" title="Register a dataset the kernel can access by path or URI"
          className="inline-flex items-center gap-1.5 rounded-lg bg-foreground px-3.5 py-1.5 text-[12.5px] font-semibold text-background">
          <Icon name="plus" size={13} /> Register path or URI
        </button>
        <button onClick={() => fileRef.current?.click()} title="Upload a dataset file"
          className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-3.5 py-1.5 text-[12.5px] font-semibold text-foreground">
          <Icon name="export" size={13} /> Upload
        </button>
        <input ref={fileRef} type="file" accept={UPLOAD_FILE_ACCEPT} className="hidden"
          onChange={(e) => { void onUpload(e.target.files?.[0]); e.target.value = '' }} />
      </div>

      {/* search + sort + active filters */}
      <div className="flex items-center gap-2 px-7 pb-2">
        <div className="relative flex-1">
          <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"><Icon name="search" size={13} /></span>
          <input value={rawQ} onChange={(e) => setRawQ(e.target.value)} data-testid="catalog-search"
            placeholder="Search by name, folder, description, or column…" aria-label="Search datasets"
            className="w-full rounded-lg border border-border bg-card py-1.5 pl-8 pr-3 text-[13px] outline-none focus:border-primary" />
        </div>
        {q && facets.semanticAvailable && (
          <div className="flex items-center rounded-lg border border-border bg-card p-0.5 text-[11.5px]">
            <span className="px-1.5 text-muted-foreground">Match:</span>
            {(['text', 'meaning'] as const).map((m) => (
              <button key={m} onClick={() => setMatch(m)} data-testid={`match-${m}`}
                className={`rounded-md px-2 py-1 ${match === m ? 'bg-accent font-semibold text-accent-foreground' : 'text-muted-foreground hover:text-foreground'}`}>{m}</button>
            ))}
          </div>
        )}
        <select value={`${sort}:${order}`} onChange={(e) => {
          const [nextSort, nextOrder] = e.target.value.split(':')
          commitQuery({ ...query, sort: nextSort as Sort, order: nextOrder as 'asc' | 'desc' })
        }}
          disabled={semantic} aria-label="Sort datasets"
          className="rounded-lg border border-border bg-card px-2 py-1.5 text-[12.5px] outline-none disabled:opacity-50" data-testid="catalog-sort">
          <option value="name:asc">Name A–Z</option>
          <option value="name:desc">Name Z–A</option>
          <option value="rows:desc">Most rows</option>
          <option value="usage:desc">Most used</option>
          <option value="updated:desc">Recently updated</option>
          <option value="folder:asc">Folder</option>
        </select>
      </div>
      {hasFilters && (
        <div className="flex flex-wrap items-center gap-1.5 px-7 pb-2 text-[11.5px]">
          {folder && <Chip label={`📁 ${folder}`} onClear={() => setFolder('')} />}
          {tags.map((t) => <Chip key={t} label={`#${t}`} onClear={() => toggleTag(t)} />)}
          {owner && <Chip label={`@${owner}`} onClear={() => setOwner('')} />}
          {hasColumns.map((c) => <Chip key={c} label={`has column ${c}`} onClear={() => setHasColumns((cur) => cur.filter((x) => x !== c))} />)}
          {q && <Chip label={`"${q}"`} onClear={() => { setRawQ(''); setQ('') }} />}
          <button onClick={clearFilters} className="text-[11px] text-muted-foreground underline">clear all</button>
        </div>
      )}

      {selectionError && <div role="alert" className="flex items-center gap-2 border-t border-destructive/30 bg-destructive/5 px-7 py-2 text-[11.5px] text-destructive">
        <span className="min-w-0 flex-1 truncate">Couldn't open the selected dataset: {selectionError}</span>
        <button onClick={() => setSelectionRevision((current) => current + 1)} className="shrink-0 font-semibold underline">Retry</button>
        <button onClick={() => selectTable(null)} className="shrink-0 font-semibold underline">Clear selection</button>
      </div>}

      {selectedIds.size > 0 && (
        <div className="flex items-center gap-2 px-7 pb-2 text-[12px]" data-testid="catalog-selection-bar">
          <span className="font-semibold text-foreground">{selectedIds.size} selected</span>
          {selectedIds.size > 1 && <span className="text-muted-foreground">Up to {CATALOG_BATCH_LIMIT} datasets</span>}
          <button onClick={useSelected} className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-1 font-semibold text-primary hover:bg-accent">
            <Icon name="plus" size={11} /> Use
          </button>
          <button onClick={() => void deleteSelected()} data-testid="catalog-delete-selected"
            disabled={!catalogSource?.capabilities?.includes('catalog.cas_unregister')}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-1 font-semibold text-destructive hover:bg-accent">
            <Icon name="trash" size={11} /> Unregister
          </button>
          <button onClick={clearSelection} className="rounded-md px-2 py-1 text-muted-foreground hover:text-foreground">Clear</button>
          <span className="flex-1" />
          {selectedIds.size < items.length && (
            <button onClick={selectAllLoaded} className="text-[11px] text-muted-foreground underline">
              Select first {Math.min(items.length, CATALOG_BATCH_LIMIT)} loaded
            </button>
          )}
        </div>
      )}

      {unregisterResult && <div role="status" data-testid="catalog-unregister-result" className="border-t border-border bg-muted/25 px-7 py-2 text-[11px] text-muted-foreground">
        <div className="flex items-center gap-2">
          <strong className="text-foreground">Best-effort unregister result</strong>
          <span>Each item was checked against its registration and metadata revision.</span>
          <button onClick={() => setUnregisterResult(null)} className="ml-auto font-semibold underline">Dismiss</button>
        </div>
        <div className="mt-1 flex max-h-20 flex-wrap gap-x-3 gap-y-0.5 overflow-y-auto">
          {unregisterResult.response.results.map((item) => <span key={item.id} title={item.detail ?? undefined} className={item.status === 'unregistered' || item.status === 'missing' ? '' : 'text-destructive'}>
            {unregisterResult.names[item.id] ?? item.id}: {item.status}{item.detail ? ` — ${item.detail}` : ''}
          </span>)}
        </div>
      </div>}

      {/* body: folder tree | list | facets */}
      <div className="flex min-h-0 flex-1 border-t border-border">
        <div className="w-[220px] flex-[0_0_220px] overflow-y-auto border-r border-border p-2">
          <FolderTree revision={catalogRevision} sourceIdentity={catalogSource} mutable={foldersMutable} selected={folder} onSelect={setFolder}
            onCreated={onFolderCreated} onRenamed={onFolderRenamed} onDeleted={onFolderDeleted} />
        </div>

        <div className="flex min-w-0 flex-1 flex-col">
          {error ? (
            <div className="grid flex-1 place-items-center px-3 py-2">
              <div className="flex flex-col items-center gap-2 text-[13px] text-muted-foreground">
                <span>Couldn't load the catalog: {error}</span>
                <button onClick={() => void loadFirst()} data-testid="catalog-retry"
                  className="rounded-md border border-border bg-card px-3 py-1 text-[12px] font-semibold text-foreground hover:bg-accent">Retry</button>
              </div>
            </div>
          ) : (
            <VirtualList
              items={items}
              rowHeight={ROW_H}
              onEndReached={semantic || loadMoreError ? undefined : loadMore}
              resetKey={semantic ? `meaning:${q}` : params}
              className="flex-1 px-3 py-2"
              emptyNote={<div className="grid h-full place-items-center text-[13px] text-muted-foreground">
                {loading ? 'Loading…' : hasFilters ? 'No datasets match these filters.' : 'No datasets registered — add one above.'}
              </div>}
              renderRow={(t) => <TableRow t={t} selected={selectedIds.has(t.id)} selectionActive={selectedIds.size > 0}
                onToggleSelect={() => toggleSelect(t.id)} onOpen={() => selectTable(t)} onUse={() => use(t)} onFolder={setFolder} />}
            />
          )}
          <div className="border-t border-border px-4 py-1.5 text-[11px] text-muted-foreground">
            {loadMoreError ? (
              <span role="alert" className="inline-flex items-center gap-2 text-destructive">
                Couldn't load more: {loadMoreError}
                <button onClick={() => void loadMore()} data-testid="catalog-load-more-retry"
                  className="font-semibold underline">Retry</button>
              </span>
            ) : loadingMoreState ? 'Loading more…'
              : semantic
                ? `Top ${items.length.toLocaleString()} by relevance`
                : `Showing ${items.length.toLocaleString()} of ${total.toLocaleString()}${hasMore ? ' — scroll for more' : ''}`}
          </div>
        </div>

        <div className="w-[220px] flex-[0_0_220px] overflow-y-auto border-l border-border p-3">
          {semantic && (
            <div className="mb-2 text-[10px] leading-snug text-muted-foreground">
              Facet counts within these top {items.length.toLocaleString()} meaning results
            </div>
          )}
          <FacetGroup title="Tags">
            {facets.tags.map((f) => (
              <FacetRow key={f.value} label={`#${f.value}`} count={f.count}
                active={tags.includes(f.value)} onClick={() => toggleTag(f.value)} />
            ))}
            {!facets.tags.length && <Empty />}
          </FacetGroup>
          <FacetGroup title="Owners">
            {facets.owners.map((f) => (
              <FacetRow key={f.value} label={`@${f.value}`} count={f.count}
                active={owner === f.value} onClick={() => setOwner(owner === f.value ? '' : f.value)} />
            ))}
            {!facets.owners.length && <Empty />}
          </FacetGroup>
        </div>
      </div>

      {registerOpen && <RegisterModal onClose={() => setRegisterOpen(false)} onRegistered={onRegistered} />}

      {/* Facets stay bounded with the active query. Empty folders remain discoverable through
          the lazy folder tree rather than forcing every folder into the page. */}
      <datalist id="dp-folder-options">
        {facets.folders.map((item) => <option key={item.value} value={item.value} />)}
      </datalist>
    </div>
  )
}

export function rankedResultFacets(items: CatalogTable[]): Facets {
  const count = (values: (string | null | undefined)[]) => {
    const counts = new Map<string, number>()
    for (const raw of values) {
      const value = raw?.trim()
      if (value) counts.set(value, (counts.get(value) ?? 0) + 1)
    }
    return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([value, n]) => ({ value, count: n }))
  }
  return {
    folders: count(items.map((t) => t.folder)),
    tags: count(items.flatMap((t) => [...new Set(t.tags ?? [])])),
    owners: count(items.map((t) => t.owner)),
    semanticAvailable: true,
  }
}

function Chip({ label, onClear }: { label: string; onClear: () => void }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-accent px-2 py-0.5 text-accent-foreground">
      {label}
      <button type="button" onClick={onClear} aria-label={`Remove filter ${label}`} className="opacity-60 hover:opacity-100"><Icon name="close" size={10} /></button>
    </span>
  )
}

function Empty() { return <div className="px-1 py-1 text-[11px] text-muted-foreground">—</div> }

function FacetGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-3">
      <div className="mb-1 px-1 text-[10px] font-bold uppercase tracking-wide text-muted-foreground">{title}</div>
      <div className="flex flex-col gap-px">{children}</div>
    </div>
  )
}

function FacetRow({ label, count, active, onClick }: { label: string; count: number; active: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick}
      className={`flex items-center justify-between gap-2 rounded-md px-2 py-1 text-left text-[12px] hover:bg-accent ${active ? 'bg-accent font-semibold text-accent-foreground' : 'text-muted-foreground'}`}>
      <span className="truncate">{label}</span>
      <span className="text-[10.5px] tabular-nums opacity-70">{count.toLocaleString()}</span>
    </button>
  )
}

function TableRow({ t, selected, selectionActive, onToggleSelect, onOpen, onUse, onFolder }: {
  t: CatalogTable; selected: boolean; selectionActive: boolean; onToggleSelect: () => void
  onOpen: () => void; onUse: () => void; onFolder: (f: string) => void
}) {
  // Checkbox / Open / folder / Use are sibling controls — a single role=button wrapping nested buttons
  // is both invalid HTML and an axe nested-interactive failure on the Tables surface.
  return (
    <div
      className={`group mx-1 flex h-[54px] items-center gap-2 rounded-lg border bg-card pr-2 hover:border-primary/40 hover:bg-accent ${selected ? 'border-primary/60' : 'border-border'}`}
      style={{ opacity: t.missing ? 0.55 : 1 }}>
      <label onClick={(e) => e.stopPropagation()}
        className={`flex h-full shrink-0 cursor-pointer items-center pl-2.5 ${!selected && !selectionActive ? 'opacity-0 group-hover:opacity-100 focus-within:opacity-100' : ''}`}>
        <input type="checkbox" checked={selected} onChange={onToggleSelect} aria-label={`Select ${t.name}`}
          className="h-3.5 w-3.5 cursor-pointer accent-primary" />
      </label>
      <button type="button" onClick={onOpen} aria-label={`Open dataset ${t.name}`}
        className="flex h-full min-w-0 flex-1 cursor-pointer items-center gap-3 rounded-lg border-0 bg-transparent pl-1 pr-3 text-left text-inherit">
        <Icon name="db" size={16} style={{ color: color.text3 }} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-semibold text-foreground">{t.name}</span>
            {t.missing && <span className="rounded bg-destructive/10 px-1.5 text-[9.5px] font-semibold text-destructive">missing</span>}
            {(t.tags ?? []).slice(0, 3).map((tag) => (
              <span key={tag} className="rounded-full bg-muted px-1.5 text-[9.5px] text-muted-foreground">#{tag}</span>
            ))}
          </div>
          <div className="truncate text-[11px] text-muted-foreground">{t.folder ?? t.uri}</div>
        </div>
        <span className="text-[11px] text-muted-foreground">{t.columns?.length ?? 0} cols</span>
        {t.rowCount != null && <span className="text-[11px] text-muted-foreground">· {t.rowCount.toLocaleString()} rows</span>}
        {t.owner && <span className="hidden text-[11px] text-muted-foreground lg:inline">· @{t.owner}</span>}
      </button>
      {t.folder && (
        <button type="button" onClick={() => onFolder(t.folder!)} aria-label={`Browse folder ${t.folder}`}
          className="shrink-0 truncate text-[11px] text-muted-foreground hover:text-foreground hover:underline">
          Folder
        </button>
      )}
      <button type="button" onClick={onUse} aria-label={`Use dataset ${t.name}`}
        className="inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-[11px] font-semibold text-primary opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus:opacity-100">
        <Icon name="plus" size={12} /> Use
      </button>
    </div>
  )
}

// ---- folder tree (lazy) -----------------------------------------------------
// Folders are first-class: create an empty one up front, or rename/delete an existing one (cascading
// to its datasets + subfolders). Mutations bubble up so the parent can refresh + keep the filter valid.
interface FolderActions {
  onCreated: () => void
  onRenamed: (oldPath: string, newPath: string) => void
  onDeleted: (path: string) => void
}

function FolderTree({ selected, onSelect, onCreated, onRenamed, onDeleted, revision, sourceIdentity, mutable }:
  { selected: string; onSelect: (f: string) => void; revision: number; sourceIdentity: KernelInfo | null; mutable: boolean } & FolderActions) {
  const pushToast = useStore((s) => s.pushToast)
  const [root, setRoot] = useState<FolderNode[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const request = useRef(0)
  // expansion (a set of open paths) lives here so a rename/remount keeps it; remap prefixes on rename,
  // drop the subtree on delete.
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const toggleExpand = (p: string) => setExpanded((s) => {
    const n = new Set(s)
    if (n.has(p)) n.delete(p)
    else n.add(p)
    return n
  })
  const renamed = (oldPath: string, newPath: string) => {
    setExpanded((s) => new Set([...s].map((p) =>
      p === oldPath ? newPath : p.startsWith(oldPath + '/') ? newPath + p.slice(oldPath.length) : p)))
    onRenamed(oldPath, newPath)
  }
  const deleted = (path: string) => {
    setExpanded((s) => new Set([...s].filter((p) => p !== path && !p.startsWith(path + '/'))))
    onDeleted(path)
  }
  const loadRoot = useCallback(async () => {
    const s = ++request.current
    setLoading(true); setError(null)
    try {
      const browse = await api.catalogTree('')
      if (s === request.current) setRoot(browse.folders)
    } catch (e) {
      if (s === request.current) setError(errorMessage(e))
    } finally {
      if (s === request.current) setLoading(false)
    }
  }, [sourceIdentity])
  // reload the root level when the catalog changes WITHOUT remounting the tree, so expanded branches
  // keep their open state across a register/create/rename/delete (they reconcile by path key).
  useEffect(() => {
    void loadRoot()
    return () => { request.current += 1 }
  }, [loadRoot, revision])
  const create = async () => {
    const path = window.prompt('New folder path (e.g. prod/images):', '')?.trim()
    if (!path) return
    try { await api.createFolder(path); onCreated(); pushToast(`Created folder “${path}”`, 'success') }
    catch (e) { pushToast(errorMessage(e), 'error') }
  }
  return (
    <div className="flex flex-col gap-px text-[12.5px]">
      <div className="mb-0.5 flex items-center gap-1">
        <button onClick={() => onSelect('')}
          className={`flex flex-1 items-center gap-1.5 rounded-md px-2 py-1 text-left hover:bg-accent ${!selected ? 'bg-accent font-semibold text-accent-foreground' : 'text-muted-foreground'}`}>
          <Icon name="db" size={13} /> All datasets
        </button>
        {mutable && (
          <button onClick={() => void create()} data-testid="folder-new" aria-label="New folder" title="New folder"
            className="grid h-6 w-6 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground">
            <Icon name="plus" size={13} />
          </button>
        )}
      </div>
      {loading && root === null && <div className="px-2 py-1 text-[11px] text-muted-foreground">Loading…</div>}
      {error && (
        <div role="alert" className="mx-1 flex flex-col gap-1 rounded-md border border-destructive/30 px-2 py-1.5 text-[11px] text-destructive">
          <span>Couldn't load folders: {error}{root ? ' (showing stale folders)' : ''}</span>
          <button onClick={() => void loadRoot()} data-testid="folder-tree-retry" className="self-start font-semibold underline">Retry</button>
        </div>
      )}
      {root?.map((f) => <FolderBranch key={f.path} node={f} depth={0} selected={selected} onSelect={onSelect}
        onRenamed={renamed} onDeleted={deleted} mutable={mutable} revision={revision}
        sourceIdentity={sourceIdentity} expanded={expanded} onToggleExpand={toggleExpand} />)}
      {root?.length === 0 && !loading && !error && <div className="px-2 py-1 text-[11px] text-muted-foreground">No folders yet</div>}
    </div>
  )
}

function FolderBranch({ node, depth, selected, onSelect, onRenamed, onDeleted, mutable, revision, sourceIdentity, expanded, onToggleExpand }:
  { node: FolderNode; depth: number; selected: string; onSelect: (f: string) => void; mutable: boolean; revision: number
    sourceIdentity: KernelInfo | null; expanded: Set<string>; onToggleExpand: (path: string) => void }
  & Pick<FolderActions, 'onRenamed' | 'onDeleted'>) {
  const pushToast = useStore((s) => s.pushToast)
  const open = expanded.has(node.path)
  const [kids, setKids] = useState<FolderNode[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const loaded = useRef<{ path: string; revision: number; sourceIdentity: KernelInfo | null } | null>(null)
  const requestGeneration = useRef(0)
  const activeRequest = useRef<{
    path: string; revision: number; sourceIdentity: KernelInfo | null; generation: number; controller: AbortController
  } | null>(null)
  const mounted = useRef(false)
  const currentIdentity = useRef<{
    path: string; revision: number; sourceIdentity: KernelInfo | null; open: boolean
  } | null>(null)
  const isSel = selected === node.path

  const invalidateChildRequest = useCallback(() => {
    requestGeneration.current += 1
    activeRequest.current?.controller.abort()
    activeRequest.current = null
  }, [])

  // Request authority belongs to the committed branch identity. Never publish render-phase props to
  // this ref: concurrent React may abandon that render after this branch has run, and an in-flight
  // response must still be judged against the identity users actually see. Layout cleanup fences and
  // aborts the previous committed identity before a replacement identity can become authoritative.
  useLayoutEffect(() => {
    mounted.current = true
    currentIdentity.current = { path: node.path, revision, sourceIdentity, open }
    return () => {
      currentIdentity.current = null
      mounted.current = false
      invalidateChildRequest()
    }
  }, [invalidateChildRequest, node.path, open, revision, sourceIdentity])

  const loadKids = useCallback(async () => {
    invalidateChildRequest()
    const request = {
      path: node.path,
      revision,
      sourceIdentity,
      generation: requestGeneration.current,
      controller: new AbortController(),
    }
    activeRequest.current = request
    const isCurrent = () => {
      const identity = currentIdentity.current
      return mounted.current
        && activeRequest.current === request
        && request.generation === requestGeneration.current
        && !request.controller.signal.aborted
        && identity !== null
        && identity.path === request.path
        && identity.revision === request.revision
        && identity.sourceIdentity === request.sourceIdentity
        && identity.open
    }
    setLoading(true); setError(null)
    try {
      const browse = await api.catalogTree(request.path, { signal: request.controller.signal })
      if (!isCurrent()) return
      if (browse.prefix !== request.path) {
        throw new Error(`Catalog returned folder “${browse.prefix}” for “${request.path}”`)
      }
      // Commit children and their identity together. A stale response can update neither half.
      loaded.current = {
        path: request.path, revision: request.revision, sourceIdentity: request.sourceIdentity,
      }
      setKids(browse.folders)
    }
    catch (e) {
      if (isCurrent()) setError(errorMessage(e))
    }
    finally {
      if (isCurrent()) {
        activeRequest.current = null
        setLoading(false)
      }
    }
  }, [invalidateChildRequest, node.path, revision, sourceIdentity])

  const expand = () => {
    // Collapse revokes this generation in the event handler, before React schedules/commits the new
    // closed identity. The layout cleanup below remains the authoritative fence for every other change.
    if (open) invalidateChildRequest()
    onToggleExpand(node.path)
  }
  // Expansion is path-owned by FolderTree. Hydrate a rename-remounted branch whose new path stays open,
  // and refresh a branch that changed while collapsed before showing its cached children again.
  useEffect(() => {
    if (!open) {
      invalidateChildRequest()
      setLoading(false)
      setError(null)
      return
    }
    if (loaded.current?.path !== node.path
      || loaded.current.revision !== revision
      || loaded.current.sourceIdentity !== sourceIdentity) void loadKids()
    return invalidateChildRequest
  }, [invalidateChildRequest, loadKids, node.path, open, revision, sourceIdentity])

  const rename = async () => {
    const next = window.prompt(`Rename folder “${node.path}” to:`, node.path)?.trim()
    if (!next || next === node.path) return
    try {
      await api.renameFolder(node.path, next)
      invalidateChildRequest()
      onRenamed(node.path, next)
      pushToast('Folder renamed', 'success')
    }
    catch (e) { pushToast(errorMessage(e), 'error') }
  }
  const remove = async () => {
    const parent = node.path.includes('/') ? node.path.slice(0, node.path.lastIndexOf('/')) : ''
    const where = parent ? `“${parent}”` : 'the top level'
    const n = node.tableCount
    // honest: delete is non-destructive — the whole subtree (datasets AND subfolders) moves up one level
    if (!window.confirm(
      `Delete folder “${node.path}”? Its ${n} dataset${n === 1 ? '' : 's'} and any subfolders move up to ${where}. Nothing is deleted.`)) return
    try {
      await api.deleteFolder(node.path)
      invalidateChildRequest()
      onDeleted(node.path)
      pushToast('Folder deleted', 'success')
    }
    catch (e) { pushToast(errorMessage(e), 'error') }
  }
  const visibleKids = loaded.current?.path === node.path && loaded.current.sourceIdentity === sourceIdentity ? kids : null
  return (
    <div>
      <div className={`group/branch flex items-center rounded-md hover:bg-accent ${isSel ? 'bg-accent' : ''}`} style={{ paddingLeft: depth * 12 }}>
        <button onClick={expand} aria-label={`${open ? 'Collapse' : 'Expand'} folder ${node.path}`} className="grid h-6 w-5 place-items-center text-muted-foreground">
          <Icon name={open ? 'chevronDown' : 'chevronRight'} size={12} />
        </button>
        <button onClick={() => onSelect(node.path)}
          className={`flex flex-1 items-center justify-between gap-1.5 px-1 py-1 text-left ${isSel ? 'font-semibold text-accent-foreground' : 'text-muted-foreground'}`}>
          <span className="truncate">📁 {node.name}</span>
          <span className="text-[10px] tabular-nums opacity-60">{node.tableCount.toLocaleString()}</span>
        </button>
        {mutable && (<>
          <button onClick={() => void rename()} data-testid={`folder-rename-${node.path}`} aria-label={`Rename folder ${node.path}`} title="Rename"
            className="grid h-6 w-5 shrink-0 place-items-center text-muted-foreground opacity-0 hover:text-foreground group-hover/branch:opacity-100 focus:opacity-100">
            <Icon name="rename" size={11} />
          </button>
          <button onClick={() => void remove()} data-testid={`folder-delete-${node.path}`} aria-label={`Delete folder ${node.path}`} title="Delete"
            className="mr-0.5 grid h-6 w-5 shrink-0 place-items-center text-muted-foreground opacity-0 hover:text-destructive group-hover/branch:opacity-100 focus:opacity-100">
            <Icon name="trash" size={11} />
          </button>
        </>)}
      </div>
      {open && loading && visibleKids === null && <div className="py-0.5 pr-1 text-[10.5px] text-muted-foreground" style={{ paddingLeft: (depth + 1) * 12 + 8 }}>Loading…</div>}
      {open && loading && visibleKids !== null && <div role="status" className="py-0.5 pr-1 text-[10.5px] text-muted-foreground" style={{ paddingLeft: (depth + 1) * 12 + 8 }}>Refreshing…</div>}
      {open && error && (
        <div role="alert" className="flex items-center gap-1 py-0.5 pr-1 text-[10.5px] text-destructive" style={{ paddingLeft: (depth + 1) * 12 + 8 }}>
          <span className="truncate">Couldn't load: {error}{visibleKids ? ' (stale)' : ''}</span>
          <button onClick={() => void loadKids()} data-testid={`folder-branch-retry-${node.path}`} className="shrink-0 font-semibold underline">Retry</button>
        </div>
      )}
      {open && visibleKids?.map((k) => <FolderBranch key={k.path} node={k} depth={depth + 1} selected={selected} onSelect={onSelect}
        onRenamed={onRenamed} onDeleted={onDeleted} mutable={mutable} revision={revision}
        sourceIdentity={sourceIdentity} expanded={expanded} onToggleExpand={onToggleExpand} />)}
    </div>
  )
}

// ---- full-page dataset viewer: rows first, evidence and curation second -----
export function CatalogDetail({ table, onClose, onUse, onChanged, onFolder, onDeleted, onOpenTable, onColumn,
  folderActionLabel = 'Browse folder', folderActionVisible = !!table.folder,
  folderActionDisabled = false, folderActionTitle, onFolderRetry, initialRevisionId, initialRevisionDatasetId,
  backLabel = 'Back to Workspace',
}: {
  table: CatalogTable; onClose: () => void; onUse: (t: CatalogTable) => void
  onChanged: (t: CatalogTable) => void; onFolder: (f: string) => void
  onDeleted: () => void; onOpenTable: (t: CatalogTable) => void; onColumn: (name: string) => void
  folderActionLabel?: string; folderActionVisible?: boolean
  folderActionDisabled?: boolean; folderActionTitle?: string
  onFolderRetry?: () => void
  initialRevisionId?: string
  initialRevisionDatasetId?: string
  backLabel?: string
}) {
  const pushToast = useStore((s) => s.pushToast)
  const openRelationships = useStore((s) => s.openRelationships)
  const catalogSource = useStore((s) => s.kernelInfo)
  const atomicMetadataEditable = catalogSource?.capabilities?.includes('catalog.atomic_metadata_edit') ?? false
  const unregisterSupported = catalogSource?.capabilities?.includes('catalog.cas_unregister') ?? false
  const [base, setBase] = useState(table)
  const [name, setName] = useState(table.name)
  const [folder, setFolder] = useState(table.folder ?? '')
  const [tags, setTags] = useState((table.tags ?? []).join(', '))
  const [owner, setOwner] = useState(table.owner ?? '')
  const [description, setDescription] = useState(table.description ?? '')
  const [lin, setLin] = useState<LineageResult | null>(null)
  const [lineageLoading, setLineageLoading] = useState(true)
  const [lineageError, setLineageError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [preview, setPreview] = useState<SampleResult | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [requestedExactDetail, setRequestedExactDetail] = useState<DatasetRevisionDetail | null>(null)
  const [requestedExactLoading, setRequestedExactLoading] = useState(false)
  const [requestedExactError, setRequestedExactError] = useState<string | null>(null)
  const [latestHead, setLatestHead] = useState<DatasetRevisionResolution | null>(null)
  const [headChecking, setHeadChecking] = useState(true)
  const [headError, setHeadError] = useState<string | null>(null)
  const [exactFacts, setExactFacts] = useState<DatasetRevisionDetail | null>(null)
  const [factsLoading, setFactsLoading] = useState(false)
  const [factsError, setFactsError] = useState<string | null>(null)
  const initialKey = (t: CatalogTable) => t.keys?.find((k) => k.confidence === 'declared')?.columns ?? []
  const [declaredPk, setDeclaredPk] = useState(() => initialKey(table))
  const [conflict, setConflict] = useState(false)
  const [conflictBase, setConflictBase] = useState<CatalogTable | null>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const lineageRequest = useRef(0)
  const previewRequest = useRef(0)
  const requestedExactRequest = useRef(0)
  const headRequest = useRef(0)
  const factsRequest = useRef(0)
  const requestedExact = initialRevisionId && initialRevisionDatasetId
    ? { datasetId: initialRevisionDatasetId, revisionId: initialRevisionId }
    : null

  const loadRequestedExact = useCallback(async () => {
    if (!initialRevisionId || !initialRevisionDatasetId) return
    const requested = { datasetId: initialRevisionDatasetId, revisionId: initialRevisionId }
    const request = ++requestedExactRequest.current
    setRequestedExactLoading(true); setRequestedExactError(null); setRequestedExactDetail(null)
    try {
      const detail = await api.datasetRevision(requested.datasetId, requested.revisionId)
      if (request !== requestedExactRequest.current) return
      if (!sameRevision(detail, requested)) {
        throw new Error(`Exact revision response did not match ${revisionLabel(requested)}`)
      }
      setRequestedExactDetail(detail)
    } catch (error) {
      if (request !== requestedExactRequest.current) return
      const status = statusOf(error)
      const prefix = status === 403
        ? 'You do not have permission to open this exact revision.'
        : status === 404 || status === 410
          ? 'This exact revision is unavailable or no longer retained.'
          : `Couldn't load this exact revision: ${errorMessage(error)}.`
      setRequestedExactError(`${prefix} Latest was not substituted.`)
    } finally {
      if (request === requestedExactRequest.current) setRequestedExactLoading(false)
    }
  }, [initialRevisionDatasetId, initialRevisionId])

  useEffect(() => {
    requestedExactRequest.current += 1
    setRequestedExactDetail(null); setRequestedExactError(null); setRequestedExactLoading(false)
    if (initialRevisionId && initialRevisionDatasetId) void loadRequestedExact()
    return () => { requestedExactRequest.current += 1 }
  }, [initialRevisionDatasetId, initialRevisionId, loadRequestedExact, table.id])

  const resolveLatestHead = useCallback(async () => {
    const request = ++headRequest.current
    setHeadChecking(true); setHeadError(null)
    try {
      const next = await api.resolveDatasetRevision(table.id)
      if (request === headRequest.current) {
        setLatestHead(next)
        setFactsError(null)
      }
    } catch (e) {
      if (request !== headRequest.current) return
      const status = statusOf(e)
      if (status === 501 || status === 410) setLatestHead(null)
      else setHeadError(errorMessage(e))
    } finally {
      if (request === headRequest.current) setHeadChecking(false)
    }
  }, [table.id])

  useEffect(() => {
    headRequest.current += 1
    factsRequest.current += 1
    setLatestHead(null); setHeadError(null); setExactFacts(null); setFactsError(null); setFactsLoading(false)
    if (requestedExact) {
      setHeadChecking(false)
    } else {
      void resolveLatestHead()
    }
    return () => {
      headRequest.current += 1
      factsRequest.current += 1
    }
  }, [resolveLatestHead, table.registrationId, table.version, initialRevisionDatasetId, initialRevisionId])

  const loadLineage = useCallback(async () => {
    const s = ++lineageRequest.current
    setLineageLoading(true); setLineageError(null)
    try {
      const next = await api.lineage(table.uri, 4, 60)
      if (s === lineageRequest.current) setLin(next)
    } catch (e) {
      if (s === lineageRequest.current) setLineageError(errorMessage(e))
    } finally {
      if (s === lineageRequest.current) setLineageLoading(false)
    }
  }, [table.uri])
  useEffect(() => {
    void loadLineage()
    return () => { lineageRequest.current += 1 }
  }, [loadLineage])
  useEffect(() => { closeRef.current?.focus() }, [])
  const loadPreview = async () => {
    if (requestedExact) return
    const s = ++previewRequest.current
    setPreviewLoading(true); setPreviewError(null)
    try {
      const next = await api.sample(table.uri, 100)
      if (s === previewRequest.current) {
        setPreview(next)
        // Preview is a current-data read. Re-resolve the provider-native head afterwards instead
        // of comparing unrelated row counts or adapter fingerprints.
        void resolveLatestHead()
      }
    } catch (e) {
      if (s === previewRequest.current) setPreviewError(errorMessage(e))
    } finally {
      if (s === previewRequest.current) setPreviewLoading(false)
    }
  }
  // A bounded preview is the primary orientation aid in a dataset detail. Keep the request small
  // and make failures retryable, but do not make researchers discover an expandable section first.
  useEffect(() => {
    previewRequest.current += 1
    setPreview(null); setPreviewError(null); setPreviewLoading(false)
    if (!requestedExact) void loadPreview()
    return () => { previewRequest.current += 1 }
  }, [table.uri, initialRevisionDatasetId, initialRevisionId])
  const refreshHeadFacts = async () => {
    if (!latestHead) return
    const target = latestHead
    const request = ++factsRequest.current
    setFactsLoading(true); setFactsError(null)
    try {
      const next = await api.datasetRevision(target.datasetId, target.revisionId)
      if (request !== factsRequest.current) return
      if (!sameRevision(next, target)) {
        throw new Error(`Exact revision response did not match ${revisionLabel(target)}`)
      }
      setExactFacts(next)
      // Fence the bounded exact read with a second head resolution. If the provider advanced while
      // it was loading, the exact facts remain visible but are explicitly stale against the new head.
      void resolveLatestHead()
    } catch (e) {
      if (request === factsRequest.current) setFactsError(errorMessage(e))
    } finally {
      if (request === factsRequest.current) setFactsLoading(false)
    }
  }
  const unregister = async () => {
    if (!unregisterSupported || !base.registrationId || !base.metadataRevision) {
      pushToast('This catalog entry cannot be removed with a version precondition', 'error')
      return
    }
    if (!window.confirm(
      `Unregister "${table.name}"? This removes the catalog registration, not underlying data.`,
    )) return
    setDeleting(true)
    try { await api.unregisterTable(table.id, base.registrationId, base.metadataRevision); pushToast('Removed from catalog', 'success'); onDeleted() }
    catch (e) { pushToast(errorMessage(e), 'error') }
    finally { setDeleting(false) }
  }
  const openLinked = async (ref: string | undefined) => {
    if (!ref) {
      pushToast("Couldn't open linked dataset: lineage node has no catalog identity", 'error')
      return
    }
    try { onOpenTable(await api.table(ref)) }
    catch (e) { pushToast(`Couldn't open linked dataset: ${errorMessage(e)}`, 'error') }
  }

  const sameList = (left: string[], right: string[]) => left.length === right.length && left.every((item, i) => item === right[i])
  const dirty = name !== base.name
    || folder !== (base.folder ?? '')
    || tags !== (base.tags ?? []).join(', ')
    || owner !== (base.owner ?? '')
    || description !== (base.description ?? '')
    || !sameList(declaredPk, initialKey(base))

  const requestClose = useCallback(() => {
    if (!dirty || window.confirm('Discard unsaved catalog edits?')) onClose()
  }, [dirty, onClose])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') requestClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [requestClose])
  const resetTo = (next: CatalogTable) => {
    setBase(next); setName(next.name); setFolder(next.folder ?? ''); setTags((next.tags ?? []).join(', '))
    setOwner(next.owner ?? ''); setDescription(next.description ?? ''); setDeclaredPk(initialKey(next))
    setConflict(false); setConflictBase(null)
    onChanged(next)
  }
  const discard = () => {
    resetTo(base)
    pushToast('Discarded unsaved catalog edits', 'info')
  }
  const copyLocation = async () => {
    if (!navigator.clipboard?.writeText) {
      pushToast('Copy is not available in this browser', 'error')
      return
    }
    try {
      await navigator.clipboard.writeText(table.uri)
      pushToast('Dataset location copied', 'success')
    } catch (e) { pushToast(`Couldn't copy dataset location: ${errorMessage(e)}`, 'error') }
  }
  const save = async (against = base) => {
    if (!atomicMetadataEditable) return
    if (!against.metadataRevision) {
      pushToast('This catalog entry does not provide a revision for atomic editing', 'error')
      return
    }
    setBusy(true)
    try {
      const next = await api.saveTableEdit(table.id, {
        expectedRevision: against.metadataRevision,
        name: name.trim() || undefined, folder: folder.trim(), owner: owner.trim() || null, description: description.trim() || null,
        tags: tags.split(',').map((t) => t.trim()).filter(Boolean), declaredKey: declaredPk,
      })
      resetTo(next); pushToast('Saved', 'success')
    } catch (e) {
      const status = e instanceof KernelError ? e.status
        : (typeof e === 'object' && e !== null ? (e as { status?: number }).status : undefined)
      if (status === 409) {
        setConflict(true); setConflictBase(null)
        try { setConflictBase(await api.table(table.id)) } catch { /* retain the draft; Reload can retry */ }
      }
      pushToast(errorMessage(e), 'error')
    }
    finally { setBusy(false) }
  }
  const lineageRoot = lin?.rootUri ?? table.uri
  const parents = (lin?.edges ?? []).filter((e) => e.child === lineageRoot)
  const children = (lin?.edges ?? []).filter((e) => e.parent === lineageRoot)
  const lineageNode = (u: string) => lin?.nodes.find((n) => n.uri === u)
  const nameOf = (u: string) => lineageNode(u)?.name ?? u.split('/').slice(-1)[0]
  const displayRowCount = requestedExact
    ? requestedExactDetail?.summary.rowCount ?? null
    : exactFacts ? exactFacts.summary.rowCount : table.rowCount
  const displayColumns = requestedExact
    ? requestedExactDetail?.preview.columns ?? []
    : exactFacts ? exactFacts.preview.columns : table.columns
  const factsMatchKnownHead = sameRevision(exactFacts, latestHead)
  const factsVerifiedLatest = factsMatchKnownHead && !headChecking && !headError
  const displayedVersion = requestedExact ?? exactFacts ?? latestHead

  const togglePk = (col: string) => {
    const next = declaredPk.includes(col) ? declaredPk.filter((c) => c !== col) : [...declaredPk, col]
    setDeclaredPk(next)
  }
  const persistedDeclaredKey = initialKey(base)

  return (
    <div className="absolute inset-0 z-30 flex overflow-hidden bg-background" data-testid="dataset-viewer">
      <div role="region" aria-label={table.name}
        className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-background">
        <div className="flex shrink-0 items-center gap-3 border-b border-border bg-card px-5 py-3">
          <button ref={closeRef} onClick={requestClose} aria-label={backLabel}
            className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11.5px] font-semibold text-muted-foreground hover:bg-accent hover:text-foreground">
            <Icon name="chevronLeft" size={14} /> Back
          </button>
          <Icon name="db" size={16} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-[15px] font-bold text-foreground">{table.name}</div>
            <div data-testid="dataset-version-context" className="truncate text-[10.5px] text-muted-foreground">
              {requestedExact ? 'Published version' : 'Latest dataset'}
            </div>
          </div>
          {requestedExact
            ? <span data-testid="detail-use-unavailable"
              className="shrink-0 rounded-md bg-muted px-2.5 py-1 text-[11.5px] font-semibold text-muted-foreground">
              Exact revision is view-only
            </span>
            : <button onClick={() => onUse(table)} data-testid="detail-use"
              className="inline-flex shrink-0 items-center gap-1 rounded-md bg-primary/10 px-2.5 py-1 text-[11.5px] font-semibold text-primary">
              <Icon name="plus" size={12} /> Use in Canvas
            </button>}
        </div>

        <div tabIndex={0} aria-label="Dataset detail content" data-testid="dataset-detail-content"
          className="min-h-0 flex-1 overflow-y-auto p-5 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-ring">
          <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-5 text-[12.5px]">
            <div className="flex flex-wrap gap-3 text-[11.5px] text-muted-foreground">
              <span>{displayRowCount == null ? '—' : displayRowCount.toLocaleString()} rows</span>
              <span>· {requestedExact && !requestedExactDetail ? '—' : displayColumns.length} cols</span>
              <span>· {table.folder ? `Folder ${table.folder}` : 'Unfiled'}</span>
              {requestedExactDetail ? <span data-testid="dataset-facts-source">· Published version</span> : null}
              {!requestedExact && exactFacts ? <span data-testid="dataset-facts-source">· Versioned facts</span> : null}
              {!requestedExact && !exactFacts && latestHead ? <span data-testid="dataset-facts-source">· Latest version</span> : null}
              {factsVerifiedLatest ? <span>· verified latest head</span> : null}
              {table.usage ? <span>· used {table.usage}×</span> : null}
            </div>

            <section aria-labelledby="dataset-preview-title" className="rounded-xl border border-border bg-card p-3 shadow-sm">
              <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <h2 id="dataset-preview-title" className="text-[13px] font-bold text-foreground">Data preview</h2>
                  <p className="text-[10.5px] text-muted-foreground">
                    Bounded first page · up to 100 rows. This viewer does not scan or render the entire dataset.
                  </p>
                </div>
                {requestedExact ? <span className="rounded bg-primary/10 px-2 py-1 text-[10px] font-semibold text-primary">
                  Exact revision
                </span> : <span className="rounded bg-muted px-2 py-1 text-[10px] font-semibold text-muted-foreground">
                  Latest dataset
                </span>}
              </div>

              {requestedExactLoading ? <div role="status" className="grid h-[240px] place-items-center text-[11px] text-muted-foreground">
                Loading exact revision preview…
              </div> : null}
              {requestedExactError ? (
                <div role="alert" className="flex h-[240px] items-center justify-center">
                  <div className="max-w-xl rounded-lg border border-destructive/30 px-4 py-3 text-[11px] text-destructive">
                    <div>{requestedExactError}</div>
                    <button type="button" onClick={() => void loadRequestedExact()}
                      data-testid="exact-preview-retry" className="mt-2 font-semibold underline">Retry exact revision</button>
                  </div>
                </div>
              ) : null}
              {requestedExactDetail ? <>
                <div role="status" aria-label="Dataset preview scope"
                  className="mb-2 rounded-md bg-muted/50 px-2 py-1 text-[10.5px] text-muted-foreground">
                  Showing {requestedExactDetail.preview.rows.length.toLocaleString()} preview
                  {requestedExactDetail.preview.rows.length === 1 ? ' row' : ' rows'} from this exact revision.
                  {requestedExactDetail.preview.hasMore
                    ? ` More rows exist; preview capped at ${requestedExactDetail.preview.rowLimit.toLocaleString()} rows.`
                    : ''}
                </div>
                <DatasetPreviewTable columns={requestedExactDetail.preview.columns}
                  rows={requestedExactDetail.preview.rows} exact />
              </> : null}

              {!requestedExact && <>
                {previewLoading && !preview ? <div role="status" className="grid h-[240px] place-items-center text-[11px] text-muted-foreground">Loading latest preview…</div> : null}
                {previewError ? (
                  <div role="alert" className="mb-2 flex items-center justify-between gap-2 rounded-lg border border-destructive/30 px-3 py-2 text-[11px] text-destructive">
                    <span>Couldn't load latest preview: {previewError}{preview ? ' (showing stale preview)' : ''}</span>
                    <button onClick={() => void loadPreview()} data-testid="detail-preview-retry" className="shrink-0 font-semibold underline">Retry</button>
                  </div>
                ) : null}
                {preview ? <div className="flex flex-col gap-2">
                  {!preview.error && !preview.notPreviewable && <CatalogPreviewScope
                    preview={preview} stale={Boolean(previewError)} visibleRows={preview.rows.length} />}
                  {preview.error || preview.notPreviewable || !preview.columns.length || !preview.rows.length
                    ? <div className="grid h-[240px] place-items-center rounded-lg border border-border px-3 py-2 text-[11px] text-muted-foreground">{preview.reason || emptyCatalogPreviewMessage(preview)}</div>
                    : <DatasetPreviewTable columns={preview.columns} rows={preview.rows} />}
                </div> : null}
              </>}
            </section>

            <details data-testid="detail-dataset-details" className="rounded-lg border border-border px-3 py-2 text-[11px]">
              <summary className="cursor-pointer font-semibold text-foreground">
                Dataset details
              </summary>
              <div className="mt-2 grid gap-2">
                <div className="flex items-start gap-2">
                  <code data-testid="dataset-location" className="min-w-0 flex-1 break-all text-[10.5px] text-muted-foreground">{table.uri}</code>
                  <button type="button" onClick={() => void copyLocation()} aria-label="Copy dataset location" className="shrink-0 rounded border border-border px-2 py-1 font-semibold text-foreground hover:bg-accent">Copy</button>
                </div>
                {table.registrationId ? <div>
                  <div className="text-[10px] text-muted-foreground">Catalog registration identity</div>
                  <code className="break-all text-[10.5px] text-foreground">{table.registrationId}</code>
                </div> : null}
                {displayedVersion ? <div data-testid="dataset-version-identity">
                  <div className="text-[10px] text-muted-foreground">
                    {requestedExact ? 'Exact version identity' : 'Version identity'}
                  </div>
                  <code className="break-all text-[10.5px] text-foreground">
                    {revisionLabel(displayedVersion)}
                  </code>
                </div> : null}
              </div>
            </details>

          {headChecking && !latestHead ? (
            <div role="status" className="text-[11px] text-muted-foreground">Checking latest dataset head…</div>
          ) : null}
          {headError ? (
            <div role="alert" className="flex items-center justify-between gap-2 rounded-lg border border-destructive/30 px-3 py-2 text-[11px] text-destructive">
              <span>Couldn't verify the latest dataset head: {headError}</span>
              <button type="button" onClick={() => void resolveLatestHead()} className="shrink-0 font-semibold underline">Retry</button>
            </div>
          ) : null}
          {latestHead && !factsMatchKnownHead ? (
            <div role="status" data-testid="dataset-facts-stale"
              className="flex flex-col gap-2 rounded-lg border border-amber-300/60 bg-amber-50/70 px-3 py-2 text-[11px] text-amber-950 dark:border-amber-700/60 dark:bg-amber-950/30 dark:text-amber-100">
              <div>
                <div className="font-semibold">Dataset facts may be out of date</div>
                <div className="break-words">{exactFacts
                  ? 'Header and columns describe an earlier version. Refresh to show facts for the latest version.'
                  : 'Refresh to show header and column facts for the latest version.'}</div>
              </div>
              {factsError ? <div role="alert">Couldn't refresh the latest dataset facts: {factsError}</div> : null}
              <button type="button" onClick={() => void refreshHeadFacts()} disabled={factsLoading}
                data-testid="refresh-dataset-facts"
                className="self-start font-semibold underline disabled:opacity-50">
                {factsLoading ? 'Refreshing dataset facts…' : factsError ? 'Retry dataset facts' : 'Refresh dataset facts'}
              </button>
            </div>
          ) : null}

          <section>
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Schema</div>
              <span className="text-[10.5px] text-muted-foreground">
                {requestedExact && !requestedExactDetail ? 'Exact revision' : `${displayColumns.length} columns`}
              </span>
            </div>
            {requestedExactLoading ? <div role="status" className="rounded-lg border border-border px-3 py-2 text-[11px] text-muted-foreground">
              Loading exact revision schema…
            </div> : requestedExactError ? <div className="rounded-lg border border-border px-3 py-2 text-[11px] text-muted-foreground">
              Exact revision schema is unavailable. Latest schema was not substituted.
            </div> : displayColumns.length ? <div tabIndex={0} aria-label="Dataset schema columns" data-testid="detail-schema-scroll"
              className="grid max-h-[132px] grid-cols-2 gap-x-3 gap-y-1 overflow-y-auto overscroll-contain rounded-lg border border-border px-3 py-2 text-[11px] focus:outline-none focus:ring-2 focus:ring-inset focus:ring-ring">
              {displayColumns.map((column) => <div key={column.name} className="flex min-w-0 items-center gap-1"><span className="min-w-0 flex-1 truncate font-mono text-foreground">{column.name}</span><span className="shrink-0 text-muted-foreground">· {column.type}</span><FieldEvidenceButton column={column} label="Details" className="shrink-0 rounded px-1 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground" /></div>)}
            </div> : <div className="rounded-lg border border-border px-3 py-2 text-[11px] text-muted-foreground">No columns were reported for this dataset.</div>}
          </section>

          <DatasetRevisionHistory key={`${table.id}:${table.registrationId ?? ''}`} table={table}
            initialRevisionId={initialRevisionId} initialRevisionDatasetId={initialRevisionDatasetId}
            detailsInViewer viewerDetail={requestedExactDetail}
            viewerLoading={requestedExactLoading} viewerError={requestedExactError}
            onViewerRetry={() => { void loadRequestedExact() }} />

          <details className="rounded-lg border border-border p-3">
            <summary className="cursor-pointer text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Edit catalog details</summary>
            <div className="mt-3 flex flex-col gap-2">
            <div className="text-[11px] text-muted-foreground">Organize this catalog entry and save its metadata separately from inspecting or using the dataset.</div>
            <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} disabled={!atomicMetadataEditable} placeholder="friendly name" className="dp-input" data-testid="detail-name" /></Field>
            <Field label="Folder"><input value={folder} onChange={(e) => setFolder(e.target.value)} disabled={!atomicMetadataEditable} list="dp-folder-options" placeholder="prod/images" className="dp-input" data-testid="detail-folder" /></Field>
            <Field label="Tags"><input value={tags} onChange={(e) => setTags(e.target.value)} disabled={!atomicMetadataEditable} placeholder="gold, pii (comma-separated)" className="dp-input" /></Field>
            <Field label="Owner"><input value={owner} onChange={(e) => setOwner(e.target.value)} disabled={!atomicMetadataEditable} placeholder="team or person" className="dp-input" /></Field>
            <Field label="Description"><textarea value={description} onChange={(e) => setDescription(e.target.value)} disabled={!atomicMetadataEditable} rows={2} className="dp-input resize-y" /></Field>
            {!atomicMetadataEditable && <div className="text-[11px] text-muted-foreground">This catalog provider does not support atomic metadata and declared-key edits.</div>}
            {atomicMetadataEditable && dirty && <div className="text-[11px] text-muted-foreground">Unsaved changes</div>}
            {conflict && <div role="alert" className="flex items-center justify-between gap-2 rounded border border-destructive/30 px-2 py-1.5 text-[11px] text-destructive">
              <span>Another editor saved changes first.</span>
              <span className="flex gap-2"><button onClick={() => void (async () => { try { resetTo(await api.table(table.id)) } catch (e) { pushToast(errorMessage(e), 'error') } })()} className="font-semibold underline">Reload</button>{conflictBase && <button onClick={() => void save(conflictBase)} className="font-semibold underline">Reapply</button>}</span>
            </div>}
            <section>
              <div className="mb-1 flex items-baseline justify-between gap-2">
                <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Key roles</div>
                <span className="text-[10.5px] text-muted-foreground">{persistedDeclaredKey.length > 1 ? 'Saved composite key' : persistedDeclaredKey.length === 1 ? 'Saved key' : 'No saved key'}</span>
              </div>
              <p className="mb-2 text-[11px] leading-snug text-muted-foreground">Select one column for a key, or several columns for one composite key. Changes apply only when you save.</p>
              <div className="max-h-[220px] overflow-y-auto rounded-lg border border-border">
                {displayColumns.map((c) => {
                  const selected = declaredPk.includes(c.name)
                  const persisted = persistedDeclaredKey.includes(c.name)
                  const pendingAdd = selected && !persisted
                  const pendingRemoval = !selected && persisted
                  const role = pendingAdd ? 'Will be a key on Save'
                    : pendingRemoval ? 'Will be removed on Save'
                      : persisted ? persistedDeclaredKey.length > 1 ? 'Composite key' : 'Key' : null
                  const action = selected ? `Remove ${c.name} from the declared key` : `Mark ${c.name} as a key`
                  return <div key={c.name} className="flex w-full items-center gap-2 border-b border-border/60 px-2 py-1.5 last:border-0 hover:bg-accent">
                    <button type="button" onClick={() => togglePk(c.name)} disabled={!atomicMetadataEditable} data-testid={`detail-pk-${c.name}`} aria-label={action} title={`${action}. This is saved only when you select Save.`} className="shrink-0 rounded border border-border bg-background px-1.5 py-0.5 text-[10px] font-semibold text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50">{selected ? 'Remove key' : 'Mark as key'}</button>
                    <button onClick={() => onColumn(c.name)} title={`Filter the list to tables with column "${c.name}"`} className="flex min-w-0 flex-1 items-center gap-2 text-left"><span className="dp-mono flex-1 truncate text-[11.5px]">{c.name}</span><span className="text-[10px] text-muted-foreground">{c.type}</span></button>
                    {role ? <span data-testid={`detail-key-state-${c.name}`} className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold ${persisted && !pendingRemoval ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'}`}>{role}</span> : null}
                  </div>
                })}
              </div>
            </section>
            <div className="flex justify-end gap-2">
              <button onClick={discard} disabled={!dirty || busy} className="rounded-md border border-border px-3 py-1.5 text-[12px] font-semibold text-foreground disabled:opacity-50" data-testid="detail-discard">Discard</button>
              <button onClick={() => void save()} disabled={!atomicMetadataEditable || busy || !dirty} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50" data-testid="detail-save">{busy ? 'Saving…' : 'Save'}</button>
            </div>
            </div>
          </details>

          {/* lineage — click a row to open that dataset */}
          <section>
            <div className="mb-1 flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-muted-foreground"><Icon name="lineage" size={12} /> {requestedExact ? 'Current catalog lineage' : 'Lineage'}{lin?.truncated ? ' (truncated)' : ''}</div>
            {lineageLoading && !lin ? <div className="py-0.5 text-[11px] text-muted-foreground">Loading…</div> : null}
            {lineageError ? (
              <div role="alert" className="flex items-center justify-between gap-2 rounded-lg border border-destructive/30 px-2 py-1.5 text-[11px] text-destructive">
                <span>Couldn't load lineage: {lineageError}{lin ? ' (showing stale lineage)' : ''}</span>
                <button onClick={() => void loadLineage()} data-testid="detail-lineage-retry" className="shrink-0 font-semibold underline">Retry</button>
              </div>
            ) : null}
            {lin && parents.length === 0 && children.length === 0 ? <div className="py-0.5 text-[11px] text-muted-foreground">No related datasets yet.</div> : null}
            {lin && (parents.length > 0 || children.length > 0) ? <>
              {parents.length > 0 ? <LineageMini label="Parents" onOpen={openLinked}
                rows={parents.map((e) => ({
                  name: nameOf(e.parent), factCount: e.factCount,
                  uri: e.parent, catalogId: lineageNode(e.parent)?.id,
                }))} /> : null}
              {children.length > 0 ? <LineageMini label="Children" onOpen={openLinked}
                rows={children.map((e) => ({
                  name: nameOf(e.child), factCount: e.factCount,
                  uri: e.child, catalogId: lineageNode(e.child)?.id,
                }))} /> : null}
            </> : null}
          </section>

          <button onClick={() => openRelationships(table.uri)} data-testid="detail-relationships"
            className="inline-flex items-center gap-1.5 self-start text-[11.5px] text-primary hover:underline">
            <Icon name="lineage" size={12} /> View relationship graph →
          </button>
          {folderActionVisible && (
            <div className="flex items-center gap-2">
              <button onClick={() => onFolder(table.folder ?? '')} disabled={folderActionDisabled} title={folderActionTitle}
                className="self-start text-[11.5px] text-primary hover:underline disabled:cursor-not-allowed disabled:opacity-45">
                {folderActionLabel}{table.folder ? ` “${table.folder}”` : ''} →
              </button>
              {onFolderRetry && <button type="button" onClick={onFolderRetry}
                className="text-[11.5px] font-semibold text-primary hover:underline">Retry</button>}
            </div>
          )}
          <button onClick={() => void unregister()} disabled={deleting || !unregisterSupported || !base.registrationId || !base.metadataRevision} data-testid="detail-unregister"
            title={!unregisterSupported ? 'This catalog provider does not support versioned unregister'
              : !base.registrationId || !base.metadataRevision ? 'Reload this dataset before removing it' : undefined}
            className="self-start text-[11.5px] text-destructive opacity-70 hover:underline hover:opacity-100 disabled:opacity-40">
            {deleting ? 'Removing…' : 'Remove from catalog…'}
          </button>
        </div>
        </div>
      </div>
    </div>
  )
}

function DatasetPreviewTable({ columns, rows, exact = false }: {
  columns: SampleResult['columns']
  rows: Record<string, unknown>[]
  exact?: boolean
}) {
  if (!columns.length) {
    return <div className="grid h-[240px] place-items-center rounded-lg border border-border px-3 py-2 text-[11px] text-muted-foreground">
      {exact ? 'This exact revision supplied no columns.' : 'This dataset supplied no columns.'}
    </div>
  }
  return <div tabIndex={0} aria-label="Dataset rows" data-testid="detail-preview-scroll"
    className="h-[52vh] min-h-[240px] max-h-[560px] overflow-auto rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-inset focus:ring-ring">
    <table className="dp-mono w-max min-w-full text-[10.5px]">
      <thead><tr>{columns.map((column) => (
        <th key={column.name}
          className="sticky top-0 z-10 border-b border-border bg-muted px-2.5 py-2 text-left font-semibold shadow-[0_1px_0_hsl(var(--border))]">
          <FieldEvidenceButton column={column} marker
            className="dp-mono rounded px-0.5 hover:bg-accent" />
        </th>
      ))}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index} className="hover:bg-muted/30">
        {columns.map((column) => <td key={column.name}
          className="max-w-[320px] truncate whitespace-nowrap border-b border-border/40 px-2.5 py-1.5">
          {(column.capabilities ?? []).includes('media')
            ? <MediaCellRenderer column={column.name} value={row[column.name]}
                mediaKind={column.mediaKind} viewport="table" />
            : cell(row[column.name])}
        </td>)}
      </tr>)}</tbody>
    </table>
    {!rows.length ? <div className="border-t border-border px-3 py-3 text-[11px] text-muted-foreground">
      {exact
        ? 'This exact revision returned no preview rows; its retained schema remains available below.'
        : 'The bounded preview returned no rows.'}
    </div> : null}
  </div>
}

function CatalogPreviewScope({ preview, stale, visibleRows }: {
  preview: SampleResult
  stale: boolean
  visibleRows: number
}) {
  const fetchedRows = preview.rows.length
  const visibleLabel = fetchedRows === 0
    ? null
    : visibleRows < fetchedRows
      ? `Showing ${visibleRows.toLocaleString()} of ${fetchedRows.toLocaleString()} preview rows.`
      : `Showing ${visibleRows.toLocaleString()} preview ${visibleRows === 1 ? 'row' : 'rows'}.`
  return (
    <div className="rounded-md bg-muted/50 px-2 py-1">
      {visibleLabel && <div role="status" className="text-[10.5px] text-muted-foreground">{visibleLabel}</div>}
      <PreviewSummary data={preview} surface="catalog" showRange={false} />
      <PreviewDetails provenance={preview.sampleProvenance} stale={stale} />
    </div>
  )
}

function emptyCatalogPreviewMessage(preview: SampleResult) {
  if (preview.completeness === 'complete' && preview.rowCount === 0) return 'No rows in this dataset'
  if (preview.rowCount != null) {
    return `No rows returned by this preview; the dataset contains ${preview.rowCount.toLocaleString()} rows.`
  }
  return 'No rows returned by this preview; dataset size is unknown.'
}

const cell = (v: unknown) => v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v)

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10.5px] text-muted-foreground">{label}</span>
      {children}
    </label>
  )
}

export function AddDataModal({
  onClose, onUploadDataset, onCompleted,
}: {
  onClose: () => void
  onUploadDataset: (file: File) => Promise<CatalogTable | null>
  onCompleted: () => void
}) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [registerOpen, setRegisterOpen] = useState(false)
  const [uploading, setUploading] = useState(false)

  const upload = async (file?: File) => {
    if (!file || uploading) return
    setUploading(true)
    try {
      if (await onUploadDataset(file)) {
        onCompleted()
        onClose()
      }
    } finally { setUploading(false) }
  }

  if (registerOpen) {
    return <RegisterModal onClose={() => setRegisterOpen(false)} onRegistered={() => {
      onCompleted()
      onClose()
    }} />
  }

  return <div className="fixed inset-0 z-50 grid place-items-center bg-black/30 p-4" onClick={onClose}>
    <div role="dialog" aria-modal="true" aria-label="Add data" data-testid="add-data-modal"
      className="flex w-[660px] max-w-full flex-col gap-4 rounded-xl border border-border bg-card p-5 shadow-xl" onClick={(event) => event.stopPropagation()}>
      <div className="flex items-center gap-2">
        <div className="flex-1">
          <h2 className="text-[15px] font-bold text-foreground">Add data</h2>
          <p className="mt-0.5 text-[12px] text-muted-foreground">Choose the option that matches where the data is available.</p>
        </div>
        <button onClick={onClose} aria-label="Close" className="text-muted-foreground hover:text-foreground"><Icon name="close" size={15} /></button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <section className="rounded-lg border border-border bg-background p-4" aria-labelledby="upload-local-file-title">
          <h3 id="upload-local-file-title" className="text-[13px] font-semibold text-foreground">Upload a local file</h3>
          <p className="mt-1 text-[11.5px] leading-relaxed text-muted-foreground">
            Choose a file from this browser. Its bytes are uploaded to Data Playground; the kernel does not need access to your computer first.
          </p>
          <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
            Supports Parquet, CSV, TSV, JSON/NDJSON, Arrow, Feather, and IPC files. Uploads are single files; Lance datasets are directories and are not supported here.
          </p>
          <button type="button" onClick={() => fileRef.current?.click()} disabled={uploading}
            className="mt-3 rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">
            {uploading ? 'Uploading…' : 'Choose local file'}
          </button>
          <input ref={fileRef} type="file" accept={UPLOAD_FILE_ACCEPT} className="hidden"
            onChange={(event) => { void upload(event.target.files?.[0]); event.target.value = '' }} />
        </section>
        <section className="rounded-lg border border-border bg-background p-4" aria-labelledby="register-accessible-title">
          <h3 id="register-accessible-title" className="text-[13px] font-semibold text-foreground">Register an accessible path or URI</h3>
          <p className="mt-1 text-[11.5px] leading-relaxed text-muted-foreground">
            Use a mounted path or object-store URI the kernel/server can already read. This does not browse files on your computer.
          </p>
          <button type="button" onClick={() => setRegisterOpen(true)}
            className="mt-3 rounded-md border border-border bg-card px-3 py-1.5 text-[12px] font-semibold text-foreground hover:bg-accent">
            Register path or URI
          </button>
        </section>
      </div>
    </div>
  </div>
}

// Register modal — the URI is required; name/folder/tags/owner/description are all optional curation
// the backend register already accepts. Folder autocompletes from the shared #dp-folder-options list.
function RegisterModal({ onClose, onRegistered }: { onClose: () => void; onRegistered: (t: CatalogTable) => void }) {
  const pushToast = useStore((s) => s.pushToast)
  const [uri, setUri] = useState('')
  const [name, setName] = useState('')
  const [folder, setFolder] = useState('')
  const [tags, setTags] = useState('')
  const [owner, setOwner] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [browseOpen, setBrowseOpen] = useState(false)
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { closeRef.current?.focus() }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  const stem = uri.trim().replace(/\/+$/, '').split(/[\\/]/).pop()?.replace(/\.[^.]+$/, '') ?? ''
  const submit = async () => {
    const u = uri.trim()
    if (busy) return
    if (!u) { setFormError('Enter a path or URI the kernel can access.'); return }
    if (u.includes('\u0000')) { setFormError('The path or URI cannot contain a null character.'); return }
    setBusy(true)
    setFormError(null)
    try {
      const t = await api.registerDataset({
        uri: u,
        name: name.trim() || undefined,
        folder: folder.trim() || undefined,
        tags: tags.split(',').map((x) => x.trim()).filter(Boolean),
        owner: owner.trim() || undefined,
        description: description.trim() || undefined,
      })
      onRegistered(t)
    } catch (e) {
      const detail = errorMessage(e)
      setFormError(`The kernel could not register “${u}”. Confirm it exists and is readable from the kernel host, then try again. ${detail}`)
      pushToast(`Registration failed: ${detail}`, 'error')
    }
    finally { setBusy(false) }
  }
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/30 p-4" onClick={(event) => {
      if (event.target === event.currentTarget) onClose()
    }}>
      <div role="dialog" aria-modal="true" aria-label="Register a dataset" data-testid="register-modal"
        className="flex w-[460px] max-w-full flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2">
          <h2 className="flex-1 text-[15px] font-bold text-foreground">Register an accessible path or URI</h2>
          <button ref={closeRef} onClick={onClose} aria-label="Close" className="text-muted-foreground hover:text-foreground"><Icon name="close" size={15} /></button>
        </div>
        <p className="text-[11.5px] leading-relaxed text-muted-foreground">
          The kernel reads this location, not your browser. Absolute paths start on the kernel host; relative paths resolve from the kernel working directory. URI schemes such as <code>s3://</code> use the kernel’s configured storage access.
        </p>
        {formError && <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-[11.5px] text-destructive">{formError}</div>}
        <Field label="Path / URI">
          <div className="flex gap-2">
            <input autoFocus value={uri} onChange={(e) => { setUri(e.target.value); setFormError(null) }}
              onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void submit() }}
              placeholder="/data/events.parquet or s3://bucket/key" className="dp-input min-w-0 flex-1" data-testid="register-uri" />
            <button type="button" onClick={() => setBrowseOpen(true)}
              className="shrink-0 rounded-md border border-border bg-card px-2.5 text-[11.5px] font-semibold text-foreground hover:bg-accent">Browse kernel storage</button>
          </div>
        </Field>
        <Field label="Name (optional)"><input value={name} onChange={(e) => setName(e.target.value)} placeholder={stem || 'defaults to the file name'} className="dp-input" /></Field>
        <Field label="Folder (optional)"><input value={folder} onChange={(e) => setFolder(e.target.value)} list="dp-folder-options" placeholder="prod/images" className="dp-input" /></Field>
        <Field label="Tags (optional)"><input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="gold, pii (comma-separated)" className="dp-input" /></Field>
        <Field label="Owner (optional)"><input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="team or person" className="dp-input" /></Field>
        <Field label="Description (optional)"><textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} className="dp-input resize-y" /></Field>
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-md border border-border bg-card px-3 py-1.5 text-[12.5px] font-semibold text-foreground hover:bg-accent">Cancel</button>
          <button onClick={() => void submit()} disabled={busy || !uri.trim()} data-testid="register-submit"
            className="rounded-md bg-foreground px-3.5 py-1.5 text-[12.5px] font-semibold text-background disabled:opacity-50">{busy ? 'Registering…' : 'Register'}</button>
        </div>
      </div>
      {browseOpen && <FileDialog mode="open" title="Browse kernel-visible storage" onClose={() => setBrowseOpen(false)}
        onPick={({ uri: pickedUri }) => { setUri(pickedUri); setFormError(null); setBrowseOpen(false) }} />}
    </div>
  )
}

function LineageMini({ label, rows, onOpen }: {
  label: string
  rows: { name: string; factCount: number; uri: string; catalogId?: string }[]
  onOpen: (catalogId: string | undefined) => void
}) {
  return (
    <div className="mb-1.5">
      <div className="text-[9.5px] font-bold uppercase tracking-wide text-muted-foreground">{label}</div>
      {rows.length
        ? rows.map((r, i) => (
            <button key={i} onClick={() => onOpen(r.catalogId)} title={r.uri}
              className="flex w-full items-center gap-1.5 rounded-md px-1 py-0.5 text-left text-[12px] text-foreground hover:bg-accent hover:underline">
              <Icon name="arrow" size={11} /> {r.name}<span className="text-[10px] text-muted-foreground">· {r.factCount} {r.factCount === 1 ? 'fact' : 'facts'}</span>
            </button>
          ))
        : null}
    </div>
  )
}
