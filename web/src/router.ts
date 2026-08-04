// Minimal hash router — no dependency, works offline. The URL reflects the app's view + open canvas
// so the browser back/forward buttons work, a refresh restores where you were, and Share can produce
// a link that opens straight into a specific canvas (#/canvas/<id>).
import type { DpView } from './store/graph'
import { ownsNavigation, startNavigation, type NavigationToken } from './navigationOwnership'
import {
  extractWorkspaceBrowseQuery,
  WORKSPACE_BROWSE_QUERY_KEYS,
} from './workspaceBrowseQuery'

export type RelationshipsMode = 'joins' | 'lineage'

export interface RelationshipsReturn {
  resourceId: string
  scope: 'all' | 'datasets'
  workspaceQuery?: string
  datasetQuery?: string
  browseQuery?: string
}

export interface RelationshipsContext {
  focusDatasetId?: string
  mode?: RelationshipsMode
  returnTo?: RelationshipsReturn
}

export interface Route {
  view: DpView
  canvasId?: string
  nodeId?: string
  workspaceResourceId?: string
  workspaceQuery?: string
  workspaceScope?: 'all' | 'datasets'
  workspaceDatasetQuery?: string
  workspaceBrowseQuery?: string
  jobsQuery?: string
  inboxQuery?: string
  transformId?: string
  transformVersion?: string
  transformCanvasId?: string
  transformNodeId?: string
  transformQuery?: string
  relationshipsContext?: RelationshipsContext
  canonicalHash?: string
}

const DATASET_QUERY_KEYS = [
  'dq', 'folder', 'tags', 'owner', 'columns', 'sort', 'order', 'match',
  'revision', 'revisionDataset', 'returnCanvas', 'returnNode', 'returnView', 'returnQuery',
] as const
const DATASET_VIEWER_QUERY_KEYS = [
  'revision', 'revisionDataset', 'returnCanvas', 'returnNode', 'returnView', 'returnQuery',
] as const
const DATASET_VIEWER_RETURN_QUERY_KEYS = {
  jobs: ['scope', 'status', 'canvas', 'node', 'backend', 'after', 'before', 'q', 'run', 'output', 'report', 'compare'],
  inbox: ['filter'],
} as const

export interface DatasetViewerCanvasReturn {
  canvasId: string
  nodeId?: string
}

export interface DatasetViewerActivityReturn {
  view: 'jobs' | 'inbox'
  query?: string
}

export type DatasetViewerReturn = DatasetViewerCanvasReturn | DatasetViewerActivityReturn
export type ParsedDatasetViewerReturn =
  | ({ view: 'canvas' } & DatasetViewerCanvasReturn)
  | DatasetViewerActivityReturn

function activityReturnQuery(view: 'jobs' | 'inbox', query?: string): string | undefined {
  const source = new URLSearchParams(query)
  const safe = new URLSearchParams()
  for (const key of DATASET_VIEWER_RETURN_QUERY_KEYS[view]) {
    const value = source.get(key)
    if (value) safe.set(key, value)
  }
  return safe.toString() || undefined
}

function datasetRouteQuery(query: string | undefined, scope: 'all' | 'datasets'): string | undefined {
  const source = new URLSearchParams(query)
  const safe = new URLSearchParams()
  for (const key of scope === 'datasets' ? DATASET_QUERY_KEYS : DATASET_VIEWER_QUERY_KEYS) {
    const value = source.get(key)
    if (value) safe.set(key, value)
  }
  return safe.toString() || undefined
}

function browseParamsNeedCanonicalization(params: URLSearchParams): boolean {
  const raw = new URLSearchParams()
  for (const key of WORKSPACE_BROWSE_QUERY_KEYS) {
    const values = params.getAll(key)
    if (values.length > 1) return true
    if (values[0] != null && values[0] !== '') raw.set(key, values[0])
  }
  if (![...raw.keys()].length) return false
  const normalized = extractWorkspaceBrowseQuery(raw)
  const norm = new URLSearchParams(normalized)
  if ([...raw.keys()].length !== [...norm.keys()].length) return true
  for (const key of raw.keys()) {
    if (raw.get(key) !== norm.get(key)) return true
  }
  return false
}

function mergeBrowseParams(target: URLSearchParams, browseQuery: string | undefined): void {
  if (!browseQuery) return
  const browse = new URLSearchParams(browseQuery)
  for (const key of WORKSPACE_BROWSE_QUERY_KEYS) {
    const value = browse.get(key)
    if (value) target.set(key, value)
  }
}

export function parseDatasetViewerReturn(query: string): ParsedDatasetViewerReturn | undefined {
  const params = new URLSearchParams(query)
  const canvasId = params.get('returnCanvas') || undefined
  if (canvasId) {
    return { view: 'canvas', canvasId, nodeId: params.get('returnNode') || undefined }
  }
  const view = params.get('returnView')
  if (view !== 'jobs' && view !== 'inbox') return undefined
  return { view, query: activityReturnQuery(view, params.get('returnQuery') || undefined) }
}

function decodeRouteSegment(value: string | undefined): string | undefined {
  if (!value) return undefined
  try {
    return decodeURIComponent(value)
  } catch {
    // A malformed bookmarked hash is not an application failure. Treat its dynamic identifier as
    // absent so routing remains on a safe shell/default surface rather than escaping render.
    return undefined
  }
}

export function parseHash(): Route {
  const h = location.hash.replace(/^#\/?/, '')
  const [path, rawQuery = ''] = h.split('?', 2)
  const [seg, id] = path.split('/')
  const params = new URLSearchParams(rawQuery)
  const workspaceQuery = params.get('q')?.trim() || undefined
  const decodedId = decodeRouteSegment(id)
  if (seg === 'canvas' && decodedId) return {
    view: 'canvas', canvasId: decodedId,
    nodeId: params.get('node') || undefined,
  }
  if (seg === 'workspace') {
    if (id !== undefined && !decodedId) return { view: 'workspace', canonicalHash: '#/workspace' }
    const workspaceScope = params.get('scope') === 'datasets' ? 'datasets' : 'all'
    const datasetParams = new URLSearchParams()
    for (const key of workspaceScope === 'datasets' ? DATASET_QUERY_KEYS : DATASET_VIEWER_QUERY_KEYS) {
      const value = params.get(key)
      if (value) datasetParams.set(key, value)
    }
    const workspaceDatasetQuery = datasetParams.toString() || undefined
    const workspaceBrowseQuery = workspaceScope === 'all'
      ? extractWorkspaceBrowseQuery(params) || undefined
      : undefined
    const route: Route = {
      view: 'workspace',
      workspaceResourceId: decodedId,
      ...(workspaceScope === 'datasets' ? { workspaceScope } : {}),
      ...(workspaceDatasetQuery ? { workspaceDatasetQuery } : {}),
      ...(workspaceBrowseQuery ? { workspaceBrowseQuery } : {}),
      ...(workspaceScope === 'all' && workspaceQuery ? { workspaceQuery } : {}),
    }
    if (workspaceScope === 'all' && browseParamsNeedCanonicalization(params)) {
      route.canonicalHash = routeHash(
        'workspace', undefined, decodedId, workspaceQuery, undefined, undefined, undefined,
        undefined, workspaceDatasetQuery, undefined, undefined, undefined, undefined, undefined,
        workspaceBrowseQuery,
      )
    }
    return route
  }
  // Recents and Tables are intentionally redirected to the single local Workspace explorer.
  if (seg === 'files' || seg === 'tables') return { view: 'workspace' }
  // Distribution reports are a Jobs detail, not a second navigation system. Preserve the exact
  // report identity in the Jobs route so browser reopen/back follows the same authorized surface.
  if (seg === 'distribution-reports') {
    const report = decodedId ?? params.get('report')
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
    if (id !== undefined && !decodedId) return { view: 'transforms', canonicalHash: '#/transforms' }
    const transformVersion = params.get('version') || undefined
    const transformCanvasId = params.get('canvas') || undefined
    const transformNodeId = params.get('node') || undefined
    params.delete('version')
    params.delete('canvas')
    params.delete('node')
    return {
      view: 'transforms',
      ...(decodedId ? { transformId: decodedId } : {}),
      ...(transformVersion ? { transformVersion } : {}),
      ...(transformCanvasId && transformNodeId ? { transformCanvasId, transformNodeId } : {}),
      ...(params.size ? { transformQuery: params.toString() } : {}),
    }
  }
  if (seg === 'relationships') {
    const focusDatasetId = params.get('focus') || undefined
    const mode = params.get('mode')
    const resourceId = params.get('returnResource') || undefined
    const scope = params.get('returnScope') === 'datasets' ? 'datasets' : 'all'
    const datasetQuery = datasetRouteQuery(params.get('returnQuery') || undefined, scope)
    const browseQuery = scope === 'all'
      ? extractWorkspaceBrowseQuery(params.get('returnBrowse') || undefined) || undefined
      : undefined
    const returnTo = resourceId ? {
      resourceId,
      scope,
      ...(scope === 'all' && params.get('returnQ')?.trim()
        ? { workspaceQuery: params.get('returnQ')!.trim() } : {}),
      ...(datasetQuery ? { datasetQuery } : {}),
      ...(browseQuery ? { browseQuery } : {}),
    } satisfies RelationshipsReturn : undefined
    const context: RelationshipsContext = {
      ...(focusDatasetId ? { focusDatasetId } : {}),
      ...(mode === 'lineage' || mode === 'joins' ? { mode } : {}),
      ...(returnTo ? { returnTo } : {}),
    }
    return { view: seg, ...(Object.keys(context).length ? { relationshipsContext: context } : {}) }
  }
  // bare "/" opens the editor on the last/newest canvas (bootstrap picks the id).
  if (path === '' && !rawQuery) return { view: 'canvas' }
  // An unrecognized route must never borrow the last Canvas and make an invalid URL look editable.
  // Recover to a truthful shell destination and replace the bad history entry in initRouter.
  return { view: 'workspace', canonicalHash: '#/workspace' }
}

export function routeHash(view: DpView, canvasId?: string, workspaceResourceId?: string, workspaceQuery?: string, jobsQuery?: string, nodeId?: string, inboxQuery?: string, workspaceScope?: 'all' | 'datasets', workspaceDatasetQuery?: string, transformId?: string, transformVersion?: string, transformQuery?: string, transformCanvasId?: string, transformNodeId?: string, workspaceBrowseQuery?: string): string {
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
  } else if (view === 'workspace') {
    if (workspaceQuery?.trim()) workspaceParams.set('q', workspaceQuery.trim())
    const datasetParams = new URLSearchParams(workspaceDatasetQuery)
    for (const key of DATASET_VIEWER_QUERY_KEYS) {
      const value = datasetParams.get(key)
      if (value) workspaceParams.set(key, value)
    }
    mergeBrowseParams(workspaceParams, workspaceBrowseQuery)
  }
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

export function relationshipsHash(context?: RelationshipsContext): string {
  const params = new URLSearchParams()
  if (context?.focusDatasetId) params.set('focus', context.focusDatasetId)
  if (context?.mode && (context.focusDatasetId || context.mode !== 'joins')) {
    params.set('mode', context.mode)
  }
  if (context?.returnTo?.resourceId) {
    const { returnTo } = context
    params.set('returnResource', returnTo.resourceId)
    params.set('returnScope', returnTo.scope)
    if (returnTo.scope === 'all' && returnTo.workspaceQuery?.trim()) {
      params.set('returnQ', returnTo.workspaceQuery.trim())
    }
    const query = datasetRouteQuery(returnTo.datasetQuery, returnTo.scope)
    if (query) params.set('returnQuery', query)
    if (returnTo.scope === 'all' && returnTo.browseQuery) {
      const browse = extractWorkspaceBrowseQuery(returnTo.browseQuery)
      if (browse) params.set('returnBrowse', browse)
    }
  }
  return `#/relationships${params.size ? `?${params}` : ''}`
}

/** One canonical route for opening either the latest dataset or one immutable revision. */
export function datasetViewerHash(
  datasetId: string,
  revisionId?: string,
  returnTo?: DatasetViewerReturn,
  workspaceResourceId?: string,
): string {
  const params = new URLSearchParams()
  if (revisionId) {
    params.set('revision', revisionId)
    params.set('revisionDataset', datasetId)
  }
  if (returnTo && 'canvasId' in returnTo) {
    params.set('returnCanvas', returnTo.canvasId)
    if (returnTo.nodeId) params.set('returnNode', returnTo.nodeId)
  } else if (returnTo) {
    params.set('returnView', returnTo.view)
    const query = activityReturnQuery(returnTo.view, returnTo.query)
    if (query) params.set('returnQuery', query)
  }
  const datasetQuery = params.size ? params.toString() : undefined
  return routeHash(
    'workspace', undefined, workspaceResourceId ?? `dataset:${datasetId}`, undefined, undefined, undefined,
    undefined, 'all', datasetQuery,
  )
}

/** A shareable absolute link that opens straight into this canvas. */
export function canvasLink(id: string): string {
  return `${location.origin}${location.pathname}${routeHash('canvas', id)}`
}

// The store shape we need — passed in so this module never imports the store (avoids an import cycle).
interface RouterState { view: DpView; doc: { id: string; nodes: { id: string }[] }; selectedId: string | null; workspaceResourceId: string | null; workspaceSearchQuery: string; workspaceScope: 'all' | 'datasets'; workspaceDatasetQuery: string; workspaceBrowseQuery: string; jobsQuery: string; inboxQuery: string; transformResourceId: string | null; transformVersion: string | null; transformUpgradeCanvasId: string | null; transformUpgradeNodeId: string | null; transformLibraryQuery: string; erFocusDatasetId?: string | null; erMode?: RelationshipsMode; erReturn?: RelationshipsReturn | null }
interface RouterStore {
  getState: () => RouterState & { applyRoute: (route: Route, navigationToken: NavigationToken) => void; select: (id: string | null) => void; requestNodeReveal: (canvasId: string, nodeId: string) => void; clearNodeReveal: () => void; requestViewportFit: () => void; pushToast: (message: string, kind?: 'info' | 'error') => void; openFile: (id: string, options?: { navigationToken?: NavigationToken; skipViewportFit?: boolean }) => Promise<boolean> }
  subscribe: (fn: (s: RouterState) => void) => () => void
}

const hashFor = (s: RouterState) => s.view === 'relationships'
  ? relationshipsHash({
      focusDatasetId: s.erFocusDatasetId ?? undefined,
      mode: s.erMode ?? 'joins',
      returnTo: s.erReturn ?? undefined,
    })
  : routeHash(s.view, s.view === 'canvas' ? s.doc.id : undefined,
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
    s.view === 'transforms' ? s.transformUpgradeNodeId ?? undefined : undefined,
    s.view === 'workspace' ? s.workspaceBrowseQuery || undefined : undefined)

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
          // A node deep link has a stronger initial-view contract than a normal Canvas open.
          // Prevent the ordinary one-shot overview from briefly winning before the target is centered.
          const ok = await st.openFile(r.canvasId, { navigationToken, skipViewportFit: !!r.nodeId })  // may be shared; server authorizes
          if (!ownsNavigation(navigationToken)) return
          if (!ok) {
            // bad / revoked / unauthorized link: reflect the ACTUAL (unchanged) state and REPLACE the
            // bad history entry, so Back doesn't return to it and the store→hash sync doesn't bounce.
            history.replaceState(null, '', hashFor(store.getState()))
            return
          }
        } else if (st.view !== 'canvas') {
          st.applyRoute({ view: 'canvas' }, navigationToken)
          // Jobs/Inbox links may return to the already-loaded document, so no openFile call occurs.
          // Give a normal Canvas route the same one-shot initial overview as a fresh saved-Canvas open.
          if (!r.nodeId && st.doc.nodes.length > 0) st.requestViewportFit()
        }
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
      } else if (ownsNavigation(navigationToken)) {
        if (r.canonicalHash && location.hash !== r.canonicalHash) {
          history.replaceState(null, '', r.canonicalHash)
        }
        st.applyRoute(r, navigationToken)
      }
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
      const samePath = location.hash.split('?', 1)[0] === want.split('?', 1)[0]
      const replaceRouteState = (samePath && want.startsWith('#/canvas/'))
        || (samePath && want.startsWith('#/relationships'))
      if (replaceRouteState) history.replaceState(null, '', want)
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
