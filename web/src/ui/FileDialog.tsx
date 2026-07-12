import { useCallback, useEffect, useRef, useState } from 'react'
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
  | { mode: 'open'; onPick: (r: OpenResult) => void | Promise<void>; onClose: () => void; title?: string }
  | { mode: 'save'; defaultName?: string; onPick: (r: SaveResult) => void; onClose: () => void; title?: string },
) {
  const { mode, onClose } = props
  const [dests, setDests] = useState<DestinationPreset[]>([])
  const [destId, setDestId] = useState('')
  const [path, setPath] = useState('')
  const [entries, setEntries] = useState<BrowseEntry[]>([])
  const [destError, setDestError] = useState<string | null>(null)
  const [browseError, setBrowseError] = useState<string | null>(null)
  const [loadingDests, setLoadingDests] = useState(true)
  const [loading, setLoading] = useState(false)
  const [writable, setWritable] = useState(true)
  const [filename, setFilename] = useState(mode === 'save' ? (props.defaultName ?? 'output.parquet') : '')
  const [pickError, setPickError] = useState<string | null>(null)
  const [pickingUri, setPickingUri] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const destinationRequest = useRef(0)
  const browseRequest = useRef(0)

  const loadDestinations = useCallback(async () => {
    const s = ++destinationRequest.current
    setLoadingDests(true); setDestError(null)
    try {
      const d = await api.destinations()
      if (s !== destinationRequest.current) return
      setDests(d.destinations)
      setDestId((cur) => d.destinations.some((x) => x.id === cur) ? cur : (d.destinations[0]?.id ?? ''))
    } catch (e) {
      if (s === destinationRequest.current) setDestError(errorMessage(e))
    } finally {
      if (s === destinationRequest.current) setLoadingDests(false)
    }
  }, [])
  useEffect(() => {
    void loadDestinations()
    return () => { destinationRequest.current += 1 }
  }, [loadDestinations])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !pickingUri) onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, pickingUri])
  const refresh = useCallback(async () => {
    const s = ++browseRequest.current
    setPickError(null)
    if (!destId) {
      setEntries([]); setBrowseError(null); setLoading(false)
      return
    }
    setLoading(true); setBrowseError(null); setEntries([])
    try {
      const r = await api.browseDestination(destId, path)
      if (s !== browseRequest.current) return
      setEntries(r.entries)
      setBrowseError(r.error ?? null)
      setWritable(r.writable !== false)
    } catch (e) {
      if (s === browseRequest.current) setBrowseError(errorMessage(e))
    } finally {
      if (s === browseRequest.current) setLoading(false)
    }
  }, [destId, path])
  useEffect(() => {
    void refresh()
    return () => { browseRequest.current += 1 }
  }, [refresh])
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
  const pickOpenFile = async (entry: BrowseEntry) => {
    if (mode !== 'open' || pickingUri) return
    setPickingUri(entry.uri); setPickError(null)
    try {
      await props.onPick({ uri: entry.uri, name: entry.name })
    } catch (e) {
      setPickError(errorMessage(e))
    } finally {
      setPickingUri(null)
    }
  }

  return createPortal(
    <div className="dp-modal-overlay fixed inset-0 z-[2100] grid place-items-center bg-black/30" onMouseDown={() => { if (!pickingUri) onClose() }}>
      <div onMouseDown={(e) => e.stopPropagation()}
        className="flex h-[min(460px,88vh)] w-[min(640px,94vw)] flex-col overflow-hidden rounded-lg border border-border bg-card shadow-lg">
        <div className="flex items-center gap-2 border-b border-border px-[14px] py-[11px]">
          <span className="flex items-center text-muted-foreground"><Icon name={mode === 'save' ? 'export' : 'db'} size={14} /></span>
          <span className="text-[13.5px] font-semibold">{props.title ?? (mode === 'save' ? 'Save output' : 'Open a file')}</span>
          <span className="flex-1" />
          <button onClick={onClose} disabled={pickingUri !== null} aria-label="Close" className="grid h-6 w-[26px] place-items-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"><Icon name="close" size={13} /></button>
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
            {loadingDests && dests.length === 0 && <div className="p-2 text-[11px] text-muted-foreground">Loading places…</div>}
            {destError && (
              <div role="alert" className="m-1 flex flex-col gap-1 rounded border border-destructive/30 p-2 text-[10.5px] text-destructive">
                <span>Couldn't load places: {destError}</span>
                <button onClick={() => void loadDestinations()} data-testid="file-dialog-destinations-retry" className="self-start font-semibold underline">Retry</button>
              </div>
            )}
            {!loadingDests && !destError && dests.length === 0 && <div className="p-2 text-[11px] text-muted-foreground">No destinations.</div>}
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
              {pickError && (
                <div role="alert" className="m-1 rounded-md border border-destructive/30 px-3 py-2 text-xs text-destructive">
                  Couldn't open file: {pickError}. Your selection has not been changed; choose the file to retry.
                </div>
              )}
              {!destId ? <div className="p-4 text-xs text-muted-foreground">
                  {loadingDests ? 'Loading places…' : destError ? 'Places are unavailable. Retry from the sidebar.' : 'No destinations configured.'}
                </div>
                : loading ? <div className="p-4 text-xs text-muted-foreground">Loading…</div>
                : browseError ? <div role="alert" className="m-1 flex items-center justify-between gap-2 rounded-md border border-destructive/30 px-3 py-2 text-xs text-destructive">
                    <span>Couldn't load this folder: {browseError}</span>
                    <button onClick={() => void refresh()} data-testid="file-dialog-browse-retry" className="shrink-0 font-semibold underline">Retry</button>
                  </div>
                : entries.length === 0 ? <div className="p-4 text-xs text-muted-foreground">Empty folder.</div>
                : entries.map((e) => (
                  <button key={e.uri} disabled={pickingUri !== null} onClick={() => {
                    if (e.kind === 'dir') setPath(path ? `${path}/${e.name}` : e.name)
                    else if (mode === 'open') void pickOpenFile(e)
                    else setFilename(e.name)  // save: click a file to overwrite it
                  }}
                    className="flex w-full items-center gap-[9px] rounded-md px-2.5 py-2 text-left text-[12.5px] text-foreground transition-colors hover:bg-accent disabled:opacity-60">
                    <span className={cn('flex items-center', e.kind === 'dir' ? 'text-primary' : 'text-muted-foreground')}><Icon name={e.kind === 'dir' ? 'grid' : 'db'} size={14} /></span>
                    <span className="flex-1 overflow-hidden text-ellipsis">{e.name}</span>
                    {pickingUri === e.uri && <span className="text-[10.5px] text-muted-foreground">Opening…</span>}
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
const errorMessage = (e: unknown) => e instanceof Error ? e.message : String(e)
