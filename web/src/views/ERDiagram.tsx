import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import {
  ReactFlow, Background, BackgroundVariant, ControlButton, Controls, Handle, Position, MarkerType,
  useReactFlow, useViewport,
  type Node, type Edge, type Connection, type NodeChange,
} from '@xyflow/react'
import { useStore } from '../store/graph'
import { api } from '../api/client'
import { resolvedTheme } from '../theme/mode'
import { MiniSelect } from '../ui/controls'
import { Icon } from '../ui/Icon'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { CatalogTable, Relationship, JoinSuggestion, Cardinality, LineageEdge } from '../types/api'
import { cn } from '@/lib/utils'
import { FieldEvidenceButton } from '../components/FieldEvidenceDetail'
import { ConfirmationDialog } from '../components/ConfirmationDialog'

// The relationship graph: entities are catalog datasets, declared joins are solid edges labelled with
// cardinality. It opens FOCUSED on one table (reached from a table's detail drawer) and shows that
// table plus its neighbours within N hops; "Show all" widens to the whole catalog (capped). A second
// mode swaps the join graph for the data-lineage (provenance) graph. Primary keys are declared in the
// table drawer, so entity columns here are read-only.

type EntityData = {
  table: CatalogTable
  fields: EntityField[]
  focused: boolean
  lineage: boolean
  expanded: boolean
  opening: boolean
  onFocus: () => void
  onOpen: () => void
}

type EntityField = {
  name: string
  type?: string
  role: 'PK' | 'FK' | 'KEY' | 'mapped' | 'field'
  column?: CatalogTable['columns'][number]
}

export function EntityNode({ data }: { data: EntityData }) {
  const { table, fields, focused, lineage, expanded, opening, onFocus, onOpen } = data
  const activate = lineage ? onOpen : onFocus
  return (
    <div data-testid="er-entity" className={cn(
      'group w-[244px] overflow-hidden rounded-xl border bg-card shadow-sm transition-[border-color,box-shadow]',
      focused ? 'border-primary ring-2 ring-primary/20' : 'border-border hover:border-primary/70 hover:shadow-md',
    )}>
      <Handle id="node-target" type="target" position={Position.Left}
        className={cn('!h-2 !w-2 !border-0', lineage ? '!bg-muted-foreground' : '!bg-primary')} />
      <button onClick={activate} disabled={opening}
        aria-label={lineage
          ? `${focused ? 'Back to' : 'Open'} dataset ${table.name}`
          : `Focus graph on ${table.name}`}
        title={lineage ? undefined : 'Focus the graph on this table'}
        className={cn(
          'nodrag flex w-full min-w-0 items-center gap-2 px-3 py-2.5 text-left hover:bg-accent disabled:cursor-wait',
          expanded && fields.length > 0 && 'border-b border-border',
        )}>
        <span className={cn(
          'grid h-7 w-7 shrink-0 place-items-center rounded-lg',
          focused ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground',
        )}><Icon name={lineage ? 'db' : 'lineage'} size={14} /></span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[12.5px] font-semibold text-foreground" title={table.name}>{table.name}</span>
          <span className="block text-[10px] text-muted-foreground">
            {opening ? 'Opening…' : focused ? 'Current dataset' : 'Dataset'}
          </span>
        </span>
      </button>
      {expanded && fields.length > 0 && <div className="flex max-h-[144px] flex-col overflow-x-hidden overflow-y-auto py-1">
        {fields.map((field) => <div key={field.name} className="relative flex min-h-6 items-center gap-1.5 px-3 py-0.5 text-left text-[11px]">
          <Handle id={`column-in:${field.name}`} type="target" position={Position.Left}
            className="!h-1.5 !w-1.5 !border-0 !bg-primary" />
          {field.role === 'field'
            ? <span aria-hidden className="w-12 shrink-0" />
            : <span className={cn(
                'w-12 shrink-0 rounded px-1 text-center text-[8px] font-bold uppercase tracking-wide',
                field.role === 'PK' ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground',
              )} data-testid={`er-field-role:${table.id}:${field.name}`}>{field.role}</span>}
          {field.column
            ? <FieldEvidenceButton column={field.column} marker className="dp-mono min-w-0 flex-1 truncate rounded px-0.5 text-left hover:bg-accent" />
            : <span className="dp-mono min-w-0 flex-1 truncate" title={field.name}>{field.name}</span>}
          {field.type && <span className="max-w-16 truncate text-[9.5px] text-muted-foreground">{field.type}</span>}
          <Handle id={`column-out:${field.name}`} type="source" position={Position.Right}
            className="!h-1.5 !w-1.5 !border-0 !bg-primary" />
        </div>)}
      </div>}
      <Handle id="node-source" type="source" position={Position.Right}
        className={cn('!h-2 !w-2 !border-0', lineage ? '!bg-muted-foreground' : '!bg-primary')} />
    </div>
  )
}

const nodeTypes = { entity: EntityNode }

// The catalog rail is outside React Flow, but these controls are overlays inside its pane. Reserve
// the whole top strip rather than only the panel's left corner so every entity title remains
// directly usable after fitting. The left inset also keeps nodes clear of the viewport buttons.
const ER_FIT_PADDING = { top: '164px', right: '16px', bottom: '16px', left: '344px' } as const
const ER_FIT_OPTIONS = { padding: ER_FIT_PADDING, maxZoom: 1 }
const LINEAGE_FIT_OPTIONS = {
  padding: { top: '168px', right: '32px', bottom: '32px', left: '32px' },
  maxZoom: 1,
} as const

function ERViewportControls({ fitKey, container, lineage, onZoomChange }: {
  fitKey: string
  container: RefObject<HTMLDivElement | null>
  lineage: boolean
  onZoomChange: (zoom: number) => void
}) {
  const { fitView } = useReactFlow()
  const { zoom } = useViewport()
  const [size, setSize] = useState({ width: 0, height: 0 })
  const fitSafely = useCallback(async () => {
    await fitView(lineage ? LINEAGE_FIT_OPTIONS : ER_FIT_OPTIONS)
  }, [fitView, lineage])

  // `useViewport` follows manual controls and programmatic fits. React Flow's `onMove` callback can
  // miss a mount-time fit, which otherwise leaves a one-node graph compact until the user nudges it.
  useEffect(() => { onZoomChange(zoom) }, [onZoomChange, zoom])

  // A relationship query can replace the node set after React Flow's mount-only `fitView` has
  // already run. Reapply the same safe fit after React Flow has laid out the replacement query and
  // whenever the pane resizes.
  useEffect(() => {
    if (size.width === 0 || size.height === 0) return
    const frame = requestAnimationFrame(() => { void fitSafely() })
    return () => cancelAnimationFrame(frame)
  }, [fitKey, fitSafely, size])

  useEffect(() => {
    const element = container.current
    if (!element) return
    const updateSize = ({ width, height }: { width: number; height: number }) => {
      setSize((current) => current.width === width && current.height === height ? current : { width, height })
    }
    const { width, height } = element.getBoundingClientRect()
    updateSize({ width, height })
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(([entry]) => {
      updateSize(entry.contentRect)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [container])

  return (
    <Controls showInteractive={false} showFitView={false}>
      <ControlButton className="react-flow__controls-fitview" onClick={() => { void fitSafely() }} title="Fit view" aria-label="Fit view">
        <Icon name="maximize" size={15} />
      </ControlButton>
    </Controls>
  )
}

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
// `user_id`), or an FK-style `id` <-> `<thing>_id` match. Deliberately NOT bare-`id` <-> bare-`id`.
const BARE_KEYS = ['id', 'uuid', 'guid', 'pk']
function sharesKey(a: CatalogTable, b: CatalogTable): boolean {
  const ka = keyColsLower(a), kb = keyColsLower(b)
  const fk = (xs: string[], ys: string[]) => xs.some((x) => BARE_KEYS.includes(x) && ys.some((y) => y.endsWith('_' + x)))
  const sharedNonBare = ka.some((x) => !BARE_KEYS.includes(x) && kb.includes(x))
  return sharedNonBare || fk(ka, kb) || fk(kb, ka)
}

// BFS the declared-relationship graph from a root uri out to `hops`, returning every reachable uri
// (root included). This is dagster's `+table+`: the neighbourhood, not the whole catalog.
function joinNeighbourhood(rootUri: string, rels: Relationship[], hops: number): string[] {
  const adj = new Map<string, Set<string>>()
  const link = (a: string, b: string) => { (adj.get(a) ?? adj.set(a, new Set()).get(a)!).add(b) }
  for (const r of rels) { link(r.leftUri, r.rightUri); link(r.rightUri, r.leftUri) }
  const seen = new Set([rootUri])
  let frontier = [rootUri]
  for (let h = 0; h < hops; h++) {
    const next: string[] = []
    for (const u of frontier) for (const v of adj.get(u) ?? []) if (!seen.has(v)) { seen.add(v); next.push(v) }
    frontier = next
    if (!frontier.length) break
  }
  return [...seen]
}

function lineageLayout(
  tables: CatalogTable[],
  edges: LineageEdge[],
  rootUri: string | null,
): Record<string, { x: number; y: number }> {
  if (!rootUri) return {}
  const rank = new Map<string, number>([[rootUri, 0]])
  for (let pass = 0; pass < tables.length; pass += 1) {
    let changed = false
    for (const edge of edges) {
      const parent = rank.get(edge.parent)
      const child = rank.get(edge.child)
      if (parent != null && child == null) {
        rank.set(edge.child, parent + 1); changed = true
      } else if (child != null && parent == null) {
        rank.set(edge.parent, child - 1); changed = true
      }
    }
    if (!changed) break
  }
  const grouped = new Map<number, CatalogTable[]>()
  for (const table of tables) {
    const value = rank.get(table.uri) ?? 0
    const group = grouped.get(value) ?? []
    group.push(table)
    grouped.set(value, group)
  }
  const output: Record<string, { x: number; y: number }> = {}
  const columnsFor = (group: CatalogTable[]) => Math.ceil(group.length / 4)
  const rankX = new Map<number, number>([[0, 0]])
  let downstreamX = 340
  for (const value of [...grouped.keys()].filter((item) => item > 0).sort((a, b) => a - b)) {
    rankX.set(value, downstreamX)
    downstreamX += columnsFor(grouped.get(value)!) * 300 + 40
  }
  let upstreamX = -340
  for (const value of [...grouped.keys()].filter((item) => item < 0).sort((a, b) => b - a)) {
    rankX.set(value, upstreamX)
    upstreamX -= columnsFor(grouped.get(value)!) * 300 + 40
  }
  for (const [value, group] of grouped) {
    group.sort((left, right) => left.name.localeCompare(right.name))
    group.forEach((table, index) => {
      const column = Math.floor(index / 4)
      const row = index % 4
      const rowsInColumn = Math.min(4, group.length - column * 4)
      const centre = (rowsInColumn - 1) / 2
      const gapOffset = column % 2 === 0
        ? 0
        : row < Math.floor(rowsInColumn / 2)
          ? -110
          : row >= Math.ceil(rowsInColumn / 2)
            ? 110
            : 0
      output[table.id] = {
        // Keep every depth in a distinct horizontal band, then wrap high fan-out siblings inside
        // that band. Eight first-page neighbours now fit as two readable columns instead of one
        // long lane that forces Fit View to shrink every label into illegibility.
        x: (rankX.get(value) ?? 0) + (value < 0 ? -column : column) * 300,
        // The detailed card can grow to about 200px at semantic-detail zoom.
        // Stagger every second column into the gaps of the preceding column. Direct lineage edges
        // can then reach wrapped siblings without disappearing behind a nearer card and implying a
        // parent-child relationship that does not exist.
        y: (row - centre) * 220 + gapOffset,
      }
    })
  }
  return output
}

const LINEAGE_PAGE_SIZE = 8

function lineageNeighbourhood(
  tables: CatalogTable[],
  edges: LineageEdge[],
  rootUri: string | null,
  limit: number,
): CatalogTable[] {
  if (!rootUri || tables.length <= limit + 1) return tables
  const byUri = new Map(tables.map((table) => [table.uri, table]))
  if (!byUri.has(rootUri)) return tables.slice(0, limit + 1)
  const adjacency = new Map<string, Set<string>>()
  for (const edge of edges) {
    if (!byUri.has(edge.parent) || !byUri.has(edge.child)) continue
    const parents = adjacency.get(edge.parent) ?? new Set<string>()
    parents.add(edge.child)
    adjacency.set(edge.parent, parents)
    const children = adjacency.get(edge.child) ?? new Set<string>()
    children.add(edge.parent)
    adjacency.set(edge.child, children)
  }
  const selected = new Set<string>([rootUri])
  let frontier = [rootUri]
  while (frontier.length && selected.size < limit + 1) {
    const candidates = [...new Set(frontier.flatMap((uri) => [...(adjacency.get(uri) ?? [])]))]
      .filter((uri) => !selected.has(uri))
      .sort((left, right) => {
        const byName = (byUri.get(left)?.name ?? left).localeCompare(byUri.get(right)?.name ?? right)
        return byName || left.localeCompare(right)
      })
    const next = candidates.slice(0, limit + 1 - selected.size)
    next.forEach((uri) => selected.add(uri))
    frontier = next
  }
  return tables.filter((table) => selected.has(table.uri))
}

// The graph renders one ENTITY per table + O(n²) join hints, so it operates on a BOUNDED set.
const ER_CAP = 60
const errorMessage = (e: unknown) => e instanceof Error ? e.message : String(e)

function relationshipFields(
  table: CatalogTable,
  relationships: Relationship[],
): EntityField[] {
  const roles = new Map<string, EntityField['role']>()
  for (const relationship of relationships) {
    const left = relationship.leftUri === table.uri
    const right = relationship.rightUri === table.uri
    if (!left && !right) continue
    const columns = left ? relationship.leftColumns : relationship.rightColumns
    const side = left ? relationship.cardinality.split(':')[0] : relationship.cardinality.split(':')[1]
    for (const column of columns) {
      if (!roles.has(column)) roles.set(column, side === '1' ? 'KEY' : side === 'N' ? 'FK' : 'KEY')
    }
  }
  // Keep relationship endpoints first so every expanded edge can land on a visible field. Updating
  // an existing Map entry changes the role without moving that field out of its endpoint-first slot.
  for (const key of table.keys ?? []) for (const column of key.columns) {
    if (key.confidence === 'declared') roles.set(column, 'PK')
  }
  for (const column of table.columns) {
    if (roles.size >= 6) break
    if (!roles.has(column.name)) roles.set(column.name, 'field')
  }
  const byName = new Map(table.columns.map((column) => [column.name, column]))
  return [...roles].slice(0, 6).map(([name, role]) => ({
    name,
    role,
    column: byName.get(name),
    type: byName.get(name)?.type,
  }))
}

function lineageFields(
  table: CatalogTable,
  edges: LineageEdge[],
): EntityField[] {
  const roles = new Map<string, EntityField['role']>()
  for (const name of edges.filter((edge) => edge.child === table.uri).flatMap((edge) => edge.columns ?? [])) {
    roles.set(name, 'mapped')
  }
  for (const key of table.keys ?? []) for (const column of key.columns) {
    if (key.confidence === 'declared') roles.set(column, 'PK')
  }
  for (const column of table.columns) {
    if (roles.size >= 6) break
    if (!roles.has(column.name)) roles.set(column.name, 'field')
  }
  const byName = new Map(table.columns.map((column) => [column.name, column]))
  return [...roles].slice(0, 6).map(([name, role]) => ({
    name,
    role,
    column: byName.get(name),
    type: byName.get(name)?.type,
  }))
}

export function ERDiagram() {
  const pushToast = useStore((s) => s.pushToast)
  const erFocusUri = useStore((s) => s.erFocusUri)
  const erFocusDatasetId = useStore((s) => s.erFocusDatasetId)
  const erMode = useStore((s) => s.erMode)
  const erReturn = useStore((s) => s.erReturn)
  const setRelationshipsFocus = useStore((s) => s.setRelationshipsFocus)
  const setRelationshipsMode = useStore((s) => s.setRelationshipsMode)
  const returnFromRelationships = useStore((s) => s.returnFromRelationships)
  const openWorkspace = useStore((s) => s.setView)
  const setWorkspaceResource = useStore((s) => s.setWorkspaceResource)

  // focus === null → the global / folder view; otherwise the neighbourhood of that uri
  const [focus, setFocus] = useState<string | null>(erFocusUri)
  const [hops, setHops] = useState(1)
  const [mode, setMode] = useState<'joins' | 'lineage'>(erMode)
  const [focusResolving, setFocusResolving] = useState(!erFocusUri && !!erFocusDatasetId)
  const [focusedTable, setFocusedTable] = useState<CatalogTable | null>(null)
  const [focusResolutionError, setFocusResolutionError] = useState<string | null>(null)
  const [focusResolutionRevision, setFocusResolutionRevision] = useState(0)
  const [folder, setFolder] = useState('')
  const [folders, setFolders] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [lineageLimit, setLineageLimit] = useState(LINEAGE_PAGE_SIZE)
  const [expandedEntities, setExpandedEntities] = useState(false)
  const [openingNode, setOpeningNode] = useState<string | null>(null)
  const updateExpandedEntities = useCallback((zoom: number) => {
    const next = zoom >= 0.82
    setExpandedEntities((current) => current === next ? current : next)
  }, [])

  const [tables, setTables] = useState<CatalogTable[]>([])
  const [total, setTotal] = useState(0)
  const [linEdges, setLinEdges] = useState<LineageEdge[]>([])
  const [lineageFocus, setLineageFocus] = useState<{
    requested: string; canonical: string
  } | null>(null)
  const [rels, setRels] = useState<Relationship[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [relsError, setRelsError] = useState<string | null>(null)
  const [relationshipToRemove, setRelationshipToRemove] = useState<Relationship | null>(null)
  const [removingRelationship, setRemovingRelationship] = useState(false)
  const [pending, setPending] = useState<{
    left: CatalogTable; right: CatalogTable; suggestions: JoinSuggestion[]
    suggestionsLoading: boolean; suggestionsError: string | null
  } | null>(null)
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>(loadPositions)
  const [reloadKey, setReloadKey] = useState(0)
  const graphContainer = useRef<HTMLDivElement>(null)
  const dataReq = useRef(0)
  const relsReq = useRef(0)
  const focusReq = useRef(0)

  useEffect(() => { setMode(erMode) }, [erMode])

  useEffect(() => {
    const request = ++focusReq.current
    setFocusResolutionError(null)
    setFocusedTable(null)
    const resolveStableFocus = async (datasetId: string): Promise<CatalogTable> => {
      if (!datasetId.startsWith('dataset:')) return api.tableByRegistration(datasetId)
      const [resolution, context] = await Promise.all([
        api.workspaceResource(datasetId),
        api.workspaceCanonicalDataset(datasetId),
      ])
      if (!resolution.resource || resolution.resource.kind !== 'dataset') {
        throw new Error('The focused Workspace dataset is unavailable')
      }
      return {
        id: `lineage:${context.sourceUri}`,
        registrationId: datasetId,
        name: resolution.resource.name,
        uri: context.sourceUri,
        columns: context.columns,
      }
    }
    if (erFocusUri) {
      setFocus(erFocusUri)
      setFocusResolving(false)
      if (!erFocusDatasetId) return
      void resolveStableFocus(erFocusDatasetId).then((table) => {
        if (request === focusReq.current) setFocusedTable(table)
      }).catch(() => {
        // Lineage remains usable when optional schema enrichment is temporarily unavailable.
      })
      return () => { focusReq.current += 1 }
    }
    if (!erFocusDatasetId) {
      setFocus(null)
      setFocusResolving(false)
      return
    }
    setFocus(null)
    setFocusResolving(true)
    void resolveStableFocus(erFocusDatasetId).then((table) => {
      if (request !== focusReq.current) return
      setFocusedTable(table)
      setFocus(table.uri)
      setFocusResolving(false)
    }).catch((caught) => {
      if (request !== focusReq.current) return
      setFocusResolutionError(errorMessage(caught))
      setFocusResolving(false)
    })
    return () => { focusReq.current += 1 }
  }, [erFocusDatasetId, erFocusUri, focusResolutionRevision])

  const loadRelationships = useCallback(async () => {
    const s = ++relsReq.current
    setRelsError(null)
    try { const next = await api.relationships(); if (s === relsReq.current) setRels(next) }
    catch (e) { if (s === relsReq.current) setRelsError(errorMessage(e)) }
  }, [])

  useEffect(() => {
    if (mode !== 'joins') {
      setRels([])
      setRelsError(null)
      return
    }
    void loadRelationships()
    api.facets().then((f) => setFolders(f.folders.map((x) => x.value))).catch(() => {})
    return () => { relsReq.current += 1 }
  }, [loadRelationships, mode])

  // a genuinely new query (focus/folder/mode/hops) must not show the previous query's rows while the
  // next request is in flight; a plain retry (reloadKey) keeps the last graph.
  useEffect(() => {
    setTables([])
    setTotal(0)
    setLinEdges([])
    setLineageLimit(LINEAGE_PAGE_SIZE)
  }, [focus, folder, mode, hops])

  // recompute the visible entity set whenever the query (focus / hops / mode / folder / rels) changes
  const visibleFocus = mode === 'lineage' && lineageFocus?.requested === focus
    ? lineageFocus.canonical : focus
  const focusName = tables.find((t) => t.uri === visibleFocus)?.name
    ?? (mode === 'lineage' ? 'Current dataset' : visibleFocus?.split('/').slice(-1)[0])
  useEffect(() => {
    if (focusResolving || focusResolutionError) {
      setLoading(focusResolving)
      return
    }
    const s = ++dataReq.current
    setLoading(true); setError(null)
    ;(async () => {
      try {
        if (focus) {
          if (mode === 'lineage') {
            // Always query from the stable route focus. The response may canonicalize that focus
            // to a physical generation for layout, but feeding that returned root back into the
            // next request can turn a connected graph into an isolated node after reload.
            const lin = await api.lineage(focus, hops, ER_CAP)
            const uris = [...new Set(lin.nodes.map((n) => n.uri))]
            const page = uris.length ? await api.tablesPage({ uris, limit: ER_CAP }) : { items: [], total: 0, hasMore: false }
            if (s !== dataReq.current) return
            const registered = new Map(page.items.map((table) => [table.uri, table]))
            // Provider datasets participate in core lineage through their stable Source URI even
            // though they are intentionally not registered in the local Catalog. Keep those nodes
            // in the graph instead of silently dropping the root while resolving richer Catalog
            // metadata for every node that does have a registration.
            const lineageTables = lin.nodes.map((node) => registered.get(node.uri)
              ?? (focusedTable && node.uri === lin.rootUri ? {
                ...focusedTable,
                uri: node.uri,
                name: node.name || focusedTable.name,
              } : null)
              ?? ({
                id: `lineage:${node.uri}`,
                name: node.name || node.uri.split('/').filter(Boolean).slice(-1)[0] || 'Dataset',
                uri: node.uri,
                columns: [],
                missing: true,
              } satisfies CatalogTable))
            setTables(lineageTables); setTotal(lineageTables.length); setLinEdges(lin.edges)
            setLineageFocus({ requested: focus, canonical: lin.rootUri })
          } else {
            const uris = joinNeighbourhood(focus, rels, hops)
            const page = await api.tablesPage({ uris, limit: ER_CAP })
            if (s !== dataReq.current) return
            setTables(page.items); setTotal(page.items.length); setLinEdges([]); setLineageFocus(null)
          }
        } else {
          const page = await api.tablesPage({ folder: folder || undefined, limit: ER_CAP, sort: 'usage', order: 'desc' })
          if (s !== dataReq.current) return
          setTables(page.items); setTotal(page.total); setLinEdges([]); setLineageFocus(null)
        }
      } catch (e) {
        if (s === dataReq.current) setError(errorMessage(e))
      } finally {
        if (s === dataReq.current) setLoading(false)
      }
    })()
    return () => { dataReq.current += 1 }
  }, [focus, hops, mode, folder, rels, reloadKey, focusResolving, focusResolutionError, focusedTable])

  const refresh = useCallback(() => setReloadKey((k) => k + 1), [])

  const filtered = useMemo(() => {
    if (focus || !search.trim()) return tables
    const q = search.trim().toLowerCase()
    return tables.filter((t) => t.name.toLowerCase().includes(q) || (t.folder ?? '').toLowerCase().includes(q))
  }, [tables, focus, search])
  const visible = useMemo(
    () => mode === 'lineage'
      ? lineageNeighbourhood(filtered, linEdges, visibleFocus, lineageLimit)
      : filtered,
    [filtered, lineageLimit, linEdges, mode, visibleFocus],
  )

  const byUri = useMemo(() => Object.fromEntries(visible.map((t) => [t.uri, t.id])), [visible])
  const visibleLineageEdges = useMemo(
    () => linEdges.filter((edge) => byUri[edge.parent] && byUri[edge.child]),
    [byUri, linEdges],
  )

  const lineagePositions = useMemo(
    () => mode === 'lineage' ? lineageLayout(visible, visibleLineageEdges, visibleFocus) : {},
    [mode, visible, visibleFocus, visibleLineageEdges],
  )
  const fieldsByTable = useMemo(() => Object.fromEntries(visible.map((table) => [
    table.id,
    mode === 'lineage'
      ? lineageFields(table, visibleLineageEdges)
      : relationshipFields(table, rels),
  ])), [mode, rels, visible, visibleLineageEdges])
  const openLineageDataset = useCallback(async (table: CatalogTable) => {
    if (openingNode) return
    if (table.uri === visibleFocus && erReturn) {
      returnFromRelationships()
      return
    }
    if (!table.id.startsWith('lineage:')) {
      setWorkspaceResource(`dataset:${table.registrationId ?? table.id}`)
      return
    }
    if (!visibleFocus) return
    setOpeningNode(table.id)
    try {
      const resource = await api.workspaceLineageResource({
        rootUri: visibleFocus,
        nodeUri: table.uri,
        name: table.name,
      })
      setWorkspaceResource(resource.id)
    } catch (caught) {
      const message = errorMessage(caught)
      pushToast(
        message.includes('lineage dataset is not registered')
          ? `No dataset details are available for ${table.name}.`
          : `Couldn't open ${table.name}: ${message}`,
        'error',
      )
    } finally {
      setOpeningNode(null)
    }
  }, [erReturn, openingNode, pushToast, returnFromRelationships, setWorkspaceResource, visibleFocus])
  const nodes: Node[] = useMemo(() => visible.map((t, i) => ({
    id: t.id, type: 'entity',
    position: mode === 'lineage'
      ? lineagePositions[t.id] ?? { x: (i % 3) * 300, y: Math.floor(i / 3) * 180 }
      : positions[t.id] ?? { x: (i % 3) * 300, y: Math.floor(i / 3) * 300 },
    data: {
      table: t,
      fields: fieldsByTable[t.id] ?? [],
      focused: t.uri === visibleFocus,
      lineage: mode === 'lineage',
      expanded: expandedEntities,
      opening: openingNode === t.id,
      onFocus: () => { setFocus(t.uri); setRelationshipsFocus(t) },
      onOpen: () => { void openLineageDataset(t) },
    } satisfies EntityData,
  })), [expandedEntities, fieldsByTable, lineagePositions, mode, openLineageDataset, openingNode, positions, setRelationshipsFocus, visible, visibleFocus])

  const edges: Edge[] = useMemo(() => {
    const out: Edge[] = []
    const declared = new Set<string>()
    if (mode === 'joins') rels.forEach((r, i) => {
      const s = byUri[r.leftUri], t = byUri[r.rightUri]
      if (!s || !t) return
      declared.add([s, t].sort().join('|'))
      out.push({
        id: `d${i}`, source: s, target: t,
        sourceHandle: expandedEntities && r.leftColumns[0]
          && fieldsByTable[s]?.some((field) => field.name === r.leftColumns[0])
          ? `column-out:${r.leftColumns[0]}` : 'node-source',
        targetHandle: expandedEntities && r.rightColumns[0]
          && fieldsByTable[t]?.some((field) => field.name === r.rightColumns[0])
          ? `column-in:${r.rightColumns[0]}` : 'node-target',
        label: r.cardinality,
        labelStyle: { fontSize: 10, fontWeight: 600 }, markerEnd: { type: MarkerType.ArrowClosed },
        style: { stroke: 'hsl(var(--primary))', strokeWidth: 1.5 }, data: { rel: r },
      })
    })
    if (mode === 'lineage') visibleLineageEdges.forEach((e, i) => {
      const s = byUri[e.parent], t = byUri[e.child]
      if (!s || !t) return
      const mappedColumn = e.columns?.[0]
      const pipeline = e.pipelineNames?.length === 1 ? e.pipelineNames[0] : null
      out.push({
        id: `l${i}`, source: s, target: t, selectable: false,
        sourceHandle: 'node-source',
        targetHandle: expandedEntities && mappedColumn
          && fieldsByTable[t]?.some((field) => field.name === mappedColumn)
          ? `column-in:${mappedColumn}` : 'node-target',
        // Long generated pipeline identifiers turn a dense graph into an unreadable wall of text.
        // Keep concise human names on the wire; the column endpoint already explains mapped data.
        label: expandedEntities && pipeline && pipeline.length <= 28 ? pipeline : undefined,
        ariaLabel: pipeline ? `Produced by ${pipeline}` : 'Lineage',
        labelStyle: { fontSize: 9.5 },
        markerEnd: { type: MarkerType.ArrowClosed },
        style: { stroke: 'hsl(var(--muted-foreground))', strokeWidth: 1.5 },
      })
    })
    if (mode === 'joins' && showSuggestions) for (let a = 0; a < visible.length; a++)
      for (let b = a + 1; b < visible.length; b++) {
        const ta = visible[a], tb = visible[b]
        if (declared.has([ta.id, tb.id].sort().join('|')) || !sharesKey(ta, tb)) continue
        out.push({
          id: `c-${ta.id}-${tb.id}`, source: ta.id, target: tb.id, selectable: false,
          style: { stroke: 'hsl(var(--muted-foreground))', strokeDasharray: '4 3', opacity: 0.45 },
        })
      }
    return out
  }, [rels, visible, byUri, expandedEntities, fieldsByTable, mode, visibleLineageEdges, showSuggestions])

  const loadSuggestions = useCallback(async (left: CatalogTable, right: CatalogTable) => {
    setPending((cur) => cur?.left.id === left.id && cur.right.id === right.id
      ? { ...cur, suggestionsLoading: true, suggestionsError: null }
      : { left, right, suggestions: [], suggestionsLoading: true, suggestionsError: null })
    try {
      const suggestions = await api.joinSuggestions(left.uri, right.uri)
      setPending((cur) => cur?.left.id === left.id && cur.right.id === right.id ? { ...cur, suggestions, suggestionsLoading: false } : cur)
    } catch (e) {
      setPending((cur) => cur?.left.id === left.id && cur.right.id === right.id ? { ...cur, suggestionsLoading: false, suggestionsError: errorMessage(e) } : cur)
    }
  }, [])
  const onConnect = useCallback((c: Connection) => {
    const s = visible.find((t) => t.id === c.source), t = visible.find((x) => x.id === c.target)
    if (!s || !t || s.id === t.id) return
    void loadSuggestions(s, t)
  }, [visible, loadSuggestions])

  const onEdgeClick = useCallback((_e: React.MouseEvent, edge: Edge) => {
    const rel = (edge.data as { rel?: Relationship } | undefined)?.rel
    if (rel) setRelationshipToRemove(rel)
  }, [])

  const removeRelationship = async () => {
    const rel = relationshipToRemove
    if (!rel) return
    setRemovingRelationship(true)
    try {
      setRels(await api.deleteRelationship(rel))
      setRelationshipToRemove(null)
    } catch (e) { pushToast(errorMessage(e), 'error') }
    finally { setRemovingRelationship(false) }
  }

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setPositions((p) => {
      if (!changes.some((ch) => ch.type === 'position' && ch.position)) return p
      const next = { ...p }
      for (const ch of changes) if (ch.type === 'position' && ch.position) next[ch.id] = ch.position
      savePositions(next)
      return next
    })
  }, [])

  const hasFocusedRoute = !!focus || !!erFocusDatasetId
  const capped = !hasFocusedRoute && total > visible.length
  const hiddenLineageConnections = Math.max(0, linEdges.length - visibleLineageEdges.length)
  const layoutKey = useMemo(() => JSON.stringify(nodes.map((node) => node.id)), [nodes])
  // Expanding a busy lineage page is a deliberate pan/zoom continuation. Fit only when the query's
  // fetched graph changes, never when Show more merely reveals another bounded sibling batch.
  const fitKey = useMemo(() => mode === 'lineage'
    ? JSON.stringify([mode, visibleFocus, hops, tables.map((table) => table.id)])
    : layoutKey, [hops, layoutKey, mode, tables, visibleFocus])

  return (
    <div ref={graphContainer} className="relative h-full w-full">
      <div data-testid="er-controls-panel" className="absolute left-3 top-3 z-10 flex w-[320px] flex-col gap-2 rounded-lg border border-border bg-card/95 px-3 py-2.5 text-[11px] text-muted-foreground shadow-sm backdrop-blur">
        <div className="flex items-center gap-2">
          <span className="text-[12.5px] font-semibold text-foreground">{mode === 'lineage' ? 'Lineage' : 'Relationships'}</span>
          <div className="inline-flex rounded-md border border-border bg-background p-0.5 text-[10.5px]" role="group" aria-label="Graph mode">
            {(['joins', 'lineage'] as const).map((graphMode) => (
              <button key={graphMode} onClick={() => { setMode(graphMode); setRelationshipsMode(graphMode) }}
                data-testid={`er-mode-${graphMode}`}
                className={cn('rounded px-1.5 py-0.5 capitalize', mode === graphMode ? 'bg-accent font-semibold text-foreground' : 'hover:text-foreground')}>
                {graphMode === 'joins' ? 'ER' : 'Lineage'}
              </button>
            ))}
          </div>
          <span className="flex-1" />
          {erReturn && <button type="button" onClick={returnFromRelationships}
            data-testid="er-back-to-dataset" aria-label="Back to dataset"
            className="text-[10.5px] font-semibold text-primary hover:underline">← Dataset</button>}
          <button onClick={() => setShowHelp((v) => !v)} aria-label="How this works" title="How this works"
            className="grid h-5 w-5 place-items-center rounded-full border border-border text-[11px] font-bold hover:bg-accent">?</button>
        </div>

        {hasFocusedRoute && mode === 'lineage' ? (
          <div className="flex flex-col gap-2" data-testid="er-focus-bar">
            <span className="truncate rounded-md bg-primary/10 px-2 py-1 text-[11px] font-semibold text-primary" title={focusName}>
              {focusName ?? (focusResolving ? 'Loading dataset…' : 'Current dataset')}
            </span>
            <div className="flex items-center gap-2">
              <span className="text-[10.5px]">Depth</span>
              <div className="inline-flex items-center rounded-md border border-border bg-background">
                <button onClick={() => setHops((h) => Math.max(1, h - 1))} className="px-1.5 py-0.5 hover:bg-accent" aria-label="Fewer hops">−</button>
                <span className="w-5 text-center text-[11px] font-semibold text-foreground" data-testid="er-hops">{hops}</span>
                <button onClick={() => setHops((h) => Math.min(5, h + 1))} className="px-1.5 py-0.5 hover:bg-accent" aria-label="More hops">+</button>
              </div>
              <span className="flex-1" />
              <span data-testid="er-connection-count" className="text-[10px] text-muted-foreground">
                {hiddenLineageConnections > 0
                  ? `${visibleLineageEdges.length} of ${linEdges.length} connections`
                  : `${linEdges.length} connection${linEdges.length === 1 ? '' : 's'}`}
              </span>
            </div>
            {hiddenLineageConnections > 0 || lineageLimit > LINEAGE_PAGE_SIZE ? <div className="flex items-center gap-2 text-[10px]">
              {hiddenLineageConnections > 0 ? <button type="button" data-testid="er-lineage-show-more"
                onClick={() => setLineageLimit((current) => current + LINEAGE_PAGE_SIZE)}
                className="font-semibold text-primary hover:underline">Show more</button> : null}
              {lineageLimit > LINEAGE_PAGE_SIZE ? <button type="button" data-testid="er-lineage-show-fewer"
                onClick={() => setLineageLimit(LINEAGE_PAGE_SIZE)}
                className="text-muted-foreground hover:text-foreground hover:underline">Show fewer</button> : null}
            </div> : null}
          </div>
        ) : hasFocusedRoute ? (
          <div className="flex flex-col gap-2" data-testid="er-focus-bar">
            <div className="flex items-center gap-1.5">
              <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10.5px] font-semibold text-primary">Focused: {focusName ?? (focusResolving ? 'loading…' : 'dataset')}</span>
              <button onClick={() => { setFocus(null); setRelationshipsFocus(null) }} className="text-[10.5px] underline hover:text-foreground" data-testid="er-clear-focus">show all</button>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[10.5px]">Hops</span>
              <div className="inline-flex items-center rounded-md border border-border">
                <button onClick={() => setHops((h) => Math.max(1, h - 1))} className="px-1.5 py-0.5 hover:bg-accent" aria-label="Fewer hops">−</button>
                <span className="w-5 text-center text-[11px] font-semibold text-foreground" data-testid="er-hops">{hops}</span>
                <button onClick={() => setHops((h) => Math.min(5, h + 1))} className="px-1.5 py-0.5 hover:bg-accent" aria-label="More hops">+</button>
              </div>
              <span className="flex-1" />
            </div>
          </div>
        ) : (
          <div className="flex items-center gap-1.5">
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Filter by name…" data-testid="er-search"
              className="min-w-0 flex-1 rounded border border-border bg-card px-2 py-1 text-[11px] outline-none focus:border-primary" />
            <select value={folder} onChange={(e) => setFolder(e.target.value)} data-testid="er-folder"
              className="rounded border border-border bg-card px-1.5 py-1 text-[10.5px] outline-none">
              <option value="">All folders</option>
              {folders.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </div>
        )}

        {mode === 'lineage' ? <div className="flex items-center gap-2 text-[10px]">
          <span className="inline-flex items-center gap-1"><span className="inline-block h-0 w-3 border-t-[1.5px] border-muted-foreground" /> upstream → current → downstream</span>
          {linEdges.length > 0 && <span className="ml-auto text-muted-foreground">Arrows show data flow</span>}
        </div> : <div className="flex items-center gap-2 text-[10px]">
          <span className="inline-flex items-center gap-1"><span className="inline-block h-0 w-3 border-t-[1.5px] border-primary" /> declared join</span>
          <label className="ml-auto inline-flex cursor-pointer items-center gap-1">
            <input type="checkbox" checked={showSuggestions} onChange={(e) => setShowSuggestions(e.target.checked)} data-testid="er-suggestions-toggle" className="h-3 w-3 accent-primary" />
            suggestions
          </label>
        </div>}

        {showHelp && (
          <div className="rounded-md border border-border bg-muted/40 p-2 text-[10.5px] leading-relaxed">
            {mode === 'lineage'
              ? 'Arrows run from source datasets to the datasets produced from them. Busy graphs open with a readable subset; use Show more or increase Depth when you need more context.'
              : 'Drag from one entity to another to declare a join. Click a solid edge to remove it. Click an entity title to re-focus the graph.'}
          </div>
        )}

        {loading && <span data-testid="er-catalog-loading">Loading…</span>}
        {focusResolutionError && (
          <span role="alert" className="text-destructive">
            Couldn't restore the focused dataset: {focusResolutionError}{' '}
            <button onClick={() => setFocusResolutionRevision((value) => value + 1)}
              data-testid="er-focus-retry" className="font-semibold underline">Retry</button>
          </span>
        )}
        {error && (
          <span role="alert" className="text-destructive">
            Couldn't load: {error}{' '}
            <button onClick={refresh} data-testid="er-catalog-retry" className="font-semibold underline">Retry</button>
          </span>
        )}
        {mode === 'joins' && relsError && (
          <span role="alert" className="text-destructive">
            Couldn't load declared relationships: {relsError}{' '}
            <button onClick={() => void loadRelationships()} data-testid="er-relationships-retry" className="font-semibold underline">Retry</button>
          </span>
        )}
        {capped && <span className="text-[10px] text-amber-600">Showing {visible.length} of {total} — focus a table or pick a folder.</span>}
      </div>

      {!loading && !error && visible.length === 0 && (
        <div className="pointer-events-none absolute inset-0 z-[5] grid place-items-center text-[13px] text-muted-foreground">
          {hasFocusedRoute ? (mode === 'lineage' ? 'No recorded inputs or outputs at this depth.' : 'No neighbours at this hop distance.') : total === 0 ? (
            <span className="pointer-events-auto">No datasets registered yet — add some in <button onClick={() => openWorkspace('workspace')} className="underline">Workspace</button>.</span>
          ) : 'No datasets in this folder.'}
        </div>
      )}

      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes}
        onNodesChange={mode === 'joins' ? onNodesChange : undefined}
        onConnect={mode === 'joins' ? onConnect : undefined}
        onEdgeClick={mode === 'joins' ? onEdgeClick : undefined}
        onMove={(_event, viewport) => updateExpandedEntities(viewport.zoom)}
        nodesDraggable={mode === 'joins'} nodesConnectable={mode === 'joins'}
        minZoom={0.2} colorMode={resolvedTheme()}
        proOptions={{ hideAttribution: true }}>
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--dots)" />
        <ERViewportControls fitKey={fitKey} container={graphContainer} lineage={mode === 'lineage'}
          onZoomChange={updateExpandedEntities} />
      </ReactFlow>
      {pending && (
        <RelationshipDialog key={`${pending.left.id}|${pending.right.id}`}
          left={pending.left} right={pending.right} suggestions={pending.suggestions}
          suggestionsLoading={pending.suggestionsLoading} suggestionsError={pending.suggestionsError}
          onRetrySuggestions={() => void loadSuggestions(pending.left, pending.right)}
          onClose={() => setPending(null)}
          onDeclared={(next) => { setRels(next); setPending(null) }} />
      )}
      <ConfirmationDialog
        open={relationshipToRemove !== null}
        title="Remove relationship?"
        description={relationshipToRemove
          ? `Remove the declared relationship ${relationshipToRemove.leftColumns.join(' + ')} = ${relationshipToRemove.rightColumns.join(' + ')}?`
          : ''}
        confirmLabel="Remove relationship"
        busy={removingRelationship}
        onCancel={() => setRelationshipToRemove(null)}
        onConfirm={() => { void removeRelationship() }}
      />
    </div>
  )
}

const CARDINALITIES: Cardinality[] = ['1:1', '1:N', 'N:1', 'N:M', 'unknown']

// Pick the join key(s) + cardinality when declaring a relationship: seed from the ranked suggestions,
// or toggle columns on each side by hand (equal counts) and choose the cardinality.
function RelationshipDialog({ left, right, suggestions, suggestionsLoading, suggestionsError, onRetrySuggestions, onClose, onDeclared }: {
  left: CatalogTable; right: CatalogTable; suggestions: JoinSuggestion[]
  suggestionsLoading: boolean; suggestionsError: string | null; onRetrySuggestions: () => void
  onClose: () => void; onDeclared: (rels: Relationship[]) => void
}) {
  const pushToast = useStore((s) => s.pushToast)
  const top = suggestions[0]
  const [lc, setLc] = useState<string[]>(top?.leftColumns ?? [])
  const [rc, setRc] = useState<string[]>(top?.rightColumns ?? [])
  const [card, setCard] = useState<Cardinality>(top?.cardinality ?? 'unknown')
  const [keysTouched, setKeysTouched] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!top || keysTouched) return
    setLc(top.leftColumns); setRc(top.rightColumns); setCard(top.cardinality)
  }, [top, keysTouched])

  const toggle = (arr: string[], set: (v: string[]) => void, col: string) => {
    setKeysTouched(true)
    set(arr.includes(col) ? arr.filter((c) => c !== col) : [...arr, col])
    setCard('unknown')
  }
  const pick = (s: JoinSuggestion) => { setKeysTouched(true); setLc(s.leftColumns); setRc(s.rightColumns); setCard(s.cardinality) }
  const ok = lc.length > 0 && lc.length === rc.length
  const declare = async () => {
    setBusy(true)
    try {
      onDeclared(await api.addRelationship({ leftUri: left.uri, leftColumns: lc, rightUri: right.uri, rightColumns: rc, cardinality: card, confidence: 'declared' }))
      pushToast(`declared ${left.name} → ${right.name} (${card})`, 'success')
    } catch (e) { pushToast(errorMessage(e), 'error'); setBusy(false) }
  }

  const colList = (t: CatalogTable, arr: string[], set: (v: string[]) => void) => (
    <div className="flex max-h-[180px] flex-1 flex-col gap-0.5 overflow-y-auto rounded-md border border-border p-1.5">
      {t.columns.map((c) => (
        <button key={c.name} onClick={() => toggle(arr, set, c.name)}
          className={cn('flex items-center gap-1.5 rounded px-1.5 py-0.5 text-left text-[11.5px] hover:bg-accent',
            arr.includes(c.name) && 'bg-primary/10 font-semibold text-foreground')}>
          <span className="w-3 text-center text-[10px]">{arr.includes(c.name) ? (arr.indexOf(c.name) + 1) : ''}</span>
          <span className="dp-mono flex-1 truncate">{c.name}</span>
          {c.capabilities?.includes('key') && <span className="text-[9px] text-muted-foreground">key</span>}
        </button>
      ))}
    </div>
  )

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-w-[480px]">
        <DialogHeader><DialogTitle className="text-[14px]">Declare a join: {left.name} → {right.name}</DialogTitle></DialogHeader>
        {suggestionsLoading && <div className="text-[11px] text-muted-foreground">Loading join suggestions…</div>}
        {suggestionsError && (
          <div role="alert" className="flex items-center justify-between gap-2 rounded-md border border-destructive/30 px-2 py-1.5 text-[11px] text-destructive">
            <span>Join suggestions unavailable: {suggestionsError}. You can still choose keys manually.</span>
            <button onClick={onRetrySuggestions} data-testid="er-suggestions-retry" className="shrink-0 font-semibold underline">Retry</button>
          </div>
        )}
        {suggestions.length > 0 && (
          <div className="flex flex-col gap-1">
            <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Suggested (measured)</div>
            {suggestions.slice(0, 5).map((s, i) => (
              <button key={i} onClick={() => pick(s)}
                className="flex items-center gap-2 rounded-md border border-border px-2 py-1 text-left hover:bg-accent">
                <span className="dp-mono flex-1 truncate text-[11px]">{s.leftColumns.join('+')} = {s.rightColumns.join('+')}</span>
                <span className="rounded bg-muted px-1.5 py-px text-[9.5px] font-semibold">{s.cardinality}</span>
              </button>
            ))}
          </div>
        )}
        <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Keys (click columns in order; equal count on each side)</div>
        <div className="flex gap-2">
          {colList(left, lc, setLc)}
          <div className="self-center text-[12px] text-muted-foreground">=</div>
          {colList(right, rc, setRc)}
        </div>
        <div className="flex items-center justify-between gap-2">
          <label className="flex items-center gap-1.5 text-[11.5px] text-muted-foreground">Cardinality
            <MiniSelect value={card} options={CARDINALITIES.map((c) => ({ value: c, label: c }))} onChange={(v) => { setKeysTouched(true); setCard(v as Cardinality) }} />
          </label>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>Cancel</Button>
            <Button size="sm" disabled={!ok || busy} onClick={declare}>Declare</Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
