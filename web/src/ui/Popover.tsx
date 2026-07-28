import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'

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
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number; width: number; maxHeight: number } | null>(null)

  useLayoutEffect(() => {
    if (!open || !anchorRef.current) return
    const update = () => {
      const el = anchorRef.current
      if (!el) return
      const r = el.getBoundingClientRect()
      const w = width ?? r.width
      let left = align === 'right' ? r.right - w : r.left
      left = Math.max(8, Math.min(left, window.innerWidth - w - 8))
      // Canvas' fixed toolbar is an interaction boundary just like the viewport edge. Prefer the
      // opposite side when a bottom popover would run into it; outside Canvas there is no toolbar.
      const toolbarTop = document.querySelector('[data-testid="toolbar"]')?.getBoundingClientRect().top
      const bottomBoundary = toolbarTop == null ? window.innerHeight : Math.min(window.innerHeight, toolbarTop - 8)
      const bottomSpace = bottomBoundary - r.bottom - 14
      const topSpace = r.top - 14
      const resolvedPlacement = placement === 'bottom' && bottomSpace < Math.min(maxHeight, 160) && topSpace > bottomSpace
        ? 'top'
        : placement
      const availableHeight = resolvedPlacement === 'top' ? topSpace : bottomSpace
      const boundedMaxHeight = Math.max(0, Math.min(maxHeight, availableHeight))
      // 'top' placement grows UPWARD from just above the anchor (anchor its bottom edge), so it
      // sits flush regardless of content height — no guessed offset, no jump.
      if (resolvedPlacement === 'top') setPos({ left, bottom: window.innerHeight - r.top + 6, width: w, maxHeight: boundedMaxHeight })
      else setPos({ left, top: r.bottom + 6, width: w, maxHeight: boundedMaxHeight })
    }
    update()
    // reposition while open (window resize); canvas pan/zoom closes via outside-mousedown/wheel
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
    // canvas pan/zoom (wheel) moves the anchor but not this fixed portal — close instead of detaching.
    // BUT scrolling INSIDE the popover (a long list) must not close it.
    const onWheel = (e: WheelEvent) => { if (!popRef.current?.contains(e.target as Node)) onClose() }
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
      className="dp-panel fixed z-[1000] overflow-y-auto rounded-lg border border-border bg-popover p-[5px] text-popover-foreground shadow-lg"
      onMouseDown={(e) => e.stopPropagation()}
      style={{ left: pos.left, top: pos.top, bottom: pos.bottom, width: pos.width, maxHeight: pos.maxHeight }}
    >
      {children}
    </div>,
    document.body,
  )
}
