import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { NodeSpec } from '../nodes/registry'
import { color, kindAccent, type WireType } from '../theme/tokens'
import { Icon } from '../ui/Icon'

type FinderResult = { spec: NodeSpec; compatible: boolean; match: number }

function normalized(value: string): string {
  return value.trim().toLocaleLowerCase()
}

function haystack(spec: NodeSpec): string[] {
  return [
    spec.blurb,
    spec.category,
    spec.source ?? 'builtin',
    ...spec.inputs.flatMap((port) => [port.id, port.label ?? '', port.wire, ...(port.accepts ?? [])]),
    ...spec.outputs.flatMap((port) => [port.id, port.label ?? '', port.wire]),
  ].map(normalized)
}

/** Stable finder ordering: title/kind matches lead secondary metadata matches; a connection
 * context promotes compatible specs but keeps every registered operation discoverable. */
export function findNodeSpecs(specs: NodeSpec[], query: string, wire?: WireType): FinderResult[] {
  const q = normalized(query)
  return specs.flatMap((spec) => {
    const title = normalized(spec.title)
    const kind = normalized(spec.kind)
    const match = !q ? 3
      : title === q || kind === q ? 0
        : title.startsWith(q) || kind.startsWith(q) ? 1
          : title.includes(q) || kind.includes(q) ? 2
            : haystack(spec).some((field) => field.includes(q)) ? 3 : -1
    if (match < 0) return []
    const compatible = !wire || spec.inputs.some((port) => (port.accepts ?? [port.wire]).includes(wire))
    return [{ spec, compatible, match }]
  }).sort((a, b) => (
    Number(b.compatible) - Number(a.compatible)
    || a.match - b.match
    || a.spec.title.localeCompare(b.spec.title)
    || a.spec.kind.localeCompare(b.spec.kind)
  ))
}

export function portSummary(spec: NodeSpec): string {
  const inputs = spec.inputs.map((port) => (port.accepts ?? [port.wire]).join('/')).join(', ') || 'none'
  const outputs = spec.outputs.map((port) => port.wire).join(', ') || 'none'
  return `in ${inputs} · out ${outputs}`
}

function sourceLabel(source?: string): string {
  return source?.startsWith('plugin:') ? `Plugin · ${source.slice('plugin:'.length)}` : 'Built-in'
}

export function NodeFinder({ specs, wire, onPick, onClose }: {
  specs: NodeSpec[]; wire?: WireType; onPick: (kind: string) => void; onClose: () => void
}) {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const results = useMemo(() => findNodeSpecs(specs, query, wire), [specs, query, wire])

  useEffect(() => { input.current?.focus() }, [])
  useEffect(() => { setActive(0) }, [query, wire])

  const choose = (result?: FinderResult) => { if (result) onPick(result.spec.kind) }
  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); onClose(); return }
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive((index) => Math.min(index + 1, results.length - 1)); return }
    if (event.key === 'ArrowUp') { event.preventDefault(); setActive((index) => Math.max(index - 1, 0)); return }
    if (event.key === 'Enter') { event.preventDefault(); choose(results[active]) }
  }

  return createPortal(
    <div className="dp-modal-overlay fixed inset-0 z-[70] grid place-items-start bg-black/20 pt-[12vh]" onMouseDown={onClose}>
      <section role="dialog" aria-modal="true" aria-label="Find a node" className="w-[min(620px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-popover shadow-xl" onMouseDown={(event) => event.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-border px-3 py-2.5">
          <Icon name="search" size={16} style={{ color: color.text3 }} />
          <input ref={input} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={onKeyDown}
            aria-label="Search nodes" placeholder="Search operations, ports, categories…"
            className="min-w-0 flex-1 bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted-foreground" />
          {wire && <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">accepts {wire}</span>}
          <kbd className="text-[10px] text-muted-foreground">Esc</kbd>
        </div>
        <div role="listbox" aria-label="Matching nodes" className="max-h-[min(480px,66vh)] overflow-y-auto p-1.5">
          {results.map((result, index) => (
            <button key={result.spec.kind} role="option" aria-selected={index === active} onMouseEnter={() => setActive(index)} onClick={() => choose(result)}
              className={`flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left ${index === active ? 'bg-accent' : 'hover:bg-accent/60'}`}>
              <span className="mt-0.5 h-8 w-1 shrink-0 rounded-sm" style={{ background: kindAccent[result.spec.kind] ?? color.text3 }} />
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2"><span className="text-[13px] font-semibold text-foreground">{result.spec.title}</span><span className="text-[10px] text-muted-foreground">{result.spec.kind}</span></span>
                <span className="mt-0.5 block text-[11px] text-muted-foreground">{result.spec.blurb || 'No description.'}</span>
                <span className="mt-1 block text-[10px] text-muted-foreground">{result.spec.category} · {sourceLabel(result.spec.source)} · {portSummary(result.spec)}</span>
              </span>
              {wire && result.compatible && <span className="mt-1 rounded bg-primary/10 px-1.5 py-0.5 text-[9.5px] font-semibold text-primary">compatible</span>}
            </button>
          ))}
          {results.length === 0 && <div className="px-3 py-8 text-center text-[12px] text-muted-foreground">No matching node.</div>}
        </div>
        <div className="border-t border-border px-3 py-2 text-[10.5px] text-muted-foreground">↑↓ to choose · Enter to add</div>
      </section>
    </div>,
    document.body,
  )
}
