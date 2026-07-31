import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import type { NodeSpec } from '../nodes/registry'
import { color, kindAccent, type WireType } from '../theme/tokens'
import { Icon } from '../ui/Icon'

type FinderResult = { spec: NodeSpec; compatible: boolean; match: number }
const MAX_RENDERED_RESULTS = 100
const PORT_POPOVER_WIDTH = 400
const PORT_POPOVER_MAX_HEIGHT = 480
const PORT_POPOVER_GUTTER = 8
const PORT_POPOVER_GAP = 8
const PORT_POPOVER_MIN_HEIGHT = 120

export type ScreenRect = {
  left: number
  right: number
  top: number
  bottom: number
}

export type PortFinderPlacement = {
  left: number
  width: number
  maxHeight: number
  top?: number
  bottom?: number
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(value, Math.max(min, max)))
}

/** Keep a port-started picker beside its port and inside the actual Canvas region. Upper-half
 * ports align the picker's top edge; lower-half ports align its bottom edge so short lists never
 * jump away from the connection that opened them. */
export function portFinderPlacement(
  anchor: ScreenRect,
  boundary: ScreenRect,
  viewportHeight: number,
): PortFinderPlacement {
  const boundaryWidth = Math.max(1, boundary.right - boundary.left)
  const width = Math.min(PORT_POPOVER_WIDTH, Math.max(1, boundaryWidth - PORT_POPOVER_GUTTER * 2))
  const rightCandidate = anchor.right + PORT_POPOVER_GAP
  const leftCandidate = anchor.left - PORT_POPOVER_GAP - width
  const left = clamp(
    rightCandidate + width <= boundary.right - PORT_POPOVER_GUTTER ? rightCandidate : leftCandidate,
    boundary.left + PORT_POPOVER_GUTTER,
    boundary.right - PORT_POPOVER_GUTTER - width,
  )
  const boundaryTop = boundary.top + PORT_POPOVER_GUTTER
  const boundaryBottom = boundary.bottom - PORT_POPOVER_GUTTER
  const alignTop = (anchor.top + anchor.bottom) / 2 <= (boundary.top + boundary.bottom) / 2

  if (alignTop) {
    const top = clamp(anchor.top, boundaryTop, boundaryBottom - PORT_POPOVER_MIN_HEIGHT)
    return {
      left,
      top,
      width,
      maxHeight: Math.min(PORT_POPOVER_MAX_HEIGHT, Math.max(1, boundaryBottom - top)),
    }
  }

  const alignedBottom = clamp(anchor.bottom, boundaryTop + PORT_POPOVER_MIN_HEIGHT, boundaryBottom)
  return {
    left,
    bottom: Math.max(0, viewportHeight - alignedBottom),
    width,
    maxHeight: Math.min(PORT_POPOVER_MAX_HEIGHT, Math.max(1, alignedBottom - boundaryTop)),
  }
}

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

export function NodeFinder({ specs, wire, compatibleOnly = false, anchor, boundary, returnFocus, onPick, onClose }: {
  specs: NodeSpec[]
  wire?: WireType
  compatibleOnly?: boolean
  anchor?: ScreenRect
  boundary?: ScreenRect
  returnFocus?: HTMLElement | null
  onPick: (kind: string) => void
  onClose: () => void
}) {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const panel = useRef<HTMLElement>(null)
  const results = useMemo(() => findNodeSpecs(specs, query, wire, compatibleOnly), [specs, query, wire, compatibleOnly])
  // Search and rank the complete effective registry. Rendering remains bounded for large plugin packs.
  const shownResults = results.slice(0, MAX_RENDERED_RESULTS)
  const truncated = results.length > shownResults.length
  const anchored = !!anchor && !!boundary

  useEffect(() => { input.current?.focus() }, [])
  useEffect(() => { setActive(0) }, [query, wire])
  useEffect(() => {
    if (!anchored) return
    const closeOutside = (event: MouseEvent) => {
      if (!panel.current?.contains(event.target as Node)) onClose()
    }
    const closeDetached = (event: WheelEvent) => {
      if (!panel.current?.contains(event.target as Node)) onClose()
    }
    const timer = window.setTimeout(() => window.addEventListener('mousedown', closeOutside), 0)
    window.addEventListener('wheel', closeDetached, { passive: true })
    window.addEventListener('resize', onClose)
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('mousedown', closeOutside)
      window.removeEventListener('wheel', closeDetached)
      window.removeEventListener('resize', onClose)
    }
  }, [anchored, onClose])

  const choose = (result?: FinderResult) => {
    if (!result || (compatibleOnly && !result.compatible)) return
    onPick(result.spec.kind)
  }
  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive((index) => Math.min(index + 1, shownResults.length - 1)); return }
    if (event.key === 'ArrowUp') { event.preventDefault(); setActive((index) => Math.max(index - 1, 0)); return }
    if (event.key === 'Enter') { event.preventDefault(); choose(shownResults[active]) }
  }
  const closeFromKeyboard = () => {
    onClose()
    window.requestAnimationFrame(() => {
      if (returnFocus?.isConnected) returnFocus.focus()
    })
  }
  const placement: CSSProperties | undefined = anchored
    ? portFinderPlacement(anchor, boundary, window.innerHeight)
    : undefined
  const picker = (
    <section
      ref={panel}
      role="dialog"
      aria-modal={anchored ? undefined : true}
      aria-label={compatibleOnly ? 'Connect to an operation' : 'Add an operation'}
      className={anchored
        ? 'dp-panel fixed z-[70] flex flex-col overflow-hidden rounded-xl border border-border bg-popover shadow-xl'
        : 'flex w-[min(620px,calc(100vw-32px))] flex-col overflow-hidden rounded-xl border border-border bg-popover shadow-xl'}
      style={placement}
      onMouseDown={(event) => event.stopPropagation()}
      onKeyDown={(event) => {
        if (event.key !== 'Escape') return
        event.preventDefault()
        event.stopPropagation()
        closeFromKeyboard()
      }}
    >
      <div className="flex flex-none items-center gap-2 border-b border-border px-3 py-2.5">
        <Icon name="search" size={16} style={{ color: color.text3 }} />
        <input ref={input} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={onKeyDown}
          aria-label="Search operations" placeholder="Search operations, ports, categories…"
          className="min-w-0 flex-1 bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted-foreground" />
        {compatibleOnly && wire && <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">For {wire} output</span>}
        <kbd className="text-[10px] text-muted-foreground">Esc</kbd>
      </div>
      <div role="listbox" aria-label="Matching nodes"
        className={anchored ? 'min-h-0 flex-1 overflow-y-auto p-1.5' : 'max-h-[min(480px,66vh)] overflow-y-auto p-1.5'}>
        {shownResults.map((result, index) => {
          const cue = secondaryCue(result, shownResults)
          return (
          <button key={result.spec.kind} role="option" aria-selected={index === active} onMouseEnter={() => setActive(index)} onClick={() => choose(result)}
            className={`flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left ${index === active ? 'bg-accent' : 'hover:bg-accent/60'}`}>
            <span className="mt-0.5 h-8 w-1 shrink-0 rounded-sm" style={{ background: kindAccent[result.spec.kind] ?? color.text3 }} />
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-2 text-[13px] font-semibold text-foreground">
                <span>{result.spec.title}</span>
              </span>
              <span className="mt-0.5 block text-[11px] text-muted-foreground">{result.spec.blurb || 'No description.'}</span>
              {cue && <span className="mt-1 block text-[10px] text-muted-foreground">{cue}</span>}
            </span>
          </button>
          )
        })}
        {results.length === 0 && <div className="px-3 py-8 text-center text-[12px] text-muted-foreground">No matching node.</div>}
        {truncated && <div className="px-3 py-2 text-center text-[11px] text-muted-foreground">Showing first {MAX_RENDERED_RESULTS} of {results.length}</div>}
      </div>
      <div className="flex-none border-t border-border px-3 py-2 text-[10.5px] text-muted-foreground">↑↓ to choose · Enter to {compatibleOnly ? 'connect' : 'add operation'}</div>
    </section>
  )

  if (anchored) return createPortal(picker, document.body)
  return createPortal(
    <div className="dp-modal-overlay fixed inset-0 z-[70] grid justify-items-center content-start bg-black/20 pt-[12vh]" onMouseDown={onClose}>
      {picker}
    </div>,
    document.body,
  )
}
