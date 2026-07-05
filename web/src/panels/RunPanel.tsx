import { useEffect } from 'react'
import { useStore } from '../store/graph'
import { color, radius, status as statusTok } from '../theme/tokens'
import { Icon } from '../ui/Icon'

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
    <div style={{ padding: 14 }}>
      {(phase === 'estimating' || (!est && phase !== 'running' && phase !== 'done' && phase !== 'failed')) && (
        <div style={{ color: color.text3, fontSize: 12, padding: '10px 0' }}>estimating…</div>
      )}

      {(phase === 'estimated' || phase === 'confirm') && est && (
        <>
          <Label>{phase === 'confirm' ? 'HEADS UP' : 'ESTIMATE'}</Label>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 2 }}>
            <span style={{ fontSize: 24, fontWeight: 700, color: color.ink }}>{est.rows.toLocaleString()} rows</span>
            <span style={{ fontSize: 13, color: color.text2 }}>· ~{fmtTime(est.seconds)}</span>
          </div>
          {est.breakdown && <div style={{ fontSize: 11, color: color.text3, marginTop: 8 }}>{est.breakdown}</div>}
          {phase === 'confirm' ? (
            <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
              <button onClick={() => doRun(nodeId, true)} style={btn('#d99a2b', '#fff', 1)}>Run</button>
              <button onClick={() => useStore.getState().closePanel(nodeId)} style={btn('#fff', color.text2, 1, true)}>Cancel</button>
            </div>
          ) : (
            <button onClick={() => doRun(nodeId, false)} style={{ ...btn(color.ink, '#fff', 1), marginTop: 14 }}>Run</button>
          )}
        </>
      )}

      {phase === 'running' && st && (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
            <span className="dp-running-glyph" style={{ color: color.running }}>●</span>
            <span style={{ fontSize: 13, fontWeight: 600 }}>running</span>
          </div>
          <ProgressBar value={st.totalRows ? st.rowsProcessed / Math.max(1, st.totalRows) : 0.3} />
          <div style={{ fontSize: 11.5, color: color.text2, margin: '8px 0' }}>
            {st.rowsProcessed.toLocaleString()}{st.totalRows ? ` / ${st.totalRows.toLocaleString()}` : ''} rows
          </div>
          <PerNode st={st} />
          <button onClick={() => cancel(nodeId)} style={{ ...btn('#fff', color.ink, 1, true), marginTop: 12 }}>
            <Icon name="stop" size={12} /> &nbsp;Stop
          </button>
        </>
      )}

      {phase === 'done' && st && (
        <>
          <Label>DONE</Label>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 2 }}>
            <span style={{ color: color.latest, fontSize: 16 }}>✓</span>
            <span style={{ fontSize: 22, fontWeight: 700, color: color.ink }}>{(st.totalRows ?? st.rowsProcessed).toLocaleString()} rows</span>
            <span style={{ fontSize: 13, color: color.text2 }}>· {fmtTime(st.ms / 1000)}</span>
          </div>
          {st.outputTable && <div style={{ fontSize: 12, color: color.ink, marginTop: 10 }}>wrote <b>{st.outputTable}</b></div>}
          {st.outputUri && (
            <div title={st.outputUri} className="dp-mono" style={{ fontSize: 10.5, color: color.text2, marginTop: 6, background: '#f7f8fa', border: `1px solid ${color.hairline}`, borderRadius: 6, padding: '5px 8px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              → {st.outputUri}
            </div>
          )}
          <PerNode st={st} compact />
        </>
      )}

      {phase === 'failed' && (
        <div style={{ padding: '8px 0' }}>
          <Label>FAILED</Label>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
            <span style={{ color: color.failed }}>✕</span>
            <span style={{ fontSize: 13, fontWeight: 600, color: color.failed }}>run failed</span>
          </div>
          <div className="dp-mono" style={{ fontSize: 11, color: color.text2, marginTop: 8, background: '#fbeff0', padding: 10, borderRadius: 8, whiteSpace: 'pre-wrap' }}>
            {run?.error ?? st?.error ?? 'unknown error'}
          </div>
          <button onClick={() => estimate(nodeId)} style={{ ...btn('#fff', color.ink, 1, true), marginTop: 12 }}>Retry</button>
        </div>
      )}
    </div>
  )
}

function PerNode({ st, compact }: { st: { perNode: { nodeId: string; status: string; label?: string | null; rows?: number | null }[] }; compact?: boolean }) {
  const items = st.perNode.filter((p) => p.nodeId !== '__error_gate__' || !compact)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: compact ? 12 : 6 }}>
      {items.map((p) => {
        const s = statusTok[(p.status as keyof typeof statusTok)] ?? statusTok.queued
        return (
          <div key={p.nodeId} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11 }}>
            <span className={p.status === 'running' ? 'dp-running-glyph' : undefined} style={{ color: s.color, width: 10 }}>{s.glyph}</span>
            <span style={{ color: color.text2 }}>{p.label ?? p.nodeId}</span>
            <span style={{ flex: 1 }} />
            {p.rows != null && p.status === 'done' && <span style={{ color: color.text3 }}>{p.rows.toLocaleString()} rows</span>}
          </div>
        )
      })}
    </div>
  )
}

function ProgressBar({ value }: { value: number }) {
  return (
    <div style={{ height: 6, background: '#eceef1', borderRadius: 4, overflow: 'hidden' }}>
      <div style={{ height: '100%', width: `${Math.min(100, Math.max(6, value * 100))}%`, background: color.running, borderRadius: 4, transition: 'width .3s' }} />
    </div>
  )
}

function Label({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: 0.6, color: color.text3 }}>{children}</div>
}

function btn(bg: string, fg: string, flex = 0, outline = false): React.CSSProperties {
  return {
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 4, flex: flex ? 1 : undefined,
    padding: '9px 14px', border: outline ? `1px solid ${color.border}` : 'none', borderRadius: radius.button,
    background: bg, color: fg, fontSize: 12.5, fontWeight: 600, width: flex ? undefined : '100%',
  }
}

function fmtTime(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`
  if (seconds < 90) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`
  return `${(seconds / 3600).toFixed(1)} h`
}
