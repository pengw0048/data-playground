import { useEffect } from 'react'
import { useStore } from '../store/graph'
import { color, status as statusTok } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export function RunPanel({ nodeId }: { nodeId: string }) {
  const run = useStore((s) => s.runs[nodeId])
  const estimate = useStore((s) => s.estimate)
  const doRun = useStore((s) => s.run)
  const cancel = useStore((s) => s.cancelRun)

  useEffect(() => {
    if (!run || run.phase === 'idle') estimate(nodeId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId])

  const phase = run?.phase ?? 'estimating'
  const est = run?.estimate
  const st = run?.status

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
          {phase === 'confirm' ? (
            <div className="mt-3.5 flex gap-2">
              <Button size="sm" onClick={() => doRun(nodeId, true)} className="flex-1 bg-[#d99a2b] text-white hover:bg-[#c98d24]">Run</Button>
              <Button size="sm" variant="outline" onClick={() => useStore.getState().closePanel(nodeId)} className="flex-1">Cancel</Button>
            </div>
          ) : (
            <Button size="sm" onClick={() => doRun(nodeId, false)} className="mt-3.5 w-full">Run</Button>
          )}
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
          <Button size="sm" variant="outline" onClick={() => cancel(nodeId)} className="mt-3 w-full">
            <Icon name="stop" size={12} /> Stop
          </Button>
        </>
      )}

      {phase === 'done' && st && (
        <>
          <Label>DONE</Label>
          <div className="mt-0.5 flex items-baseline gap-2">
            <span className="text-base" style={{ color: color.latest }}>✓</span>
            <span className="text-[22px] font-bold text-foreground">{(st.totalRows ?? st.rowsProcessed).toLocaleString()} rows</span>
            <span className="text-[13px] text-muted-foreground">· {fmtTime(st.ms / 1000)}</span>
          </div>
          {st.outputTable && <div className="mt-2.5 text-xs text-foreground">wrote <b>{st.outputTable}</b></div>}
          {st.outputUri && (
            <div title={st.outputUri} className="dp-mono mt-1.5 overflow-hidden text-ellipsis whitespace-nowrap rounded-md border border-border bg-muted px-2 py-[5px] text-[10.5px] text-muted-foreground">
              → {st.outputUri}
            </div>
          )}
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
          <Button size="sm" variant="outline" onClick={() => estimate(nodeId)} className="mt-3 w-full">Retry</Button>
        </div>
      )}
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
