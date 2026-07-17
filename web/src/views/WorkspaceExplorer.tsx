import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { api, type CanvasFile } from '../api/client'
import { useStore } from '../store/graph'
import type { CatalogTable, WorkspaceMoveCanvasResult, WorkspaceResource } from '../types/api'
import { Icon } from '../ui/Icon'
import { CatalogDetail } from './CatalogView'

const LOCAL_ROOT_ID = 'workspace-local-root'
const PAGE_SIZE = 50

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)
const identity = (resource: WorkspaceResource) => resource.id.slice(resource.id.indexOf(':') + 1)

// The explorer deliberately consumes the bounded Workspace API rather than composing a canvas list
// and catalog page in the browser. A resource URL is opaque and remains valid when its display name
// or placement changes; only containers are expanded locally, one page at a time.
export function WorkspaceExplorer() {
  const requestedResourceId = useStore((s) => s.workspaceResourceId)
  const setWorkspaceResource = useStore((s) => s.setWorkspaceResource)
  const openFile = useStore((s) => s.openFile)
  const files = useStore((s) => s.files)
  const refreshFiles = useStore((s) => s.refreshFiles)
  const rememberTables = useStore((s) => s.rememberTables)
  const pushToast = useStore((s) => s.pushToast)
  const [containerId, setContainerId] = useState(LOCAL_ROOT_ID)
  const [container, setContainer] = useState<WorkspaceResource | null>(null)
  const [crumbs, setCrumbs] = useState<WorkspaceResource[]>([])
  const [items, setItems] = useState<WorkspaceResource[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null)
  const [selectedTable, setSelectedTable] = useState<CatalogTable | null>(null)
  const [selectedDataset, setSelectedDataset] = useState<WorkspaceResource | null>(null)
  const [selectedDetached, setSelectedDetached] = useState<WorkspaceResource | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [datasetAction, setDatasetAction] = useState<{ resource: WorkspaceResource; table: CatalogTable } | null>(null)
  const [moveResource, setMoveResource] = useState<WorkspaceResource | null>(null)
  const [undoMove, setUndoMove] = useState<{
    resource: WorkspaceResource; previousContainer: WorkspaceResource; destination: WorkspaceResource
  } | null>(null)
  const [undoBusy, setUndoBusy] = useState(false)
  const [revision, setRevision] = useState(0)
  const request = useRef(0)

  const load = useCallback(async (targetId: string, nextCursor?: string | null) => {
    const sequence = ++request.current
    const more = !!nextCursor
    if (more) { setLoadingMore(true); setLoadMoreError(null) }
    else {
      setLoading(true); setError(null); setLoadMoreError(null); setItems([]); setCursor(null); setHasMore(false)
    }
    try {
      const page = await api.workspaceBrowse(targetId, { limit: PAGE_SIZE, cursor: nextCursor ?? undefined })
      if (sequence !== request.current) return
      setContainerId(identity(page.container))
      setContainer(page.container)
      if (!more) setCrumbs((current) => current.length && current[current.length - 1].id === page.container.id ? current : [...current, page.container])
      setItems((current) => {
        const next = more ? current : []
        const seen = new Set(next.map((item) => item.id))
        return [...next, ...page.items.filter((item) => !seen.has(item.id))]
      })
      setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
    } catch (caught) {
      if (sequence !== request.current) return
      if (more) setLoadMoreError(errorMessage(caught))
      else setError(errorMessage(caught))
    } finally {
      if (sequence === request.current) { setLoading(false); setLoadingMore(false) }
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    const resolve = async () => {
      setSelectedTable(null); setSelectedDataset(null); setSelectedDetached(null)
      if (!requestedResourceId) {
        setCrumbs([])
        await load(LOCAL_ROOT_ID)
        return
      }
      try {
        const resolved = await api.workspaceResource(requestedResourceId)
        if (cancelled) return
        const container = resolved.resource.kind === 'container'
          ? resolved.resource
          : resolved.ancestors[resolved.ancestors.length - 1]
        if (!container) throw new Error('Workspace resource has no local container')
        setCrumbs(resolved.resource.kind === 'container'
          ? [...resolved.ancestors, resolved.resource]
          : resolved.ancestors)
        await load(identity(container))
        if (cancelled || resolved.resource.kind !== 'dataset') return
        if (resolved.resource.detached) { setSelectedDetached(resolved.resource); return }
        try {
          setSelectedDataset(resolved.resource)
          setSelectedTable(await api.tableByRegistration(identity(resolved.resource)))
        }
        catch (caught) {
          if (cancelled) return
          const status = typeof caught === 'object' && caught !== null
            ? (caught as { status?: unknown }).status
            : undefined
          if (status === 404) setSelectedDetached({ ...resolved.resource, detached: true })
          else { setError(errorMessage(caught)); setItems([]); setHasMore(false) }
        }
      } catch (caught) {
        if (!cancelled) { setError(errorMessage(caught)); setItems([]); setHasMore(false) }
      }
    }
    void resolve()
    return () => { cancelled = true; request.current += 1 }
  }, [requestedResourceId, load, revision])

  const open = (resource: WorkspaceResource) => {
    if (resource.kind === 'canvas') { void openFile(identity(resource)); return }
    setWorkspaceResource(resource.id)
  }
  const closeDetail = () => setWorkspaceResource(`container:${containerId}`)
  const useTable = (table: CatalogTable) => {
    if (!selectedDataset) {
      pushToast('Could not resolve the stable Workspace dataset identity', 'error')
      return
    }
    rememberTables([table])
    void refreshFiles()
    setDatasetAction({ resource: selectedDataset, table })
  }
  // Re-resolve the stable resource before reloading. This keeps rename/move refreshes truthful and
  // retries the same deep link rather than silently falling back to a different container.
  const reload = () => setRevision((current) => current + 1)
  const undoLastMove = async () => {
    if (!undoMove?.resource.placementId || undoMove.resource.version == null || undoMove.previousContainer.version == null) return
    setUndoBusy(true)
    try {
      await api.workspaceMoveCanvas(undoMove.resource.placementId, {
        containerId: identity(undoMove.previousContainer),
        expectedContainerVersion: undoMove.previousContainer.version,
        expectedVersion: undoMove.resource.version,
      })
      setUndoMove(null)
      pushToast('Canvas move undone', 'success')
      reload()
    } catch (caught) {
      pushToast(`Could not undo move: ${errorMessage(caught)}`, 'error')
    } finally { setUndoBusy(false) }
  }

  return (
    <div className="flex h-full min-w-0 flex-col">
      <header className="flex min-h-[68px] items-center gap-3 border-b border-border px-7 py-3">
        <div className="min-w-0">
          <h1 className="text-[20px] font-bold text-foreground">Workspace</h1>
          <nav aria-label="Workspace path" className="mt-0.5 flex min-w-0 items-center gap-1 overflow-hidden text-[11.5px] text-muted-foreground">
            <button onClick={() => setWorkspaceResource(null)} className="shrink-0 hover:text-foreground">Workspace</button>
            {crumbs.slice(1).map((crumb) => <span key={crumb.id} className="flex min-w-0 items-center gap-1"><span>/</span><button onClick={() => setWorkspaceResource(crumb.id)} className="truncate hover:text-foreground">{crumb.name}</button></span>)}
          </nav>
        </div>
        <span className="flex-1" />
        <div className="hidden items-center gap-2 sm:flex" aria-label="Workspace actions">
          <button onClick={() => setCreateOpen(true)} disabled={!container || container.version == null || loading}
            title={container ? `Create in ${container.name}` : 'Load a Workspace destination first'}
            className="rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-semibold text-foreground disabled:text-muted-foreground disabled:opacity-65">New canvas here</button>
        </div>
        <button onClick={reload} disabled={loading || loadingMore} data-testid="workspace-reload" className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-semibold text-foreground disabled:opacity-50">
          <Icon name="refresh" size={13} /> Reload
        </button>
      </header>

      {undoMove && <div role="status" className="flex items-center gap-2 border-b border-border bg-primary/5 px-7 py-2 text-[12px] text-foreground">
        <span className="flex-1">Moved “{undoMove.resource.name}” to {undoMove.destination.name}.</span>
        <button onClick={() => void undoLastMove()} disabled={undoBusy} className="font-semibold text-primary underline disabled:opacity-50">{undoBusy ? 'Undoing…' : 'Undo move'}</button>
        <button onClick={() => setUndoMove(null)} aria-label="Dismiss move confirmation"><Icon name="close" size={13} /></button>
      </div>}

      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        {error ? <div role="alert" className="mx-auto flex max-w-md flex-col items-center gap-2 rounded-lg border border-destructive/30 p-5 text-center text-[13px] text-destructive">
          <span>Couldn't load this Workspace location: {error}</span>
          <button onClick={reload} className="font-semibold underline">Retry</button>
        </div> : loading ? <div className="grid h-full place-items-center text-[13px] text-muted-foreground">Loading Workspace…</div> : items.length ? <div className="mx-auto grid max-w-5xl gap-2">
          {items.map((resource) => <ResourceRow key={resource.id} resource={resource} onOpen={() => open(resource)}
            onMove={resource.kind === 'canvas' && !resource.detached ? () => setMoveResource(resource) : undefined} />)}
          {loadMoreError && <div role="alert" className="mx-auto mt-2 text-[12px] text-destructive">Couldn't load more: {loadMoreError}</div>}
          {hasMore && <button onClick={() => void load(containerId, cursor)} disabled={loadingMore} data-testid="workspace-load-more" className="mx-auto mt-2 rounded-md border border-border bg-card px-3 py-1.5 text-[12px] font-semibold text-foreground disabled:opacity-50">
            {loadingMore ? 'Loading…' : loadMoreError ? 'Retry load more' : 'Load more'}
          </button>}
        </div> : <div className="grid h-full place-items-center px-4 text-center text-[13px] text-muted-foreground"><span>This local container is empty. Create a canvas here to get started.</span></div>}
      </div>

      {selectedTable && <CatalogDetail table={selectedTable} onClose={closeDetail} onUse={useTable}
        onChanged={(table) => { setSelectedTable(table); void load(containerId) }} onDeleted={closeDetail}
        onOpenTable={setSelectedTable} onFolder={() => pushToast('Dataset folders are not Workspace containers.', 'info')}
        onColumn={() => pushToast('Column filters are available from the dataset detail only.', 'info')} />}
      {selectedDetached && <DetachedResource resource={selectedDetached} onClose={closeDetail} />}
      {createOpen && container?.version != null && <NewCanvasDialog container={container} onClose={() => setCreateOpen(false)}
        onCreated={(canvasId) => { setCreateOpen(false); void openFile(canvasId) }} />}
      {datasetAction && container?.version != null && <DatasetActionDialog action={datasetAction} container={container}
        files={files} onClose={() => setDatasetAction(null)}
        onOpened={(canvasId) => { setDatasetAction(null); setSelectedTable(null); setSelectedDataset(null); void openFile(canvasId) }} />}
      {moveResource && container && <MoveCanvasDialog resource={moveResource} sourceContainer={container} onClose={() => setMoveResource(null)}
        onMoved={(result) => {
          setMoveResource(null)
          setUndoMove({ resource: result.resource, previousContainer: result.previousContainer, destination: result.container })
          reload()
        }} />}
    </div>
  )
}

function NewCanvasDialog({ container, onClose, onCreated }: {
  container: WorkspaceResource; onClose: () => void; onCreated: (canvasId: string) => void
}) {
  const [name, setName] = useState('untitled')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = async () => {
    if (!name.trim() || container.version == null || busy) return
    setBusy(true); setError(null)
    try {
      const created = await api.workspaceCreateCanvas({
        containerId: identity(container), expectedContainerVersion: container.version, name: name.trim(),
      })
      onCreated(created.id)
    } catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label="New canvas here" onClose={onClose}>
    <p className="text-[12px] text-muted-foreground">Destination: <strong className="text-foreground">{container.name}</strong></p>
    <label className="grid gap-1 text-[11px] text-muted-foreground">Canvas name
      <input autoFocus value={name} onChange={(event) => setName(event.target.value)} className="dp-input" />
    </label>
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void submit()} disabled={!name.trim() || busy} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Creating…' : 'Create canvas'}</button></div>
  </Modal>
}

function DatasetActionDialog({ action, container, files, onClose, onOpened }: {
  action: { resource: WorkspaceResource; table: CatalogTable }; container: WorkspaceResource
  files: CanvasFile[]; onClose: () => void; onOpened: (canvasId: string) => void
}) {
  const editable = files.filter((file) => file.role === 'owner' || file.role === 'editor')
  const [mode, setMode] = useState<'explore' | 'add'>('explore')
  const [name, setName] = useState(`${action.table.name} exploration`)
  const [canvasId, setCanvasId] = useState(editable[0]?.id ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!editable.some((file) => file.id === canvasId)) setCanvasId(editable[0]?.id ?? '')
  }, [canvasId, files])
  const submit = async () => {
    if (busy) return
    setBusy(true); setError(null)
    try {
      const datasetId = identity(action.resource)
      if (mode === 'explore') {
        if (container.version == null || !name.trim()) return
        const created = await api.workspaceCreateCanvas({
          containerId: identity(container), expectedContainerVersion: container.version,
          name: name.trim(), datasetId,
        })
        onOpened(created.id)
      } else {
        const target = editable.find((file) => file.id === canvasId)
        if (!target) { setError('Choose an editable target canvas'); return }
        await api.workspaceAddDataset(target.id, {
          datasetId, expectedCanvasVersion: target.version,
        })
        onOpened(target.id)
      }
    } catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label={`Use ${action.table.name}`} onClose={onClose}>
    <p className="break-all text-[11px] text-muted-foreground">Stable dataset: {action.resource.id}</p>
    <div className="grid grid-cols-2 gap-2">
      <button onClick={() => setMode('explore')} aria-pressed={mode === 'explore'} className={`rounded-lg border p-3 text-left ${mode === 'explore' ? 'border-primary bg-primary/5' : 'border-border'}`}>
        <span className="block text-[12px] font-semibold">Explore in new canvas</span><span className="text-[10.5px] text-muted-foreground">Create in {container.name}</span>
      </button>
      <button onClick={() => setMode('add')} aria-pressed={mode === 'add'} className={`rounded-lg border p-3 text-left ${mode === 'add' ? 'border-primary bg-primary/5' : 'border-border'}`}>
        <span className="block text-[12px] font-semibold">Add to canvas</span><span className="text-[10.5px] text-muted-foreground">Choose one exact target</span>
      </button>
    </div>
    {mode === 'explore' ? <label className="grid gap-1 text-[11px] text-muted-foreground">New canvas name
      <input value={name} onChange={(event) => setName(event.target.value)} className="dp-input" />
    </label> : editable.length ? <label className="grid gap-1 text-[11px] text-muted-foreground">Target canvas
      <select aria-label="Target canvas" value={canvasId} onChange={(event) => setCanvasId(event.target.value)} className="dp-input">
        {editable.map((file) => <option key={file.id} value={file.id}>{file.name} · {file.id}</option>)}
      </select>
    </label> : <div role="status" className="text-[12px] text-muted-foreground">No editable canvas is available. Explore in a new canvas instead.</div>}
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void submit()} disabled={busy || (mode === 'explore' ? !name.trim() : !canvasId)} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Applying…' : mode === 'explore' ? 'Create and open' : 'Add and open'}</button></div>
  </Modal>
}

function MoveCanvasDialog({ resource, sourceContainer, onClose, onMoved }: {
  resource: WorkspaceResource; sourceContainer: WorkspaceResource; onClose: () => void
  onMoved: (result: WorkspaceMoveCanvasResult) => void
}) {
  const [path, setPath] = useState<WorkspaceResource[]>([])
  const [container, setContainer] = useState<WorkspaceResource | null>(null)
  const [children, setChildren] = useState<WorkspaceResource[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(async (targetId: string, nextCursor?: string | null, nextPath?: WorkspaceResource[]) => {
    setLoading(true); setError(null)
    try {
      const page = await api.workspaceBrowse(targetId, { limit: PAGE_SIZE, cursor: nextCursor ?? undefined })
      setContainer(page.container)
      setChildren((current) => nextCursor ? [...current, ...page.items.filter((item) => item.kind === 'container')] : page.items.filter((item) => item.kind === 'container'))
      setCursor(page.nextCursor ?? null); setHasMore(page.hasMore)
      if (!nextCursor) setPath(nextPath ?? [page.container])
    } catch (caught) { setError(errorMessage(caught)) }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void load(LOCAL_ROOT_ID) }, [load])
  const move = async () => {
    if (!resource.placementId || resource.version == null || !container || container.version == null || busy) return
    setBusy(true); setError(null)
    try {
      onMoved(await api.workspaceMoveCanvas(resource.placementId, {
        containerId: identity(container), expectedContainerVersion: container.version,
        expectedVersion: resource.version,
      }))
    } catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <Modal label={`Move ${resource.name}`} onClose={onClose}>
    <p className="text-[11px] text-muted-foreground">Current location: <strong className="text-foreground">{sourceContainer.name}</strong></p>
    <nav aria-label="Choose destination path" className="flex flex-wrap gap-1 text-[11px]">
      {path.map((item, index) => <button key={item.id} onClick={() => void load(identity(item), null, path.slice(0, index + 1))} className="text-primary underline">{item.name}</button>)}
    </nav>
    <div className="max-h-[220px] overflow-y-auto rounded-lg border border-border p-1">
      {loading && !children.length ? <div className="p-3 text-[11px] text-muted-foreground">Loading containers…</div> : children.map((child) => <button key={child.id} onClick={() => void load(identity(child), null, [...path, child])}
        className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] hover:bg-accent"><Icon name="chevronRight" size={12} /> {child.name}</button>)}
      {!loading && !children.length && <div className="p-3 text-[11px] text-muted-foreground">No child containers.</div>}
      {hasMore && <button onClick={() => void load(identity(container!), cursor)} disabled={loading} className="p-2 text-[11px] font-semibold text-primary">Load more containers</button>}
    </div>
    {container && <p className="text-[12px]">Destination: <strong>{container.name}</strong></p>}
    {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
    <div className="flex justify-end gap-2"><button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button>
      <button onClick={() => void move()} disabled={busy || !container || container.id === sourceContainer.id} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Moving…' : `Move to ${container?.name ?? 'destination'}`}</button></div>
  </Modal>
}

function Modal({ label, onClose, children }: { label: string; onClose: () => void; children: ReactNode }) {
  return <div className="fixed inset-0 z-50 grid place-items-center bg-black/30 p-4" onClick={onClose}>
    <div role="dialog" aria-modal="true" aria-label={label} className="grid w-[460px] max-w-full gap-3 rounded-xl border border-border bg-card p-5 shadow-xl" onClick={(event) => event.stopPropagation()}>
      <div className="flex items-center gap-2"><h2 className="flex-1 text-[15px] font-bold">{label}</h2><button onClick={onClose} aria-label="Close"><Icon name="close" size={15} /></button></div>
      {children}
    </div>
  </div>
}

function ResourceRow({ resource, onOpen, onMove }: { resource: WorkspaceResource; onOpen: () => void; onMove?: () => void }) {
  const icon = resource.kind === 'dataset' ? 'db' : resource.kind === 'canvas' ? 'grid' : 'chevronRight'
  const kind = resource.kind === 'container' ? 'Container' : resource.kind === 'canvas' ? 'Canvas' : 'Dataset'
  return <div className="flex min-w-0 items-center rounded-lg border border-border bg-card hover:border-primary/40 hover:bg-accent">
    <button type="button" onClick={onOpen} aria-label={`Open ${kind.toLowerCase()} ${resource.name}`}
      className="flex min-w-0 flex-1 items-center gap-3 px-3 py-3 text-left">
      <Icon name={icon} size={16} style={{ color: 'hsl(var(--muted-foreground))' }} />
      <span className="min-w-0 flex-1"><span className="block truncate text-[13px] font-semibold text-foreground">{resource.name}</span><span className="text-[11px] text-muted-foreground">{kind}{resource.detached ? ' · detached' : ''}</span></span>
      {resource.kind === 'container' && <Icon name="chevronRight" size={14} style={{ color: 'hsl(var(--muted-foreground))' }} />}
    </button>
    {onMove && <button type="button" onClick={onMove} aria-label={`Move canvas ${resource.name}`}
      className="mr-2 rounded-md border border-border bg-card px-2 py-1 text-[11px] font-semibold text-muted-foreground hover:text-foreground">Move</button>}
  </div>
}

function DetachedResource({ resource, onClose }: { resource: WorkspaceResource; onClose: () => void }) {
  return <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}>
    <div role="dialog" aria-modal="true" aria-label={resource.name} onClick={(event) => event.stopPropagation()} className="flex h-full w-[420px] flex-col border-l border-border bg-card p-5 shadow-xl">
      <div className="flex items-center gap-2"><Icon name="db" size={16} /><div className="min-w-0 flex-1 truncate text-[14px] font-bold">{resource.name}</div><button onClick={onClose} aria-label="Close"><Icon name="close" size={15} /></button></div>
      <p className="mt-5 text-[13px] leading-6 text-muted-foreground">This Workspace placement is detached: its local dataset is no longer available. Its stable placement remains visible, but there is no dataset detail to show.</p>
    </div>
  </div>
}
