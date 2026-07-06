import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ReactFlow, Background, BackgroundVariant, Controls, Handle, Position, MarkerType,
  type Node, type Edge, type Connection, type NodeChange,
} from '@xyflow/react'
import { useStore } from '../store/graph'
import { api } from '../api/client'
import { resolvedTheme } from '../theme/mode'
import type { CatalogTable, Relationship } from '../types/api'
import { cn } from '@/lib/utils'

// The ER / "UML" view: every catalog dataset is an entity (columns, primary key badged); declared
// relationships are solid edges labelled with their cardinality, and name-based candidate joins are
// dashed hints. Click a column to declare its primary key; drag between two tables to declare a join
// (its keys + measured cardinality come from the join-suggestion engine); click a solid edge to drop it.

type EntityData = { table: CatalogTable; pk: string[]; onToggle: (col: string) => void }

function EntityNode({ data }: { data: EntityData }) {
  const { table, pk, onToggle } = data
  return (
    <div className="w-[240px] overflow-hidden rounded-lg border border-border bg-card shadow-sm">
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-primary" />
      <div className="truncate border-b border-border bg-muted px-3 py-1.5 text-[12px] font-semibold text-foreground">{table.name}</div>
      <div className="flex max-h-[220px] flex-col overflow-y-auto py-1">
        {table.columns.map((c) => {
          const isPk = pk.includes(c.name)
          const isKey = c.capabilities?.includes('key')
          return (
            <button key={c.name} onClick={() => onToggle(c.name)}
              title={isPk ? 'declared primary key — click to clear' : 'click to declare as the primary key'}
              className={cn('flex items-center gap-1.5 px-3 py-0.5 text-left text-[11px] hover:bg-accent', isPk && 'font-semibold text-foreground')}>
              <span className="w-3 text-center text-[10px]">{isPk ? '🔑' : isKey ? '·' : ''}</span>
              <span className="dp-mono flex-1 truncate">{c.name}</span>
              <span className="text-[9.5px] text-muted-foreground">{c.type}</span>
            </button>
          )
        })}
      </div>
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-primary" />
    </div>
  )
}

const nodeTypes = { entity: EntityNode }

const _POS_KEY = 'dp-er-positions'
function loadPositions(): Record<string, { x: number; y: number }> {
  try { return JSON.parse(localStorage.getItem(_POS_KEY) || '{}') } catch { return {} }
}
function savePositions(p: Record<string, { x: number; y: number }>): void {
  try { localStorage.setItem(_POS_KEY, JSON.stringify(p)) } catch { /* storage full / disabled — layout just won't persist */ }
}

function keyColsLower(t: CatalogTable): string[] {
  return t.columns.filter((c) => c.capabilities?.includes('key')).map((c) => c.name.toLowerCase())
}

// cheap client-side "these could plausibly join": a shared NON-generic key name (e.g. both have
// `user_id`), or an FK-style `id` <-> `<thing>_id` match. Deliberately NOT bare-`id` <-> bare-`id`:
// every table has a surrogate `id`, so matching those would draw a complete-graph hairball of
// meaningless hints. A real join needs a qualified signal.
const BARE_KEYS = ['id', 'uuid', 'guid', 'pk']
function sharesKey(a: CatalogTable, b: CatalogTable): boolean {
  const ka = keyColsLower(a), kb = keyColsLower(b)
  const fk = (xs: string[], ys: string[]) => xs.some((x) => BARE_KEYS.includes(x) && ys.some((y) => y.endsWith('_' + x)))
  const sharedNonBare = ka.some((x) => !BARE_KEYS.includes(x) && kb.includes(x))
  return sharedNonBare || fk(ka, kb) || fk(kb, ka)
}

export function ERDiagram() {
  const catalog = useStore((s) => s.catalog)
  const refreshCatalog = useStore((s) => s.refreshCatalog)
  const pushToast = useStore((s) => s.pushToast)
  const [rels, setRels] = useState<Relationship[]>([])
  // node layout survives navigation (the view unmounts) via localStorage, keyed by table id
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>(loadPositions)

  useEffect(() => {
    api.relationships().then(setRels).catch(() => setRels([]))
    refreshCatalog()  // show current keys (another view / instance may have changed them)
  }, [refreshCatalog])

  const byUri = useMemo(() => Object.fromEntries(catalog.map((t) => [t.uri, t.id])), [catalog])
  const pkOf = (t: CatalogTable) => t.keys?.find((k) => k.confidence === 'declared')?.columns ?? []

  // clicking a column toggles its membership in the declared primary key — so clicking several
  // columns builds a COMPOSITE key (in click order); clicking a member again removes it.
  const toggleCol = useCallback(async (tableId: string, col: string) => {
    const t = catalog.find((x) => x.id === tableId)
    if (!t) return
    const cur = t.keys?.find((k) => k.confidence === 'declared')?.columns ?? []
    const next = cur.includes(col) ? cur.filter((c) => c !== col) : [...cur, col]
    try {
      await api.declareKey(tableId, next)
      await refreshCatalog()
    } catch (e) { pushToast(String((e as Error).message || e), 'error') }
  }, [catalog, refreshCatalog, pushToast])

  const nodes: Node[] = useMemo(() => catalog.map((t, i) => ({
    id: t.id, type: 'entity',
    position: positions[t.id] ?? { x: (i % 3) * 300, y: Math.floor(i / 3) * 300 },
    data: { table: t, pk: pkOf(t), onToggle: (col: string) => toggleCol(t.id, col) } satisfies EntityData,
  })), [catalog, positions, toggleCol])

  const edges: Edge[] = useMemo(() => {
    const out: Edge[] = []
    const declared = new Set<string>()
    rels.forEach((r, i) => {
      const s = byUri[r.leftUri], t = byUri[r.rightUri]
      if (!s || !t) return
      declared.add([s, t].sort().join('|'))
      out.push({
        id: `d${i}`, source: s, target: t,
        label: `${r.leftColumns.join('+')} → ${r.rightColumns.join('+')}  ${r.cardinality}`,
        labelStyle: { fontSize: 9.5 }, markerEnd: { type: MarkerType.ArrowClosed },
        style: { stroke: 'var(--primary)', strokeWidth: 1.5 }, data: { rel: r },
      })
    })
    for (let a = 0; a < catalog.length; a++)
      for (let b = a + 1; b < catalog.length; b++) {
        const ta = catalog[a], tb = catalog[b]
        if (declared.has([ta.id, tb.id].sort().join('|')) || !sharesKey(ta, tb)) continue
        out.push({
          id: `c-${ta.id}-${tb.id}`, source: ta.id, target: tb.id, selectable: false,
          style: { stroke: 'var(--muted-foreground)', strokeDasharray: '4 3', opacity: 0.45 },
        })
      }
    return out
  }, [rels, catalog, byUri])

  const onConnect = useCallback(async (c: Connection) => {
    const s = catalog.find((t) => t.id === c.source), t = catalog.find((x) => x.id === c.target)
    if (!s || !t || s.id === t.id) return
    try {
      const top = (await api.joinSuggestions(s.uri, t.uri))[0]
      if (!top) { pushToast('no matching key columns between these tables', 'error'); return }
      setRels(await api.addRelationship({
        leftUri: s.uri, leftColumns: top.leftColumns, rightUri: t.uri, rightColumns: top.rightColumns,
        cardinality: top.cardinality, confidence: 'declared',
      }))
      pushToast(`declared ${s.name} → ${t.name} (${top.cardinality})`, 'success')
    } catch (e) { pushToast(String((e as Error).message || e), 'error') }
  }, [catalog, pushToast])

  const onEdgeClick = useCallback(async (_e: React.MouseEvent, edge: Edge) => {
    const rel = (edge.data as { rel?: Relationship } | undefined)?.rel
    if (!rel || !window.confirm(`Remove declared relationship ${rel.leftColumns.join('+')} = ${rel.rightColumns.join('+')}?`)) return
    try { setRels(await api.deleteRelationship(rel)) } catch (e) { pushToast(String((e as Error).message || e), 'error') }
  }, [pushToast])

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setPositions((p) => {
      const next = { ...p }
      for (const ch of changes) if (ch.type === 'position' && ch.position) next[ch.id] = ch.position
      savePositions(next)
      return next
    })
  }, [])

  if (catalog.length === 0)
    return <div className="grid h-full place-items-center text-[13px] text-muted-foreground">No datasets registered yet — add some in Tables.</div>

  return (
    <div className="relative h-full w-full">
      <div className="pointer-events-none absolute left-3 top-3 z-10 max-w-[320px] rounded-md border border-border bg-card/90 px-3 py-2 text-[10.5px] leading-relaxed text-muted-foreground backdrop-blur">
        <div className="mb-0.5 text-[12px] font-semibold text-foreground">Relationships (ER)</div>
        🔑 click column(s) to declare the primary key (click several for a composite key) · drag between two tables to declare a join (keys + cardinality measured) · click a solid edge to remove · dashed = possible join
      </div>
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange}
        onConnect={onConnect} onEdgeClick={onEdgeClick} fitView minZoom={0.2} colorMode={resolvedTheme()}
        proOptions={{ hideAttribution: true }}>
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--dots)" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
