import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, type ShareInfo } from '../api/client'
import { useStore } from '../store/graph'
import { canvasLink } from '../router'
import { color, radius, shadow } from '../theme/tokens'
import { Icon } from '../ui/Icon'

// Share a canvas: workspace visibility + explicit collaborators (owner-only). Mirrors Figma's Share.
export function ShareModal({ onClose }: { onClose: () => void }) {
  const canvasId = useStore((s) => s.doc.id)
  const users = useStore((s) => s.users)
  const currentUser = useStore((s) => s.currentUser)
  const pushToast = useStore((s) => s.pushToast)
  const [visibility, setVisibility] = useState('private')
  const [shares, setShares] = useState<ShareInfo[]>([])
  const [pick, setPick] = useState('')

  const load = () => api.getShares(canvasId).then((r) => { setVisibility(r.visibility); setShares(r.shares) }).catch(() => {})
  useEffect(() => { load() }, [canvasId])

  const setVis = async (v: string) => { setVisibility(v); await api.addShare(canvasId, { visibility: v }).catch((e) => pushToast((e as Error).message, 'error')) }
  const add = async () => {
    if (!pick) return
    await api.addShare(canvasId, { userId: pick, role: 'editor' }).catch((e) => pushToast((e as Error).message, 'error'))
    setPick(''); load()
  }
  const remove = async (userId: string) => { await api.removeShare(canvasId, userId).catch(() => {}); load() }

  const sharedIds = new Set([currentUser?.id, ...shares.map((s) => s.userId)])
  const addable = users.filter((u) => !sharedIds.has(u.id))

  return createPortal(
    <div className="dp-modal-overlay" onClick={onClose} style={{ position: 'fixed', inset: 0, zIndex: 50, background: 'rgba(16,20,30,0.4)', display: 'grid', placeItems: 'center' }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: 460, background: '#fff', borderRadius: radius.panel, boxShadow: shadow.panel, overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '13px 16px', borderBottom: `1px solid ${color.hairline}` }}>
          <Icon name="link" size={15} style={{ color: color.text3 }} />
          <span style={{ fontSize: 14, fontWeight: 600, color: color.ink }}>Share this canvas</span>
          <span style={{ flex: 1 }} />
          <button onClick={onClose} aria-label="Close" style={{ border: 'none', background: 'transparent', color: color.text2, cursor: 'pointer' }}><Icon name="close" size={16} /></button>
        </div>
        <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', color: color.text3, marginBottom: 6 }}>Link</div>
            <div style={{ display: 'flex', gap: 6 }}>
              <input readOnly value={canvasLink(canvasId)} onClick={(e) => (e.target as HTMLInputElement).select()}
                style={{ flex: 1, fontSize: 11.5, border: `1px solid ${color.border}`, borderRadius: 6, padding: '6px 8px', color: color.text2, background: '#f7f8fa', outline: 'none' }} />
              <button data-testid="copy-link" onClick={() => { navigator.clipboard?.writeText(canvasLink(canvasId)).then(() => pushToast('Link copied', 'success'), () => {}) }}
                style={{ border: `1px solid ${color.border}`, borderRadius: 6, background: '#fff', color: color.ink, fontSize: 12, fontWeight: 600, padding: '0 12px', cursor: 'pointer' }}>Copy</button>
            </div>
            <div style={{ fontSize: 10.5, color: color.text3, marginTop: 5 }}>Opens this canvas directly. People need at least workspace access (or an explicit invite below).</div>
          </div>
          <div>
            <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', color: color.text3, marginBottom: 6 }}>Visibility</div>
            <div style={{ display: 'inline-flex', gap: 3, background: '#f1f2f4', padding: 2, borderRadius: radius.button }}>
              {(['private', 'workspace'] as const).map((v) => (
                <button key={v} onClick={() => setVis(v)}
                  style={{ fontSize: 11.5, fontWeight: 600, padding: '4px 12px', border: 'none', borderRadius: 6, cursor: 'pointer', background: visibility === v ? color.ink : 'transparent', color: visibility === v ? '#fff' : color.text2 }}>
                  {v === 'private' ? 'Private' : 'Everyone in workspace'}
                </button>
              ))}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', color: color.text3, marginBottom: 6 }}>Collaborators</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, color: color.text2 }}>
                <span style={{ width: 22, height: 22, borderRadius: '50%', background: '#e7e0fb', color: '#6b4bd6', display: 'grid', placeItems: 'center', fontSize: 10, fontWeight: 700 }}>{(currentUser?.name ?? '?').slice(0, 1).toUpperCase()}</span>
                {currentUser?.name ?? 'you'} <span style={{ color: color.text3 }}>· owner</span>
              </div>
              {shares.map((sh) => (
                <div key={sh.userId} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, color: color.ink }}>
                  <span style={{ width: 22, height: 22, borderRadius: '50%', background: '#eef0f3', color: color.text2, display: 'grid', placeItems: 'center', fontSize: 10, fontWeight: 700 }}>{sh.name.slice(0, 1).toUpperCase()}</span>
                  <span style={{ flex: 1 }}>{sh.name} <span style={{ color: color.text3 }}>· {sh.role}</span></span>
                  <button onClick={() => remove(sh.userId)} title="Remove" style={{ border: 'none', background: 'transparent', color: color.text3, cursor: 'pointer' }}><Icon name="close" size={13} /></button>
                </div>
              ))}
            </div>
            {addable.length > 0 && (
              <div style={{ display: 'flex', gap: 6, marginTop: 10 }}>
                <select value={pick} onChange={(e) => setPick(e.target.value)}
                  style={{ flex: 1, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 6, padding: '6px 8px', background: '#fff' }}>
                  <option value="">Add a collaborator…</option>
                  {addable.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
                </select>
                <button onClick={add} disabled={!pick} style={{ border: 'none', borderRadius: 6, background: color.ink, color: '#fff', fontSize: 12, fontWeight: 600, padding: '0 14px', cursor: pick ? 'pointer' : 'not-allowed', opacity: pick ? 1 : 0.5 }}>Add</button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
