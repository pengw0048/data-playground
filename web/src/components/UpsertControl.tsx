import { useCallback, useEffect, useRef, useState } from 'react'
import { api, KernelError } from '../api/client'
import { roleCanEdit, useStore } from '../store/graph'
import type { DatasetRevisionDetail, UpsertPreflight, UpsertRequest, UpsertTask, WriteReceipt } from '../types/api'
import type { NodeConfig } from '../types/graph'
import { Button } from '@/components/ui/button'

export interface KeyedUpsertConfig {
  submissionId?: string
  semanticKey?: string
  submissionState?: 'response_unknown'
  taskId?: string
  keys?: string[]
}

interface ResolvedIntent {
  datasetId: string
  expectedHeadRevisionId: string
  payloadDatasetId: string
  payloadRevisionId: string
  keys: string[]
}

function configOf(value: NodeConfig): KeyedUpsertConfig {
  const raw = value.keyedUpsert
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const input = raw as Record<string, unknown>
  return {
    submissionId: typeof input.submissionId === 'string' ? input.submissionId : undefined,
    semanticKey: typeof input.semanticKey === 'string' ? input.semanticKey : undefined,
    submissionState: input.submissionState === 'response_unknown' ? 'response_unknown' : undefined,
    taskId: typeof input.taskId === 'string' ? input.taskId : undefined,
    keys: Array.isArray(input.keys) ? input.keys.filter((item): item is string => typeof item === 'string') : [],
  }
}

function newSubmissionId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `upsert-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function cleanError(caught: unknown): string {
  const message = caught instanceof Error ? caught.message : String(caught)
  return message || 'Keyed upsert could not be admitted.'
}

function submissionDefinitelyRejected(caught: unknown): boolean {
  // A 4xx proves this POST created no Task; network/5xx may have committed before the response was lost.
  return caught instanceof KernelError && caught.status >= 400 && caught.status < 500
}

function countLabel(value: number): string { return value.toLocaleString() }

function semanticKey(intent: ResolvedIntent): string {
  return JSON.stringify({
    datasetId: intent.datasetId, expectedHead: intent.expectedHeadRevisionId,
    payloadDatasetId: intent.payloadDatasetId, payloadRevisionId: intent.payloadRevisionId,
    keys: intent.keys,
  })
}

function requestFrom(intent: ResolvedIntent, submissionId: string): UpsertRequest {
  return {
    submissionId, datasetId: intent.datasetId,
    expectedHeadRevisionId: intent.expectedHeadRevisionId,
    payloadDatasetId: intent.payloadDatasetId, payloadRevisionId: intent.payloadRevisionId,
    keys: intent.keys,
  }
}

function ExactRevision({ receipt }: { receipt: WriteReceipt }) {
  const [detail, setDetail] = useState<DatasetRevisionDetail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const open = async () => {
    setLoading(true); setError('')
    try {
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
      <div>{detail.parentRevisionId ? <>Parent <span className="font-mono">{detail.parentRevisionId}</span></> : 'No parent revision'} · {detail.preview.columns.length} fields</div>
    </div>}
  </div>
}

function EvidenceSummary({ evidence, base, expectedHead, keys, schema, eligible }: {
  evidence: UpsertPreflight['evidence']; base: string; expectedHead: string; keys: string[]
  schema?: UpsertPreflight['outputSchema']; eligible?: boolean
}) {
  return <div aria-label="Upsert projection" className="mt-2 rounded border border-border bg-background p-2 text-[10.5px] text-muted-foreground">
    {eligible !== undefined && <div className="font-semibold text-foreground">{eligible ? 'Eligible keyed upsert' : 'Not eligible'}</div>}
    <div className="mt-0.5 break-all font-mono">base {base}</div>
    <div>Keys: <span className="font-mono">{keys.join(', ') || 'none'}</span></div>
    <div>{countLabel(evidence.matched)} matched · {countLabel(evidence.inserted)} inserted · {countLabel(evidence.unchanged)} unchanged</div>
    <div>{countLabel(evidence.rejected)} rejected · {countLabel(evidence.duplicate)} duplicate · {countLabel(evidence.conflict)} conflict</div>
    <div>Expected head: <span className="font-mono">{expectedHead}</span></div>
    {schema && <div>Output schema: {schema.length ? schema.map((field) => `${field.name}: ${field.type}`).join(', ') : 'no fields'}</div>}
  </div>
}

/**
 * The one UI surface for the certified keyed upsert. It has no client-side eligibility logic:
 * every correctness statement comes from #637 preflight/admission. It renders only for the supported
 * workflow — an exact managed-local Source directly feeding a managed-local-file Write with a head.
 */
export function UpsertControl({ nodeId }: { nodeId: string }) {
  const node = useStore((state) => state.doc.nodes.find((item) => item.id === nodeId))
  const edges = useStore((state) => state.doc.edges)
  const nodes = useStore((state) => state.doc.nodes)
  const updateConfig = useStore((state) => state.updateConfig)
  const canEdit = useStore((state) => roleCanEdit(state.canvasRole))
  const admission = useStore((state) => state.runs[nodeId]?.writeAdmission)
  const config = configOf((node?.data.config ?? {}) as NodeConfig)

  const [preflight, setPreflight] = useState<UpsertPreflight | null>(null)
  const [preflightKey, setPreflightKey] = useState<string | null>(null)
  const [task, setTask] = useState<UpsertTask | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<'preflight' | 'submit' | 'cancel' | 'retry' | null>(null)
  const actionGeneration = useRef(0)
  const taskGeneration = useRef(0)
  const actionBusy = useRef(false)

  // Resolve the frozen refs entirely from the write admission (base/head) and the single exact
  // Source upstream (payload). No graph is sent; the API takes exact revisions.
  const resolve = useCallback((): ResolvedIntent | null => {
    if (!node || node.type !== 'write' || admission?.provider !== 'managed-local-file' || !admission.expectedHead) return null
    const upstream = edges.filter((edge) => edge.target === nodeId).map((edge) => edge.source)
    if (upstream.length !== 1) return null
    const source = nodes.find((item) => item.id === upstream[0])
    const ref = (source?.data.config as NodeConfig | undefined)?.datasetRef as
      { kind?: string; datasetId?: string; revisionId?: string } | undefined
    if (source?.type !== 'source' || ref?.kind !== 'exact' || !ref.datasetId || !ref.revisionId) return null
    return {
      datasetId: admission.expectedHead.datasetId,
      expectedHeadRevisionId: admission.expectedHead.revisionId,
      payloadDatasetId: ref.datasetId, payloadRevisionId: ref.revisionId,
      keys: config.keys ?? [],
    }
  }, [admission, config.keys, edges, node, nodeId, nodes])

  const intent = resolve()
  const currentSemanticKey = intent ? semanticKey(intent) : null

  const persist = useCallback((next: KeyedUpsertConfig) => {
    updateConfig(nodeId, { keyedUpsert: next })
  }, [nodeId, updateConfig])

  const loadTask = useCallback(async (taskId: string) => {
    if (actionBusy.current) return
    const sequence = ++taskGeneration.current
    try {
      const result = await api.upsertTask(taskId)
      if (sequence === taskGeneration.current && !actionBusy.current) { setError(''); setTask(result) }
    } catch (caught) { if (sequence === taskGeneration.current && !actionBusy.current) setError(cleanError(caught)) }
  }, [])

  useEffect(() => {
    if (!config.taskId) { setTask(null); return }
    void loadTask(config.taskId)
    return () => { taskGeneration.current += 1 }
  }, [config.taskId, loadTask])
  useEffect(() => {
    if (!config.taskId || !task || ['done', 'failed', 'cancelled'].includes(task.status)) return
    const timer = window.setInterval(() => { void loadTask(config.taskId!) }, 1500)
    return () => window.clearInterval(timer)
  }, [config.taskId, loadTask, task])
  useEffect(() => {
    // Only a genuinely different resolved intent invalidates a prior preflight. A transient null
    // (e.g. the write admission is briefly recomputing after an autosave) must not clear it.
    if (preflightKey && currentSemanticKey && preflightKey !== currentSemanticKey) {
      setPreflight(null); setPreflightKey(null)
    }
  }, [currentSemanticKey, preflightKey])

  const withSubmission = useCallback((): KeyedUpsertConfig => {
    if (!intent) return config
    const key = semanticKey(intent)
    if (config.submissionState === 'response_unknown' || (config.submissionId && config.semanticKey === key)) return config
    const next = { ...config, submissionId: newSubmissionId(), semanticKey: key, submissionState: undefined, taskId: undefined }
    persist(next)
    return next
  }, [config, intent, persist])

  const check = async () => {
    if (!intent) return
    const sequence = ++actionGeneration.current
    const key = semanticKey(intent)
    // Preflight is side-effect-free: it never rotates or persists a submission id (the server ignores
    // one), so this cannot trigger an autosave that would race the in-flight result.
    setBusy('preflight'); setError(''); setPreflight(null); setPreflightKey(null)
    try {
      const result = await api.upsertPreflight(requestFrom(intent, config.submissionId ?? newSubmissionId()))
      if (sequence === actionGeneration.current) { setPreflight(result); setPreflightKey(key) }
    } catch (caught) { if (sequence === actionGeneration.current) setError(cleanError(caught)) }
    finally { if (sequence === actionGeneration.current) setBusy(null) }
  }

  const submit = async () => {
    if (!intent) return
    const key = semanticKey(intent)
    if (!preflight?.eligible || preflightKey !== key) {
      setError('Check the current exact upsert before running it.')
      return
    }
    const next = withSubmission()
    const sequence = ++actionGeneration.current
    taskGeneration.current += 1
    actionBusy.current = true
    setBusy('submit'); setError('')
    try {
      persist({ ...next, submissionState: 'response_unknown' })
      const result = await api.submitUpsert(requestFrom(intent, next.submissionId ?? newSubmissionId()))
      if (sequence !== actionGeneration.current) return
      setTask(result)
      persist({ ...next, taskId: result.taskId, submissionState: undefined })
    } catch (caught) {
      if (sequence === actionGeneration.current) {
        if (submissionDefinitelyRejected(caught)) { persist({ ...next, submissionState: undefined }); setPreflight(null); setPreflightKey(null) }
        setError(cleanError(caught))
      }
    } finally { if (sequence === actionGeneration.current) { actionBusy.current = false; setBusy(null) } }
  }

  const recover = async () => {
    if (!intent || config.submissionState !== 'response_unknown' || config.semanticKey !== semanticKey(intent) || !config.submissionId) return
    const sequence = ++actionGeneration.current
    taskGeneration.current += 1
    actionBusy.current = true
    setBusy('submit'); setError('')
    try {
      const result = await api.submitUpsert(requestFrom(intent, config.submissionId))
      if (sequence !== actionGeneration.current) return
      setTask(result)
      persist({ ...config, taskId: result.taskId, submissionState: undefined })
    } catch (caught) {
      if (sequence === actionGeneration.current) {
        if (submissionDefinitelyRejected(caught)) { persist({ ...config, submissionState: undefined }) }
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
    try { const result = await api.cancelUpsertTask(config.taskId); if (sequence === actionGeneration.current) setTask(result) }
    catch (caught) { if (sequence === actionGeneration.current) setError(cleanError(caught)) }
    finally { if (sequence === actionGeneration.current) { actionBusy.current = false; setBusy(null) } }
  }

  const retry = async () => {
    if (!config.taskId) return
    const sequence = ++actionGeneration.current
    taskGeneration.current += 1
    actionBusy.current = true
    setBusy('retry'); setError('')
    try { const result = await api.retryUpsertTask(config.taskId, newSubmissionId()); if (sequence === actionGeneration.current) setTask(result) }
    catch (caught) { if (sequence === actionGeneration.current) setError(cleanError(caught)) }
    finally { if (sequence === actionGeneration.current) { actionBusy.current = false; setBusy(null) } }
  }

  const reAdmit = () => {
    actionGeneration.current += 1
    taskGeneration.current += 1
    persist({ ...config, submissionId: newSubmissionId(), semanticKey: intent ? semanticKey(intent) : undefined, submissionState: undefined, taskId: undefined })
    setTask(null); setPreflight(null); setPreflightKey(null); setError('')
  }

  const changeKeys = (value: string) => {
    if (actionBusy.current || config.submissionState === 'response_unknown') return
    actionGeneration.current += 1
    taskGeneration.current += 1
    const keys = value.split(',').map((item) => item.trim()).filter(Boolean)
    const next = { ...config, keys }
    const resolved = intent ? { ...intent, keys } : null
    persist({ ...next, submissionId: newSubmissionId(), semanticKey: resolved ? semanticKey(resolved) : undefined, submissionState: undefined, taskId: undefined })
    setPreflight(null); setPreflightKey(null); setTask(null); setError(''); setBusy(null)
  }

  if (!node || node.type !== 'write') return null
  // Never advertise the mode for an unsupported destination or graph shape.
  if (!intent && !config.taskId && !config.keys?.length) return null

  const taskTerminal = task && ['done', 'failed', 'cancelled'].includes(task.status)
  const staleHead = task?.diagnosticCode === 'stale_expected_head'
  const responseUnknown = !task && config.submissionState === 'response_unknown'
  const recoveryAvailable = responseUnknown && !!config.submissionId && config.semanticKey === currentSemanticKey
  const trackedTaskPending = !!config.taskId && !task
  const intentLocked = (busy !== null && busy !== 'preflight') || responseUnknown || trackedTaskPending || (!!task && !taskTerminal)

  return <div aria-label="Certified keyed upsert" className="mt-3 rounded-md border border-border bg-muted/30 p-2">
    <div className="font-semibold text-[11px] text-foreground">Keyed upsert</div>
    {!intent
      ? <div className="mt-0.5 text-[10.5px] leading-snug text-muted-foreground">Available for one exact managed-local Source feeding a managed-local-file destination that already has a head.</div>
      : <div className="mt-0.5 text-[10.5px] leading-snug text-muted-foreground">Update rows matched by the keys and insert new keys. Preflight proves key validity and the current head.</div>}
    {intent && <label className="mt-2 block text-[10.5px] text-muted-foreground">Key columns
      <input aria-label="Upsert key columns" value={(config.keys ?? []).join(', ')} disabled={!canEdit || intentLocked}
        onChange={(event) => changeKeys(event.target.value)} placeholder="id, frame_id" className="mt-1 h-7 w-full rounded border border-border bg-background px-2 text-[11px] text-foreground" />
    </label>}

    {preflight && <EvidenceSummary evidence={preflight.evidence} base={`${preflight.base.datasetId}@${preflight.base.revisionId}`}
      expectedHead={preflight.expectedHead.revisionId} keys={preflight.keys} schema={preflight.outputSchema} eligible={preflight.eligible} />}

    {task && <div className="mt-2 rounded border border-border bg-background p-2 text-[10.5px] text-muted-foreground">
      <div className="font-semibold text-foreground">{task.status}{task.diagnosticCode ? ` · ${task.diagnosticCode.replaceAll('_', ' ')}` : ''}</div>
      {task.evidence && <div className="mt-0.5">{countLabel(task.evidence.matched)} matched · {countLabel(task.evidence.inserted)} inserted · {countLabel(task.evidence.unchanged)} unchanged</div>}
      {(task.canCancel || task.canRetry) && <div className="mt-1 flex gap-1">
        {task.canCancel && <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" onClick={() => void cancel()} disabled={!canEdit || busy !== null}>Cancel</Button>}
        {task.canRetry && <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" onClick={() => void retry()} disabled={!canEdit || busy !== null}>Retry</Button>}
      </div>}
    </div>}
    {task?.receipt && <ExactRevision receipt={task.receipt} />}

    {trackedTaskPending && <div className="mt-2 rounded border border-border bg-background p-2 text-[10.5px] text-muted-foreground">
      <div className="font-semibold text-foreground">Tracked durable Task</div>
      <div className="mt-0.5">Its current state is loading or temporarily unavailable. This is not treated as a new admission.</div>
    </div>}
    {responseUnknown && !recoveryAvailable && <div className="mt-2 rounded border border-amber-500/30 bg-amber-500/5 p-2 text-[10.5px] text-muted-foreground">
      <div className="font-semibold text-foreground">Previous submission outcome is unresolved</div>
      <div className="mt-0.5">The intent changed after submission. A second admission is blocked; undo those edits to recover the same submission, or start a new admission.</div>
    </div>}
    {staleHead && <div className="mt-2 text-[10px] text-muted-foreground">The destination moved. Nothing was rebased and this never retries automatically; start a new admission against the current head.</div>}
    {error && <div role="alert" className="mt-2 text-[10.5px] leading-snug text-destructive">{error}</div>}

    <div className="mt-2 flex gap-1">
      {intent && !task && !config.taskId && !responseUnknown && <Button size="sm" variant="outline" className="h-7 flex-1 text-[10.5px]" onClick={() => void check()} disabled={!canEdit || busy !== null || !config.keys?.length}>{busy === 'preflight' ? 'Checking…' : 'Check eligibility'}</Button>}
      {intent && !task && !config.taskId && !responseUnknown && <Button size="sm" className="h-7 flex-1 text-[10.5px]" onClick={() => void submit()} disabled={!canEdit || busy !== null || preflight?.eligible !== true || preflightKey !== currentSemanticKey}>{busy === 'submit' ? 'Submitting…' : 'Run keyed upsert'}</Button>}
      {recoveryAvailable && <Button size="sm" className="h-7 flex-1 text-[10.5px]" onClick={() => void recover()} disabled={!canEdit || busy !== null}>{busy === 'submit' ? 'Recovering…' : 'Recover previous submission'}</Button>}
      {(taskTerminal || staleHead) && <Button size="sm" variant="outline" className="h-7 text-[10.5px]" onClick={reAdmit} disabled={!canEdit || busy !== null}>Start new admission</Button>}
    </div>
  </div>
}
