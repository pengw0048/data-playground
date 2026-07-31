import { useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { useReactFlow, useViewport } from '@xyflow/react'
import { allSpecs } from '../nodes'
import { useStore, roleCanEdit } from '../store/graph'
import { categoryOrder, color, kindAccent, type Category } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { Tooltip } from '../ui/Tooltip'
import { Popover } from '../ui/Popover'
import { NodeFinder } from './NodeFinder'
import { ExistingNodeLocator } from './ExistingNodeLocator'
import { locateNode } from './locateNode'
import { cn } from '@/lib/utils'
import { toolbarSafePosition, type ToolbarSafeBounds } from './toolbarPlacement'
import { canvasFitOptions } from './viewportFit'

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
  const toolbarRef = useRef<HTMLDivElement>(null)

  const specs = allSpecs()
  const cats = categoryOrder.filter((c) => specs.some((s) => s.category === c))

  const toolbarBounds = (): ToolbarSafeBounds | null => {
    const surface = toolbarRef.current?.parentElement?.getBoundingClientRect()
    const toolbar = toolbarRef.current?.getBoundingClientRect()
    return surface && toolbar ? {
      left: screenToFlowPosition({ x: surface.left, y: surface.top }).x,
      top: screenToFlowPosition({ x: surface.left, y: surface.top }).y,
      right: screenToFlowPosition({ x: surface.right, y: surface.top }).x,
      // The shelf is part of the node's interaction footprint, so the toolbar itself is excluded.
      bottom: screenToFlowPosition({ x: surface.left, y: toolbar.top }).y,
    } : null
  }

  const safeToolbarPosition = (
    nodes: typeof doc.nodes,
    base: { x: number; y: number },
  ) => {
    const bounds = toolbarBounds()
    return bounds ? toolbarSafePosition(nodes, base, bounds) : base
  }

  const add = (kind: string) => {
    const c = screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 })
    const base = { x: c.x - 116, y: c.y - 40 }
    const pos = safeToolbarPosition(useStore.getState().doc.nodes, base)
    addNode(kind, pos)
    setOpen(null)
    setOperationFinderOpen(false)
  }

  const toolbarDensity = useToolbarDensity(toolbarRef)
  const labelsVisible = toolbarDensity !== 'icons'

  const locate = (id: string) => {
    const nodes = useStore.getState().doc.nodes
    if (!nodes.some((node) => node.id === id)) return
    select(id)
    setLocatorOpen(false)
    void locateNode(nodes, id, { setCenter, getZoom })
  }

  const canEdit = roleCanEdit(canvasRole)

  return (
    <>
      {!canEdit && (
        <div data-testid="view-only-badge" className="absolute bottom-[74px] left-1/2 z-[16] -translate-x-1/2 rounded-full border border-border bg-card px-3 py-1.5 text-[11.5px] font-medium text-muted-foreground shadow-sm">
          {canvasRole === 'viewer' ? 'View-only canvas' : 'Checking canvas access…'}
        </div>
      )}
      {doc.nodes.length > 0 && <CanvasViewportControls />}
      {canEdit && (
        <div ref={toolbarRef} data-testid="toolbar" data-density={toolbarDensity} className="absolute bottom-[22px] left-1/2 z-[16] -translate-x-1/2">
          <div className="flex max-w-[calc(100vw-24px)] items-center gap-1 rounded-2xl border border-border bg-card p-1.5 shadow-lg">
            <div data-testid="toolbar-add-controls" role="group" aria-label="Add controls" className="flex min-w-0 items-center gap-1">
              {labelsVisible && <span className="px-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Add</span>}
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

              <ToolbarIconButton label="Add operation" icon="plus" onClick={() => { setOpen(null); setLocatorOpen(false); setOperationFinderOpen(true) }} />
              <ToolbarIconButton label="Locate existing node" icon="search" onClick={() => { setOpen(null); setOperationFinderOpen(false); setLocatorOpen(true) }} />

              <Tooltip label={`Agent — ${agentOpen ? 'open' : 'closed'}`}>
                <button
                  type="button"
                  aria-pressed={agentOpen}
                  onClick={() => setAgentOpen(!agentOpen)}
                  className="inline-flex items-center gap-[7px] rounded-lg px-3.5 py-[7px] text-[12.5px] font-semibold"
                  // Agent brand accent (violet) — no design token expresses it; matches the AgentDock it opens.
                  style={{ background: agentOpen ? '#efeaff' : 'linear-gradient(180deg,#f3effe,#ece5fc)', color: '#6b4bd6' }}
                >
                  <Icon name="sparkle" size={14} /> Agent
                </button>
              </Tooltip>
            </div>
          </div>
        </div>
      )}
      {operationFinderOpen && <NodeFinder specs={specs} onPick={add} onClose={() => setOperationFinderOpen(false)} />}
      {locatorOpen && <ExistingNodeLocator nodes={doc.nodes} onPick={locate} onClose={() => setLocatorOpen(false)} />}
    </>
  )
}

// The Canvas region changes with the Inspector, so this threshold uses that region rather than
// the browser window. Narrow Canvases keep every global action but hide decorative group labels.
const LABELLED_TOOLBAR_MIN_WIDTH = 900
type ToolbarDensity = 'icons' | 'comfortable'

export function toolbarDensityForWidth(width: number): ToolbarDensity {
  if (width < LABELLED_TOOLBAR_MIN_WIDTH) return 'icons'
  return 'comfortable'
}

function useToolbarDensity(ref: RefObject<HTMLDivElement | null>) {
  const [regionWidth, setRegionWidth] = useState(0)

  useLayoutEffect(() => {
    const region = ref.current?.parentElement
    if (!region) return
    const update = () => setRegionWidth(region.clientWidth)
    update()
    const observer = new ResizeObserver(update)
    observer.observe(region)
    return () => observer.disconnect()
  }, [ref])

  return toolbarDensityForWidth(regionWidth)
}

export function CanvasViewportControls() {
  const { zoomIn, zoomOut, fitView } = useReactFlow()
  const { zoom } = useViewport()
  const nodeCount = useStore((s) => s.doc.nodes.length)

  return (
    <div
      data-testid="canvas-viewport-controls"
      role="group"
      aria-label="Viewport controls"
      className="absolute bottom-[80px] left-3 z-[16] flex items-center gap-1 rounded-xl border border-border bg-card p-1 shadow-sm"
    >
      <ToolbarIconButton label="Zoom in" icon="plus" onClick={() => { void zoomIn() }} disabled={zoom >= 2.5} />
      <ToolbarIconButton label="Zoom out" icon="minus" onClick={() => { void zoomOut() }} disabled={zoom <= 0.2} />
      <ToolbarIconButton label="Fit view" icon="maximize" onClick={() => { void fitView(canvasFitOptions(nodeCount)) }} />
    </div>
  )
}

function ToolbarIconButton({ label, tooltip = label, icon, onClick, disabled = false, pressed, showLabel = false }: {
  label: string
  tooltip?: string
  icon: IconName
  onClick: () => void
  disabled?: boolean
  pressed?: boolean
  showLabel?: boolean
}) {
  return (
    <Tooltip label={tooltip}>
      <button
        type="button"
        aria-label={label}
        aria-pressed={pressed}
        disabled={disabled}
        onClick={onClick}
        className={cn(
          'inline-flex h-[34px] w-[38px] items-center justify-center gap-1.5 rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50',
          showLabel && 'w-auto px-2.5',
          pressed && 'bg-accent text-foreground',
        )}
      >
        <Icon name={icon} size={16} />
        {showLabel && <span className="whitespace-nowrap text-[11.5px] font-medium">{label.replace(/^(Show|Hide) /, '')}</span>}
      </button>
    </Tooltip>
  )
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
          type="button"
          ref={ref}
          aria-label={CATEGORY_LABEL[cat]}
          aria-expanded={open}
          aria-pressed={open}
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
