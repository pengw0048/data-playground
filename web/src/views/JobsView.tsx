import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type CanvasFile, type DatasetTaskKind, type WorkspaceJobDto, type WorkspaceJobsQuery } from '../api/client'
import type { WriteReceipt } from '../types/api'
import { datasetViewerHash, routeHash } from '../router'
import { useStore } from '../store/graph'
import { status as statusTok } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { FullResult } from '../panels/DataPanel'
import { fmtMs } from '../panels/RunHistoryModal'
import { CanvasCopyModal, type CanvasCopySource } from '../panels/CanvasCopyModal'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { DistributionReportPage } from './DistributionReports'

const PAGE_SIZE = 50
const STATUSES = ['', 'queued', 'running', 'done', 'failed', 'cancelled'] as const
const RECENT_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
const SELECTION_QUERY_KEYS = ['run', 'output', 'report', 'compare'] as const

function queryFrom(params: URLSearchParams, cursor?: string): WorkspaceJobsQuery {
  const status = params.get('status')
  return {
    limit: PAGE_SIZE, cursor,
    status: STATUSES.includes(status as typeof STATUSES[number]) && status
      ? status as Exclude<typeof STATUSES[number], ''> : undefined,
    canvasId: params.get('canvas') || undefined,
    nodeId: params.get('node') || undefined,
    backend: params.get('backend') || undefined,
    after: params.get('after') || undefined,
    before: params.get('before') || undefined,
    q: params.get('q') || undefined,
  }
}

const localDate = (value: string | null) => {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}
const isoDate = (value: string) => value ? new Date(value).toISOString() : ''
const outputKey = (nodeId: string, portId: string) => `${nodeId}:${portId}`
const jobKey = (job: WorkspaceJobDto) => job.runId ?? job.id
const readable = (value: string) => value.replaceAll('_', ' ')
const progressLabel = (progress: number | null | undefined) => (
  progress == null ? 'Unavailable' : `${Math.round(progress * 100)}%`
)
const rowLabel = (rows: number) => `${rows.toLocaleString()} row${rows === 1 ? '' : 's'}`
const updateLabel = (updatedAt: string | null | undefined) => (
  updatedAt ? new Date(updatedAt).toLocaleString() : 'Unavailable'
)
const refreshLabel = (refreshedAt: number) => new Date(refreshedAt).toLocaleTimeString()

type QuickView = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'recent' | 'all'

function clearSelectionParams(params: URLSearchParams) {
  for (const key of SELECTION_QUERY_KEYS) params.delete(key)
}

function hasOrdinaryFilters(params: URLSearchParams): boolean {
  return ['status', 'canvas', 'node', 'backend', 'after', 'before', 'q']
    .some((key) => Boolean(params.get(key)))
}

function selectedQuickView(params: URLSearchParams): QuickView | null {
  const status = params.get('status')
  if (status === 'queued' || status === 'running' || status === 'done' || status === 'failed' || status === 'cancelled') return status
  if (!status && params.get('after') && !params.get('before')) return 'recent'
  if (!status && !params.get('after') && !params.get('before')) return 'all'
  return null
}

const DATASET_TASK_LABELS: Record<DatasetTaskKind, string> = {
  restore_revision_write: 'Dataset restore',
  keyed_upsert_write: 'Keyed upsert',
  merge_columns_write: 'Column merge',
}

function jobStep(item: WorkspaceJobDto): string | null {
  if (item.mergeColumns) return item.mergeColumns.phase === 'failed' ? 'Column merge failed' : 'Merging columns'
  if (item.externalWait) {
    if (item.externalWait.phase === 'downloading') return 'Downloading data'
    return `Waiting for external work · ${readable(item.externalWait.phase)}`
  }
  if (item.checkpoint) {
    return item.checkpoint.phase === 'materializing'
      ? 'Saving result for reuse'
      : `Preparing reusable result · ${readable(item.checkpoint.phase)}`
  }
  if (item.boundedFanout) return 'Processing partitions'
  return null
}

function managedWriteRevisionReceipt(
  item: WorkspaceJobDto,
  committed: WorkspaceJobDto['outputs'],
): WriteReceipt | null {
  const receipt = item.outputReceipt
    ?? committed.find((output) => output.writeReceipt)?.writeReceipt
  return receipt && (
    receipt.publication.provider === 'managed-local-file'
    || receipt.publication.provider === 'managed-local-lance'
  ) ? receipt : null
}

export function JobsView() {
  const jobsQuery = useStore((state) => state.jobsQuery)
  const setJobsQuery = useStore((state) => state.setJobsQuery)
  const canvases = useStore((state) => state.files)
  const refreshFiles = useStore((state) => state.refreshFiles)
  const params = useMemo(() => new URLSearchParams(jobsQuery), [jobsQuery])
  const filterKey = useMemo(() => {
    const copy = new URLSearchParams(params)
    clearSelectionParams(copy)
    return copy.toString()
  }, [params])
  const [items, setItems] = useState<WorkspaceJobDto[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const [refreshError, setRefreshError] = useState('')
  const [loadMoreError, setLoadMoreError] = useState('')
  const [loadedMore, setLoadedMore] = useState(false)
  const [hasActiveFirstPage, setHasActiveFirstPage] = useState(false)
  const [lastSuccessfulRefresh, setLastSuccessfulRefresh] = useState<number | null>(null)
  const [actionError, setActionError] = useState('')
  const [acting, setActing] = useState('')
  const [copySource, setCopySource] = useState<CanvasCopySource | null>(null)
  const [selectedRunUnavailable, setSelectedRunUnavailable] = useState(false)
  const [selectedRunLookupError, setSelectedRunLookupError] = useState('')
  const [selectedRunRetry, setSelectedRunRetry] = useState(0)
  const request = useRef(0)
  const deepLinkRequest = useRef('')
  const directInjectedJob = useRef<{ itemId: string; runId: string } | null>(null)
  const retryActions = useRef(new Map<string, string>())

  // Jobs only names canvases returned by the existing authorized list. Refresh it when entering the
  // view so a revoked share does not remain selectable after the rest of the shell has updated.
  useEffect(() => { void refreshFiles() }, [refreshFiles])

  const load = useCallback(async (nextCursor?: string, mode: 'initial' | 'refresh' = 'initial') => {
    const sequence = ++request.current
    if (nextCursor) {
      // Loading another keyset page commits this view to a bounded snapshot, even if the
      // request needs a retry. A background first-page response must not race that retry.
      setLoadingMore(true); setLoadMoreError(''); setLoadedMore(true)
    }
    else {
      setLoading(true); setError(''); setRefreshError(''); setLoadMoreError('')
      if (mode === 'initial') {
        setItems([]); setCursor(null); setHasMore(false); setLoadedMore(false); setHasActiveFirstPage(false); setLastSuccessfulRefresh(null)
        directInjectedJob.current = null
        deepLinkRequest.current = ''
        setSelectedRunUnavailable(false)
        setSelectedRunLookupError('')
      } else {
        // A selected run may have been injected from its direct link rather than the first page.
        // Recheck it after replacing the page so Refresh preserves that explicit selection.
        directInjectedJob.current = null
        deepLinkRequest.current = ''
        setSelectedRunUnavailable(false)
        setSelectedRunLookupError('')
      }
    }
    try {
      const page = await api.workspaceJobs(queryFrom(new URLSearchParams(filterKey), nextCursor))
      if (sequence !== request.current) return
      const visibleItems = page.items
      const injected = directInjectedJob.current
      if (injected && visibleItems.some((item) => item.id === injected.itemId)) {
        // Once an ordinary page contains the linked Job it is no longer a synthetic row.
        // Keep it when the user closes the direct link, even if the cursor has moved past it.
        directInjectedJob.current = null
      }
      if (!nextCursor) {
        setError('')
        setRefreshError('')
        setHasActiveFirstPage(visibleItems.some((item) => item.status === 'queued' || item.status === 'running'))
        setLastSuccessfulRefresh(Date.now())
        setLoadedMore(false)
      }
      setItems((current) => nextCursor
        ? [...current, ...visibleItems.filter((item) => !current.some((row) => row.id === item.id))]
        : visibleItems)
      setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
    } catch (caught) {
      if (sequence !== request.current) return
      const message = caught instanceof Error ? caught.message : String(caught)
      if (nextCursor) setLoadMoreError(message)
      else if (mode === 'initial') setError(message)
      else setRefreshError(message)
    } finally {
      if (sequence === request.current) { setLoading(false); setLoadingMore(false) }
    }
  }, [filterKey])

  useEffect(() => { void load(); return () => { request.current += 1 } }, [load])
  useEffect(() => {
    const runId = params.get('run')
    if (!runId) {
      const injected = directInjectedJob.current
      directInjectedJob.current = null
      if (injected) setItems((current) => current.filter((item) => item.id !== injected.itemId))
      deepLinkRequest.current = ''
      setSelectedRunUnavailable(false)
      setSelectedRunLookupError('')
      return
    }
    const previousInjected = directInjectedJob.current
    if (previousInjected && previousInjected.runId !== runId) {
      directInjectedJob.current = null
      setItems((current) => current.filter((item) => item.id !== previousInjected.itemId))
    }
    if (loading) return
    if (items.some((item) => jobKey(item) === runId)) {
      setSelectedRunUnavailable(false)
      setSelectedRunLookupError('')
      return
    }
    const key = `${filterKey}\u0000${runId}\u0000${selectedRunRetry}`
    if (deepLinkRequest.current === key) return
    deepLinkRequest.current = key
    setSelectedRunUnavailable(false)
    setSelectedRunLookupError('')
    let live = true
    // A copied Job URL identifies one exact run. Ordinary list filters describe the surrounding
    // page and must not make that selected run look missing when it no longer matches them.
    void api.workspaceJobs({ limit: 1, runId })
      .then((page) => {
        if (!live) return
        const exact = page.items.find((item) => jobKey(item) === runId)
        if (exact) {
          directInjectedJob.current = { itemId: exact.id, runId }
          setItems((current) => [exact, ...current.filter((item) => item.id !== exact.id)])
        }
        else setSelectedRunUnavailable(true)
      })
      .catch((caught) => {
        if (!live) return
        setSelectedRunLookupError(caught instanceof Error ? caught.message : String(caught))
      })
    return () => { live = false }
  }, [filterKey, items, loading, params, selectedRunRetry])
  useEffect(() => {
    if (loadedMore || !hasActiveFirstPage) return
    const timer = window.setInterval(() => { if (!loading && !loadingMore) void load(undefined, 'refresh') }, 5000)
    return () => window.clearInterval(timer)
  }, [hasActiveFirstPage, load, loadedMore, loading, loadingMore])

  const update = (name: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value); else next.delete(name)
    clearSelectionParams(next)
    setJobsQuery(next.toString())
  }
  const selectRun = (runId: string | null, output?: string) => {
    const next = new URLSearchParams(params)
    if (runId) next.set('run', runId); else next.delete('run')
    if (output) next.set('output', output); else next.delete('output')
    next.delete('report'); next.delete('compare')
    setJobsQuery(next.toString())
  }
  const clearSelection = () => {
    const next = new URLSearchParams(params)
    clearSelectionParams(next)
    setJobsQuery(next.toString())
  }
  const selectQuickView = (view: QuickView) => {
    const next = new URLSearchParams(params)
    if (view === 'queued' || view === 'running' || view === 'done' || view === 'failed' || view === 'cancelled') {
      next.set('status', view)
      next.delete('after'); next.delete('before')
    } else if (view === 'recent') {
      next.delete('status'); next.delete('before')
      next.set('after', new Date(Date.now() - RECENT_WINDOW_MS).toISOString())
    } else {
      next.delete('status'); next.delete('after'); next.delete('before')
    }
    clearSelectionParams(next)
    setJobsQuery(next.toString())
  }
  const selected = items.find((item) => jobKey(item) === params.get('run'))
  const outputParam = params.get('output')
  const selectedOutput = selected?.outputs.find((output) =>
    outputKey(output.nodeId, output.portId) === outputParam)
  const checkpointOutput = (
    selected?.checkpoint
    && outputParam === outputKey(selected.checkpoint.clientKey, selected.checkpoint.outputPortId)
  ) ? selected.checkpoint : null
  const act = async (item: WorkspaceJobDto, action: 'cancel' | 'retry') => {
    const runId = item.runId ?? item.id
    setActing(`${runId}:${action}`); setActionError('')
    try {
      if (item.mergeColumns && item.taskId) {
        const managed = item.mergeColumns.producerKind === 'managed-sidecar'
        if (action === 'cancel') {
          if (managed) await api.cancelManagedSidecarMergeTask(item.taskId)
          else await api.cancelMergeColumnsTask(item.taskId)
        }
        else {
          const actionId = retryActions.current.get(runId) ?? globalThis.crypto.randomUUID()
          retryActions.current.set(runId, actionId)
          if (managed) await api.retryManagedSidecarMergeTask(item.taskId, actionId)
          else await api.retryMergeColumnsTask(item.taskId, actionId)
          retryActions.current.delete(runId)
        }
      } else if (action === 'cancel') await api.cancelRun(runId)
      else {
        const actionId = retryActions.current.get(runId) ?? globalThis.crypto.randomUUID()
        retryActions.current.set(runId, actionId)
        await api.retryRun(runId, actionId)
        retryActions.current.delete(runId)
      }
      await load(undefined, 'refresh')
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : String(caught))
    } finally { setActing('') }
  }
  const selectCanvas = (value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set('canvas', value); else next.delete('canvas')
    // A node identity is scoped to its canvas. Do not leave an invisible stale node filter
    // behind when choosing a different canvas (or returning to all accessible canvases).
    next.delete('node')
    clearSelectionParams(next)
    setJobsQuery(next.toString())
  }
  const freshness = lastSuccessfulRefresh == null
    ? null
    : refreshError
      ? loadedMore
        ? `Refresh failed · Showing loaded job history · Auto-refresh paused · Updated ${refreshLabel(lastSuccessfulRefresh)}`
        : `Refresh failed · Updated ${refreshLabel(lastSuccessfulRefresh)}`
      : loadedMore
        ? `Loaded job history · Auto-refresh paused · Updated ${refreshLabel(lastSuccessfulRefresh)}`
        : hasActiveFirstPage
          ? `Active jobs refresh automatically · Updated ${refreshLabel(lastSuccessfulRefresh)}`
          : `No active jobs · Updated ${refreshLabel(lastSuccessfulRefresh)}`
  const quickView = selectedQuickView(params)
  const ordinaryFilters = hasOrdinaryFilters(params)

  const reportId = params.get('report')
  if (reportId) {
    const next = new URLSearchParams(params)
    next.delete('report'); next.delete('compare')
    const backHref = routeHash('jobs', undefined, undefined, undefined, next.toString())
    return <DistributionReportPage reportId={reportId} compareReportId={params.get('compare') || undefined}
      backHref={backHref} onClose={() => setJobsQuery(next.toString())} />
  }

  return (
    <div className="flex h-full min-w-0 flex-col">
      <header className="flex min-h-[68px] flex-wrap items-center gap-3 border-b border-border px-4 py-3 sm:px-7">
        <div><h1 className="text-[20px] font-bold text-foreground">Jobs</h1>
          <p className="text-[11.5px] text-muted-foreground">{items.length || ordinaryFilters ? freshness ?? 'Persisted runs across accessible canvases' : 'Progress and results across accessible canvases'}</p></div>
        <span className="flex-1" />
        <Button variant="outline" size="sm" onClick={() => void load(undefined, 'refresh')} disabled={loading || loadingMore}>
          <Icon name="refresh" size={13} /> Refresh
        </Button>
      </header>

      <section aria-label="Job quick views" className="flex flex-wrap items-center gap-2 border-b border-border bg-card/60 px-4 py-3 sm:px-7">
        <span className="mr-1 text-[11.5px] text-muted-foreground">Show</span>
        {([
          ['queued', 'Queued'], ['running', 'Running'], ['done', 'Completed'], ['failed', 'Failed'], ['cancelled', 'Cancelled'], ['recent', 'Recent'], ['all', 'All'],
        ] as [QuickView, string][]).map(([view, label]) => <Button key={view} size="sm" variant={quickView === view ? 'default' : 'outline'} onClick={() => selectQuickView(view)}>{label}</Button>)}
      </section>

      <section aria-label="Job filters" className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,12rem),1fr))] gap-2 border-b border-border bg-card/30 px-4 py-3 text-[11.5px] xl:px-7">
        <CanvasSelector canvases={canvases} value={params.get('canvas') ?? ''} onChange={selectCanvas} />
        <Filter label="Canvas step ID" name="node" value={params.get('node') ?? ''} onChange={update} placeholder="Any step" />
        <Filter label="Run mode" name="backend" value={params.get('backend') ?? ''} onChange={update} placeholder="Any mode" />
        <label className="grid min-w-0 gap-1 text-[10.5px] text-muted-foreground">From
          <input aria-label="Filter jobs from time" type="datetime-local" value={localDate(params.get('after'))} onChange={(event) => update('after', isoDate(event.target.value))} className="h-8 min-w-0 w-full rounded-md border border-border bg-background px-2 text-[12px] text-foreground" /></label>
        <label className="grid min-w-0 gap-1 text-[10.5px] text-muted-foreground">To
          <input aria-label="Filter jobs to time" type="datetime-local" value={localDate(params.get('before'))} onChange={(event) => update('before', isoDate(event.target.value))} className="h-8 min-w-0 w-full rounded-md border border-border bg-background px-2 text-[12px] text-foreground" /></label>
        <Filter label="Text" name="q" value={params.get('q') ?? ''} onChange={update} placeholder="Run, canvas, failure…" />
      </section>

      <div className="min-h-0 flex-1 overflow-auto px-3 py-3 sm:px-7">
        {actionError && <div role="alert" className="mb-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-[12px] text-destructive">Job action failed: {actionError}</div>}
        {loading && <div className="p-5 text-[12.5px] text-muted-foreground">Loading Jobs…</div>}
        {!loading && error && <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-4 text-[12.5px] text-destructive">Couldn’t load Jobs: {error} <button className="ml-2 font-semibold underline" onClick={() => void load()}>Retry</button></div>}
        {!loading && refreshError && <div role="alert" className="mb-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-[12px] text-destructive">Couldn’t refresh Jobs: {refreshError} Showing previously loaded jobs.</div>}
        {!loading && !error && selectedRunLookupError && <div role="alert" className="mb-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-[12px] text-destructive">
          Couldn’t open the linked Job: {selectedRunLookupError}. The Jobs list below is still available.
          <Button variant="outline" size="sm" className="ml-2" onClick={() => setSelectedRunRetry((attempt) => attempt + 1)}>Retry</Button>
          <Button variant="outline" size="sm" className="ml-2" onClick={clearSelection}>Back to Jobs</Button>
        </div>}
        {!loading && !error && selectedRunUnavailable && <div className="mb-3 rounded-lg border border-border bg-card p-5 text-center text-[12.5px] text-muted-foreground"><p>This Job is unavailable or you no longer have access.</p><Button variant="outline" size="sm" className="mt-3" onClick={clearSelection}>Back to Jobs</Button></div>}
        {!loading && !error && items.length === 0 && !selectedRunUnavailable && !selectedRunLookupError && <div className="rounded-lg border border-dashed border-border p-8 text-center text-[12.5px] text-muted-foreground">{ordinaryFilters ? 'No Jobs match these filters.' : 'No Jobs yet. Run a Canvas to see its progress and results here.'}</div>}
        {items.length > 0 && <section aria-labelledby="jobs-list-heading">
          <h2 id="jobs-list-heading" className="mb-2 text-[12px] font-semibold text-foreground">Runs and background tasks</h2>
          <div className="min-w-0 overflow-hidden rounded-lg border border-border bg-card">
            <div className="grid grid-cols-[88px_minmax(0,1fr)_minmax(110px,1fr)] gap-2 border-b border-border bg-muted/40 px-3 py-2 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground md:grid-cols-[96px_minmax(150px,1.3fr)_minmax(130px,1fr)_100px_140px] md:gap-3 xl:grid-cols-[108px_minmax(190px,1.3fr)_minmax(150px,1fr)_120px_100px_150px]">
              <span>State</span><span>Context</span><span>Outcome</span><span className="hidden md:block">Duration</span><span className="hidden xl:block">Backend</span><span className="hidden md:block">Recorded</span>
            </div>
            {items.map((item) => <JobRow key={item.id} item={item} expanded={selected?.id === item.id} onSelect={() => selectRun(selected?.id === item.id ? null : item.runId ?? item.id)} onOutput={(key) => selectRun(item.runId ?? item.id, key)} selectedOutput={params.get('output')} onAction={(action) => void act(item, action)} acting={acting.startsWith(`${item.runId ?? item.id}:`)} onClone={item.canvasId ? () => setCopySource({ canvasId: item.canvasId!, subjectId: item.id, name: item.canvasName || 'Untitled canvas' }) : undefined} returnQuery={jobsQuery} />)}
          </div>
        </section>}
        {loadMoreError && <div role="alert" className="mt-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-[12px] text-destructive">Couldn’t load more Jobs: {loadMoreError} <button className="ml-2 font-semibold underline" onClick={() => cursor && void load(cursor)}>Retry load more</button></div>}
        {hasMore && !loadMoreError && <Button variant="outline" className="mt-3 w-full" disabled={loadingMore || !cursor} onClick={() => cursor && void load(cursor)}>{loadingMore ? 'Loading…' : 'Load more'}</Button>}
      </div>

      {selected && selectedOutput?.outcome === 'committed' && selectedOutput.uri && (
        <aside aria-label="Saved result" className="max-h-[45vh] overflow-auto border-t border-border bg-card">
          <div className="flex items-center border-b border-border px-4 py-2 text-[12px] font-semibold">Saved result<span className="flex-1" /><button aria-label="Close saved result" onClick={() => selectRun(selected.runId ?? selected.id)}><Icon name="close" size={14} /></button></div>
          <FullResult uri={selectedOutput.uri} total={selectedOutput.rows ?? null} runId={selected.runId ?? undefined} nodeId={selectedOutput.nodeId} portId={selectedOutput.portId} publicationKind={selectedOutput.publicationKind} name={selectedOutput.table ?? selectedOutput.portLabel ?? selectedOutput.portId} />
        </aside>
      )}
      {selected && checkpointOutput && (
        <aside aria-label="Saved checkpoint" className="max-h-[45vh] overflow-auto border-t border-border bg-card">
          <div className="flex items-center border-b border-border px-4 py-2 text-[12px] font-semibold">Saved checkpoint · {checkpointOutput.checkpointNodeId}<span className="flex-1" /><button aria-label="Close saved checkpoint" onClick={() => selectRun(selected.runId ?? selected.id)}><Icon name="close" size={14} /></button></div>
          <FullResult uri={checkpointOutput.clientKey} total={checkpointOutput.rows ?? null} runId={selected.runId ?? undefined} nodeId={checkpointOutput.clientKey} portId={checkpointOutput.outputPortId} publicationKind="result" name={checkpointOutput.checkpointNodeId} />
        </aside>
      )}
      {copySource && <CanvasCopyModal source={copySource} onClose={() => setCopySource(null)} />}
    </div>
  )
}

function CanvasSelector({ canvases, value, onChange }: { canvases: CanvasFile[]; value: string; onChange: (value: string) => void }) {
  const listed = canvases.some((canvas) => canvas.id === value)
  return <label className="grid min-w-0 gap-1 text-[10.5px] text-muted-foreground">Canvas
    <select aria-label="Filter jobs by canvas" value={value} onChange={(event) => onChange(event.target.value)} className="h-8 min-w-0 w-full rounded-md border border-border bg-background px-2 text-[12px] text-foreground">
      <option value="">All accessible canvases</option>
      {!listed && value && <option value={value}>Canvas ID from link: {value}</option>}
      {canvases.map((canvas) => <option key={canvas.id} value={canvas.id}>{canvasLabel(canvas)}</option>)}
    </select></label>
}

function canvasLabel(canvas: CanvasFile): string {
  return `${canvas.name || 'Untitled canvas'} · ${canvas.id}`
}

function Filter({ label, name, value, onChange, placeholder }: { label: string; name: string; value: string; onChange: (name: string, value: string) => void; placeholder?: string }) {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])
  return <label className="grid min-w-0 gap-1 text-[10.5px] text-muted-foreground">{label}<input aria-label={`Filter jobs by ${label.toLowerCase()}`} value={draft} placeholder={placeholder} onChange={(event) => setDraft(event.target.value)} onBlur={() => onChange(name, draft.trim())} onKeyDown={(event) => { if (event.key === 'Enter') onChange(name, draft.trim()) }} className="h-8 min-w-0 w-full rounded-md border border-border bg-background px-2 text-[12px] text-foreground" /></label>
}

const JOB_SUBJECT_SUFFIX_LENGTH = 18
const jobSubjectSegmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' })

function JobSubject({ name }: { name: string }) {
  const graphemes = Array.from(jobSubjectSegmenter.segment(name), ({ segment }) => segment)
  if (graphemes.length <= JOB_SUBJECT_SUFFIX_LENGTH) {
    return <span className="block truncate font-semibold text-foreground" title={name}>{name}</span>
  }
  const split = graphemes.length - JOB_SUBJECT_SUFFIX_LENGTH
  return (
    <span className="flex min-w-0 font-semibold text-foreground" title={name}>
      <span className="min-w-0 truncate">{graphemes.slice(0, split).join('')}</span>
      <span className="flex max-w-full shrink-0 justify-end overflow-hidden whitespace-nowrap">
        <span className="shrink-0">{graphemes.slice(split).join('')}</span>
      </span>
    </span>
  )
}

function JobRow({ item, expanded, onSelect, onOutput, selectedOutput, onAction, acting, onClone, returnQuery }: { item: WorkspaceJobDto; expanded: boolean; onSelect: () => void; onOutput: (key: string) => void; selectedOutput: string | null; onAction: (action: 'cancel' | 'retry') => void; acting: boolean; onClone?: () => void; returnQuery: string }) {
  const token = statusTok[item.status as keyof typeof statusTok] ?? statusTok.draft
  const committed = item.outputs.filter((output) => output.outcome === 'committed')
  const publishedRevision = managedWriteRevisionReceipt(item, committed)
  const rows = item.rows ?? item.profile?.rowCount ?? null
  const report = item.distributionReport
  const dataset = item.datasetContext
  const datasetHref = publishedRevision
    ? datasetViewerHash(publishedRevision.datasetId, publishedRevision.revisionId, { view: 'jobs', query: returnQuery })
    : dataset
      ? datasetViewerHash(dataset.datasetId, dataset.revisionId ?? undefined, { view: 'jobs', query: returnQuery })
      : null
  const active = item.status === 'queued' || item.status === 'running'
  const subject = report ? `Distribution report · ${item.nodeLabel || report.datasetViewId}`
    : dataset ? `${DATASET_TASK_LABELS[dataset.taskKind]} · ${dataset.name || dataset.datasetId}`
    : item.canvasName || 'Unavailable canvas'
  const context = report ? report.complete == null ? 'Coverage pending' : report.complete ? 'Complete saved report' : 'Sampled report'
    : dataset ? `Dataset ${dataset.name || dataset.datasetId}`
      : item.nodeLabel || 'Whole canvas'
  const outcome = report ? report.measuredRows == null ? 'Report pending' : `${report.measuredRows.toLocaleString()} measured rows`
    : item.status === 'failed' ? 'Failed'
      : item.status === 'cancelled' ? 'Cancelled'
        : item.cancelRequested ? 'Cancellation requested'
          : publishedRevision ? 'Dataset revision published'
            : committed.length ? `${committed.length} output${committed.length === 1 ? '' : 's'} available`
              : item.status === 'done' ? 'Completed'
                : item.externalWait ? 'Waiting for external work'
                  : item.status === 'queued' ? 'Queued'
                    : 'In progress'
  const outcomeDetail = item.error ? 'Open for failure details' : !report && rows != null ? rowLabel(rows) : null
  const duration = item.ms != null ? fmtMs(item.ms) : active ? 'In progress' : 'Unavailable'
  const step = jobStep(item)
  return <article className="border-b border-border last:border-b-0">
    <button type="button" onClick={onSelect} aria-expanded={expanded}
      aria-label={`Open run ${item.runId ?? item.id} in ${subject}`}
      className="grid w-full grid-cols-[88px_minmax(0,1fr)_minmax(110px,1fr)] gap-2 px-3 py-2.5 text-left text-[12px] hover:bg-muted/35 md:grid-cols-[96px_minmax(150px,1.3fr)_minmax(130px,1fr)_100px_140px] md:gap-3 xl:grid-cols-[108px_minmax(190px,1.3fr)_minmax(150px,1fr)_120px_100px_150px]">
      <span className="flex flex-wrap items-center gap-1.5">
        <Badge variant="secondary" className="capitalize" style={{ color: token.color }}>{item.status}</Badge>
        {active && item.progress != null && <span className="text-[10.5px] text-muted-foreground">{progressLabel(item.progress)}</span>}
      </span>
      <span className="min-w-0">
        {!report && !dataset
          ? <JobSubject name={subject} />
          : <span className="block truncate font-semibold text-foreground">{subject}</span>}
        <span className="block truncate text-muted-foreground">{context}</span>
      </span>
      <span className="min-w-0"><span className={`block truncate font-medium ${item.status === 'failed' ? 'text-destructive' : 'text-foreground'}`}>{outcome}</span>{outcomeDetail && <span className="block truncate text-muted-foreground">{outcomeDetail}</span>}</span>
      <span className="hidden text-muted-foreground md:block">{duration}</span>
      <span className="hidden truncate text-muted-foreground xl:block" title={item.backend}>{item.backend}</span>
      <span className="hidden whitespace-nowrap text-[10.5px] text-muted-foreground md:block">{item.createdAt ? new Date(item.createdAt).toLocaleString() : '—'}</span>
    </button>
    {expanded && <div className="grid gap-2 border-t border-border bg-muted/20 px-4 py-3 text-[11.5px] sm:grid-cols-2">
      {step && <div role="status" aria-label="Job progress" className="flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground sm:col-span-2">
        <span><strong className="text-foreground">Current step</strong> · {step}</span>
        {item.boundedFanout && <span>{item.boundedFanout.completedPartitions} of {item.boundedFanout.partitionCount} partitions complete</span>}
      </div>}
      {(item.cancelRequested || item.error) && <div className="grid gap-1 sm:col-span-2">
        {item.cancelRequested && <div className="text-amber-700">Cancellation requested; waiting for the owned work to stop or be fenced.</div>}
        {item.error && <div role="alert" className="whitespace-pre-wrap rounded border border-destructive/25 bg-destructive/10 p-2 text-destructive">{item.error}</div>}
      </div>}
      <div className="flex flex-wrap content-start gap-2 sm:col-span-2">
        {item.canvasId && <a className="rounded-md border border-border bg-background px-2 py-1 font-semibold hover:bg-accent" href={routeHash('canvas', item.canvasId, undefined, undefined, undefined, item.targetNodeId ?? undefined)}>Open in Canvas</a>}
        {report && <a className="rounded-md border border-border bg-background px-2 py-1 font-semibold hover:bg-accent" href={`#/distribution-reports/${encodeURIComponent(report.reportId)}`}>Open report</a>}
        {datasetHref && <a className="rounded-md border border-border bg-background px-2 py-1 font-semibold hover:bg-accent" href={datasetHref}>Open dataset</a>}
        {!publishedRevision && committed.map((output, index) => <button key={outputKey(output.nodeId, output.portId)} className={`rounded-md border px-2 py-1 font-semibold ${selectedOutput === outputKey(output.nodeId, output.portId) ? 'border-primary bg-primary/10' : 'border-border bg-background hover:bg-accent'}`} onClick={() => onOutput(outputKey(output.nodeId, output.portId))}>
          {committed.length === 1 ? 'Open result' : `Open result ${index + 1}`}
        </button>)}
        {item.taskId && (item.canCancel ?? (item.status === 'queued' || item.status === 'running')) && <Button size="sm" variant="outline" disabled={acting || item.cancelRequested} onClick={() => onAction('cancel')}>Cancel task</Button>}
        {item.taskId && item.canRetry && <Button size="sm" variant="outline" disabled={acting} onClick={() => onAction('retry')}>{item.checkpoint?.retryLabel || 'Retry task'}</Button>}
        {onClone && <Button size="sm" variant="outline" onClick={onClone}>Duplicate Canvas</Button>}
      </div>
    </div>}
  </article>
}
