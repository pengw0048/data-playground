import type { ReactNode } from 'react'

const GLYPH: Record<string, string> = { ' ': '␣', '\t': '⇥', '\n': '↵', '\r': '␍' }
const NAME: Record<string, string> = { ' ': 'space', '\t': 'tab', '\n': 'newline', '\r': 'carriage return' }

function label(run: string): string {
  const counts = new Map<string, number>()
  for (const ch of run) counts.set(NAME[ch], (counts.get(NAME[ch]) ?? 0) + 1)
  return [...counts].map(([name, n]) => `${n} ${name}${n > 1 ? 's' : ''}`).join(' + ')
}

/** Renders a string with the whitespace the layout hides shown as muted glyphs. */
export function WhitespaceText({ value }: { value: string }) {
  // Leading, trailing, tabs, newlines and multi-space runs; a lone inner space renders as itself.
  const hidden = [...value.matchAll(/[ \t\r\n]+/g)]
    .filter((m) => m.index === 0 || m.index + m[0].length === value.length || m[0] !== ' ')
  if (!hidden.length) return <span>{value}</span>
  const parts: ReactNode[] = []
  let at = 0
  for (const m of hidden) {
    if (m.index > at) parts.push(value.slice(at, m.index))
    parts.push(
      <span key={m.index} title={label(m[0])} className="text-muted-foreground/70">
        {[...m[0]].map((ch) => (ch === '\n' ? `${GLYPH[ch]}\n` : GLYPH[ch])).join('')}
      </span>,
    )
    at = m.index + m[0].length
  }
  if (at < value.length) parts.push(value.slice(at))
  return <span>{parts}</span>
}
