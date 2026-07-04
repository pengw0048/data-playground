import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import { useStore } from '../store/graph'
import { color, radius, shadow } from '../theme/tokens'
import { Icon } from '../ui/Icon'

// Settings scoped to THIS canvas (not the app/workspace ones). Kept deliberately separate from the
// global SettingsModal — this is about the open file: its name and who can see it.
export function CanvasSettingsModal({ onClose }: { onClose: () => void }) {
  const doc = useStore((s) => s.doc)
  const renameFile = useStore((s) => s.renameFile)
  const [name, setName] = useState(doc.name ?? '')
  const [visibility, setVisibility] = useState<'private' | 'workspace'>('private')

  useEffect(() => {
    api.getShares(doc.id).then((s) => setVisibility(s.visibility === 'workspace' ? 'workspace' : 'private')).catch(() => {})
  }, [doc.id])

  const setVis = (v: 'private' | 'workspace') => {
    setVisibility(v)
    api.addShare(doc.id, { visibility: v }).catch(() => {})
  }

  return createPortal(
    <div onMouseDown={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(20,22,28,.28)', zIndex: 2000, display: 'grid', placeItems: 'center' }}>
      <div onMouseDown={(e) => e.stopPropagation()}
        style={{ width: 420, maxWidth: '92vw', background: '#fff', border: `1px solid ${color.border}`, borderRadius: radius.panel, boxShadow: shadow.panel }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 16px', borderBottom: `1px solid ${color.hairline}` }}>
          <Icon name="grid" size={14} style={{ color: color.text2 }} />
          <span style={{ fontSize: 14, fontWeight: 600 }}>Canvas settings</span>
          <span style={{ flex: 1 }} />
          <button aria-label="Close" onClick={onClose} style={{ width: 26, height: 24, border: 'none', background: 'transparent', color: color.text3, display: 'grid', placeItems: 'center' }}><Icon name="close" size={13} /></button>
        </div>
        <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 16 }}>
          <label style={{ display: 'block' }}>
            <div style={{ fontSize: 11.5, color: color.text2, marginBottom: 4 }}>Name</div>
            <input value={name} onChange={(e) => { setName(e.target.value); renameFile(e.target.value) }}
              placeholder="untitled" style={{ width: '100%', fontSize: 12.5, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', outline: 'none' }} />
          </label>
          <div>
            <div style={{ fontSize: 11.5, color: color.text2, marginBottom: 6 }}>Visibility</div>
            <div style={{ display: 'flex', gap: 8 }}>
              {(['private', 'workspace'] as const).map((v) => (
                <button key={v} onClick={() => setVis(v)}
                  style={{ flex: 1, padding: '9px 10px', border: `1px solid ${visibility === v ? color.focus : color.border}`, borderRadius: 8, background: visibility === v ? '#eef2fe' : '#fff', color: color.ink, fontSize: 12, fontWeight: 600, cursor: 'pointer', textAlign: 'left' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <Icon name={v === 'private' ? 'grid' : 'link'} size={12} /> {v === 'private' ? 'Private' : 'Workspace'}
                  </div>
                  <div style={{ fontSize: 10.5, fontWeight: 400, color: color.text3, marginTop: 3 }}>{v === 'private' ? 'Only you and people you invite' : 'Everyone in the workspace can edit'}</div>
                </button>
              ))}
            </div>
            <div style={{ fontSize: 10.5, color: color.text3, marginTop: 8 }}>Invite specific people from the <b>Share</b> button.</div>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
