import { useCallback, useEffect, useRef, useState } from 'react'
import { api, KernelError } from '../api/client'
import { compareSchemas } from '../lib/schemaCompatibility'
import { useStore } from '../store/graph'
import type {
  CatalogTable, DatasetRevision, DatasetRevisionDetail, DatasetRevisionSummary, DatasetViewDefinition,
  RestoreRevisionTask, SchemaCompatibilityStatus,
} from '../types/api'
import { Icon } from '../ui/Icon'
import { FieldEvidenceButton } from '../components/FieldEvidenceDetail'

const PAGE_SIZE = 20
const MAX_RESERVOIR_SEED = 2_147_483_647
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

export function DatasetRevisionHistory({ table, initialRevisionId, initialRevisionDatasetId }: { table: CatalogTable; initialRevisionId?: string; initialRevisionDatasetId?: string }) {
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
  const [saveDetail, setSaveDetail] = useState<DatasetRevisionDetail | null>(null)
  const [restoreDetail, setRestoreDetail] = useState<DatasetRevisionDetail | null>(null)
  const [canSaveView, setCanSaveView] = useState(false)
  const historyRequest = useRef(0)
  const capabilityRequest = useRef(0)
  const detailRequest = useRef(0)

  const loadFirst = useCallback(async () => {
    const request = ++historyRequest.current
    const capability = ++capabilityRequest.current
    setAvailability('checking'); setHistoryError(null); setLoadMoreError(null); setCanSaveView(false)
    const capabilities = api.datasetRevisionCapabilities(table.id).catch(() => null)
    try {
      const page = await api.datasetRevisions(table.id, { limit: PAGE_SIZE })
      if (request !== historyRequest.current) return
      setItems(page.items); setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
      setAvailability('supported')
      const advertised = await capabilities
      if (capability === capabilityRequest.current) {
        setCanSaveView(advertised?.datasetViewSave === true)
      }
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
    return () => {
      historyRequest.current += 1
      capabilityRequest.current += 1
      detailRequest.current += 1
    }
  }, [loadFirst])

  useEffect(() => {
    if (!initialRevisionId || !initialRevisionDatasetId) return
    // The route supplies only a stable dataset/revision identity. Opening it goes through the
    // same exact-revision reader as a click in history; a missing/compacted revision stays honest.
    void openRevision({ datasetId: initialRevisionDatasetId, revisionId: initialRevisionId, retentionOwner: 'core' })
  // openRevision is defined below as a stable callback; the requested identity is the fence.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [table.id, table.registrationId, initialRevisionId, initialRevisionDatasetId])

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
        error={detailError} parentError={parentError} onRetry={() => void openRevision(selected)}
        canSave={canSaveView} onSave={setSaveDetail} headRevisionId={items[0]?.revisionId ?? null}
        onRestore={setRestoreDetail} />}
    </>}
    {saveDetail && <SaveDatasetViewDialog table={table} detail={saveDetail} onClose={() => setSaveDetail(null)} />}
    {restoreDetail && <RestoreRevisionDialog detail={restoreDetail} headRevisionId={items[0]?.revisionId ?? ''}
      onClose={() => setRestoreDetail(null)}
      onRestored={(child) => {
        setRestoreDetail(null)
        void loadFirst()
        void openRevision({ datasetId: child.sourceDatasetId, revisionId: child.childRevisionId!,
          committedAt: null, retentionOwner: 'core' })
      }} />}
  </section>
}

function HistoryFailure({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <div role="alert" className="flex items-center justify-between gap-2 text-[11px] text-destructive">
    <span>{message}</span><button onClick={() => void onRetry()} className="shrink-0 font-semibold underline">Retry</button>
  </div>
}

function RevisionDetail({ revision, detail, parent, loading, error, parentError, onRetry, canSave, onSave,
  headRevisionId, onRestore }: {
  revision: DatasetRevision; detail: DatasetRevisionDetail | null; parent: DatasetRevisionDetail | null
  loading: boolean; error: string | null; parentError: string | null; onRetry: () => void
  canSave: boolean
  onSave: (detail: DatasetRevisionDetail) => void
  headRevisionId: string | null
  onRestore: (detail: DatasetRevisionDetail) => void
}) {
  if (loading) return <div role="status" className="rounded-md bg-muted/40 px-2 py-2 text-[11px] text-muted-foreground">Opening exact revision {revision.revisionId}…</div>
  if (error) return <HistoryFailure message={error} onRetry={onRetry} />
  if (!detail) return null
  const compatibility = parent ? compareSchemas(parent.preview.columns, detail.preview.columns) : null
  const notableFields = compatibility?.fields.filter((field) => field.kind !== 'unchanged' || field.status !== 'compatible') ?? []
  return <div className="flex flex-col gap-2 border-t border-border pt-2" data-testid="revision-detail">
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
      <div className="dp-mono break-all text-[10.5px] font-semibold text-foreground">Exact revision {detail.revisionId}</div>
      <div className="text-[9.5px] text-muted-foreground">Dataset {detail.datasetId} · {timestamp(detail.committedAt)} · retained by {detail.retentionOwner}</div>
      <div className="text-[9.5px] text-muted-foreground">Parent {detail.parentRevisionId ?? 'not evidenced'} · producer {detail.producerOperation ?? 'not provided'}</div>
      </div>
      <div className="flex shrink-0 flex-col gap-1">
        {canSave && <button type="button" onClick={() => onSave(detail)}
          className="rounded-md border border-border bg-card px-2 py-1 text-[10.5px] font-semibold text-foreground hover:bg-accent">
          Save view
        </button>}
        {detail.retentionOwner === 'core' && detail.revisionId !== headRevisionId
          && <button type="button" data-testid="restore-revision" onClick={() => onRestore(detail)}
            className="rounded-md border border-border bg-card px-2 py-1 text-[10.5px] font-semibold text-foreground hover:bg-accent">
            Restore as new revision
          </button>}
      </div>
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
    {!columns.length ? <div className="rounded-md border border-border px-2 py-1.5 text-[10.5px] text-muted-foreground">This exact revision supplied no columns.</div>
      : <div className="max-h-[220px] overflow-auto rounded-md border border-border">
        <table className="dp-mono w-max text-[9.5px]">
          <thead><tr>{columns.map((column) => <th key={column.name} className="sticky top-0 border-b border-border bg-muted px-2 py-1 text-left font-semibold"><FieldEvidenceButton column={column} marker className="dp-mono rounded px-0.5 hover:bg-accent" /></th>)}</tr></thead>
          <tbody>{rows.map((row, index) => <tr key={index}>{columns.map((column) => <td key={column.name} className="max-w-[180px] truncate whitespace-nowrap border-b border-border/40 px-2 py-0.5">{cell(row[column.name])}</td>)}</tr>)}</tbody>
        </table>
        {!rows.length && <div className="border-t border-border px-2 py-1.5 text-[10.5px] text-muted-foreground">This exact revision returned no preview rows; its retained schema remains inspectable above.</div>}
      </div>}
  </div>
}

const cell = (value: unknown) => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value)

function SaveDatasetViewDialog({ table, detail, onClose }: {
  table: CatalogTable; detail: DatasetRevisionDetail; onClose: () => void
}) {
  const pushToast = useStore((state) => state.pushToast)
  const setWorkspaceResource = useStore((state) => state.setWorkspaceResource)
  const switchWorkspaceScope = useStore((state) => state.switchWorkspaceScope)
  const columns = detail.preview.columns.map((column) => column.name)
  const [name, setName] = useState(`${table.name} view`)
  const [selected, setSelected] = useState(columns)
  const [predicate, setPredicate] = useState('')
  const [sampling, setSampling] = useState<'all' | 'reservoir'>('all')
  const [size, setSize] = useState('1000')
  const [seed, setSeed] = useState('42')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submission = useRef({ fingerprint: '', id: '' })
  const generation = useRef(0)

  useEffect(() => () => { generation.current += 1 }, [])

  const toggle = (column: string) => setSelected((current) => current.includes(column)
    ? current.filter((item) => item !== column)
    : columns.filter((item) => item === column || current.includes(item)))

  const submit = async () => {
    const normalizedName = name.trim()
    const normalizedPredicate = predicate.trim() || null
    const sampleSize = Number(size)
    const sampleSeed = Number(seed)
    if (!normalizedName || !selected.length || busy) return
    if (sampling === 'reservoir' && (!Number.isInteger(sampleSize) || sampleSize < 1 || sampleSize > 100_000
      || !Number.isInteger(sampleSeed) || sampleSeed < 0 || sampleSeed > MAX_RESERVOIR_SEED)) {
      setError('Reservoir size must be 1–100,000 and seed must be between 0 and 2,147,483,647.')
      return
    }
    const fingerprint = JSON.stringify({
      name: normalizedName,
      datasetId: detail.datasetId,
      revisionId: detail.revisionId,
      selected,
      predicate: normalizedPredicate,
      sampling: sampling === 'all' ? { kind: 'all' } : { kind: 'reservoir', size: sampleSize, seed: sampleSeed },
    })
    if (submission.current.fingerprint !== fingerprint) {
      submission.current = { fingerprint, id: crypto.randomUUID() }
    }
    const request = ++generation.current
    setBusy(true); setError(null)
    try {
      const created: DatasetViewDefinition = await api.createDatasetView({
        submissionId: submission.current.id,
        name: normalizedName,
        datasetRef: {
          kind: 'exact', datasetId: detail.datasetId, revisionId: detail.revisionId,
          lastKnown: detail.committedAt ? { committedAt: detail.committedAt } : null,
        },
        selectedColumns: selected,
        predicate: normalizedPredicate,
        sampling: sampling === 'all'
          ? { kind: 'all' }
          : { kind: 'reservoir', size: sampleSize, seed: sampleSeed },
      })
      if (request !== generation.current) return
      submission.current = { fingerprint: '', id: '' }
      pushToast(`Saved “${created.name}” beside its source in Workspace`, 'success')
      onClose()
      switchWorkspaceScope('all')
      setWorkspaceResource(`dataset_view:${created.id}`)
    } catch (caught) {
      if (request === generation.current) setError(errorMessage(caught))
    } finally {
      if (request === generation.current) setBusy(false)
    }
  }

  return <div className="fixed inset-0 z-50 grid place-items-center bg-black/30 p-4" onClick={() => { if (!busy) onClose() }}>
    <div role="dialog" aria-modal="true" aria-label="Save exact revision as view" aria-busy={busy}
      className="flex max-h-[90vh] w-[560px] max-w-full flex-col gap-3 overflow-hidden rounded-xl border border-border bg-card p-5 shadow-xl"
      onClick={(event) => event.stopPropagation()}>
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1"><h2 className="text-[15px] font-bold">Save exact revision as view</h2>
          <p className="truncate text-[10.5px] text-muted-foreground">{table.name} · revision {detail.revisionId}</p></div>
        <button onClick={onClose} disabled={busy} aria-label="Close save view dialog" className="disabled:opacity-40"><Icon name="close" size={15} /></button>
      </div>
      <div className="min-h-0 overflow-y-auto pr-1">
        <div className="grid gap-3">
          <label className="grid gap-1 text-[11px] font-semibold text-foreground">Name
            <input value={name} disabled={busy} onChange={(event) => setName(event.target.value)} className="dp-input font-normal" />
          </label>
          <fieldset disabled={busy} className="grid gap-1.5">
            <legend className="text-[11px] font-semibold text-foreground">Columns in view</legend>
            <div className="grid max-h-36 grid-cols-2 gap-x-3 gap-y-1 overflow-y-auto rounded-md border border-border p-2">
              {columns.map((column) => <label key={column} className="flex min-w-0 items-center gap-1.5 text-[10.5px] text-foreground">
                <input type="checkbox" checked={selected.includes(column)} onChange={() => toggle(column)} />
                <span className="truncate" title={column}>{column}</span>
              </label>)}
            </div>
            <span className="text-[10px] text-muted-foreground">Output order follows the exact revision schema. Choose at least one column.</span>
          </fieldset>
          <label className="grid gap-1 text-[11px] font-semibold text-foreground">Row predicate <span className="font-normal text-muted-foreground">(optional DuckDB expression)</span>
            <textarea value={predicate} disabled={busy} rows={3} onChange={(event) => setPredicate(event.target.value)}
              placeholder={"status = 'ready' AND score >= 0.8"} className="dp-input resize-y font-mono font-normal" />
            <span className="text-[10px] font-normal text-muted-foreground">Evaluated against the exact source before columns are projected.</span>
          </label>
          <fieldset disabled={busy} className="grid gap-2">
            <legend className="text-[11px] font-semibold text-foreground">Rows</legend>
            <label className="flex items-start gap-2 rounded-md border border-border p-2 text-[11px]"><input type="radio" name="view-sampling" checked={sampling === 'all'} onChange={() => setSampling('all')} />
              <span><strong>All matching rows</strong><span className="block text-[10px] text-muted-foreground">The definition stays lazy; previews remain bounded to 100 rows.</span></span></label>
            <label className="flex items-start gap-2 rounded-md border border-border p-2 text-[11px]"><input type="radio" name="view-sampling" checked={sampling === 'reservoir'} onChange={() => setSampling('reservoir')} />
              <span className="min-w-0 flex-1"><strong>Deterministic reservoir</strong><span className="block text-[10px] text-muted-foreground">Saving scans the complete filtered local revision to establish evidence. Each preview replays that full scan; the rows are not materialized.</span>
                {sampling === 'reservoir' && <span className="mt-2 grid grid-cols-2 gap-2">
                  <label className="grid gap-1 text-[10px] text-muted-foreground">Rows<input aria-label="Reservoir rows" type="number" min="1" max="100000" value={size} onChange={(event) => setSize(event.target.value)} className="dp-input text-foreground" /></label>
                  <label className="grid gap-1 text-[10px] text-muted-foreground">Seed<input aria-label="Reservoir seed" type="number" min="0" max={MAX_RESERVOIR_SEED} value={seed} onChange={(event) => setSeed(event.target.value)} className="dp-input text-foreground" /></label>
                </span>}
              </span></label>
          </fieldset>
        </div>
      </div>
      {error && <div role="alert" className="text-[11px] text-destructive">Couldn't save this view: {error}</div>}
      <div className="flex justify-end gap-2">
        <button onClick={onClose} disabled={busy} className="rounded-md border border-border px-3 py-1.5 text-[12px] disabled:opacity-50">Cancel</button>
        <button onClick={() => void submit()} disabled={busy || !name.trim() || !selected.length}
          className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Saving exact view…' : 'Save view'}</button>
      </div>
    </div>
  </div>
}

const restoreFailure = (task: RestoreRevisionTask) => task.diagnosticCode === 'stale_expected_head'
  ? 'The current head changed before this restore published. Reload the history and try again.'
  : task.diagnosticCode === 'revision_unavailable'
    ? 'The source revision is no longer retained, so it cannot be restored.'
    : 'The restore did not publish. Reload the history and try again.'

function RestoreRevisionDialog({ detail, headRevisionId, onClose, onRestored }: {
  detail: DatasetRevisionDetail; headRevisionId: string
  onClose: () => void; onRestored: (task: RestoreRevisionTask) => void
}) {
  const pushToast = useStore((state) => state.pushToast)
  const [head, setHead] = useState<DatasetRevisionDetail | null>(null)
  const [headError, setHeadError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submission = useRef('')
  const generation = useRef(0)

  useEffect(() => {
    const request = ++generation.current
    api.datasetRevision(detail.datasetId, headRevisionId)
      .then((next) => { if (request === generation.current) setHead(next) })
      .catch((caught) => { if (request === generation.current) setHeadError(errorMessage(caught)) })
    return () => { generation.current += 1 }
  }, [detail.datasetId, headRevisionId])

  const submit = async () => {
    if (busy) return
    if (!submission.current) submission.current = crypto.randomUUID()
    const request = ++generation.current
    setBusy(true); setError(null)
    try {
      let task = await api.restoreRevision(detail.datasetId, detail.revisionId,
        { submissionId: submission.current, expectedHeadRevisionId: headRevisionId })
      while (task.status !== 'done' && task.status !== 'failed' && task.status !== 'cancelled') {
        await new Promise((resolve) => setTimeout(resolve, 400))
        if (request !== generation.current) return
        task = await api.restoreRevisionTask(task.taskId)
      }
      if (request !== generation.current) return
      if (task.status === 'done' && task.childRevisionId) {
        submission.current = ''
        pushToast(`Published revision ${task.childRevisionId} from the restored source`, 'success')
        onRestored(task)
        return
      }
      setError(task.status === 'failed' ? restoreFailure(task) : 'The restore was cancelled.')
    } catch (caught) {
      if (request !== generation.current) return
      const status = statusOf(caught)
      setError(status === 409
        ? 'The current head changed. Reload the history and try again.'
        : status === 410 ? 'The source revision is no longer retained, so it cannot be restored.'
          : `Couldn't restore this revision: ${errorMessage(caught)}`)
    } finally {
      if (request === generation.current) setBusy(false)
    }
  }

  return <div className="fixed inset-0 z-50 grid place-items-center bg-black/30 p-4" onClick={() => { if (!busy) onClose() }}>
    <div role="dialog" aria-modal="true" aria-label="Restore revision as new head" aria-busy={busy}
      className="flex max-h-[90vh] w-[520px] max-w-full flex-col gap-3 overflow-hidden rounded-xl border border-border bg-card p-5 shadow-xl"
      onClick={(event) => event.stopPropagation()}>
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1"><h2 className="text-[15px] font-bold">Restore revision as new head</h2>
          <p className="text-[10.5px] text-muted-foreground">Publishes the contents of an old revision as a new current revision. History stays append-only.</p></div>
        <button onClick={onClose} disabled={busy} aria-label="Close restore dialog" className="disabled:opacity-40"><Icon name="close" size={15} /></button>
      </div>
      <div className="grid gap-2">
        <div className="rounded-md border border-border p-2">
          <div className="text-[9px] uppercase tracking-wide text-muted-foreground">Restoring this source</div>
          <div className="dp-mono break-all text-[10.5px] font-semibold text-foreground">{detail.revisionId}</div>
          <div className="text-[9.5px] text-muted-foreground">{number(detail.summary.rowCount)} rows · {bytes(detail.summary.totalBytes)} · {timestamp(detail.committedAt)}</div>
        </div>
        <div className="rounded-md border border-border p-2">
          <div className="text-[9px] uppercase tracking-wide text-muted-foreground">Over the current head</div>
          {headError ? <div role="alert" className="text-[10.5px] text-destructive">Couldn't load the current head: {headError}</div>
            : !head ? <div role="status" className="text-[10.5px] text-muted-foreground">Loading current head…</div>
              : <><div className="dp-mono break-all text-[10.5px] font-semibold text-foreground">{head.revisionId}</div>
                <div className="text-[9.5px] text-muted-foreground">{number(head.summary.rowCount)} rows · {bytes(head.summary.totalBytes)} · {timestamp(head.committedAt)}</div></>}
        </div>
      </div>
      {error && <div role="alert" className="text-[11px] text-destructive">{error}</div>}
      <div className="flex justify-end gap-2">
        <button onClick={onClose} disabled={busy} className="rounded-md border border-border px-3 py-1.5 text-[12px] disabled:opacity-50">Cancel</button>
        <button onClick={() => void submit()} disabled={busy || !!headError} data-testid="restore-revision-confirm"
          className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Restoring…' : 'Restore as new head'}</button>
      </div>
    </div>
  </div>
}
