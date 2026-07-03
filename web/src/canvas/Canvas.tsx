import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow, Background, BackgroundVariant, Controls, MiniMap,
  applyNodeChanges, applyEdgeChanges, useReactFlow,
  type Node, type Edge, type Connection, type NodeChange, type EdgeChange, type OnConnectStartParams,
} from '@xyflow/react'
import { buildNodeTypes } from '../nodes'
import { WireEdge } from '../wires/WireEdge'
import { canConnect, portWire, getSpec } from '../nodes/registry'
import { useStore, newId } from '../store/graph'
import { kindAccent, color } from '../theme/tokens'
import type { WireType } from '../theme/tokens'
import { ConnectMenu } from './ConnectMenu'
import { PanelHost } from '../panels/PanelHost'

const edgeTypes = { wire: WireEdge }

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

function EmptyState() {
  const { screenToFlowPosition } = useReactFlow()
  const addNode = useStore((s) => s.addNode)
  const setAgentOpen = useStore((s) => s.setAgentOpen)
  const add = () => {
    const c = screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 })
    addNode('source', { x: c.x - 116, y: c.y - 40 })
  }
  return (
    <div style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', pointerEvents: 'none' }}>
      <div style={{ textAlign: 'center', pointerEvents: 'auto' }}>
        <div style={{ fontSize: 15, fontWeight: 600, color: color.ink }}>Empty canvas</div>
        <div style={{ fontSize: 12.5, color: color.text3, marginTop: 6, maxWidth: 320, lineHeight: 1.5 }}>
          Add a dataset source to begin — then wire operators, preview on a sample, and run at scale.
        </div>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'center', marginTop: 14 }}>
          <button onClick={add} style={{ padding: '8px 16px', border: 'none', borderRadius: 9, background: color.ink, color: '#fff', fontSize: 12.5, fontWeight: 600 }}>+ Add a source</button>
          <button onClick={() => setAgentOpen(true)} style={{ padding: '8px 16px', border: `1px solid ${color.border}`, borderRadius: 9, background: '#fff', color: color.text2, fontSize: 12.5, fontWeight: 600 }}>Ask the Agent</button>
        </div>
      </div>
    </div>
  )
}

export function Canvas() {
  const specsVersion = useStore((s) => s.specsVersion)
  const nodeTypes = useMemo(() => buildNodeTypes(), [specsVersion])
  const doc = useStore((s) => s.doc)
  const selectedIds = useStore((s) => s.selectedIds)
  const setNodes = useStore((s) => s.setNodes)
  const setEdges = useStore((s) => s.setEdges)
  const connect = useStore((s) => s.connect)
  const select = useStore((s) => s.select)
  const removeSelected = useStore((s) => s.removeSelected)
  const bypass = useStore((s) => s.bypass)
  const mute = useStore((s) => s.mute)
  const { screenToFlowPosition } = useReactFlow()

  const connectStart = useRef<OnConnectStartParams | null>(null)
  const [menu, setMenu] = useState<{ x: number; y: number; wire: WireType; source: OnConnectStartParams } | null>(null)

  // React Flow needs to own node measurements (`measured`/width/height): if we rebuilt node
  // objects from the store every render we'd drop them, and RF keeps unmeasured nodes hidden.
  // So we keep a local RF-nodes state and reconcile from the store, preserving measured fields.
  const [rfNodes, setRfNodes] = useState<Node[]>([])
  useEffect(() => {
    setRfNodes((prev) => {
      const prevById = new Map(prev.map((n) => [n.id, n]))
      const sel = new Set(selectedIds)
      return doc.nodes.map((n) => {
        const p = prevById.get(n.id)
        return {
          id: n.id, type: n.type, position: n.position, data: n.data as any,
          // React Flow owns `selected` while it drives selection (click/shift/box); preserve it
          // across rebuilds so a mid-drag rubber-band isn't reset. New nodes seed from the store.
          selected: p ? p.selected : sel.has(n.id),
          ...(p ? { measured: p.measured, width: p.width, height: p.height } : {}),
        }
      })
    })
  }, [doc.nodes, selectedIds])

  const rfEdges: Edge[] = useMemo(
    () => doc.edges.map((e) => ({
      id: e.id, source: e.source, target: e.target,
      sourceHandle: e.sourceHandle ?? undefined, targetHandle: e.targetHandle ?? undefined,
      type: 'wire', data: e.data as any, markerEnd: 'dp-arrow',
    })),
    [doc.edges],
  )

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    // apply ALL changes locally (incl. dimensions/measured, so RF shows the nodes)
    setRfNodes((prev) => applyNodeChanges(changes, prev))
    // sync position changes back to the store (source of truth for persistence)
    const moved = changes.filter((c) => c.type === 'position' && (c as any).position) as any[]
    if (moved.length) {
      const byId = new Map(moved.map((c) => [c.id, c.position]))
      setNodes(useStore.getState().doc.nodes.map((n) => (byId.has(n.id) ? { ...n, position: byId.get(n.id) } : n)))
    }
    // fold select changes into the multi-selection set (box-select emits many)
    const selChanges = changes.filter((c) => c.type === 'select') as any[]
    if (selChanges.length) {
      const cur = new Set(useStore.getState().selectedIds)
      for (const c of selChanges) (c.selected ? cur.add(c.id) : cur.delete(c.id))
      useStore.getState().setSelection([...cur])
    }
  }, [setNodes])

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    const applied = applyEdgeChanges(changes, rfEdges)
    const keep = new Set(applied.map((e) => e.id))
    setEdges(doc.edges.filter((e) => keep.has(e.id)))
  }, [rfEdges, doc.edges, setEdges])

  const isValidConnection = useCallback((c: Connection | Edge) => {
    const sw = portWire(doc.nodes, c.source!, c.sourceHandle, 'source')
    const tgt = doc.nodes.find((n) => n.id === c.target)
    if (!tgt) return false
    return canConnect(sw, tgt.type, c.targetHandle)
  }, [doc.nodes])

  const onConnect = useCallback((c: Connection) => {
    if (!isValidConnection(c)) return
    const wire = (portWire(doc.nodes, c.source!, c.sourceHandle, 'source') ?? 'dataset') as WireType
    connect({
      id: newId('e'), source: c.source!, target: c.target!,
      sourceHandle: c.sourceHandle, targetHandle: c.targetHandle, data: { wire },
    })
  }, [isValidConnection, connect, doc.nodes])

  const onConnectStart = useCallback((_: any, params: OnConnectStartParams) => {
    connectStart.current = params
  }, [])

  const onConnectEnd = useCallback((event: MouseEvent | TouchEvent) => {
    const params = connectStart.current
    connectStart.current = null
    if (!params || params.handleType !== 'source') return
    const target = event.target as HTMLElement
    const droppedOnPane = target?.classList?.contains('react-flow__pane')
    if (!droppedOnPane) return
    const wire = portWire(doc.nodes, params.nodeId!, params.handleId, 'source')
    if (!wire) return
    const pt = 'clientX' in event ? { x: event.clientX, y: event.clientY } : { x: 0, y: 0 }
    setMenu({ x: pt.x, y: pt.y, wire: wire as WireType, source: params })
  }, [doc.nodes])

  // keyboard: Delete / Backspace remove selection; B bypass; M mute
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as HTMLElement)?.isContentEditable) return
      // undo / redo work regardless of selection
      if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z')) {
        e.preventDefault()
        if (e.shiftKey) useStore.getState().redo()
        else useStore.getState().undo()
        return
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === 'y' || e.key === 'Y')) { e.preventDefault(); useStore.getState().redo(); return }
      const ids = useStore.getState().selectedIds
      if (!ids.length) return
      if (e.key === 'Delete' || e.key === 'Backspace') { removeSelected(); e.preventDefault() }
      if (e.key === 'b' || e.key === 'B') {
        // honor canBypass (matches the ⋯ menu) — bypass only the selected nodes that allow it
        ids.forEach((id) => {
          const n = useStore.getState().doc.nodes.find((x) => x.id === id)
          if (n && getSpec(n.type)?.canBypass) bypass(id)
        })
      }
      if (e.key === 'm' || e.key === 'M') ids.forEach((id) => mute(id))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [removeSelected, bypass, mute])

  return (
    <div style={{ position: 'absolute', inset: 0 }}>
      <ArrowDefs />
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onConnectStart={onConnectStart}
        onConnectEnd={onConnectEnd}
        isValidConnection={isValidConnection}
        onPaneClick={() => { select(null); setMenu(null) }}
        onNodeClick={(e, n) => { if (!e.shiftKey && !e.metaKey && !e.ctrlKey) select(n.id) }}
        defaultEdgeOptions={{ type: 'wire' }}
        proOptions={{ hideAttribution: true }}
        minZoom={0.2}
        maxZoom={2.5}
        fitView
        fitViewOptions={{ padding: 0.3, maxZoom: 1 }}
        panOnScroll
        selectionOnDrag
        panOnDrag={[1, 2]}
        selectionKeyCode={null}
        multiSelectionKeyCode={['Meta', 'Shift']}
        deleteKeyCode={null}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1.4} color="#d6d9df" />
        <Controls showInteractive={false} position="bottom-left" style={{ marginBottom: 84 }} />
        <MiniMap
          pannable
          zoomable
          position="bottom-left"
          style={{ marginBottom: 140, width: 168, height: 108 }}
          maskColor="rgba(244,245,247,0.7)"
          nodeColor={(n) => kindAccent[n.type ?? ''] ?? color.text3}
          nodeStrokeWidth={0}
        />
      </ReactFlow>

      {doc.nodes.length === 0 && <EmptyState />}

      <PanelHost />

      {menu && (
        <ConnectMenu
          x={menu.x}
          y={menu.y}
          wire={menu.wire}
          onClose={() => setMenu(null)}
          onPick={(kind) => {
            const pos = screenToFlowPosition({ x: menu.x, y: menu.y })
            const node = useStore.getState().addNode(kind, { x: pos.x, y: pos.y })
            if (node) {
              const wire = menu.wire
              useStore.getState().connect({
                id: newId('e'), source: menu.source.nodeId!, target: node.id,
                sourceHandle: menu.source.handleId, targetHandle: null, data: { wire },
              })
            }
            setMenu(null)
          }}
        />
      )}
    </div>
  )
}
