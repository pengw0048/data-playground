import { useEffect, useState } from 'react'
import { api, toGraph, type ExecutionManifestDetail, type RunRecordDto } from '../api/client'
import { roleCanEdit, useStore } from '../store/graph'
import type { CanvasDoc } from '../types/graph'
import type { CanvasResultRecovery, RunStatus } from '../types/api'
import { Button } from '@/components/ui/button'
import { Icon } from '../ui/Icon'
import { presentRunError } from '../lib/runErrors'
import { fmtMs, HistoryOutputs, RunInputManifest } from './RunHistoryModal'

// Only invalidates requests. The server remains the authority for plan/result reuse.
function requestKey(doc: CanvasDoc): string {
  const graph = toGraph(doc)
  return JSON.stringify({
    requirements: graph.requirements, parameters: graph.parameters, executionBackend: graph.executionBackend,
    nodes: graph.nodes.map(({ position: _position, data: { status: _status, ...data }, ...node }) => ({ ...node, data })),
    edges: graph.edges,
  })
}

const isActive = (status: RunStatus) => status.status === 'running' || status.status === 'queued'
const readable = (run: RunRecordDto) => run.outputs.some((output) => output.outcome === 'committed' && output.uri)
const time = (value?: string | null) => value ? new Date(value).toLocaleString() : 'Time not recorded'

export function CanvasRunsPanel(props: { onClose: () => void; onHistory: () => void }) {
  const canvasId = useStore((s) => s.doc.id)
  const principal = useStore((s) => s.currentUser?.id)
  return <CanvasRuns key={`${canvasId}:${principal}`} {...props} />
}

function CanvasRuns({ onClose, onHistory }: { onClose: () => void; onHistory: () => void }) {
  const doc = useStore((s) => s.doc)
  const runs = useStore((s) => s.runs)
  const graphRun = useStore((s) => s.graphRun)
  const detached = useStore((s) => s.detachedRuns)
  const recovery = useStore((s) => s.executionRecovery)
  const kernelUp = useStore((s) => s.kernelUp)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const [history, setHistory] = useState<RunRecordDto[]>()
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [openOutput, setOpenOutput] = useState<string | null>(null)
  const [proof, setProof] = useState<{ key: string; value?: CanvasResultRecovery; error?: string }>()
  const graphKey = requestKey(doc)
  const executionKey = JSON.stringify([
    graphRun?.phase, graphRun?.runId, graphRun?.status?.status,
    Object.entries(runs).map(([id, run]) => [id, run.phase, run.status?.runId, run.status?.status]),
    Object.values(detached).map((run) => [run.status.runId, run.status.status]),
  ])

  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout>
    const load = async () => {
      try {
        const next = await api.listRuns(doc.id)
        if (live) { setHistory(next.filter((run) => run.jobType === 'run')); setError('') }
      } catch (caught) {
        if (live) setError(caught instanceof Error ? caught.message : String(caught))
      } finally {
        if (live) timer = setTimeout(() => void load(), 3000)
      }
    }
    void load()
    return () => { live = false; clearTimeout(timer) }
  }, [doc.id, executionKey, refresh])

  const historyKey = JSON.stringify(history?.map((run) => [run.id, run.status, run.outputs]))
  useEffect(() => {
    let live = true
    const requestDoc = useStore.getState().doc
    const timer = setTimeout(() => {
      void api.currentResults(requestDoc).then((value) => {
        if (live) setProof({ key: graphKey, value })
      }).catch((caught) => {
        if (live) setProof({ key: graphKey, error: caught instanceof Error ? caught.message : String(caught) })
      })
    }, 250)
    return () => { live = false; clearTimeout(timer) }
  }, [graphKey, historyKey, refresh])

  const selected = history?.find((run) => run.id === selectedId) ?? history?.[0]
  const lastSuccess = history?.find((run) => run.status === 'done' && readable(run))
  const active = new Map<string, { status: RunStatus; cancel: () => Promise<void>; cancelLabel?: string }>()
  if (graphRun?.status && isActive(graphRun.status)) active.set(graphRun.status.runId, {
    status: graphRun.status, cancel: () => useStore.getState().cancelGraphRun(),
  })
  for (const [nodeId, run] of Object.entries(runs)) {
    if (run.status && isActive(run.status)) active.set(run.status.runId, {
      status: run.status, cancel: () => useStore.getState().cancelRun(nodeId),
    })
  }
  for (const run of Object.values(detached)) {
    if (isActive(run.status)) active.set(run.status.runId, {
      status: run.status, cancel: () => useStore.getState().cancelDetachedRuns(),
      cancelLabel: 'Stop all off-canvas runs',
    })
  }
  const reveal = (nodeId: string) => {
    useStore.getState().select(nodeId)
    useStore.getState().requestNodeReveal(doc.id, nodeId)
    onClose()
  }
  const nodeName = (nodeId?: string | null) => nodeId
    ? doc.nodes.find((node) => node.id === nodeId)?.data.title || 'Removed node'
    : 'Whole Canvas'
  const pending = Object.entries(runs).filter(([, run]) => ['parameters', 'estimating', 'estimated', 'confirm', 'drift'].includes(run.phase))
  const unknown = graphRun?.phase === 'unknown' || recovery?.phase === 'unknown'
  const currentProof = proof?.key === graphKey ? proof : undefined

  return <aside aria-label="Canvas runs and results" className="nokey absolute bottom-4 right-4 top-[86px] z-30 flex w-[560px] max-w-[calc(100%-2rem)] flex-col overflow-hidden rounded-xl border border-border bg-card shadow-xl">
    <div className="flex items-center gap-2 border-b border-border px-4 py-3">
      <h2 className="min-w-0 flex-1 text-sm font-semibold">Runs &amp; results</h2>
      <Button size="sm" variant="ghost" onClick={() => setRefresh((value) => value + 1)}>Refresh</Button>
      <button type="button" aria-label="Close runs and results" onClick={onClose} className="p-1 text-muted-foreground"><Icon name="close" size={16} /></button>
    </div>
    <div className="min-h-0 flex-1 overflow-y-auto text-xs">
      <section aria-label="Current execution" className="space-y-2 border-b border-border px-4 py-3">
        {!kernelUp && <p className="text-destructive">Offline — run status cannot be confirmed.</p>}
        {unknown && <p className="text-destructive">Run status is unknown. <button className="underline" onClick={() => useStore.getState().retryExecutionRecovery()}>Retry run check</button></p>}
        {recovery?.phase === 'checking' && <p>Checking active runs…</p>}
        {graphRun?.phase === 'submitting' && <p>Starting this Canvas…</p>}
        {[...active.values()].map(({ status, cancel, cancelLabel }) => <ActiveRun key={status.runId} status={status}
          label={nodeName(status.targetNodeId)} canStop={canEdit && kernelUp} cancel={cancel} cancelLabel={cancelLabel} reveal={reveal}
          nodeExists={(id) => doc.nodes.some((node) => node.id === id)} />)}
        {pending.map(([id, run]) => <div key={id}>
          {nodeName(id)} · {run.phase === 'estimating' ? 'Preparing run…' : 'Waiting for your input before running.'}
          <button className="ml-2 text-primary underline" onClick={() => { useStore.getState().openPanel(id, 'run'); reveal(id) }}>Continue at node</button>
        </div>)}
        {!active.size && !pending.length && !graphRun && !recovery && kernelUp && <p className="text-muted-foreground">Run a node or this Canvas to follow its progress here.</p>}
      </section>
      {error && <div role="alert" className="px-4 py-3 text-destructive">Couldn’t refresh runs: {error}. Saved information may be out of date.</div>}
      {history === undefined && !error && <p className="p-4 text-muted-foreground">Loading this Canvas’s runs…</p>}
      {history?.length === 0 && <p className="p-4 text-muted-foreground">No saved runs yet. Run a node or this Canvas to find its outputs here.</p>}
      {lastSuccess && lastSuccess.id !== selected?.id && <section aria-label="Last successful result" className="border-b border-border bg-muted/20 px-4 py-3">
        <div className="font-semibold">Last successful result in recent history</div>
        <p className="my-1 text-muted-foreground">{nodeName(lastSuccess.targetNodeId)} · {time(lastSuccess.createdAt)}</p>
        <p className="mb-2 text-muted-foreground">This run recorded a saved output. A later failure does not turn it into a failed result.</p>
        <Button variant="outline" size="sm" onClick={() => { setSelectedId(lastSuccess.id); setOpenOutput(null) }}>View last successful result</Button>
      </section>}
      {selected && <section aria-label="Saved run">
        <div className="space-y-2 px-4 py-3">
          <label className="block text-muted-foreground">Saved runs (from the most recent 50 history entries)
            <select className="dp-input mt-1 w-full" aria-label="Choose saved run" value={selected.id}
              onChange={(event) => { setSelectedId(event.target.value); setOpenOutput(null) }}>
              {history?.map((run) => <option key={run.id} value={run.id}>{time(run.createdAt)} · {nodeName(run.targetNodeId)} · {run.status}{run.rows == null ? '' : ` · ${run.rows.toLocaleString()} rows`}</option>)}
            </select>
          </label>
          <div className="flex items-center justify-between gap-2">
            <strong>{selected.status === 'done' ? 'Run completed' : selected.status === 'failed' ? 'Run failed' : `Run ${selected.status}`}</strong>
            {selected.ms != null && <span className="text-muted-foreground">{fmtMs(selected.ms)}</span>}
          </div>
          {selected.error && <RunFailure error={selected.error} />}
          {selected.perNode?.filter((step) => step.status === 'failed').map((step) => <div key={step.nodeId} className="rounded border border-destructive/30 p-2">
            <strong>{step.label || nodeName(step.nodeId)}</strong>
            {step.error && <RunFailure error={step.error} />}
            {doc.nodes.some((node) => node.id === step.nodeId) && <button className="mt-1 text-primary underline" onClick={() => reveal(step.nodeId)}>Go to failed node</button>}
          </div>)}
          {readable(selected) && <ResultRelation run={selected} proof={currentProof} />}
          {selected.status === 'done' && !readable(selected) && <p className="text-muted-foreground">This run has no retained output to open. A completed step does not necessarily keep its intermediate data.</p>}
          {selected.targetNodeId && doc.nodes.some((node) => node.id === selected.targetNodeId) && <button className="text-primary underline" onClick={() => reveal(selected.targetNodeId!)}>Go to output node</button>}
        </div>
        <HistoryOutputs canvasId={doc.id} historyId={selected.id} runId={selected.runId ?? undefined}
          outputs={selected.outputs} openKey={openOutput} onToggle={(key) => setOpenOutput(openOutput === key ? null : key)} />
        <RunInputManifest key={selected.id} historyId={selected.id} manifest={selected.inputManifest} />
        <RunSettings key={selected.id} canvasId={doc.id} run={selected} />
      </section>}
    </div>
    <div className="border-t border-border px-4 py-2"><button className="text-xs text-primary underline" onClick={onHistory}>Open detailed run history</button></div>
  </aside>
}

function RunFailure({ error }: { error: string }) {
  const presented = presentRunError(error)
  return <div className="my-1 text-destructive"><p>{presented.summary}</p>
    {presented.details && <details className="mt-1 text-muted-foreground"><summary>Technical details</summary><pre className="whitespace-pre-wrap break-words text-[11px]">{presented.details}</pre></details>}
  </div>
}

function ActiveRun({ status, label, canStop, cancel, cancelLabel = 'Stop run', reveal, nodeExists }: {
  status: RunStatus; label: string; canStop: boolean; cancel: () => Promise<void>; cancelLabel?: string
  reveal: (nodeId: string) => void; nodeExists: (nodeId: string) => boolean
}) {
  const [stopping, setStopping] = useState(false)
  const completed = status.perNode.filter((step) => step.status === 'done').length
  return <div className="space-y-2">
    <div className="flex items-center justify-between gap-2"><strong>{label} · {status.status}</strong>
      <Button size="sm" variant="outline" disabled={!canStop || stopping} onClick={async () => {
        setStopping(true)
        try { await cancel() } finally { setStopping(false) }
      }}>{stopping ? 'Requesting stop…' : cancelLabel}</Button>
    </div>
    <p className="text-muted-foreground">{completed} of {status.perNode.length} steps completed · {status.rowsProcessed.toLocaleString()} rows processed</p>
    {status.stalled && <p>This step is taking longer. The run has not reported completion.</p>}
    {status.perNode.filter((step) => step.status === 'running' || step.status === 'failed').map((step) => <div key={step.nodeId}>
      <span>{step.label || step.nodeId} · {step.status}</span>
      {nodeExists(step.nodeId) && <button className="ml-2 text-primary underline" onClick={() => reveal(step.nodeId)}>Show node</button>}
    </div>)}
  </div>
}

function ResultRelation({ run, proof }: { run: RunRecordDto; proof?: { value?: CanvasResultRecovery; error?: string } }) {
  if (!proof) return <p className="text-muted-foreground">Checking whether these outputs match the current Canvas…</p>
  if (proof.error || !proof.value) return <p className="text-muted-foreground">Current result status could not be verified. You can still try opening this saved run.</p>
  const outputs = run.outputs.filter((output) => output.outcome === 'committed' && output.uri)
  const matching = outputs.filter((output) => proof.value!.results.some((item) => item.runId === run.runId
    && item.output.nodeId === output.nodeId && item.output.portId === output.portId
    && item.output.uri === output.uri))
  if (matching.length === outputs.length) return <p className="text-muted-foreground">These outputs match the current Canvas plan using this run’s recorded parameters and input versions.</p>
  return <p className="text-amber-700 dark:text-amber-400">Saved result — not verified as the current output. Open this run’s exact version below; it may differ from your current Canvas.</p>
}

function RunSettings({ canvasId, run }: { canvasId: string; run: RunRecordDto }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<ExecutionManifestDetail>()
  const [error, setError] = useState('')
  useEffect(() => {
    if (!open) return
    let live = true
    void api.executionManifest(canvasId, run.id).then((value) => { if (live) { setDetail(value); setError('') } })
      .catch((caught) => { if (live) setError(caught instanceof Error ? caught.message : String(caught)) })
    return () => { live = false }
  }, [open, canvasId, run.id])
  const bindings = Array.isArray(detail?.document?.parameters) ? detail.document.parameters : []
  const steps = detail?.document?.graph?.nodes ?? []
  return <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)} className="border-t border-border px-4 py-2">
    <summary className="cursor-pointer font-semibold">Settings used for this run</summary>
    {open && <div className="mt-2 space-y-1 text-muted-foreground">
      {error ? <p>Couldn’t load saved settings: {error}</p> : !detail ? <p>Loading…</p>
        : detail.availability !== 'available' ? <p>The saved definition is unavailable. Current settings are not used in its place.</p>
          : <>
            <p>Saved with this run; later edits do not change these values.</p>
            {bindings.length ? <div className="space-y-1 py-2"><strong>Canvas parameters</strong>
              {bindings.map((binding, index) => <div key={index} className="break-words"><strong>{String(binding.name)}</strong>: <code>{JSON.stringify(binding.value)}</code></div>)}
            </div> : <p>No Canvas parameter values were used.</p>}
            {steps.map((value) => {
              const node = asRecord(value)
              if (!node || typeof node.id !== 'string') return null
              const data = asRecord(node.data)
              const config = asRecord(data?.config)
              const fields = Object.entries(config ?? {}).filter(([key]) => !key.startsWith('_') && key !== 'filterBuilder')
              if (!fields.length) return null
              const label = run.perNode?.find((step) => step.nodeId === node.id)?.label || String(data?.title || node.type)
              return <details key={node.id} className="border-t border-border/60 py-1.5">
                <summary className="cursor-pointer text-foreground">{label}</summary>
                <dl className="mt-1 space-y-2">{fields.map(([key, value]) => <div key={key}>
                  <dt className="font-medium">{key.replace(/([a-z])([A-Z])/g, '$1 $2')}</dt>
                  <dd className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/40 p-1.5 font-mono text-[11px]">{typeof value === 'string' ? value : JSON.stringify(value, null, 2)}</dd>
                </div>)}</dl>
              </details>
            })}
          </>}
    </div>}
  </details>
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined
}
