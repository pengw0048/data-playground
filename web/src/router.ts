// Minimal hash router — no dependency, works offline. The URL reflects the app's view + open canvas
// so the browser back/forward buttons work, a refresh restores where you were, and Share can produce
// a link that opens straight into a specific canvas (#/canvas/<id>).
import type { DpView } from './store/graph'

export interface Route { view: DpView; canvasId?: string }

export function parseHash(): Route {
  const h = location.hash.replace(/^#\/?/, '')
  const [seg, id] = h.split('/')
  if (seg === 'canvas' && id) return { view: 'canvas', canvasId: decodeURIComponent(id) }
  if (seg === 'files' || seg === 'tables' || seg === 'transforms') return { view: seg }
  // bare "/" opens the editor on the last/newest canvas (bootstrap picks the id) — matches the app's
  // default landing; the files home is reachable via #/files.
  return { view: 'canvas' }
}

export function routeHash(view: DpView, canvasId?: string): string {
  return view === 'canvas' && canvasId ? `#/canvas/${encodeURIComponent(canvasId)}` : `#/${view}`
}

/** A shareable absolute link that opens straight into this canvas. */
export function canvasLink(id: string): string {
  return `${location.origin}${location.pathname}${routeHash('canvas', id)}`
}

// The store shape we need — passed in so this module never imports the store (avoids an import cycle).
interface RouterStore {
  getState: () => { view: DpView; doc: { id: string }; setView: (v: DpView) => void; openFile: (id: string) => Promise<void> }
  subscribe: (fn: (s: { view: DpView; doc: { id: string } }) => void) => void
}

/** Wire the store ↔ the URL hash (two-way, loop-guarded). Call once at startup, after bootstrap. */
export function initRouter(store: RouterStore): void {
  let applying = false
  const apply = () => {
    const r = parseHash()
    const st = store.getState()
    applying = true
    try {
      if (r.view === 'canvas' && r.canvasId) {
        if (st.doc.id !== r.canvasId) void st.openFile(r.canvasId)  // may be a shared canvas → authorized server-side
        else if (st.view !== 'canvas') st.setView('canvas')
      } else if (st.view !== r.view) {
        st.setView(r.view)
      }
    } finally { applying = false }
  }
  window.addEventListener('hashchange', apply)
  // store → hash: only when the view or open canvas actually changes (not on every autosave)
  store.subscribe((s) => {
    if (applying) return
    const want = routeHash(s.view, s.view === 'canvas' ? s.doc.id : undefined)
    if (location.hash !== want) location.hash = want  // pushes a history entry → back/forward works
  })
  // reflect the state bootstrap just settled into the URL, without adding a history entry
  const st0 = store.getState()
  const want0 = routeHash(st0.view, st0.view === 'canvas' ? st0.doc.id : undefined)
  if (location.hash !== want0) history.replaceState(null, '', want0)
}
