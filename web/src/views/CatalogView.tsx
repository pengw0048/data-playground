import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useStore } from '../store/graph'
import { api, KernelError } from '../api/client'
import { color, radius } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { VirtualList } from '../ui/VirtualList'
import { FileDialog } from '../ui/FileDialog'
import { DatasetRevisionHistory } from './DatasetRevisionHistory'
import type { CatalogQueryParams, CatalogTable, Facets, FolderNode, KernelInfo, LineageResult, SampleResult } from '../types/api'

// The Tables catalog — built to browse thousands of datasets. Nothing is loaded up front: a left
// FOLDER TREE (lazy), a center VIRTUALIZED list fed by a server-side filtered/sorted/paginated query
// (infinite scroll), and a right FACET RAIL (tags/owners with counts). A search box (debounced) and a
// sort control drive the same query; clicking a row opens a detail drawer to inspect columns + lineage
// and curate the dataset's folder/tags/owner/description.

const PAGE = 50
const ROW_H = 58
type Sort = NonNullable<CatalogQueryParams['sort']>
const errorMessage = (e: unknown) => e instanceof Error ? e.message : String(e)

/**
 * The bounded catalog browser is deliberately independent from the destination of a `Use` action.
 * CatalogView keeps the legacy current-canvas adapter below, while Workspace can supply an explicit
 * target in #497 without copying its query, paging, selection, or curation behavior.
 */
export interface CatalogDiscoveryProps {
  sourceIdentity: KernelInfo | null
  foldersMutable: boolean
  onUseTables: (tables: CatalogTable[]) => void
  onUploadDataset: (file: File) => Promise<CatalogTable | null>
}

export function CatalogView() {
  const addToCanvas = useStore((s) => s.addToCanvas)
  const rememberTables = useStore((s) => s.rememberTables)
  const uploadDataset = useStore((s) => s.uploadDataset)
  // folder create/rename/delete only mean something when the active catalog provider owns the local
  // folder store; a read-only/external provider omits this capability and we hide the affordances.
  const catalogSource = useStore((s) => s.kernelInfo)
  const foldersMutable = catalogSource?.capabilities?.includes('catalog.folder_mutation') ?? false

  const useTables = useCallback((tables: CatalogTable[]) => {
    rememberTables(tables)
    tables.forEach((t) => addToCanvas('source', { uri: t.uri, tableId: t.id }, t.name))
  }, [addToCanvas, rememberTables])

  return <CatalogDiscovery sourceIdentity={catalogSource} foldersMutable={foldersMutable}
    onUseTables={useTables} onUploadDataset={uploadDataset} />
}

export function CatalogDiscovery({ sourceIdentity: catalogSource, foldersMutable, onUseTables, onUploadDataset }: CatalogDiscoveryProps) {
  const pushToast = useStore((s) => s.pushToast)
  const fileRef = useRef<HTMLInputElement>(null)

  // query state
  const [rawQ, setRawQ] = useState('')
  const [q, setQ] = useState('')
  const [folder, setFolder] = useState('')
  const [tags, setTags] = useState<string[]>([])
  const [owner, setOwner] = useState('')
  const [hasColumns, setHasColumns] = useState<string[]>([])
  const [sort, setSort] = useState<Sort>('name')
  const [order, setOrder] = useState<'asc' | 'desc'>('asc')
  const [match, setMatch] = useState<'text' | 'meaning'>('text')

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
  const [dialog, setDialog] = useState(false)
  const [registerOpen, setRegisterOpen] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [catalogRevision, setCatalogRevision] = useState(0)
  const [folderPaths, setFolderPaths] = useState<string[]>([])  // folder ENTITIES (incl. empty) for autocomplete
  const seq = useRef(0)
  const loadingMore = useRef(false)

  // debounce the search box into the query
  useEffect(() => { const t = setTimeout(() => setQ(rawQ.trim()), 250); return () => clearTimeout(t) }, [rawQ])

  // folder entities (empty folders live only here) reload with every catalog change; best-effort
  const reloadFolderList = useCallback(async () => {
    try { setFolderPaths((await api.catalogFolders()).map((f) => f.path)) } catch { /* autocomplete only */ }
  }, [])
  useEffect(() => { void reloadFolderList() }, [reloadFolderList, catalogRevision])

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
  const clearFilters = () => { setFolder(''); setTags([]); setOwner(''); setHasColumns([]); setRawQ(''); setQ('') }
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
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })
  const clearSelection = () => setSelectedIds(new Set())
  const selectAllLoaded = () => setSelectedIds(new Set(items.map((t) => t.id)))
  const useSelected = () => {
    const ts = items.filter((t) => selectedIds.has(t.id)); if (!ts.length) return
    onUseTables(ts)
    clearSelection()
  }
  const deleteSelected = async () => {
    const ids = [...selectedIds]; if (!ids.length) return
    if (!window.confirm(`Remove ${ids.length} dataset${ids.length === 1 ? '' : 's'} from the catalog?`)) return
    try {
      const res = await api.unregisterTables(ids)
      pushToast(res.missing.length
        ? `Removed ${res.deleted.length}, ${res.missing.length} already gone`
        : `Removed ${res.deleted.length} dataset${res.deleted.length === 1 ? '' : 's'}`, 'success')
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

  return (
    <div className="flex h-full flex-col">
      {/* header: title + register / upload */}
      <div className="flex items-center gap-3 px-7 pb-3 pt-5">
        <h1 className="text-[20px] font-bold text-foreground">Tables</h1>
        <span className="text-[12px] text-muted-foreground">{total.toLocaleString()} datasets</span>
        <span className="flex-1" />
        <button onClick={() => setRegisterOpen(true)} data-testid="register-dataset" title="Register a dataset by path / uri"
          className="inline-flex items-center gap-1.5 rounded-lg bg-foreground px-3.5 py-1.5 text-[12.5px] font-semibold text-background">
          <Icon name="plus" size={13} /> Register
        </button>
        <button onClick={() => fileRef.current?.click()} title="Upload a dataset file"
          className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-3.5 py-1.5 text-[12.5px] font-semibold text-foreground">
          <Icon name="export" size={13} /> Upload
        </button>
        <input ref={fileRef} type="file" accept=".parquet,.pq,.csv,.tsv,.json,.ndjson,.arrow,.feather,.ipc" className="hidden"
          onChange={(e) => { void onUpload(e.target.files?.[0]); e.target.value = '' }} />
      </div>

      {/* search + sort + active filters */}
      <div className="flex items-center gap-2 px-7 pb-2">
        <div className="relative flex-1">
          <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"><Icon name="search" size={13} /></span>
          <input value={rawQ} onChange={(e) => setRawQ(e.target.value)} data-testid="catalog-search"
            placeholder="Search by name, folder, description, or column…" aria-label="Search tables"
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
        <select value={`${sort}:${order}`} onChange={(e) => { const [s, o] = e.target.value.split(':'); setSort(s as Sort); setOrder(o as 'asc' | 'desc') }}
          disabled={semantic} aria-label="Sort tables"
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

      {selectedIds.size > 0 && (
        <div className="flex items-center gap-2 px-7 pb-2 text-[12px]" data-testid="catalog-selection-bar">
          <span className="font-semibold text-foreground">{selectedIds.size} selected</span>
          <button onClick={useSelected} className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-1 font-semibold text-primary hover:bg-accent">
            <Icon name="plus" size={11} /> Use
          </button>
          <button onClick={() => void deleteSelected()} data-testid="catalog-delete-selected"
            className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-1 font-semibold text-destructive hover:bg-accent">
            <Icon name="trash" size={11} /> Delete
          </button>
          <button onClick={clearSelection} className="rounded-md px-2 py-1 text-muted-foreground hover:text-foreground">Clear</button>
          <span className="flex-1" />
          {selectedIds.size < items.length && (
            <button onClick={selectAllLoaded} className="text-[11px] text-muted-foreground underline">Select all {items.length} loaded</button>
          )}
        </div>
      )}

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
                onToggleSelect={() => toggleSelect(t.id)} onOpen={() => setSelected(t)} onUse={() => use(t)} onFolder={setFolder} />}
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

      {selected && (
        <CatalogDetail key={selected.id} table={selected} onClose={() => setSelected(null)} onUse={use}
          onChanged={(t) => { setSelected(t); setCatalogRevision((v) => v + 1); void loadFirst() }} onFolder={(f) => { setFolder(f); setSelected(null) }}
          onDeleted={() => { setSelected(null); setCatalogRevision((v) => v + 1); void loadFirst() }} onOpenTable={setSelected}
          onColumn={(c) => { setHasColumns((cur) => cur.includes(c) ? cur : [...cur, c]); setSelected(null) }} />
      )}
      {dialog && <FileDialog mode="open" title="Open a dataset" onClose={() => setDialog(false)}
        onPick={async (r) => {
          await api.registerFile(r.uri)
          setCatalogRevision((v) => v + 1)
          await loadFirst()
          setDialog(false)
        }} />}
      {registerOpen && <RegisterModal onClose={() => setRegisterOpen(false)} onRegistered={onRegistered} />}

      {/* known folder paths → autocomplete for every folder input (register modal + detail drawer):
          the union of entry-derived facet folders and the folder ENTITIES (which include empty ones) */}
      <datalist id="dp-folder-options">
        {[...new Set([...facets.folders.map((f) => f.value), ...folderPaths])].map((v) => <option key={v} value={v} />)}
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
      <button type="button" onClick={onOpen} aria-label={`Open table ${t.name}`}
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
      <button type="button" onClick={onUse} aria-label={`Use table ${t.name}`}
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
          <Icon name="db" size={13} /> All tables
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

// ---- detail drawer: columns + metadata editor + lineage ---------------------
export function CatalogDetail({ table, onClose, onUse, onChanged, onFolder, onDeleted, onOpenTable, onColumn }: {
  table: CatalogTable; onClose: () => void; onUse: (t: CatalogTable) => void
  onChanged: (t: CatalogTable) => void; onFolder: (f: string) => void
  onDeleted: () => void; onOpenTable: (t: CatalogTable) => void; onColumn: (name: string) => void
}) {
  const pushToast = useStore((s) => s.pushToast)
  const openRelationships = useStore((s) => s.openRelationships)
  const catalogSource = useStore((s) => s.kernelInfo)
  const atomicMetadataEditable = catalogSource?.capabilities?.includes('catalog.atomic_metadata_edit') ?? false
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
  const [previewOpen, setPreviewOpen] = useState(false)
  const [preview, setPreview] = useState<SampleResult | null>(null)  // lazy: fetched on first expand only
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const initialKey = (t: CatalogTable) => t.keys?.find((k) => k.confidence === 'declared')?.columns ?? []
  const [declaredPk, setDeclaredPk] = useState(() => initialKey(table))
  const [conflict, setConflict] = useState(false)
  const [conflictBase, setConflictBase] = useState<CatalogTable | null>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const lineageRequest = useRef(0)
  const previewRequest = useRef(0)

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
    const s = ++previewRequest.current
    setPreviewLoading(true); setPreviewError(null)
    try {
      const next = await api.sample(table.uri, 30)
      if (s === previewRequest.current) setPreview(next)
    } catch (e) {
      if (s === previewRequest.current) setPreviewError(errorMessage(e))
    } finally {
      if (s === previewRequest.current) setPreviewLoading(false)
    }
  }
  const togglePreview = () => {
    const next = !previewOpen
    setPreviewOpen(next)
    if (next && !preview && !previewLoading) void loadPreview()
  }
  const unregister = async () => {
    if (!window.confirm(`Remove "${table.name}" from the catalog?`)) return
    setDeleting(true)
    try { await api.unregisterTable(table.id); pushToast('Removed from catalog', 'success'); onDeleted() }
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

  const togglePk = (col: string) => {
    const next = declaredPk.includes(col) ? declaredPk.filter((c) => c !== col) : [...declaredPk, col]
    setDeclaredPk(next)
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={requestClose}>
      <div role="dialog" aria-modal="true" aria-label={table.name}
        className="flex h-full w-[420px] flex-col overflow-y-auto border-l border-border bg-card shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <Icon name="db" size={16} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-[14px] font-bold text-foreground">{table.name}</div>
            <div className="truncate text-[10.5px] text-muted-foreground">{table.uri}</div>
          </div>
          <button onClick={() => onUse(table)} data-testid="detail-use" className="inline-flex items-center gap-1 rounded-md bg-primary/10 px-2.5 py-1 text-[11.5px] font-semibold text-primary"><Icon name="plus" size={12} /> Use</button>
          <button ref={closeRef} onClick={requestClose} aria-label="Close" className="text-muted-foreground hover:text-foreground"><Icon name="close" size={15} /></button>
        </div>

        <div className="flex flex-col gap-4 p-4 text-[12.5px]">
          <div className="flex flex-wrap gap-3 text-[11.5px] text-muted-foreground">
            <span>{table.rowCount == null ? '—' : table.rowCount.toLocaleString()} rows</span>
            <span>· {table.columns?.length ?? 0} cols</span>
            <span>· {table.version ?? 'v1'}</span>
            {table.usage ? <span>· used {table.usage}×</span> : null}
          </div>

          <DatasetRevisionHistory key={table.id} table={table} />

          {/* organization editor */}
          <section className="flex flex-col gap-2 rounded-lg border border-border p-3">
            <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Organization</div>
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
            <div className="flex justify-end">
              <button onClick={() => void save()} disabled={!atomicMetadataEditable || busy || !dirty} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50" data-testid="detail-save">{busy ? 'Saving…' : 'Save'}</button>
            </div>
          </section>

          {/* columns — the 🔑 toggles the declared primary key; the name filters the list to tables with it */}
          <section>
            <div className="mb-1 text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Columns</div>
            <div className="max-h-[220px] overflow-y-auto rounded-lg border border-border">
              {table.columns.map((c) => {
                const isPk = declaredPk.includes(c.name)
                return (
                  <div key={c.name} className="flex w-full items-center gap-1 border-b border-border/60 px-2 py-1 last:border-0 hover:bg-accent">
                    <button onClick={() => togglePk(c.name)} disabled={!atomicMetadataEditable} data-testid={`detail-pk-${c.name}`}
                      title={isPk ? 'Declared primary key — click to clear' : 'Click to declare as the primary key'}
                      className={`w-5 shrink-0 text-center text-[11px] ${isPk ? '' : 'opacity-25 hover:opacity-70'}`}>🔑</button>
                    <button onClick={() => onColumn(c.name)} title={`Filter the list to tables with column "${c.name}"`}
                      className="flex min-w-0 flex-1 items-center gap-2 text-left">
                      <span className="dp-mono flex-1 truncate text-[11.5px]">{c.name}</span>
                      <span className="text-[10px] text-muted-foreground">{c.type}</span>
                    </button>
                  </div>
                )
              })}
            </div>
          </section>

          {/* preview (lazy — a sample is only fetched when the section is first expanded) */}
          <section>
            <button onClick={togglePreview} data-testid="detail-preview"
              className="mb-1 flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide text-muted-foreground hover:text-foreground">
              <Icon name={previewOpen ? 'chevronDown' : 'chevronRight'} size={11} /> Preview
            </button>
            {previewOpen && <>
              {previewLoading && !preview ? <div className="px-1 py-1 text-[11px] text-muted-foreground">Loading…</div> : null}
              {previewError ? (
                <div role="alert" className="flex items-center justify-between gap-2 rounded-lg border border-destructive/30 px-3 py-2 text-[11px] text-destructive">
                  <span>Couldn't load preview: {previewError}{preview ? ' (showing stale preview)' : ''}</span>
                  <button onClick={() => void loadPreview()} data-testid="detail-preview-retry" className="shrink-0 font-semibold underline">Retry</button>
                </div>
              ) : null}
              {preview ? (
                <div className="flex flex-col gap-1">
                  {!preview.error && !preview.notPreviewable && <CatalogPreviewScope preview={preview} />}
                  {preview.error || preview.notPreviewable || !preview.rows.length
                    ? <div className="rounded-lg border border-border px-3 py-2 text-[11px] text-muted-foreground">
                        {preview.reason || emptyCatalogPreviewMessage(preview)}
                      </div>
                    : <div className="max-h-[240px] overflow-auto rounded-lg border border-border">
                        <table className="dp-mono w-max text-[10.5px]">
                          <thead><tr>{preview.columns.map((c) => (
                            <th key={c.name} className="sticky top-0 border-b border-border bg-muted px-2 py-1 text-left font-semibold">{c.name}</th>
                          ))}</tr></thead>
                          <tbody>{preview.rows.map((r, i) => (
                            <tr key={i}>{preview.columns.map((c) => (
                              <td key={c.name} className="max-w-[180px] truncate whitespace-nowrap border-b border-border/40 px-2 py-0.5">{cell(r[c.name])}</td>
                            ))}</tr>
                          ))}</tbody>
                        </table>
                      </div>}
                </div>
              ) : null}
            </>}
          </section>

          {/* lineage — click a row to open that dataset */}
          <section>
            <div className="mb-1 flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-muted-foreground"><Icon name="lineage" size={12} /> Lineage{lin?.truncated ? ' (truncated)' : ''}</div>
            {lineageLoading && !lin ? <div className="py-0.5 text-[11px] text-muted-foreground">Loading…</div> : null}
            {lineageError ? (
              <div role="alert" className="flex items-center justify-between gap-2 rounded-lg border border-destructive/30 px-2 py-1.5 text-[11px] text-destructive">
                <span>Couldn't load lineage: {lineageError}{lin ? ' (showing stale lineage)' : ''}</span>
                <button onClick={() => void loadLineage()} data-testid="detail-lineage-retry" className="shrink-0 font-semibold underline">Retry</button>
              </div>
            ) : null}
            {lin ? <>
              <LineageMini label="Parents" empty="no upstream datasets" onOpen={openLinked}
                rows={parents.map((e) => ({
                  name: nameOf(e.parent), factCount: e.factCount,
                  uri: e.parent, catalogId: lineageNode(e.parent)?.id,
                }))} />
              <LineageMini label="Children" empty="no downstream datasets" onOpen={openLinked}
                rows={children.map((e) => ({
                  name: nameOf(e.child), factCount: e.factCount,
                  uri: e.child, catalogId: lineageNode(e.child)?.id,
                }))} />
            </> : null}
          </section>

          <button onClick={() => openRelationships(table.uri)} data-testid="detail-relationships"
            className="inline-flex items-center gap-1.5 self-start text-[11.5px] text-primary hover:underline">
            <Icon name="lineage" size={12} /> View relationship graph →
          </button>
          {table.folder && (
            <button onClick={() => onFolder(table.folder!)} className="self-start text-[11.5px] text-primary hover:underline">Browse folder “{table.folder}” →</button>
          )}
          <button onClick={() => void unregister()} disabled={deleting} data-testid="detail-unregister"
            className="self-start text-[11.5px] text-destructive opacity-70 hover:underline hover:opacity-100 disabled:opacity-40">
            {deleting ? 'Removing…' : 'Remove from catalog…'}
          </button>
        </div>
      </div>
    </div>
  )
}

function CatalogPreviewScope({ preview }: { preview: SampleResult }) {
  const shown = preview.rows.length
  const total = preview.rowCount ?? null
  const provenance = preview.sampleProvenance
  const provenanceCounts = provenance
    ? `Requested ${provenance.requestedRows.toLocaleString()} rows · scanned ${provenance.scannedRows?.toLocaleString() ?? 'unknown'} · returned ${provenance.returnedRows.toLocaleString()} · total ${provenance.totalRows?.toLocaleString() ?? 'unknown'}.`
    : null
  let label: string
  if (preview.completeness === 'complete') {
    label = `Complete dataset · ${total ?? shown} ${(total ?? shown) === 1 ? 'row' : 'rows'}`
  } else if (preview.completeness === 'capped') {
    label = `Dataset preview · stopped at ${(preview.rowLimit ?? shown).toLocaleString()} rows${total == null ? '; total unknown' : ` of ${total.toLocaleString()}`}`
  } else if (total != null) {
    label = `Dataset preview · showing ${shown.toLocaleString()} of ${total.toLocaleString()} rows`
  } else {
    label = `Dataset preview · showing ${shown.toLocaleString()} rows; total unknown`
  }
  return (
    <div role="status" className="rounded-md bg-muted/50 px-2 py-1 text-[10.5px] text-muted-foreground">
      <div>{label}</div>
      {provenance && (
        <div className="mt-0.5 space-y-0.5">
          <div>{preview.completeness === 'complete' ? 'Complete dataset.' : 'Prefix preview.'} {provenanceCounts}</div>
          <div className="break-all">Input {provenance.datasetIdentity ?? 'unknown'} · revision {provenance.datasetRevision ?? 'unknown'}.</div>
          {provenance.limitations.map((limitation) => <div key={limitation}>{limitation}</div>)}
        </div>
      )}
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
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { closeRef.current?.focus() }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  const stem = uri.trim().replace(/\/+$/, '').split(/[\\/]/).pop()?.replace(/\.[^.]+$/, '') ?? ''
  const submit = async () => {
    const u = uri.trim(); if (!u || busy) return
    setBusy(true)
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
    } catch (e) { pushToast(errorMessage(e), 'error') }
    finally { setBusy(false) }
  }
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/30 p-4" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label="Register a dataset" data-testid="register-modal"
        className="flex w-[460px] max-w-full flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2">
          <h2 className="flex-1 text-[15px] font-bold text-foreground">Register a dataset</h2>
          <button ref={closeRef} onClick={onClose} aria-label="Close" className="text-muted-foreground hover:text-foreground"><Icon name="close" size={15} /></button>
        </div>
        <Field label="Path / URI">
          <input autoFocus value={uri} onChange={(e) => setUri(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void submit() }}
            placeholder="/data/events.parquet or s3://bucket/key" className="dp-input" data-testid="register-uri" />
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
    </div>
  )
}

function LineageMini({ label, empty, rows, onOpen }: {
  label: string; empty: string
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
        : <div className="py-0.5 text-[11px] text-muted-foreground">{empty}</div>}
    </div>
  )
}
