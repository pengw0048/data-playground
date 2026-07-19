import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, KernelError, toMergeColumnsGraph } from '../api/client'
import { roleCanEdit, useStore } from '../store/graph'
import type { DatasetRevisionDetail, MergeColumnRule, MergeColumnsPreflight, MergeColumnsRequest, MergeColumnsTask, WriteReceipt } from '../types/api'
import type { NodeConfig } from '../types/graph'
import { Button } from '@/components/ui/button'

export interface MergeColumnsConfig {
  submissionId?: string
  semanticKey?: string
  submissionState?: 'response_unknown'
  taskId?: string
  identityColumns?: string[]
  rules?: MergeColumnRule[]
}

function configOf(value: NodeConfig): MergeColumnsConfig {
  const raw = value.mergeColumns
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const input = raw as Record<string, unknown>
  return {
    submissionId: typeof input.submissionId === 'string' ? input.submissionId : undefined,
    semanticKey: typeof input.semanticKey === 'string' ? input.semanticKey : undefined,
    submissionState: input.submissionState === 'response_unknown' ? 'response_unknown' : undefined,
    taskId: typeof input.taskId === 'string' ? input.taskId : undefined,
    identityColumns: Array.isArray(input.identityColumns) ? input.identityColumns.filter((item): item is string => typeof item === 'string') : [],
    rules: Array.isArray(input.rules) ? input.rules.flatMap((item) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return []
      const rule = item as Record<string, unknown>
      return typeof rule.source === 'string' && typeof rule.target === 'string'
        && (rule.mode === 'add' || rule.mode === 'replace') ? [{ source: rule.source, target: rule.target, mode: rule.mode }] : []
    }) : [],
  }
}

function newSubmissionId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `merge-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function cleanError(caught: unknown): string {
  const message = caught instanceof Error ? caught.message : String(caught)
  // API diagnostics are already sanitized.  Do not decorate them with request or storage detail.
  return message || 'Column merge could not be admitted.'
}

function submissionDefinitelyRejected(caught: unknown): boolean {
  // A normal 4xx response proves that this POST did not create the requested Task. Network errors
  // and 5xx responses do not: the server may have committed before the response was lost.
  return caught instanceof KernelError && caught.status >= 400 && caught.status < 500
}

function phaseLabel(task?: MergeColumnsTask | null): string {
  const phase = task?.mergeColumns?.phase
  if (!phase) return task?.status ?? 'not submitted'
  return phase.replaceAll('_', ' ')
}

function countLabel(value: number): string { return value.toLocaleString() }

function semanticKey(request: MergeColumnsRequest): string {
  // Matches the consumer-visible admission meaning while excluding Write-card UI bookkeeping.
  // This is a fence, not a replacement for the server's canonical semantics.
  const graph = request.graph as { id: string; nodes: Array<{ id: string; type: string; data?: { title?: string; config?: Record<string, unknown>; bypassed?: boolean; disabled?: boolean } }>; edges: Array<{ source: string; target: string; sourceHandle?: string | null; targetHandle?: string | null }> }
  return JSON.stringify({
    graph: {
      id: graph.id,
      nodes: graph.nodes.map((node) => {
        const config = { ...(node.data?.config ?? {}) }
        delete config.mergeColumns
        // SinkSpec uses the Write title only when neither filename nor name is configured. Keep
        // that exact fallback in the client fence so a rename cannot reuse an eligible preflight
        // or submission id for a different destination, without treating unrelated card titles as
        // execution semantics.
        const fallbackTitle = node.type === 'write' && !config.filename && !config.name
          ? node.data?.title ?? '' : undefined
        return { id: node.id, type: node.type, config, ...(fallbackTitle !== undefined ? { title: fallbackTitle } : {}), bypassed: !!node.data?.bypassed, disabled: !!node.data?.disabled }
      }),
      edges: graph.edges.map((edge) => [edge.source, edge.target, edge.sourceHandle ?? null, edge.targetHandle ?? null]),
    },
    identityColumns: request.identityColumns, rules: request.rules,
  })
}

function requestKey(request: MergeColumnsRequest): string {
  return JSON.stringify({ semantic: semanticKey(request), submissionId: request.submissionId })
}

function ExactReceipt({ receipt }: { receipt: WriteReceipt }) {
  const [detail, setDetail] = useState<DatasetRevisionDetail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const open = async () => {
    setLoading(true); setError('')
    try {
      // Exact revision only.  A compacted/unavailable revision remains an honest error; never
      // silently substitute the moving catalog head.
      setDetail(await api.datasetRevision(receipt.datasetId, receipt.revisionId))
    } catch (caught) { setError(cleanError(caught)) } finally { setLoading(false) }
  }
  return <div className="mt-2 rounded-md border border-emerald-500/30 bg-emerald-500/5 p-2 text-[10.5px] text-muted-foreground">
    <div className="font-semibold text-foreground">Published exact revision</div>
    <div className="mt-0.5 break-all font-mono">{receipt.datasetId}@{receipt.revisionId}</div>
    <div>{countLabel(receipt.rows)} rows · {countLabel(receipt.bytes)} bytes</div>
    <Button size="sm" variant="outline" className="mt-1 h-6 px-2 text-[10px]" onClick={() => void open()} disabled={loading}>
      {loading ? 'Opening exact revision…' : 'Open exact revision'}
    </Button>
    {error && <div role="alert" className="mt-1 text-destructive">Exact revision unavailable: {error}</div>}
    {detail && <div aria-label="Exact revision detail" className="mt-2 rounded border border-border bg-muted/30 p-2">
      <div>Committed {detail.committedAt ? new Date(detail.committedAt).toLocaleString() : 'at an unknown time'} · {detail.summary.rowCount == null ? 'row count unavailable' : `${countLabel(detail.summary.rowCount)} rows`}</div>
      <div>{detail.parentRevisionId ? <>Parent <span className="font-mono">{detail.parentRevisionId}</span></> : 'No parent revision'} · {detail.preview.columns.length} fields</div>
      <div className="mt-1 overflow-auto"><table className="w-full text-left"><thead><tr>{detail.preview.columns.map((field) => <th key={field.name} className="pr-2 font-medium">{field.name}</th>)}</tr></thead><tbody>{detail.preview.rows.slice(0, 5).map((row, index) => <tr key={index}>{detail.preview.columns.map((field) => <td key={field.name} className="max-w-24 truncate pr-2 font-mono">{String(row[field.name] ?? '')}</td>)}</tr>)}</tbody></table></div>
      {detail.preview.hasMore && <div className="mt-1">Preview is bounded; this remains the exact published revision.</div>}
    </div>}
  </div>
}

/**
 * The one UI surface for the certified local merge.  It has no client-side eligibility logic:
 * configuration is persisted on the existing Write card, whereas every correctness statement
 * comes from #583 preflight/admission.
 */
export function MergeColumnsControl({ nodeId, compact = false }: { nodeId: string; compact?: boolean }) {
  const node = useStore((state) => state.doc.nodes.find((item) => item.id === nodeId))
  const doc = useStore((state) => state.doc)
  const updateConfig = useStore((state) => state.updateConfig)
  const canEdit = useStore((state) => roleCanEdit(state.canvasRole))
  const setJobsQuery = useStore((state) => state.setJobsQuery)
  const config = configOf((node?.data.config ?? {}) as NodeConfig)
  const [preflight, setPreflight] = useState<MergeColumnsPreflight | null>(null)
  const [preflightKey, setPreflightKey] = useState<string | null>(null)
  const [task, setTask] = useState<MergeColumnsTask | null>(null)
  const [receipt, setReceipt] = useState<WriteReceipt | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<'preflight' | 'submit' | 'cancel' | 'retry' | 'retarget' | null>(null)
  const actionGeneration = useRef(0)
  const taskGeneration = useRef(0)
  const actionBusy = useRef(false)

  const requestFor = useCallback((next = config): MergeColumnsRequest | null => {
    if (!node) return null
    return {
      graph: toMergeColumnsGraph(doc, nodeId),
      submissionId: next.submissionId ?? newSubmissionId(),
      identityColumns: next.identityColumns ?? [], rules: next.rules ?? [],
    }
  }, [config, doc, node, nodeId])
  const currentRequest = requestFor(config)
  const currentRequestKey = currentRequest ? requestKey(currentRequest) : null
  const currentSemanticKey = currentRequest ? semanticKey(currentRequest) : null

  const persist = useCallback((next: MergeColumnsConfig) => {
    updateConfig(nodeId, { mergeColumns: next })
  }, [nodeId, updateConfig])
  const withSubmission = useCallback(() => {
    const candidate = requestFor(config)
    if (!candidate) return config
    const currentSemanticKey = semanticKey(candidate)
    // A response-loss retry keeps precisely the same frozen consumer meaning and submission id.
    // Any Source/Select/Write or merge-rule change gets a new id before it reaches the API.
    if (config.submissionState === 'response_unknown'
        || config.submissionId && config.semanticKey === currentSemanticKey) return config
    const next = {
      ...config,
      submissionId: newSubmissionId(),
      semanticKey: currentSemanticKey,
      // A new semantic request must never inherit an ambiguous response from an old one.
      submissionState: undefined,
      taskId: undefined,
    }
    persist(next)
    return next
  }, [config, persist, requestFor])

  const check = useCallback(async () => {
    const sequence = ++actionGeneration.current
    const next = withSubmission()
    const request = requestFor(next)
    if (!request) return
    const key = requestKey(request)
    setBusy('preflight'); setError(''); setPreflight(null); setPreflightKey(null)
    try {
      const result = await api.mergeColumnsPreflight(request)
      if (sequence === actionGeneration.current) { setPreflight(result); setPreflightKey(key) }
    } catch (caught) { if (sequence === actionGeneration.current) setError(cleanError(caught)) }
    finally { if (sequence === actionGeneration.current) setBusy(null) }
  }, [requestFor, withSubmission])

  const loadTask = useCallback(async (taskId: string) => {
    if (actionBusy.current) return
    const sequence = ++taskGeneration.current
    try {
      const result = await api.mergeColumnsTask(taskId)
      if (sequence !== taskGeneration.current || actionBusy.current) return
      setError('')
      setTask(result)
      if (result.status === 'done') {
        // Dedicated task status intentionally excludes receipt.  The existing, authorized Jobs
        // projection is the sole bridge from durable task to ordinary typed Write receipt.
        const page = await api.workspaceJobs({ runId: taskId, limit: 1 })
        if (sequence === taskGeneration.current && !actionBusy.current) setReceipt(page.items[0]?.outputReceipt ?? null)
      }
    } catch (caught) { if (sequence === taskGeneration.current && !actionBusy.current) setError(cleanError(caught)) }
  }, [])

  useEffect(() => {
    if (!config.taskId) { setTask(null); setReceipt(null); return }
    void loadTask(config.taskId)
    return () => { taskGeneration.current += 1 }
  }, [config.taskId, loadTask])
  useEffect(() => {
    if (!config.taskId || !task || ['done', 'failed', 'cancelled'].includes(task.status)) return
    const timer = window.setInterval(() => { void loadTask(config.taskId!) }, 1500)
    return () => window.clearInterval(timer)
  }, [config.taskId, loadTask, task])
  // A Source/Select/destination edit can occur outside this inspector.  Its former preflight is
  // useful only as history, never as authority to submit a changed graph.
  useEffect(() => {
    if (preflightKey && preflightKey !== currentRequestKey) {
      setPreflight(null); setPreflightKey(null)
    }
  }, [currentRequestKey, preflightKey])

  const submit = async () => {
    const next = withSubmission()
    const request = requestFor(next)
    if (!request) return
    const key = requestKey(request)
    if (!preflight?.eligible || preflightKey !== key) {
      setError('Check the current exact graph before submitting this column merge.')
      return
    }
    const sequence = ++actionGeneration.current
    taskGeneration.current += 1
    actionBusy.current = true
    setBusy('submit'); setError('')
    try {
      // Persist before the POST: a tab close or lost response can safely replay this exact id.
      persist({ ...next, submissionState: 'response_unknown' })
      const result = await api.submitMergeColumns(request)
      if (sequence !== actionGeneration.current) return
      setTask(result)
      persist({ ...next, taskId: result.taskId, submissionState: undefined })
    } catch (caught) {
      if (sequence === actionGeneration.current) {
        if (submissionDefinitelyRejected(caught)) {
          persist({ ...next, submissionState: undefined })
          setPreflight(null); setPreflightKey(null)
        }
        setError(cleanError(caught))
      }
    } finally { if (sequence === actionGeneration.current) { actionBusy.current = false; setBusy(null) } }
  }
  const recover = async () => {
    const request = requestFor(config)
    if (!request || config.submissionState !== 'response_unknown'
        || config.semanticKey !== semanticKey(request)) return
    const sequence = ++actionGeneration.current
    taskGeneration.current += 1
    actionBusy.current = true
    setBusy('submit'); setError('')
    try {
      const result = await api.submitMergeColumns(request)
      if (sequence !== actionGeneration.current) return
      setTask(result)
      persist({ ...config, taskId: result.taskId, submissionState: undefined })
    } catch (caught) {
      if (sequence === actionGeneration.current) {
        if (submissionDefinitelyRejected(caught)) {
          persist({ ...config, submissionState: undefined })
          setPreflight(null); setPreflightKey(null)
        }
        setError(cleanError(caught))
      }
    } finally { if (sequence === actionGeneration.current) { actionBusy.current = false; setBusy(null) } }
  }
  const cancel = async () => {
    if (!config.taskId) return
    const sequence = ++actionGeneration.current
    taskGeneration.current += 1
    actionBusy.current = true
    setBusy('cancel'); setError('')
    try { const result = await api.cancelMergeColumnsTask(config.taskId); if (sequence === actionGeneration.current) setTask(result) } catch (caught) { if (sequence === actionGeneration.current) setError(cleanError(caught)) } finally { if (sequence === actionGeneration.current) { actionBusy.current = false; setBusy(null) } }
  }
  const retry = async () => {
    if (!config.taskId) return
    const sequence = ++actionGeneration.current
    taskGeneration.current += 1
    actionBusy.current = true
    setBusy('retry'); setError('')
    try { const result = await api.retryMergeColumnsTask(config.taskId, newSubmissionId()); if (sequence === actionGeneration.current) setTask(result) } catch (caught) { if (sequence === actionGeneration.current) setError(cleanError(caught)) } finally { if (sequence === actionGeneration.current) { actionBusy.current = false; setBusy(null) } }
  }
  const reAdmit = () => {
    actionGeneration.current += 1
    taskGeneration.current += 1
    const candidate = requestFor(config)
    persist({ ...config, submissionId: newSubmissionId(), semanticKey: candidate ? semanticKey(candidate) : undefined, submissionState: undefined, taskId: undefined })
    setTask(null); setReceipt(null); setPreflight(null); setPreflightKey(null); setError('')
  }
  const useCurrentHead = async () => {
    const source = toMergeColumnsGraph(doc, nodeId).nodes.find((item: any) => item.type === 'source') as any
    const sourceConfig = source?.data?.config as Record<string, unknown> | undefined
    const ref = sourceConfig?.datasetRef as { kind?: string; datasetId?: string } | undefined
    if (!source || ref?.kind !== 'exact' || !ref.datasetId) {
      setError('Choose one exact Source revision before selecting the current head.')
      return
    }
    const sequence = ++actionGeneration.current
    taskGeneration.current += 1
    actionBusy.current = true
    setBusy('retarget'); setError('')
    try {
      // This is deliberately user-triggered.  RevisionControl alone changes only datasetRef; a
      // managed-local exact Source also needs the catalog’s current artifact URI to become an
      // admissible, matching Source.  We never perform this retarget during ordinary preflight.
      const table = await api.tableByRegistration(ref.datasetId)
      const current = await api.resolveDatasetRevision(table.id)
      if (sequence !== actionGeneration.current) return
      updateConfig(source.id, {
        uri: table.uri, tableId: table.id,
        datasetRef: { kind: 'exact', datasetId: current.datasetId, revisionId: current.revisionId,
          lastKnown: { committedAt: current.committedAt ?? null } },
      })
      persist({ ...config, submissionId: newSubmissionId(), semanticKey: undefined, submissionState: undefined, taskId: undefined })
      setTask(null); setReceipt(null); setPreflight(null); setPreflightKey(null)
    } catch (caught) { if (sequence === actionGeneration.current) setError(cleanError(caught)) }
    finally { if (sequence === actionGeneration.current) { actionBusy.current = false; setBusy(null) } }
  }
  const changed = (next: MergeColumnsConfig) => {
    // Every mounted surface observes this persisted fence. Do not let another Inspector/Run panel
    // abandon an in-flight submission whose response may still establish a durable Task.
    if (actionBusy.current || config.submissionState === 'response_unknown') return
    actionGeneration.current += 1
    taskGeneration.current += 1
    // Changed intent cannot replay a prior submission.  A response-loss retry is the only path
    // that retains this identifier, and it leaves config untouched.
    const request = requestFor(next)
    persist({ ...next, submissionId: newSubmissionId(), semanticKey: request ? semanticKey(request) : undefined, submissionState: undefined, taskId: undefined })
    setPreflight(null); setPreflightKey(null); setTask(null); setReceipt(null); setError(''); setBusy(null)
  }
  const changeIdentities = (value: string) => {
    const identityColumns = value.split(',').map((item) => item.trim()).filter(Boolean)
    changed({ ...config, identityColumns })
  }
  const changeRule = (index: number, patch: Partial<MergeColumnRule>) => {
    const rules = [...(config.rules ?? [])]
    rules[index] = { ...rules[index]!, ...patch }
    changed({ ...config, rules })
  }
  const addRule = () => changed({ ...config, rules: [...(config.rules ?? []), { source: '', target: '', mode: 'add' }] })
  const removeRule = (index: number) => changed({ ...config, rules: (config.rules ?? []).filter((_, item) => item !== index) })
  const taskTerminal = task && ['done', 'failed', 'cancelled'].includes(task.status)
  // The durable diagnostic is authoritative. The current #583 preflight contract has no
  // operation-specific code, so recognize only its exact public head-mismatch detail; never infer
  // a destination move from an unrelated message that merely contains "stale".
  const staleHead = task?.mergeColumns?.diagnosticCode === 'stale_expected_head'
    || error === 'merge-columns destination head must equal the exact Source revision'
  const responseUnknown = !task && config.submissionState === 'response_unknown'
  const recoveryAvailable = responseUnknown
    && !!config.submissionId && config.semanticKey === currentSemanticKey
  const trackedTaskPending = !!config.taskId && !task
  const intentLocked = (busy !== null && busy !== 'preflight') || responseUnknown || trackedTaskPending
    || (!!task && !taskTerminal)

  if (!node) return null
  return <div aria-label="Certified column merge" className={compact ? 'mt-2 text-[10.5px]' : 'mt-3 rounded-md border border-border bg-muted/30 p-2'}>
    {!compact && <div className="font-semibold text-[11px] text-foreground">Add or replace columns</div>}
    {!compact && <div className="mt-0.5 text-[10.5px] leading-snug text-muted-foreground">Exact local Source → Select → Write only. Preflight proves identity coverage and the current head.</div>}
    {!compact && <label className="mt-2 block text-[10.5px] text-muted-foreground">Identity columns
      <input aria-label="Merge identity columns" value={(config.identityColumns ?? []).join(', ')} disabled={!canEdit || intentLocked}
        onChange={(event) => changeIdentities(event.target.value)} placeholder="id, frame_id" className="mt-1 h-7 w-full rounded border border-border bg-background px-2 text-[11px] text-foreground" />
    </label>}
    {!compact && <div className="mt-2 space-y-1">
      <div className="text-[10.5px] text-muted-foreground">Selected payload → destination column</div>
      {(config.rules ?? []).map((rule, index) => <div key={index} className="grid grid-cols-[1fr_74px_1fr_24px] gap-1">
        <input aria-label={`Merge source column ${index + 1}`} value={rule.source} disabled={!canEdit || intentLocked} onChange={(event) => changeRule(index, { source: event.target.value })} placeholder="source" className="h-7 min-w-0 rounded border border-border bg-background px-1.5 text-[10.5px]" />
        <select aria-label={`Merge mode ${index + 1}`} value={rule.mode} disabled={!canEdit || intentLocked} onChange={(event) => changeRule(index, { mode: event.target.value as MergeColumnRule['mode'] })} className="h-7 rounded border border-border bg-background px-1 text-[10px]"><option value="add">add</option><option value="replace">replace</option></select>
        <input aria-label={`Merge target column ${index + 1}`} value={rule.target} disabled={!canEdit || intentLocked} onChange={(event) => changeRule(index, { target: event.target.value })} placeholder="target" className="h-7 min-w-0 rounded border border-border bg-background px-1.5 text-[10.5px]" />
        <button aria-label={`Remove merge rule ${index + 1}`} disabled={!canEdit || intentLocked} onClick={() => removeRule(index)} className="text-muted-foreground hover:text-destructive">×</button>
      </div>)}
      <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" onClick={addRule} disabled={!canEdit || intentLocked}>Add mapping</Button>
    </div>}
    {preflight && <PreflightSummary value={preflight} />}
    {task && <div className="mt-2 rounded border border-border bg-background p-2 text-[10.5px] text-muted-foreground">
      <div className="font-semibold text-foreground">{phaseLabel(task)}</div>
      {task.mergeColumns && <div className="mt-0.5">Candidate {task.mergeColumns.candidate}{task.mergeColumns.reused ? ' · reused' : ''}{task.mergeColumns.candidateRows != null ? ` · ${countLabel(task.mergeColumns.candidateRows)} rows` : ''}</div>}
      {!compact && <div className="mt-1 flex gap-1"><Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" onClick={() => setJobsQuery(new URLSearchParams({ run: task.taskId }).toString())}>Open in Jobs</Button>
        {task.canCancel && <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" onClick={() => void cancel()} disabled={!canEdit || busy !== null}>Cancel</Button>}
        {task.canRetry && <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" onClick={() => void retry()} disabled={!canEdit || busy !== null}>Retry</Button>}</div>}
    </div>}
    {receipt && <ExactReceipt receipt={receipt} />}
    {trackedTaskPending && <div className="mt-2 rounded border border-border bg-background p-2 text-[10.5px] text-muted-foreground">
      <div className="font-semibold text-foreground">Tracked durable Task</div>
      <div className="mt-0.5">Its current state is loading or temporarily unavailable. This is not treated as a new admission.</div>
      <Button size="sm" variant="outline" className="mt-1 h-6 px-2 text-[10px]" onClick={() => setJobsQuery(new URLSearchParams({ run: config.taskId! }).toString())}>Open in Jobs</Button>
    </div>}
    {responseUnknown && !recoveryAvailable && <div className="mt-2 rounded border border-amber-500/30 bg-amber-500/5 p-2 text-[10.5px] text-muted-foreground">
      <div className="font-semibold text-foreground">Previous submission outcome is unresolved</div>
      <div className="mt-0.5">The graph changed after submission. A second admission is blocked; undo those edits to recover the same submission, or inspect Jobs for its durable outcome.</div>
      <Button size="sm" variant="outline" className="mt-1 h-6 px-2 text-[10px]" onClick={() => setJobsQuery('')}>Open Jobs</Button>
    </div>}
    {error && <div role="alert" className="mt-2 text-[10.5px] leading-snug text-destructive">{error}</div>}
    {!compact && <div className="mt-2 flex gap-1">
      {!task && !config.taskId && !responseUnknown && <Button size="sm" variant="outline" className="h-7 flex-1 text-[10.5px]" onClick={() => void check()} disabled={!canEdit || busy !== null}>{busy === 'preflight' ? 'Checking…' : 'Check eligibility'}</Button>}
      {!task && !config.taskId && !responseUnknown && <Button size="sm" className="h-7 flex-1 text-[10.5px]" onClick={() => void submit()} disabled={!canEdit || busy !== null || preflight?.eligible !== true || preflightKey !== currentRequestKey}>{busy === 'submit' ? 'Submitting…' : 'Run column merge'}</Button>}
      {recoveryAvailable && <Button size="sm" className="h-7 flex-1 text-[10.5px]" onClick={() => void recover()} disabled={!canEdit || busy !== null}>{busy === 'submit' ? 'Recovering…' : 'Recover previous submission'}</Button>}
      {taskTerminal && <Button size="sm" variant="outline" className="h-7 text-[10.5px]" onClick={reAdmit} disabled={!canEdit || busy !== null}>Start new admission</Button>}
      {staleHead && <div className="flex flex-col gap-1"><span className="text-[10px] text-muted-foreground">The destination moved. Nothing has been retargeted. Choose one of the explicit actions below, then check again; this never rebases automatically.</span><div className="flex gap-1"><Button size="sm" variant="outline" className="h-7 text-[10.5px]" onClick={() => void useCurrentHead()} disabled={!canEdit || busy !== null}>Use current head and recompute</Button><Button size="sm" variant="outline" className="h-7 text-[10.5px]" onClick={reAdmit} disabled={!canEdit || busy !== null}>Reset for a new admission</Button></div></div>}
    </div>}
  </div>
}

function PreflightSummary({ value }: { value: MergeColumnsPreflight }) {
  const c = value.coverage
  return <div aria-label="Merge preflight" className="mt-2 rounded border border-border bg-background p-2 text-[10.5px] text-muted-foreground">
    <div className="font-semibold text-foreground">{value.eligible ? 'Eligible exact merge' : 'Not eligible'}</div>
    <div className="mt-0.5 break-all font-mono">base {value.base.datasetId}@{value.base.revisionId}</div>
    <div>Identity order: <span className="font-mono">{value.identityColumns.join(', ') || 'none'}</span></div>
    <div>Declared key suggestion (not verified): {value.declaredKey.length ? <span className="font-mono">{value.declaredKey.join(', ')}</span> : 'none'}</div>
    <div>Base coverage: {countLabel(c.base.rows)} rows · {countLabel(c.base.uniqueIdentities)} unique · {countLabel(c.base.nullRows)} null · {countLabel(c.base.duplicateGroups)} duplicate groups ({countLabel(c.base.duplicateRows)} rows)</div>
    <div>Candidate coverage: {countLabel(c.candidate.rows)} rows · {countLabel(c.candidate.uniqueIdentities)} unique · {countLabel(c.candidate.nullRows)} null · {countLabel(c.candidate.duplicateGroups)} duplicate groups ({countLabel(c.candidate.duplicateRows)} rows)</div>
    <div>Coverage: {c.status} · {countLabel(c.matchedIdentities)} matched · {countLabel(c.missingIdentities)} missing · {countLabel(c.extraIdentities)} extra</div>
    <div>Mapping: {value.rules.map((rule) => `${rule.source} → ${rule.target} (${rule.mode})`).join('; ') || 'none'}</div>
    <div>Expected head: <span className="font-mono">{value.expectedHead.revisionId}</span></div>
    <div>Output schema: {value.outputSchema.length ? value.outputSchema.map((field) => `${field.name}: ${field.type}`).join(', ') : 'no fields'}</div>
    <div>Evidence: {value.provenance.producer} · {value.provenance.source} · {value.provenance.selectKind} v{value.provenance.selectVersion}</div>
  </div>
}
