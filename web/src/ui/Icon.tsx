// Drawn monochrome icons — 14px, stroke ~1.4, recognizable (no hamburger). Labels on hover
// are added by callers. eye=view · play=run · clock=history · {} =code · … =more (tokens page).
import type { CSSProperties } from 'react'

export type IconName =
  | 'eye' | 'play' | 'clock' | 'code' | 'more' | 'refresh' | 'stop' | 'plus' | 'close'
  | 'chevronDown' | 'chevronRight' | 'chevronLeft' | 'lineage' | 'power' | 'mute' | 'rename'
  | 'duplicate' | 'export' | 'trash' | 'search' | 'sparkle' | 'grid' | 'branch' | 'loop'
  | 'fx' | 'sample' | 'arrow' | 'external' | 'check' | 'db' | 'sigma' | 'sql' | 'note'
  | 'minus' | 'link' | 'settings'

const P: Record<IconName, JSX.Element> = {
  eye: <><path d="M1 8s2.7-5 7-5 7 5 7 5-2.7 5-7 5-7-5-7-5Z" /><circle cx="8" cy="8" r="2.2" /></>,
  settings: <><circle cx="8" cy="8" r="2.3" /><path d="M8 1v2.2M8 12.8V15M15 8h-2.2M3.2 8H1M12.9 3.1l-1.6 1.6M4.7 11.3l-1.6 1.6M12.9 12.9l-1.6-1.6M4.7 4.7 3.1 3.1" /></>,
  play: <path d="M4.5 3.2v9.6l7.5-4.8-7.5-4.8Z" />,
  clock: <><circle cx="8" cy="8" r="6.2" /><path d="M8 4.5V8l2.6 1.6" /></>,
  code: <path d="M5.5 4 2 8l3.5 4M10.5 4 14 8l-3.5 4" />,
  more: <><circle cx="3" cy="8" r="1" /><circle cx="8" cy="8" r="1" /><circle cx="13" cy="8" r="1" /></>,
  refresh: <><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" /><path d="M13.8 3.2v2.6h-2.6" /></>,
  stop: <rect x="4" y="4" width="8" height="8" rx="1.2" />,
  plus: <path d="M8 3v10M3 8h10" />,
  minus: <path d="M3 8h10" />,
  close: <path d="M4 4l8 8M12 4l-8 8" />,
  chevronDown: <path d="M4 6l4 4 4-4" />,
  chevronRight: <path d="M6 4l4 4-4 4" />,
  chevronLeft: <path d="M10 4L6 8l4 4" />,
  lineage: <><circle cx="3.5" cy="8" r="1.6" /><circle cx="12.5" cy="4" r="1.6" /><circle cx="12.5" cy="12" r="1.6" /><path d="M5 7l6-2.4M5 9l6 2.4" /></>,
  power: <><path d="M8 2.5v5" /><path d="M4.5 5a5 5 0 1 0 7 0" /></>,
  mute: <><circle cx="8" cy="8" r="6" /><path d="M4 4l8 8" /></>,
  rename: <><path d="M10.5 3.2 12.8 5.5 6 12.3 3.5 12.8 4 10.3z" /></>,
  duplicate: <><rect x="5.5" y="5.5" width="7.5" height="7.5" rx="1.2" /><path d="M3 10.5V3.2h7.3" /></>,
  export: <><path d="M8 2.5v7.5" /><path d="M5 6.5 8 3.5l3 3" /><path d="M3 11.5v1.8h10v-1.8" /></>,
  trash: <><path d="M3.5 4.5h9M6 4.5V3h4v1.5M5 4.5l.6 8.3h4.8L11 4.5" /></>,
  search: <><circle cx="7" cy="7" r="4" /><path d="M10 10l3.5 3.5" /></>,
  sparkle: <path d="M8 2.5l1.4 3.6L13 7.5l-3.6 1.4L8 12.5 6.6 8.9 3 7.5l3.6-1.4z" />,
  grid: <><rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1" /><rect x="9" y="2.5" width="4.5" height="4.5" rx="1" /><rect x="2.5" y="9" width="4.5" height="4.5" rx="1" /><rect x="9" y="9" width="4.5" height="4.5" rx="1" /></>,
  branch: <><circle cx="4" cy="8" r="1.6" /><circle cx="12" cy="4" r="1.6" /><circle cx="12" cy="12" r="1.6" /><path d="M5.5 7.3 10.6 4.6M5.5 8.7l5.1 2.7" /></>,
  loop: <><path d="M4 8a4 4 0 0 1 4-4h1.5" /><path d="M12 8a4 4 0 0 1-4 4H6.5" /><path d="M9 2.5 10.5 4 9 5.5M7 10.5 5.5 12 7 13.5" /></>,
  fx: <path d="M5 12.5c0-6 1-9 3.5-9M3.5 7.5h5" />,
  sample: <><rect x="2.5" y="2.5" width="11" height="11" rx="2" /><path d="M2.5 6.5h11M6.5 6.5v7" opacity=".5" /></>,
  arrow: <path d="M3 8h9M9 5l3 3-3 3" />,
  external: <><path d="M6 3.5H3.5v9h9V10" /><path d="M8.5 3.5H13v4.5M13 3.5 7.5 9" /></>,
  check: <path d="M3.5 8.5 6.5 11.5 12.5 4.5" />,
  db: <><ellipse cx="8" cy="4" rx="5" ry="2" /><path d="M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4" /><path d="M3 8c0 1.1 2.2 2 5 2s5-.9 5-2" opacity=".5" /></>,
  sigma: <path d="M11.5 3.5H4.5L8 8l-3.5 4.5h7" />,
  sql: <><ellipse cx="8" cy="4.5" rx="4.5" ry="1.8" /><path d="M3.5 4.5v6c0 1 2 1.8 4.5 1.8s4.5-.8 4.5-1.8v-6" /></>,
  note: <><rect x="3" y="2.5" width="10" height="11" rx="1.5" /><path d="M5.5 6h5M5.5 8.5h5M5.5 11h3" opacity=".6" /></>,
  link: <><path d="M6.5 9.5 9.5 6.5" /><path d="M7 5l1-1a2.5 2.5 0 0 1 3.5 3.5l-1 1M9 11l-1 1A2.5 2.5 0 0 1 4.5 9.5l1-1" /></>,
}

export function Icon({ name, size = 14, style, strokeWidth = 1.4, filled = false }: {
  name: IconName; size?: number; style?: CSSProperties; strokeWidth?: number; filled?: boolean
}) {
  const solid = name === 'play' || name === 'sparkle' || (filled && name === 'stop')
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill={solid ? 'currentColor' : 'none'}
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ display: 'block', ...style }}
    >
      {P[name]}
    </svg>
  )
}
