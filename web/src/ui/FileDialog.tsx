import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, type BrowseEntry, type DestinationPreset } from '../api/client'
import { color, radius, shadow } from '../theme/tokens'
import { Icon } from './Icon'

// An open/save dialog over the configured destinations (local dirs + object-store prefixes), styled
// like a system file picker: a left "places" sidebar to switch destination, a breadcrumb + folder
// list to navigate, New Folder, and (save) a filename field whose base name is pre-selected.
export interface OpenResult { uri: string; name: string }
export interface SaveResult { destId: string; destName: string; path: string; filename: string }

export function FileDialog(props:
  | { mode: 'open'; onPick: (r: OpenResult) => void; onClose: () => void; title?: string }
  | { mode: 'save'; defaultName?: string; onPick: (r: SaveResult) => void; onClose: () => void; title?: string },
) {
  const { mode, onClose } = props
  const [dests, setDests] = useState<DestinationPreset[]>([])
  const [destId, setDestId] = useState('')
  const [path, setPath] = useState('')
  const [entries, setEntries] = useState<BrowseEntry[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [writable, setWritable] = useState(true)
  const [filename, setFilename] = useState(mode === 'save' ? (props.defaultName ?? 'output.parquet') : '')
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    api.destinations().then((d) => {
      setDests(d.destinations)
      setDestId((cur) => cur || d.destinations[0]?.id || '')
      if (d.destinations.length === 0) setLoading(false)
    }).catch((e) => { setErr((e as Error).message); setLoading(false) })
  }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  const refresh = () => {
    if (!destId) { setLoading(false); return }
    setLoading(true)
    api.browseDestination(destId, path)
      .then((r) => { setEntries(r.entries); setErr(r.error ?? null); setWritable(r.writable !== false) })
      .catch((e) => { setEntries([]); setErr((e as Error).message) })
      .finally(() => setLoading(false))
  }
  useEffect(refresh, [destId, path])
  // pre-select the base name (before the extension), macOS-style, once when a save dialog opens
  useEffect(() => {
    if (mode !== 'save') return
    const el = fileRef.current
    if (el) { el.focus(); const dot = filename.lastIndexOf('.'); el.setSelectionRange(0, dot > 0 ? dot : filename.length) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const dest = dests.find((d) => d.id === destId)
  const segs = path ? path.split('/').filter(Boolean) : []
  const newFolder = async () => {
    const name = window.prompt('New folder name')?.trim()
    if (!name) return
    const r = await api.mkdirDestination(destId, path, name).catch((e) => ({ error: (e as Error).message }))
    // surface the failure without masking the folder list (which the shared `err` state would do)
    if (r.error) { window.alert(`Couldn't create folder: ${r.error}`); return }
    setPath(path ? `${path}/${name}` : name)
  }

  return createPortal(
    <div className="dp-modal-overlay" onMouseDown={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(20,22,28,.35)', zIndex: 2100, display: 'grid', placeItems: 'center' }}>
      <div onMouseDown={(e) => e.stopPropagation()}
        style={{ width: 'min(640px, 94vw)', height: 'min(460px, 88vh)', background: '#fff', border: `1px solid ${color.border}`, borderRadius: radius.panel, boxShadow: shadow.panel, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '11px 14px', borderBottom: `1px solid ${color.hairline}` }}>
          <Icon name={mode === 'save' ? 'export' : 'db'} size={14} style={{ color: color.text2 }} />
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>{props.title ?? (mode === 'save' ? 'Save output' : 'Open a file')}</span>
          <span style={{ flex: 1 }} />
          <button onClick={onClose} aria-label="Close" style={{ width: 26, height: 24, border: 'none', background: 'transparent', color: color.text3, display: 'grid', placeItems: 'center' }}><Icon name="close" size={13} /></button>
        </div>

        <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
          {/* left "places" sidebar — switch destination */}
          <div style={{ width: 168, flex: '0 0 168px', borderRight: `1px solid ${color.hairline}`, overflowY: 'auto', padding: 6, background: '#fbfbfc' }}>
            <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', color: color.text3, padding: '4px 8px' }}>Places</div>
            {dests.map((d) => (
              <button key={d.id} onClick={() => { setDestId(d.id); setPath('') }}
                style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left', padding: '7px 8px', border: 'none', borderRadius: 7, cursor: 'pointer', fontSize: 12,
                  background: d.id === destId ? '#e9ecf2' : 'transparent', color: d.id === destId ? color.ink : color.text2 }}>
                <Icon name={d.backend === 'local' ? 'grid' : 'link'} size={13} style={{ color: color.text3 }} />
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.name}</span>
              </button>
            ))}
            {dests.length === 0 && <div style={{ fontSize: 11, color: color.text3, padding: 8 }}>No destinations.</div>}
          </div>

          {/* main: breadcrumb + entries */}
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '7px 12px', borderBottom: `1px solid ${color.hairline}`, fontSize: 11.5, color: color.text2, overflowX: 'auto', whiteSpace: 'nowrap' }}>
              <button onClick={() => setPath('')} style={crumbBtn}>{dest?.name ?? '—'}</button>
              {segs.map((s, i) => (
                <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                  <Icon name="chevronRight" size={10} style={{ color: color.text3 }} />
                  <button onClick={() => setPath(segs.slice(0, i + 1).join('/'))} style={crumbBtn}>{s}</button>
                </span>
              ))}
              <span style={{ flex: 1 }} />
              {mode === 'save' && writable && <button onClick={newFolder} title="New folder" style={{ ...crumbBtn, color: color.text3 }}><Icon name="plus" size={11} /> Folder</button>}
            </div>
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: 6 }}>
              {loading ? <div style={{ padding: 16, fontSize: 12, color: color.text3 }}>loading…</div>
                : err ? <div style={{ padding: 16, fontSize: 12, color: color.text2, lineHeight: 1.5 }}>{err}</div>
                : entries.length === 0 ? <div style={{ padding: 16, fontSize: 12, color: color.text3 }}>Empty folder.</div>
                : entries.map((e) => (
                  <button key={e.uri} onClick={() => {
                    if (e.kind === 'dir') setPath(path ? `${path}/${e.name}` : e.name)
                    else if (mode === 'open') props.onPick({ uri: e.uri, name: e.name })
                    else setFilename(e.name)  // save: click a file to overwrite it
                  }}
                    style={{ display: 'flex', alignItems: 'center', gap: 9, width: '100%', textAlign: 'left', padding: '8px 10px', border: 'none', background: 'transparent', borderRadius: 7, fontSize: 12.5, color: color.ink, cursor: 'pointer' }}
                    onMouseEnter={(ev) => (ev.currentTarget.style.background = '#f2f3f5')} onMouseLeave={(ev) => (ev.currentTarget.style.background = 'transparent')}>
                    <Icon name={e.kind === 'dir' ? 'grid' : 'db'} size={14} style={{ color: e.kind === 'dir' ? color.focus : color.text3 }} />
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}>{e.name}</span>
                    {e.kind === 'dir' && <Icon name="chevronRight" size={12} style={{ color: color.text3 }} />}
                  </button>
                ))}
            </div>
          </div>
        </div>

        {mode === 'save' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px', borderTop: `1px solid ${color.hairline}` }}>
            {!writable
              ? <span style={{ flex: 1, fontSize: 11, color: '#a2731a' }}>This destination can't be written from the core — install its plugin or pick a local place.</span>
              : <>
                  <span style={{ fontSize: 11.5, color: color.text3 }}>Save as</span>
                  <input ref={fileRef} value={filename} onChange={(e) => setFilename(e.target.value)}
                    className="dp-mono" style={{ flex: 1, fontSize: 12.5, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', outline: 'none' }} />
                </>}
            <button disabled={!filename.trim() || !dest || !writable} onClick={() => dest && props.onPick({ destId, destName: dest.name, path, filename: filename.trim() })}
              style={{ padding: '8px 16px', border: 'none', borderRadius: 8, background: color.ink, color: '#fff', fontSize: 12.5, fontWeight: 600, opacity: filename.trim() && dest && writable ? 1 : 0.5, cursor: filename.trim() && writable ? 'pointer' : 'not-allowed' }}>
              Save
            </button>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

const crumbBtn = { border: 'none', background: 'transparent', color: color.focus, fontSize: 11.5, fontWeight: 600, cursor: 'pointer', padding: '2px 4px', borderRadius: 5, display: 'inline-flex', alignItems: 'center', gap: 3 } as const
