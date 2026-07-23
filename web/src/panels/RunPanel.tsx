import { useEffect } from 'react'
import { hasConfiguredManagedSidecarMerge, roleCanEdit, targetParameterDeclarations, useStore } from '../store/graph'
import { color, status as statusTok } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { Button } from '@/components/ui/button'
import { MergeColumnsControl } from '../components/MergeColumnsControl'
import { ManagedSidecarMergeControl } from '../components/ManagedSidecarMergeControl'
import { UpsertControl } from '../components/UpsertControl'
import { WritePublicationSummary } from '../components/WritePublicationSummary'
import { cn } from '@/lib/utils'
import type { InputDrift, RunOutput } from '../types/api'
import { datasetRefIdentity, isParameterRef, type CanvasDoc, type CanvasParameterDeclaration, type DatasetRef } from '../types/graph'

export function RunPanel({ nodeId }: { nodeId: string }) {
  const run = useStore((s) => s.runs[nodeId])
  const estimate = useStore((s) => s.estimate)
  const doRun = useStore((s) => s.run)
  const cancel = useStore((s) => s.cancelRun)
  const refreshPreviewInputs = useStore((s) => s.refreshPreviewInputs)
  const hasRetainedPreviewBinding = useStore((s) => !!s.previewBindings[nodeId])
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const doc = useStore((s) => s.doc)
  const target = doc.nodes.find((node) => node.id === nodeId)
  const isWrite = target?.type === 'write'
  const mergeRules = target?.data.config.mergeColumns
  const isConfiguredMerge = !!mergeRules && typeof mergeRules === 'object' && !Array.isArray(mergeRules)
    && Array.isArray((mergeRules as { rules?: unknown }).rules)
    && (mergeRules as { rules: unknown[] }).rules.length > 0
  const isConfiguredManagedSidecarMerge = hasConfiguredManagedSidecarMerge(doc, nodeId)
  const upsertKeys = target?.data.config.keyedUpsert
  const isConfiguredUpsert = !!upsertKeys && typeof upsertKeys === 'object' && !Array.isArray(upsertKeys)
    && Array.isArray((upsertKeys as { keys?: unknown }).keys)
    && (upsertKeys as { keys: unknown[] }).keys.length > 0
  const setParameterBinding = useStore((s) => s.setRunParameterBinding)
  const clearParameterBinding = useStore((s) => s.clearRunParameterBinding)
  const editParameters = useStore((s) => s.editRunParameters)
  const submitParameters = useStore((s) => s.submitRunParameters)
  const setJobsQuery = useStore((s) => s.setJobsQuery)

  useEffect(() => {
    if (!isConfiguredManagedSidecarMerge && !isConfiguredMerge && !isConfiguredUpsert && (!run || run.phase === 'idle')) estimate(nodeId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isConfiguredManagedSidecarMerge, isConfiguredMerge, isConfiguredUpsert, nodeId])

  const phase = run?.phase ?? 'estimating'
  const est = run?.estimate
  const st = run?.status
  const pinnedInputs = pinnedSourceInputs(doc, nodeId)
  const writeAdmission = run?.writeAdmission
    ?? (run?.phase === 'done' ? run.writeOutcomeAdmission : undefined)
  const writeSubmissionUnresolved = Boolean(
    writeAdmission?.managed && writeAdmission.intent && run?.writeSubmissionId,
  )
  const parameterDeclarations = targetParameterDeclarations(doc, nodeId)
  const parameterBindings = new Map((run?.parameterBindings ?? []).map((item) => [item.name, item.value]))
  const parameterErrors = parameterDeclarations.map((item) => parameterValueError(
    item, parameterBindings.has(item.name), parameterBindings.get(item.name)))
  const currentJobRunId = st?.runId && (
    (phase === 'running' && (st.status === 'queued' || st.status === 'running'))
    || (phase === 'done' && st.status === 'done')
    || (phase === 'failed' && st.status === 'failed')
    || (phase === 'idle' && st.status === 'cancelled')
  ) ? st.runId : null
  const writeConfig = (target?.data.config ?? {}) as Record<string, unknown>
  const outputName = String(writeConfig.filename ?? writeConfig.name ?? target?.data.title ?? 'output')
  const destination = `${String(writeConfig.destName ?? 'Workspace outputs')}${writeConfig.destPath ? `/${String(writeConfig.destPath)}` : ''}`
  const receipt = st?.outputs.find((output) => output.writeReceipt)?.writeReceipt ?? writeAdmission?.recoveredReceipt

  if (isConfiguredManagedSidecarMerge) return (
    <div className="p-3.5">
      <Label>MANAGED SIDECAR MERGE</Label>
      <div className="mt-1 text-[11px] text-muted-foreground">This Write merges an already-published exact sidecar into a selected current base head. The server certifies every coverage and publication fact.</div>
      <ManagedSidecarMergeControl nodeId={nodeId} />
    </div>
  )

  if (isConfiguredMerge) return (
    <div className="p-3.5">
      <Label>CERTIFIED COLUMN MERGE</Label>
      <div className="mt-1 text-[11px] text-muted-foreground">This Write is admitted as an exact, version-aware column merge rather than an ordinary overwrite run.</div>
      <MergeColumnsControl nodeId={nodeId} />
    </div>
  )

  if (isConfiguredUpsert) return (
    <div className="p-3.5">
      <Label>CERTIFIED KEYED UPSERT</Label>
      <div className="mt-1 text-[11px] text-muted-foreground">This Write is admitted as an exact, version-aware keyed upsert rather than an ordinary overwrite run.</div>
      <UpsertControl nodeId={nodeId} />
    </div>
  )

  return (
    <div className="p-3.5">
      {phase === 'parameters' && (
        <>
          <Label>RUN PARAMETERS</Label>
          <div className="mt-1 text-[11px] text-muted-foreground">Bindings apply only to this target's upstream pipeline.</div>
          <div className="mt-3 flex flex-col gap-3">
            {parameterDeclarations.map((declaration) => {
              const bound = parameterBindings.get(declaration.name)
              return <ParameterField key={`${declaration.name}:${declaration.type}`} declaration={declaration}
                isBound={parameterBindings.has(declaration.name)} value={bound}
                error={parameterValueError(declaration, parameterBindings.has(declaration.name), bound)}
                setValue={(value) => setParameterBinding(nodeId, { name: declaration.name, value })}
                clear={() => clearParameterBinding(nodeId, declaration.name)} />
            })}
          </div>
          <Button size="sm" onClick={() => void submitParameters(nodeId)} disabled={!canEdit || parameterErrors.some(Boolean)} className="mt-4 w-full">Continue</Button>
        </>
      )}

      {(phase === 'estimating' || (!est && phase !== 'parameters' && phase !== 'running' && phase !== 'done' && phase !== 'failed')) && (
        <div className="py-2.5 text-xs text-muted-foreground">estimating…</div>
      )}

      {(phase === 'estimated' || phase === 'confirm') && est && (
        <>
          <Label>{phase === 'confirm' ? 'HEADS UP' : 'ESTIMATE'}</Label>
          <div className="mt-0.5 flex items-baseline gap-2">
            <span className="text-2xl font-bold text-foreground">
              {est.rows != null ? `${est.rows.toLocaleString()} rows` : 'Size unknown'}
            </span>
          </div>
          {est.breakdown && <div className="mt-2 text-[11px] text-muted-foreground">{est.breakdown}</div>}
          {isWrite && <WritePublicationSummary compact outputName={outputName} destination={destination} admission={writeAdmission} receipt={receipt} />}
          {pinnedInputs.length > 0 && (
            <div aria-label="Pinned run inputs" className="mt-2 rounded-md border border-border bg-muted/40 px-2 py-1.5 text-[10.5px] text-muted-foreground">
              <div className="font-semibold text-foreground">Pinned exact inputs for this run</div>
              {pinnedInputs.map((input) => {
                const exact = datasetRefIdentity(input.ref)
                return <div key={input.nodeId} className="mt-0.5 break-all">
                  {input.title} · dataset {exact.datasetId} · revision {exact.revisionId}
                  {input.ref.kind === 'as_of' ? ` · as of ${new Date(input.ref.asOf).toLocaleString()}` : ''}
                </div>
              })}
            </div>
          )}
          {phase === 'confirm' ? (
            <div className="mt-3.5 flex gap-2">
              <Button size="sm" onClick={() => doRun(nodeId, true)} disabled={!canEdit} title={canEdit ? 'Run' : 'View-only canvas'} className="flex-1 bg-[#d99a2b] text-white hover:bg-[#c98d24]">Run</Button>
              <Button size="sm" variant="outline" onClick={() => useStore.getState().closePanel(nodeId)} className="flex-1">Cancel</Button>
            </div>
          ) : (
            <Button size="sm" onClick={() => doRun(nodeId, false)} disabled={!canEdit} title={canEdit ? 'Run' : 'View-only canvas'} className="mt-3.5 w-full">Run</Button>
          )}
        </>
      )}

      {phase === 'drift' && run?.inputDrift && (
        <>
          <Label>PREVIEW INPUTS MOVED</Label>
          <div className="mt-1 text-[11px] text-muted-foreground">
            Latest changed after this preview. The full run will keep the preview's exact inputs unless you explicitly refresh.
          </div>
          <InputDriftNotice drift={run.inputDrift} doc={doc} />
          <div className="mt-3 flex gap-2">
            <Button size="sm" onClick={() => doRun(nodeId, !!est?.needsConfirm, true)} disabled={!canEdit}
              title={canEdit ? 'Run the exact preview inputs' : 'View-only canvas'} className="flex-1">
              Run preview inputs
            </Button>
            <Button size="sm" variant="outline" onClick={() => void refreshPreviewInputs(nodeId)} disabled={!canEdit}
              title={canEdit ? 'Accept latest inputs and refresh the preview' : 'View-only canvas'} className="flex-1">
              Refresh to latest
            </Button>
          </div>
        </>
      )}

      {phase === 'running' && st && (
        <>
          <div className="mb-2.5 flex items-center gap-2">
            <span className="dp-running-glyph text-primary">●</span>
            <span className="text-[13px] font-semibold">running</span>
            {st.progress != null && <span className="text-[11.5px] text-muted-foreground">{Math.round(st.progress * 100)}%</span>}
          </div>
          {/* step-progress (deterministic) when we have it, else the row-based fallback */}
          <ProgressBar value={st.progress ?? (st.totalRows ? st.rowsProcessed / Math.max(1, st.totalRows) : 0.3)} />
          <div className="my-2 text-[11.5px] text-muted-foreground">
            {st.rowsProcessed.toLocaleString()}{st.totalRows ? ` / ${st.totalRows.toLocaleString()}` : ''} rows
          </div>
          {st.stalled && (
            <div className="mb-2 rounded bg-amber-500/10 px-2 py-1 text-[11px] text-amber-600 dark:text-amber-400">
              ⚠ no step has completed recently — the run may be stuck (or on a long step)
            </div>
          )}
          <PerNode st={st} />
          <RunOutputs outputs={st.outputs} />
          <Button size="sm" variant="outline" onClick={() => cancel(nodeId)} disabled={!canEdit} title={canEdit ? 'Stop this run' : 'View-only canvas'} className="mt-3 w-full">
            <Icon name="stop" size={12} /> Stop
          </Button>
        </>
      )}

      {phase === 'done' && st && (
        isWrite ? <>
          <Label>PUBLISHED</Label>
          <WritePublicationSummary outputName={outputName} destination={destination} admission={writeAdmission}
            outcomeAdmission={run?.writeOutcomeAdmission} receipt={receipt} outputs={st.outputs} completed />
          <PerNode st={st} compact />
        </> : <>
          <Label>DONE</Label>
          <div className="mt-0.5 flex items-baseline gap-2">
            <span className="text-base" style={{ color: color.latest }}>✓</span>
            <span className="text-[22px] font-bold text-foreground">
              {st.totalRows != null
                ? `${st.totalRows.toLocaleString()} rows`
                : `${st.outputs.length.toLocaleString()} output${st.outputs.length === 1 ? '' : 's'}`}
            </span>
            <span className="text-[13px] text-muted-foreground">· {fmtTime(st.ms / 1000)}</span>
          </div>
          <RunOutputs outputs={st.outputs} />
          <PerNode st={st} compact />
        </>
      )}

      {phase === 'failed' && (
        <div className="py-2">
          <Label>FAILED</Label>
          <div className="mt-1 flex items-center gap-2">
            <span className="text-destructive">✕</span>
            <span className="text-[13px] font-semibold text-destructive">run failed</span>
          </div>
          <div className="dp-mono mt-2 whitespace-pre-wrap rounded-lg bg-destructive/10 p-2.5 text-[11px] text-muted-foreground">
            {run?.error ?? st?.error ?? 'unknown error'}
          </div>
          {st && <RunOutputs outputs={st.outputs} />}
          <div className="mt-3 flex gap-2">
            <Button size="sm" variant="outline"
              onClick={() => writeSubmissionUnresolved
                ? doRun(nodeId, !!est?.needsConfirm)
                : estimate(nodeId)}
              className="flex-1">Retry</Button>
            {(run?.inputDrift || hasRetainedPreviewBinding) && <Button size="sm" variant="outline" onClick={() => void refreshPreviewInputs(nodeId)}
              disabled={!canEdit} className="flex-1">Refresh to latest</Button>}
          </div>
        </div>
      )}

      {currentJobRunId && <Button size="sm" variant="outline" className="mt-3 w-full"
        onClick={() => setJobsQuery(new URLSearchParams({ run: currentJobRunId }).toString())}>
        <Icon name="clock" size={12} /> View in Jobs
      </Button>}

      {parameterDeclarations.length > 0 && phase !== 'parameters' && (
        <Button size="sm" variant="outline" onClick={() => editParameters(nodeId)}
          disabled={!canEdit || phase === 'running'}
          title={phase === 'running' ? 'Stop or wait for the active run before editing its parameters.' : 'Edit bindings, then return to a fresh estimate.'}
          className="mt-3 w-full">Edit parameters</Button>
      )}
    </div>
  )
}

function isBuiltInSecretRef(value: string): boolean {
  return /^(?:env|file):/i.test(value)
}

function parameterValueError(declaration: CanvasParameterDeclaration, isBound: boolean, value: unknown): string | null {
  if (!isBound) return declaration.default == null ? 'A binding is required because this parameter has no default.' : null
  if (declaration.type === 'string') {
    if (typeof value !== 'string') return 'Enter a string value.'
    if (isBuiltInSecretRef(value)) return 'Secret references are not public run parameters.'
    if (declaration.constraints?.minLength != null && value.length < declaration.constraints.minLength) return `Minimum length is ${declaration.constraints.minLength}.`
    if (declaration.constraints?.maxLength != null && value.length > declaration.constraints.maxLength) return `Maximum length is ${declaration.constraints.maxLength}.`
  } else if (declaration.type === 'integer') {
    if (!Number.isSafeInteger(value)) return 'Enter a complete safe integer.'
  } else if (declaration.type === 'float') {
    if (typeof value !== 'number' || !Number.isFinite(value)) return 'Enter a finite number.'
  } else if (declaration.type === 'boolean') {
    if (typeof value !== 'boolean') return 'Select true or false.'
  } else if (declaration.type === 'date') {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)
        || Number.isNaN(Date.parse(`${value}T00:00:00Z`))
        || new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) !== value) return 'Enter a real date (YYYY-MM-DD).'
  } else if (declaration.type === 'datetime') {
    if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value) || Number.isNaN(Date.parse(value))) return 'Enter ISO 8601 with an explicit timezone.'
  } else {
    const ref = value as { kind?: unknown; datasetId?: unknown; revisionId?: unknown }
    if (!value || typeof value !== 'object' || !['exact', 'latest'].includes(String(ref.kind))
        || typeof ref.datasetId !== 'string' || !ref.datasetId
        || isBuiltInSecretRef(ref.datasetId)
        || (ref.kind === 'exact' && (typeof ref.revisionId !== 'string' || !ref.revisionId))) return 'Choose latest or exact and provide the dataset identity and revision.'
  }
  if ((declaration.type === 'integer' || declaration.type === 'float') && typeof value === 'number') {
    if (declaration.constraints?.minimum != null && value < declaration.constraints.minimum) return `Minimum is ${declaration.constraints.minimum}.`
    if (declaration.constraints?.maximum != null && value > declaration.constraints.maximum) return `Maximum is ${declaration.constraints.maximum}.`
  }
  return null
}

function ParameterField({ declaration, isBound, value, error, setValue, clear }: {
  declaration: CanvasParameterDeclaration; isBound: boolean; value: unknown; error: string | null
  setValue: (value: unknown) => void; clear: () => void
}) {
  const label = declaration.label || declaration.name
  const common = 'w-full rounded-md border border-border bg-background px-2 py-1.5'
  const fallback = declaration.default == null ? 'Use declared type' : `Use default (${JSON.stringify(declaration.default)})`
  let control
  if (declaration.type === 'boolean') {
    control = <select aria-label={label} value={value == null ? '' : String(value)} onChange={(event) => {
      event.target.value ? setValue(event.target.value === 'true') : clear()
    }} className={common}><option value="">{fallback}</option><option value="true">true</option><option value="false">false</option></select>
  } else if (declaration.type === 'dataset') {
    type DatasetParameterValue = { kind?: string; datasetId?: string; revisionId?: string }
    const declaredDefault = !isBound && declaration.default && typeof declaration.default === 'object'
      ? declaration.default as DatasetParameterValue
      : null
    const ref = value && typeof value === 'object' ? value as DatasetParameterValue : declaredDefault ?? {}
    const usingDefault = declaredDefault != null
    const kind = ref.kind === 'latest' ? 'latest' : 'exact'
    control = <div className="grid grid-cols-[92px_1fr] gap-1.5">
      <select aria-label={`${label} selection`} value={kind} onChange={(event) => setValue({
        kind: event.target.value, datasetId: ref.datasetId ?? '', ...(event.target.value === 'exact' ? { revisionId: ref.revisionId ?? '' } : {}),
      })} disabled={usingDefault} className={common}><option value="exact">Exact</option><option value="latest">Follow latest</option></select>
      <input aria-label={`${label} dataset`} value={ref.datasetId ?? ''} placeholder="Dataset identity" onChange={(event) => {
        event.target.value ? setValue({ kind, datasetId: event.target.value, ...(kind === 'exact' ? { revisionId: ref.revisionId ?? '' } : {}) }) : clear()
      }} disabled={usingDefault} className={common} />
      {kind === 'exact' && <input aria-label={`${label} revision`} value={ref.revisionId ?? ''} placeholder="Exact revision" onChange={(event) => {
        event.target.value ? setValue({ kind: 'exact', datasetId: ref.datasetId ?? '', revisionId: event.target.value }) : clear()
      }} disabled={usingDefault} className={`col-start-2 ${common}`} />}
      {usingDefault && <div className="col-span-2 flex items-center justify-between gap-2 text-muted-foreground">
        <span>Using declared default.</span>
        <Button type="button" size="sm" variant="ghost" onClick={() => setValue(kind === 'latest'
          ? { kind: 'latest', datasetId: ref.datasetId ?? '' }
          : { kind: 'exact', datasetId: ref.datasetId ?? '', revisionId: ref.revisionId ?? '' })}
          className="h-6 px-1.5 text-[10px]">Override default</Button>
      </div>}
    </div>
  } else {
    const text = value == null ? '' : String(value)
    control = <input aria-label={label} type={declaration.type === 'date' ? 'date' : 'text'} value={text}
      placeholder={declaration.type === 'datetime' ? '2026-07-18T14:30:00-04:00' : fallback}
      onChange={(event) => {
        const raw = event.target.value
        if (!raw && declaration.type !== 'string') return clear()
        if (declaration.type === 'integer') return setValue(/^[+-]?\d+$/.test(raw) ? Number(raw) : raw)
        if (declaration.type === 'float') return setValue(Number.isFinite(Number(raw)) ? Number(raw) : raw)
        setValue(raw)
      }} className={common} />
  }
  return <label className="text-[11px]">
    <span className="mb-1 block font-medium text-foreground">{label}{declaration.required ? ' *' : ''}</span>
    {control}
    {declaration.type === 'datetime' && <span className="mt-1 block text-muted-foreground">Timezone required; the server records UTC.</span>}
    {declaration.help && <span className="mt-1 block text-muted-foreground">{declaration.help}</span>}
    {declaration.type === 'string' && !isBound && <Button type="button" size="sm" variant="ghost"
      onClick={() => setValue('')} className="mt-1 h-6 px-1.5 text-[10px]">Use empty string</Button>}
    {isBound && <Button type="button" size="sm" variant="ghost" onClick={clear}
      className="mt-1 h-6 px-1.5 text-[10px]">{declaration.default == null ? 'Clear binding' : 'Use default'}</Button>}
    {error && <span role="alert" className="mt-1 block text-destructive">{error}</span>}
  </label>
}

function InputDriftNotice({ drift, doc }: { drift: InputDrift; doc: CanvasDoc }) {
  const titles = new Map(doc.nodes.map((node) => [node.id, node.data.title]))
  return <div aria-label="Preview input drift" className="mt-2 flex flex-col gap-1.5">
    {drift.sources.map((source) => {
      const compatibility = source.compatibility
      const notable = compatibility?.fields.filter((field) => field.kind !== 'unchanged' || field.status !== 'compatible') ?? []
      return <div key={`${source.nodeId}:${source.previewRevisionId}`} className="rounded-md border border-border bg-muted/40 px-2 py-1.5 text-[10.5px]">
        <div className="font-semibold text-foreground">{titles.get(source.nodeId) ?? source.nodeId}</div>
        <div className="dp-mono mt-0.5 break-all text-muted-foreground">
          revision {source.previewRevisionId} → {source.latestRevisionId ?? 'latest unavailable'}
        </div>
        <div className="mt-0.5 text-muted-foreground">
          {!source.oldRevisionReadable ? 'Preview input is no longer readable; refresh is required before another run.'
            : compatibility ? `Schema compatibility: ${compatibility.status}` : 'Schema compatibility: unknown' }
        </div>
        {notable.slice(0, 3).map((field, index) => <div key={`${field.fieldId ?? field.oldName ?? field.newName}:${index}`}
          className="mt-0.5 text-[9.5px] text-muted-foreground">
          <span className="font-semibold text-foreground">{field.newName ?? field.oldName ?? field.fieldId ?? 'field'}: </span>{field.reason}
        </div>)}
      </div>
    })}
  </div>
}

function pinnedSourceInputs(doc: CanvasDoc, targetNodeId: string): { nodeId: string; title: string; ref: DatasetRef }[] {
  const byId = new Map(doc.nodes.map((node) => [node.id, node]))
  const incoming = new Map<string, string[]>()
  const children = new Map<string, string[]>()
  for (const edge of doc.edges) incoming.set(edge.target, [...(incoming.get(edge.target) ?? []), edge.source])
  for (const node of doc.nodes) {
    if (node.parentId) children.set(node.parentId, [...(children.get(node.parentId) ?? []), node.id])
  }
  const selected = new Set<string>()
  const pending = byId.has(targetNodeId) ? [targetNodeId] : []
  while (pending.length) {
    const current = pending.pop()!
    if (selected.has(current)) continue
    selected.add(current)
    pending.push(...(incoming.get(current) ?? []))
    if (byId.get(current)?.type === 'section') pending.push(...(children.get(current) ?? []))
  }
  return doc.nodes.flatMap((node) => {
    const ref = node.data.config.datasetRef
    return selected.has(node.id) && node.type === 'source' && ref && !isParameterRef(ref)
      ? [{ nodeId: node.id, title: node.data.title, ref }]
      : []
  })
}

function RunOutputs({ outputs }: { outputs: RunOutput[] }) {
  if (outputs.length === 0) return null
  return (
    <div aria-label="Run outputs" className="mt-2.5 flex flex-col gap-1.5">
      {outputs.map((output) => {
        const label = output.portLabel || output.portId
        return (
          <div key={`${output.nodeId}:${output.portId}`} className="rounded-md border border-border bg-muted/50 px-2 py-1.5 text-[10.5px]">
            <div className="flex items-center gap-1.5">
              <span className="dp-mono min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap font-semibold text-foreground"
                title={`${output.nodeId}:${output.portId}`}>{label}</span>
              {output.table && <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap text-foreground" title={output.table}>→ {output.table}</span>}
              {output.rows != null && <span className="shrink-0 text-muted-foreground">{output.rows.toLocaleString()} rows</span>}
              <span className={cn(
                'shrink-0 rounded px-1 py-px text-[9px] font-semibold uppercase tracking-[0.3px]',
                output.outcome === 'committed' ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                  : output.outcome === 'failed' ? 'bg-destructive/10 text-destructive'
                    : 'bg-muted text-muted-foreground',
              )}>{output.outcome}</span>
            </div>
            {output.uri && <div className="dp-mono mt-1 overflow-hidden text-ellipsis whitespace-nowrap text-muted-foreground" title={output.uri}>→ {output.uri}</div>}
            {output.writeReceipt && (
              <div aria-label="Durable write receipt" className="mt-1 text-muted-foreground">
                <span className="font-semibold text-foreground">revision {output.writeReceipt.revisionId}</span>
                {' · '}dataset {output.writeReceipt.datasetId}
                {' · '}{output.writeReceipt.bytes.toLocaleString()} bytes
                {output.writeReceipt.parentHead ? ` · parent ${output.writeReceipt.parentHead.revisionId}` : ' · no parent'}
                {output.writeReceipt.publication.backendVersion ? ` · backend ${output.writeReceipt.publication.backendVersion}` : ''}
              </div>
            )}
            {output.error && <div className="dp-mono mt-1 whitespace-pre-wrap text-destructive">{output.error}</div>}
          </div>
        )
      })}
    </div>
  )
}

function PerNode({ st, compact }: { st: { perNode: { nodeId: string; status: string; label?: string | null; rows?: number | null; error?: string | null }[] }; compact?: boolean }) {
  const items = st.perNode.filter((p) => p.nodeId !== '__error_gate__' || !compact)
  return (
    <div className={cn('flex flex-col gap-1', compact ? 'mt-3' : 'mt-1.5')}>
      {items.map((p) => {
        const s = statusTok[(p.status as keyof typeof statusTok)] ?? statusTok.queued
        return (
          <div key={p.nodeId} className="flex flex-col gap-0.5">
            <div className="flex items-center gap-2 text-[11px]">
              <span className={cn('w-2.5', p.status === 'running' && 'dp-running-glyph')} style={{ color: s.color }}>{s.glyph}</span>
              <span className={cn(p.status === 'failed' ? 'font-semibold text-destructive' : 'text-muted-foreground')}>{p.label ?? p.nodeId}</span>
              <span className="flex-1" />
              {p.rows != null && p.status === 'done' && <span className="text-muted-foreground">{p.rows.toLocaleString()} rows</span>}
            </div>
            {p.status === 'failed' && p.error && (
              <div className="dp-mono ml-[18px] whitespace-pre-wrap rounded bg-destructive/10 px-2 py-1 text-[10.5px] text-muted-foreground">{p.error}</div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function ProgressBar({ value }: { value: number }) {
  return (
    <div className="h-1.5 overflow-hidden rounded bg-muted">
      <div className="h-full rounded bg-primary transition-[width] duration-300" style={{ width: `${Math.min(100, Math.max(6, value * 100))}%` }} />
    </div>
  )
}

function Label({ children }: { children: React.ReactNode }) {
  return <div className="text-[9.5px] font-bold tracking-[0.6px] text-muted-foreground">{children}</div>
}

function fmtTime(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`
  if (seconds < 90) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`
  return `${(seconds / 3600).toFixed(1)} h`
}
