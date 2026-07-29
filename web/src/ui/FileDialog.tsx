import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, type BrowseEntry, type DestinationPreset } from '../api/client'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Icon } from './Icon'

// A browser over configured kernel-accessible destinations (local dirs + object-store prefixes).
// Open mode selects a file; save mode chooses a destination and logical output name. Only a fresh
// write admission can say whether that selection publishes a managed revision.
export interface OpenResult { uri: string; name: string }
export interface SaveResult { destId: string; destName: string; path: string; filename: string }

export function FileDialog(props:
  | { mode: 'open'; onPick: (r: OpenResult) => void | Promise<void>; onClose: () => void; title?: string }
  | {
      mode: 'save'
      defaultName?: string
      onPick: (r: SaveResult) => void
      onClose: () => void
      onManageDestinations?: () => void
      title?: string
    },
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
  const [filename, setFilename] = useState(mode === 'save' ? (props.defaultName ?? 'output') : '')
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
    if (mode === 'save') {
      setEntries([]); setBrowseError(null); setLoading(false); setWritable(true)
      return
    }
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
  }, [destId, mode, path])
  useEffect(() => {
    void refresh()
    return () => { browseRequest.current += 1 }
  }, [refresh])
  // Select the proposed logical name once when an output-destination dialog opens.
  useEffect(() => {
    if (mode !== 'save') return
    const el = fileRef.current
    if (el) { el.focus(); const dot = filename.lastIndexOf('.'); el.setSelectionRange(0, dot > 0 ? dot : filename.length) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const dest = dests.find((d) => d.id === destId)
  const segs = path ? path.split('/').filter(Boolean) : []
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
          <span className="text-[13.5px] font-semibold">
            {props.title ?? (mode === 'save' ? 'Choose output destination' : 'Open a file')}
          </span>
          <span className="flex-1" />
          <button onClick={onClose} disabled={pickingUri !== null} aria-label="Close" className="grid h-6 w-[26px] place-items-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"><Icon name="close" size={13} /></button>
        </div>

        <div className="flex min-h-0 flex-1">
          {/* left sidebar — switch configured destination */}
          <div className="w-[168px] shrink-0 overflow-y-auto border-r border-border bg-muted/30 p-1.5">
            <div className="px-2 py-1 text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground">
              {mode === 'save' ? 'Destinations' : 'Places'}
            </div>
            {dests.map((d) => (
              <button key={d.id} onClick={() => { setDestId(d.id); setPath('') }}
                className={cn('flex w-full items-center gap-2 rounded-md px-2 py-[7px] text-left text-xs transition-colors',
                  d.id === destId ? 'bg-accent text-foreground' : 'text-muted-foreground hover:bg-accent/50')}>
                <span className="flex items-center text-muted-foreground"><Icon name={d.backend === 'local' ? 'grid' : 'link'} size={13} /></span>
                <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{d.name}</span>
              </button>
            ))}
            {loadingDests && dests.length === 0 && <div className="p-2 text-[11px] text-muted-foreground">
              {mode === 'save' ? 'Loading destinations…' : 'Loading places…'}
            </div>}
            {destError && (
              <div role="alert" className="m-1 flex flex-col gap-1 rounded border border-destructive/30 p-2 text-[10.5px] text-destructive">
                <span>{mode === 'save' ? "Couldn't load destinations" : "Couldn't load places"}: {destError}</span>
                <button onClick={() => void loadDestinations()} data-testid="file-dialog-destinations-retry" className="self-start font-semibold underline">Retry</button>
              </div>
            )}
            {!loadingDests && !destError && dests.length === 0 && <div className="p-2 text-[11px] text-muted-foreground">
              No destinations.
            </div>}
          </div>

          {mode === 'save' ? (
            <div className="flex min-w-0 flex-1 items-center justify-center p-6">
              {dest ? (
                <div aria-label="Selected destination"
                  className="w-full max-w-sm rounded-md border border-border bg-muted/30 p-4">
                  <div className="text-[10px] font-bold uppercase tracking-[0.5px] text-muted-foreground">
                    Selected destination
                  </div>
                  <div className="mt-1 flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-sm font-semibold text-foreground">{dest.name}</span>
                    <span className="rounded border border-border bg-background px-1.5 py-0.5 text-[9.5px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {destinationBackendLabel(dest.backend)}
                    </span>
                  </div>
                  <div className="mt-3 text-[10px] font-medium text-muted-foreground">Root or prefix</div>
                  <div className="dp-mono mt-1 break-all rounded border border-border bg-background px-2 py-1.5 text-[11px] text-foreground">
                    {dest.root}
                  </div>
                  <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
                    The next run will request the output name below inside this configured location.
                    The server confirms access before writing.
                  </p>
                  <div className="mt-3 border-t border-border pt-3 text-[11px] leading-relaxed text-muted-foreground">
                    {props.onManageDestinations
                      ? 'Add local, S3, or GCS locations as named destinations in Settings, then select one here.'
                      : 'Destinations are configured by an administrator. Ask an administrator to add or change a local, S3, or GCS location.'}
                  </div>
                  {props.onManageDestinations && (
                    <Button variant="outline" size="sm" className="mt-2" onClick={props.onManageDestinations}>
                      <Icon name="settings" size={12} /> Manage destinations
                    </Button>
                  )}
                </div>
              ) : (
                <div className="text-xs text-muted-foreground">
                  {loadingDests
                    ? 'Loading destinations…'
                    : destError
                      ? 'Destinations are unavailable. Retry from the sidebar.'
                      : 'No destinations configured.'}
                </div>
              )}
            </div>
          ) : (
            /* Open mode keeps the kernel-accessible path browser. */
            <div className="flex min-w-0 flex-1 flex-col">
              <div className="flex items-center gap-1 overflow-x-auto whitespace-nowrap border-b border-border px-3 py-[7px] text-[11.5px] text-muted-foreground">
                <button onClick={() => setPath('')} className={crumbBtn}>{dest?.name ?? '—'}</button>
                {segs.map((s, i) => (
                  <span key={i} className="inline-flex items-center gap-1">
                    <span className="flex items-center text-muted-foreground"><Icon name="chevronRight" size={10} /></span>
                    <button onClick={() => setPath(segs.slice(0, i + 1).join('/'))} className={crumbBtn}>{s}</button>
                  </span>
                ))}
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
                      else void pickOpenFile(e)
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
          )}
        </div>

        {mode === 'save' && (
          <div className="flex items-center gap-2 border-t border-border px-[14px] py-2.5">
            {!writable
              ? <span className="flex-1 text-[11px] text-amber-600">This destination can't accept this output — install its plugin or choose another destination.</span>
              : <>
                  <span className="text-[11.5px] text-muted-foreground">Output name</span>
                  <Input ref={fileRef} value={filename} onChange={(e) => setFilename(e.target.value)}
                    className="dp-mono min-w-0 flex-1 text-[12.5px]" />
                </>}
            <Button size="sm" disabled={!filename.trim() || !dest || !writable}
              onClick={() => dest && props.onPick({ destId, destName: dest.name, path: '', filename: filename.trim() })}>
              Use destination
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
const destinationBackendLabel = (backend: string) => {
  if (backend === 'local') return 'Local'
  if (backend === 's3') return 'S3'
  if (backend === 'gs') return 'GCS'
  return backend
}
