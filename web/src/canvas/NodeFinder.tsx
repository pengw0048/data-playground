import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { NodeSpec } from '../nodes/registry'
import { color, kindAccent, type WireType } from '../theme/tokens'
import { Icon } from '../ui/Icon'

type FinderResult = { spec: NodeSpec; compatible: boolean; match: number }
const MAX_RENDERED_RESULTS = 100

function normalized(value: string): string {
  return value.trim().toLowerCase()
}

/** Compare Unicode code points directly, rather than inheriting the browser's locale collation. */
function codePointCompare(left: string, right: string): number {
  const a = Array.from(left)
  const b = Array.from(right)
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    const delta = a[index].codePointAt(0)! - b[index].codePointAt(0)!
    if (delta) return delta
  }
  return a.length - b.length
}

function structuredTerms(spec: NodeSpec): string[] {
  return [
    spec.category,
    ...spec.inputs.flatMap((port) => [port.id, port.label ?? '', port.wire, ...(port.accepts ?? [])]),
    ...spec.outputs.flatMap((port) => [port.id, port.label ?? '', port.wire]),
  ].map(normalized)
}

function descriptiveTerms(spec: NodeSpec): string[] {
  return [spec.blurb, spec.source ?? 'builtin'].map(normalized)
}

/** Stable operation-search ordering: title/kind matches lead secondary metadata matches. A connection
 * context may restrict the same effective registry to specs with an accepting input port. */
export function findNodeSpecs(specs: NodeSpec[], query: string, wire?: WireType, compatibleOnly = false): FinderResult[] {
  const q = normalized(query)
  const matches = specs.flatMap((spec) => {
    const title = normalized(spec.title)
    const kind = normalized(spec.kind)
    // Exact/prefix matches on the operation's name are direct commands, while exact/prefix
    // category and port matches are also intentional searches. A substring in an internal name
    // is only a weak match: `io` must still find the I/O category instead of just union/section.
    const match = !q ? 5
      : title === q || kind === q ? 0
        : title.startsWith(q) ? 1
          : kind.startsWith(q) ? 2
            : structuredTerms(spec).some((field) => field === q || field.startsWith(q)) ? 3
              : title.includes(q) || kind.includes(q) ? 4
                : structuredTerms(spec).some((field) => field.includes(q)) || descriptiveTerms(spec).some((field) => field.includes(q)) ? 5 : -1
    if (match < 0) return []
    const compatible = !wire || spec.inputs.some((port) => (port.accepts ?? [port.wire]).includes(wire))
    if (compatibleOnly && !compatible) return []
    return [{ spec, compatible, match }]
  })
  // A direct operation command wins over a category/port match ("sample" should not turn into a
  // generic sample-wire browse list). If there is no direct command, a deliberate structured
  // search wins over weak internal-name substrings and prose.
  const nameMatches = q ? matches.filter((result) => result.match < 3) : []
  const structuredMatches = q ? matches.filter((result) => result.match === 3) : []
  const focusedMatches = nameMatches.length > 0 ? nameMatches
    : structuredMatches.length > 0 ? structuredMatches : matches
  return focusedMatches.sort((a, b) => (
    a.match - b.match
    || Number(b.compatible) - Number(a.compatible)
    || codePointCompare(normalized(a.spec.title), normalized(b.spec.title))
    || codePointCompare(normalized(a.spec.kind), normalized(b.spec.kind))
  ))
}

function secondaryCue(result: FinderResult, results: FinderResult[]): string | null {
  // Registry provenance only earns space when two operations would otherwise look alike.
  const sameTitle = results.filter((candidate) => normalized(candidate.spec.title) === normalized(result.spec.title))
  if (sameTitle.length < 2) return null
  const source = result.spec.source?.startsWith('plugin:')
    ? `Plugin · ${result.spec.source.slice('plugin:'.length)}`
    : null
  return source ?? result.spec.category
}

export function NodeFinder({ specs, wire, compatibleOnly = false, nextStepKinds, nextStepLabel, onPick, onClose }: {
  specs: NodeSpec[]; wire?: WireType; compatibleOnly?: boolean; nextStepKinds?: ReadonlySet<string>; nextStepLabel?: string; onPick: (kind: string, asNextStep?: boolean) => void; onClose: () => void
}) {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const results = useMemo(() => findNodeSpecs(specs, query, wire, compatibleOnly), [specs, query, wire, compatibleOnly])
  // Search and rank the complete effective registry. Rendering remains bounded for large plugin packs.
  const shownResults = results.slice(0, MAX_RENDERED_RESULTS)
  const truncated = results.length > shownResults.length

  useEffect(() => { input.current?.focus() }, [])
  useEffect(() => { setActive(0) }, [query, wire])

  const choose = (result?: FinderResult) => {
    if (!result || (compatibleOnly && !result.compatible)) return
    const asNextStep = nextStepKinds?.has(result.spec.kind)
    if (asNextStep) onPick(result.spec.kind, true)
    else onPick(result.spec.kind)
  }
  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); onClose(); return }
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive((index) => Math.min(index + 1, shownResults.length - 1)); return }
    if (event.key === 'ArrowUp') { event.preventDefault(); setActive((index) => Math.max(index - 1, 0)); return }
    if (event.key === 'Enter') { event.preventDefault(); choose(shownResults[active]) }
  }

  return createPortal(
    <div className="dp-modal-overlay fixed inset-0 z-[70] grid justify-items-center content-start bg-black/20 pt-[12vh]" onMouseDown={onClose}>
      <section role="dialog" aria-modal="true" aria-label={compatibleOnly ? 'Connect to an operation' : 'Add an operation'} className="w-[min(620px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-popover shadow-xl" onMouseDown={(event) => event.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-border px-3 py-2.5">
          <Icon name="search" size={16} style={{ color: color.text3 }} />
          <input ref={input} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={onKeyDown}
            aria-label="Search operations" placeholder="Search operations, ports, categories…"
            className="min-w-0 flex-1 bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted-foreground" />
          {compatibleOnly && wire && <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">For {wire} output</span>}
          <kbd className="text-[10px] text-muted-foreground">Esc</kbd>
        </div>
        <div role="listbox" aria-label="Matching nodes" className="max-h-[min(480px,66vh)] overflow-y-auto p-1.5">
          {shownResults.map((result, index) => {
            const cue = secondaryCue(result, shownResults)
            return (
            <button key={result.spec.kind} role="option" aria-selected={index === active} onMouseEnter={() => setActive(index)} onClick={() => choose(result)}
              className={`flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left ${index === active ? 'bg-accent' : 'hover:bg-accent/60'}`}>
              <span className="mt-0.5 h-8 w-1 shrink-0 rounded-sm" style={{ background: kindAccent[result.spec.kind] ?? color.text3 }} />
              <span className="min-w-0 flex-1">
                <span className="text-[13px] font-semibold text-foreground">{result.spec.title}</span>
                <span className="mt-0.5 block text-[11px] text-muted-foreground">{result.spec.blurb || 'No description.'}</span>
                {nextStepKinds?.has(result.spec.kind) && <span className="mt-1 block text-[10px] font-medium text-primary">Add next step{nextStepLabel ? ` after ${nextStepLabel}` : ''}</span>}
                {cue && <span className="mt-1 block text-[10px] text-muted-foreground">{cue}</span>}
              </span>
            </button>
            )
          })}
          {results.length === 0 && <div className="px-3 py-8 text-center text-[12px] text-muted-foreground">No matching node.</div>}
          {truncated && <div className="px-3 py-2 text-center text-[11px] text-muted-foreground">Showing first {MAX_RENDERED_RESULTS} of {results.length}</div>}
        </div>
        <div className="border-t border-border px-3 py-2 text-[10.5px] text-muted-foreground">↑↓ to choose · Enter to {compatibleOnly ? 'connect' : 'add operation'}</div>
      </section>
    </div>,
    document.body,
  )
}
