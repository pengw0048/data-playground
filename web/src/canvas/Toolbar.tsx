import { useEffect, useRef, useState, type RefObject } from 'react'
import { useReactFlow, useViewport } from '@xyflow/react'
import { allSpecs } from '../nodes'
import { useStore, freePosition, roleCanEdit } from '../store/graph'
import { categoryOrder, color, kindAccent, type Category } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { Tooltip } from '../ui/Tooltip'
import { Popover } from '../ui/Popover'
import { NodeFinder } from './NodeFinder'
import { ExistingNodeLocator } from './ExistingNodeLocator'
import { locateNode } from './locateNode'
import { cn } from '@/lib/utils'

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
  const addNode = useStore((s) => s.addNode)
  const select = useStore((s) => s.select)
  const setAgentOpen = useStore((s) => s.setAgentOpen)
  const agentOpen = useStore((s) => s.agentOpen)
  const canvasRole = useStore((s) => s.canvasRole)
  const [open, setOpen] = useState<Category | null>(null)
  const [operationFinderOpen, setOperationFinderOpen] = useState(false)
  const [locatorOpen, setLocatorOpen] = useState(false)
  const toolbarRef = useRef<HTMLDivElement>(null)
  const labelsVisible = useToolbarLabels(toolbarRef)

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
      <div ref={toolbarRef} data-testid="toolbar" className="absolute bottom-[22px] left-1/2 z-[16] -translate-x-1/2">
        <div className="flex max-w-[calc(100vw-24px)] items-center gap-1 rounded-2xl border border-border bg-card p-1.5 shadow-lg">
          {canEdit && (
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
          )}

          {canEdit && <div aria-hidden className="mx-1 h-[22px] w-px bg-border" />}
          <CanvasViewControls
            inspectorCollapsed={inspectorCollapsed}
            onInspectorToggle={onInspectorToggle}
            hasNodes={doc.nodes.length > 0}
            labelsVisible={labelsVisible}
          />
        </div>
      </div>
      {operationFinderOpen && <NodeFinder specs={specs} onPick={add} onClose={() => setOperationFinderOpen(false)} />}
      {locatorOpen && <ExistingNodeLocator nodes={doc.nodes} onPick={locate} onClose={() => setLocatorOpen(false)} />}
    </>
  )
}

// The labelled toolbar measures about 860px with the current registry; leave a little room for its
// centered position and borders. The Canvas region changes width when the Inspector opens, so this
// must be based on that region instead of the browser window.
const LABELLED_TOOLBAR_MIN_WIDTH = 900

function useToolbarLabels(ref: RefObject<HTMLDivElement | null>) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    const region = ref.current?.parentElement
    if (!region) return
    const update = () => setVisible(region.clientWidth >= LABELLED_TOOLBAR_MIN_WIDTH)
    update()
    const observer = new ResizeObserver(update)
    observer.observe(region)
    return () => observer.disconnect()
  }, [ref])

  return visible
}

export function CanvasViewControls({ inspectorCollapsed, onInspectorToggle, hasNodes, labelsVisible = false }: {
  inspectorCollapsed: boolean
  onInspectorToggle: () => void
  hasNodes: boolean
  labelsVisible?: boolean
}) {
  const { zoomIn, zoomOut, fitView } = useReactFlow()
  const { zoom } = useViewport()

  return (
    <div data-testid="toolbar-view-controls" role="group" aria-label="View controls" className="flex items-center gap-1">
      {labelsVisible && <span className="px-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">View</span>}
      {hasNodes && <div role="group" aria-label="Viewport controls" className="flex items-center gap-1">
        <ToolbarIconButton label="Zoom in" icon="plus" onClick={() => { void zoomIn() }} disabled={zoom >= 2.5} showLabel={labelsVisible} />
        <ToolbarIconButton label="Zoom out" icon="minus" onClick={() => { void zoomOut() }} disabled={zoom <= 0.2} showLabel={labelsVisible} />
        <ToolbarIconButton label="Fit view" icon="maximize" onClick={() => { void fitView({ padding: 0.3, maxZoom: 1 }) }} showLabel={labelsVisible} />
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
