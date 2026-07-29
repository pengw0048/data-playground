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
import { uniqueNextStepConnection } from './nextStep'
import { cn } from '@/lib/utils'
import { toolbarRevealDelta, toolbarSafePosition, type ToolbarSafeBounds } from './toolbarPlacement'
import { canvasFitOptions } from './viewportFit'

const CATEGORY_ICON: Record<Category, IconName> = {
  io: 'db', shape: 'sample', compute: 'fx', query: 'sql', inspect: 'note', control: 'code',
}
const CATEGORY_LABEL: Record<Category, string> = {
  io: 'Sources & sinks', shape: 'Shape', compute: 'Compute', query: 'Query', inspect: 'Inspect', control: 'Control flow',
}

// Bottom toolbar — auto-populated from the node registry, grouped by category (FR-C2a).
export function Toolbar({ inspectorCollapsed, onInspectorToggle }: {
  inspectorCollapsed: boolean
  onInspectorToggle: () => void
}) {
  const { screenToFlowPosition, setCenter, getZoom } = useReactFlow()
  const doc = useStore((s) => s.doc)
  const selectedIds = useStore((s) => s.selectedIds)
  const addNode = useStore((s) => s.addNode)
  const addConnectedNode = useStore((s) => s.addConnectedNode)
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

  const selectedNode = selectedIds.length === 1
    ? doc.nodes.find((node) => node.id === selectedIds[0]) ?? null
    : null
  const nextStepKinds = new Set(selectedNode
    ? specs.filter((spec) => uniqueNextStepConnection(selectedNode, spec.kind, doc.edges)).map((spec) => spec.kind)
    : [])
  const nextStepSource = nextStepKinds.size ? selectedNode : null
  const toolbarDensity = useToolbarDensity(toolbarRef, !!nextStepSource)
  const labelsVisible = toolbarDensity !== 'icons'
  const compact = toolbarDensity === 'compact'
  const addNext = (kind: string, asNextStep?: boolean) => {
    if (!asNextStep) { add(kind); return }
    // Re-evaluate at activation time: a remote edit or a changed selection must never turn the
    // explicit next-step affordance into a guessed connection.
    const current = useStore.getState().doc
    const currentSelection = useStore.getState().selectedIds
    const source = currentSelection.length === 1
      ? current.nodes.find((node) => node.id === currentSelection[0]) ?? null
      : null
    const connection = source && uniqueNextStepConnection(source, kind, current.edges)
    if (!source || !connection) return
    const bounds = toolbarBounds()
    const pos = bounds ? toolbarSafePosition(
      current.nodes,
      { x: source.position.x + 300, y: source.position.y },
      bounds,
    ) : { x: source.position.x + 300, y: source.position.y }
    const added = addConnectedNode(kind, pos, { source: source.id, ...connection })
    const surface = toolbarRef.current?.parentElement?.getBoundingClientRect()
    const reveal = added?.data.autoPlaced && bounds ? toolbarRevealDelta(added.position, bounds) : null
    if (surface && reveal) {
      const center = screenToFlowPosition({
        x: surface.left + surface.width / 2,
        y: surface.top + surface.height / 2,
      })
      void setCenter(center.x + reveal.x, center.y + reveal.y, { zoom: getZoom(), duration: 0 })
    }
    setOperationFinderOpen(false)
  }

  const locate = (id: string) => {
    select(id)
    if (locateNode(useStore.getState().doc.nodes, id, { setCenter, getZoom })) setLocatorOpen(false)
  }

  const canEdit = roleCanEdit(canvasRole)

  return (
    <>
      {!canEdit && (
        <div data-testid="view-only-badge" className="absolute bottom-[74px] left-1/2 z-[16] -translate-x-1/2 rounded-full border border-border bg-card px-3 py-1.5 text-[11.5px] font-medium text-muted-foreground shadow-sm">
          {canvasRole === 'viewer' ? 'View-only canvas' : 'Checking canvas access…'}
        </div>
      )}
      <div ref={toolbarRef} data-testid="toolbar" data-density={toolbarDensity} className="absolute bottom-[22px] left-1/2 z-[16] -translate-x-1/2">
        <div className={cn(
          'flex max-w-[calc(100vw-24px)] items-center rounded-2xl border border-border bg-card shadow-lg',
          compact ? 'gap-0.5 p-1' : 'gap-1 p-1.5',
        )}>
          {canEdit && (
            <div data-testid="toolbar-add-controls" role="group" aria-label="Add controls" className={cn('flex min-w-0 items-center', compact ? 'gap-0.5' : 'gap-1')}>
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

              <div className={cn('h-[22px] w-px bg-border', compact ? 'mx-0.5' : 'mx-1')} />

              <ToolbarIconButton label={nextStepSource ? 'Add next step' : 'Add operation'} icon="plus" showLabel={!!nextStepSource} onClick={() => { setOpen(null); setLocatorOpen(false); setOperationFinderOpen(true) }} />
              <ToolbarIconButton label="Locate existing node" icon="search" onClick={() => { setOpen(null); setOperationFinderOpen(false); setLocatorOpen(true) }} />

              <Tooltip label={`Agent — ${agentOpen ? 'open' : 'closed'}`}>
                <button
                  type="button"
                  aria-pressed={agentOpen}
                  onClick={() => setAgentOpen(!agentOpen)}
                  className={cn(
                    'inline-flex items-center gap-[7px] rounded-lg py-[7px] text-[12.5px] font-semibold',
                    compact ? 'px-2.5' : 'px-3.5',
                  )}
                  // Agent brand accent (violet) — no design token expresses it; matches the AgentDock it opens.
                  style={{ background: agentOpen ? '#efeaff' : 'linear-gradient(180deg,#f3effe,#ece5fc)', color: '#6b4bd6' }}
                >
                  <Icon name="sparkle" size={14} /> Agent
                </button>
              </Tooltip>
            </div>
          )}

          {canEdit && <div aria-hidden className={cn('h-[22px] w-px bg-border', compact ? 'mx-0.5' : 'mx-1')} />}
          <CanvasViewControls
            inspectorCollapsed={inspectorCollapsed}
            onInspectorToggle={onInspectorToggle}
            hasNodes={doc.nodes.length > 0}
            labelsVisible={labelsVisible}
          />
        </div>
      </div>
      {operationFinderOpen && nextStepSource && <NodeFinder
        specs={specs}
        nextStepKinds={nextStepKinds}
        nextStepLabel={nextStepSource.data.title}
        onPick={addNext}
        onClose={() => setOperationFinderOpen(false)}
      />}
      {operationFinderOpen && !nextStepSource && <NodeFinder specs={specs} onPick={add} onClose={() => setOperationFinderOpen(false)} />}
      {locatorOpen && <ExistingNodeLocator nodes={doc.nodes} onPick={locate} onClose={() => setLocatorOpen(false)} />}
    </>
  )
}

// The ordinary labelled toolbar measures about 860px with the current registry. A contextual
// Add-next-step label needs a compact labelled density at the 980px Canvas region created by either
// a 1280px viewport with its Inspector open or a 1024px viewport with it collapsed. The Canvas
// region changes with the Inspector, so these thresholds use that region, not the browser window.
const LABELLED_TOOLBAR_MIN_WIDTH = 900
const COMFORTABLE_NEXT_STEP_TOOLBAR_MIN_WIDTH = 1024
type ToolbarDensity = 'icons' | 'compact' | 'comfortable'

export function toolbarDensityForWidth(width: number, hasNextStepLabel: boolean): ToolbarDensity {
  if (width < LABELLED_TOOLBAR_MIN_WIDTH) return 'icons'
  if (hasNextStepLabel && width < COMFORTABLE_NEXT_STEP_TOOLBAR_MIN_WIDTH) return 'compact'
  return 'comfortable'
}

function useToolbarDensity(ref: RefObject<HTMLDivElement | null>, hasNextStepLabel: boolean) {
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

  // Selection can expose Add next step without changing the region width. Deriving here prevents
  // one paint with the previous (wider) density before an effect catches up.
  return toolbarDensityForWidth(regionWidth, hasNextStepLabel)
}

export function CanvasViewControls({ inspectorCollapsed, onInspectorToggle, hasNodes, labelsVisible = false }: {
  inspectorCollapsed: boolean
  onInspectorToggle: () => void
  hasNodes: boolean
  labelsVisible?: boolean
}) {
  const { zoomIn, zoomOut, fitView } = useReactFlow()
  const { zoom } = useViewport()
  const nodeCount = useStore((s) => s.doc.nodes.length)

  return (
    <div data-testid="toolbar-view-controls" role="group" aria-label="View controls" className="flex items-center gap-1">
      {labelsVisible && <span className="px-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">View</span>}
      {hasNodes && <div role="group" aria-label="Viewport controls" className="flex items-center gap-1">
        <ToolbarIconButton label="Zoom in" icon="plus" onClick={() => { void zoomIn() }} disabled={zoom >= 2.5} showLabel={labelsVisible} />
        <ToolbarIconButton label="Zoom out" icon="minus" onClick={() => { void zoomOut() }} disabled={zoom <= 0.2} showLabel={labelsVisible} />
        <ToolbarIconButton label="Fit view" icon="maximize" onClick={() => { void fitView(canvasFitOptions(nodeCount)) }} showLabel={labelsVisible} />
      </div>}
      {hasNodes && <div aria-hidden className="mx-1 h-[22px] w-px bg-border" />}
      <div role="group" aria-label="Panel controls" className="flex items-center gap-1">
        <ToolbarIconButton
          label={inspectorCollapsed ? 'Show Inspector' : 'Hide Inspector'}
          tooltip={`Inspector — ${inspectorCollapsed ? 'hidden' : 'shown'}`}
          icon="eye"
          onClick={onInspectorToggle}
          pressed={!inspectorCollapsed}
          showLabel={labelsVisible}
        />
      </div>
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
      <Tooltip label={`${CATEGORY_LABEL[cat]} — ${open ? 'expanded' : 'collapsed'}`}>
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
