import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { color, radius, shadow } from '../theme/tokens'

// A popover rendered in a portal on document.body, positioned relative to an anchor. This is
// how in-node menus (table picker, processor picker, ⋯ menu) escape the node's clipping and
// stacking context — they are never cut off or hidden behind another node.
export function Popover({
  anchorRef, open, onClose, children, width, align = 'left', placement = 'bottom', maxHeight = 300,
}: {
  anchorRef: RefObject<HTMLElement>
  open: boolean
  onClose: () => void
  children: ReactNode
  width?: number
  align?: 'left' | 'right'
  placement?: 'bottom' | 'top'
  maxHeight?: number
}) {
  const popRef = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number; width: number } | null>(null)

  useLayoutEffect(() => {
    if (!open || !anchorRef.current) return
    const update = () => {
      const el = anchorRef.current
      if (!el) return
      const r = el.getBoundingClientRect()
      const w = width ?? r.width
      let left = align === 'right' ? r.right - w : r.left
      left = Math.max(8, Math.min(left, window.innerWidth - w - 8))
      const top = placement === 'bottom' ? r.bottom + 6 : Math.max(8, r.top - 6 - maxHeight)
      setPos({ left, top, width: w })
    }
    update()
    // reposition while open (window resize); canvas pan/zoom closes via outside-mousedown
    window.addEventListener('resize', update)
    return () => window.removeEventListener('resize', update)
  }, [open, anchorRef, width, align, placement, maxHeight])

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (popRef.current?.contains(t) || anchorRef.current?.contains(t)) return
      onClose()
    }
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    // canvas pan/zoom (wheel) moves the anchor but not this fixed portal — close instead of detaching
    const onWheel = () => onClose()
    const id = setTimeout(() => window.addEventListener('mousedown', onDown), 0)
    window.addEventListener('keydown', onEsc)
    window.addEventListener('wheel', onWheel, { passive: true })
    return () => {
      clearTimeout(id)
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onEsc)
      window.removeEventListener('wheel', onWheel)
    }
  }, [open, onClose, anchorRef])

  if (!open || !pos) return null

  return createPortal(
    <div
      ref={popRef}
      className="dp-panel"
      onMouseDown={(e) => e.stopPropagation()}
      style={{
        position: 'fixed', left: pos.left, top: pos.top, width: pos.width, zIndex: 1000,
        background: '#fff', border: `1px solid ${color.border}`, borderRadius: radius.panel,
        boxShadow: shadow.panel, padding: 5, maxHeight, overflowY: 'auto',
      }}
    >
      {children}
    </div>,
    document.body,
  )
}
