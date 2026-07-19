import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type CanvasFile, type WorkspaceJobDto, type WorkspaceJobsQuery } from '../api/client'
import type { DatasetRevisionDetail, WriteReceipt } from '../types/api'
import { routeHash } from '../router'
import { useStore } from '../store/graph'
import { status as statusTok } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { FullResult } from '../panels/DataPanel'
import { fmtMs } from '../panels/RunHistoryModal'
import { ExecutionManifestDetail } from '../components/ExecutionManifestDetail'
import { CanvasCopyModal, type CanvasCopySource } from '../panels/CanvasCopyModal'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { DistributionReportPage } from './DistributionReports'

const PAGE_SIZE = 50
const STATUSES = ['', 'queued', 'running', 'done', 'failed', 'cancelled'] as const

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
const updateLabel = (updatedAt: string | null | undefined) => (
  updatedAt ? new Date(updatedAt).toLocaleString() : 'Unavailable'
)
const refreshLabel = (refreshedAt: number) => new Date(refreshedAt).toLocaleTimeString()

function jobPhase(item: WorkspaceJobDto): string | null {
  if (item.mergeColumns) return `Column merge · ${readable(item.mergeColumns.phase)}`
  if (item.externalWait) return `External wait · ${readable(item.externalWait.phase)}`
  if (item.checkpoint) return `Checkpoint · ${readable(item.checkpoint.phase)}`
  if (item.boundedFanout) return `Fan-out · ${readable(item.boundedFanout.stage)}`
  return null
}

export function JobsView() {
  const jobsQuery = useStore((state) => state.jobsQuery)
  const setJobsQuery = useStore((state) => state.setJobsQuery)
  const canvases = useStore((state) => state.files)
  const refreshFiles = useStore((state) => state.refreshFiles)
  const params = useMemo(() => new URLSearchParams(jobsQuery), [jobsQuery])
  const filterKey = useMemo(() => {
    const copy = new URLSearchParams(params)
    copy.delete('run'); copy.delete('output'); copy.delete('report')
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
  const request = useRef(0)
  const deepLinkRequest = useRef('')
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
        deepLinkRequest.current = ''
      } else {
        // A selected run may have been injected from its direct link rather than the first page.
        // Recheck it after replacing the page so Refresh preserves that explicit selection.
        deepLinkRequest.current = ''
      }
    }
    try {
      const page = await api.workspaceJobs(queryFrom(new URLSearchParams(filterKey), nextCursor))
      if (sequence !== request.current) return
      if (!nextCursor) {
        setError('')
        setRefreshError('')
        setHasActiveFirstPage(page.items.some((item) => item.status === 'queued' || item.status === 'running'))
        setLastSuccessfulRefresh(Date.now())
        setLoadedMore(false)
      }
      setItems((current) => nextCursor
        ? [...current, ...page.items.filter((item) => !current.some((row) => row.id === item.id))]
        : page.items)
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
    if (!runId || loading || items.some((item) => jobKey(item) === runId)) return
    const key = `${filterKey}\u0000${runId}`
    if (deepLinkRequest.current === key) return
    deepLinkRequest.current = key
    let live = true
    void api.workspaceJobs({ ...queryFrom(new URLSearchParams(filterKey)), limit: 1, runId })
      .then((page) => { if (live && page.items[0]) setItems((current) => [page.items[0], ...current]) })
      .catch(() => { /* the main page remains useful; an unavailable deep link is simply not expanded */ })
    return () => { live = false }
  }, [filterKey, items, loading, params])
  useEffect(() => {
    if (loadedMore || !hasActiveFirstPage) return
    const timer = window.setInterval(() => { if (!loading && !loadingMore) void load(undefined, 'refresh') }, 5000)
    return () => window.clearInterval(timer)
  }, [hasActiveFirstPage, load, loadedMore, loading, loadingMore])

  const update = (name: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value); else next.delete(name)
    next.delete('run'); next.delete('output'); next.delete('report')
    setJobsQuery(next.toString())
  }
  const selectRun = (runId: string | null, output?: string) => {
    const next = new URLSearchParams(params)
    if (runId) next.set('run', runId); else next.delete('run')
    if (output) next.set('output', output); else next.delete('output')
    next.delete('report')
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
        if (action === 'cancel') await api.cancelMergeColumnsTask(item.taskId)
        else {
          const actionId = retryActions.current.get(runId) ?? globalThis.crypto.randomUUID()
          retryActions.current.set(runId, actionId)
          await api.retryMergeColumnsTask(item.taskId, actionId)
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
  const nodeChoices = useMemo(() => currentPageNodeChoices(items), [items])
  const backendChoices = useMemo(() => [...new Set(items.map((item) => item.backend).filter(Boolean))], [items])
  const selectedNodeChoice = nodeChoiceValue(params.get('canvas'), params.get('node'))
  const listedNode = nodeChoices.some((choice) => choice.value === selectedNodeChoice)
  const backend = params.get('backend') ?? ''
  const listedBackend = backendChoices.includes(backend)
  const selectNode = (value: string) => {
    if (!value) {
      update('node', '')
      return
    }
    const [canvasId, nodeId] = JSON.parse(value) as [string, string]
    const next = new URLSearchParams(params)
    next.set('canvas', canvasId)
    next.set('node', nodeId)
    next.delete('run'); next.delete('output'); next.delete('report')
    setJobsQuery(next.toString())
  }
  const selectCanvas = (value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set('canvas', value); else next.delete('canvas')
    // A node identity is scoped to its canvas. Do not leave an invisible stale node filter
    // behind when choosing a different canvas (or returning to all accessible canvases).
    next.delete('node')
    next.delete('run'); next.delete('output'); next.delete('report')
    setJobsQuery(next.toString())
  }
  const freshness = lastSuccessfulRefresh == null
    ? null
    : refreshError
      ? loadedMore
        ? `Refresh failed; showing the prior paginated snapshot. Automatic refresh remains paused. Last successful refresh: ${refreshLabel(lastSuccessfulRefresh)}`
        : `Refresh failed; showing the last successful first page. Last successful refresh: ${refreshLabel(lastSuccessfulRefresh)}`
      : loadedMore
        ? `Automatic refresh paused after loading more. Last successful refresh: ${refreshLabel(lastSuccessfulRefresh)}`
        : hasActiveFirstPage
          ? `Live first page. Last successful refresh: ${refreshLabel(lastSuccessfulRefresh)}`
          : `Snapshot; no active Jobs. Last successful refresh: ${refreshLabel(lastSuccessfulRefresh)}`

  const reportId = params.get('report')
  if (reportId) return <DistributionReportPage reportId={reportId} compareReportId={params.get('compare') || undefined} onClose={() => {
    const next = new URLSearchParams(params)
    next.delete('report'); next.delete('compare')
    setJobsQuery(next.toString())
  }} />

  return (
    <div className="flex h-full min-w-0 flex-col">
      <header className="flex min-h-[68px] flex-wrap items-center gap-3 border-b border-border px-4 py-3 sm:px-7">
        <div><h1 className="text-[20px] font-bold text-foreground">Jobs</h1>
          <p className="text-[11.5px] text-muted-foreground">{freshness ?? 'Persisted runs across accessible canvases'}</p></div>
        <span className="flex-1" />
        <Button variant="outline" size="sm" onClick={() => void load(undefined, 'refresh')} disabled={loading || loadingMore}>
          <Icon name="refresh" size={13} /> Refresh
        </Button>
      </header>

      <section aria-label="Job filters" className="grid grid-cols-2 gap-2 border-b border-border bg-card/60 px-4 py-3 sm:grid-cols-4 xl:grid-cols-7 xl:px-7">
        <label className="grid gap-1 text-[10.5px] text-muted-foreground">Status
          <select aria-label="Filter jobs by status" value={params.get('status') ?? ''} onChange={(event) => update('status', event.target.value)} className="h-8 rounded-md border border-border bg-background px-2 text-[12px] text-foreground">
            {STATUSES.map((value) => <option key={value} value={value}>{value || 'All states'}</option>)}
          </select></label>
        <CanvasSelector canvases={canvases} value={params.get('canvas') ?? ''} onChange={selectCanvas} />
        <label className="grid gap-1 text-[10.5px] text-muted-foreground">Node
          <select aria-label="Filter jobs by node" value={selectedNodeChoice} onChange={(event) => selectNode(event.target.value)} className="h-8 rounded-md border border-border bg-background px-2 text-[12px] text-foreground">
            <option value="">All nodes on loaded Jobs</option>
            {!listedNode && selectedNodeChoice && <option value={selectedNodeChoice}>Exact node ID: {params.get('node')}</option>}
            {nodeChoices.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
          </select></label>
        <label className="grid gap-1 text-[10.5px] text-muted-foreground">Backend
          <select aria-label="Filter jobs by backend" value={backend} onChange={(event) => update('backend', event.target.value)} className="h-8 rounded-md border border-border bg-background px-2 text-[12px] text-foreground">
            <option value="">All backends on loaded Jobs</option>
            {!listedBackend && backend && <option value={backend}>Exact backend ID: {backend}</option>}
            {backendChoices.map((backend) => <option key={backend} value={backend}>{backend}</option>)}
          </select></label>
        <label className="grid gap-1 text-[10.5px] text-muted-foreground">From
          <input aria-label="Filter jobs from time" type="datetime-local" value={localDate(params.get('after'))} onChange={(event) => update('after', isoDate(event.target.value))} className="h-8 rounded-md border border-border bg-background px-2 text-[12px] text-foreground" /></label>
        <label className="grid gap-1 text-[10.5px] text-muted-foreground">To
          <input aria-label="Filter jobs to time" type="datetime-local" value={localDate(params.get('before'))} onChange={(event) => update('before', isoDate(event.target.value))} className="h-8 rounded-md border border-border bg-background px-2 text-[12px] text-foreground" /></label>
        <Filter label="Text" name="q" value={params.get('q') ?? ''} onChange={update} placeholder="Run, canvas, failure…" />
      </section>

      <details className="border-b border-border bg-card/30 px-4 py-2 text-[11.5px] xl:px-7">
        <summary className="cursor-pointer text-muted-foreground">Advanced exact IDs</summary>
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3">
          <Filter label="Canvas ID (exact)" name="canvas" value={params.get('canvas') ?? ''} onChange={update} />
          <Filter label="Node ID (exact)" name="node" value={params.get('node') ?? ''} onChange={update} />
          <Filter label="Backend ID (exact)" name="backend" value={params.get('backend') ?? ''} onChange={update} />
        </div>
      </details>

      <div className="min-h-0 flex-1 overflow-auto px-3 py-3 sm:px-7">
        {actionError && <div role="alert" className="mb-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-[12px] text-destructive">Job action failed: {actionError}</div>}
        {loading && <div className="p-5 text-[12.5px] text-muted-foreground">Loading Jobs…</div>}
        {!loading && error && <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-4 text-[12.5px] text-destructive">Couldn’t load Jobs: {error} <button className="ml-2 font-semibold underline" onClick={() => void load()}>Retry</button></div>}
        {!loading && refreshError && <div role="alert" className="mb-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-[12px] text-destructive">Couldn’t refresh Jobs: {refreshError} Showing the prior Jobs snapshot.</div>}
        {!loading && !error && items.length === 0 && <div className="rounded-lg border border-dashed border-border p-8 text-center text-[12.5px] text-muted-foreground">No runs match these filters.</div>}
        {items.length > 0 && <div className="min-w-[850px] overflow-hidden rounded-lg border border-border bg-card">
          <div className="grid grid-cols-[108px_minmax(170px,1fr)_minmax(150px,1fr)_110px_120px_105px] gap-3 border-b border-border bg-muted/40 px-3 py-2 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
            <span>State</span><span>Canvas / node</span><span>Attempt / output</span><span>Backend</span><span>Timing</span><span>Recorded</span>
          </div>
          {items.map((item) => <JobRow key={item.id} item={item} expanded={selected?.id === item.id} onSelect={() => selectRun(selected?.id === item.id ? null : item.runId ?? item.id)} onOutput={(key) => selectRun(item.runId ?? item.id, key)} selectedOutput={params.get('output')} onAction={(action) => void act(item, action)} acting={acting.startsWith(`${item.runId ?? item.id}:`)} onClone={item.canvasId ? () => setCopySource({ canvasId: item.canvasId!, subjectId: item.id, name: item.canvasName || 'Untitled canvas' }) : undefined} />)}
        </div>}
        {loadMoreError && <div role="alert" className="mt-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-[12px] text-destructive">Couldn’t load more Jobs: {loadMoreError} <button className="ml-2 font-semibold underline" onClick={() => cursor && void load(cursor)}>Retry load more</button></div>}
        {hasMore && !loadMoreError && <Button variant="outline" className="mt-3 w-full" disabled={loadingMore || !cursor} onClick={() => cursor && void load(cursor)}>{loadingMore ? 'Loading…' : 'Load more'}</Button>}
      </div>

      {selected && selectedOutput?.outcome === 'committed' && selectedOutput.uri && (
        <aside aria-label="Retained artifact" className="max-h-[45vh] overflow-auto border-t border-border bg-card">
          <div className="flex items-center border-b border-border px-4 py-2 text-[12px] font-semibold">Retained artifact · {selectedOutput.portLabel || selectedOutput.portId}<span className="flex-1" /><button aria-label="Close retained artifact" onClick={() => selectRun(selected.runId ?? selected.id)}><Icon name="close" size={14} /></button></div>
          <FullResult uri={selectedOutput.uri} total={selectedOutput.rows ?? null} runId={selected.runId ?? undefined} nodeId={selectedOutput.nodeId} portId={selectedOutput.portId} publicationKind={selectedOutput.publicationKind} name={selectedOutput.table ?? selectedOutput.portLabel ?? selectedOutput.portId} />
        </aside>
      )}
      {selected && checkpointOutput && (
        <aside aria-label="Retained checkpoint" className="max-h-[45vh] overflow-auto border-t border-border bg-card">
          <div className="flex items-center border-b border-border px-4 py-2 text-[12px] font-semibold">Retained checkpoint · {checkpointOutput.checkpointNodeId}<span className="flex-1" /><button aria-label="Close retained checkpoint" onClick={() => selectRun(selected.runId ?? selected.id)}><Icon name="close" size={14} /></button></div>
          <FullResult uri={checkpointOutput.clientKey} total={checkpointOutput.rows ?? null} runId={selected.runId ?? undefined} nodeId={checkpointOutput.clientKey} portId={checkpointOutput.outputPortId} publicationKind="result" name={checkpointOutput.checkpointNodeId} />
        </aside>
      )}
      {copySource && <CanvasCopyModal source={copySource} onClose={() => setCopySource(null)} />}
    </div>
  )
}

function CanvasSelector({ canvases, value, onChange }: { canvases: CanvasFile[]; value: string; onChange: (value: string) => void }) {
  const listed = canvases.some((canvas) => canvas.id === value)
  return <label className="grid gap-1 text-[10.5px] text-muted-foreground">Canvas
    <select aria-label="Filter jobs by canvas" value={value} onChange={(event) => onChange(event.target.value)} className="h-8 rounded-md border border-border bg-background px-2 text-[12px] text-foreground">
      <option value="">All accessible canvases</option>
      {!listed && value && <option value={value}>Exact canvas ID: {value}</option>}
      {canvases.map((canvas) => <option key={canvas.id} value={canvas.id}>{canvasLabel(canvas)}</option>)}
    </select></label>
}

function canvasLabel(canvas: CanvasFile): string {
  return `${canvas.name || 'Untitled canvas'} · ${canvas.id}`
}

function nodeChoiceValue(canvasId: string | null, nodeId: string | null): string {
  return canvasId && nodeId ? JSON.stringify([canvasId, nodeId]) : ''
}

function currentPageNodeChoices(items: WorkspaceJobDto[]) {
  const choices = new Map<string, { value: string; label: string }>()
  for (const item of items) {
    if (!item.targetNodeId || !item.canvasId) continue
    const value = nodeChoiceValue(item.canvasId, item.targetNodeId)
    if (choices.has(value)) continue
    const node = item.nodeLabel || `Node ${item.targetNodeId}`
    choices.set(value, {
      value,
      label: `${node} · ${item.canvasName || 'Untitled canvas'} (${item.canvasId}) · ${item.targetNodeId}`,
    })
  }
  return [...choices.values()]
}

function Filter({ label, name, value, onChange, placeholder }: { label: string; name: string; value: string; onChange: (name: string, value: string) => void; placeholder?: string }) {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])
  return <label className="grid gap-1 text-[10.5px] text-muted-foreground">{label}<input aria-label={`Filter jobs by ${label.toLowerCase()}`} value={draft} placeholder={placeholder} onChange={(event) => setDraft(event.target.value)} onBlur={() => onChange(name, draft.trim())} onKeyDown={(event) => { if (event.key === 'Enter') onChange(name, draft.trim()) }} className="h-8 min-w-0 rounded-md border border-border bg-background px-2 text-[12px] text-foreground" /></label>
}

function JobRow({ item, expanded, onSelect, onOutput, selectedOutput, onAction, acting, onClone }: { item: WorkspaceJobDto; expanded: boolean; onSelect: () => void; onOutput: (key: string) => void; selectedOutput: string | null; onAction: (action: 'cancel' | 'retry') => void; acting: boolean; onClone?: () => void }) {
  const token = statusTok[item.status as keyof typeof statusTok] ?? statusTok.draft
  const committed = item.outputs.filter((output) => output.outcome === 'committed')
  const rows = item.rows ?? item.profile?.rowCount ?? null
  const phase = jobPhase(item)
  const report = item.distributionReport
  const subject = report ? `Distribution report · ${item.nodeLabel || report.datasetViewId}` : item.canvasName || 'Unavailable canvas'
  return <article className="border-b border-border last:border-b-0">
    <button type="button" onClick={onSelect} aria-expanded={expanded}
      aria-label={`Open run ${item.runId ?? item.id} in ${subject}`}
      className="grid w-full grid-cols-[108px_minmax(170px,1fr)_minmax(150px,1fr)_110px_120px_105px] gap-3 px-3 py-2.5 text-left text-[12px] hover:bg-muted/35">
      <span className="flex flex-wrap items-center gap-1.5"><span style={{ color: token.color }}>{token.glyph}</span><Badge variant="secondary" className="capitalize">{item.status}</Badge>{item.progress != null && <span className="text-[10.5px] text-muted-foreground">{progressLabel(item.progress)}</span>}</span>
      <span className="min-w-0"><span className="block truncate font-semibold text-foreground">{subject}</span><span className="block truncate text-muted-foreground">{report ? report.complete == null ? 'Coverage pending' : report.complete ? 'Complete retained report' : 'Sample retained report' : item.nodeLabel || item.targetNodeId || 'Whole canvas'}</span></span>
      <span className="min-w-0"><span className="block truncate font-mono text-[10.5px] text-muted-foreground" title={item.attempt}>{item.attempt}</span><span>{report ? report.measuredRows == null ? 'Report pending' : `${report.measuredRows.toLocaleString()} measured rows` : committed.length ? `${committed.length} retained output${committed.length === 1 ? '' : 's'}` : rows != null ? `${rows.toLocaleString()} rows` : 'No retained output'}</span></span>
      <span className="truncate text-muted-foreground" title={item.backend}>{item.backend}</span>
      <span className="text-muted-foreground">{item.ms != null ? fmtMs(item.ms) : 'In progress'}{rows != null && <span className="block">{rows.toLocaleString()} rows</span>}</span>
      <span className="text-[10.5px] text-muted-foreground">{item.createdAt ? new Date(item.createdAt).toLocaleString() : '—'}</span>
    </button>
    {expanded && <div className="grid gap-2 border-t border-border bg-muted/20 px-4 py-3 text-[11.5px] sm:grid-cols-2">
      <div className="grid gap-1"><div><strong>{item.taskId ? 'Task' : 'Run'}:</strong> <span className="font-mono">{item.runId ?? item.id}</span></div><div><strong>State:</strong> <span className="capitalize">{item.status}</span></div>{phase && <div><strong>Phase:</strong> {phase}</div>}<div><strong>Current attempt:</strong> <span className="font-mono">{item.attempt}</span></div><div><strong>Progress:</strong> {progressLabel(item.progress)}</div><div><strong>Last durable update:</strong> {updateLabel(item.updatedAt)}</div>{item.cancelRequested && <div className="text-amber-700">Cancellation requested; waiting for the owned work to stop or be fenced.</div>}{item.error && <div role="alert" className="whitespace-pre-wrap rounded border border-destructive/25 bg-destructive/10 p-2 text-destructive">{item.error}</div>}</div>
      <div className="flex flex-wrap content-start gap-2">
        {item.canvasId && <a className="rounded-md border border-border bg-background px-2 py-1 font-semibold hover:bg-accent" href={routeHash('canvas', item.canvasId)}>Open canvas</a>}
        {item.targetNodeId && item.canvasId && <a className="rounded-md border border-border bg-background px-2 py-1 font-semibold hover:bg-accent" href={routeHash('canvas', item.canvasId, undefined, undefined, undefined, item.targetNodeId)}>Open node</a>}
        {report && <a className="rounded-md border border-border bg-background px-2 py-1 font-semibold hover:bg-accent" href={`#/distribution-reports/${encodeURIComponent(report.reportId)}`}>Open report</a>}
        {committed.map((output) => <button key={outputKey(output.nodeId, output.portId)} className={`rounded-md border px-2 py-1 font-semibold ${selectedOutput === outputKey(output.nodeId, output.portId) ? 'border-primary bg-primary/10' : 'border-border bg-background hover:bg-accent'}`} onClick={() => onOutput(outputKey(output.nodeId, output.portId))}>Open {output.portLabel || output.portId}</button>)}
        {item.taskId && (item.canCancel ?? (item.status === 'queued' || item.status === 'running')) && <Button size="sm" variant="outline" disabled={acting || item.cancelRequested} onClick={() => onAction('cancel')}>Cancel task</Button>}
        {item.taskId && item.canRetry && <Button size="sm" variant="outline" disabled={acting} onClick={() => onAction('retry')}>{item.checkpoint?.retryLabel || 'Retry task'}</Button>}
      </div>
      {item.canvasId && <div className="sm:col-span-2">
        <ExecutionManifestDetail canvasId={item.canvasId} subjectId={item.id} summary={item} onClone={onClone} />
      </div>}
      {item.taskId && <div className="grid gap-2 sm:col-span-2">
        {item.taskAttempts?.length ? <div><strong>Attempts:</strong><ol className="mt-1 grid gap-1">{item.taskAttempts.map((attempt) => <li key={attempt.id} className="rounded border border-border bg-background px-2 py-1"><span className="font-semibold">#{attempt.attemptNumber} {readable(attempt.status)}</span> · Progress {progressLabel(attempt.progress)} · Updated {updateLabel(attempt.updatedAt)}</li>)}</ol></div> : null}
        {item.externalWait && <div><strong>External provider:</strong> {item.externalWait.providerKind} · provider attempt #{item.externalWait.attemptNumber}</div>}
        {item.checkpoint && <div><strong>Checkpoint:</strong> {item.checkpoint.checkpointNodeId}:{item.checkpoint.outputPortId}{item.checkpoint.resumeEligible ? ' · resume eligible' : ''}{item.checkpoint.contentDigest ? ` · ${item.checkpoint.contentDigest}` : ''}{item.checkpoint.rows != null ? ` · ${item.checkpoint.rows.toLocaleString()} rows` : ''}{item.checkpoint.diagnosticCode ? ` · ${item.checkpoint.diagnosticCode}` : ''}</div>}
        {item.boundedFanout && <div><strong>Fan-out:</strong> {item.boundedFanout.completedPartitions}/{item.boundedFanout.partitionCount ?? '—'} partitions{item.boundedFanout.failedPartitions ? ` · ${item.boundedFanout.failedPartitions} failed` : ''} · checkpoint {item.boundedFanout.checkpoint} · gather {item.boundedFanout.gather}{item.boundedFanout.diagnosticCode ? ` · ${item.boundedFanout.diagnosticCode}` : ''}</div>}
        {item.mergeColumns && <div><strong>Column merge:</strong> {readable(item.mergeColumns.phase)} · candidate {item.mergeColumns.candidate}{item.mergeColumns.reused ? ' (reused)' : ''}{item.mergeColumns.candidateRows != null ? ` · ${item.mergeColumns.candidateRows.toLocaleString()} rows` : ''}{item.mergeColumns.diagnosticCode ? ` · ${item.mergeColumns.diagnosticCode}` : ''}</div>}
        <div><strong>Exact inputs:</strong> {item.inputManifest?.length ? item.inputManifest.map((input) => `${input.dataset_id}@${input.revision_id}`).join(', ') : 'No versioned sources'}</div>
        {item.writeIntent && <div><strong>Write:</strong> {item.writeIntent.mode} · {item.writeIntent.destination.name} · expected head {item.writeIntent.expectedHead?.revisionId ?? 'none'}</div>}
        {item.outputReceipt && <ExactRevisionReceipt receipt={item.outputReceipt} />}
        {item.checkpoint?.resumeEligible && item.checkpoint.clientKey && <button type="button" className="rounded-md border border-border bg-background px-2 py-1 font-semibold hover:bg-accent w-fit" onClick={() => onOutput(outputKey(item.checkpoint!.clientKey, item.checkpoint!.outputPortId))}>Open checkpoint</button>}
      </div>}
    </div>}
  </article>
}

function ExactRevisionReceipt({ receipt }: { receipt: WriteReceipt }) {
  const [detail, setDetail] = useState<DatasetRevisionDetail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const open = async () => {
    setLoading(true); setError('')
    try { setDetail(await api.datasetRevision(receipt.datasetId, receipt.revisionId)) }
    catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setLoading(false) }
  }
  return <div className="rounded border border-border bg-background p-2"><strong>Receipt:</strong> dataset <span className="font-mono">{receipt.datasetId}</span> · revision <span className="font-mono">{receipt.revisionId}</span> · {receipt.rows.toLocaleString()} rows · {receipt.bytes.toLocaleString()} bytes
    <Button size="sm" variant="outline" className="ml-2 h-6 px-2 text-[10px]" onClick={() => void open()} disabled={loading}>{loading ? 'Opening…' : 'Open exact revision'}</Button>
    {error && <div role="alert" className="mt-1 text-destructive">Exact revision unavailable: {error}</div>}
    {detail && <div aria-label="Exact revision detail" className="mt-2 rounded border border-border bg-muted/30 p-2 text-[10.5px]"><div>Committed {detail.committedAt ? new Date(detail.committedAt).toLocaleString() : 'at an unknown time'} · {detail.summary.rowCount == null ? 'row count unavailable' : `${detail.summary.rowCount.toLocaleString()} rows`}</div><div>{detail.parentRevisionId ? <>Parent <span className="font-mono">{detail.parentRevisionId}</span></> : 'No parent revision'} · {detail.preview.columns.length} fields</div><div className="mt-1 overflow-auto"><table className="w-full text-left"><thead><tr>{detail.preview.columns.map((column) => <th key={column.name} className="pr-2 font-medium">{column.name}</th>)}</tr></thead><tbody>{detail.preview.rows.slice(0, 5).map((row, index) => <tr key={index}>{detail.preview.columns.map((column) => <td key={column.name} className="max-w-24 truncate pr-2 font-mono">{String(row[column.name] ?? '')}</td>)}</tr>)}</tbody></table></div>{detail.preview.hasMore && <div className="mt-1 text-muted-foreground">Preview is bounded; this remains the exact published revision.</div>}</div>}
  </div>
}
