import { useEffect, useState } from 'react'
import {
  type DocumentTitlePhase,
  projectDocumentTitle,
} from './documentTitle'
import { parseHash } from './router'
import { useStore } from './store/graph'

function routeCanvasClaim(): string | null {
  const route = parseHash()
  return route.view === 'canvas' && route.canvasId ? route.canvasId : null
}

/**
 * Project `document.title` from committed store state and the current hash Canvas claim.
 * Mount once at the App root so auth / bootstrap early returns still clear a stale Canvas name.
 */
export function useDocumentTitle(phase: DocumentTitlePhase): void {
  const view = useStore((s) => s.view)
  const canvasId = useStore((s) => s.doc.id)
  const canvasName = useStore((s) => s.doc.name)
  const relationshipsMode = useStore((s) => s.erMode)
  const [routeCanvasId, setRouteCanvasId] = useState(routeCanvasClaim)

  useEffect(() => {
    const syncRoute = () => setRouteCanvasId(routeCanvasClaim())
    // pushState (store→hash) does not emit hashchange; re-read whenever committed identity moves.
    syncRoute()
    window.addEventListener('hashchange', syncRoute)
    window.addEventListener('popstate', syncRoute)
    return () => {
      window.removeEventListener('hashchange', syncRoute)
      window.removeEventListener('popstate', syncRoute)
    }
  }, [view, canvasId])

  useEffect(() => {
    document.title = projectDocumentTitle({
      phase,
      view,
      canvasId,
      canvasName,
      routeCanvasId,
      relationshipsMode,
    })
  }, [phase, view, canvasId, canvasName, routeCanvasId, relationshipsMode])
}
