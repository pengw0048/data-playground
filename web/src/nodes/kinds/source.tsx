import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useNodeTransientSurface } from '../nodeTransientSurface'
import { roleCanEdit, useStore } from '../../store/graph'
import { Icon } from '../../ui/Icon'
import { Popover } from '../../ui/Popover'
import { FileDialog } from '../../ui/FileDialog'
import { api } from '../../api/client'
import type { CatalogTable, DatasetRevision, DatasetRevisionDetail, WorkspaceSearchGroup } from '../../types/api'
import { datasetRefIdentity, isParameterRef, type DatasetRef } from '../../types/graph'

type ExactRevisionState = 'idle' | 'checking' | 'available' | 'unavailable' | 'permission' | 'offline' | 'error'

const revisionUtcFormatter = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC',
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})

function formatRevisionUtc(value: string): string {
  const instant = new Date(value)
  return Number.isNaN(instant.getTime()) ? 'time unknown' : `${revisionUtcFormatter.format(instant)} UTC`
}

const kernelErrorStatus = (error: unknown) => typeof error === 'object' && error !== null
  && typeof (error as { status?: unknown }).status === 'number'
  ? (error as { status: number }).status : undefined

function exactRevisionFailure(error: unknown): Exclude<ExactRevisionState, 'idle' | 'checking' | 'available'> {
  const facts = typeof error === 'object' && error !== null
    ? error as { code?: unknown; status?: unknown } : {}
  if (facts.code === 'permission_denied' || facts.status === 403) return 'permission'
  if (facts.code === 'service_unavailable' || facts.status === 503) return 'offline'
  if (facts.code === 'resource_gone' || facts.status === 404 || facts.status === 410) return 'unavailable'
  return 'error'
}

function countSummary(rowCount: number | null | undefined, columnCount: number | null | undefined): string {
  const rows = rowCount == null ? 'Rows unknown' : `${rowCount.toLocaleString()} ${rowCount === 1 ? 'row' : 'rows'}`
  const columns = columnCount == null ? 'columns unknown' : `${columnCount} ${columnCount === 1 ? 'column' : 'columns'}`
  return `${rows} · ${columns}`
}

const localDatasetBinding = (table: CatalogTable) => ({
  uri: table.uri,
  tableId: table.id,
  ...(table.registrationId ? { registrationId: table.registrationId } : {}),
})

export type SourceEntryAction = 'select' | 'upload' | 'browse'

const pendingEntryActions = new Map<string, SourceEntryAction>()

// The Inspector uses the same picker that powers the Source card. Keeping the action at the
// Source component preserves the existing catalog, upload, and destination-registration paths.
export function requestSourceEntryAction(nodeId: string, action: SourceEntryAction) {
  pendingEntryActions.set(nodeId, action)
  window.dispatchEvent(new CustomEvent<SourceEntryAction>(`dataplay:source-entry:${nodeId}`, { detail: action }))
  window.setTimeout(() => {
    if (pendingEntryActions.get(nodeId) === action) pendingEntryActions.delete(nodeId)
  }, 5000)
}

function Source({ id, data }: NodeComponentProps) {
  const [open, setOpen] = useState(false)
  const [dialog, setDialog] = useState(false)
  const [registerOpen, setRegisterOpen] = useState(false)
  const [registerUri, setRegisterUri] = useState('')
  const [registering, setRegistering] = useState(false)
  const [registerError, setRegisterError] = useState<string | null>(null)
  const [workspaceDialog, setWorkspaceDialog] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [q, setQ] = useState('')
  const [results, setResults] = useState<CatalogTable[] | null>(null)  // null = not yet searched
  const [resultsError, setResultsError] = useState<string | null>(null)
  const [searchRevision, setSearchRevision] = useState(0)

  useEffect(() => {
    if (!registerOpen || registering) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setRegisterOpen(false)
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [registerOpen, registering])

  const btnRef = useRef<HTMLButtonElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const catalog = useStore((s) => s.catalog)
  const kernelUp = useStore((s) => s.kernelUp)
  const uploadDataset = useStore((s) => s.uploadDataset)
  const rememberTables = useStore((s) => s.rememberTables)
  const updateConfig = useStore((s) => s.updateConfig)
  const replaceSourceBinding = useStore((s) => s.replaceSourceBinding)
  const select = useStore((s) => s.select)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  // show the bound dataset even when the source was configured by tableId or a bare catalog NAME (an
  // agent/example/programmatic source), not only by an exact uri match.
  const tid = data.config.tableId
  const ref = String(data.config.uri ?? '')
  const table = catalog.find((t) => (tid && t.id === tid) || t.uri === ref || t.name === ref)
  const providerDataset = !!data.config.providerResourceRef
  const providerBinding = providerDataset || !!data.config.providerReadMode || ref.startsWith('workspace-provider://')
  const datasetRef = data.config.datasetRef
  const datasetParameter = isParameterRef(datasetRef) ? datasetRef : null
  const selectedRef = datasetRef && !isParameterRef(datasetRef) ? datasetRef : null
  const canvasParameters = useStore((s) => s.doc.parameters)
  const datasetParameters = (canvasParameters ?? []).filter((item) => item.type === 'dataset')
  const selectedExact = selectedRef ? datasetRefIdentity(selectedRef) : null
  const [exactDetail, setExactDetail] = useState<DatasetRevisionDetail | null>(null)
  const [exactDetailState, setExactDetailState] = useState<ExactRevisionState>('idle')
  const [exactDetailRequest, setExactDetailRequest] = useState(0)
  useNodeTransientSurface(`source-dataset-picker:${id}`, open, () => setOpen(false))

  // The selected exact revision is authoritative for a Source schema. In particular, a provider
  // binding may have no local CatalogTable at all, and a cached catalog row may have moved on.
  useEffect(() => {
    let live = true
    setExactDetail(null)
    if (!selectedExact) { setExactDetailState('idle'); return () => { live = false } }
    setExactDetailState('checking')
    api.datasetRevision(selectedExact.datasetId, selectedExact.revisionId).then((next) => {
      if (!live) return
      setExactDetail(next); setExactDetailState('available')
    }).catch((error) => {
      if (live) setExactDetailState(exactRevisionFailure(error))
    })
    return () => { live = false }
  }, [selectedExact?.datasetId, selectedExact?.revisionId, exactDetailRequest])

  useEffect(() => {
    if (!canEdit) { setOpen(false); setDialog(false); setRegisterOpen(false); setWorkspaceDialog(false) }
  }, [canEdit])

  useEffect(() => {
    const eventName = `dataplay:source-entry:${id}`
    const applyEntryAction = (action: SourceEntryAction) => {
      if (!canEdit) return
      if (action === 'select') setOpen(true)
      if (action === 'upload') fileRef.current?.click()
      if (action === 'browse') { setOpen(false); setRegisterError(null); setRegisterOpen(true) }
    }
    const handleEntryAction = (event: Event) => {
      pendingEntryActions.delete(id)
      applyEntryAction((event as CustomEvent<SourceEntryAction>).detail)
    }
    window.addEventListener(eventName, handleEntryAction)
    const pending = pendingEntryActions.get(id)
    if (pending) {
      pendingEntryActions.delete(id)
      applyEntryAction(pending)
    }
    return () => window.removeEventListener(eventName, handleEntryAction)
  }, [canEdit, id])

  // Server-side search picker — the catalog can be thousands of tables, so we never render them all.
  // Empty query shows the working-set recents PLUS a top-usage page from the server (a fresh session
  // has an empty working set — without the fetch a full catalog would look empty); typing searches
  // the whole catalog.
  useEffect(() => {
    if (!open) return
    const term = q.trim()
    setResults(null); setResultsError(null)
    let live = true
    const timer = setTimeout(async () => {
      try {
        const r = await api.tablesPage({ q: term || undefined, limit: 12, sort: 'usage', order: 'desc' })
        if (live) setResults(Array.isArray(r.items) ? r.items : [])
      } catch (e) {
        if (live) setResultsError(e instanceof Error ? e.message : String(e))
      }
    }, term ? 200 : 0)
    return () => { live = false; clearTimeout(timer) }
  }, [q, open, searchRevision])

  const recentIds = new Set(catalog.map((t) => t.id))
  const shown = (q.trim()
    ? (results ?? [])
    : [...catalog, ...(results ?? []).filter((t) => !recentIds.has(t.id))]  // recents first, deduped
  ).slice(0, 12)
  const pick = (t: CatalogTable) => {
    if (!canEdit) return
    rememberTables([t])  // warm the cache so the card resolves this immediately
    replaceSourceBinding(id, t.name, localDatasetBinding(t))
    setOpen(false); setQ('')
  }

  const pickWorkspaceProvider = async (resourceId: string) => {
    if (!canEdit) return
    const source = await api.workspaceProviderSource(resourceId)
    // This endpoint is the only source of provider binding, URI, and exact-revision facts. A
    // Workspace occurrence is navigation context, never a client-side recipe for Source config.
    replaceSourceBinding(id, source.name, source.config)
    setWorkspaceDialog(false)
  }

  // upload a local file → store it + bind this source to it
  const onUpload = async (f: File | undefined) => {
    if (!f || !canEdit) return
    setOpen(false); setUploading(true)
    const t = await uploadDataset(f)  // uploads + refreshes catalog; toasts on failure
    setUploading(false)
    if (t) replaceSourceBinding(id, t.name, localDatasetBinding(t))
  }

  // pick a file from a destination (local dir / object store) → register it + use it as this source
  const pickFile = async (uri: string) => {
    if (!canEdit) return
    const t = await api.registerFile(uri)
    rememberTables([t]); replaceSourceBinding(id, t.name, localDatasetBinding(t))
    setDialog(false); setRegisterOpen(false); setOpen(false)
  }

  const registerPath = async () => {
    const uri = registerUri.trim()
    if (!uri || registering) return
    setRegistering(true); setRegisterError(null)
    try { await pickFile(uri); setRegisterUri('') }
    catch (error) { setRegisterError(error instanceof Error ? error.message : String(error)) }
    finally { setRegistering(false) }
  }

  // A card is for choosing and orienting.  It deliberately names one source, one version state,
  // and one count/schema summary; source details belong in Inspector → Data details.
  const sourceLabel = providerBinding ? data.config.providerName ?? 'Provider' : 'Datasets'
  const meta = datasetParameter
    ? `${sourceLabel} · Run-time dataset parameter · Rows and columns vary by run`
    : selectedExact
      ? exactDetailState === 'available' && exactDetail
        ? `${sourceLabel} · Saved version · ${countSummary(exactDetail.summary.rowCount, exactDetail.preview.columns.length)}`
        : exactDetailState === 'unavailable'
          ? `${sourceLabel} · Selected version unavailable`
          : exactDetailState === 'permission'
            ? `${sourceLabel} · Selected version needs permission`
            : exactDetailState === 'offline'
              ? `${sourceLabel} · Selected version cannot be checked`
              : exactDetailState === 'error'
                ? `${sourceLabel} · Selected version could not be checked`
                : `${sourceLabel} · Selected version · Loading rows and columns…`
      : table
        ? `${sourceLabel} · ${countSummary(table.rowCount, table.columns.length)}`
        : providerBinding
          ? `${sourceLabel} · ${data.config.providerReadMode === 'exact' ? 'Version not selected · ' : ''}Rows and columns unknown`
          : 'Choose a dataset'

  return (
    <NodeCard id={id} data={data} metaOverride={meta}>
      {table || providerBinding ? (
        // Show the bound dataset name (the node title is separately editable, so it cannot be
        // relied on to say what is bound). The header eye is the distinct preview affordance.
        <button
          ref={btnRef}
          aria-label="Change dataset"
          title={`${table?.name ?? data.title} · ${String(data.config.uri ?? '')}\nClick to change dataset`}
          onClick={(e) => { e.stopPropagation(); select(id); setOpen((v) => !v) }}
          className="flex w-full items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1.5 text-[11.5px] text-muted-foreground"
        >
          <Icon name="db" size={13} />
          <span className="flex-1 truncate text-left font-medium text-foreground">{table?.name ?? data.title}</span>
          <Icon name="chevronDown" size={12} />
        </button>
      ) : (
        <button
          ref={btnRef}
          aria-label="Select dataset"
          title="Select dataset"
          onClick={(e) => { e.stopPropagation(); select(id); setOpen((v) => !v) }}
          className="flex w-full items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1.5 text-[11.5px] text-muted-foreground"
        >
          <Icon name="db" size={13} />
          <span className="flex-1 text-left">Select dataset</span>
          <Icon name="chevronDown" size={12} />
        </button>
      )}

      <Popover anchorRef={btnRef} open={open} onClose={() => setOpen(false)} width={250}>
        <div className="px-[9px] pb-1 pt-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Datasets</div>
        {/* Search the local catalog server-side (it can be thousands of tables). */}
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} onClick={(e) => e.stopPropagation()}
          placeholder="Search datasets…" data-testid="source-search"
          className="mb-1 w-full rounded-md border border-border bg-card px-2 py-1.5 text-[11.5px] outline-none focus:border-primary" />
        {resultsError && kernelUp && (
          <div role="alert" className="m-1 flex items-center justify-between gap-2 rounded-md border border-destructive/30 px-2 py-1.5 text-[11px] text-destructive">
            <span>Couldn't load catalog: {resultsError}{shown.length ? ' (showing recent datasets)' : ''}</span>
            <button onClick={(e) => { e.stopPropagation(); setSearchRevision((v) => v + 1) }} data-testid="source-search-retry"
              className="shrink-0 font-semibold underline">Retry</button>
          </div>
        )}
        {shown.length === 0 && (
          // distinguish a healthy-but-empty result from a down kernel (UX-14) — don't cry "offline" on
          // a fresh install with zero datasets
          <div className="p-2 text-[11.5px] text-muted-foreground">
            {!kernelUp ? 'Offline — datasets unavailable'
              : resultsError ? 'Catalog results unavailable'
              : q.trim() ? (results === null ? 'Searching…' : 'No matches')
              : results === null ? 'Loading…'
              : 'Catalog is empty — upload or browse below'}
          </div>
        )}
        {shown.map((t) => (
          <button
            key={t.id}
            onClick={(e) => { e.stopPropagation(); pick(t) }}
            className="flex w-full flex-col gap-px rounded-md px-[9px] py-[7px] text-left hover:bg-accent"
          >
            <span className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
              <span className="truncate">{t.name}</span>
              {t.folder && <span className="truncate text-[9.5px] font-normal text-muted-foreground">📁 {t.folder}</span>}
            </span>
            <span className="text-[10px] text-muted-foreground">
              {t.rowCount == null ? '—' : t.rowCount.toLocaleString()} rows · {t.columns.length} cols
            </span>
          </button>
        ))}
        <div className="my-1 h-px bg-border" />
        <button onClick={(e) => { e.stopPropagation(); setOpen(false); setWorkspaceDialog(true) }}
          className="flex w-full items-center gap-[7px] rounded-md px-[9px] py-[7px] text-left text-xs text-primary hover:bg-accent">
          <Icon name="search" size={12} /> Browse Workspace catalog…
        </button>
        <button onClick={(e) => { e.stopPropagation(); setOpen(false); setRegisterError(null); setRegisterOpen(true) }}
          className="flex w-full items-center gap-[7px] rounded-md px-[9px] py-[7px] text-left text-xs text-primary hover:bg-accent">
          <Icon name="search" size={12} /> Register accessible path / URI…
        </button>
        <button onClick={(e) => { e.stopPropagation(); fileRef.current?.click() }}
          className="flex w-full items-center gap-[7px] rounded-md px-[9px] py-[7px] text-left text-xs text-primary hover:bg-accent">
          <Icon name="export" size={12} /> Upload local file…
        </button>
      </Popover>
      {workspaceDialog && createPortal(
        <WorkspaceProviderPicker onClose={() => setWorkspaceDialog(false)} onPick={pickWorkspaceProvider} />,
        document.body,
      )}
      {uploading && <div className="mt-1 text-[10.5px] text-muted-foreground">Uploading…</div>}
      {datasetParameters.length > 0 && <select aria-label="Dataset run parameter" value={datasetParameter?.parameterRef ?? ''}
        disabled={!canEdit} onChange={(event) => updateConfig(id, {
          datasetRef: event.target.value ? { parameterRef: event.target.value } : undefined,
        })} className="mt-1.5 w-full rounded-md border border-border bg-background px-2 py-1 text-[10.5px]">
        <option value="">Pinned/current dataset</option>
        {datasetParameters.map((item) => <option key={item.name} value={item.name}>Parameter: {item.label || item.name}</option>)}
      </select>}
      {!datasetParameter && !providerBinding && (table || selectedRef) && <RevisionControl nodeId={id} table={table} selected={selectedRef ?? undefined}
        exactDetailState={exactDetailState} onRetryExact={() => setExactDetailRequest((value) => value + 1)}
        canEdit={canEdit} onChange={(datasetRef) => updateConfig(id, { datasetRef })} />}
      <input ref={fileRef} type="file" accept=".parquet,.pq,.csv,.tsv,.json,.ndjson,.arrow,.feather,.ipc" style={{ display: 'none' }}
        onChange={(e) => { void onUpload(e.target.files?.[0]); e.target.value = '' }} />
      {registerOpen && createPortal(<div className="fixed inset-0 z-50 grid place-items-center bg-black/30 p-4" onClick={() => { if (!registering) setRegisterOpen(false) }}>
        <form role="dialog" aria-modal="true" aria-label="Register path or URL"
          className="grid w-[500px] max-w-full gap-3 rounded-xl border border-border bg-card p-5 shadow-xl"
          onSubmit={(event) => { event.preventDefault(); void registerPath() }} onClick={(event) => event.stopPropagation()}>
          <div className="flex items-center gap-2">
            <h2 className="flex-1 text-[15px] font-bold text-foreground">Register path or URL</h2>
            <button type="button" onClick={() => setRegisterOpen(false)} disabled={registering} aria-label="Close"><Icon name="close" size={15} /></button>
          </div>
          <p className="text-[11.5px] leading-relaxed text-muted-foreground">Enter a file path or storage URL that Data Playground can access.</p>
          <input autoFocus aria-label="Dataset path or URL" value={registerUri} onChange={(event) => setRegisterUri(event.target.value)}
            placeholder="/data/events.parquet or s3://bucket/key" className="dp-input" />
          {registerError && <div role="alert" className="text-[11.5px] text-destructive">Couldn't register this dataset: {registerError}</div>}
          <div className="flex flex-wrap justify-end gap-2">
            <button type="button" onClick={() => { setRegisterOpen(false); setDialog(true) }} disabled={registering}
              className="mr-auto rounded-md border border-border px-3 py-1.5 text-[12px] font-semibold text-foreground">Browse storage</button>
            <button type="button" onClick={() => setRegisterOpen(false)} disabled={registering}
              className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
            <button type="submit" disabled={!registerUri.trim() || registering}
              className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{registering ? 'Registering…' : 'Register'}</button>
          </div>
        </form>
      </div>, document.body)}
      {dialog && <FileDialog mode="open" title="Open a dataset" onClose={() => setDialog(false)} onPick={(r) => pickFile(r.uri)} />}
    </NodeCard>
  )
}

function WorkspaceProviderPicker({ onClose, onPick }: {
  onClose: () => void
  onPick: (resourceId: string) => Promise<void>
}) {
  const [query, setQuery] = useState('')
  const [groups, setGroups] = useState<WorkspaceSearchGroup[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [request, setRequest] = useState(0)
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null)
  const [selecting, setSelecting] = useState<string | null>(null)
  const [selectionError, setSelectionError] = useState<string | null>(null)

  useEffect(() => {
    const term = query.trim()
    if (!term) { setGroups(null); setError(null); setNextCursor(null); setHasMore(false); return }
    let live = true
    const timer = setTimeout(() => {
      setGroups(null); setError(null); setSelectionError(null); setNextCursor(null); setHasMore(false); setLoadMoreError(null)
      void api.workspaceSearch(term, { limit: 25 }).then((page) => {
        if (!live) return
        setGroups(page.groups ?? []); setNextCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
      }).catch((caught) => {
        if (live) setError(caught instanceof Error ? caught.message : String(caught))
      })
    }, 150)
    return () => { live = false; clearTimeout(timer) }
  }, [query, request])

  const datasets = (groups ?? []).flatMap((group) => group.items
    .filter((item) => group.source.kind === 'provider' && ['complete', 'page'].includes(group.source.completeness)
      && item.source === 'provider' && item.kind === 'dataset' && !item.detached
      && item.referenceState === 'current' && item.canonicalReferenceState !== 'offline'
      && item.canonicalReferenceState !== 'permission_lost' && item.canonicalReferenceState !== 'detached'
      && item.canonicalReferenceState !== 'provider_error' && !item.lastKnown)
    .map((item) => ({ item, provider: group.source.provider ?? item.provider ?? 'Provider' })))
  const unavailable = (groups ?? []).filter((group) => group.source.kind === 'provider'
    && !['complete', 'page'].includes(group.source.completeness))
  const selectProvider = async (resourceId: string) => {
    if (selecting) return
    setSelecting(resourceId); setSelectionError(null)
    try { await onPick(resourceId) }
    catch (caught) { setSelectionError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setSelecting(null) }
  }
  const loadMore = async () => {
    const term = query.trim()
    if (!term || !nextCursor || loadingMore) return
    setLoadingMore(true); setLoadMoreError(null)
    try {
      const page = await api.workspaceSearch(term, { limit: 25, cursor: nextCursor })
      setGroups((current) => {
        const merged = new Map((current ?? []).map((group) => [group.source.id, { ...group, items: [...group.items] }]))
        for (const incoming of page.groups ?? []) {
          const existing = merged.get(incoming.source.id)
          if (!existing) { merged.set(incoming.source.id, incoming); continue }
          const ids = new Set(existing.items.map((item) => item.id))
          existing.items.push(...incoming.items.filter((item) => !ids.has(item.id)))
          existing.source = incoming.source
        }
        return [...merged.values()]
      })
      setNextCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
    } catch (caught) { setLoadMoreError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setLoadingMore(false) }
  }

  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/20 p-4" onMouseDown={onClose}>
    <div role="dialog" aria-modal="true" aria-label="Browse Workspace catalog" onMouseDown={(event) => event.stopPropagation()}
      className="flex max-h-[calc(100vh-2rem)] w-full max-w-[560px] flex-col rounded-lg border border-border bg-card p-4 shadow-xl">
      <div className="flex items-center gap-2"><div className="min-w-0 flex-1 text-sm font-semibold">Browse Workspace catalog</div><button type="button" aria-label="Close Workspace catalog" onClick={onClose}><Icon name="close" size={15} /></button></div>
      <p className="mt-1 text-[11px] text-muted-foreground">Search datasets from connected catalogs.</p>
      <input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} data-testid="workspace-source-search"
        placeholder="Search mounted datasets…" className="mt-3 w-full rounded-md border border-border bg-background px-2 py-2 text-sm outline-none focus:border-primary" />
      <div className="mt-2 min-h-0 overflow-y-auto rounded-md border border-border p-1" data-testid="workspace-source-results">
        {!query.trim() && <div className="p-2 text-[12px] text-muted-foreground">Enter a search to browse mounted Workspace providers.</div>}
        {query.trim() && groups === null && !error && <div className="p-2 text-[12px] text-muted-foreground">Searching mounted providers…</div>}
        {error && <div role="alert" className="flex items-center justify-between gap-2 p-2 text-[12px] text-destructive"><span>Couldn't search Workspace: {error}</span><button type="button" className="font-semibold underline" onClick={() => setRequest((value) => value + 1)}>Retry</button></div>}
        {unavailable.map((group) => <div key={group.source.id} role="status" className="flex items-center justify-between gap-2 rounded p-2 text-[11px] text-muted-foreground"><span>{group.source.provider ?? 'Provider'} unavailable: {group.source.error ?? group.source.completeness}</span><button type="button" className="font-semibold underline" onClick={() => setRequest((value) => value + 1)}>Retry</button></div>)}
        {datasets.map(({ item, provider }) => <button key={item.id} type="button" disabled={selecting !== null} onClick={() => void selectProvider(item.id)}
          className="flex w-full flex-col rounded-md px-2 py-2 text-left hover:bg-accent disabled:opacity-50">
          <span className="truncate text-xs font-semibold text-foreground">{item.name}</span><span className="truncate text-[10.5px] text-muted-foreground">{provider}{item.providerDatasetId ? ` · ${item.providerDatasetId}` : ''}</span>
        </button>)}
        {query.trim() && groups !== null && !error && datasets.length === 0 && unavailable.length === 0 && <div className="p-2 text-[12px] text-muted-foreground">No mounted provider datasets match this search.</div>}
        {hasMore && <button type="button" disabled={loadingMore} onClick={() => void loadMore()} className="w-full rounded-md px-2 py-2 text-xs font-semibold text-primary hover:bg-accent disabled:opacity-50">{loadingMore ? 'Loading…' : loadMoreError ? 'Retry loading more provider datasets' : 'Load more provider datasets'}</button>}
      </div>
      {selectionError && <div role="alert" className="mt-2 text-[12px] text-destructive">Couldn't select provider dataset: {selectionError}</div>}
      <div className="mt-3 flex justify-end"><button type="button" onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-xs">Cancel</button></div>
    </div>
  </div>
}

function RevisionControl({ nodeId, table, selected, exactDetailState: detailState, onRetryExact, canEdit, onChange }: {
  nodeId: string
  table?: CatalogTable
  selected?: DatasetRef
  exactDetailState: ExactRevisionState
  onRetryExact: () => void
  canEdit: boolean
  onChange: (value: DatasetRef | undefined) => void
}) {
  const anchorRef = useRef<HTMLButtonElement>(null)
  const historyGeneration = useRef(0)
  const [open, setOpen] = useState(false)
  const [request, setRequest] = useState(0)
  const [availability, setAvailability] = useState<'checking' | 'available' | 'unavailable' | 'error'>('checking')
  const [revisions, setRevisions] = useState<DatasetRevision[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [historyError, setHistoryError] = useState('')
  const [capabilitiesChecking, setCapabilitiesChecking] = useState(true)
  const [exactAvailable, setExactAvailable] = useState(false)
  const [asOfAvailable, setAsOfAvailable] = useState(false)
  const [asOfLocal, setAsOfLocal] = useState('')
  const [asOfResolving, setAsOfResolving] = useState(false)
  const [asOfError, setAsOfError] = useState('')

  useEffect(() => {
    const generation = ++historyGeneration.current
    let live = true
    setOpen(false); setRevisions([]); setCursor(null); setHasMore(false); setHistoryError('')
    setAsOfError(''); setExactAvailable(false); setAsOfAvailable(false); setAsOfResolving(false); setCapabilitiesChecking(true)
    if (!table) {
      setAvailability('unavailable'); setCapabilitiesChecking(false)
      return () => { live = false }
    }
    setAvailability('checking')
    api.datasetRevisionCapabilities(table.id).then((capabilities) => {
      if (!live || generation !== historyGeneration.current) return
      const exact = capabilities.selectors.includes('exact')
      setExactAvailable(exact)
      setAsOfAvailable(capabilities.selectors.includes('as_of')
        && capabilities.asOfOrdering === 'latest_committed_at_at_or_before'
        && capabilities.timezone === 'UTC')
      if (!exact) {
        setAvailability('unavailable')
        return
      }
      api.datasetRevisions(table.id, { limit: 20 }).then((page) => {
        if (!live || generation !== historyGeneration.current) return
        setRevisions(page.items)
        setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
        setAvailability('available')
      }).catch((error) => {
        if (!live || generation !== historyGeneration.current) return
        if (kernelErrorStatus(error) === 410 || kernelErrorStatus(error) === 501) {
          setAvailability('unavailable')
        } else {
          setHistoryError(error instanceof Error ? error.message : String(error))
          setAvailability('error')
        }
      })
    }).catch((error) => {
      if (live && generation === historyGeneration.current) {
        setExactAvailable(false); setAsOfAvailable(false)
        // A provider's explicit unsupported/missing response proves this Source has no selector.
        // Transport and server failures do not: keep the retryable error visible rather than making
        // a potentially supported control silently disappear.
        if (kernelErrorStatus(error) === 410 || kernelErrorStatus(error) === 501) {
          setAvailability('unavailable')
        } else {
          setHistoryError(error instanceof Error ? error.message : String(error))
          setAvailability('error')
        }
      }
    }).finally(() => {
      if (live && generation === historyGeneration.current) setCapabilitiesChecking(false)
    })
    return () => { live = false }
  }, [table?.id, table?.uri, request])

  const resolveAsOf = async () => {
    if (!table) return
    const requested = new Date(`${asOfLocal}Z`)
    if (!asOfLocal || Number.isNaN(requested.getTime())) {
      setAsOfError('Choose a valid UTC date and time.'); return
    }
    const generation = historyGeneration.current
    const asOf = requested.toISOString()
    setAsOfResolving(true); setAsOfError('')
    try {
      const resolved = await api.resolveDatasetRevision(table.id, asOf)
      if (generation !== historyGeneration.current) return
      if (resolved.selector !== 'as_of' || !resolved.committedAt) {
        throw new Error('Provider returned ambiguous ordering evidence.')
      }
      onChange({ kind: 'as_of', asOf, resolved: { ...resolved, selector: 'as_of' } })
      setOpen(false)
    } catch (error) {
      if (generation !== historyGeneration.current) return
      if (kernelErrorStatus(error) === 410) {
        setAsOfError('No saved version exists at or before that time.')
      } else if (kernelErrorStatus(error) === 409) {
        setAsOfError('The provider could not identify one stable version for that time.')
      } else {
        setAsOfError(error instanceof Error ? error.message : String(error))
      }
    } finally {
      if (generation === historyGeneration.current) setAsOfResolving(false)
    }
  }

  const loadMore = async () => {
    if (!table || !cursor || loadingMore) return
    const generation = ++historyGeneration.current
    setLoadingMore(true); setHistoryError('')
    try {
      const page = await api.datasetRevisions(table.id, { limit: 20, cursor })
      if (generation !== historyGeneration.current) return
      setRevisions((current) => {
        const seen = new Set(current.map((revision) => `${revision.datasetId}\u0000${revision.revisionId}`))
        return [...current, ...page.items.filter((revision) => !seen.has(`${revision.datasetId}\u0000${revision.revisionId}`))]
      })
      setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
    } catch (error) {
      if (generation === historyGeneration.current) setHistoryError(error instanceof Error ? error.message : String(error))
    } finally {
      if (generation === historyGeneration.current) setLoadingMore(false)
    }
  }

  const selectedExact = selected ? datasetRefIdentity(selected) : null
  const lastKnownAt = selected?.kind === 'as_of'
    ? selected.resolved.committedAt
    : selected?.lastKnown?.committedAt
  const staleLastKnown = lastKnownAt
    ? <> Last known provider commit {formatRevisionUtc(lastKnownAt)} <span className="font-semibold">(stale)</span>.</>
    : null
  const controlAvailable = (exactAvailable && availability === 'available') || asOfAvailable
  const registrationReplaced = selectedExact != null && revisions.length > 0
    && revisions.every((revision) => revision.datasetId !== selectedExact.datasetId)
  const checking = availability === 'checking' || capabilitiesChecking
  // Do not reserve card space for an inactive selector after capability discovery has proved that
  // neither exact nor as-of is available. Loading and errors remain visible because neither is proof.
  const showControl = checking || availability === 'error' || exactAvailable || asOfAvailable
  const capabilityError = availability === 'error' && !exactAvailable && !asOfAvailable
  const controlLabel = checking && !controlAvailable ? 'Checking revision capabilities…'
    : !controlAvailable ? 'Revision selection unavailable'
      : selected?.kind === 'as_of' ? `Change version selected as of ${formatRevisionUtc(selected.asOf)}`
        : selectedExact ? 'Change selected version'
          : availability === 'available' && asOfAvailable ? 'Choose a saved or as-of version'
            : asOfAvailable ? 'Choose version by time' : 'Pin a version'

  if (!showControl && !selectedExact) return null

  return (
    <div className="mt-1.5" data-testid={`source-revision-${nodeId}`}>
      {showControl && <button ref={anchorRef} type="button" disabled={!canEdit || !controlAvailable}
        title={controlLabel} onClick={(event) => { event.stopPropagation(); setOpen((value) => !value) }}
        className="flex w-full items-center gap-1 rounded-md border border-border bg-muted/30 px-2 py-1 text-left text-[10px] text-muted-foreground disabled:cursor-not-allowed disabled:opacity-60">
        <Icon name="clock" size={11} />
        <span className="min-w-0 flex-1 truncate">{controlLabel}</span>
        {controlAvailable && <Icon name="chevronDown" size={10} />}
      </button>}
      {availability === 'error' && (
        <div role="alert" className="mt-1 text-[9.5px] text-destructive">
          {capabilityError ? "Couldn't check revision capabilities" : "Couldn't load revision history"}: {historyError}{' '}
          <button type="button" className="font-semibold underline" onClick={() => setRequest((value) => value + 1)}>Retry</button>
        </div>
      )}
      {selectedExact && detailState === 'checking' && <div role="status" className="mt-1 text-[9.5px] text-muted-foreground">Checking selected version…</div>}
      {selectedExact && detailState === 'unavailable' && (
        <div role="alert" className="mt-1 text-[9.5px] text-destructive">
          Selected version is missing or compacted. Your selection is unchanged.
          {staleLastKnown}{' '}
          {registrationReplaced && 'The current catalog entry now points to a different dataset. '}
          {controlAvailable && <button type="button" disabled={!canEdit} className="font-semibold underline disabled:opacity-50" onClick={() => setOpen(true)}>Choose another saved version</button>}
          {controlAvailable && ' or '}
          {table ? <><button type="button" disabled={!canEdit} className="font-semibold underline disabled:opacity-50" onClick={() => onChange(undefined)}>follow current latest explicitly</button>.</>
            : 'Choose a new dataset above to create a new binding.'}
        </div>
      )}
      {selectedExact && detailState === 'permission' && (
        <div role="alert" className="mt-1 text-[9.5px] text-destructive">
          Permission to open the selected version was lost. Your selection is unchanged.{staleLastKnown}{' '}
          <button type="button" className="font-semibold underline" onClick={onRetryExact}>Retry selected version</button>
        </div>
      )}
      {selectedExact && detailState === 'offline' && (
        <div role="alert" className="mt-1 text-[9.5px] text-destructive">
          The provider is offline, so the selected version could not be verified. Your selection is unchanged.{staleLastKnown}{' '}
          <button type="button" className="font-semibold underline" onClick={onRetryExact}>Retry provider</button>
        </div>
      )}
      {selectedExact && detailState === 'error' && (
        <div role="alert" className="mt-1 text-[9.5px] text-destructive">
          The selected version could not be verified. Your selection is unchanged.{staleLastKnown}{' '}
          <button type="button" className="font-semibold underline" onClick={onRetryExact}>Retry verification</button>
        </div>
      )}
      {showControl && <Popover anchorRef={anchorRef} open={open} onClose={() => setOpen(false)} width={320} maxHeight={380}>
        <div className="px-2 py-1 text-[10px] text-muted-foreground">Select one saved version. This Source will not switch to latest automatically.</div>
        {selected && (
          <button type="button" onClick={() => { onChange(undefined); setOpen(false) }}
            className="w-full rounded-md px-2 py-1.5 text-left text-[11px] font-semibold text-primary hover:bg-accent">
            Follow latest instead
          </button>
        )}
        {availability === 'available' && revisions.length === 0 && <div className="px-2 py-2 text-[11px] text-muted-foreground">No saved versions.</div>}
        {revisions.map((revision, index) => {
          const active = selectedExact?.datasetId === revision.datasetId && selectedExact.revisionId === revision.revisionId
          return (
            <button key={`${revision.datasetId}:${revision.revisionId}`} type="button"
              aria-pressed={active} onClick={() => {
                onChange({
                  kind: 'exact', datasetId: revision.datasetId, revisionId: revision.revisionId,
                  lastKnown: { committedAt: revision.committedAt ?? null },
                }); setOpen(false)
              }} className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-accent ${active ? 'bg-accent' : ''}`}>
              <span className="dp-mono min-w-0 flex-1 truncate text-[11px] font-semibold text-foreground">{revision.revisionId}</span>
              {index === 0 && <span className="rounded bg-muted px-1 text-[9px] text-muted-foreground">latest saved</span>}
              <span className="shrink-0 text-[9px] text-muted-foreground">{revision.committedAt ? formatRevisionUtc(revision.committedAt) : 'time unknown'}</span>
            </button>
          )
        })}
        {hasMore && (
          <button type="button" disabled={loadingMore} onClick={() => void loadMore()}
            className="w-full rounded-md px-2 py-1.5 text-center text-[10.5px] font-semibold text-primary hover:bg-accent disabled:opacity-50">
            {loadingMore ? 'Loading…' : historyError ? 'Retry loading more' : 'Load more saved versions'}
          </button>
        )}
        {asOfAvailable && <div className="mt-1 border-t border-border px-2 pt-2">
          <div className="text-[10.5px] font-semibold text-foreground">Resolve as of a timestamp</div>
          <div className="mt-0.5 text-[9.5px] text-muted-foreground">
            Select the latest provider commit at or before this UTC instant (inclusive). The saved intent remains UTC.
          </div>
          <div className="mt-1.5 flex gap-1">
            <input type="datetime-local" step={1} value={asOfLocal} onChange={(event) => setAsOfLocal(event.target.value)}
              aria-label="As-of UTC date and time" className="min-w-0 flex-1 rounded border border-border bg-card px-1.5 py-1 text-[10.5px]" />
            <button type="button" disabled={!asOfLocal || asOfResolving} onClick={() => void resolveAsOf()}
              className="rounded bg-primary px-2 py-1 text-[10px] font-semibold text-primary-foreground disabled:opacity-50">
              {asOfResolving ? 'Resolving…' : 'Resolve once'}
            </button>
          </div>
          {asOfError && <div role="alert" className="mt-1 text-[9.5px] text-destructive">{asOfError}</div>}
        </div>}
      </Popover>}
    </div>
  )
}

register(
  {
    kind: 'source',
    title: 'source',
    category: 'io',
    tag: 'dataset',
    inputs: [],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'Choose a registered dataset',
    defaultData: () => ({ title: 'source', status: 'draft', config: {}, meta: 'pick a table' }),
  },
  Source,
)
