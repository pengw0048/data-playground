import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { useStore } from '../store/graph'
import type { CatalogTable, WorkspaceResource } from '../types/api'
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
  const addToCanvas = useStore((s) => s.addToCanvas)
  const rememberTables = useStore((s) => s.rememberTables)
  const pushToast = useStore((s) => s.pushToast)
  const [containerId, setContainerId] = useState(LOCAL_ROOT_ID)
  const [crumbs, setCrumbs] = useState<WorkspaceResource[]>([])
  const [items, setItems] = useState<WorkspaceResource[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null)
  const [selectedTable, setSelectedTable] = useState<CatalogTable | null>(null)
  const [selectedDetached, setSelectedDetached] = useState<WorkspaceResource | null>(null)
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
      setSelectedTable(null); setSelectedDetached(null)
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
        try { setSelectedTable(await api.table(identity(resolved.resource))) }
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
    rememberTables([table])
    addToCanvas('source', { uri: table.uri, tableId: table.id }, table.name)
  }
  // Re-resolve the stable resource before reloading. This keeps rename/move refreshes truthful and
  // retries the same deep link rather than silently falling back to a different container.
  const reload = () => setRevision((current) => current + 1)

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
          <button disabled title="Canvas creation and placement workflows are not available yet" className="rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-semibold text-muted-foreground opacity-65">New canvas</button>
          <button disabled title="Dataset placement workflows are not available yet" className="rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-semibold text-muted-foreground opacity-65">Add dataset</button>
        </div>
        <button onClick={reload} disabled={loading || loadingMore} data-testid="workspace-reload" className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-semibold text-foreground disabled:opacity-50">
          <Icon name="refresh" size={13} /> Reload
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        {error ? <div role="alert" className="mx-auto flex max-w-md flex-col items-center gap-2 rounded-lg border border-destructive/30 p-5 text-center text-[13px] text-destructive">
          <span>Couldn't load this Workspace location: {error}</span>
          <button onClick={reload} className="font-semibold underline">Retry</button>
        </div> : loading ? <div className="grid h-full place-items-center text-[13px] text-muted-foreground">Loading Workspace…</div> : items.length ? <div className="mx-auto grid max-w-5xl gap-2">
          {items.map((resource) => <ResourceRow key={resource.id} resource={resource} onOpen={() => open(resource)} />)}
          {loadMoreError && <div role="alert" className="mx-auto mt-2 text-[12px] text-destructive">Couldn't load more: {loadMoreError}</div>}
          {hasMore && <button onClick={() => void load(containerId, cursor)} disabled={loadingMore} data-testid="workspace-load-more" className="mx-auto mt-2 rounded-md border border-border bg-card px-3 py-1.5 text-[12px] font-semibold text-foreground disabled:opacity-50">
            {loadingMore ? 'Loading…' : loadMoreError ? 'Retry load more' : 'Load more'}
          </button>}
        </div> : <div className="grid h-full place-items-center px-4 text-center text-[13px] text-muted-foreground"><span>This local container is empty. Canvas creation and dataset placement will appear here when their workflows are available.</span></div>}
      </div>

      {selectedTable && <CatalogDetail table={selectedTable} onClose={closeDetail} onUse={useTable}
        onChanged={(table) => { setSelectedTable(table); void load(containerId) }} onDeleted={closeDetail}
        onOpenTable={setSelectedTable} onFolder={() => pushToast('Dataset folders are not Workspace containers.', 'info')}
        onColumn={() => pushToast('Column filters are available from the dataset detail only.', 'info')} />}
      {selectedDetached && <DetachedResource resource={selectedDetached} onClose={closeDetail} />}
    </div>
  )
}

function ResourceRow({ resource, onOpen }: { resource: WorkspaceResource; onOpen: () => void }) {
  const icon = resource.kind === 'dataset' ? 'db' : resource.kind === 'canvas' ? 'grid' : 'chevronRight'
  const kind = resource.kind === 'container' ? 'Container' : resource.kind === 'canvas' ? 'Canvas' : 'Dataset'
  return <button type="button" onClick={onOpen} aria-label={`Open ${kind.toLowerCase()} ${resource.name}`}
    className="flex min-w-0 items-center gap-3 rounded-lg border border-border bg-card px-3 py-3 text-left hover:border-primary/40 hover:bg-accent">
    <Icon name={icon} size={16} style={{ color: 'hsl(var(--muted-foreground))' }} />
    <span className="min-w-0 flex-1"><span className="block truncate text-[13px] font-semibold text-foreground">{resource.name}</span><span className="text-[11px] text-muted-foreground">{kind}{resource.detached ? ' · detached' : ''}</span></span>
    {resource.kind === 'container' && <Icon name="chevronRight" size={14} style={{ color: 'hsl(var(--muted-foreground))' }} />}
  </button>
}

function DetachedResource({ resource, onClose }: { resource: WorkspaceResource; onClose: () => void }) {
  return <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}>
    <div role="dialog" aria-modal="true" aria-label={resource.name} onClick={(event) => event.stopPropagation()} className="flex h-full w-[420px] flex-col border-l border-border bg-card p-5 shadow-xl">
      <div className="flex items-center gap-2"><Icon name="db" size={16} /><div className="min-w-0 flex-1 truncate text-[14px] font-bold">{resource.name}</div><button onClick={onClose} aria-label="Close"><Icon name="close" size={15} /></button></div>
      <p className="mt-5 text-[13px] leading-6 text-muted-foreground">This Workspace placement is detached: its local dataset is no longer available. Its stable placement remains visible, but there is no dataset detail to show.</p>
    </div>
  </div>
}
