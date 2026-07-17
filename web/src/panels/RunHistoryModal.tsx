import { useEffect, useState } from 'react'
import { api, type PerNodeStat, type RunRecordDto } from '../api/client'
import { useStore } from '../store/graph'
import { status as statusTok } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { FullResult } from './DataPanel'
import { SampleProvenanceSummary } from './DataPanel'
import type { RunOutput } from '../types/api'

// Persisted run history + telemetry for the current canvas (survives restarts) — /canvas/{id}/runs.
// Charts are native inline SVG (no external lib) so they work fully offline and theme-aware.
export function RunHistoryModal({ onClose }: { onClose: () => void }) {
  const canvasId = useStore((s) => s.doc.id)
  const [runs, setRuns] = useState<RunRecordDto[] | null>(null)
  const [err, setErr] = useState('')
  const [open, setOpen] = useState<string | null>(null)  // expanded run id → per-node breakdown
  const [resultOpen, setResultOpen] = useState<string | null>(null)
  useEffect(() => {
    api.listRuns(canvasId).then(setRuns).catch((e) => setErr((e as Error).message))
  }, [canvasId])

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="dp-modal-overlay flex max-h-[80vh] w-[620px] max-w-[calc(100vw-2rem)] flex-col gap-0 overflow-hidden p-0 [&>button]:hidden">
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <span className="text-muted-foreground"><Icon name="clock" size={15} /></span>
          <DialogTitle className="text-sm font-semibold text-foreground">Run history</DialogTitle>
          <span className="flex-1" />
          <button onClick={onClose} aria-label="Close" className="cursor-pointer border-0 bg-transparent p-0 text-muted-foreground hover:text-foreground"><Icon name="close" size={16} /></button>
        </div>
        <div className="overflow-y-auto">
          {err && <div className="p-4 text-[12.5px] text-destructive">Couldn’t load run history: {err}</div>}
          {!err && runs === null && <div className="p-4 text-[12.5px] text-muted-foreground">Loading…</div>}
          {!err && runs?.length === 0 && <div className="p-4 text-[12.5px] text-muted-foreground">No runs yet — run a pipeline and it’ll show here.</div>}
          {runs && runs.length > 0 && <DurationTrend runs={runs} />}
          <div className="p-2">
            {runs?.map((r) => {
              const st = statusTok[r.status as keyof typeof statusTok] ?? statusTok.draft
              const hasNodes = !!r.perNode && r.perNode.length > 0
              const isOpen = open === r.id
              return (
                <div key={r.id} className="border-b border-border">
                  <div
                    className={`flex items-center gap-2.5 px-2.5 py-2 text-[12.5px] ${hasNodes ? 'cursor-pointer hover:bg-muted/40' : ''}`}
                    onClick={() => hasNodes && setOpen(isOpen ? null : r.id)}
                  >
                    <span className="w-3 text-center text-muted-foreground">{hasNodes ? (isOpen ? '▾' : '▸') : ''}</span>
                    <span className="w-3 text-center" style={{ color: st.color }}>{st.glyph}</span>
                    <Badge variant="secondary" className="w-[70px] justify-center">{r.status}</Badge>
                    <Badge variant="outline" className="w-[54px] justify-center capitalize">{r.jobType}</Badge>
                    <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-foreground">
                      {r.targetNodeId ?? '—'}
                      {r.outputs.length > 0 && <span className="text-muted-foreground"> · {r.outputs.length} output{r.outputs.length === 1 ? '' : 's'}</span>}
                      {r.error && <span className="text-destructive"> · {r.error}</span>}
                    </span>
                    {r.rows != null && <span className="text-muted-foreground">{r.rows.toLocaleString()} rows</span>}
                    {r.ms != null && <span className="w-16 text-right text-muted-foreground">{fmtMs(r.ms)}</span>}
                    <span className="w-32 text-right text-[11px] text-muted-foreground">{r.createdAt ? new Date(r.createdAt).toLocaleString() : ''}</span>
                  </div>
                  {isOpen && hasNodes && <PerNodeBreakdown nodes={r.perNode!} />}
                  {r.inputManifest && r.inputManifest.length > 0 && (
                    <div aria-label={`Resolved inputs for run ${r.id}`} className="border-t border-border bg-muted/20 px-4 py-2 text-[10.5px] text-muted-foreground">
                      <div className="font-semibold text-foreground">Resolved exact inputs</div>
                      {r.inputManifest.map((input) => (
                        <div key={input.nodeId} className="mt-0.5 break-all">
                          {input.nodeId} · dataset {input.datasetId} · revision {input.revisionId} · {input.provider}
                        </div>
                      ))}
                    </div>
                  )}
                  {r.outputs.length > 0 && (
                    <HistoryOutputs historyId={r.id} runId={r.runId ?? undefined}
                      outputs={r.outputs} openKey={resultOpen}
                      onToggle={(key) => setResultOpen(resultOpen === key ? null : key)} />
                  )}
                  {r.profile?.sampleProvenance && (
                    <div className="border-t border-border bg-muted/20 px-4 py-2">
                      <SampleProvenanceSummary provenance={r.profile.sampleProvenance} />
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

function historyOutputKey(runId: string, output: RunOutput): string {
  return JSON.stringify([runId, output.nodeId, output.portId])
}

function HistoryOutputs({ historyId, runId, outputs, openKey, onToggle }: {
  historyId: string
  runId?: string
  outputs: RunOutput[]
  openKey: string | null
  onToggle: (key: string) => void
}) {
  return (
    <div aria-label={`Outputs for run ${historyId}`} className="border-t border-border bg-muted/20">
      {outputs.map((output) => {
        const key = historyOutputKey(historyId, output)
        const readable = output.outcome === 'committed' && !!output.uri
        const label = output.portLabel || output.portId
        const publishedDataset = output.publicationKind === 'catalog'
        return (
          <div key={`${output.nodeId}:${output.portId}`} className="border-b border-border/60 last:border-b-0">
            <div className="flex items-center gap-2 px-4 py-2 text-[11px]">
              <span className="dp-mono min-w-0 max-w-36 overflow-hidden text-ellipsis whitespace-nowrap font-semibold text-foreground"
                title={`${output.nodeId}:${output.portId}`}>{label}</span>
              <Badge variant="outline" className="h-5 px-1.5 text-[9px] uppercase">{output.outcome}</Badge>
              <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-muted-foreground"
                title={output.table || output.uri || undefined}>
                {output.table ? `→ ${output.table}` : output.uri ? `→ ${output.uri}` : output.publicationKind}
              </span>
              {output.rows != null && (
                <span className="shrink-0 text-muted-foreground">
                  {output.rows.toLocaleString()} rows{output.publicationKind === 'catalog' ? ' written' : ''}
                </span>
              )}
              {readable && (
                <Button variant="ghost" size="sm" className="h-6 px-2 text-[10.5px]"
                  onClick={() => onToggle(key)}>
                  {openKey === key
                    ? publishedDataset ? 'Hide dataset' : 'Hide result'
                    : outputs.length === 1
                      ? publishedDataset ? 'Open published dataset' : 'Open full result'
                      : `Open ${label}`}
                </Button>
              )}
            </div>
            {output.error && <div className="dp-mono px-4 pb-2 text-[10.5px] text-destructive">{output.error}</div>}
            {output.sampleProvenance && <div className="px-4 pb-2"><SampleProvenanceSummary provenance={output.sampleProvenance} /></div>}
            {openKey === key && readable && (
              <div className="border-t border-border">
                <FullResult uri={output.uri!}
                  total={output.publicationKind === 'result' ? output.rows ?? null : null}
                  runId={runId} nodeId={output.nodeId} portId={output.portId}
                  publicationKind={output.publicationKind}
                  name={`${output.nodeId}-${label}`} />
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export function fmtMs(ms: number): string {
  if (ms < 1000) return `${ms} ms`
  const totalSec = Math.round(ms / 1000)  // round FIRST so unit choice + carry are consistent (no "60 s"/"1m 60s")
  if (totalSec < 60) return totalSec < 10 ? `${(ms / 1000).toFixed(1)} s` : `${totalSec} s`
  return `${Math.floor(totalSec / 60)}m ${totalSec % 60}s`
}

// A compact bar-per-run duration trend (oldest → newest), colored by status. Native SVG.
export function DurationTrend({ runs }: { runs: RunRecordDto[] }) {
  const chron = [...runs].reverse()  // list is newest-first; chart reads left→right in time
  const max = Math.max(1, ...chron.map((r) => r.ms ?? 0))
  const W = 6, GAP = 2, H = 44
  const width = chron.length * (W + GAP)
  return (
    <div className="border-b border-border px-4 py-3">
      <div className="mb-1.5 flex items-baseline justify-between text-[11px] text-muted-foreground">
        <span>Run duration · last {chron.length}</span>
        <span>max {fmtMs(max)}</span>
      </div>
      <svg width="100%" height={H} viewBox={`0 0 ${Math.max(width, 1)} ${H}`} preserveAspectRatio="none" role="img" aria-label="run duration trend">
        {chron.map((r, i) => {
          const st = statusTok[r.status as keyof typeof statusTok] ?? statusTok.draft
          const h = Math.max(2, Math.round(((r.ms ?? 0) / max) * (H - 2)))
          return (
            <rect key={r.id} x={i * (W + GAP)} y={H - h} width={W} height={h} rx={1} fill={st.color} opacity={0.85}>
              <title>{`${r.status} · ${fmtMs(r.ms ?? 0)}${r.rows != null ? ` · ${r.rows.toLocaleString()} rows` : ''}${r.createdAt ? `\n${new Date(r.createdAt).toLocaleString()}` : ''}`}</title>
            </rect>
          )
        })}
      </svg>
    </div>
  )
}

// Per-node plan-build-time/row breakdown for one run — a horizontal bar chart. Native SVG.
export function PerNodeBreakdown({ nodes }: { nodes: PerNodeStat[] }) {
  const max = Math.max(1, ...nodes.map((n) => n.ms ?? 0))
  return (
    <div className="bg-muted/30 px-3 py-2.5">
      {/* honest label (DATA-05): this is the time to BUILD each node's lazy plan step, not to
          materialize it — the out-of-core engine defers the heavy work to the target's single pass,
          so don't read these as each node's share of the run. */}
      <div className="mb-1.5 text-[11px] text-muted-foreground"
           title="Time to build each node's lazy plan step — not its materialization time (the engine defers the heavy work to the target's single pass).">
        Plan build time per node
      </div>
      <div className="flex flex-col gap-1">
        {nodes.map((n) => {
          const st = statusTok[n.status as keyof typeof statusTok] ?? statusTok.draft
          const pct = Math.max(2, Math.round(((n.ms ?? 0) / max) * 100))
          return (
            <div key={n.nodeId} className="flex items-center gap-2 text-[11.5px]">
              <span className="w-28 shrink-0 overflow-hidden text-ellipsis whitespace-nowrap text-foreground" title={n.nodeId}>
                {n.label || n.nodeId}
              </span>
              <div className="relative h-3.5 min-w-0 flex-1 rounded-sm bg-border/60">
                <div className="absolute inset-y-0 left-0 rounded-sm" style={{ width: `${pct}%`, backgroundColor: st.color, opacity: 0.85 }} />
              </div>
              <span className="w-14 shrink-0 text-right text-muted-foreground">{n.ms != null ? fmtMs(n.ms) : '—'}</span>
              <span className="w-16 shrink-0 text-right text-muted-foreground">{n.rows != null ? `${n.rows.toLocaleString()}` : ''}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
