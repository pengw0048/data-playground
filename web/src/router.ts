// Minimal hash router — no dependency, works offline. The URL reflects the app's view + open canvas
// so the browser back/forward buttons work, a refresh restores where you were, and Share can produce
// a link that opens straight into a specific canvas (#/canvas/<id>).
import type { DpView } from './store/graph'

export interface Route { view: DpView; canvasId?: string; workspaceResourceId?: string }

export function parseHash(): Route {
  const h = location.hash.replace(/^#\/?/, '')
  const [seg, id] = h.split('/')
  if (seg === 'canvas' && id) return { view: 'canvas', canvasId: decodeURIComponent(id) }
  if (seg === 'workspace') return { view: 'workspace', workspaceResourceId: id ? decodeURIComponent(id) : undefined }
  // Recents and Tables are intentionally redirected to the single local Workspace explorer.
  if (seg === 'files' || seg === 'tables') return { view: 'workspace' }
  if (seg === 'transforms' || seg === 'relationships') return { view: seg }
  // bare "/" opens the editor on the last/newest canvas (bootstrap picks the id).
  return { view: 'canvas' }
}

export function routeHash(view: DpView, canvasId?: string, workspaceResourceId?: string): string {
  return view === 'canvas' && canvasId ? `#/canvas/${encodeURIComponent(canvasId)}` : `#/${view}`
    + (view === 'workspace' && workspaceResourceId ? `/${encodeURIComponent(workspaceResourceId)}` : '')
}

/** A shareable absolute link that opens straight into this canvas. */
export function canvasLink(id: string): string {
  return `${location.origin}${location.pathname}${routeHash('canvas', id)}`
}

// The store shape we need — passed in so this module never imports the store (avoids an import cycle).
interface RouterStore {
  getState: () => { view: DpView; doc: { id: string }; workspaceResourceId: string | null; setView: (v: DpView) => void; setWorkspaceResource: (id: string | null) => void; openFile: (id: string) => Promise<boolean> }
  subscribe: (fn: (s: { view: DpView; doc: { id: string }; workspaceResourceId: string | null }) => void) => void
}

const hashFor = (s: { view: DpView; doc: { id: string }; workspaceResourceId: string | null }) =>
  routeHash(s.view, s.view === 'canvas' ? s.doc.id : undefined, s.view === 'workspace' ? s.workspaceResourceId ?? undefined : undefined)

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
      } else if (r.view === 'workspace' && (st.view !== 'workspace' || st.workspaceResourceId !== (r.workspaceResourceId ?? null))) {
        st.setWorkspaceResource(r.workspaceResourceId ?? null)
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
    if (location.hash !== want) location.hash = want  // pushes a history entry → back/forward works
  })
  // reflect the state bootstrap just settled into the URL, without adding a history entry
  if (location.hash !== hashFor(store.getState())) history.replaceState(null, '', hashFor(store.getState()))
}
