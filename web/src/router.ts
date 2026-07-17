// Minimal hash router — no dependency, works offline. The URL reflects the app's view + open canvas
// so the browser back/forward buttons work, a refresh restores where you were, and Share can produce
// a link that opens straight into a specific canvas (#/canvas/<id>).
import type { DpView } from './store/graph'

export interface Route { view: DpView; canvasId?: string; nodeId?: string; workspaceResourceId?: string; workspaceQuery?: string; jobsQuery?: string }

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
  if (seg === 'workspace') return {
    view: 'workspace',
    workspaceResourceId: id ? decodeURIComponent(id) : undefined,
    ...(workspaceQuery ? { workspaceQuery } : {}),
  }
  // Recents and Tables are intentionally redirected to the single local Workspace explorer.
  if (seg === 'files' || seg === 'tables') return { view: 'workspace' }
  if (seg === 'jobs') return { view: 'jobs', jobsQuery: params.toString() }
  if (seg === 'transforms' || seg === 'relationships') return { view: seg }
  // bare "/" opens the editor on the last/newest canvas (bootstrap picks the id).
  return { view: 'canvas' }
}

export function routeHash(view: DpView, canvasId?: string, workspaceResourceId?: string, workspaceQuery?: string, jobsQuery?: string, nodeId?: string): string {
  const path = view === 'canvas' && canvasId ? `#/canvas/${encodeURIComponent(canvasId)}` : `#/${view}`
    + (view === 'workspace' && workspaceResourceId ? `/${encodeURIComponent(workspaceResourceId)}` : '')
  const query = view === 'workspace' && workspaceQuery?.trim()
    ? `?${new URLSearchParams({ q: workspaceQuery.trim() })}`
    : view === 'jobs' && jobsQuery ? `?${jobsQuery}`
    : view === 'canvas' && nodeId ? `?${new URLSearchParams({ node: nodeId })}` : ''
  return path + query
}

/** A shareable absolute link that opens straight into this canvas. */
export function canvasLink(id: string): string {
  return `${location.origin}${location.pathname}${routeHash('canvas', id)}`
}

// The store shape we need — passed in so this module never imports the store (avoids an import cycle).
interface RouterState { view: DpView; doc: { id: string; nodes: { id: string }[] }; selectedId: string | null; workspaceResourceId: string | null; workspaceSearchQuery: string; jobsQuery: string }
interface RouterStore {
  getState: () => RouterState & { setView: (v: DpView) => void; select: (id: string | null) => void; setWorkspaceResource: (id: string | null) => void; setWorkspaceSearchQuery: (query: string) => void; setJobsQuery: (query: string) => void; openFile: (id: string) => Promise<boolean> }
  subscribe: (fn: (s: RouterState) => void) => void
}

const hashFor = (s: RouterState) =>
  routeHash(s.view, s.view === 'canvas' ? s.doc.id : undefined,
    s.view === 'workspace' ? s.workspaceResourceId ?? undefined : undefined,
    s.view === 'workspace' ? s.workspaceSearchQuery : undefined,
    s.view === 'jobs' ? s.jobsQuery : undefined,
    s.view === 'canvas' ? s.selectedId ?? undefined : undefined)

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
          || st.workspaceSearchQuery !== (r.workspaceQuery ?? ''))) {
        st.setWorkspaceResource(r.workspaceResourceId ?? null)
        st.setWorkspaceSearchQuery(r.workspaceQuery ?? '')
      } else if (r.view === 'jobs' && (st.view !== 'jobs' || st.jobsQuery !== (r.jobsQuery ?? ''))) {
        st.setJobsQuery(r.jobsQuery ?? '')
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
