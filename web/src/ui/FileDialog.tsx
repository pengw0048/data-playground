import { useCallback, useEffect, useRef, useState } from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
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
export interface SaveDialogDraft { destId: string; path: string; filename: string }

type CommonDialogProps = {
  onClose: () => void
  title?: string
  /** Preserve the original picker trigger across a temporary Settings handoff. */
  restoreFocusTo?: HTMLElement | null
}

export function FileDialog(props:
  | (CommonDialogProps & { mode: 'open'; onPick: (r: OpenResult) => void | Promise<void> })
  | (CommonDialogProps & {
      mode: 'save'
      defaultName?: string
      initialDraft?: SaveDialogDraft
      onPick: (r: SaveResult) => void
      onManageDestinations?: (draft: SaveDialogDraft, restoreFocusTo: HTMLElement | null) => void
    }),
) {
  const { mode, onClose } = props
  const [dests, setDests] = useState<DestinationPreset[]>([])
  const [destId, setDestId] = useState(mode === 'save' ? (props.initialDraft?.destId ?? '') : '')
  const [path, setPath] = useState(mode === 'save' ? (props.initialDraft?.path ?? '') : '')
  const [entries, setEntries] = useState<BrowseEntry[]>([])
  const [destError, setDestError] = useState<string | null>(null)
  const [browseError, setBrowseError] = useState<string | null>(null)
  const [loadingDests, setLoadingDests] = useState(true)
  const [loading, setLoading] = useState(false)
  const [writable, setWritable] = useState(true)
  const [filename, setFilename] = useState(mode === 'save'
    ? (props.initialDraft?.filename ?? props.defaultName ?? 'output') : '')
  const [pickError, setPickError] = useState<string | null>(null)
  const [pickingUri, setPickingUri] = useState<string | null>(null)
  const [creatingFolder, setCreatingFolder] = useState(false)
  const [newFolderName, setNewFolderName] = useState('')
  const [folderError, setFolderError] = useState<string | null>(null)
  const [makingFolder, setMakingFolder] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const handingOff = useRef(false)
  const [returnFocus] = useState<HTMLElement | null>(() => props.restoreFocusTo !== undefined
    ? props.restoreFocusTo
    : document.activeElement instanceof HTMLElement ? document.activeElement : null)
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
  const refresh = useCallback(async () => {
    const s = ++browseRequest.current
    setPickError(null)
    if (!destId) {
      setEntries([]); setBrowseError(null); setLoading(false)
      return
    }
    setLoading(true); setBrowseError(null); setEntries([]); setWritable(true)
    try {
      const r = await api.browseDestination(destId, path)
      if (s !== browseRequest.current) return
      if (r.path !== path) setPath(r.path)
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
  const dest = dests.find((d) => d.id === destId)
  const filenameError = mode === 'save' ? validateDatasetName(filename) : null
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
  const selectDestination = (id: string) => {
    setDestId(id)
    setPath('')
    setCreatingFolder(false)
    setNewFolderName('')
    setFolderError(null)
  }
  const createFolder = async () => {
    if (mode !== 'save' || !dest || makingFolder) return
    const name = newFolderName
    const invalid = !name || name !== name.trim() || name === '.' || name === '..'
      || /[\\/]/.test(name) || [...name].some((char) => char.charCodeAt(0) < 32)
    if (invalid) {
      setFolderError('Enter one folder name without surrounding spaces, slashes, control characters, “.”, or “..”.')
      return
    }
    setMakingFolder(true)
    setFolderError(null)
    try {
      const result = await api.mkdirDestination(dest.id, path, name)
      if (result.error) {
        setFolderError(result.error)
        return
      }
      setCreatingFolder(false)
      setNewFolderName('')
      setPath(joinRelativePath(path, name))
    } catch (e) {
      setFolderError(errorMessage(e))
    } finally {
      setMakingFolder(false)
    }
  }
  const pickSaveDestination = () => {
    if (mode !== 'save' || !dest || !writable || filenameError) return
    props.onPick({ destId, destName: dest.name, path, filename })
  }

  return (
    <DialogPrimitive.Root open onOpenChange={(open) => { if (!open && !pickingUri) onClose() }}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="dp-modal-overlay fixed inset-0 z-[2100] bg-black/30" />
        <DialogPrimitive.Content aria-modal="true" aria-describedby={undefined}
          onEscapeKeyDown={(event) => {
            // The dialog closes during document capture. Stop this native event there so React
            // Flow cannot also treat it as Escape on the underlying selected node.
            event.stopPropagation()
            if (pickingUri) event.preventDefault()
          }}
          onPointerDownOutside={(event) => { if (pickingUri) event.preventDefault() }}
          onOpenAutoFocus={(event) => {
            if (mode !== 'save' || !fileRef.current) return
            event.preventDefault()
            fileRef.current.focus()
            const dot = filename.lastIndexOf('.')
            fileRef.current.setSelectionRange(0, dot > 0 ? dot : filename.length)
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault()
            if (!handingOff.current) returnFocus?.focus()
          }}
        className={cn(
          'fixed left-1/2 top-1/2 z-[2100] flex -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-lg border border-border bg-card shadow-lg',
          mode === 'save'
            ? 'h-[min(520px,88vh)] w-[min(760px,94vw)]'
            : 'h-[min(460px,88vh)] w-[min(640px,94vw)]',
        )}>
        <div className="flex items-center gap-2 border-b border-border px-[14px] py-[11px]">
          <span className="flex items-center text-muted-foreground"><Icon name={mode === 'save' ? 'export' : 'db'} size={14} /></span>
          <DialogPrimitive.Title className="text-[13.5px] font-semibold">
            {props.title ?? (mode === 'save' ? 'Choose output destination' : 'Open a file')}
          </DialogPrimitive.Title>
          <span className="flex-1" />
          <DialogPrimitive.Close asChild>
            <button disabled={pickingUri !== null} aria-label="Close" className="grid h-6 w-[26px] place-items-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"><Icon name="close" size={13} /></button>
          </DialogPrimitive.Close>
        </div>

        <div className="flex min-h-0 flex-1">
          {/* left sidebar — switch configured destination */}
          <div className="flex w-[184px] shrink-0 flex-col overflow-y-auto border-r border-border bg-muted/30 p-1.5">
            <div className="px-2 py-1 text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground">
              {mode === 'save' ? 'Destinations' : 'Places'}
            </div>
            {dests.map((d) => (
              <button key={d.id} onClick={() => selectDestination(d.id)}
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
            <span className="flex-1" />
            {mode === 'save' && props.onManageDestinations && (
              <Button variant="ghost" size="sm" className="mt-2 justify-start" onClick={() => {
                handingOff.current = true
                props.onManageDestinations?.({ destId, path, filename }, returnFocus)
              }}>
                <Icon name="settings" size={12} /> Manage destinations
              </Button>
            )}
          </div>

          {/* Open and save share one browser. Save selects the current folder, not an arbitrary URI. */}
          <div className="flex min-w-0 flex-1 flex-col">
              <div className="flex min-h-10 items-center gap-1 overflow-x-auto whitespace-nowrap border-b border-border px-3 py-[7px] text-[11.5px] text-muted-foreground">
                <button onClick={() => setPath('')} className={crumbBtn}>{dest?.name ?? '—'}</button>
                {segs.map((s, i) => (
                  <span key={i} className="inline-flex items-center gap-1">
                    <span className="flex items-center text-muted-foreground"><Icon name="chevronRight" size={10} /></span>
                    <button onClick={() => setPath(segs.slice(0, i + 1).join('/'))} className={crumbBtn}>{s}</button>
                  </span>
                ))}
                <span className="flex-1" />
                {mode === 'save' && dest && writable && (
                  <Button variant="ghost" size="sm" className="shrink-0"
                    onClick={() => { setCreatingFolder(true); setFolderError(null) }}>
                    <Icon name="plus" size={12} /> New folder
                  </Button>
                )}
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
                {mode === 'save' && creatingFolder && (
                  <form className="m-1 rounded-md border border-border bg-muted/30 p-2"
                    onSubmit={(e) => { e.preventDefault(); void createFolder() }}>
                    <div className="flex items-center gap-2">
                      <Input autoFocus aria-label="New folder name" value={newFolderName}
                        onChange={(e) => { setNewFolderName(e.target.value); setFolderError(null) }}
                        placeholder="Folder name" className="min-w-0 flex-1 text-xs" />
                      <Button size="sm" type="submit" disabled={makingFolder}>
                        {makingFolder ? 'Creating…' : 'Create'}
                      </Button>
                      <Button size="sm" type="button" variant="ghost" disabled={makingFolder}
                        onClick={() => { setCreatingFolder(false); setNewFolderName(''); setFolderError(null) }}>
                        Cancel
                      </Button>
                    </div>
                    {folderError && <div role="alert" className="mt-1.5 text-[11px] text-destructive">{folderError}</div>}
                  </form>
                )}
                {pickError && (
                  <div role="alert" className="m-1 rounded-md border border-destructive/30 px-3 py-2 text-xs text-destructive">
                    Couldn't open file: {pickError}. Your selection has not been changed; choose the file to retry.
                  </div>
                )}
                {!destId ? <div className="p-4 text-xs text-muted-foreground">
                    {loadingDests ? 'Loading places…' : destError ? 'Places are unavailable. Retry from the sidebar.' : 'No destinations configured.'}
                  </div>
                  : loading ? <div className="p-4 text-xs text-muted-foreground">Loading…</div>
                  : browseError ? <>
                      <div role="alert" className="m-1 flex items-center justify-between gap-2 rounded-md border border-destructive/30 px-3 py-2 text-xs text-destructive">
                        <span>Couldn't load this folder: {browseError}</span>
                        <button onClick={() => void refresh()} data-testid="file-dialog-browse-retry" className="shrink-0 font-semibold underline">Retry</button>
                      </div>
                      {mode === 'save' && writable && (
                        <div className="px-3 py-2 text-[11px] text-muted-foreground">
                          You can still save to this configured location; write access is checked when the run starts.
                        </div>
                      )}
                    </>
                  : entries.length === 0 ? <div className="p-4 text-xs text-muted-foreground">Empty folder.</div>
                  : entries.map((e) => e.kind === 'dir' || mode === 'open' ? (
                      <button key={e.uri} disabled={pickingUri !== null} onClick={() => {
                        if (e.kind === 'dir') setPath(joinRelativePath(path, e.name))
                        else void pickOpenFile(e)
                      }}
                        className="flex w-full items-center gap-[9px] rounded-md px-2.5 py-2 text-left text-[12.5px] text-foreground transition-colors hover:bg-accent disabled:opacity-60">
                        <span className={cn('flex items-center', e.kind === 'dir' ? 'text-primary' : 'text-muted-foreground')}><Icon name={e.kind === 'dir' ? 'grid' : 'db'} size={14} /></span>
                        <span className="flex-1 overflow-hidden text-ellipsis">{e.name}</span>
                        {pickingUri === e.uri && <span className="text-[10.5px] text-muted-foreground">Opening…</span>}
                        {e.kind === 'dir' && <span className="flex items-center text-muted-foreground"><Icon name="chevronRight" size={12} /></span>}
                      </button>
                    ) : (
                      <div key={e.uri} className="flex w-full items-center gap-[9px] rounded-md px-2.5 py-2 text-left text-[12.5px] text-muted-foreground">
                        <span className="flex items-center"><Icon name="db" size={14} /></span>
                        <span className="flex-1 overflow-hidden text-ellipsis">{e.name}</span>
                      </div>
                    ))}
              </div>
          </div>
        </div>

        {mode === 'save' && (
          <div className="flex items-end gap-2 border-t border-border px-[14px] py-2.5">
            {!writable
              ? <span className="flex-1 text-[11px] text-amber-600">This destination can't accept this output — install its plugin or choose another destination.</span>
              : <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <label htmlFor="file-dialog-dataset-name" className="shrink-0 text-[11.5px] text-muted-foreground">
                      Dataset name
                    </label>
                    <Input id="file-dialog-dataset-name" ref={fileRef} value={filename}
                      aria-invalid={filenameError ? true : undefined}
                      aria-describedby={filenameError ? 'file-dialog-dataset-name-error' : undefined}
                      onChange={(e) => setFilename(e.target.value)}
                      className="dp-mono min-w-0 flex-1 text-[12.5px]" />
                  </div>
                  {filenameError && (
                    <div id="file-dialog-dataset-name-error" role="alert"
                      className="mt-1 text-[11px] text-destructive">
                      {filenameError}
                    </div>
                  )}
                </div>}
            <Button size="sm" disabled={!!filenameError || !dest || !writable}
              onClick={pickSaveDestination}>
              Save here
            </Button>
          </div>
        )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

const crumbBtn = 'inline-flex items-center gap-[3px] rounded px-1 py-0.5 text-[11.5px] font-semibold text-primary transition-colors hover:bg-accent/60'
const errorMessage = (e: unknown) => e instanceof Error ? e.message : String(e)
const joinRelativePath = (path: string, child: string) => path ? `${path}/${child}` : child

function validateDatasetName(value: string): string | null {
  if (!value.trim()) return 'Enter a dataset name.'
  if (value !== value.trim()) {
    return 'Dataset name cannot contain surrounding whitespace. Edit it to continue.'
  }
  if (/^\.+$/.test(value)) return 'Dataset name cannot consist only of dots.'
  const hasControlCharacter = [...value].some((character) => {
    const codePoint = character.codePointAt(0) ?? 0
    return codePoint <= 0x1f || (codePoint >= 0x7f && codePoint <= 0x9f)
  })
  if (['/', '\\', ':', '*', '?', '['].some((character) => value.includes(character))
      || hasControlCharacter) {
    return 'Dataset name must be one name, not a path. Remove slashes, “:”, “*”, “?”, “[”, and control characters.'
  }
  return null
}
