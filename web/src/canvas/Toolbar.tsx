import { useRef, useState } from 'react'
import { useReactFlow } from '@xyflow/react'
import { allSpecs } from '../nodes'
import { useStore, freePosition, roleCanEdit } from '../store/graph'
import { categoryOrder, color, kindAccent, type Category } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { Tooltip } from '../ui/Tooltip'
import { Popover } from '../ui/Popover'
import type { CanvasNode } from '../types/graph'
import { NodeFinder } from './NodeFinder'
import { ExistingNodeLocator } from './ExistingNodeLocator'
import { cn } from '@/lib/utils'

const CATEGORY_ICON: Record<Category, IconName> = {
  io: 'db', shape: 'sample', compute: 'fx', query: 'sql', inspect: 'note', control: 'code',
}
const CATEGORY_LABEL: Record<Category, string> = {
  io: 'Sources & sinks', shape: 'Shape', compute: 'Compute', query: 'Query', inspect: 'Inspect', control: 'Control flow',
}

// Bottom toolbar — auto-populated from the node registry, grouped by category (FR-C2a).
export function Toolbar() {
  const { screenToFlowPosition, setCenter, getZoom } = useReactFlow()
  const doc = useStore((s) => s.doc)
  const addNode = useStore((s) => s.addNode)
  const select = useStore((s) => s.select)
  const setAgentOpen = useStore((s) => s.setAgentOpen)
  const agentOpen = useStore((s) => s.agentOpen)
  const canvasRole = useStore((s) => s.canvasRole)
  const [open, setOpen] = useState<Category | null>(null)
  const [operationFinderOpen, setOperationFinderOpen] = useState(false)
  const [locatorOpen, setLocatorOpen] = useState(false)

  const specs = allSpecs()
  const cats = categoryOrder.filter((c) => specs.some((s) => s.category === c))

  const add = (kind: string) => {
    const c = screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 })
    const pos = freePosition(useStore.getState().doc.nodes, { x: c.x - 116, y: c.y - 40 })
    addNode(kind, pos)
    setOpen(null)
    setOperationFinderOpen(false)
  }

  const locate = (id: string) => {
    const node = useStore.getState().doc.nodes.find((candidate) => candidate.id === id)
    if (!node) return
    select(id)
    const position = absolutePosition(useStore.getState().doc.nodes, node)
    setCenter(position.x + 116, position.y + 72, { zoom: Math.max(0.8, Math.min(getZoom(), 1.3)), duration: 350 })
    setLocatorOpen(false)
  }

  if (!roleCanEdit(canvasRole)) {
    return (
      <div data-testid="view-only-badge" className="absolute bottom-[22px] left-1/2 z-[16] -translate-x-1/2 rounded-full border border-border bg-card px-3 py-1.5 text-[11.5px] font-medium text-muted-foreground shadow-sm">
        {canvasRole === 'viewer' ? 'View-only canvas' : 'Checking canvas access…'}
      </div>
    )
  }

  return (
    <div data-testid="toolbar" className="absolute bottom-[22px] left-1/2 z-[16] -translate-x-1/2">
      <div className="flex items-center gap-1 rounded-2xl border border-border bg-card p-1.5 shadow-lg">
        {cats.map((cat) => (
          <CategoryButton
            key={cat}
            cat={cat}
            open={open === cat}
            onToggle={() => setOpen((o) => (o === cat ? null : cat))}
            onClose={() => setOpen(null)}
            specs={specs.filter((s) => s.category === cat)}
            onPick={add}
          />
        ))}

        <div className="mx-1 h-[22px] w-px bg-border" />

        <Tooltip label="Add operation">
          <button aria-label="Add operation" onClick={() => { setOpen(null); setLocatorOpen(false); setOperationFinderOpen(true) }}
            className="grid h-[34px] w-[38px] place-items-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground">
            <Icon name="plus" size={16} />
          </button>
        </Tooltip>

        <Tooltip label="Locate existing node">
          <button aria-label="Locate existing node" onClick={() => { setOpen(null); setOperationFinderOpen(false); setLocatorOpen(true) }}
            className="grid h-[34px] w-[38px] place-items-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground">
            <Icon name="search" size={16} />
          </button>
        </Tooltip>

        <button
          onClick={() => setAgentOpen(!agentOpen)}
          className="inline-flex items-center gap-[7px] rounded-lg px-3.5 py-[7px] text-[12.5px] font-semibold"
          // Agent brand accent (violet) — no design token expresses it; matches the AgentDock it opens.
          style={{ background: agentOpen ? '#efeaff' : 'linear-gradient(180deg,#f3effe,#ece5fc)', color: '#6b4bd6' }}
        >
          <Icon name="sparkle" size={14} /> Agent
        </button>
      </div>
      {operationFinderOpen && <NodeFinder specs={specs} onPick={add} onClose={() => setOperationFinderOpen(false)} />}
      {locatorOpen && <ExistingNodeLocator nodes={doc.nodes} onPick={locate} onClose={() => setLocatorOpen(false)} />}
    </div>
  )
}

function absolutePosition(nodes: CanvasNode[], node: CanvasNode): { x: number; y: number } {
  const byId = new Map(nodes.map((candidate) => [candidate.id, candidate]))
  const seen = new Set<string>()
  let current = node
  let x = current.position.x
  let y = current.position.y
  while (current.parentId && !seen.has(current.parentId)) {
    seen.add(current.parentId)
    const parent = byId.get(current.parentId)
    if (!parent) break
    x += parent.position.x
    y += parent.position.y
    current = parent
  }
  return { x, y }
}

function CategoryButton({ cat, open, onToggle, onClose, specs, onPick }: {
  cat: Category; open: boolean; onToggle: () => void; onClose: () => void
  specs: ReturnType<typeof allSpecs>; onPick: (kind: string) => void
}) {
  const ref = useRef<HTMLButtonElement>(null)
  return (
    <>
      <Tooltip label={CATEGORY_LABEL[cat]}>
        <button
          ref={ref}
          aria-label={CATEGORY_LABEL[cat]}
          onClick={(e) => { e.stopPropagation(); onToggle() }}
          className={cn(
            'grid h-[34px] w-[38px] place-items-center rounded-lg transition-colors',
            open ? 'bg-accent text-foreground' : 'text-muted-foreground hover:bg-accent hover:text-foreground',
          )}
        >
          <Icon name={CATEGORY_ICON[cat]} size={16} />
        </button>
      </Tooltip>
      {/* portal popover positioned once against the button (no percentage-based jump) */}
      <Popover anchorRef={ref} open={open} onClose={onClose} width={210} placement="top" align="left">
        <div className="px-2 py-[5px] text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground">
          {CATEGORY_LABEL[cat]}
        </div>
        {specs.map((s) => (
          <button
            key={s.kind}
            onClick={(e) => { e.stopPropagation(); onPick(s.kind) }}
            className="flex w-full items-center gap-[9px] rounded-md px-2 py-[7px] text-left hover:bg-accent"
          >
            <span className="h-[15px] w-1 rounded-sm" style={{ background: kindAccent[s.kind] ?? color.text3 }} />
            <span className="flex flex-col">
              <span className="text-[12.5px] font-semibold text-foreground">{s.title}</span>
              <span className="text-[10px] text-muted-foreground">{s.blurb}</span>
            </span>
          </button>
        ))}
      </Popover>
    </>
  )
}
