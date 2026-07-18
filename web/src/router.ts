// Minimal hash router — no dependency, works offline. The URL reflects the app's view + open canvas
// so the browser back/forward buttons work, a refresh restores where you were, and Share can produce
// a link that opens straight into a specific canvas (#/canvas/<id>).
import type { DpView } from './store/graph'

export interface Route { view: DpView; canvasId?: string; nodeId?: string; workspaceResourceId?: string; workspaceQuery?: string; workspaceScope?: 'all' | 'datasets'; workspaceDatasetQuery?: string; jobsQuery?: string; inboxQuery?: string; transformId?: string; transformVersion?: string; transformCanvasId?: string; transformNodeId?: string; transformQuery?: string }

const DATASET_QUERY_KEYS = ['dq', 'folder', 'tags', 'owner', 'columns', 'sort', 'order', 'match'] as const

export function parseHash(): Route {
  const h = location.hash.replace(/^#\/?/, '')
  const [path, rawQuery = ''] = h.split('?', 2)
  const [seg, id] = path.split('/')
  const params = new URLSearchParams(rawQuery)
  const workspaceQuery = params.get('q')?.trim() || undefined
  if (seg === 'canvas' && id) return {
    view: 'canvas', canvasId: decodeURIComponent(id),
    nodeId: params.get('node') || undefined,
  }
  if (seg === 'workspace') {
    const workspaceScope = params.get('scope') === 'datasets' ? 'datasets' : 'all'
    const datasetParams = new URLSearchParams()
    for (const key of DATASET_QUERY_KEYS) {
      const value = params.get(key)
      if (value) datasetParams.set(key, value)
    }
    const workspaceDatasetQuery = datasetParams.toString() || undefined
    return {
      view: 'workspace',
      workspaceResourceId: id ? decodeURIComponent(id) : undefined,
      ...(workspaceScope === 'datasets' ? { workspaceScope } : {}),
      ...(workspaceScope === 'datasets' && workspaceDatasetQuery ? { workspaceDatasetQuery } : {}),
      ...(workspaceScope === 'all' && workspaceQuery ? { workspaceQuery } : {}),
    }
  }
  // Recents and Tables are intentionally redirected to the single local Workspace explorer.
  if (seg === 'files' || seg === 'tables') return { view: 'workspace' }
  if (seg === 'jobs') return { view: 'jobs', jobsQuery: params.toString() }
  if (seg === 'inbox') return { view: 'inbox', inboxQuery: params.toString() }
  if (seg === 'transforms') {
    const transformVersion = params.get('version') || undefined
    const transformCanvasId = params.get('canvas') || undefined
    const transformNodeId = params.get('node') || undefined
    params.delete('version')
    params.delete('canvas')
    params.delete('node')
    return {
      view: 'transforms',
      ...(id ? { transformId: decodeURIComponent(id) } : {}),
      ...(transformVersion ? { transformVersion } : {}),
      ...(transformCanvasId && transformNodeId ? { transformCanvasId, transformNodeId } : {}),
      ...(params.size ? { transformQuery: params.toString() } : {}),
    }
  }
  if (seg === 'relationships') return { view: seg }
  // bare "/" opens the editor on the last/newest canvas (bootstrap picks the id).
  return { view: 'canvas' }
}

export function routeHash(view: DpView, canvasId?: string, workspaceResourceId?: string, workspaceQuery?: string, jobsQuery?: string, nodeId?: string, inboxQuery?: string, workspaceScope?: 'all' | 'datasets', workspaceDatasetQuery?: string, transformId?: string, transformVersion?: string, transformQuery?: string, transformCanvasId?: string, transformNodeId?: string): string {
  const path = view === 'canvas' && canvasId ? `#/canvas/${encodeURIComponent(canvasId)}`
    : view === 'transforms' && transformId ? `#/transforms/${encodeURIComponent(transformId)}` : `#/${view}`
    + (view === 'workspace' && workspaceResourceId ? `/${encodeURIComponent(workspaceResourceId)}` : '')
  const workspaceParams = new URLSearchParams()
  if (view === 'workspace' && workspaceScope === 'datasets') {
    workspaceParams.set('scope', 'datasets')
    const datasetParams = new URLSearchParams(workspaceDatasetQuery)
    for (const key of DATASET_QUERY_KEYS) {
      const value = datasetParams.get(key)
      if (value) workspaceParams.set(key, value)
    }
  } else if (view === 'workspace' && workspaceQuery?.trim()) workspaceParams.set('q', workspaceQuery.trim())
  const transformParams = new URLSearchParams(transformQuery)
  if (view === 'transforms' && transformVersion) transformParams.set('version', transformVersion)
  if (view === 'transforms' && transformCanvasId && transformNodeId) {
    transformParams.set('canvas', transformCanvasId)
    transformParams.set('node', transformNodeId)
  }
  const query = view === 'workspace' && workspaceParams.size
    ? `?${workspaceParams}`
    : view === 'jobs' && jobsQuery ? `?${jobsQuery}`
    : view === 'inbox' && inboxQuery ? `?${inboxQuery}`
    : view === 'canvas' && nodeId ? `?${new URLSearchParams({ node: nodeId })}`
    : view === 'transforms' && transformParams.size ? `?${transformParams}` : ''
  return path + query
}

/** A shareable absolute link that opens straight into this canvas. */
export function canvasLink(id: string): string {
  return `${location.origin}${location.pathname}${routeHash('canvas', id)}`
}

// The store shape we need — passed in so this module never imports the store (avoids an import cycle).
interface RouterState { view: DpView; doc: { id: string; nodes: { id: string }[] }; selectedId: string | null; workspaceResourceId: string | null; workspaceSearchQuery: string; workspaceScope: 'all' | 'datasets'; workspaceDatasetQuery: string; jobsQuery: string; inboxQuery: string; transformResourceId: string | null; transformVersion: string | null; transformUpgradeCanvasId: string | null; transformUpgradeNodeId: string | null; transformLibraryQuery: string }
interface RouterStore {
  getState: () => RouterState & { setView: (v: DpView) => void; select: (id: string | null) => void; setWorkspaceResource: (id: string | null) => void; setWorkspaceSearchQuery: (query: string) => void; setWorkspaceScope: (scope: 'all' | 'datasets') => void; setWorkspaceDatasetQuery: (query: string) => void; setJobsQuery: (query: string) => void; setInboxQuery: (query: string) => void; setTransformResource: (id: string | null, version?: string | null, upgrade?: { canvasId: string; nodeId: string } | null) => void; setTransformLibraryQuery: (query: string) => void; openFile: (id: string) => Promise<boolean> }
  subscribe: (fn: (s: RouterState) => void) => void
}

const hashFor = (s: RouterState) =>
  routeHash(s.view, s.view === 'canvas' ? s.doc.id : undefined,
    s.view === 'workspace' ? s.workspaceResourceId ?? undefined : undefined,
    s.view === 'workspace' ? s.workspaceSearchQuery : undefined,
    s.view === 'jobs' ? s.jobsQuery : undefined,
    s.view === 'canvas' ? s.selectedId ?? undefined : undefined,
    s.view === 'inbox' ? s.inboxQuery : undefined,
    s.view === 'workspace' ? s.workspaceScope : undefined,
    s.view === 'workspace' ? s.workspaceDatasetQuery : undefined,
    s.view === 'transforms' ? s.transformResourceId ?? undefined : undefined,
    s.view === 'transforms' ? s.transformVersion ?? undefined : undefined,
    s.view === 'transforms' ? s.transformLibraryQuery : undefined,
    s.view === 'transforms' ? s.transformUpgradeCanvasId ?? undefined : undefined,
    s.view === 'transforms' ? s.transformUpgradeNodeId ?? undefined : undefined)

let _inited = false
/** Wire the store ↔ the URL hash (two-way, loop-guarded). Call once at startup, after bootstrap. */
export function initRouter(store: RouterStore): void {
  if (_inited) return  // idempotent (React StrictMode double-invokes effects in dev)
  _inited = true
  let applying = false
  const apply = async () => {
    const r = parseHash()
    const st = store.getState()
    applying = true  // held across the await so openFile's sets don't trigger the store→hash push
    try {
      if (r.view === 'canvas' && r.canvasId) {
        if (st.doc.id !== r.canvasId) {
          const ok = await st.openFile(r.canvasId)  // may be a shared canvas → authorized server-side
          if (!ok) {
            // bad / revoked / unauthorized link: reflect the ACTUAL (unchanged) state and REPLACE the
            // bad history entry, so Back doesn't return to it and the store→hash sync doesn't bounce.
            history.replaceState(null, '', hashFor(store.getState()))
          }
        } else if (st.view !== 'canvas') st.setView('canvas')
        const current = store.getState()
        const nodeExists = !!r.nodeId && current.doc.id === r.canvasId
          && current.doc.nodes.some((node) => node.id === r.nodeId)
        current.select(nodeExists ? r.nodeId! : null)
        if (r.nodeId && !nodeExists) history.replaceState(null, '', hashFor(store.getState()))
      } else if (r.view === 'workspace' && (st.view !== 'workspace'
          || st.workspaceResourceId !== (r.workspaceResourceId ?? null)
          || st.workspaceScope !== (r.workspaceScope ?? 'all')
          || ((r.workspaceScope ?? 'all') === 'all'
            ? st.workspaceSearchQuery !== (r.workspaceQuery ?? '')
            : st.workspaceDatasetQuery !== (r.workspaceDatasetQuery ?? '')))) {
        st.setWorkspaceResource(r.workspaceResourceId ?? null)
        st.setWorkspaceScope(r.workspaceScope ?? 'all')
        if ((r.workspaceScope ?? 'all') === 'datasets') {
          st.setWorkspaceDatasetQuery(r.workspaceDatasetQuery ?? '')
        } else st.setWorkspaceSearchQuery(r.workspaceQuery ?? '')
      } else if (r.view === 'jobs' && (st.view !== 'jobs' || st.jobsQuery !== (r.jobsQuery ?? ''))) {
        st.setJobsQuery(r.jobsQuery ?? '')
      } else if (r.view === 'inbox' && (st.view !== 'inbox' || st.inboxQuery !== (r.inboxQuery ?? ''))) {
        st.setInboxQuery(r.inboxQuery ?? '')
      } else if (r.view === 'transforms' && (st.view !== 'transforms'
          || st.transformResourceId !== (r.transformId ?? null)
          || st.transformVersion !== (r.transformVersion ?? null)
          || st.transformUpgradeCanvasId !== (r.transformCanvasId ?? null)
          || st.transformUpgradeNodeId !== (r.transformNodeId ?? null)
          || st.transformLibraryQuery !== (r.transformQuery ?? ''))) {
        st.setTransformLibraryQuery(r.transformQuery ?? '')
        st.setTransformResource(
          r.transformId ?? null, r.transformVersion ?? null,
          r.transformCanvasId && r.transformNodeId
            ? { canvasId: r.transformCanvasId, nodeId: r.transformNodeId } : null,
        )
      } else if (st.view !== r.view) {
        st.setView(r.view)
      }
    } finally { applying = false }
  }
  window.addEventListener('hashchange', () => { void apply() })
  // store → hash: only when the view or open canvas actually changes (not on every autosave)
  store.subscribe((s) => {
    if (applying) return
    const want = hashFor(s)
    if (location.hash !== want) {
      // Node focus is a deep-linkable selection inside one canvas, not a new destination. Keep the
      // current history entry shareable without making Back walk through every inspector click.
      const sameCanvas = location.hash.split('?', 1)[0] === want.split('?', 1)[0]
        && want.startsWith('#/canvas/')
      if (sameCanvas) history.replaceState(null, '', want)
      else location.hash = want  // destination/filter changes remain real back/forward entries
    }
  })
  // reflect the state bootstrap just settled into the URL, without adding a history entry
  if (location.hash !== hashFor(store.getState())) history.replaceState(null, '', hashFor(store.getState()))
}
