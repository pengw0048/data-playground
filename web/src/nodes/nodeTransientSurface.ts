import { useEffect, useRef } from 'react'

const NODE_TRANSIENT_SURFACE_OPEN = 'dataplay:node-transient-surface-open'

// Node cards own their local open state, but their short-lived actionable surfaces must not stack.
// A DOM event keeps that boundary local to node UI without adding canvas/store state.
export function useNodeTransientSurface(id: string, open: boolean, close: () => void) {
  const closeRef = useRef(close)
  closeRef.current = close

  useEffect(() => {
    const onOpen = (event: Event) => {
      if ((event as CustomEvent<string>).detail !== id) closeRef.current()
    }
    window.addEventListener(NODE_TRANSIENT_SURFACE_OPEN, onOpen)
    return () => window.removeEventListener(NODE_TRANSIENT_SURFACE_OPEN, onOpen)
  }, [id])

  useEffect(() => {
    if (!open) return
    window.dispatchEvent(new CustomEvent<string>(NODE_TRANSIENT_SURFACE_OPEN, { detail: id }))
  }, [id, open])
}
