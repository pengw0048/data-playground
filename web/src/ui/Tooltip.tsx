import { useState, type ReactNode } from 'react'

// Hover label — the small black tip from the actions page. Shows above the trigger.
export function Tooltip({ label, children, side = 'top' }: {
  label: string; children: ReactNode; side?: 'top' | 'bottom'
}) {
  const [hover, setHover] = useState(false)
  return (
    <span
      style={{ position: 'relative', display: 'inline-flex' }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      {children}
      {hover && (
        <span
          style={{
            position: 'absolute',
            [side === 'top' ? 'bottom' : 'top']: 'calc(100% + 6px)',
            left: '50%',
            transform: 'translateX(-50%)',
            background: '#1a1c22',
            color: '#fff',
            fontSize: 10.5,
            fontWeight: 500,
            padding: '3px 7px',
            borderRadius: 5,
            whiteSpace: 'nowrap',
            pointerEvents: 'none',
            zIndex: 50,
            boxShadow: '0 2px 8px rgba(0,0,0,.18)',
          }}
        >
          {label}
        </span>
      )}
    </span>
  )
}
