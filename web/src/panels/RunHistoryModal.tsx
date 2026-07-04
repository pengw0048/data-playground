import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, type RunRecordDto } from '../api/client'
import { useStore } from '../store/graph'
import { color, radius, shadow, status as statusTok } from '../theme/tokens'
import { Icon } from '../ui/Icon'

// Persisted run history for the current canvas (survives restarts) — /canvas/{id}/runs.
export function RunHistoryModal({ onClose }: { onClose: () => void }) {
  const canvasId = useStore((s) => s.doc.id)
  const [runs, setRuns] = useState<RunRecordDto[] | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    api.listRuns(canvasId).then(setRuns).catch((e) => setErr((e as Error).message))
  }, [canvasId])

  return createPortal(
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, zIndex: 50, background: 'rgba(16,20,30,0.4)', display: 'grid', placeItems: 'center' }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: 560, maxHeight: '76vh', display: 'flex', flexDirection: 'column', background: '#fff', borderRadius: radius.panel, boxShadow: shadow.panel, overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '13px 16px', borderBottom: `1px solid ${color.hairline}` }}>
          <Icon name="clock" size={15} style={{ color: color.text3 }} />
          <span style={{ fontSize: 14, fontWeight: 600, color: color.ink }}>Run history</span>
          <span style={{ flex: 1 }} />
          <button onClick={onClose} aria-label="Close" style={{ border: 'none', background: 'transparent', color: color.text2, cursor: 'pointer' }}><Icon name="close" size={16} /></button>
        </div>
        <div style={{ overflowY: 'auto', padding: 8 }}>
          {err && <div style={{ padding: 16, color: color.failed, fontSize: 12.5 }}>Couldn’t load run history: {err}</div>}
          {!err && runs === null && <div style={{ padding: 16, color: color.text3, fontSize: 12.5 }}>Loading…</div>}
          {!err && runs?.length === 0 && <div style={{ padding: 16, color: color.text3, fontSize: 12.5 }}>No runs yet — run a pipeline and it’ll show here.</div>}
          {runs?.map((r) => {
            const st = statusTok[r.status as keyof typeof statusTok] ?? statusTok.draft
            return (
              <div key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 10px', borderBottom: `1px solid ${color.hairline}`, fontSize: 12.5 }}>
                <span style={{ color: st.color, width: 12, textAlign: 'center' }}>{st.glyph}</span>
                <span style={{ width: 70, color: color.text2 }}>{r.status}</span>
                <span style={{ flex: 1, minWidth: 0, color: color.ink, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {r.outputTable ? `→ ${r.outputTable}` : r.targetNodeId ?? '—'}
                  {r.error && <span style={{ color: color.failed }}> · {r.error}</span>}
                </span>
                {r.rows != null && <span style={{ color: color.text3 }}>{r.rows.toLocaleString()} rows</span>}
                {r.ms != null && <span style={{ color: color.text3, width: 56, textAlign: 'right' }}>{r.ms} ms</span>}
                <span style={{ color: color.text3, width: 128, textAlign: 'right', fontSize: 11 }}>{r.createdAt ? new Date(r.createdAt).toLocaleString() : ''}</span>
              </div>
            )
          })}
        </div>
      </div>
    </div>,
    document.body,
  )
}
