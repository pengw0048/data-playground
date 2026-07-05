import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, type CanvasVersionDto } from '../api/client'
import { useStore } from '../store/graph'
import { color, radius, shadow } from '../theme/tokens'
import { Icon } from '../ui/Icon'

// Server-side snapshot history for the current canvas (/canvas/{id}/versions) with one-click restore.
// Snapshots are captured (throttled) on every save, so a bad edit is recoverable after the fact.
export function VersionHistoryModal({ onClose }: { onClose: () => void }) {
  const canvasId = useStore((s) => s.doc.id)
  const loadDoc = useStore((s) => s.loadDoc)
  const pushToast = useStore((s) => s.pushToast)
  const [versions, setVersions] = useState<CanvasVersionDto[] | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')

  const load = () => api.listVersions(canvasId).then(setVersions).catch((e) => setErr((e as Error).message))
  useEffect(() => { load() }, [canvasId])

  const restore = async (v: CanvasVersionDto) => {
    setBusy(v.id)
    try {
      const r = await api.restoreCanvas(canvasId, v.id)
      loadDoc(r.doc)            // swap the canvas to the restored state (also snapshots the pre-restore state)
      pushToast('Restored an earlier version', 'success')
      onClose()
    } catch (e) {
      pushToast((e as Error).message || 'Restore failed', 'error')
    } finally {
      setBusy('')
    }
  }

  return createPortal(
    <div className="dp-modal-overlay" onClick={onClose} style={{ position: 'fixed', inset: 0, zIndex: 50, background: 'rgba(16,20,30,0.4)', display: 'grid', placeItems: 'center' }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: 560, maxHeight: '76vh', display: 'flex', flexDirection: 'column', background: '#fff', borderRadius: radius.panel, boxShadow: shadow.panel, overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '13px 16px', borderBottom: `1px solid ${color.hairline}` }}>
          <Icon name="clock" size={15} style={{ color: color.text3 }} />
          <span style={{ fontSize: 14, fontWeight: 600, color: color.ink }}>Version history</span>
          <span style={{ flex: 1 }} />
          <button onClick={onClose} aria-label="Close" style={{ border: 'none', background: 'transparent', color: color.text2, cursor: 'pointer' }}><Icon name="close" size={16} /></button>
        </div>
        <div style={{ overflowY: 'auto', padding: 8 }}>
          {err && <div style={{ padding: 16, color: color.failed, fontSize: 12.5 }}>Couldn’t load history: {err}</div>}
          {!err && versions === null && <div style={{ padding: 16, color: color.text3, fontSize: 12.5 }}>Loading…</div>}
          {!err && versions?.length === 0 && <div style={{ padding: 16, color: color.text3, fontSize: 12.5 }}>No snapshots yet — they’re captured as you edit.</div>}
          {versions?.map((v) => (
            <div key={v.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 10px', borderBottom: `1px solid ${color.hairline}`, fontSize: 12.5 }}>
              <Icon name={v.label ? 'refresh' : 'clock'} size={13} style={{ color: v.label ? color.focus : color.text3 }} />
              <span style={{ flex: 1, minWidth: 0, color: color.ink, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {v.label ?? `Snapshot · v${v.version}`}
              </span>
              <span style={{ color: color.text3, fontSize: 11 }}>{v.createdAt ? new Date(v.createdAt).toLocaleString() : ''}</span>
              <button onClick={() => restore(v)} disabled={!!busy}
                style={{ border: `1px solid ${color.border}`, borderRadius: 6, background: '#fff', color: color.ink, fontSize: 11.5, fontWeight: 600, padding: '4px 10px', cursor: busy ? 'default' : 'pointer', opacity: busy && busy !== v.id ? 0.5 : 1 }}>
                {busy === v.id ? 'Restoring…' : 'Restore'}
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>,
    document.body,
  )
}
