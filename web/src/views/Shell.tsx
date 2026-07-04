import { useState } from 'react'
import { useStore, type DpView } from '../store/graph'
import { api } from '../api/client'
import { color, radius, shadow } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { SettingsModal } from '../panels/SettingsModal'

// The non-canvas app shell (Figma-style): a left rail with destinations + a content area. Renders
// the Files home, the Tables catalog, or the Transforms catalog based on the store's `view`.
export function Shell() {
  const view = useStore((s) => s.view)
  const [settingsOpen, setSettingsOpen] = useState(false)
  return (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', background: color.canvas ?? '#fbfbfc' }}>
      <Rail onSettings={() => setSettingsOpen(true)} />
      <main style={{ flex: 1, minWidth: 0, overflowY: 'auto' }}>
        {view === 'files' && <FilesContent />}
        {view === 'tables' && <TablesContent />}
        {view === 'transforms' && <TransformsContent />}
      </main>
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
    </div>
  )
}

function Rail({ onSettings }: { onSettings: () => void }) {
  const view = useStore((s) => s.view)
  const setView = useStore((s) => s.setView)
  const currentUser = useStore((s) => s.currentUser)
  const users = useStore((s) => s.users)
  const switchUser = useStore((s) => s.switchUser)
  const createUser = useStore((s) => s.createUser)
  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')

  const item = (v: DpView, icon: IconName, label: string) => (
    <button onClick={() => setView(v)} data-testid={`rail-${v}`}
      style={{ display: 'flex', alignItems: 'center', gap: 10, width: '100%', textAlign: 'left', padding: '8px 10px', border: 'none', borderRadius: 8, cursor: 'pointer', fontSize: 13, fontWeight: 500,
        background: view === v ? '#e9ecf2' : 'transparent', color: view === v ? color.ink : color.text2 }}>
      <Icon name={icon} size={15} /> {label}
    </button>
  )

  return (
    <aside style={{ width: 232, flex: '0 0 232px', height: '100%', background: '#fff', borderRight: `1px solid ${color.border}`, display: 'flex', flexDirection: 'column', padding: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 8px 12px' }}>
        <span style={{ width: 22, height: 22, borderRadius: 6, background: color.ink, color: '#fff', display: 'grid', placeItems: 'center', fontSize: 13, fontWeight: 700 }}>D</span>
        <span style={{ fontSize: 13.5, fontWeight: 700, color: color.ink }}>Data Playground</span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {item('files', 'clock', 'Recents')}
        {item('tables', 'db', 'Tables')}
        {item('transforms', 'fx', 'Transforms')}
        <button onClick={onSettings} style={{ display: 'flex', alignItems: 'center', gap: 10, width: '100%', textAlign: 'left', padding: '8px 10px', border: 'none', borderRadius: 8, cursor: 'pointer', fontSize: 13, fontWeight: 500, background: 'transparent', color: color.text2 }}>
          <Icon name="settings" size={15} /> Settings
        </button>
      </div>
      <div style={{ flex: 1 }} />
      {/* user switcher */}
      <div style={{ borderTop: `1px solid ${color.hairline}`, paddingTop: 10 }}>
        {users.map((u) => (
          <button key={u.id} onClick={() => switchUser(u.id)}
            style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left', padding: '6px 8px', border: 'none', borderRadius: 7, cursor: 'pointer', fontSize: 12.5,
              background: u.id === currentUser?.id ? '#eef0f3' : 'transparent', color: color.ink }}>
            <span style={{ width: 20, height: 20, borderRadius: '50%', background: '#e7e0fb', color: '#6b4bd6', display: 'grid', placeItems: 'center', fontSize: 10, fontWeight: 700 }}>{(u.name ?? '?').slice(0, 1).toUpperCase()}</span>
            <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}>{u.name}</span>
            {u.id === currentUser?.id && <Icon name="check" size={13} style={{ color: color.latest }} />}
          </button>
        ))}
        {adding ? (
          <div style={{ display: 'flex', gap: 6, padding: '6px 4px 0' }}>
            <input autoFocus value={name} onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && name.trim()) { createUser(name.trim()); setName(''); setAdding(false) } if (e.key === 'Escape') setAdding(false) }}
              placeholder="new user…" style={{ flex: 1, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 6, padding: '5px 8px', outline: 'none' }} />
          </div>
        ) : (
          <button onClick={() => setAdding(true)} style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: '6px 8px', border: 'none', background: 'transparent', color: color.text3, fontSize: 12, cursor: 'pointer' }}>
            <Icon name="plus" size={13} /> Add user
          </button>
        )}
      </div>
    </aside>
  )
}

function ViewHeader({ title, action }: { title: string; action?: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', padding: '22px 28px 14px', gap: 12 }}>
      <h1 style={{ margin: 0, fontSize: 20, fontWeight: 700, color: color.ink }}>{title}</h1>
      <span style={{ flex: 1 }} />
      {action}
    </div>
  )
}

function FilesContent() {
  const files = useStore((s) => s.files)
  const openFile = useStore((s) => s.openFile)
  const newFile = useStore((s) => s.newFile)
  const deleteFile = useStore((s) => s.deleteFile)
  return (
    <>
      <ViewHeader title="Recents" action={
        <button onClick={() => newFile()} data-testid="new-file" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 14px', border: 'none', borderRadius: 9, background: color.ink, color: '#fff', fontSize: 12.5, fontWeight: 600, cursor: 'pointer' }}>
          <Icon name="plus" size={13} /> New file
        </button>
      } />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 16, padding: '4px 28px 28px' }}>
        {files.map((f) => (
          <div key={f.id} className="dp-file-card" onClick={() => openFile(f.id)}
            style={{ cursor: 'pointer', borderRadius: 12, border: `1px solid ${color.border}`, background: '#fff', overflow: 'hidden', boxShadow: shadow.card }}>
            <div style={{ height: 132, background: 'linear-gradient(135deg,#f3f5f8,#e9edf3)', display: 'grid', placeItems: 'center', color: color.text3 }}>
              <Icon name="grid" size={26} />
            </div>
            <div style={{ padding: '10px 12px', display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: 600, color: color.ink, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.name || 'untitled'}</span>
              <button title="Delete" onClick={(e) => { e.stopPropagation(); deleteFile(f.id) }}
                style={{ border: 'none', background: 'transparent', color: color.text3, cursor: 'pointer', padding: 2 }}><Icon name="trash" size={13} /></button>
            </div>
          </div>
        ))}
        {files.length === 0 && <div style={{ color: color.text3, fontSize: 13, padding: 20 }}>No files yet — create one with “New file”.</div>}
      </div>
    </>
  )
}

function TablesContent() {
  const catalog = useStore((s) => s.catalog)
  const refreshCatalog = useStore((s) => s.refreshCatalog)
  const addToCanvas = useStore((s) => s.addToCanvas)
  const [uri, setUri] = useState('')
  const [err, setErr] = useState('')
  const register = async () => {
    const u = uri.trim(); if (!u) return; setErr('')
    try { await api.registerFile(u); await refreshCatalog(); setUri('') } catch (e) { setErr((e as Error).message) }
  }
  return (
    <>
      <ViewHeader title="Tables" />
      <div style={{ padding: '4px 28px 28px', display: 'flex', flexDirection: 'column', gap: 8 }}>
        {/* register a dataset right here — view and manage in one place, not split into Settings */}
        <div style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
          <input value={uri} onChange={(e) => setUri(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') register() }} data-testid="register-dataset"
            placeholder="Register a dataset — path or uri to Parquet/CSV/JSON/Arrow/Lance"
            style={{ flex: 1, fontSize: 12.5, border: `1px solid ${color.border}`, borderRadius: 9, padding: '9px 12px', outline: 'none' }} />
          <button onClick={register} style={{ border: 'none', borderRadius: 9, background: color.ink, color: '#fff', fontSize: 12.5, fontWeight: 600, padding: '0 16px', cursor: 'pointer' }}>Register</button>
        </div>
        {err && <div style={{ fontSize: 11, color: color.failed }}>{err}</div>}
        {catalog.map((t) => (
          <button key={t.uri} onClick={() => addToCanvas('source', { uri: t.uri, tableId: t.id }, t.name)} title="Add as a source on the canvas"
            style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px', border: `1px solid ${color.border}`, borderRadius: 10, background: '#fff', cursor: 'pointer', textAlign: 'left' }}
            onMouseEnter={(e) => (e.currentTarget.style.background = '#f7f8fa')} onMouseLeave={(e) => (e.currentTarget.style.background = '#fff')}>
            <Icon name="db" size={16} style={{ color: color.text3 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: color.ink }}>{t.name}</div>
              <div style={{ fontSize: 11, color: color.text3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.uri}</div>
            </div>
            <span style={{ fontSize: 11.5, color: color.text2 }}>{t.columns?.length ?? 0} cols</span>
            {t.rowCount != null && <span style={{ fontSize: 11.5, color: color.text3 }}>· {t.rowCount.toLocaleString()} rows</span>}
            <span style={{ fontSize: 11, color: color.focus, fontWeight: 600, display: 'inline-flex', alignItems: 'center', gap: 4 }}><Icon name="plus" size={12} /> Use</span>
          </button>
        ))}
        {catalog.length === 0 && <div style={{ color: color.text3, fontSize: 13, padding: 20 }}>No datasets registered — add one above.</div>}
      </div>
    </>
  )
}

function TransformsContent() {
  const processors = useStore((s) => s.processors)
  const addToCanvas = useStore((s) => s.addToCanvas)
  return (
    <>
      <ViewHeader title="Transforms" />
      <div style={{ padding: '4px 28px 28px', display: 'flex', flexDirection: 'column', gap: 8 }}>
        {processors.map((p) => (
          <button key={p.id} onClick={() => addToCanvas('transform', { source: 'library', processor: p.id, version: p.version, mode: p.mode }, p.title || p.id)} title="Add to the canvas"
            style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px', border: `1px solid ${color.border}`, borderRadius: 10, background: '#fff', cursor: 'pointer', textAlign: 'left' }}
            onMouseEnter={(e) => (e.currentTarget.style.background = '#f7f8fa')} onMouseLeave={(e) => (e.currentTarget.style.background = '#fff')}>
            <Icon name="fx" size={16} style={{ color: color.text3 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: color.ink }}>{p.title || p.id}</div>
              <div style={{ fontSize: 11, color: color.text3 }}>{p.category}</div>
            </div>
            {p.mode && <span style={{ fontSize: 10.5, color: color.text3, background: '#f1f2f4', padding: '2px 7px', borderRadius: radius.chip }}>{p.mode}</span>}
            <span style={{ fontSize: 11, color: color.focus, fontWeight: 600, display: 'inline-flex', alignItems: 'center', gap: 4 }}><Icon name="plus" size={12} /> Use</span>
          </button>
        ))}
        {processors.length === 0 && <div style={{ color: color.text3, fontSize: 13, padding: 20, lineHeight: 1.6 }}>No transform processors yet. Write an ad-hoc code node on a canvas and “Promote to library” to add one here.</div>}
      </div>
    </>
  )
}
