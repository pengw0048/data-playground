import { useCallback, useEffect, useRef, useState } from 'react'
import { api, KernelError } from '../api/client'
import { compareSchemas } from '../lib/schemaCompatibility'
import type {
  CatalogTable, DatasetRevision, DatasetRevisionDetail, DatasetRevisionSummary,
  SchemaCompatibilityStatus,
} from '../types/api'
import { Icon } from '../ui/Icon'

const PAGE_SIZE = 20
const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)
const statusOf = (error: unknown) => error instanceof KernelError ? error.status
  : typeof error === 'object' && error !== null ? (error as { status?: unknown }).status : undefined

function timestamp(value?: string | null) {
  if (!value) return 'Commit time not provided'
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toISOString().replace('.000Z', 'Z')
}

function bytes(value?: number | null) {
  if (value == null) return 'unknown'
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let amount = value / 1024
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1 }
  return `${amount.toFixed(amount >= 10 ? 0 : 1)} ${units[unit]}`
}

function number(value?: number | null) { return value == null ? 'unknown' : value.toLocaleString() }
function delta(current?: number | null, parent?: number | null) {
  if (current == null || parent == null) return 'change unknown'
  const change = current - parent
  return change === 0 ? 'no change' : `${change > 0 ? '+' : ''}${change.toLocaleString()}`
}

export function DatasetRevisionHistory({ table }: { table: CatalogTable }) {
  const [availability, setAvailability] = useState<'checking' | 'supported' | 'absent' | 'unavailable' | 'error'>('checking')
  const [items, setItems] = useState<DatasetRevision[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null)
  const [selected, setSelected] = useState<DatasetRevision | null>(null)
  const [detail, setDetail] = useState<DatasetRevisionDetail | null>(null)
  const [parent, setParent] = useState<DatasetRevisionDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [parentError, setParentError] = useState<string | null>(null)
  const historyRequest = useRef(0)
  const detailRequest = useRef(0)

  const loadFirst = useCallback(async () => {
    const request = ++historyRequest.current
    setAvailability('checking'); setHistoryError(null); setLoadMoreError(null)
    try {
      const page = await api.datasetRevisions(table.id, { limit: PAGE_SIZE })
      if (request !== historyRequest.current) return
      setItems(page.items); setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
      setAvailability('supported')
    } catch (error) {
      if (request !== historyRequest.current) return
      const status = statusOf(error)
      if (status === 501) setAvailability('absent')
      else if (status === 410) setAvailability('unavailable')
      else { setAvailability('error'); setHistoryError(errorMessage(error)) }
    }
  }, [table.id])

  useEffect(() => {
    void loadFirst()
    return () => { historyRequest.current += 1; detailRequest.current += 1 }
  }, [loadFirst])

  const loadMore = async () => {
    if (!cursor || loadingMore) return
    const request = ++historyRequest.current
    setLoadingMore(true); setLoadMoreError(null)
    try {
      const page = await api.datasetRevisions(table.id, { limit: PAGE_SIZE, cursor })
      if (request !== historyRequest.current) return
      setItems((current) => {
        const seen = new Set(current.map((revision) => `${revision.datasetId}\u0000${revision.revisionId}`))
        return [...current, ...page.items.filter((revision) => !seen.has(`${revision.datasetId}\u0000${revision.revisionId}`))]
      })
      setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
    } catch (error) {
      if (request === historyRequest.current) setLoadMoreError(errorMessage(error))
    } finally {
      if (request === historyRequest.current) setLoadingMore(false)
    }
  }

  const openRevision = useCallback(async (revision: DatasetRevision) => {
    const request = ++detailRequest.current
    setSelected(revision); setDetail(null); setParent(null); setDetailError(null); setParentError(null); setDetailLoading(true)
    try {
      const next = await api.datasetRevision(revision.datasetId, revision.revisionId)
      if (request !== detailRequest.current) return
      setDetail(next); setDetailLoading(false)
      if (!next.parentRevisionId) return
      try {
        const parentDetail = await api.datasetRevision(next.datasetId, next.parentRevisionId)
        if (request === detailRequest.current) setParent(parentDetail)
      } catch (error) {
        if (request !== detailRequest.current) return
        setParentError(statusOf(error) === 410
          ? 'The parent revision is no longer retained; schema and summary comparison are unavailable.'
          : `Couldn't load the parent comparison: ${errorMessage(error)}`)
      }
    } catch (error) {
      if (request !== detailRequest.current) return
      setDetailError(statusOf(error) === 410
        ? 'This exact revision is no longer retained. The Catalog did not substitute latest.'
        : `Couldn't open this exact revision: ${errorMessage(error)}`)
      setDetailLoading(false)
    }
  }, [])

  if (availability === 'absent') return null
  return <section data-testid="dataset-revision-history" className="flex flex-col gap-2 rounded-lg border border-border p-3">
    <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-muted-foreground">
      <Icon name="clock" size={12} /> Revision history
    </div>
    {availability === 'checking' && <div role="status" className="text-[11px] text-muted-foreground">Checking revision history availability…</div>}
    {availability === 'unavailable' && <div className="text-[11px] text-muted-foreground">Revision history is unavailable for this registration. No latest revision was substituted.</div>}
    {availability === 'error' && <HistoryFailure message={`Couldn't load revision history: ${historyError}`} onRetry={loadFirst} />}
    {availability === 'supported' && <>
      {items.length === 0 ? <div className="text-[11px] text-muted-foreground">No retained revisions are available.</div>
        : <div className="max-h-[188px] overflow-y-auto rounded-md border border-border">
          {items.map((revision) => {
            const active = selected?.datasetId === revision.datasetId && selected.revisionId === revision.revisionId
            return <button key={`${revision.datasetId}:${revision.revisionId}`} type="button"
              aria-label={`Open revision ${revision.revisionId}`} onClick={() => void openRevision(revision)}
              className={`flex w-full items-start gap-2 border-b border-border/60 px-2 py-1.5 text-left last:border-0 hover:bg-accent ${active ? 'bg-accent' : ''}`}>
              <span className="min-w-0 flex-1">
                <span className="dp-mono block break-all text-[10.5px] font-semibold text-foreground">{revision.revisionId}</span>
                <span className="block text-[9.5px] text-muted-foreground">{timestamp(revision.committedAt)}</span>
              </span>
              <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[9px] text-muted-foreground">retained by {revision.retentionOwner}</span>
            </button>
          })}
        </div>}
      {hasMore && <div className="flex items-center justify-between gap-2 text-[10.5px] text-muted-foreground">
        <span>More retained revisions are available through the bounded history cursor.</span>
        <button onClick={() => void loadMore()} disabled={loadingMore} data-testid="revision-history-load-more" className="shrink-0 font-semibold text-primary underline disabled:opacity-50">{loadingMore ? 'Loading…' : loadMoreError ? 'Retry' : 'Load more'}</button>
      </div>}
      {loadMoreError && <div role="alert" className="text-[10.5px] text-destructive">Couldn't load more history: {loadMoreError}</div>}
      {selected && <RevisionDetail revision={selected} detail={detail} parent={parent} loading={detailLoading}
        error={detailError} parentError={parentError} onRetry={() => void openRevision(selected)} />}
    </>}
  </section>
}

function HistoryFailure({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <div role="alert" className="flex items-center justify-between gap-2 text-[11px] text-destructive">
    <span>{message}</span><button onClick={() => void onRetry()} className="shrink-0 font-semibold underline">Retry</button>
  </div>
}

function RevisionDetail({ revision, detail, parent, loading, error, parentError, onRetry }: {
  revision: DatasetRevision; detail: DatasetRevisionDetail | null; parent: DatasetRevisionDetail | null
  loading: boolean; error: string | null; parentError: string | null; onRetry: () => void
}) {
  if (loading) return <div role="status" className="rounded-md bg-muted/40 px-2 py-2 text-[11px] text-muted-foreground">Opening exact revision {revision.revisionId}…</div>
  if (error) return <HistoryFailure message={error} onRetry={onRetry} />
  if (!detail) return null
  const compatibility = parent ? compareSchemas(parent.preview.columns, detail.preview.columns) : null
  const notableFields = compatibility?.fields.filter((field) => field.kind !== 'unchanged' || field.status !== 'compatible') ?? []
  return <div className="flex flex-col gap-2 border-t border-border pt-2" data-testid="revision-detail">
    <div>
      <div className="dp-mono break-all text-[10.5px] font-semibold text-foreground">Exact revision {detail.revisionId}</div>
      <div className="text-[9.5px] text-muted-foreground">Dataset {detail.datasetId} · {timestamp(detail.committedAt)} · retained by {detail.retentionOwner}</div>
      <div className="text-[9.5px] text-muted-foreground">Parent {detail.parentRevisionId ?? 'not evidenced'} · producer {detail.producerOperation ?? 'not provided'}</div>
    </div>
    <Summary current={detail.summary} parent={parent?.summary ?? null} />
    {parentError ? <div role="alert" className="text-[10.5px] text-muted-foreground">{parentError}</div>
      : !detail.parentRevisionId ? <div className="text-[10.5px] text-muted-foreground">No retained parent evidence is available; schema and summary changes are unknown.</div>
        : !parent ? <div role="status" className="text-[10.5px] text-muted-foreground">Loading parent comparison…</div>
          : <div className="rounded-md border border-border p-2">
            <div className="flex items-center justify-between gap-2 text-[10px] font-semibold text-foreground">
              <span>Schema compatibility with parent</span><CompatibilityBadge status={compatibility!.status} />
            </div>
            {notableFields.length ? <div className="mt-1 flex flex-col gap-1">
              {notableFields.map((field, index) => <div key={`${field.fieldId ?? field.oldName ?? field.newName}:${index}`} className="text-[9.5px] text-muted-foreground">
                <span className="font-semibold text-foreground">{field.newName ?? field.oldName ?? field.fieldId ?? 'field'}: </span>{field.reason}
              </div>)}
            </div> : <div className="mt-1 text-[9.5px] text-muted-foreground">No schema field changes.</div>}
          </div>}
    <ExactPreview detail={detail} />
  </div>
}

function CompatibilityBadge({ status }: { status: SchemaCompatibilityStatus }) {
  const className = status === 'breaking' ? 'bg-destructive/10 text-destructive'
    : status === 'compatible' ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
      : 'bg-muted text-muted-foreground'
  return <span className={`rounded px-1.5 py-0.5 text-[9px] ${className}`}>{status}</span>
}

function Summary({ current, parent }: { current: DatasetRevisionSummary; parent: DatasetRevisionSummary | null }) {
  const facts: [string, string, string][] = [
    ['Rows', number(current.rowCount), parent ? delta(current.rowCount, parent.rowCount) : 'comparison unavailable'],
    ['Bytes', bytes(current.totalBytes), parent ? delta(current.totalBytes, parent.totalBytes) : 'comparison unavailable'],
    ['Data files', number(current.dataFileCount), parent ? delta(current.dataFileCount, parent.dataFileCount) : 'comparison unavailable'],
    ['Fragments', number(current.fragmentCount), parent ? delta(current.fragmentCount, parent.fragmentCount) : 'comparison unavailable'],
  ]
  return <div className="grid grid-cols-2 gap-1">
    {facts.map(([label, value, change]) => <div key={label} className="rounded bg-muted/40 px-2 py-1">
      <div className="text-[9px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="text-[10.5px] font-semibold text-foreground">{value}</div>
      <div className="text-[9px] text-muted-foreground">{change}</div>
    </div>)}
  </div>
}

function ExactPreview({ detail }: { detail: DatasetRevisionDetail }) {
  const { columns, rows, hasMore, rowLimit } = detail.preview
  return <div>
    <div className="mb-1 flex items-center justify-between gap-2 text-[10px] font-semibold text-foreground">
      <span>Exact revision preview</span>
      <span className="font-normal text-muted-foreground">{rows.length.toLocaleString()} rows</span>
    </div>
    {hasMore && <div className="mb-1 text-[9.5px] text-muted-foreground">Preview truncated at {rowLimit} rows; every row shown is still bound to this exact revision.</div>}
    {!rows.length ? <div className="rounded-md border border-border px-2 py-1.5 text-[10.5px] text-muted-foreground">This exact revision returned no preview rows.</div>
      : <div className="max-h-[220px] overflow-auto rounded-md border border-border">
        <table className="dp-mono w-max text-[9.5px]">
          <thead><tr>{columns.map((column) => <th key={column.name} className="sticky top-0 border-b border-border bg-muted px-2 py-1 text-left font-semibold">{column.name}</th>)}</tr></thead>
          <tbody>{rows.map((row, index) => <tr key={index}>{columns.map((column) => <td key={column.name} className="max-w-[180px] truncate whitespace-nowrap border-b border-border/40 px-2 py-0.5">{cell(row[column.name])}</td>)}</tr>)}</tbody>
        </table>
      </div>}
  </div>
}

const cell = (value: unknown) => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value)
