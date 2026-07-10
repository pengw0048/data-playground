import { useRef, useState } from 'react'
import { useStore, type DpView } from '../store/graph'
import { api } from '../api/client'
import { examples } from '../examples'
import { color, radius, shadow } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { SettingsModal } from '../panels/SettingsModal'
import { ERDiagram } from './ERDiagram'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'

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
        {view === 'relationships' && <div style={{ height: '100%' }}><ERDiagram /></div>}
      </main>
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
    </div>
  )
}

function Rail({ onSettings }: { onSettings: () => void }) {
  const view = useStore((s) => s.view)
  const setView = useStore((s) => s.setView)
  const currentUser = useStore((s) => s.currentUser)
  const authEnabled = useStore((s) => s.authEnabled)
  const [pwOpen, setPwOpen] = useState(false)
  const logout = async () => { await api.logout().catch(() => {}); location.reload() }

  const item = (v: DpView, icon: IconName, label: string) => (
    <Button variant="ghost" onClick={() => setView(v)} data-testid={`rail-${v}`}
      className={cn('h-auto w-full justify-start gap-2.5 px-2.5 py-2 text-[13px] font-medium text-muted-foreground',
        view === v && 'bg-accent text-accent-foreground')}>
      <Icon name={icon} size={15} /> {label}
    </Button>
  )

  return (
    <aside className="flex h-full w-[232px] flex-[0_0_232px] flex-col border-r border-border bg-card p-3">
      <div className="flex items-center gap-2 px-2 pb-3 pt-1">
        <span className="grid h-[22px] w-[22px] place-items-center rounded-md bg-foreground text-[13px] font-bold text-background">D</span>
        <span className="text-[13.5px] font-bold text-foreground">Data Playground</span>
      </div>
      <div className="flex flex-col gap-0.5">
        {item('files', 'clock', 'Recents')}
        {item('tables', 'db', 'Tables')}
        {item('transforms', 'fx', 'Transforms')}
        {item('relationships', 'lineage', 'Relationships')}
        <Button variant="ghost" onClick={onSettings}
          className="h-auto w-full justify-start gap-2.5 px-2.5 py-2 text-[13px] font-medium text-muted-foreground">
          <Icon name="settings" size={15} /> Settings
        </Button>
      </div>
      <div className="flex-1" />
      {/* who you are — identity only. Switching users is gone; logout shows when a login is in force. */}
      <div className="flex items-center gap-2 border-t border-border px-2 pb-1 pt-2.5">
        <span className="grid h-6 w-6 place-items-center rounded-full bg-primary/10 text-[11px] font-bold text-primary">{(currentUser?.name ?? '?').slice(0, 1).toUpperCase()}</span>
        <div className="min-w-0 flex-1">
          <div className="overflow-hidden text-ellipsis whitespace-nowrap text-[12.5px] font-semibold text-foreground">{currentUser?.name ?? 'local'}</div>
          <div className="text-[10px] text-muted-foreground">signed in</div>
        </div>
        {authEnabled && (
          <>
            <Button variant="outline" size="sm" onClick={() => setPwOpen(true)} title="Change password" data-testid="change-password" className="px-2.5 text-[11.5px]">
              Password
            </Button>
            <Button variant="outline" size="sm" onClick={logout} title="Log out" data-testid="logout" className="px-2.5 text-[11.5px]">
              Log out
            </Button>
          </>
        )}
      </div>
      {pwOpen && <ChangePasswordModal onClose={() => setPwOpen(false)} />}
    </aside>
  )
}

function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const pushToast = useStore((s) => s.pushToast)
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async () => {
    if (newPw.length < 6) { setErr('New password must be at least 6 characters'); return }
    setBusy(true); setErr('')
    try { await api.changePassword(oldPw, newPw); pushToast('Password changed', 'success'); onClose() }
    catch (e) { setErr((e as Error).message || 'Could not change password') }
    finally { setBusy(false) }
  }
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent aria-describedby={undefined} className="dp-modal-overlay max-w-[320px]">
        <DialogHeader>
          <DialogTitle>Change password</DialogTitle>
        </DialogHeader>
        <div className="grid gap-2.5">
          <div className="grid gap-1">
            <Label htmlFor="dp-current-pw" className="text-[11px] font-normal text-muted-foreground">Current password</Label>
            <Input id="dp-current-pw" type="password" value={oldPw} autoFocus onChange={(e) => setOldPw(e.target.value)} />
          </div>
          <div className="grid gap-1">
            <Label htmlFor="dp-new-pw" className="text-[11px] font-normal text-muted-foreground">New password</Label>
            <Input id="dp-new-pw" type="password" value={newPw} onChange={(e) => setNewPw(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') submit() }} />
          </div>
          {err && <div className="text-xs text-destructive">{err}</div>}
        </div>
        <div className="flex gap-2">
          <Button onClick={submit} disabled={busy} className="flex-1">
            {busy ? 'Saving…' : 'Change password'}
          </Button>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
        </div>
      </DialogContent>
    </Dialog>
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

function relTime(iso?: string): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  const s = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  if (s < 604800) return `${Math.round(s / 86400)}d ago`
  return `${Math.round(s / 604800)}w ago`
}

// A deterministic mini "pipeline" motif per canvas, so the Recents wall isn't a grid of identical
// placeholders. Nodes are chained left→right at seeded heights — derived purely from the id hash
// (stable across renders, no per-file fetch).
function CanvasThumb({ seed }: { seed: string }) {
  let h = 2166136261
  for (let i = 0; i < seed.length; i++) { h ^= seed.charCodeAt(i); h = Math.imul(h, 16777619) }
  const rand = () => { h = Math.imul(h ^ (h >>> 15), 2246822507); h ^= h >>> 13; return ((h >>> 0) % 1000) / 1000 }
  const W = 240, H = 132, padX = 40, padY = 34
  const n = 3 + Math.floor(rand() * 3)  // 3..5 nodes
  const pts = Array.from({ length: n }, (_, i) => ({ x: padX + (i / (n - 1)) * (W - 2 * padX), y: padY + rand() * (H - 2 * padY) }))
  return (
    <svg width="100%" height="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ display: 'block', position: 'absolute', inset: 0 }} aria-hidden>
      {pts.slice(1).map((p, i) => (
        <line key={i} x1={pts[i].x} y1={pts[i].y} x2={p.x} y2={p.y} stroke="hsl(var(--foreground))" strokeOpacity={0.16} strokeWidth={1.5} />
      ))}
      {pts.map((p, i) => (
        <g key={i}>
          <rect x={p.x - 11} y={p.y - 7} width={22} height={14} rx={4} fill="hsl(var(--card))" stroke="hsl(var(--foreground))" strokeOpacity={0.26} />
          <circle cx={p.x - 5} cy={p.y} r={1.8} fill="hsl(var(--foreground))" fillOpacity={0.38} />
        </g>
      ))}
    </svg>
  )
}

function FilesContent() {
  const files = useStore((s) => s.files)
  const openFile = useStore((s) => s.openFile)
  const newFile = useStore((s) => s.newFile)
  const deleteFile = useStore((s) => s.deleteFile)
  const newFromExample = useStore((s) => s.newFromExample)
  return (
    <>
      <ViewHeader title="Recents" action={
        <button onClick={() => newFile()} data-testid="new-file" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 14px', border: 'none', borderRadius: 9, background: color.ink, color: 'hsl(var(--background))', fontSize: 12.5, fontWeight: 600, cursor: 'pointer' }}>
          <Icon name="plus" size={13} /> New file
        </button>
      } />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 16, padding: '4px 28px 28px' }}>
        {files.map((f) => {
          const meta = [relTime(f.updatedAt), f.version != null ? `v${f.version}` : ''].filter(Boolean).join(' · ')
          return (
          <div key={f.id} className="dp-file-card" onClick={() => openFile(f.id)}
            style={{ cursor: 'pointer', borderRadius: 12, border: `1px solid ${color.border}`, background: 'hsl(var(--card))', overflow: 'hidden', boxShadow: shadow.card }}>
            <div style={{ height: 132, background: 'linear-gradient(135deg, hsl(var(--muted)), hsl(var(--accent)))', position: 'relative' }}>
              <CanvasThumb seed={f.id} />
            </div>
            <div style={{ padding: '10px 12px', display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: color.ink, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.name || 'untitled'}</div>
                {meta && <div style={{ fontSize: 11, color: color.text3, marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{meta}</div>}
              </div>
              <button title="Delete" onClick={(e) => { e.stopPropagation(); deleteFile(f.id) }}
                style={{ border: 'none', background: 'transparent', color: color.text3, cursor: 'pointer', padding: 2 }}><Icon name="trash" size={13} /></button>
            </div>
          </div>
          )
        })}
        {files.length === 0 && (
          <>
            {/* a fresh install lands here with nothing — offer runnable starters, not just a dead end */}
            <div style={{ gridColumn: '1 / -1', color: color.text3, fontSize: 12.5, padding: '2px 2px 4px' }}>
              No files yet — open a runnable example, or “New file”.
            </div>
            {examples.map((ex) => (
              <div key={ex.key} className="dp-file-card" onClick={() => { void newFromExample(ex.key) }} title={ex.blurb}
                style={{ cursor: 'pointer', borderRadius: 12, border: `1px solid ${color.border}`, background: 'hsl(var(--card))', overflow: 'hidden', boxShadow: shadow.card }}>
                <div style={{ height: 132, background: 'linear-gradient(135deg, hsl(var(--muted)), hsl(var(--accent)))', position: 'relative' }}>
                  <CanvasThumb seed={ex.key} />
                </div>
                <div style={{ padding: '10px 12px' }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: color.ink }}>{ex.name}</div>
                  <div style={{ fontSize: 11, color: color.text3, marginTop: 2, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{ex.blurb}</div>
                </div>
              </div>
            ))}
          </>
        )}
      </div>
    </>
  )
}

function TablesContent() {
  const catalog = useStore((s) => s.catalog)
  const refreshCatalog = useStore((s) => s.refreshCatalog)
  const uploadDataset = useStore((s) => s.uploadDataset)
  const addToCanvas = useStore((s) => s.addToCanvas)
  const fileRef = useRef<HTMLInputElement>(null)
  const [uri, setUri] = useState('')
  const [err, setErr] = useState('')
  const [uploading, setUploading] = useState(false)
  const register = async () => {
    const u = uri.trim(); if (!u) return; setErr('')
    try { await api.registerFile(u); await refreshCatalog(); setUri('') } catch (e) { setErr((e as Error).message) }
  }
  const onUpload = async (f?: File) => {
    if (!f) return
    setUploading(true); await uploadDataset(f); setUploading(false)  // uploads + refreshes the catalog
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
          <button onClick={register} style={{ border: 'none', borderRadius: 9, background: color.ink, color: 'hsl(var(--background))', fontSize: 12.5, fontWeight: 600, padding: '0 16px', cursor: 'pointer' }}>Register</button>
          {/* or upload the bytes directly (server-side path not required) */}
          <button onClick={() => fileRef.current?.click()} disabled={uploading} title="Upload a dataset file from your machine"
            style={{ border: `1px solid ${color.border}`, borderRadius: 9, background: 'hsl(var(--card))', color: color.ink, fontSize: 12.5, fontWeight: 600, padding: '0 16px', cursor: uploading ? 'default' : 'pointer', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <Icon name="export" size={13} /> {uploading ? 'Uploading…' : 'Upload'}
          </button>
          <input ref={fileRef} type="file" accept=".parquet,.pq,.csv,.tsv,.json,.ndjson,.arrow,.feather,.ipc" style={{ display: 'none' }}
            onChange={(e) => { void onUpload(e.target.files?.[0]); e.target.value = '' }} />
        </div>
        {err && <div style={{ fontSize: 11, color: color.failed }}>{err}</div>}
        {catalog.map((t) => (
          <button key={t.uri} onClick={() => addToCanvas('source', { uri: t.uri, tableId: t.id }, t.name)} title="Add as a source on the canvas"
            style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px', border: `1px solid ${color.border}`, borderRadius: 10, background: 'hsl(var(--card))', cursor: 'pointer', textAlign: 'left' }}
            onMouseEnter={(e) => (e.currentTarget.style.background = 'hsl(var(--accent))')} onMouseLeave={(e) => (e.currentTarget.style.background = 'hsl(var(--card))')}>
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
            style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px', border: `1px solid ${color.border}`, borderRadius: 10, background: 'hsl(var(--card))', cursor: 'pointer', textAlign: 'left' }}
            onMouseEnter={(e) => (e.currentTarget.style.background = 'hsl(var(--accent))')} onMouseLeave={(e) => (e.currentTarget.style.background = 'hsl(var(--card))')}>
            <Icon name="fx" size={16} style={{ color: color.text3 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: color.ink }}>{p.title || p.id}</div>
              <div style={{ fontSize: 11, color: color.text3 }}>{p.category}</div>
            </div>
            {p.mode && <span style={{ fontSize: 10.5, color: color.text3, background: 'hsl(var(--muted))', padding: '2px 7px', borderRadius: radius.chip }}>{p.mode}</span>}
            <span style={{ fontSize: 11, color: color.focus, fontWeight: 600, display: 'inline-flex', alignItems: 'center', gap: 4 }}><Icon name="plus" size={12} /> Use</span>
          </button>
        ))}
        {processors.length === 0 && <div style={{ color: color.text3, fontSize: 13, padding: 20, lineHeight: 1.6 }}>No transform processors yet. Write an ad-hoc code node on a canvas and “Promote to library” to add one here.</div>}
      </div>
    </>
  )
}
