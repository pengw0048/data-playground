import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { useStore } from '../store/graph'
import { color, shadow } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { Popover } from '../ui/Popover'
import { SettingsModal } from '../panels/SettingsModal'
import { CanvasSettingsModal } from '../panels/CanvasSettingsModal'
import { RunHistoryModal } from '../panels/RunHistoryModal'
import { VersionHistoryModal } from '../panels/VersionHistoryModal'
import { ShareModal } from '../panels/ShareModal'
import { crdtUndoActive } from '../collab/undo'

export function TopBar() {
  const doc = useStore((s) => s.doc)
  const kernelUp = useStore((s) => s.kernelUp)
  const kernelInfo = useStore((s) => s.kernelInfo)
  const saved = useStore((s) => s.saved)
  const rerunAll = useStore((s) => s.rerunAll)
  // in a co-edit session undo/redo go through the CRDT manager (not the snapshot stacks), so enable the
  // buttons whenever collab is active — pressing with empty history is a harmless no-op
  const canUndo = useStore((s) => s.past.length > 0) || crdtUndoActive()
  const canRedo = useStore((s) => s.future.length > 0) || crdtUndoActive()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [canvasSettingsOpen, setCanvasSettingsOpen] = useState(false)
  const [runsOpen, setRunsOpen] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)

  // let anything (e.g. the agent's "Configure a model" CTA) open Settings
  useEffect(() => {
    const onOpen = () => setSettingsOpen(true)
    window.addEventListener('dp-open-settings', onOpen)
    return () => window.removeEventListener('dp-open-settings', onOpen)
  }, [])

  return (
    <>
      <div style={{ position: 'absolute', top: 16, left: 20, zIndex: 15, display: 'flex', alignItems: 'center', gap: 8 }}>
        <AppMenu onSettings={() => setSettingsOpen(true)} onRunHistory={() => setRunsOpen(true)} onVersionHistory={() => setVersionsOpen(true)} />
        <span style={{ fontSize: 13.5, color: color.text3 }}>/</span>
        <FileMenu onCanvasSettings={() => setCanvasSettingsOpen(true)} />
        <span data-testid="autosave" title={!kernelUp && saved ? 'Kernel offline — saved to this browser only' : undefined} style={{ fontSize: 11, color: color.text3, marginLeft: 2 }}>· {saved ? (kernelUp ? 'saved' : 'saved locally') : 'saving…'}</span>
        <span style={{ display: 'inline-flex', gap: 2, marginLeft: 6 }}>
          <IconBtn name="undo" label="Undo" disabled={!canUndo} onClick={() => useStore.getState().undo()} />
          <IconBtn name="redo" label="Redo" disabled={!canRedo} onClick={() => useStore.getState().redo()} />
        </span>
      </div>

      <div style={{ position: 'absolute', top: 16, right: 20, zIndex: 15, display: 'flex', alignItems: 'center', gap: 10 }}>
        <PeerAvatars />
        <div
          title={kernelInfo ? `${kernelInfo.backend} · ${kernelInfo.runners.join(', ')}` : 'kernel offline'}
          style={{
            display: 'flex', alignItems: 'center', gap: 7, padding: '6px 12px', background: '#fff',
            border: `1px solid ${color.border}`, borderRadius: 20, boxShadow: shadow.card, fontSize: 12, color: color.text2,
          }}
        >
          <span style={{ width: 8, height: 8, borderRadius: '50%', background: kernelUp ? color.latest : color.failed }} />
          kernel · {kernelUp ? 'warm' : 'offline'}
        </div>
        <button onClick={rerunAll} title="Re-run the whole graph" style={{ ...pill, background: color.ink, color: '#fff', border: 'none' }}>
          <Icon name="refresh" size={13} /> Rerun all
        </button>
        <button data-testid="share-btn" onClick={() => setShareOpen(true)} title="Share this canvas"
          style={{ ...pill, background: '#2f6ef0', color: '#fff', border: 'none' }}>
          <Icon name="link" size={13} /> Share
        </button>
        {/* Settings lives in the app menu (top-left); identity + log out live on the files shell —
            no redundant Settings button / account avatar here. */}
      </div>
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
      {canvasSettingsOpen && <CanvasSettingsModal onClose={() => setCanvasSettingsOpen(false)} />}
      {runsOpen && <RunHistoryModal onClose={() => setRunsOpen(false)} />}
      {versionsOpen && <VersionHistoryModal onClose={() => setVersionsOpen(false)} />}
      {shareOpen && <ShareModal onClose={() => setShareOpen(false)} />}
    </>
  )
}

// The app menu (Figma-style hamburger): Back to files, New file, Settings.
// Live presence: avatars of other people currently on this canvas (realtime collab).
function PeerAvatars() {
  const peers = useStore((s) => s.peers)
  const list = Object.entries(peers)
  if (list.length === 0) return null
  return (
    <div style={{ display: 'flex', alignItems: 'center' }} title={`${list.length} other${list.length > 1 ? 's' : ''} here`}>
      {list.slice(0, 5).map(([id, p], i) => (
        <span key={id} style={{ width: 26, height: 26, borderRadius: '50%', background: p.color, color: '#fff', display: 'grid', placeItems: 'center', fontSize: 11, fontWeight: 700, border: '2px solid #fff', marginLeft: i === 0 ? 0 : -8, boxShadow: shadow.card }}>
          {(p.name || '?').slice(0, 1).toUpperCase()}
        </span>
      ))}
    </div>
  )
}

function AppMenu({ onSettings, onRunHistory, onVersionHistory }: { onSettings: () => void; onRunHistory: () => void; onVersionHistory: () => void }) {
  const ref = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const setView = useStore((s) => s.setView)
  const newFile = useStore((s) => s.newFile)
  return (
    <>
      <button ref={ref} data-testid="app-menu" onClick={() => setOpen((v) => !v)} title="Menu"
        style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 13.5, fontWeight: 700, color: color.ink, background: 'transparent', border: 'none', cursor: 'pointer', padding: '2px 4px', borderRadius: 6 }}>
        <span style={{ width: 20, height: 20, borderRadius: 5, background: color.ink, color: '#fff', display: 'grid', placeItems: 'center', fontSize: 12, fontWeight: 700 }}>D</span>
        <Icon name="chevronDown" size={12} style={{ color: color.text3 }} />
      </button>
      <Popover anchorRef={ref} open={open} onClose={() => setOpen(false)} width={210} align="left">
        <MenuItem icon="chevronLeft" label="Back to files" onClick={() => { setView('files'); setOpen(false) }} />
        <MenuItem icon="plus" label="New file" onClick={() => { newFile(); setOpen(false) }} />
        <MenuItem icon="clock" label="Run history" onClick={() => { onRunHistory(); setOpen(false) }} />
        <MenuItem icon="refresh" label="Version history" onClick={() => { onVersionHistory(); setOpen(false) }} />
        <div style={{ height: 1, background: color.hairline, margin: '4px 0' }} />
        <MenuItem icon="settings" label="Settings" onClick={() => { onSettings(); setOpen(false) }} />
      </Popover>
    </>
  )
}

function FileMenu({ onCanvasSettings }: { onCanvasSettings: () => void }) {
  const ref = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const doc = useStore((s) => s.doc)
  const files = useStore((s) => s.files)
  const openFile = useStore((s) => s.openFile)
  const newFile = useStore((s) => s.newFile)
  const renameFile = useStore((s) => s.renameFile)
  const deleteFile = useStore((s) => s.deleteFile)

  return (
    <>
      <button
        ref={ref}
        data-testid="file-menu"
        onClick={() => setOpen((v) => !v)}
        style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 13.5, fontWeight: 600, color: color.ink, background: 'transparent', border: 'none', cursor: 'pointer', padding: '2px 4px', borderRadius: 6 }}
      >
        {doc.name ?? 'untitled'} <Icon name="chevronDown" size={12} style={{ color: color.text3 }} />
      </button>
      <Popover anchorRef={ref} open={open} onClose={() => setOpen(false)} width={240} align="left">
        <div style={{ padding: '6px 8px' }}>
          <input
            value={doc.name ?? ''}
            onChange={(e) => renameFile(e.target.value)}
            placeholder="untitled"
            style={{ width: '100%', fontSize: 12.5, fontWeight: 600, border: `1px solid ${color.border}`, borderRadius: 6, padding: '5px 8px', outline: 'none' }}
          />
        </div>
        <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', color: color.text3, padding: '4px 10px' }}>Files</div>
        <div style={{ maxHeight: 220, overflowY: 'auto' }}>
          {files.map((f) => (
            <button
              key={f.id}
              onClick={() => { openFile(f.id); setOpen(false) }}
              style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left', padding: '7px 10px', border: 'none', background: f.id === doc.id ? '#eef0f3' : 'transparent', borderRadius: 7, fontSize: 12.5, color: color.ink }}
              onMouseEnter={(e) => { if (f.id !== doc.id) e.currentTarget.style.background = '#f2f3f5' }}
              onMouseLeave={(e) => { if (f.id !== doc.id) e.currentTarget.style.background = 'transparent' }}
            >
              <Icon name="grid" size={12} style={{ color: color.text3 }} />
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.name || 'untitled'}</span>
            </button>
          ))}
          {files.length === 0 && <div style={{ padding: 10, fontSize: 11.5, color: color.text3 }}>No files yet.</div>}
        </div>
        <div style={{ height: 1, background: color.hairline, margin: '4px 0' }} />
        <MenuItem icon="settings" label="Canvas settings…" onClick={() => { onCanvasSettings(); setOpen(false) }} />
        <MenuItem icon="plus" label="New file" onClick={() => { newFile(); setOpen(false) }} />
        <MenuItem icon="trash" label="Delete this file" danger onClick={() => { deleteFile(doc.id); setOpen(false) }} />
      </Popover>
    </>
  )
}

// A pure identity indicator — who you are, nothing to switch. (Real users don't switch identity;
// in an auth deployment it comes from login. Logout lives on the files home.)
function IconBtn({ name, label, onClick, disabled }: { name: IconName; label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <button aria-label={label} title={label} onClick={onClick} disabled={disabled}
      style={{ width: 26, height: 26, display: 'grid', placeItems: 'center', border: 'none', borderRadius: 6, background: 'transparent', color: disabled ? '#c8ccd2' : color.text3, cursor: disabled ? 'default' : 'pointer' }}>
      <Icon name={name} size={14} />
    </button>
  )
}

function MenuItem({ icon, label, onClick, danger }: { icon: IconName; label: string; onClick: () => void; danger?: boolean }) {
  return (
    <button
      onClick={onClick}
      style={{ display: 'flex', alignItems: 'center', gap: 9, width: '100%', padding: '7px 10px', border: 'none', background: 'transparent', color: danger ? color.failed : color.text2, fontSize: 12, textAlign: 'left', borderRadius: 6 }}
      onMouseEnter={(e) => (e.currentTarget.style.background = '#f2f3f5')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
    >
      <Icon name={icon} size={13} /> {label}
    </button>
  )
}

const pill: CSSProperties = {
  display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 14px', background: '#fff',
  border: `1px solid ${color.border}`, borderRadius: 20, boxShadow: shadow.card, fontSize: 12.5, fontWeight: 600,
  color: color.text2, cursor: 'pointer',
}
