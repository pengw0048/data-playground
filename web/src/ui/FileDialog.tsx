import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, type BrowseEntry, type DestinationPreset } from '../api/client'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
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
    <div className="dp-modal-overlay fixed inset-0 z-[2100] grid place-items-center bg-black/30" onMouseDown={onClose}>
      <div onMouseDown={(e) => e.stopPropagation()}
        className="flex h-[min(460px,88vh)] w-[min(640px,94vw)] flex-col overflow-hidden rounded-lg border border-border bg-card shadow-lg">
        <div className="flex items-center gap-2 border-b border-border px-[14px] py-[11px]">
          <span className="flex items-center text-muted-foreground"><Icon name={mode === 'save' ? 'export' : 'db'} size={14} /></span>
          <span className="text-[13.5px] font-semibold">{props.title ?? (mode === 'save' ? 'Save output' : 'Open a file')}</span>
          <span className="flex-1" />
          <button onClick={onClose} aria-label="Close" className="grid h-6 w-[26px] place-items-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"><Icon name="close" size={13} /></button>
        </div>

        <div className="flex min-h-0 flex-1">
          {/* left "places" sidebar — switch destination */}
          <div className="w-[168px] shrink-0 overflow-y-auto border-r border-border bg-muted/30 p-1.5">
            <div className="px-2 py-1 text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground">Places</div>
            {dests.map((d) => (
              <button key={d.id} onClick={() => { setDestId(d.id); setPath('') }}
                className={cn('flex w-full items-center gap-2 rounded-md px-2 py-[7px] text-left text-xs transition-colors',
                  d.id === destId ? 'bg-accent text-foreground' : 'text-muted-foreground hover:bg-accent/50')}>
                <span className="flex items-center text-muted-foreground"><Icon name={d.backend === 'local' ? 'grid' : 'link'} size={13} /></span>
                <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{d.name}</span>
              </button>
            ))}
            {dests.length === 0 && <div className="p-2 text-[11px] text-muted-foreground">No destinations.</div>}
          </div>

          {/* main: breadcrumb + entries */}
          <div className="flex min-w-0 flex-1 flex-col">
            <div className="flex items-center gap-1 overflow-x-auto whitespace-nowrap border-b border-border px-3 py-[7px] text-[11.5px] text-muted-foreground">
              <button onClick={() => setPath('')} className={crumbBtn}>{dest?.name ?? '—'}</button>
              {segs.map((s, i) => (
                <span key={i} className="inline-flex items-center gap-1">
                  <span className="flex items-center text-muted-foreground"><Icon name="chevronRight" size={10} /></span>
                  <button onClick={() => setPath(segs.slice(0, i + 1).join('/'))} className={crumbBtn}>{s}</button>
                </span>
              ))}
              <span className="flex-1" />
              {mode === 'save' && writable && <button onClick={newFolder} title="New folder" className={cn(crumbBtn, 'text-muted-foreground')}><Icon name="plus" size={11} /> Folder</button>}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
              {loading ? <div className="p-4 text-xs text-muted-foreground">loading…</div>
                : err ? <div className="p-4 text-xs leading-relaxed text-muted-foreground">{err}</div>
                : entries.length === 0 ? <div className="p-4 text-xs text-muted-foreground">Empty folder.</div>
                : entries.map((e) => (
                  <button key={e.uri} onClick={() => {
                    if (e.kind === 'dir') setPath(path ? `${path}/${e.name}` : e.name)
                    else if (mode === 'open') props.onPick({ uri: e.uri, name: e.name })
                    else setFilename(e.name)  // save: click a file to overwrite it
                  }}
                    className="flex w-full items-center gap-[9px] rounded-md px-2.5 py-2 text-left text-[12.5px] text-foreground transition-colors hover:bg-accent">
                    <span className={cn('flex items-center', e.kind === 'dir' ? 'text-primary' : 'text-muted-foreground')}><Icon name={e.kind === 'dir' ? 'grid' : 'db'} size={14} /></span>
                    <span className="flex-1 overflow-hidden text-ellipsis">{e.name}</span>
                    {e.kind === 'dir' && <span className="flex items-center text-muted-foreground"><Icon name="chevronRight" size={12} /></span>}
                  </button>
                ))}
            </div>
          </div>
        </div>

        {mode === 'save' && (
          <div className="flex items-center gap-2 border-t border-border px-[14px] py-2.5">
            {!writable
              ? <span className="flex-1 text-[11px] text-amber-600">This destination can't be written from the core — install its plugin or pick a local place.</span>
              : <>
                  <span className="text-[11.5px] text-muted-foreground">Save as</span>
                  <Input ref={fileRef} value={filename} onChange={(e) => setFilename(e.target.value)}
                    className="dp-mono min-w-0 flex-1 text-[12.5px]" />
                </>}
            <Button size="sm" disabled={!filename.trim() || !dest || !writable}
              onClick={() => dest && props.onPick({ destId, destName: dest.name, path, filename: filename.trim() })}>
              Save
            </Button>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

const crumbBtn = 'inline-flex items-center gap-[3px] rounded px-1 py-0.5 text-[11.5px] font-semibold text-primary transition-colors hover:bg-accent/60'
