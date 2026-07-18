import { useEffect, useRef, useState } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { roleCanEdit, useStore } from '../../store/graph'
import { Icon } from '../../ui/Icon'
import { Popover } from '../../ui/Popover'
import { FileDialog } from '../../ui/FileDialog'
import { api } from '../../api/client'
import type { CatalogTable, DatasetRevision, DatasetRevisionDetail } from '../../types/api'
import { datasetRefIdentity, type DatasetRef } from '../../types/graph'

type ExactRevisionState = 'idle' | 'checking' | 'available' | 'unavailable' | 'permission' | 'offline' | 'error'

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

function Source({ id, data }: NodeComponentProps) {
  const [open, setOpen] = useState(false)
  const [dialog, setDialog] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [q, setQ] = useState('')
  const [results, setResults] = useState<CatalogTable[] | null>(null)  // null = not yet searched
  const [resultsError, setResultsError] = useState<string | null>(null)
  const [searchRevision, setSearchRevision] = useState(0)
  const btnRef = useRef<HTMLButtonElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const catalog = useStore((s) => s.catalog)
  const kernelUp = useStore((s) => s.kernelUp)
  const uploadDataset = useStore((s) => s.uploadDataset)
  const rememberTables = useStore((s) => s.rememberTables)
  const updateConfig = useStore((s) => s.updateConfig)
  const rename = useStore((s) => s.rename)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  // show the bound dataset even when the source was configured by tableId or a bare catalog NAME (an
  // agent/example/programmatic source), not only by an exact uri match.
  const tid = data.config.tableId
  const ref = String(data.config.uri ?? '')
  const table = catalog.find((t) => (tid && t.id === tid) || t.uri === ref || t.name === ref)

  useEffect(() => {
    if (!canEdit) { setOpen(false); setDialog(false) }
  }, [canEdit])

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
    updateConfig(id, { uri: t.uri, tableId: t.id, datasetRef: undefined })
    rename(id, t.name)
    setOpen(false); setQ('')
  }

  // upload a local file → store it + bind this source to it
  const onUpload = async (f: File | undefined) => {
    if (!f || !canEdit) return
    setOpen(false); setUploading(true)
    const t = await uploadDataset(f)  // uploads + refreshes catalog; toasts on failure
    setUploading(false)
    if (t) { updateConfig(id, { uri: t.uri, tableId: t.id, datasetRef: undefined }); rename(id, t.name) }
  }

  // pick a file from a destination (local dir / object store) → register it + use it as this source
  const pickFile = async (uri: string) => {
    if (!canEdit) return
    const t = await api.registerFile(uri)
    rememberTables([t]); updateConfig(id, { uri: t.uri, tableId: t.id, datasetRef: undefined }); rename(id, t.name)
    setDialog(false); setOpen(false)
  }

  const meta = table
    // an UNKNOWN count (null/undefined) shows "—", not a fake "0 rows" (UX-14)
    ? `${table.rowCount == null ? '—' : table.rowCount.toLocaleString()} rows · ${table.columns.length} cols · ${table.version ?? 'v1'}`
    : 'pick a table'

  return (
    <NodeCard id={id} data={data} metaOverride={meta}>
      {table ? (
        // show the BOUND dataset name (the node title is separately editable, so it can't be relied on
        // to say what's bound); the row itself is the "change" affordance, uri in the tooltip
        <button
          ref={btnRef}
          title={`${table.name} · ${String(data.config.uri ?? '')}\nClick to change dataset`}
          onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
          className="flex w-full items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1.5 text-[11.5px] text-muted-foreground"
        >
          <Icon name="db" size={13} />
          <span className="flex-1 truncate text-left font-medium text-foreground">{table.name}</span>
          <Icon name="chevronDown" size={12} />
        </button>
      ) : (
        <button
          ref={btnRef}
          onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
          className="flex w-full items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1.5 text-[11.5px] text-muted-foreground"
        >
          <Icon name="db" size={13} />
          <span className="flex-1 text-left">Select dataset</span>
          <Icon name="chevronDown" size={12} />
        </button>
      )}

      <Popover anchorRef={btnRef} open={open} onClose={() => setOpen(false)} width={250}>
        {/* search the whole catalog server-side (it can be thousands of tables) */}
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
            {!kernelUp ? 'Kernel offline — no catalog'
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
        <button onClick={(e) => { e.stopPropagation(); setOpen(false); setDialog(true) }}
          className="flex w-full items-center gap-[7px] rounded-md px-[9px] py-[7px] text-left text-xs text-primary hover:bg-accent">
          <Icon name="search" size={12} /> Browse files…
        </button>
        <button onClick={(e) => { e.stopPropagation(); fileRef.current?.click() }}
          className="flex w-full items-center gap-[7px] rounded-md px-[9px] py-[7px] text-left text-xs text-primary hover:bg-accent">
          <Icon name="export" size={12} /> Upload a file…
        </button>
      </Popover>
      {uploading && <div className="mt-1 text-[10.5px] text-muted-foreground">Uploading…</div>}
      {(table || data.config.datasetRef) && <RevisionControl nodeId={id} table={table} selected={data.config.datasetRef}
        canEdit={canEdit} onChange={(datasetRef) => updateConfig(id, { datasetRef })} />}
      <input ref={fileRef} type="file" accept=".parquet,.pq,.csv,.tsv,.json,.ndjson,.arrow,.feather,.ipc" style={{ display: 'none' }}
        onChange={(e) => { void onUpload(e.target.files?.[0]); e.target.value = '' }} />
      {dialog && <FileDialog mode="open" title="Open a dataset" onClose={() => setDialog(false)} onPick={(r) => pickFile(r.uri)} />}
    </NodeCard>
  )
}

function RevisionControl({ nodeId, table, selected, canEdit, onChange }: {
  nodeId: string
  table?: CatalogTable
  selected?: DatasetRef
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
  const [detail, setDetail] = useState<DatasetRevisionDetail | null>(null)
  const [detailState, setDetailState] = useState<ExactRevisionState>('idle')
  const [detailRequest, setDetailRequest] = useState(0)
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

  useEffect(() => {
    let live = true
    setDetail(null)
    if (!selected) { setDetailState('idle'); return () => { live = false } }
    const exact = datasetRefIdentity(selected)
    setDetailState('checking')
    api.datasetRevision(exact.datasetId, exact.revisionId).then((next) => {
      if (!live) return
      setDetail(next); setDetailState('available')
    }).catch((error) => {
      if (live) setDetailState(exactRevisionFailure(error))
    })
    return () => { live = false }
  }, [selected, detailRequest])

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
        setAsOfError('No retained revision exists at or before that instant.')
      } else if (kernelErrorStatus(error) === 409) {
        setAsOfError('The provider could not prove one exact revision for that instant.')
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
    ? <> Last known provider commit {new Date(lastKnownAt).toLocaleString()} <span className="font-semibold">(stale)</span>.</>
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
      : selected?.kind === 'as_of' ? `As of ${new Date(selected.asOf).toLocaleString()} → ${selectedExact?.revisionId}`
        : selectedExact ? `Change pinned revision ${selectedExact.revisionId}`
          : availability === 'available' && asOfAvailable ? 'Choose exact or as-of revision'
            : asOfAvailable ? 'Choose revision as of a time' : 'Pin exact revision'

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
      {selectedExact && detailState === 'checking' && <div role="status" className="mt-1 text-[9.5px] text-muted-foreground">Opening selected revision {selectedExact.revisionId}…</div>}
      {selected && detailState === 'available' && detail && (
        <div className="mt-1 break-all text-[9.5px] text-muted-foreground">
          {selected.kind === 'as_of' ? `As-of intent ${new Date(selected.asOf).toLocaleString()} resolved once to` : 'Pinned exact'} revision {detail.revisionId} · {detail.summary.rowCount?.toLocaleString() ?? 'unknown'} rows
        </div>
      )}
      {selectedExact && detailState === 'unavailable' && (
        <div role="alert" className="mt-1 text-[9.5px] text-destructive">
          Selected revision {selectedExact.revisionId} or its registration is missing or compacted. Selection preserved; latest was not substituted.
          {staleLastKnown}{' '}
          {registrationReplaced && 'The current catalog registration has a different dataset identity. '}
          {controlAvailable && <button type="button" disabled={!canEdit} className="font-semibold underline disabled:opacity-50" onClick={() => setOpen(true)}>Choose another retained revision</button>}
          {controlAvailable && ' or '}
          {table ? <><button type="button" disabled={!canEdit} className="font-semibold underline disabled:opacity-50" onClick={() => onChange(undefined)}>follow current latest explicitly</button>.</>
            : 'Choose a new dataset above to create a new binding.'}
        </div>
      )}
      {selectedExact && detailState === 'permission' && (
        <div role="alert" className="mt-1 text-[9.5px] text-destructive">
          Permission to open exact revision {selectedExact.revisionId} was lost. Selection preserved; latest was not substituted.{staleLastKnown}{' '}
          <button type="button" className="font-semibold underline" onClick={() => setDetailRequest((value) => value + 1)}>Retry exact revision</button>
        </div>
      )}
      {selectedExact && detailState === 'offline' && (
        <div role="alert" className="mt-1 text-[9.5px] text-destructive">
          The provider is offline, so exact revision {selectedExact.revisionId} could not be verified. Selection preserved; latest was not substituted.{staleLastKnown}{' '}
          <button type="button" className="font-semibold underline" onClick={() => setDetailRequest((value) => value + 1)}>Retry provider</button>
        </div>
      )}
      {selectedExact && detailState === 'error' && (
        <div role="alert" className="mt-1 text-[9.5px] text-destructive">
          Exact revision {selectedExact.revisionId} could not be verified. Selection preserved; latest was not substituted.{staleLastKnown}{' '}
          <button type="button" className="font-semibold underline" onClick={() => setDetailRequest((value) => value + 1)}>Retry verification</button>
        </div>
      )}
      {showControl && <Popover anchorRef={anchorRef} open={open} onClose={() => setOpen(false)} width={320} maxHeight={380}>
        <div className="px-2 py-1 text-[10px] text-muted-foreground">Persist one exact provider revision. The Source will never retarget it to latest.</div>
        {selected && (
          <button type="button" onClick={() => { onChange(undefined); setOpen(false) }}
            className="w-full rounded-md px-2 py-1.5 text-left text-[11px] font-semibold text-primary hover:bg-accent">
            Follow latest instead
          </button>
        )}
        {availability === 'available' && revisions.length === 0 && <div className="px-2 py-2 text-[11px] text-muted-foreground">No retained revisions.</div>}
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
              {index === 0 && <span className="rounded bg-muted px-1 text-[9px] text-muted-foreground">latest retained</span>}
              <span className="shrink-0 text-[9px] text-muted-foreground">{revision.committedAt ? new Date(revision.committedAt).toLocaleString() : 'time unknown'}</span>
            </button>
          )
        })}
        {hasMore && (
          <button type="button" disabled={loadingMore} onClick={() => void loadMore()}
            className="w-full rounded-md px-2 py-1.5 text-center text-[10.5px] font-semibold text-primary hover:bg-accent disabled:opacity-50">
            {loadingMore ? 'Loading…' : historyError ? 'Retry loading more' : 'Load more retained revisions'}
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
    blurb: 'read a registered dataset',
    defaultData: () => ({ title: 'source', status: 'draft', config: {}, meta: 'pick a table' }),
  },
  Source,
)
