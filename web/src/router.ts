// Minimal hash router — no dependency, works offline. The URL reflects the app's view + open canvas
// so the browser back/forward buttons work, a refresh restores where you were, and Share can produce
// a link that opens straight into a specific canvas (#/canvas/<id>).
import type { DpView } from './store/graph'
import { ownsNavigation, startNavigation, type NavigationToken } from './navigationOwnership'

export interface Route { view: DpView; canvasId?: string; nodeId?: string; workspaceResourceId?: string; workspaceQuery?: string; workspaceScope?: 'all' | 'datasets'; workspaceDatasetQuery?: string; jobsQuery?: string; inboxQuery?: string; transformId?: string; transformVersion?: string; transformCanvasId?: string; transformNodeId?: string; transformQuery?: string }

const DATASET_QUERY_KEYS = ['dq', 'folder', 'tags', 'owner', 'columns', 'sort', 'order', 'match', 'revision', 'revisionDataset'] as const

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
  // Distribution reports are a Jobs detail, not a second navigation system. Preserve the exact
  // report identity in the Jobs route so browser reopen/back follows the same authorized surface.
  if (seg === 'distribution-reports') {
    const report = id ? decodeURIComponent(id) : params.get('report')
    if (report) {
      const query = new URLSearchParams({ report })
      const compare = params.get('compare')
      if (compare) query.set('compare', compare)
      return { view: 'jobs', jobsQuery: query.toString() }
    }
  }
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
  getState: () => RouterState & { applyRoute: (route: Route, navigationToken: NavigationToken) => void; select: (id: string | null) => void; requestNodeReveal: (canvasId: string, nodeId: string) => void; clearNodeReveal: () => void; pushToast: (message: string, kind?: 'info' | 'error') => void; openFile: (id: string, options?: { navigationToken?: NavigationToken }) => Promise<boolean> }
  subscribe: (fn: (s: RouterState) => void) => () => void
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

export interface RouterController {
  settleBootstrap: (navigationToken: NavigationToken) => void
}

let _router: RouterController | null = null
let _resetForTests: (() => void) | null = null

export function resetRouterForTests(): void {
  _resetForTests?.()
  _resetForTests = null
  _router = null
}

/** Wire the store ↔ the URL hash before bootstrap; bootstrap settles its own initial route token. */
export function initRouter(store: RouterStore, bootstrapToken?: NavigationToken): RouterController {
  if (_router) return _router  // idempotent (React StrictMode double-invokes effects in dev)
  let applyingToken: NavigationToken | null = bootstrapToken ?? null
  const apply = async () => {
    const navigationToken = startNavigation()
    const r = parseHash()
    const st = store.getState()
    applyingToken = navigationToken
    try {
      // A reveal belongs to one explicit node= route only. Leaving the Canvas or returning through a
      // bare Canvas URL invalidates any request that has not yet been consumed by React Flow.
      if (r.view !== 'canvas' || !r.nodeId) st.clearNodeReveal()
      if (r.view === 'canvas' && r.canvasId) {
        if (st.doc.id !== r.canvasId) {
          const ok = await st.openFile(r.canvasId, { navigationToken })  // may be shared; server authorizes
          if (!ownsNavigation(navigationToken)) return
          if (!ok) {
            // bad / revoked / unauthorized link: reflect the ACTUAL (unchanged) state and REPLACE the
            // bad history entry, so Back doesn't return to it and the store→hash sync doesn't bounce.
            history.replaceState(null, '', hashFor(store.getState()))
            return
          }
        } else if (st.view !== 'canvas') st.applyRoute({ view: 'canvas' }, navigationToken)
        if (!ownsNavigation(navigationToken)) return
        const current = store.getState()
        const nodeExists = !!r.nodeId && current.doc.id === r.canvasId
          && current.doc.nodes.some((node) => node.id === r.nodeId)
        current.select(nodeExists ? r.nodeId! : null)
        if (nodeExists) current.requestNodeReveal(r.canvasId, r.nodeId!)
        else if (r.nodeId) {
          current.clearNodeReveal()
          current.pushToast('The requested node is no longer in this Canvas.', 'info')
          if (ownsNavigation(navigationToken)) history.replaceState(null, '', hashFor(store.getState()))
        }
      } else if (ownsNavigation(navigationToken)) st.applyRoute(r, navigationToken)
    } finally {
      if (applyingToken === navigationToken) applyingToken = null
    }
  }
  let applyQueued = false
  const requestApply = () => {
    if (applyQueued) return
    applyQueued = true
    queueMicrotask(() => { applyQueued = false; void apply() })
  }
  window.addEventListener('hashchange', requestApply)
  window.addEventListener('popstate', requestApply)
  // store → hash: only when the view or open canvas actually changes (not on every autosave)
  const unsubscribe = store.subscribe((s) => {
    // A pending route token suppresses only its own writes. A user action claims a newer token,
    // immediately re-enabling publication while the older Canvas request is still awaiting.
    if (applyingToken !== null && ownsNavigation(applyingToken)) return
    const want = hashFor(s)
    if (location.hash !== want) {
      // Node focus is a deep-linkable selection inside one canvas, not a new destination. Keep the
      // current history entry shareable without making Back walk through every inspector click.
      const sameCanvas = location.hash.split('?', 1)[0] === want.split('?', 1)[0]
        && want.startsWith('#/canvas/')
      if (sameCanvas) history.replaceState(null, '', want)
      // State-owned navigation must not emit hashchange and make the router claim a competing token.
      // pushState preserves Back/Forward; popstate above is the router's history entrypoint.
      else history.pushState(null, '', want)
    }
  })
  _router = {
    settleBootstrap: (navigationToken) => {
      if (applyingToken === navigationToken) applyingToken = null
      if (ownsNavigation(navigationToken)) {
        if (location.hash !== hashFor(store.getState())) history.replaceState(null, '', hashFor(store.getState()))
      }
    },
  }
  _resetForTests = () => {
    window.removeEventListener('hashchange', requestApply)
    window.removeEventListener('popstate', requestApply)
    unsubscribe()
  }
  return _router
}
