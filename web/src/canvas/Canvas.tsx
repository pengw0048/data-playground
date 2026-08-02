import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow, Background, BackgroundVariant, MiniMap,
  applyNodeChanges, useNodesInitialized, useReactFlow,
  useStore as useReactFlowStore,
  type Node, type Edge, type Connection, type NodeChange, type EdgeChange, type OnConnectEnd,
} from '@xyflow/react'
import { allSpecs, buildNodeTypes } from '../nodes'
import { SECTION_W, SECTION_H } from '../nodes/kinds/section'
import { WireEdge } from '../wires/WireEdge'
import { canConnect, firstCompatibleInput, getSpec, portWire, portMulti } from '../nodes/registry'
import { schemaWarnings } from '../nodes/schema'
import {
  canvasViewportDocumentIdentity, currentPreviews, useStore, newId, freePosition, roleCanEdit,
} from '../store/graph'
import { api } from '../api/client'
import { examples } from '../examples'
import { categoryOrder, kindAccent, color } from '../theme/tokens'
import type { Category, WireType } from '../theme/tokens'
import type { CanvasNode } from '../types/graph'
import { NodeFinder, type ScreenRect } from './NodeFinder'
import { NodeTypeIcon } from './NodeTypeIcon'
import { PanelHost } from '../panels/PanelHost'
import { PeerCursors } from './PeerCursors'
import { connectCollab, disconnectCollab, sendCursor } from '../collab/collab'
import { Button } from '@/components/ui/button'
import { Icon } from '../ui/Icon'
import { absoluteNodePosition, locateNode } from './locateNode'
import { useExampleCreationIntent } from './useExampleCreationIntent'
import { cycleConnectionReason, cycleGestureReason } from './connectionCycle'
import { canvasFitOptions } from './viewportFit'
import { requestSourceEntryAction, type SourceEntryAction } from '../nodes/kinds/source'
import {
  ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuLabel,
  ContextMenuSeparator, ContextMenuShortcut, ContextMenuSub, ContextMenuSubContent,
  ContextMenuSubTrigger, ContextMenuTrigger,
} from '@/components/ui/context-menu'

const edgeTypes = { wire: WireEdge }

const CONTEXT_CATEGORY_LABEL: Record<Category, string> = {
  io: 'Sources & sinks', shape: 'Shape', compute: 'Compute', query: 'Query',
  inspect: 'Inspect', control: 'Control flow',
}

function viewportNodeGeometryIdentity(nodes: readonly Node[]): string {
  return JSON.stringify(nodes.map((node) => [
    node.id, node.type ?? null, node.parentId ?? null, node.position.x, node.position.y,
  ]))
}

function revealNodeGroup(
  doc: CanvasNode[],
  rfNodes: readonly Node[],
  nodeIds: readonly string[],
  surface: DOMRect,
  viewport: { setCenter: (x: number, y: number, options: { zoom: number, duration: number }) => Promise<boolean> },
): Promise<boolean> | null {
  const measured = new Map(rfNodes.map((node) => [node.id, node]))
  const nodes = nodeIds.map((id) => doc.find((node) => node.id === id))
  if (nodes.some((node) => !node)) return null
  const bounds = (nodes as CanvasNode[]).map((node) => {
    const measuredNode = measured.get(node.id)
    const width = measuredNode?.measured?.width ?? measuredNode?.width
    const height = measuredNode?.measured?.height ?? measuredNode?.height
    if (!width || !height) return null
    const position = absoluteNodePosition(doc, node)
    return { x: position.x, y: position.y, width, height }
  })
  if (bounds.some((bound) => bound === null)) return null
  const boxes = bounds as Array<{ x: number, y: number, width: number, height: number }>
  const left = Math.min(...boxes.map((box) => box.x))
  const top = Math.min(...boxes.map((box) => box.y))
  const right = Math.max(...boxes.map((box) => box.x + box.width))
  const bottom = Math.max(...boxes.map((box) => box.y + box.height))
  // Keep a newly confirmed pair clear of the fixed top bar, bottom toolbar and bottom-left minimap.
  // The Canvas surface already excludes the Inspector, so this remains responsive when it is collapsed.
  const safe = { left: 196, top: 96, right: surface.width - 16, bottom: surface.height - 208 }
  const width = safe.right - safe.left
  const height = safe.bottom - safe.top
  if (width <= 0 || height <= 0) return null
  const zoom = Math.max(0.2, Math.min(1, width / (right - left + 64), height / (bottom - top + 64)))
  const safeCenterX = (safe.left + safe.right) / 2
  const safeCenterY = (safe.top + safe.bottom) / 2
  return viewport.setCenter(
    (left + right) / 2 + (surface.width / 2 - safeCenterX) / zoom,
    (top + bottom) / 2 + (surface.height / 2 - safeCenterY) / zoom,
    { zoom, duration: 350 },
  ).then(() => true)
}

// Directional arrowheads for the wires (open chevrons). Defined once; referenced by id from
// every edge path. `userSpaceOnUse` keeps them a constant size regardless of stroke width.
function ArrowDefs() {
  const marker = (id: string, stroke: string) => (
    <marker id={id} markerWidth="14" markerHeight="14" refX="8.5" refY="5" orient="auto" markerUnits="userSpaceOnUse">
      <path d="M1.5,1.5 L9,5 L1.5,8.5" fill="none" stroke={stroke} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </marker>
  )
  return (
    <svg style={{ position: 'absolute', width: 0, height: 0 }} aria-hidden>
      <defs>
        {marker('dp-arrow', color.wire)}
        {marker('dp-arrow-active', color.wireActive)}
        {marker('dp-arrow-sel', '#7f8792')}
      </defs>
    </svg>
  )
}

function EmptyState({ canEdit }: { canEdit: boolean }) {
  const { screenToFlowPosition } = useReactFlow()
  const addNode = useStore((s) => s.addNode)
  const setAgentOpen = useStore((s) => s.setAgentOpen)
  const newFromExample = useStore((s) => s.newFromExample)
  // gate the Agent CTA on a configured model — otherwise the most prominent first-run button leads
  // straight to "Agent unavailable" (the default is no model).
  const [agentOk, setAgentOk] = useState(false)
  const exampleIntent = useExampleCreationIntent(canEdit)
  const exampleCreatesSeparate = exampleIntent === 'create-separate'
  useEffect(() => {
    if (!canEdit) return
    api.agentStatus().then((s) => setAgentOk(!!s.available)).catch(() => setAgentOk(false))
  }, [canEdit])
  const add = (action: SourceEntryAction) => {
    const c = screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 })
    const node = addNode('source', { x: c.x - 116, y: c.y - 40 })
    if (node) requestSourceEntryAction(node.id, action)
  }
  return (
    <div className="pointer-events-none absolute inset-0 grid place-items-center">
      <div className="pointer-events-auto text-center">
        <div className="text-[15px] font-semibold text-foreground">Empty canvas</div>
        <div className="mx-auto mt-1.5 max-w-[340px] text-[12.5px] leading-normal text-muted-foreground">
          {canEdit ? 'Add a dataset source to begin — or open a runnable example.' : 'You have view-only access to this canvas.'}
        </div>
        {canEdit && (
          <>
            <div className="mt-3.5 flex flex-wrap justify-center gap-2">
              <Button variant="outline" onClick={() => add('select')} className="rounded-lg text-[12.5px] text-muted-foreground">Choose dataset</Button>
              <Button variant="outline" onClick={() => add('upload')} className="rounded-lg text-[12.5px] text-muted-foreground">Upload file</Button>
              <Button variant="outline" onClick={() => add('browse')} className="rounded-lg text-[12.5px] text-muted-foreground">Register path or URL</Button>
              {agentOk && <Button variant="outline" onClick={() => setAgentOpen(true)} className="rounded-lg text-[12.5px] text-muted-foreground">Ask the Agent</Button>}
            </div>
            <div className="mt-2 text-[10.5px] text-muted-foreground/80">
              Or drop a Parquet, CSV, JSON, or Arrow file here.
            </div>
          </>
        )}
        {/* runnable starters on the seeded data — a first-timer never opens the file menu to find them */}
        {canEdit && <div className="mx-auto mt-6 grid max-w-[460px] gap-2">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.6px] text-muted-foreground/70">Start from an example</div>
          {examples.map((ex) => (
            <button key={ex.key} onClick={() => { void newFromExample(ex.key, exampleIntent) }} title={ex.blurb}
              aria-label={exampleCreatesSeparate ? `Create example Canvas: ${ex.name}` : `Use example in this Canvas: ${ex.name}`}
              className="rounded-lg border border-border bg-card px-3 py-2 text-left transition-colors hover:border-primary/50 hover:bg-accent">
              <div className="text-[12px] font-semibold text-foreground">{ex.name}</div>
              <div className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-muted-foreground">{ex.blurb}</div>
            </button>
          ))}
        </div>}
      </div>
    </div>
  )
}

export function Canvas() {
  const specsVersion = useStore((s) => s.specsVersion)
  const nodeTypes = useMemo(() => buildNodeTypes(), [specsVersion])
  const doc = useStore((s) => s.doc)
  const canvasRole = useStore((s) => s.canvasRole)
  const canEdit = roleCanEdit(canvasRole)
  const schemas = useStore((s) => s.schemas)
  const previews = useStore((s) => s.previews)
  const catalog = useStore((s) => s.catalog)
  const selectedIds = useStore((s) => s.selectedIds)
  const canUndo = useStore((s) => s.past.length > 0)
  const canRedo = useStore((s) => s.future.length > 0)
  const nodeRevealRequest = useStore((s) => s.nodeRevealRequest)
  const acknowledgeNodeReveal = useStore((s) => s.acknowledgeNodeReveal)
  const viewportFitRequest = useStore((s) => s.viewportFitRequest)
  const acknowledgeViewportFit = useStore((s) => s.acknowledgeViewportFit)
  const setNodes = useStore((s) => s.setNodes)
  const connect = useStore((s) => s.connect)
  const reconnectEdge = useStore((s) => s.reconnectEdge)
  const removeEdge = useStore((s) => s.removeEdge)
  const setParent = useStore((s) => s.setParent)
  const select = useStore((s) => s.select)
  const removeSelected = useStore((s) => s.removeSelected)
  const bypass = useStore((s) => s.bypass)
  const disable = useStore((s) => s.disable)
  const { screenToFlowPosition, setCenter, getZoom, fitView, viewportInitialized } = useReactFlow()
  const canvasRef = useRef<HTMLDivElement>(null)
  const flowWidth = useReactFlowStore((state) => state.width)
  const flowHeight = useReactFlowStore((state) => state.height)
  const internalNodeGeometryIdentity = useReactFlowStore(
    (state) => viewportNodeGeometryIdentity(state.nodes),
  )
  const nodesInitialized = useNodesInitialized()
  const revealedRequestId = useRef<number | null>(null)
  const fittedRequestId = useRef<number | null>(null)
  // React Flow validates a reconnect before calling `onReconnect`. Keep the edge being moved here
  // so the validator can exclude it from the target-port and cycle checks.
  const reconnectingEdgeId = useRef<string | null>(null)

  // realtime collaboration: join this canvas's presence room; leave on switch/unmount
  const docId = doc.id
  useEffect(() => {
    connectCollab(docId)
    return () => disconnectCollab()
  }, [docId])

  const [finder, setFinder] = useState<{
    anchor: ScreenRect
    boundary: ScreenRect
    opener: HTMLElement
    wire: WireType
    source: { nodeId: string; handleId: string | null }
  } | null>(null)
  const [contextPosition, setContextPosition] = useState({ x: 0, y: 0 })

  // Drag a data file from the OS onto the canvas → upload it and drop a bound source node where it landed.
  const [dropActive, setDropActive] = useState(false)
  const onDragOverFiles = useCallback((e: React.DragEvent) => {
    if (!canEdit) return
    if (!Array.from(e.dataTransfer?.types ?? []).includes('Files')) return  // ignore node/text drags
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
    setDropActive(true)
  }, [canEdit])
  const onDragLeaveFiles = useCallback((e: React.DragEvent) => {
    if (!e.currentTarget.contains(e.relatedTarget as HTMLElement | null)) setDropActive(false)  // not a child-to-child move
  }, [])
  const onDropFiles = useCallback(async (e: React.DragEvent) => {
    if (!canEdit) return
    const files = Array.from(e.dataTransfer?.files ?? [])
    if (!files.length) return
    e.preventDefault()
    setDropActive(false)
    const base = screenToFlowPosition({ x: e.clientX, y: e.clientY })
    const s = useStore.getState()
    if (!s.kernelUp) { s.pushToast('Kernel offline — cannot upload a file', 'error'); return }
    for (const file of files) {
      const t = await s.uploadDataset(file)  // uploads + refreshes the catalog; toasts on failure
      if (!t) continue
      const g = useStore.getState()
      const pos = freePosition(g.doc.nodes, { x: base.x - 116, y: base.y - 40 })
      g.addNode(
        'source',
        pos,
        { uri: t.uri, tableId: t.id, registrationId: t.registrationId ?? undefined },
        t.name,
        { autoPlaced: false },
      )
    }
  }, [canEdit, screenToFlowPosition])

  // React Flow needs to own node measurements (`measured`/width/height): if we rebuilt node
  // objects from the store every render we'd drop them, and RF keeps unmeasured nodes hidden.
  // So we keep a local RF-nodes state and reconcile from the store, preserving measured fields.
  const [rfNodes, setRfNodes] = useState<Node[]>([])
  useEffect(() => {
    setRfNodes((prev) => {
      const prevById = new Map(prev.map((n) => [n.id, n]))
      const sel = new Set(selectedIds)
      const mapped = doc.nodes.map((n) => {
        const p = prevById.get(n.id)
        return {
          id: n.id, type: n.type,
          // keep the LIVE position of a node React Flow is currently dragging — the store now holds only
          // its pre-drag value (positions commit on drag stop), so a remote peer edit arriving mid-drag
          // must not snap the dragged node back to that stale value.
          position: p?.dragging ? p.position : n.position, data: n.data as any,
          // a section is a sized container; its contained nodes render inside it (parentId), clamped
          ...(n.type === 'section' ? { style: { width: SECTION_W, height: SECTION_H } } : {}),
          ...(n.parentId ? { parentId: n.parentId } : {}), // no extent:'parent' so a child can be dragged back out to detach
          // The store is the selection source of truth. This also keeps a programmatic selection
          // (for example, locating an off-screen node) visibly selected in React Flow.
          selected: sel.has(n.id),
          ...(p ? { measured: p.measured, width: p.width, height: p.height } : {}),
        }
      })
      // React Flow requires a parent to precede its children in the array
      return [...mapped.filter((n) => !n.parentId), ...mapped.filter((n) => n.parentId)]
    })
  }, [doc.nodes, selectedIds])

  // React Flow's fitView prop applies only at mount (when a first-run Canvas is still empty). A
  // successful example or saved Canvas open therefore carries one explicit, document-bound request.
  // Consume it only after every card is measured; the request is then gone, so later renders and
  // user pan/zoom remain entirely presentation-local.
  useEffect(() => {
    if (!viewportFitRequest || viewportFitRequest.canvasId !== doc.id
        || fittedRequestId.current === viewportFitRequest.id) return
    // A shareable node= route deliberately owns the initial viewport. This also cancels a normal
    // open's request if the URL changes to a deep link while React Flow is still mounting.
    if (nodeRevealRequest?.canvasId === doc.id) {
      acknowledgeViewportFit(viewportFitRequest.id)
      return
    }
    if (viewportFitRequest.documentIdentity !== canvasViewportDocumentIdentity(doc)) {
      acknowledgeViewportFit(viewportFitRequest.id)
      return
    }
    // A locally recovered document can arrive synchronously while React Flow is mounting. Its
    // cards may already report dimensions before the pan/zoom instance exists; fitView queued in
    // that window cannot move the viewport. Wait for the library's explicit readiness signal.
    if (!viewportInitialized || !nodesInitialized) return
    // On a document switch the prior document's measured RF nodes can briefly have the same count.
    // Never fit that stale geometry while the reconcile effect is replacing it with the new document.
    const docNodeIds = new Set(doc.nodes.map((node) => node.id))
    if (rfNodes.length !== doc.nodes.length || rfNodes.length === 0
        || rfNodes.some((node) => !docNodeIds.has(node.id))) return
    if (internalNodeGeometryIdentity !== viewportNodeGeometryIdentity(rfNodes)) return
    const measured = rfNodes.every((node) => {
      const width = node.measured?.width ?? node.width
      const height = node.measured?.height ?? node.height
      return typeof width === 'number' && width > 0 && typeof height === 'number' && height > 0
    })
    if (!measured) return
    const requestId = viewportFitRequest.id
    fittedRequestId.current = requestId
    void fitView(canvasFitOptions(doc.nodes.length)).then((fitted) => {
      if (fitted) acknowledgeViewportFit(requestId)
      else if (fittedRequestId.current === requestId) fittedRequestId.current = null
    })
  }, [
    viewportFitRequest, nodeRevealRequest, doc, rfNodes, internalNodeGeometryIdentity,
    viewportInitialized, nodesInitialized, fitView, acknowledgeViewportFit,
  ])

  // An explicit reveal is intentionally distinct from normal selection: a route or a newly
  // connected step may own the viewport, while an ordinary click must never seize it. Wait for
  // React Flow to mount the requested card, then consume this exact request once.
  useEffect(() => {
    if (!nodeRevealRequest || nodeRevealRequest.canvasId !== doc.id
        || revealedRequestId.current === nodeRevealRequest.id) return
    const nodeIds = nodeRevealRequest.nodeIds ?? [nodeRevealRequest.nodeId]
    const mounted = Array.from(document.querySelectorAll<HTMLElement>('.react-flow__node'))
    if (!nodeIds.every((nodeId) => mounted.some((element) => element.dataset.id === nodeId))) return
    const surface = canvasRef.current?.getBoundingClientRect()
    // A route selection can mount the Inspector and shrink the flexed Canvas in the same commit.
    // Wait for React Flow's ResizeObserver to accept that real size before centering; otherwise it
    // uses the previous full-width viewport and leaves the selected card underneath the Inspector.
    if (!surface || Math.abs(flowWidth - surface.width) > 0.5 || Math.abs(flowHeight - surface.height) > 0.5) return
    const consumeAfterReveal = (completion: Promise<boolean>) => {
      const requestId = nodeRevealRequest.id
      revealedRequestId.current = requestId
      // Keep the request observable until React Flow finishes the animated viewport transition.
      // A following coordinate-driven gesture can then wait on completion rather than guess a delay.
      void completion.then(
        () => acknowledgeNodeReveal(requestId),
        () => acknowledgeNodeReveal(requestId),
      )
    }
    if (nodeIds.length > 1) {
      if (!viewportInitialized) return
      const completion = revealNodeGroup(doc.nodes, rfNodes, nodeIds, surface, { setCenter })
      if (completion) consumeAfterReveal(completion)
      return
    }
    consumeAfterReveal(locateNode(doc.nodes, nodeRevealRequest.nodeId, { setCenter, getZoom }))
  }, [
    nodeRevealRequest, doc.id, doc.nodes, rfNodes, flowWidth, flowHeight,
    viewportInitialized, setCenter, getZoom, acknowledgeNodeReveal,
  ])

  // nodes whose config references a column absent from their (known) input — drives the amber wire cue.
  // Keyed by a stable membership string so warnedIds only changes IDENTITY when the set actually changes
  // → rfEdges (and every WireEdge) don't rebuild on an unrelated keystroke.
  const warnedKey = useMemo(() => {
    const boundPreviews = currentPreviews(doc, previews)
    const ids: string[] = []
    for (const n of doc.nodes) if (schemaWarnings(doc, schemas, boundPreviews, catalog, n.id).length) ids.push(n.id)
    return ids.sort().join(',')
  }, [doc, schemas, previews, catalog])
  const warnedIds = useMemo(() => new Set(warnedKey ? warnedKey.split(',') : []), [warnedKey])

  const rfEdges: Edge[] = useMemo(
    () => doc.edges.map((e) => ({
      id: e.id, source: e.source, target: e.target,
      sourceHandle: e.sourceHandle ?? undefined, targetHandle: e.targetHandle ?? undefined,
      // Match node selection: React Flow receives a controlled edge list, so selection must be
      // reflected from the store or an edge click is lost on the next render.
      selected: selectedIds.includes(e.id),
      type: 'wire', data: { ...(e.data as any), warned: warnedIds.has(e.target) }, markerEnd: 'dp-arrow',
    })),
    [doc.edges, selectedIds, warnedIds],
  )

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    // apply ALL changes locally (incl. dimensions/measured, so RF shows the nodes)
    setRfNodes((prev) => applyNodeChanges(changes, prev))
    // sync position changes to the store (persistence source of truth) ONLY when settled — skip the
    // per-frame `dragging:true` changes. Otherwise every mousemove rewrites the whole doc.nodes array,
    // re-runs the reconcile effect (rebuilding ALL RF nodes, O(n)/frame) and floods the collab socket.
    // RF emits the final position with dragging:false at release; non-drag moves have it unset too.
    const moved = changes.filter((c) => c.type === 'position' && (c as any).position && !(c as any).dragging) as any[]
    if (canEdit && moved.length) {
      const byId = new Map(moved.map((c) => [c.id, c.position]))
      // snapshot the pre-move doc so a settled drag is its OWN undo step (setNodes doesn't commit);
      // also marks a CRDT boundary so undo behaves the same solo and while co-editing.
      useStore.getState().commit()
      setNodes(useStore.getState().doc.nodes.map((n) => (byId.has(n.id) ? { ...n, position: byId.get(n.id) } : n)))
    }
    // fold select changes into the multi-selection set (box-select emits many)
    const selChanges = changes.filter((c) => c.type === 'select') as any[]
    if (selChanges.length) {
      const cur = new Set(useStore.getState().selectedIds)
      for (const c of selChanges) (c.selected ? cur.add(c.id) : cur.delete(c.id))
      useStore.getState().setSelection([...cur])
    }
  }, [canEdit, setNodes])

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    if (!canEdit) return
    // Edge clicks emit selection changes, just like nodes. Keep their ids in the common selection
    // state so the existing Delete handler can remove either kind of graph element in one action.
    const selected = changes.filter((change) => change.type === 'select') as Array<{ id: string; selected: boolean }>
    if (!selected.length) return
    const ids = new Set(useStore.getState().selectedIds)
    for (const change of selected) (change.selected ? ids.add(change.id) : ids.delete(change.id))
    useStore.getState().setSelection([...ids])
  }, [canEdit])

  const isValidConnection = useCallback((c: Connection | Edge) => {
    if (!canEdit) return false
    if (cycleConnectionReason(doc.edges, c, reconnectingEdgeId.current)) return false
    const sw = portWire(doc.nodes, c.source!, c.sourceHandle, 'source')
    const tgt = doc.nodes.find((n) => n.id === c.target)
    if (!tgt) return false
    if (!canConnect(sw, tgt.type, c.targetHandle)) return false
    // one edge per input port (each handle is single-input; join has separate a/b handles) — unless the
    // port is `multi` (union), which stacks many incoming edges on the same handle.
    if (portMulti(tgt.type, c.targetHandle)) return true
    const occupied = doc.edges.some((e) => e.id !== reconnectingEdgeId.current
      && e.target === c.target && (e.targetHandle ?? null) === (c.targetHandle ?? null))
    return !occupied
  }, [canEdit, doc.nodes, doc.edges])

  // `isValidConnection` only returns a boolean. React Flow gives us the rejected endpoints at the
  // end of a drag, which lets the product explain why the connection did not persist.
  const onConnectEnd = useCallback<OnConnectEnd>((_event, state) => {
    if (state.isValid !== false || !state.fromNode || !state.toNode) return
    const reason = cycleGestureReason(doc.edges, state, reconnectingEdgeId.current)
    if (reason) useStore.getState().pushToast(reason, 'error')
  }, [doc.edges])

  const onConnect = useCallback((c: Connection) => {
    if (!isValidConnection(c)) return
    const wire = (portWire(doc.nodes, c.source!, c.sourceHandle, 'source') ?? 'dataset') as WireType
    connect({
      id: newId('e'), source: c.source!, target: c.target!,
      sourceHandle: c.sourceHandle, targetHandle: c.targetHandle, data: { wire },
    })
  }, [isValidConnection, connect, doc.nodes])

  // Reroute a wire by dragging an endpoint to a new port (RF onReconnect). Validate the new target
  // ignoring the edge being moved (so re-dropping on its own port isn't a false "occupied"), then
  // swap it. Without this a wire could only be changed by deleting a node.
  const onReconnect = useCallback((oldEdge: Edge, c: Connection) => {
    if (!canEdit) return
    if (cycleConnectionReason(doc.edges, c, oldEdge.id)) return
    const sw = portWire(doc.nodes, c.source!, c.sourceHandle, 'source')
    const tgt = doc.nodes.find((n) => n.id === c.target)
    if (!tgt || !canConnect(sw, tgt.type, c.targetHandle)) return
    const occupied = !portMulti(tgt.type, c.targetHandle) && doc.edges.some((e) => e.id !== oldEdge.id
      && e.target === c.target && (e.targetHandle ?? null) === (c.targetHandle ?? null))
    if (occupied) return
    reconnectEdge(oldEdge.id, { id: oldEdge.id, source: c.source!, target: c.target!,
      sourceHandle: c.sourceHandle, targetHandle: c.targetHandle, data: { wire: (sw ?? 'dataset') as WireType } })
  }, [canEdit, doc.nodes, doc.edges, reconnectEdge])

  // Dropping a node onto a section makes it a contained child (parentId); dragging it out detaches
  // it. Coordinates convert between absolute (top-level) and relative-to-section on the boundary.
  const onNodeDragStop = useCallback((e: MouseEvent | TouchEvent, dragged: Node) => {
    if (!canEdit) return
    // This callback is an actual pointer drag release; React Flow's generic position changes also
    // cover initialization/reconciliation and cannot safely claim presentation ownership.
    useStore.getState().updateData(dragged.id, { autoPlaced: false })
    // visual drag-containment is one level: section frames are a fixed size, so a same-size section
    // can't sit cleanly inside another. Nested logic is expressed in the driver script instead — a
    // section's script can run() another section (the engine carries the nested subtree; see
    // kernel/section.py run()/_descendants and test_section_nests_multiple_levels_by_parentid).
    if (dragged.type === 'section') return
    const nodes = useStore.getState().doc.nodes
    const cur = nodes.find((n) => n.id === dragged.id)
    const curParent = cur?.parentId ?? null
    // contained if the dragged node's rendered box overlaps a section's rendered box (both read
    // fresh at drop time in screen space) — robust to zoom/pan and to nodes re-laying-out mid-drag.
    const dr = document.querySelector(`.react-flow__node[data-id="${dragged.id}"]`)?.getBoundingClientRect()
    const hit = dr && nodes.find((n) => {
      if (n.type !== 'section' || n.id === dragged.id) return false
      const r = document.querySelector(`.react-flow__node[data-id="${n.id}"]`)?.getBoundingClientRect()
      return !!r && dr.left < r.right && dr.right > r.left && dr.top < r.bottom && dr.bottom > r.top
    })
    // the dragged node's absolute position (its stored position is relative if currently parented)
    const parent = curParent ? nodes.find((n) => n.id === curParent) : null
    const abs = parent ? { x: parent.position.x + dragged.position.x, y: parent.position.y + dragged.position.y } : { x: dragged.position.x, y: dragged.position.y }
    if (hit) {
      if (curParent !== hit.id) setParent(dragged.id, hit.id, { x: abs.x - hit.position.x, y: abs.y - hit.position.y })
    } else if (curParent) {
      setParent(dragged.id, null, abs) // dragged out → back to absolute top-level coords
    }
  }, [canEdit, setParent])

  // A CLICK on an output port opens the shared compatible-node picker (Port dispatches this event). A drag to
  // connect never fires a click on the origin handle, so pulling a wire never pops the picker.
  useEffect(() => {
    const onPortClick = (ev: Event) => {
      if (!canEdit) return
      const { nodeId, handleId, anchor, opener } = (ev as CustomEvent).detail as {
        nodeId: string
        handleId: string
        anchor: ScreenRect
        opener: HTMLElement
      }
      // Resolve compatibility at activation time. A newly added node can paint before this effect
      // is refreshed with the next graph snapshot; reading the store avoids dropping a fast click
      // or key press against an otherwise visible port.
      const wire = portWire(useStore.getState().doc.nodes, nodeId, handleId, 'source')
      const surface = canvasRef.current?.getBoundingClientRect()
      if (!wire || !surface) return
      const toolbarTop = document.querySelector<HTMLElement>('[data-testid="toolbar"]')?.getBoundingClientRect().top
      const boundary = {
        left: surface.left,
        right: surface.right,
        top: surface.top,
        bottom: toolbarTop == null ? surface.bottom : Math.min(surface.bottom, toolbarTop - 8),
      }
      setFinder({ anchor, boundary, opener, wire: wire as WireType, source: { nodeId, handleId } })
    }
    window.addEventListener('dp-port-click', onPortClick)
    return () => window.removeEventListener('dp-port-click', onPortClick)
  }, [canEdit])

  // keyboard: Delete / Backspace remove selection; B bypass; M mute
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // a fullscreen code editor / any open modal sits over the canvas — its own Esc handling wins;
      // don't let Delete/b/d/Esc act on (or wipe) the canvas beneath it
      if (useStore.getState().fullscreenCode) return
      if (document.querySelector('.dp-modal-overlay')) return
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as HTMLElement)?.isContentEditable) return
      const editable = roleCanEdit(useStore.getState().canvasRole)
      // undo / redo work regardless of selection
      if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z')) {
        e.preventDefault()
        if (!editable) return
        if (e.shiftKey) useStore.getState().redo()
        else useStore.getState().undo()
        return
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === 'y' || e.key === 'Y')) { e.preventDefault(); if (editable) useStore.getState().redo(); return }
      // clipboard + selection (work on the canvas, not in a field — inputs bailed out above)
      if (e.metaKey || e.ctrlKey) {
        const k = e.key.toLowerCase()
        if (k === 'a') { e.preventDefault(); useStore.getState().selectAll(); return }
        if (k === 'c') { e.preventDefault(); useStore.getState().copySelection(); return }
        if (k === 'x') { e.preventDefault(); if (editable) useStore.getState().cutSelection(); return }
        if (k === 'v') { e.preventDefault(); if (editable) useStore.getState().paste(); return }
        if (k === 'd') { e.preventDefault(); if (editable) useStore.getState().duplicateSelected(); return }
      }
      // Escape closes any open floating panel (data viewer / run / …) and clears the selection
      if (e.key === 'Escape') {
        if (Object.keys(useStore.getState().openPanels).length) useStore.setState({ openPanels: {} })
        else useStore.getState().select(null)
        return
      }
      const ids = useStore.getState().selectedIds
      if (!ids.length) return
      if (!editable) return
      if (e.key === 'Delete' || e.key === 'Backspace') { removeSelected(); e.preventDefault() }
      if (e.key === 'b' || e.key === 'B') {
        // honor canBypass (matches the ⋯ menu) — bypass only the selected nodes that allow it
        ids.forEach((id) => {
          const n = useStore.getState().doc.nodes.find((x) => x.id === id)
          if (n && getSpec(n.type)?.canBypass) bypass(id)
        })
      }
      if (e.key === 'd' || e.key === 'D') ids.forEach((id) => disable(id))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [removeSelected, bypass, disable])

  const addNodeAtContext = (kind: string) => {
    if (!canEdit) return
    const base = { x: contextPosition.x - 116, y: contextPosition.y - 40 }
    const position = freePosition(useStore.getState().doc.nodes, base)
    useStore.getState().addNode(kind, position, undefined, undefined, { autoPlaced: false })
  }
  const contextSpecs = allSpecs()

  return (
    <ContextMenu>
    <ContextMenuTrigger asChild>
    <div ref={canvasRef}
      data-node-reveal-pending={nodeRevealRequest?.canvasId === doc.id ? 'true' : 'false'}
      style={{ position: 'absolute', inset: 0 }}
      onContextMenu={(event) => {
        setContextPosition(screenToFlowPosition({ x: event.clientX, y: event.clientY }))
        select(null)
        setFinder(null)
      }}
      onMouseMove={(e) => { const p = screenToFlowPosition({ x: e.clientX, y: e.clientY }); sendCursor(p.x, p.y) }}
      onDragOver={onDragOverFiles} onDragLeave={onDragLeaveFiles} onDrop={onDropFiles}>
      <ArrowDefs />
      <PeerCursors />
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onConnectEnd={onConnectEnd}
        onReconnect={onReconnect}
        onReconnectStart={(_, edge) => { reconnectingEdgeId.current = edge.id }}
        onReconnectEnd={() => { reconnectingEdgeId.current = null }}
        onEdgeDoubleClick={(_, edge) => { if (canEdit) removeEdge(edge.id) }}
        onNodeDragStop={onNodeDragStop}
        isValidConnection={isValidConnection}
        nodesDraggable={canEdit}
        nodesConnectable={canEdit}
        edgesReconnectable={canEdit}
        // Explicit keyboard a11y: Tab cycles focusable nodes; Enter/Space selects the focused node.
        // Focus ring is styled in index.css (.react-flow__node:focus-visible). Edges stay focusable
        // too (library default) so wire selection matches mouse selection via keyboard.
        nodesFocusable
        edgesFocusable
        disableKeyboardA11y={false}
        onPaneClick={() => { select(null); setFinder(null); useStore.setState({ openPanels: {} }) }}
        onNodeClick={(e, n) => { if (!e.shiftKey && !e.metaKey && !e.ctrlKey) select(n.id) }}
        defaultEdgeOptions={{ type: 'wire' }}
        proOptions={{ hideAttribution: true }}
        minZoom={0.2}
        maxZoom={2.5}
        fitView
        fitViewOptions={canvasFitOptions(doc.nodes.length)}
        panOnScroll
        connectOnClick={false}
        selectionOnDrag
        // Keep right-click available for the product menu; middle-drag still pans the Canvas.
        panOnDrag={[1]}
        selectionKeyCode={null}
        multiSelectionKeyCode={['Meta', 'Shift']}
        deleteKeyCode={null}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1.4} color="var(--dots)" />  {/* themed: light/dark via --dots */}
        {/* Keep the minimap only once there's something to navigate — on an empty canvas it would
            just be a stray box over the first-run prompt. */}
        {doc.nodes.length > 0 && (
          <>
            {/* MiniMap paints to a 2D canvas where CSS vars don't resolve, so maskColor + the nodeColor
                fallback are literals (a theme-neutral gray veil; not the now-var color.text3). */}
            <MiniMap
              pannable
              position="bottom-left"
              // Leave room for the icon-only viewport controls directly below the minimap.
              style={{ marginBottom: 132, marginLeft: 12, width: 168, height: 108 }}
              maskColor="rgba(128,128,128,0.2)"
              nodeColor={(n) => kindAccent[n.type ?? ''] ?? '#98a0ac'}
              nodeStrokeWidth={0}
              onClick={(_, pos) => setCenter(pos.x, pos.y, { zoom: getZoom(), duration: 350 })}
            />
          </>
        )}
      </ReactFlow>

      {doc.nodes.length === 0 && <EmptyState canEdit={canEdit} />}

      {/* file-drop affordance — pointer-events:none so it never intercepts the drop itself */}
      {canEdit && dropActive && (
        <div className="pointer-events-none absolute inset-3 z-50 grid place-items-center rounded-xl border-2 border-dashed border-primary bg-primary/5">
          <div className="rounded-lg bg-card px-4 py-2.5 text-[13px] font-semibold text-foreground shadow-lg">
            Drop to upload as a source · Parquet / CSV / JSON / Arrow
          </div>
        </div>
      )}

      <PanelHost />

      {canEdit && finder && (
        <NodeFinder
          specs={allSpecs()}
          wire={finder.wire}
          compatibleOnly
          anchor={finder.anchor}
          boundary={finder.boundary}
          returnFocus={finder.opener}
          onClose={() => setFinder(null)}
          onPick={(kind) => {
            const target = firstCompatibleInput(kind, finder.wire)
            // Keep the graph contract at the creation boundary as well as in the picker. This prevents
            // a stale plugin registry update from creating an incompatible unconnected node.
            if (target) {
              const p = screenToFlowPosition({
                x: finder.anchor.right,
                y: (finder.anchor.top + finder.anchor.bottom) / 2,
              })
              const pos = freePosition(useStore.getState().doc.nodes, { x: p.x + 60, y: p.y - 20 })
              const created = useStore.getState().addConnectedNode(kind, pos, {
                source: finder.source.nodeId, sourceHandle: finder.source.handleId ?? '',
                targetHandle: target.id, wire: finder.wire,
              })
              // Selection opens the Inspector and narrows the Canvas. Reveal only after that real
              // surface resize, so a consecutive rightward insertion cannot land underneath it.
              if (created) {
                const current = useStore.getState()
                current.requestNodeReveal(current.doc.id, created.id)
              }
            }
            setFinder(null)
          }}
        />
      )}
    </div>
    </ContextMenuTrigger>
    <ContextMenuContent aria-label="Canvas actions" className="w-[220px]">
      <ContextMenuLabel>Canvas</ContextMenuLabel>
      {canEdit && <>
        <ContextMenuItem disabled={!canUndo} onSelect={() => useStore.getState().undo()}>
          <Icon name="undo" /> Undo <ContextMenuShortcut>⌘Z</ContextMenuShortcut>
        </ContextMenuItem>
        <ContextMenuItem disabled={!canRedo} onSelect={() => useStore.getState().redo()}>
          <Icon name="redo" /> Redo <ContextMenuShortcut>⇧⌘Z</ContextMenuShortcut>
        </ContextMenuItem>
        <ContextMenuSeparator />
        {categoryOrder.map((category) => {
          const specs = contextSpecs.filter((spec) => spec.category === category)
          if (!specs.length) return null
          return <ContextMenuSub key={category}>
            <ContextMenuSubTrigger>Add {CONTEXT_CATEGORY_LABEL[category]}</ContextMenuSubTrigger>
            <ContextMenuSubContent className="max-h-80 w-56 overflow-y-auto">
              {specs.map((spec) => <ContextMenuItem key={spec.kind} onSelect={() => addNodeAtContext(spec.kind)}>
                <span aria-hidden="true" className="grid h-5 w-5 shrink-0 place-items-center rounded border border-border bg-card text-muted-foreground">
                  <NodeTypeIcon spec={spec} size={12} />
                </span>
                <span className="min-w-0 truncate">{spec.title}</span>
              </ContextMenuItem>)}
            </ContextMenuSubContent>
          </ContextMenuSub>
        })}
        <ContextMenuItem onSelect={() => useStore.getState().paste()}>
          <Icon name="duplicate" /> Paste <ContextMenuShortcut>⌘V</ContextMenuShortcut>
        </ContextMenuItem>
      </>}
      <ContextMenuItem disabled={!doc.nodes.length} onSelect={() => useStore.getState().selectAll()}>
        <Icon name="check" /> Select all <ContextMenuShortcut>⌘A</ContextMenuShortcut>
      </ContextMenuItem>
      <ContextMenuSeparator />
      <ContextMenuItem disabled={!doc.nodes.length} onSelect={() => { void fitView(canvasFitOptions(doc.nodes.length)) }}>
        <Icon name="maximize" /> Fit canvas
      </ContextMenuItem>
    </ContextMenuContent>
    </ContextMenu>
  )
}
