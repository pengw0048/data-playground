import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type WorkspaceJobDto, type WorkspaceJobsQuery } from '../api/client'
import { routeHash } from '../router'
import { useStore } from '../store/graph'
import { status as statusTok } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { FullResult } from '../panels/DataPanel'
import { fmtMs } from '../panels/RunHistoryModal'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'

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

export function JobsView() {
  const jobsQuery = useStore((state) => state.jobsQuery)
  const setJobsQuery = useStore((state) => state.setJobsQuery)
  const params = useMemo(() => new URLSearchParams(jobsQuery), [jobsQuery])
  const filterKey = useMemo(() => {
    const copy = new URLSearchParams(params)
    copy.delete('run'); copy.delete('output')
    return copy.toString()
  }, [params])
  const [items, setItems] = useState<WorkspaceJobDto[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const [loadMoreError, setLoadMoreError] = useState('')
  const [loadedMore, setLoadedMore] = useState(false)
  const request = useRef(0)
  const deepLinkRequest = useRef('')

  const load = useCallback(async (nextCursor?: string, refresh = false) => {
    const sequence = ++request.current
    if (nextCursor) { setLoadingMore(true); setLoadMoreError(''); setLoadedMore(true) }
    else if (!refresh) {
      setLoading(true); setError(''); setItems([]); setLoadedMore(false)
      deepLinkRequest.current = ''
    }
    try {
      const page = await api.workspaceJobs(queryFrom(new URLSearchParams(filterKey), nextCursor))
      if (sequence !== request.current) return
      if (!nextCursor) setError('')
      setItems((current) => nextCursor
        ? [...current, ...page.items.filter((item) => !current.some((row) => row.id === item.id))]
        : page.items)
      setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
    } catch (caught) {
      if (sequence !== request.current) return
      const message = caught instanceof Error ? caught.message : String(caught)
      if (nextCursor) setLoadMoreError(message)
      else setError(message)
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
    if (loadedMore || !items.some((item) => item.status === 'queued' || item.status === 'running')) return
    const timer = window.setInterval(() => { if (!loadingMore) void load(undefined, true) }, 5000)
    return () => window.clearInterval(timer)
  }, [items, load, loadedMore, loadingMore])

  const update = (name: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value); else next.delete(name)
    next.delete('run'); next.delete('output')
    setJobsQuery(next.toString())
  }
  const selectRun = (runId: string | null, output?: string) => {
    const next = new URLSearchParams(params)
    if (runId) next.set('run', runId); else next.delete('run')
    if (output) next.set('output', output); else next.delete('output')
    setJobsQuery(next.toString())
  }
  const selected = items.find((item) => jobKey(item) === params.get('run'))
  const selectedOutput = selected?.outputs.find((output) =>
    outputKey(output.nodeId, output.portId) === params.get('output'))

  return (
    <div className="flex h-full min-w-0 flex-col">
      <header className="flex min-h-[68px] flex-wrap items-center gap-3 border-b border-border px-4 py-3 sm:px-7">
        <div><h1 className="text-[20px] font-bold text-foreground">Jobs</h1>
          <p className="text-[11.5px] text-muted-foreground">Persisted runs across accessible canvases</p></div>
        <span className="flex-1" />
        <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading || loadingMore}>
          <Icon name="refresh" size={13} /> Refresh
        </Button>
      </header>

      <section aria-label="Job filters" className="grid grid-cols-2 gap-2 border-b border-border bg-card/60 px-4 py-3 sm:grid-cols-4 xl:grid-cols-7 xl:px-7">
        <label className="grid gap-1 text-[10.5px] text-muted-foreground">Status
          <select aria-label="Filter jobs by status" value={params.get('status') ?? ''} onChange={(event) => update('status', event.target.value)} className="h-8 rounded-md border border-border bg-background px-2 text-[12px] text-foreground">
            {STATUSES.map((value) => <option key={value} value={value}>{value || 'All states'}</option>)}
          </select></label>
        <Filter label="Canvas ID" name="canvas" value={params.get('canvas') ?? ''} onChange={update} />
        <Filter label="Node ID" name="node" value={params.get('node') ?? ''} onChange={update} />
        <Filter label="Backend" name="backend" value={params.get('backend') ?? ''} onChange={update} />
        <label className="grid gap-1 text-[10.5px] text-muted-foreground">From
          <input aria-label="Filter jobs from time" type="datetime-local" value={localDate(params.get('after'))} onChange={(event) => update('after', isoDate(event.target.value))} className="h-8 rounded-md border border-border bg-background px-2 text-[12px] text-foreground" /></label>
        <label className="grid gap-1 text-[10.5px] text-muted-foreground">To
          <input aria-label="Filter jobs to time" type="datetime-local" value={localDate(params.get('before'))} onChange={(event) => update('before', isoDate(event.target.value))} className="h-8 rounded-md border border-border bg-background px-2 text-[12px] text-foreground" /></label>
        <Filter label="Text" name="q" value={params.get('q') ?? ''} onChange={update} placeholder="Run, canvas, failure…" />
      </section>

      <div className="min-h-0 flex-1 overflow-auto px-3 py-3 sm:px-7">
        {loading && <div className="p-5 text-[12.5px] text-muted-foreground">Loading Jobs…</div>}
        {!loading && error && <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-4 text-[12.5px] text-destructive">Couldn’t load Jobs: {error} <button className="ml-2 font-semibold underline" onClick={() => void load()}>Retry</button></div>}
        {!loading && !error && items.length === 0 && <div className="rounded-lg border border-dashed border-border p-8 text-center text-[12.5px] text-muted-foreground">No runs match these filters.</div>}
        {items.length > 0 && <div className="min-w-[850px] overflow-hidden rounded-lg border border-border bg-card">
          <div className="grid grid-cols-[108px_minmax(170px,1fr)_minmax(150px,1fr)_110px_120px_105px] gap-3 border-b border-border bg-muted/40 px-3 py-2 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
            <span>State</span><span>Canvas / node</span><span>Attempt / output</span><span>Backend</span><span>Timing</span><span>Recorded</span>
          </div>
          {items.map((item) => <JobRow key={item.id} item={item} expanded={selected?.id === item.id} onSelect={() => selectRun(selected?.id === item.id ? null : item.runId ?? item.id)} onOutput={(key) => selectRun(item.runId ?? item.id, key)} selectedOutput={params.get('output')} />)}
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
    </div>
  )
}

function Filter({ label, name, value, onChange, placeholder }: { label: string; name: string; value: string; onChange: (name: string, value: string) => void; placeholder?: string }) {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])
  return <label className="grid gap-1 text-[10.5px] text-muted-foreground">{label}<input aria-label={`Filter jobs by ${label.toLowerCase()}`} value={draft} placeholder={placeholder} onChange={(event) => setDraft(event.target.value)} onBlur={() => onChange(name, draft.trim())} onKeyDown={(event) => { if (event.key === 'Enter') onChange(name, draft.trim()) }} className="h-8 min-w-0 rounded-md border border-border bg-background px-2 text-[12px] text-foreground" /></label>
}

function JobRow({ item, expanded, onSelect, onOutput, selectedOutput }: { item: WorkspaceJobDto; expanded: boolean; onSelect: () => void; onOutput: (key: string) => void; selectedOutput: string | null }) {
  const token = statusTok[item.status as keyof typeof statusTok] ?? statusTok.draft
  const committed = item.outputs.filter((output) => output.outcome === 'committed')
  const rows = item.rows ?? item.profile?.rowCount ?? null
  return <article className="border-b border-border last:border-b-0">
    <button type="button" onClick={onSelect} aria-expanded={expanded}
      aria-label={`Open run ${item.runId ?? item.id} in ${item.canvasName}`}
      className="grid w-full grid-cols-[108px_minmax(170px,1fr)_minmax(150px,1fr)_110px_120px_105px] gap-3 px-3 py-2.5 text-left text-[12px] hover:bg-muted/35">
      <span className="flex items-center gap-1.5"><span style={{ color: token.color }}>{token.glyph}</span><Badge variant="secondary" className="capitalize">{item.status}</Badge></span>
      <span className="min-w-0"><span className="block truncate font-semibold text-foreground">{item.canvasName}</span><span className="block truncate text-muted-foreground">{item.nodeLabel || item.targetNodeId || 'Whole canvas'}</span></span>
      <span className="min-w-0"><span className="block truncate font-mono text-[10.5px] text-muted-foreground" title={item.attempt}>{item.attempt}</span><span>{committed.length ? `${committed.length} retained output${committed.length === 1 ? '' : 's'}` : rows != null ? `${rows.toLocaleString()} rows` : 'No retained output'}</span></span>
      <span className="truncate text-muted-foreground" title={item.backend}>{item.backend}</span>
      <span className="text-muted-foreground">{item.ms != null ? fmtMs(item.ms) : 'In progress'}{rows != null && <span className="block">{rows.toLocaleString()} rows</span>}</span>
      <span className="text-[10.5px] text-muted-foreground">{item.createdAt ? new Date(item.createdAt).toLocaleString() : '—'}</span>
    </button>
    {expanded && <div className="grid gap-2 border-t border-border bg-muted/20 px-4 py-3 text-[11.5px] sm:grid-cols-2">
      <div className="grid gap-1"><div><strong>Run:</strong> <span className="font-mono">{item.runId ?? item.id}</span></div><div><strong>Attempt:</strong> <span className="font-mono">{item.attempt}</span></div>{item.error && <div role="alert" className="whitespace-pre-wrap rounded border border-destructive/25 bg-destructive/10 p-2 text-destructive">{item.error}</div>}</div>
      <div className="flex flex-wrap content-start gap-2">
        <a className="rounded-md border border-border bg-background px-2 py-1 font-semibold hover:bg-accent" href={routeHash('canvas', item.canvasId)}>Open canvas</a>
        {item.targetNodeId && <a className="rounded-md border border-border bg-background px-2 py-1 font-semibold hover:bg-accent" href={routeHash('canvas', item.canvasId, undefined, undefined, undefined, item.targetNodeId)}>Open node</a>}
        {committed.map((output) => <button key={outputKey(output.nodeId, output.portId)} className={`rounded-md border px-2 py-1 font-semibold ${selectedOutput === outputKey(output.nodeId, output.portId) ? 'border-primary bg-primary/10' : 'border-border bg-background hover:bg-accent'}`} onClick={() => onOutput(outputKey(output.nodeId, output.portId))}>Open {output.portLabel || output.portId}</button>)}
      </div>
    </div>}
  </article>
}
