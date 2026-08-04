// Design tokens — compact neutral application chrome inspired by Figma UI3.
// P4: colors have exactly one job. Type accents are MUTED / non-semantic; red·amber·green
// are reserved for status; blue = focus/selection AND running (never on the same element).

// These mirror the shadcn CSS-var tokens in index.css (neutral grays + one blue primary). Keeping
// them here lets the many inline-styled components re-skin in one place while they migrate to the
// Tailwind/shadcn primitives. ONE primary blue now (the review found two: #2f6ef0 vs #3b7fe0).
export const color = {
  // Neutrals resolve to the CSS-var tokens (index.css :root + [data-theme='dark']) so every inline
  // style that reads color.* re-skins with the theme in one place — no static light hex that would
  // stay light in dark mode. Used only in style={{}} / className contexts, where var() resolves.
  canvas: 'var(--canvas)',
  card: 'hsl(var(--card))',
  border: 'hsl(var(--border))',
  hairline: 'var(--hairline)',
  ink: 'hsl(var(--foreground))',
  text2: 'hsl(var(--muted-foreground))',
  text3: 'var(--text-3)',

  // status — reserved semantic (kept as literal hex: read on both themes, and some are used as canvas
  // fill / in alpha-concatenated shadow strings where a CSS var() would not resolve)
  latest: '#16a34a',
  stale: '#d99a2b',
  running: '#0099ff',
  failed: '#e0483d',
  queued: '#8a94a6',
  draft: '#aab1bd',
  checking: '#8a94a6',
  unknown: '#8a94a6',

  // wire / selection — literal hex on purpose: consumed as SVG presentation attributes (ArrowDefs,
  // WireEdge) and in alpha-concatenated strings (shadow.focus), where var() does NOT resolve.
  wire: '#9b9b9b',
  wireActive: '#0099ff',
  focus: '#007acc',
} as const

// Muted node colors remain for the minimap and wire-scale overview only. Normal UI chrome uses
// neutral glyph tiles so operation type is not expressed as a decorative colored stripe.
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
  note: '#eab308',  // annotation — sticky-note amber
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

export type StatusKey =
  | 'draft' | 'idle' | 'checking' | 'latest' | 'stale' | 'unknown' | 'queued' | 'running' | 'failed' | 'done'

export const status: Record<StatusKey, { color: string; glyph: string; label: string }> = {
  draft: { color: color.draft, glyph: '○', label: 'draft' },
  idle: { color: color.draft, glyph: '', label: 'no saved result' },
  checking: { color: color.checking, glyph: '…', label: 'checking' },
  latest: { color: color.latest, glyph: '✓', label: 'latest' },
  stale: { color: color.stale, glyph: '▲', label: 'stale' },
  queued: { color: color.queued, glyph: '◔', label: 'queued' },
  running: { color: color.running, glyph: '●', label: 'running' },
  failed: { color: color.failed, glyph: '✕', label: 'failed' },
  unknown: { color: color.unknown, glyph: '?', label: 'status unavailable' },
  done: { color: color.latest, glyph: '✓', label: 'done' },  // per-node run completion
}

// Status rendered as text or a glyph. Theme-aware (index.css) because the fill hexes above fail
// WCAG AA as text on the light chip surface.
export const statusText: Record<StatusKey, string> = {
  draft: 'var(--status-draft)',
  idle: 'var(--status-draft)',
  checking: 'var(--status-queued)',
  latest: 'var(--status-latest)',
  stale: 'var(--status-stale)',
  queued: 'var(--status-queued)',
  running: 'var(--status-running)',
  failed: 'var(--status-failed)',
  unknown: 'var(--status-queued)',
  done: 'var(--status-latest)',
}

export const radius = { chip: 4, button: 6, node: 8, panel: 8, section: 8 } as const

export const shadow = {
  card: '0 1px 2px rgba(0,0,0,0.06)',
  panel: '0 8px 24px rgba(0,0,0,0.12), 0 2px 6px rgba(0,0,0,0.08)',
  focus: `0 0 0 2px ${color.focus}33, 0 1px 2px rgba(0,0,0,0.08)`,
} as const

export const font = {
  sans: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
  mono: "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace",
} as const

// Category grouping for the bottom toolbar (auto-populated from the registry).
export const categoryOrder = ['io', 'shape', 'compute', 'query', 'inspect', 'control'] as const
export type Category = (typeof categoryOrder)[number]
