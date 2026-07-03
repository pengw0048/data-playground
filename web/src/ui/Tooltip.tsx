import { useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

// Hover label — the small black tip from the actions page. Rendered in a portal on document.body
// so it is never clipped by a node card's `overflow: hidden` (it escapes the card's box).
export function Tooltip({ label, children, side = 'top' }: {
  label: string; children: ReactNode; side?: 'top' | 'bottom'
}) {
  const ref = useRef<HTMLSpanElement>(null)
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null)

  const show = () => {
    const el = ref.current
    if (!el) return
    const r = el.getBoundingClientRect()
    setPos({ x: r.left + r.width / 2, y: side === 'top' ? r.top - 6 : r.bottom + 6 })
  }
  const hide = () => setPos(null)

  return (
    <span
      ref={ref}
      style={{ display: 'inline-flex' }}
      onMouseEnter={show}
      onMouseLeave={hide}
    >
      {children}
      {pos && createPortal(
        <span
          style={{
            position: 'fixed',
            left: pos.x,
            top: pos.y,
            transform: `translate(-50%, ${side === 'top' ? '-100%' : '0'})`,
            background: '#1a1c22',
            color: '#fff',
            fontSize: 10.5,
            fontWeight: 500,
            padding: '3px 7px',
            borderRadius: 5,
            whiteSpace: 'nowrap',
            pointerEvents: 'none',
            zIndex: 1000,
            boxShadow: '0 2px 8px rgba(0,0,0,.18)',
          }}
        >
          {label}
        </span>,
        document.body,
      )}
    </span>
  )
}
