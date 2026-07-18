import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { nodeOutputs } from '../nodes/registry'
import type { CanvasNode } from '../types/graph'
import { color, status } from '../theme/tokens'
import { Icon } from '../ui/Icon'

export type ExistingNodeResult = { node: CanvasNode; match: number; outputs: string[]; labels: string[] }
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

function fieldMatch(value: string, query: string, exact: number, prefix: number, includes: number): number {
  const field = normalized(value)
  if (field === query) return exact
  if (field.startsWith(query)) return prefix
  return field.includes(query) ? includes : -1
}

function outputLabels(node: CanvasNode): string[] {
  return nodeOutputs(node).map((port) => port.label && port.label !== port.id
    ? `${port.label} (${port.id} · ${port.wire})`
    : `${port.id} · ${port.wire}`)
}

function stateLabels(node: CanvasNode): string[] {
  return [
    status[node.data.status]?.label ?? node.data.status,
    ...(node.data.disabled ? ['disabled'] : []),
    ...(node.data.bypassed ? ['bypassed'] : []),
    ...(node.data.meta ? [node.data.meta] : []),
  ]
}

/** Search only the current canvas document. Ranking and the 100-result render bound are stable. */
export function findExistingNodes(nodes: CanvasNode[], query: string): ExistingNodeResult[] {
  const q = normalized(query)
  return nodes.flatMap((node) => {
    const outputs = outputLabels(node)
    const labels = stateLabels(node)
    const candidates = [
      fieldMatch(node.data.title, q, 0, 3, 6),
      fieldMatch(node.type, q, 1, 4, 7),
      fieldMatch(node.id, q, 2, 5, 8),
      ...[...labels, ...outputs].map((label) => fieldMatch(label, q, 9, 9, 9)),
    ].filter((candidate) => candidate >= 0)
    const match = !q ? 10 : (candidates.length ? Math.min(...candidates) : -1)
    return match === -1 ? [] : [{ node, match, outputs, labels }]
  }).sort((left, right) => (
    left.match - right.match
    || codePointCompare(normalized(left.node.data.title), normalized(right.node.data.title))
    || codePointCompare(normalized(left.node.type), normalized(right.node.type))
    || codePointCompare(normalized(left.node.id), normalized(right.node.id))
  ))
}

export function ExistingNodeLocator({ nodes, onPick, onClose }: {
  nodes: CanvasNode[]; onPick: (id: string) => void; onClose: () => void
}) {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const results = useMemo(() => findExistingNodes(nodes, query), [nodes, query])
  const shownResults = results.slice(0, MAX_RENDERED_RESULTS)
  const truncated = results.length > shownResults.length

  useEffect(() => { input.current?.focus() }, [])
  useEffect(() => { setActive(0) }, [query, nodes])

  const choose = (result?: ExistingNodeResult) => { if (result) onPick(result.node.id) }
  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); onClose(); return }
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive((index) => Math.min(index + 1, shownResults.length - 1)); return }
    if (event.key === 'ArrowUp') { event.preventDefault(); setActive((index) => Math.max(index - 1, 0)); return }
    if (event.key === 'Enter') { event.preventDefault(); choose(shownResults[active]) }
  }

  return createPortal(
    <div className="dp-modal-overlay fixed inset-0 z-[70] grid place-items-start bg-black/20 pt-[12vh]" onMouseDown={onClose}>
      <section role="dialog" aria-modal="true" aria-label="Locate an existing node" className="w-[min(620px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-popover shadow-xl" onMouseDown={(event) => event.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-border px-3 py-2.5">
          <Icon name="search" size={16} style={{ color: color.text3 }} />
          <input ref={input} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={onKeyDown}
            aria-label="Search existing nodes" placeholder="Search titles, kinds, IDs, status, outputs…"
            className="min-w-0 flex-1 bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted-foreground" />
          <kbd className="text-[10px] text-muted-foreground">Esc</kbd>
        </div>
        <div role="listbox" aria-label="Matching existing nodes" className="max-h-[min(480px,66vh)] overflow-y-auto p-1.5">
          {shownResults.map((result, index) => (
            <button key={result.node.id} role="option" aria-selected={index === active} onMouseEnter={() => setActive(index)} onClick={() => choose(result)}
              className={`flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left ${index === active ? 'bg-accent' : 'hover:bg-accent/60'}`}>
              <span className="mt-0.5 grid h-8 min-w-8 place-items-center rounded bg-muted px-1 font-mono text-[10px] font-semibold text-muted-foreground">{result.node.type}</span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2"><span className="truncate text-[13px] font-semibold text-foreground">{result.node.data.title}</span><span className="shrink-0 text-[10px] text-muted-foreground">{result.node.type} · {result.node.id}</span></span>
                <span className="mt-0.5 block truncate text-[11px] text-muted-foreground">{result.labels.join(' · ')}</span>
                <span className="mt-1 block truncate text-[10px] text-muted-foreground">outputs: {result.outputs.join(', ') || 'none'}</span>
              </span>
            </button>
          ))}
          {results.length === 0 && <div className="px-3 py-8 text-center text-[12px] text-muted-foreground">No matching existing node.</div>}
          {truncated && <div className="px-3 py-2 text-center text-[11px] text-muted-foreground">Showing first {MAX_RENDERED_RESULTS} of {results.length}</div>}
        </div>
        <div className="border-t border-border px-3 py-2 text-[10.5px] text-muted-foreground">↑↓ to choose · Enter to locate</div>
      </section>
    </div>,
    document.body,
  )
}
