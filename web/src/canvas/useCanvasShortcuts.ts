import { useEffect } from 'react'
import { getSpec } from '../nodes/registry'
import { roleCanEdit, useStore } from '../store/graph'

/** Canvas commands yield to fields, widgets, and modal surfaces that own keyboard input. */
export function useCanvasShortcuts(
  removeSelected: () => void,
  bypass: (id: string) => void,
  disable: (id: string) => void,
) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // a fullscreen code editor / any open modal sits over the canvas — its own Esc handling wins;
      // don't let Delete/b/d/Esc act on (or wipe) the canvas beneath it
      if (useStore.getState().fullscreenCode) return
      if (document.querySelector('.dp-modal-overlay')) return
      // Native selectors and ARIA widgets own typing and navigation even when an older node
      // remains selected. Respect handlers that already consumed this event, including portals.
      if (e.defaultPrevented || e.isComposing) return
      const target = e.target instanceof Element ? e.target : null
      if ((target instanceof HTMLElement && target.isContentEditable)
          || target?.closest('input, textarea, select, [role="combobox"], [role="listbox"], [role="menu"], [role="dialog"], .nokey')) return
      const editable = roleCanEdit(useStore.getState().canvasRole)
      // undo / redo work regardless of selection
      if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z')) {
        e.preventDefault()
        if (!editable) return
        if (e.shiftKey) useStore.getState().redo()
        else useStore.getState().undo()
        return
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === 'y' || e.key === 'Y')) { e.preventDefault(); if (editable) useStore.getState().redo(); return }
      // clipboard + selection (work on the canvas, not in a field — inputs bailed out above)
      if (e.metaKey || e.ctrlKey) {
        const k = e.key.toLowerCase()
        if (k === 'a') { e.preventDefault(); useStore.getState().selectAll(); return }
        if (k === 'c') { e.preventDefault(); useStore.getState().copySelection(); return }
        if (k === 'x') { e.preventDefault(); if (editable) useStore.getState().cutSelection(); return }
        if (k === 'v') { e.preventDefault(); if (editable) useStore.getState().paste(); return }
        if (k === 'd') { e.preventDefault(); if (editable) useStore.getState().duplicateSelected(); return }
      }
      // Escape closes any open floating panel (data viewer / run / …) and clears the selection
      if (e.key === 'Escape') {
        if (Object.keys(useStore.getState().openPanels).length) useStore.setState({ openPanels: {} })
        else useStore.getState().select(null)
        return
      }
      const ids = useStore.getState().selectedIds
      if (!ids.length) return
      if (!editable) return
      if (e.key === 'Delete' || e.key === 'Backspace') { removeSelected(); e.preventDefault() }
      if (e.key === 'b' || e.key === 'B') {
        // honor canBypass (matches the ⋯ menu) — bypass only the selected nodes that allow it
        ids.forEach((id) => {
          const n = useStore.getState().doc.nodes.find((x) => x.id === id)
          if (n && getSpec(n.type)?.canBypass) bypass(id)
        })
      }
      if (e.key === 'd' || e.key === 'D') ids.forEach((id) => disable(id))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [removeSelected, bypass, disable])

}
