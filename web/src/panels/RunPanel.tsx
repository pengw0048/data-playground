import { useEffect } from 'react'
import { roleCanEdit, useStore } from '../store/graph'
import { color, status as statusTok } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { InputDrift, RunOutput } from '../types/api'
import { datasetRefIdentity, type CanvasDoc, type DatasetRef } from '../types/graph'

export function RunPanel({ nodeId }: { nodeId: string }) {
  const run = useStore((s) => s.runs[nodeId])
  const estimate = useStore((s) => s.estimate)
  const doRun = useStore((s) => s.run)
  const cancel = useStore((s) => s.cancelRun)
  const refreshPreviewInputs = useStore((s) => s.refreshPreviewInputs)
  const hasRetainedPreviewBinding = useStore((s) => !!s.previewBindings[nodeId])
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const doc = useStore((s) => s.doc)

  useEffect(() => {
    if (!run || run.phase === 'idle') estimate(nodeId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId])

  const phase = run?.phase ?? 'estimating'
  const est = run?.estimate
  const st = run?.status
  const pinnedInputs = pinnedSourceInputs(doc, nodeId)
  const writeAdmission = run?.writeAdmission
  const writeSubmissionUnresolved = Boolean(
    writeAdmission?.managed && writeAdmission.intent && run?.writeSubmissionId,
  )

  return (
    <div className="p-3.5">
      {(phase === 'estimating' || (!est && phase !== 'running' && phase !== 'done' && phase !== 'failed')) && (
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
          {writeAdmission && <WriteAdmissionSummary admission={writeAdmission} />}
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
        <>
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
    </div>
  )
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
    return selected.has(node.id) && node.type === 'source' && ref
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
              </div>
            )}
            {output.error && <div className="dp-mono mt-1 whitespace-pre-wrap text-destructive">{output.error}</div>}
          </div>
        )
      })}
    </div>
  )
}

function WriteAdmissionSummary({ admission }: { admission: import('../types/api').WriteAdmission }) {
  return (
    <div aria-label="Write admission" className="mt-2 rounded-md border border-border bg-muted/40 px-2 py-1.5 text-[10.5px] text-muted-foreground">
      <div className="font-semibold text-foreground">
        {admission.managed ? `${admission.mode} · ${admission.provider}` : `${admission.mode} · provider-neutral`}
      </div>
      <div className="dp-mono mt-0.5 break-all">{admission.destination}</div>
      <div className="mt-0.5">{admission.expectedSchema.length} schema fields · {admission.partitions.length ? `${admission.partitions.length} partitions` : 'unpartitioned'}</div>
      {admission.expectedHead && <div className="dp-mono mt-0.5">expected revision {admission.expectedHead.revisionId}</div>}
      {admission.blocker && <div className="mt-1 text-destructive">{admission.blocker}</div>}
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
