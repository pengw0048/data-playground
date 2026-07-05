// Design tokens — the authoritative values from the Figma `design — tokens` page.
// P4: colors have exactly one job. Type accents are MUTED / non-semantic; red·amber·green
// are reserved for status; blue = focus/selection AND running (never on the same element).

// These mirror the shadcn CSS-var tokens in index.css (slate neutrals + one blue primary). Keeping
// them here lets the many inline-styled components re-skin in one place while they migrate to the
// Tailwind/shadcn primitives. ONE primary blue now (the review found two: #2f6ef0 vs #3b7fe0).
export const color = {
  // neutrals (slate)
  canvas: '#f7f8fa',
  card: '#ffffff',
  border: '#e5e8ec',
  hairline: '#eef1f4',
  ink: '#171a21',
  text2: '#5b616e',
  text3: '#98a0ac',

  // status — reserved semantic
  latest: '#16a34a',
  stale: '#d99a2b',
  running: '#2f7ff5',
  failed: '#e0483d',
  queued: '#8a94a6',
  draft: '#aab1bd',

  // wire / selection — single brand blue (matches --primary)
  wire: '#aab0ba',
  wireActive: '#2f7ff5',
  focus: '#2f7ff5',
} as const

// Muted, non-semantic accent stripe per node kind (left edge, 6px).
export const kindAccent: Record<string, string> = {
  source: '#5b6cc4',
  sample: '#8b6fce',
  filter: '#7a8595',
  select: '#6a8caf',
  transform: '#2f9e8f',
  join: '#c56b8a',
  aggregate: '#b0728f',
  sort: '#7f8896',
  dedup: '#94897a',
  sql: '#5aa0b5',
  'vector-search': '#7a6fce',
  write: '#64748b',
  metric: '#c39a4b',
  notebook: '#4f8ba8',
  note: '#eab308',  // annotation — sticky-note amber
  // control-flow: graphite, not data-colored
  branch: '#566173',
  loop: '#566173',
  variable: '#566173',
  opaque: '#566173',
}

// Wire types — each has a distinct port shape + neutral tint (design — wire types).
export type WireType = 'dataset' | 'selection' | 'sample' | 'sql-view' | 'metric' | 'value'

export const wire: Record<WireType, { color: string; shape: 'dot' | 'ring' | 'square' | 'diamond' }> = {
  dataset: { color: '#5b6cc4', shape: 'dot' },
  selection: { color: '#2f9e8f', shape: 'ring' },
  sample: { color: '#8b6fce', shape: 'dot' },
  'sql-view': { color: '#64748b', shape: 'square' },
  metric: { color: '#8a8f98', shape: 'diamond' },
  value: { color: '#8a8f98', shape: 'diamond' },
}

export type StatusKey = 'draft' | 'latest' | 'stale' | 'queued' | 'running' | 'failed' | 'done'

export const status: Record<StatusKey, { color: string; glyph: string; label: string }> = {
  draft: { color: color.draft, glyph: '○', label: 'draft' },
  latest: { color: color.latest, glyph: '✓', label: 'latest' },
  stale: { color: color.stale, glyph: '▲', label: 'stale' },
  queued: { color: color.queued, glyph: '◔', label: 'queued' },
  running: { color: color.running, glyph: '●', label: 'running' },
  failed: { color: color.failed, glyph: '✕', label: 'failed' },
  done: { color: color.latest, glyph: '✓', label: 'done' },  // per-node run completion
}

export const radius = { chip: 4, button: 8, node: 12, panel: 12, section: 14 } as const

export const shadow = {
  card: '0 1px 2px rgba(16,20,30,0.04), 0 1px 3px rgba(16,20,30,0.06)',
  panel: '0 6px 24px rgba(16,20,30,0.12), 0 2px 6px rgba(16,20,30,0.08)',
  focus: `0 0 0 2px ${color.focus}33, 0 1px 3px rgba(16,20,30,0.10)`,
} as const

export const font = {
  sans: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
  mono: "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace",
} as const

// Category grouping for the bottom toolbar (auto-populated from the registry).
export const categoryOrder = ['io', 'shape', 'compute', 'query', 'inspect', 'control'] as const
export type Category = (typeof categoryOrder)[number]
