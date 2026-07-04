import { useRef, useState, type CSSProperties } from 'react'
import { useStore } from '../store/graph'
import { color, shadow } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { Popover } from '../ui/Popover'
import { SettingsModal } from '../panels/SettingsModal'
import { RunHistoryModal } from '../panels/RunHistoryModal'
import { ShareModal } from '../panels/ShareModal'

export function TopBar() {
  const doc = useStore((s) => s.doc)
  const kernelUp = useStore((s) => s.kernelUp)
  const kernelInfo = useStore((s) => s.kernelInfo)
  const saved = useStore((s) => s.saved)
  const rerunAll = useStore((s) => s.rerunAll)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [runsOpen, setRunsOpen] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)

  return (
    <>
      <div style={{ position: 'absolute', top: 16, left: 20, zIndex: 15, display: 'flex', alignItems: 'center', gap: 8 }}>
        <AppMenu onSettings={() => setSettingsOpen(true)} onRunHistory={() => setRunsOpen(true)} />
        <span style={{ fontSize: 13.5, color: color.text3 }}>/</span>
        <FileMenu />
        <span data-testid="autosave" style={{ fontSize: 11, color: color.text3, marginLeft: 2 }}>· {saved ? 'saved' : 'saving…'}</span>
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
        <button aria-label="Settings" title="Settings" onClick={() => setSettingsOpen(true)}
          style={{ width: 34, height: 34, display: 'grid', placeItems: 'center', background: '#fff', border: `1px solid ${color.border}`, borderRadius: 20, boxShadow: shadow.card, color: color.text2, cursor: 'pointer' }}>
          <Icon name="settings" size={15} />
        </button>
        <UserMenu />
      </div>
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
      {runsOpen && <RunHistoryModal onClose={() => setRunsOpen(false)} />}
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

function AppMenu({ onSettings, onRunHistory }: { onSettings: () => void; onRunHistory: () => void }) {
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
        <div style={{ height: 1, background: color.hairline, margin: '4px 0' }} />
        <MenuItem icon="settings" label="Settings" onClick={() => { onSettings(); setOpen(false) }} />
      </Popover>
    </>
  )
}

function FileMenu() {
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
        <MenuItem icon="plus" label="New file" onClick={() => { newFile(); setOpen(false) }} />
        <MenuItem icon="trash" label="Delete this file" danger onClick={() => { deleteFile(doc.id); setOpen(false) }} />
      </Popover>
    </>
  )
}

function UserMenu() {
  const ref = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const currentUser = useStore((s) => s.currentUser)
  const users = useStore((s) => s.users)
  const switchUser = useStore((s) => s.switchUser)
  const createUser = useStore((s) => s.createUser)

  const add = () => { const n = name.trim(); if (n) { createUser(n); setName(''); setOpen(false) } }

  return (
    <>
      <button ref={ref} onClick={() => setOpen((v) => !v)} title="Switch user" style={{ ...pill }}>
        <span style={{ width: 18, height: 18, borderRadius: '50%', background: '#e7e0fb', color: '#6b4bd6', display: 'grid', placeItems: 'center', fontSize: 10, fontWeight: 700 }}>
          {(currentUser?.name ?? '?').slice(0, 1).toUpperCase()}
        </span>
        {currentUser?.name ?? 'local'} <Icon name="chevronDown" size={12} style={{ color: color.text3 }} />
      </button>
      <Popover anchorRef={ref} open={open} onClose={() => setOpen(false)} width={220} align="right">
        <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', color: color.text3, padding: '5px 10px' }}>Users</div>
        {users.map((u) => (
          <button
            key={u.id}
            onClick={() => { switchUser(u.id); setOpen(false) }}
            style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left', padding: '7px 10px', border: 'none', background: u.id === currentUser?.id ? '#eef0f3' : 'transparent', borderRadius: 7, fontSize: 12.5, color: color.ink }}
            onMouseEnter={(e) => { if (u.id !== currentUser?.id) e.currentTarget.style.background = '#f2f3f5' }}
            onMouseLeave={(e) => { if (u.id !== currentUser?.id) e.currentTarget.style.background = 'transparent' }}
          >
            <span style={{ flex: 1 }}>{u.name}</span>
            {u.id === currentUser?.id && <Icon name="check" size={13} style={{ color: color.latest }} />}
          </button>
        ))}
        <div style={{ height: 1, background: color.hairline, margin: '4px 0' }} />
        <div style={{ display: 'flex', gap: 6, padding: '4px 8px 6px' }}>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') add() }}
            placeholder="new user…"
            style={{ flex: 1, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 6, padding: '5px 8px', outline: 'none' }}
          />
          <button onClick={add} style={{ border: 'none', borderRadius: 6, background: color.ink, color: '#fff', fontSize: 12, fontWeight: 600, padding: '0 10px' }}>Add</button>
        </div>
      </Popover>
    </>
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
