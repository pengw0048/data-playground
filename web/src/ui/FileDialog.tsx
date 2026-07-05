import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, type BrowseEntry, type DestinationPreset } from '../api/client'
import { color, radius, shadow } from '../theme/tokens'
import { Icon } from './Icon'

// An open/save dialog over the configured destinations (local dirs + object-store prefixes), like a
// system file picker. `open` → pick a file to read; `save` → choose a destination + folder + name.
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
  const [filename, setFilename] = useState(mode === 'save' ? (props.defaultName ?? 'output') : '')

  useEffect(() => {
    api.destinations().then((d) => {
      setDests(d.destinations)
      setDestId((cur) => cur || d.destinations[0]?.id || '')
    }).catch((e) => setErr((e as Error).message))
  }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  useEffect(() => {
    if (!destId) return
    setLoading(true)
    api.browseDestination(destId, path)
      .then((r) => { setEntries(r.entries); setErr(r.error ?? null) })
      .catch((e) => { setEntries([]); setErr((e as Error).message) })
      .finally(() => setLoading(false))
  }, [destId, path])

  const dest = dests.find((d) => d.id === destId)
  const segs = path ? path.split('/').filter(Boolean) : []
  const up = () => setPath(segs.slice(0, -1).join('/'))

  return createPortal(
    <div className="dp-modal-overlay" onMouseDown={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(20,22,28,.35)', zIndex: 2100, display: 'grid', placeItems: 'center' }}>
      <div onMouseDown={(e) => e.stopPropagation()}
        style={{ width: 560, maxWidth: '94vw', height: 480, maxHeight: '88vh', background: '#fff', border: `1px solid ${color.border}`, borderRadius: radius.panel, boxShadow: shadow.panel, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '11px 14px', borderBottom: `1px solid ${color.hairline}` }}>
          <Icon name={mode === 'save' ? 'export' : 'db'} size={14} style={{ color: color.text2 }} />
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>{props.title ?? (mode === 'save' ? 'Save output to…' : 'Open a file')}</span>
          <span style={{ flex: 1 }} />
          <select value={destId} onChange={(e) => { setDestId(e.target.value); setPath('') }}
            style={{ fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 7, padding: '5px 8px', background: '#fff', maxWidth: 220 }}>
            {dests.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
          <button onClick={onClose} aria-label="Close" style={{ width: 26, height: 24, border: 'none', background: 'transparent', color: color.text3, display: 'grid', placeItems: 'center' }}><Icon name="close" size={13} /></button>
        </div>

        {/* breadcrumb */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '7px 14px', borderBottom: `1px solid ${color.hairline}`, fontSize: 11.5, color: color.text2, overflowX: 'auto', whiteSpace: 'nowrap' }}>
          <button onClick={() => setPath('')} style={crumbBtn}>{dest?.name ?? '—'}</button>
          {segs.map((s, i) => (
            <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <Icon name="chevronRight" size={10} style={{ color: color.text3 }} />
              <button onClick={() => setPath(segs.slice(0, i + 1).join('/'))} style={crumbBtn}>{s}</button>
            </span>
          ))}
          {segs.length > 0 && <button onClick={up} title="Up" style={{ ...crumbBtn, marginLeft: 6, color: color.text3 }}>↑</button>}
        </div>

        {/* entries */}
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: 6 }}>
          {loading ? <div style={{ padding: 16, fontSize: 12, color: color.text3 }}>loading…</div>
            : err ? <div style={{ padding: 16, fontSize: 12, color: color.text2, lineHeight: 1.5 }}>{err}</div>
            : entries.length === 0 ? <div style={{ padding: 16, fontSize: 12, color: color.text3 }}>Empty.</div>
            : entries.map((e) => (
              <button key={e.uri} onClick={() => {
                if (e.kind === 'dir') setPath(path ? `${path}/${e.name}` : e.name)
                else if (mode === 'open') props.onPick({ uri: e.uri, name: e.name })
                else setFilename(e.name)  // save: click a file to overwrite its name
              }}
                style={{ display: 'flex', alignItems: 'center', gap: 9, width: '100%', textAlign: 'left', padding: '8px 10px', border: 'none', background: 'transparent', borderRadius: 7, fontSize: 12.5, color: color.ink, cursor: 'pointer' }}
                onMouseEnter={(ev) => (ev.currentTarget.style.background = '#f2f3f5')} onMouseLeave={(ev) => (ev.currentTarget.style.background = 'transparent')}>
                <Icon name={e.kind === 'dir' ? 'grid' : 'db'} size={14} style={{ color: color.text3 }} />
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}>{e.name}</span>
                {e.kind === 'dir' && <Icon name="chevronRight" size={12} style={{ color: color.text3 }} />}
              </button>
            ))}
        </div>

        {/* save footer */}
        {mode === 'save' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px', borderTop: `1px solid ${color.hairline}` }}>
            <span style={{ fontSize: 11.5, color: color.text3 }}>file name</span>
            <input value={filename} onChange={(e) => setFilename(e.target.value)}
              style={{ flex: 1, fontSize: 12.5, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', outline: 'none' }} />
            <button disabled={!filename.trim() || !dest} onClick={() => dest && props.onPick({ destId, destName: dest.name, path, filename: filename.trim() })}
              style={{ padding: '8px 16px', border: 'none', borderRadius: 8, background: color.ink, color: '#fff', fontSize: 12.5, fontWeight: 600, opacity: filename.trim() && dest ? 1 : 0.5, cursor: filename.trim() ? 'pointer' : 'not-allowed' }}>
              Save here
            </button>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

const crumbBtn = { border: 'none', background: 'transparent', color: color.focus, fontSize: 11.5, fontWeight: 600, cursor: 'pointer', padding: '2px 4px', borderRadius: 5 } as const
