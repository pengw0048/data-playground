import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, KernelError } from '../api/client'
import { routeHash } from '../router'
import { roleCanEdit, useStore } from '../store/graph'
import type { CatalogTable, ManagedSidecarMergePreflight, ManagedSidecarMergeRequest, ManagedSidecarMergeTask, MergeColumnRule } from '../types/api'
import type { ColumnSchema, NodeConfig } from '../types/graph'
import { Button } from '@/components/ui/button'

type Exact = { kind: 'exact'; datasetId: string; revisionId: string }
export interface ManagedSidecarMergeConfig {
  submissionId?: string
  semanticKey?: string
  submissionState?: 'response_unknown'
  taskId?: string
  base?: Exact
  identityColumns?: string[]
  rules?: MergeColumnRule[]
}

const freshId = () => globalThis.crypto?.randomUUID?.() ?? `sidecar-merge-${Date.now()}-${Math.random().toString(36).slice(2)}`
const errorText = (caught: unknown) => caught instanceof Error ? caught.message : String(caught)
const definitelyRejected = (caught: unknown) => caught instanceof KernelError && caught.status >= 400 && caught.status < 500
// Dataset column identifiers are ASCII-compatible today.  Fold only ASCII here so the draft
// guard cannot silently invent locale or Unicode-normalization semantics the service does not
// implement.
const asciiFold = (value: string) => value.replace(/[A-Z]/g, (character) => character.toLowerCase())

function configOf(value: NodeConfig): ManagedSidecarMergeConfig {
  const raw = value.managedSidecarMerge
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const valueOf = raw as Record<string, unknown>
  const exact = (value: unknown): Exact | undefined => value && typeof value === 'object'
    && !Array.isArray(value) && (value as Record<string, unknown>).kind === 'exact'
    && typeof (value as Record<string, unknown>).datasetId === 'string'
    && typeof (value as Record<string, unknown>).revisionId === 'string'
    ? value as Exact : undefined
  return {
    submissionId: typeof valueOf.submissionId === 'string' ? valueOf.submissionId : undefined,
    semanticKey: typeof valueOf.semanticKey === 'string' ? valueOf.semanticKey : undefined,
    submissionState: valueOf.submissionState === 'response_unknown' ? 'response_unknown' : undefined,
    taskId: typeof valueOf.taskId === 'string' ? valueOf.taskId : undefined,
    base: exact(valueOf.base),
    identityColumns: Array.isArray(valueOf.identityColumns) ? valueOf.identityColumns.filter((item): item is string => typeof item === 'string') : [],
    rules: Array.isArray(valueOf.rules) ? valueOf.rules.flatMap((item) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return []
      const rule = item as Record<string, unknown>
      return typeof rule.source === 'string' && typeof rule.target === 'string'
        && (rule.mode === 'add' || rule.mode === 'replace')
        ? [{ source: rule.source, target: rule.target, mode: rule.mode }] : []
    }) : [],
  }
}

function directExactSidecar(doc: { nodes: Array<any>; edges: Array<any> }, writeId: string): Exact | null {
  const incoming = doc.edges.filter((item) => item.target === writeId)
  if (incoming.length !== 1) return null
  const source = doc.nodes.find((item) => item.id === incoming[0].source && item.type === 'source')
  const ref = source?.data?.config?.datasetRef
  return ref?.kind === 'exact' && typeof ref.datasetId === 'string' && typeof ref.revisionId === 'string'
    ? { kind: 'exact', datasetId: ref.datasetId, revisionId: ref.revisionId } : null
}

function requestFor(config: ManagedSidecarMergeConfig, sidecar: Exact | null): ManagedSidecarMergeRequest | null {
  if (!sidecar || !config.base) return null
  return {
    submissionId: config.submissionId ?? freshId(), base: config.base, sidecar,
    expectedHead: config.base, identityColumns: config.identityColumns ?? [], rules: config.rules ?? [],
  }
}

function semanticKey(request: Omit<ManagedSidecarMergeRequest, 'submissionId'>): string {
  return JSON.stringify({ base: request.base, sidecar: request.sidecar, expectedHead: request.expectedHead,
    identityColumns: request.identityColumns, rules: request.rules })
}

function requestKey(request: ManagedSidecarMergeRequest): string {
  const { submissionId: _submissionId, ...semantic } = request
  return JSON.stringify({ semantic: semanticKey(semantic), submissionId: request.submissionId })
}

function revisionHistoryHref(datasetId: string, revisionId: string): string {
  return routeHash('workspace', undefined, `dataset:${datasetId}`, undefined, undefined, undefined,
    undefined, 'datasets', new URLSearchParams({ revision: revisionId, revisionDataset: datasetId }).toString())
}

function coverageLine(value: ManagedSidecarMergePreflight['coverage']) {
  return `${value.status} · ${value.matchedIdentities.toLocaleString()} matched · ${value.missingIdentities.toLocaleString()} missing · ${value.extraIdentities.toLocaleString()} extra`
}

/**
 * The headless sidecar variant deliberately stores only a small, recoverable request on the
 * existing Write card. The server owns eligibility, schemas, coverage, task phase, and receipt.
 */
export function ManagedSidecarMergeControl({ nodeId, compact = false }: { nodeId: string; compact?: boolean }) {
  const node = useStore((state) => state.doc.nodes.find((item) => item.id === nodeId))
  const doc = useStore((state) => state.doc)
  const updateConfig = useStore((state) => state.updateConfig)
  const canEdit = useStore((state) => roleCanEdit(state.canvasRole))
  const setJobsQuery = useStore((state) => state.setJobsQuery)
  const enabled = !!node?.data.config.managedSidecarMerge
    && typeof node.data.config.managedSidecarMerge === 'object'
    && !Array.isArray(node.data.config.managedSidecarMerge)
  const config = configOf((node?.data.config ?? {}) as NodeConfig)
  const sidecar = directExactSidecar(doc, nodeId)
  const [preflight, setPreflight] = useState<ManagedSidecarMergePreflight | null>(null)
  const [preflightKey, setPreflightKey] = useState<string | null>(null)
  const [task, setTask] = useState<ManagedSidecarMergeTask | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<'preflight' | 'submit' | 'cancel' | 'retry' | 'refresh-base' | null>(null)
  const [baseQuery, setBaseQuery] = useState('')
  const [baseOptions, setBaseOptions] = useState<CatalogTable[]>([])
  const [baseLoading, setBaseLoading] = useState(false)
  const [suggestionSchemas, setSuggestionSchemas] = useState<{ base: ColumnSchema[]; sidecar: ColumnSchema[]; declared: string[] } | null>(null)
  const admissionGeneration = useRef(0)
  const catalogGeneration = useRef(0)
  const latestRequestKey = useRef<string | null>(null)
  const latestTaskId = useRef<string | null>(null)
  const taskGeneration = useRef(0)
  const actionBusy = useRef(false)

  const persist = useCallback((next: ManagedSidecarMergeConfig) => {
    updateConfig(nodeId, { managedSidecarMerge: next })
  }, [nodeId, updateConfig])
  const currentRequest = requestFor(config, sidecar)
  const currentSemanticKey = currentRequest ? semanticKey(currentRequest) : null
  const currentRequestKey = currentRequest ? requestKey(currentRequest) : null
  latestRequestKey.current = currentRequestKey
  latestTaskId.current = config.taskId ?? null

  // A peer or another mounted surface can replace the Write config/source while a request is
  // in flight. Fence that external semantic change before an older response can persist itself.
  useEffect(() => { admissionGeneration.current += 1 }, [currentSemanticKey])

  const ensureSubmission = useCallback((input = config) => {
    const candidate = requestFor(input, sidecar)
    if (!candidate) return input
    const semantic = semanticKey(candidate)
    if (input.submissionState === 'response_unknown' || (input.submissionId && input.semanticKey === semantic)) return input
    const next = { ...input, submissionId: freshId(), semanticKey: semantic, submissionState: undefined, taskId: undefined }
    persist(next)
    return next
  }, [config, persist, sidecar])

  useEffect(() => {
    if (preflightKey && preflightKey !== currentRequestKey) { setPreflight(null); setPreflightKey(null) }
  }, [currentRequestKey, preflightKey])

  // These exact revision reads only make optional drafts available before preflight. The service
  // remains the authority for all merge eligibility and coverage claims.
  useEffect(() => {
    if (!sidecar || !config.base) { setSuggestionSchemas(null); return }
    let live = true
    Promise.all([
      api.datasetRevision(config.base.datasetId, config.base.revisionId),
      api.datasetRevision(sidecar.datasetId, sidecar.revisionId),
      api.tableByRegistration(config.base.datasetId).catch(() => null),
    ]).then(([base, sidecarDetail, table]) => {
      if (live) setSuggestionSchemas({ base: base.preview.columns, sidecar: sidecarDetail.preview.columns,
        declared: table?.keys?.find((key) => key.confidence === 'declared')?.columns ?? [] })
    })
      .catch(() => { if (live) setSuggestionSchemas(null) })
    return () => { live = false }
  }, [config.base?.datasetId, config.base?.revisionId, sidecar?.datasetId, sidecar?.revisionId])

  useEffect(() => {
    if (!canEdit || compact || !enabled) return
    const sequence = ++catalogGeneration.current
    const timer = window.setTimeout(async () => {
      setBaseLoading(true)
      try {
        const page = await api.tablesPage({ q: baseQuery.trim() || undefined, limit: 10, sort: 'usage', order: 'desc' })
        if (sequence === catalogGeneration.current) setBaseOptions(page.items)
      } catch (caught) { if (sequence === catalogGeneration.current) setError(errorText(caught)) }
      finally { if (sequence === catalogGeneration.current) setBaseLoading(false) }
    }, baseQuery.trim() ? 200 : 0)
    return () => window.clearTimeout(timer)
  }, [baseQuery, canEdit, compact, enabled])

  const loadTask = useCallback(async (taskId: string) => {
    if (actionBusy.current) return
    const sequence = ++taskGeneration.current
    try {
      const value = await api.managedSidecarMergeTask(taskId)
      if (sequence === taskGeneration.current && !actionBusy.current) { setTask(value); setError('') }
    } catch (caught) { if (sequence === taskGeneration.current && !actionBusy.current) setError(errorText(caught)) }
  }, [])
  useEffect(() => {
    if (busy !== null) return
    if (!config.taskId) { setTask(null); return }
    void loadTask(config.taskId)
    return () => { taskGeneration.current += 1 }
  }, [busy, config.taskId, loadTask])
  useEffect(() => {
    if (!config.taskId || !task || ['done', 'failed', 'cancelled'].includes(task.status)) return
    const timer = window.setInterval(() => void loadTask(config.taskId!), 1500)
    return () => window.clearInterval(timer)
  }, [config.taskId, loadTask, task])

  const change = (next: ManagedSidecarMergeConfig) => {
    if (actionBusy.current || config.submissionState === 'response_unknown') return
    admissionGeneration.current += 1; taskGeneration.current += 1
    const candidate = requestFor(next, sidecar)
    persist({ ...next, submissionId: freshId(), semanticKey: candidate ? semanticKey(candidate) : undefined, submissionState: undefined, taskId: undefined })
    setTask(null); setPreflight(null); setPreflightKey(null); setError(''); setBusy(null)
  }
  const selectBase = async (table: CatalogTable) => {
    const sequence = ++admissionGeneration.current
    setBusy('refresh-base'); setError('')
    try {
      const head = await api.resolveDatasetRevision(table.id)
      if (sequence !== admissionGeneration.current) return
      change({ ...config, base: { kind: 'exact', datasetId: head.datasetId, revisionId: head.revisionId } })
      setBaseQuery(table.name)
    } catch (caught) { if (sequence === admissionGeneration.current) setError(errorText(caught)) }
    finally { if (sequence === admissionGeneration.current) setBusy(null) }
  }
  const check = async () => {
    if (draftError) { setError(draftError); return }
    const sequence = ++admissionGeneration.current
    const next = ensureSubmission()
    const request = requestFor(next, sidecar)
    if (!request) { setError('Choose one direct exact sidecar Source and one destination base first.'); return }
    const key = requestKey(request)
    setBusy('preflight'); setError(''); setPreflight(null); setPreflightKey(null)
    try {
      const result = await api.managedSidecarMergePreflight(request)
      if (sequence === admissionGeneration.current) { setPreflight(result); setPreflightKey(key) }
    } catch (caught) { if (sequence === admissionGeneration.current) setError(errorText(caught)) }
    finally { if (sequence === admissionGeneration.current) setBusy(null) }
  }
  const submitRequest = async (request: ManagedSidecarMergeRequest, next: ManagedSidecarMergeConfig) => {
    const sequence = ++admissionGeneration.current
    const activeKey = requestKey(request)
    // `ensureSubmission` can have persisted this new id before React re-renders. Keep the
    // latest fence coherent for that self-originated render, then let any peer render replace it.
    latestRequestKey.current = activeKey
    taskGeneration.current += 1; actionBusy.current = true; setBusy('submit'); setError('')
    try {
      persist({ ...next, submissionState: 'response_unknown' })
      const result = await api.submitManagedSidecarMerge(request)
      if (sequence === admissionGeneration.current && latestRequestKey.current === activeKey) { setTask(result); persist({ ...next, taskId: result.taskId, submissionState: undefined }) }
    } catch (caught) {
      if (sequence === admissionGeneration.current && latestRequestKey.current === activeKey) {
        if (definitelyRejected(caught)) { persist({ ...next, submissionState: undefined }); setPreflight(null); setPreflightKey(null) }
        setError(errorText(caught))
      }
    } finally {
      // An obsolete response must not persist task state, but it still owns this one in-flight
      // action and must release the UI after a peer changes the semantic request.
      actionBusy.current = false
      setBusy(null)
    }
  }
  const submit = async () => {
    if (draftError) { setError(draftError); return }
    const next = ensureSubmission(); const request = requestFor(next, sidecar)
    if (!request || !preflight?.eligible || preflightKey !== (request && requestKey(request))) {
      setError('Check the complete current request before submitting.'); return
    }
    await submitRequest(request, next)
  }
  const recover = async () => {
    const request = requestFor(config, sidecar)
    if (!request || config.submissionState !== 'response_unknown' || config.semanticKey !== currentSemanticKey) return
    await submitRequest(request, config)
  }
  const cancel = async () => {
    if (!config.taskId) return
    const taskId = config.taskId
    const activeKey = currentRequestKey
    const sequence = ++admissionGeneration.current; taskGeneration.current += 1; actionBusy.current = true; setBusy('cancel'); setError('')
    try {
      const value = await api.cancelManagedSidecarMergeTask(taskId)
      if (sequence === admissionGeneration.current && latestTaskId.current === taskId
          && latestRequestKey.current === activeKey) setTask(value)
    } catch (caught) {
      if (sequence === admissionGeneration.current && latestTaskId.current === taskId
          && latestRequestKey.current === activeKey) setError(errorText(caught))
    }
    finally { actionBusy.current = false; setBusy(null) }
  }
  const retry = async () => {
    if (!config.taskId) return
    const taskId = config.taskId
    const activeKey = currentRequestKey
    const sequence = ++admissionGeneration.current; taskGeneration.current += 1; actionBusy.current = true; setBusy('retry'); setError('')
    try {
      const value = await api.retryManagedSidecarMergeTask(taskId, freshId())
      if (sequence === admissionGeneration.current && latestTaskId.current === taskId
          && latestRequestKey.current === activeKey) setTask(value)
    } catch (caught) {
      if (sequence === admissionGeneration.current && latestTaskId.current === taskId
          && latestRequestKey.current === activeKey) setError(errorText(caught))
    }
    finally { actionBusy.current = false; setBusy(null) }
  }
  const refreshBase = async () => {
    if (!config.base) return
    const sequence = ++admissionGeneration.current; setBusy('refresh-base'); setError('')
    try {
      const table = await api.tableByRegistration(config.base.datasetId)
      const head = await api.resolveDatasetRevision(table.id)
      if (sequence !== admissionGeneration.current) return
      // Deliberately preserve the exact sidecar Source. This only makes a fresh destination draft.
      change({ ...config, base: { kind: 'exact', datasetId: head.datasetId, revisionId: head.revisionId } })
    } catch (caught) { if (sequence === admissionGeneration.current) setError(errorText(caught)) }
    finally { if (sequence === admissionGeneration.current) setBusy(null) }
  }
  const suggestedFields = useMemo(() => preflight
    ? { base: preflight.baseSchema, sidecar: preflight.sidecarSchema, declared: [] as string[] }
    : suggestionSchemas, [preflight, suggestionSchemas])
  const identityCandidates = useMemo(() => {
    if (!suggestedFields) return [] as string[][]
    const base = new Map(suggestedFields.base.map((field) => [field.name, field.type]))
    const compatible = suggestedFields.sidecar.filter((field) => base.get(field.name) === field.type).map((field) => field.name)
    const candidates = suggestedFields.declared.length && suggestedFields.declared.every((field) => compatible.includes(field))
      ? [suggestedFields.declared, ...compatible.map((field) => [field])]
      : compatible.map((field) => [field])
    const seen = new Set<string>()
    return candidates.filter((candidate) => {
      const key = candidate.join('\u0000')
      if (seen.has(key)) return false
      seen.add(key); return true
    })
  }, [suggestedFields])
  const useSuggestion = (candidate: string[]) => {
    if (!suggestedFields) return
    if (candidate.length) change({ ...config, identityColumns: candidate.slice(0, 16) })
  }
  const addSuggestedRules = () => {
    if (!suggestedFields) return
    const identity = new Set(config.identityColumns)
    const base = new Set(suggestedFields.base.map((field) => field.name))
    const existing = new Set((config.rules ?? []).map((rule) => `${rule.source}\0${rule.target}`))
    const additions = suggestedFields.sidecar.filter((field) => !identity.has(field.name)
      && !existing.has(`${field.name}\0${field.name}`)).map((field) => ({
      source: field.name, target: field.name, mode: base.has(field.name) ? 'replace' as const : 'add' as const,
    }))
    if (additions.length) change({ ...config, rules: [...(config.rules ?? []), ...additions] })
  }

  const responseUnknown = !task && config.submissionState === 'response_unknown'
  const recoveryAvailable = responseUnknown && !!config.submissionId && config.semanticKey === currentSemanticKey
  const trackedTaskPending = !!config.taskId && !task
  const taskTerminal = task && ['done', 'failed', 'cancelled'].includes(task.status)
  const intentLocked = responseUnknown || trackedTaskPending || (!!task && !taskTerminal) || (busy !== null && busy !== 'preflight')
  const staleHead = task?.diagnosticCode === 'stale_expected_head' || /destination head moved/i.test(error)
  const selectedBase = config.base
  const draftError = useMemo(() => {
    const identities = config.identityColumns ?? []
    const rules = config.rules ?? []
    if (!identities.length) return 'Choose at least one identity column before checking this draft.'
    if (identities.some((identity) => !identity.trim())) return 'Identity columns cannot be blank.'
    const identityNames = new Set<string>()
    for (const identity of identities) {
      const key = asciiFold(identity)
      if (identityNames.has(key)) return `Identity column “${identity}” is duplicated in this draft.`
      identityNames.add(key)
    }
    if (!rules.length) return 'Add at least one payload mapping before checking this draft.'

    const baseFields = new Map((suggestedFields?.base ?? []).map((field) => [field.name, field]))
    const sidecarFields = new Map((suggestedFields?.sidecar ?? []).map((field) => [field.name, field]))
    if (suggestedFields) {
      for (const identity of identities) {
        const base = baseFields.get(identity)
        const source = sidecarFields.get(identity)
        if (!base || !source) return `Identity “${identity}” is not present in both exact draft schemas.`
        if (base.type !== source.type) return `Identity “${identity}” has incompatible draft types (${source.type} → ${base.type}).`
      }
    }

    const sourceNames = new Set<string>()
    const targetNames = new Set<string>()
    for (const rule of rules) {
      if (!rule.source.trim() || !rule.target.trim()) return 'Every mapping needs both a sidecar and destination column.'
      const sourceKey = asciiFold(rule.source)
      const targetKey = asciiFold(rule.target)
      if (identityNames.has(sourceKey)) return 'An identity column cannot also be a payload mapping.'
      if (sourceNames.has(sourceKey)) return `Sidecar payload “${rule.source}” is mapped more than once.`
      if (targetNames.has(targetKey)) return `Destination column “${rule.target}” has conflicting mappings.`
      sourceNames.add(sourceKey); targetNames.add(targetKey)
      if (suggestedFields) {
        const source = sidecarFields.get(rule.source)
        const target = baseFields.get(rule.target)
        if (!source) return `Sidecar payload “${rule.source}” is not present in the exact draft schema.`
        if (rule.mode === 'add' && target) return `“${rule.target}” already exists in the destination draft; use replace or choose a new column.`
        if (rule.mode === 'replace' && !target) return `“${rule.target}” is not in the destination draft; use add or choose an existing column.`
        if (target && source.type !== target.type) return `Mapping “${rule.source}” → “${rule.target}” has incompatible draft types (${source.type} → ${target.type}).`
      }
    }
    if (suggestedFields) {
      const payload = suggestedFields.sidecar.filter((field) => !identityNames.has(asciiFold(field.name)))
      const unused = payload.filter((field) => !sourceNames.has(asciiFold(field.name)))
      if (unused.length) return `Add explicit mappings for sidecar payload: ${unused.map((field) => field.name).join(', ')}.`
    }
    return null
  }, [config.identityColumns, config.rules, suggestedFields])
  if (!node) return null
  if (!enabled) return <div className="mt-2 rounded-md border border-border bg-muted/20 p-2 text-[10.5px] text-muted-foreground">
    <div className="font-semibold text-foreground">Merge an exact managed sidecar</div>
    <div className="mt-0.5">Use a direct exact Source as a published sidecar, then choose the current head and explicit column mappings.</div>
    {!compact && <Button size="sm" variant="outline" className="mt-2 h-6 px-2 text-[10px]" disabled={!canEdit}
      onClick={() => persist({ identityColumns: [], rules: [] })}>Configure managed sidecar merge</Button>}
  </div>

  return <div aria-label="Managed sidecar column merge" className={compact ? 'mt-2 text-[10.5px]' : 'mt-3 rounded-md border border-border bg-muted/30 p-2'}>
    {!compact && <><div className="font-semibold text-[11px] text-foreground">Add or replace columns in an existing dataset</div>
      <div className="mt-0.5 text-[10.5px] leading-snug text-muted-foreground">The upstream transform already published this exact sidecar. This action merges only the declared payload columns into a core-owned local dataset; it never writes back to an external provider.</div></>}
    {!sidecar && <div role="alert" className="mt-2 text-[10.5px] text-destructive">Connect one exact managed-local Source directly to this Write before configuring the merge.</div>}
    {sidecar && <div className="mt-2 rounded border border-border bg-background p-2 text-[10px] text-muted-foreground"><strong className="text-foreground">Exact sidecar Source</strong><div className="mt-0.5 break-all font-mono">{sidecar.datasetId}@{sidecar.revisionId}</div></div>}
    {!compact && <label className="mt-2 block text-[10.5px] text-muted-foreground">Destination base (current exact head)
      <input aria-label="Search destination bases" disabled={!canEdit || intentLocked} value={baseQuery} onChange={(event) => setBaseQuery(event.target.value)} placeholder="Search the catalog…" className="mt-1 h-7 w-full rounded border border-border bg-background px-2 text-[11px] text-foreground" />
      {baseLoading && <span className="mt-1 block text-[10px]">Searching catalog…</span>}
      {!intentLocked && baseOptions.length > 0 && <div className="mt-1 max-h-28 overflow-auto rounded border border-border bg-background">{baseOptions.map((table) => <button type="button" key={table.id} onClick={() => void selectBase(table)} className="block w-full border-b border-border px-2 py-1 text-left text-[10px] hover:bg-accent last:border-b-0"><strong className="block truncate text-foreground">{table.name}</strong><span>registration {table.registrationId ?? table.id} · catalog version {table.version ?? 'unavailable'} · format not advertised · {table.columns.map((field) => `${field.name}: ${field.type}`).join(', ') || 'schema unavailable'}</span></button>)}</div>}
    </label>}
    {selectedBase && <div className="mt-2 rounded border border-border bg-background p-2 text-[10px] text-muted-foreground"><strong className="text-foreground">Destination exact base</strong><div className="mt-0.5 break-all font-mono">{selectedBase.datasetId}@{selectedBase.revisionId}</div></div>}
    {!compact && <label className="mt-2 block text-[10.5px] text-muted-foreground">Identity columns
      <input aria-label="Managed sidecar identity columns" disabled={!canEdit || intentLocked} value={(config.identityColumns ?? []).join(', ')} onChange={(event) => change({ ...config, identityColumns: event.target.value.split(',').map((item) => item.trim()).filter(Boolean) })} placeholder="id, frame_id" className="mt-1 h-7 w-full rounded border border-border bg-background px-2 text-[11px] text-foreground" />
    </label>}
    {identityCandidates.length > 0 && !compact && <div className="mt-1 flex flex-wrap items-center gap-1"><span className="text-[10px] text-muted-foreground">Suggested draft identities:</span>{identityCandidates.map((candidate) => <Button key={candidate.join('\u0000')} size="sm" variant="outline" className="h-6 px-2 text-[10px]" onClick={() => useSuggestion(candidate)} disabled={!canEdit || intentLocked}>Use {candidate.join(', ')}</Button>)}<span className="text-[10px] text-muted-foreground">Preflight remains authoritative.</span></div>}
    {!compact && <div className="mt-2 space-y-1"><div className="text-[10.5px] text-muted-foreground">Sidecar payload → destination column</div>
      {(config.rules ?? []).map((rule, index) => <div key={index} className="grid grid-cols-[1fr_74px_1fr_24px] gap-1"><input aria-label={`Managed sidecar source column ${index + 1}`} value={rule.source} disabled={!canEdit || intentLocked} onChange={(event) => { const rules = [...(config.rules ?? [])]; rules[index] = { ...rules[index]!, source: event.target.value }; change({ ...config, rules }) }} placeholder="sidecar field" className="h-7 min-w-0 rounded border border-border bg-background px-1.5 text-[10.5px]" /><select aria-label={`Managed sidecar mode ${index + 1}`} value={rule.mode} disabled={!canEdit || intentLocked} onChange={(event) => { const rules = [...(config.rules ?? [])]; rules[index] = { ...rules[index]!, mode: event.target.value as MergeColumnRule['mode'] }; change({ ...config, rules }) }} className="h-7 rounded border border-border bg-background px-1 text-[10px]"><option value="add">add</option><option value="replace">replace</option></select><input aria-label={`Managed sidecar target column ${index + 1}`} value={rule.target} disabled={!canEdit || intentLocked} onChange={(event) => { const rules = [...(config.rules ?? [])]; rules[index] = { ...rules[index]!, target: event.target.value }; change({ ...config, rules }) }} placeholder="base field" className="h-7 min-w-0 rounded border border-border bg-background px-1.5 text-[10.5px]" /><button aria-label={`Remove managed sidecar rule ${index + 1}`} disabled={!canEdit || intentLocked} onClick={() => change({ ...config, rules: (config.rules ?? []).filter((_, item) => item !== index) })} className="text-muted-foreground hover:text-destructive">×</button></div>)}
      <div className="flex gap-1"><Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" disabled={!canEdit || intentLocked} onClick={() => change({ ...config, rules: [...(config.rules ?? []), { source: '', target: '', mode: 'add' }] })}>Add mapping</Button>{suggestedFields && <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" disabled={!canEdit || intentLocked} onClick={addSuggestedRules}>Add suggested rules</Button>}</div>
    </div>}
    {preflight && <div aria-label="Managed sidecar preflight" className="mt-2 rounded border border-border bg-background p-2 text-[10.5px] text-muted-foreground"><div className="font-semibold text-foreground">{preflight.eligible ? 'Eligible exact sidecar merge' : 'Not eligible'}</div><div className="mt-0.5 break-all font-mono">base {preflight.base.datasetId}@{preflight.base.revisionId}</div><div className="break-all font-mono">sidecar {preflight.sidecar.datasetId}@{preflight.sidecar.revisionId}</div><div>Identity order: <span className="font-mono">{preflight.identityColumns.join(', ') || 'none'}</span></div><div>Mappings: {preflight.rules.map((rule) => `${rule.source} → ${rule.target} (${rule.mode})`).join('; ') || 'none'}</div><div>Coverage: {coverageLine(preflight.coverage)}</div><div>Base: {preflight.coverage.base.rows.toLocaleString()} rows · {preflight.coverage.base.uniqueIdentities.toLocaleString()} unique · {preflight.coverage.base.nullRows.toLocaleString()} null · {preflight.coverage.base.duplicateGroups.toLocaleString()} duplicate groups / {preflight.coverage.base.duplicateRows.toLocaleString()} duplicate rows</div><div>Sidecar: {preflight.coverage.candidate.rows.toLocaleString()} rows · {preflight.coverage.candidate.uniqueIdentities.toLocaleString()} unique · {preflight.coverage.candidate.nullRows.toLocaleString()} null · {preflight.coverage.candidate.duplicateGroups.toLocaleString()} duplicate groups / {preflight.coverage.candidate.duplicateRows.toLocaleString()} duplicate rows</div><div>Expected head: <span className="font-mono">{preflight.expectedHead.revisionId}</span></div><div>Resulting schema: {preflight.outputSchema.map((field) => `${field.name}: ${field.type}`).join(', ') || 'no fields'}</div></div>}
    {task && <div className="mt-2 rounded border border-border bg-background p-2 text-[10.5px] text-muted-foreground"><div className="font-semibold text-foreground">{task.mergeColumns?.phase?.replaceAll('_', ' ') ?? task.status}</div><div className="mt-0.5">Candidate {task.mergeColumns?.candidate ?? 'pending'}{task.mergeColumns?.reused ? ' · reused' : ''}</div><div className="mt-1 flex flex-wrap gap-1"><Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" onClick={() => setJobsQuery(new URLSearchParams({ run: task.taskId }).toString())}>Open in Jobs</Button>{task.canCancel && <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" onClick={() => void cancel()} disabled={!canEdit || busy !== null}>Cancel</Button>}{task.canRetry && <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" onClick={() => void retry()} disabled={!canEdit || busy !== null}>Retry</Button>}{task.receipt && <a className="rounded border border-border bg-card px-2 py-1 text-[10px] font-medium text-foreground hover:bg-accent" href={revisionHistoryHref(task.receipt.datasetId, task.receipt.revisionId)}>Open published child</a>}{task.base && <a className="rounded border border-border bg-card px-2 py-1 text-[10px] font-medium text-foreground hover:bg-accent" href={revisionHistoryHref(task.base.datasetId, task.base.revisionId)}>Open exact base</a>}</div></div>}
    {trackedTaskPending && <div className="mt-2 rounded border border-border bg-background p-2 text-[10.5px] text-muted-foreground"><strong className="text-foreground">Tracked durable Task</strong><div>The task is loading, unavailable, or belongs to another submitter. It is never treated as a new admission; its submitter can inspect it in Jobs.</div><Button size="sm" variant="outline" className="mt-1 h-6 px-2 text-[10px]" onClick={() => setJobsQuery(new URLSearchParams({ run: config.taskId! }).toString())}>Open in Jobs</Button></div>}
    {responseUnknown && !recoveryAvailable && <div className="mt-2 rounded border border-amber-500/30 bg-amber-500/5 p-2 text-[10.5px] text-muted-foreground"><strong className="text-foreground">Previous submission outcome is unresolved</strong><div>The request changed after submission. Inspect Jobs or restore the same request to recover its durable outcome.</div></div>}
    {draftError && <div role="alert" className="mt-2 text-[10.5px] leading-snug text-destructive">{draftError}</div>}
    {error && <div role="alert" className="mt-2 text-[10.5px] leading-snug text-destructive">{error}</div>}
    {!compact && <div className="mt-2 flex flex-wrap gap-1">{!task && !config.taskId && !responseUnknown && <><Button size="sm" variant="outline" className="h-7 text-[10.5px]" onClick={() => void check()} disabled={!canEdit || busy !== null || !!draftError}>{busy === 'preflight' ? 'Checking…' : 'Check eligibility'}</Button><Button size="sm" className="h-7 text-[10.5px]" onClick={() => void submit()} disabled={!canEdit || busy !== null || !!draftError || !preflight?.eligible || preflightKey !== currentRequestKey}>{busy === 'submit' ? 'Submitting…' : 'Start managed merge'}</Button></>}{recoveryAvailable && <Button size="sm" className="h-7 text-[10.5px]" onClick={() => void recover()} disabled={!canEdit || busy !== null}>{busy === 'submit' ? 'Recovering…' : 'Recover previous submission'}</Button>}{taskTerminal && <Button size="sm" variant="outline" className="h-7 text-[10.5px]" onClick={() => change({ ...config, taskId: undefined, submissionState: undefined })} disabled={!canEdit || busy !== null}>Start new admission</Button>}{!config.taskId && !responseUnknown && <Button size="sm" variant="ghost" className="h-7 text-[10.5px]" onClick={() => { if (window.confirm('Discard this unsubmitted managed-sidecar merge draft?')) updateConfig(nodeId, { managedSidecarMerge: undefined }) }} disabled={!canEdit || busy !== null}>Exit merge setup</Button>}{staleHead && <div className="basis-full text-[10px] text-muted-foreground">The destination moved. The exact sidecar has not changed. <Button size="sm" variant="outline" className="ml-1 h-6 px-2 text-[10px]" onClick={() => void refreshBase()} disabled={!canEdit || busy !== null}>Refresh destination head</Button></div>}</div>}
  </div>
}
