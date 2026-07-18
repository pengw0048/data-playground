import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow, Background, BackgroundVariant, Controls, Handle, Position, MarkerType,
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

// The relationship graph: entities are catalog datasets, declared joins are solid edges labelled with
// cardinality. It opens FOCUSED on one table (reached from a table's detail drawer) and shows that
// table plus its neighbours within N hops; "Show all" widens to the whole catalog (capped). A second
// mode swaps the join graph for the data-lineage (provenance) graph. Primary keys are declared in the
// table drawer, so entity columns here are read-only.

type EntityData = { table: CatalogTable; pk: string[]; focused: boolean; onFocus: () => void }

function EntityNode({ data }: { data: EntityData }) {
  const { table, pk, focused, onFocus } = data
  return (
    <div className={cn('w-[240px] overflow-hidden rounded-lg border bg-card shadow-sm', focused ? 'border-primary ring-2 ring-primary/20' : 'border-border')}>
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-primary" />
      <button onClick={onFocus} title="Focus the graph on this table"
        className="flex w-full items-center gap-1.5 truncate border-b border-border bg-muted px-3 py-1.5 text-left text-[12px] font-semibold text-foreground hover:bg-accent">
        <Icon name="lineage" size={11} /> <span className="truncate">{table.name}</span>
      </button>
      <div className="flex max-h-[220px] flex-col overflow-y-auto py-1">
        {table.columns.map((c) => {
          const isPk = pk.includes(c.name)
          const isKey = c.capabilities?.includes('key')
          return (
            <div key={c.name} className={cn('flex items-center gap-1.5 px-3 py-0.5 text-left text-[11px]', isPk && 'font-semibold text-foreground')}>
              <span className="w-3 text-center text-[10px]">{isPk ? '🔑' : isKey ? '·' : ''}</span>
              <span className="dp-mono flex-1 truncate">{c.name}</span>
              <span className="text-[9.5px] text-muted-foreground">{c.type}</span>
            </div>
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

// The graph renders one ENTITY per table + O(n²) join hints, so it operates on a BOUNDED set.
const ER_CAP = 60
const errorMessage = (e: unknown) => e instanceof Error ? e.message : String(e)

export function ERDiagram() {
  const pushToast = useStore((s) => s.pushToast)
  const erFocusUri = useStore((s) => s.erFocusUri)
  const openWorkspace = useStore((s) => s.setView)

  // focus === null → the global / folder view; otherwise the neighbourhood of that uri
  const [focus, setFocus] = useState<string | null>(erFocusUri)
  const [hops, setHops] = useState(1)
  const [mode, setMode] = useState<'joins' | 'lineage'>('joins')
  const [folder, setFolder] = useState('')
  const [folders, setFolders] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [showHelp, setShowHelp] = useState(false)

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
  const [pending, setPending] = useState<{
    left: CatalogTable; right: CatalogTable; suggestions: JoinSuggestion[]
    suggestionsLoading: boolean; suggestionsError: string | null
  } | null>(null)
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>(loadPositions)
  const [reloadKey, setReloadKey] = useState(0)
  const dataReq = useRef(0)
  const relsReq = useRef(0)

  const loadRelationships = useCallback(async () => {
    const s = ++relsReq.current
    setRelsError(null)
    try { const next = await api.relationships(); if (s === relsReq.current) setRels(next) }
    catch (e) { if (s === relsReq.current) setRelsError(errorMessage(e)) }
  }, [])

  useEffect(() => {
    void loadRelationships()
    api.facets().then((f) => setFolders(f.folders.map((x) => x.value))).catch(() => {})
    return () => { relsReq.current += 1 }
  }, [loadRelationships])

  // a genuinely new query (focus/folder/mode/hops) must not show the previous query's rows while the
  // next request is in flight; a plain retry (reloadKey) keeps the last graph.
  useEffect(() => { setTables([]); setTotal(0); setLinEdges([]) }, [focus, folder, mode, hops])

  // recompute the visible entity set whenever the query (focus / hops / mode / folder / rels) changes
  const visibleFocus = mode === 'lineage' && lineageFocus?.requested === focus
    ? lineageFocus.canonical : focus
  const focusName = tables.find((t) => t.uri === visibleFocus)?.name ?? visibleFocus?.split('/').slice(-1)[0]
  useEffect(() => {
    const s = ++dataReq.current
    setLoading(true); setError(null)
    ;(async () => {
      try {
        if (visibleFocus) {
          if (mode === 'lineage') {
            const lin = await api.lineage(visibleFocus, hops, ER_CAP)
            const uris = [...new Set(lin.nodes.map((n) => n.uri))]
            const page = uris.length ? await api.tablesPage({ uris, limit: ER_CAP }) : { items: [], total: 0, hasMore: false }
            if (s !== dataReq.current) return
            setTables(page.items); setTotal(page.items.length); setLinEdges(lin.edges)
            setLineageFocus({ requested: focus ?? visibleFocus, canonical: lin.rootUri })
          } else {
            const uris = joinNeighbourhood(visibleFocus, rels, hops)
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
  }, [focus, visibleFocus, hops, mode, folder, rels, reloadKey])

  const refresh = useCallback(() => setReloadKey((k) => k + 1), [])

  const visible = useMemo(() => {
    if (focus || !search.trim()) return tables
    const q = search.trim().toLowerCase()
    return tables.filter((t) => t.name.toLowerCase().includes(q) || (t.folder ?? '').toLowerCase().includes(q))
  }, [tables, focus, search])

  const byUri = useMemo(() => Object.fromEntries(visible.map((t) => [t.uri, t.id])), [visible])
  const pkOf = (t: CatalogTable) => t.keys?.find((k) => k.confidence === 'declared')?.columns ?? []

  const nodes: Node[] = useMemo(() => visible.map((t, i) => ({
    id: t.id, type: 'entity',
    position: positions[t.id] ?? { x: (i % 3) * 300, y: Math.floor(i / 3) * 300 },
    data: { table: t, pk: pkOf(t), focused: t.uri === visibleFocus, onFocus: () => setFocus(t.uri) } satisfies EntityData,
  })), [visible, positions, visibleFocus])

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
    if (mode === 'lineage') linEdges.forEach((e, i) => {
      const s = byUri[e.parent], t = byUri[e.child]
      if (!s || !t) return
      out.push({
        id: `l${i}`, source: s, target: t, selectable: false,
        markerEnd: { type: MarkerType.ArrowClosed },
        style: { stroke: 'var(--muted-foreground)', strokeWidth: 1.5 },
      })
    })
    if (showSuggestions) for (let a = 0; a < visible.length; a++)
      for (let b = a + 1; b < visible.length; b++) {
        const ta = visible[a], tb = visible[b]
        if (declared.has([ta.id, tb.id].sort().join('|')) || !sharesKey(ta, tb)) continue
        out.push({
          id: `c-${ta.id}-${tb.id}`, source: ta.id, target: tb.id, selectable: false,
          style: { stroke: 'var(--muted-foreground)', strokeDasharray: '4 3', opacity: 0.45 },
        })
      }
    return out
  }, [rels, visible, byUri, mode, linEdges, showSuggestions])

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

  const onEdgeClick = useCallback(async (_e: React.MouseEvent, edge: Edge) => {
    const rel = (edge.data as { rel?: Relationship } | undefined)?.rel
    if (!rel || !window.confirm(`Remove declared relationship ${rel.leftColumns.join('+')} = ${rel.rightColumns.join('+')}?`)) return
    try { setRels(await api.deleteRelationship(rel)) } catch (e) { pushToast(errorMessage(e), 'error') }
  }, [pushToast])

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setPositions((p) => {
      if (!changes.some((ch) => ch.type === 'position' && ch.position)) return p
      const next = { ...p }
      for (const ch of changes) if (ch.type === 'position' && ch.position) next[ch.id] = ch.position
      savePositions(next)
      return next
    })
  }, [])

  const capped = !focus && total > visible.length

  return (
    <div className="relative h-full w-full">
      <div className="absolute left-3 top-3 z-10 flex w-[320px] flex-col gap-2 rounded-lg border border-border bg-card/95 px-3 py-2.5 text-[11px] text-muted-foreground shadow-sm backdrop-blur">
        <div className="flex items-center gap-2">
          <span className="text-[12.5px] font-semibold text-foreground">Relationships</span>
          <span className="flex-1" />
          <button onClick={() => setShowHelp((v) => !v)} aria-label="How this works" title="How this works"
            className="grid h-5 w-5 place-items-center rounded-full border border-border text-[11px] font-bold hover:bg-accent">?</button>
        </div>

        {focus ? (
          <div className="flex flex-col gap-2" data-testid="er-focus-bar">
            <div className="flex items-center gap-1.5">
              <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10.5px] font-semibold text-primary">Focused: {focusName}</span>
              <button onClick={() => setFocus(null)} className="text-[10.5px] underline hover:text-foreground" data-testid="er-clear-focus">show all</button>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[10.5px]">Hops</span>
              <div className="inline-flex items-center rounded-md border border-border">
                <button onClick={() => setHops((h) => Math.max(1, h - 1))} className="px-1.5 py-0.5 hover:bg-accent" aria-label="Fewer hops">−</button>
                <span className="w-5 text-center text-[11px] font-semibold text-foreground" data-testid="er-hops">{hops}</span>
                <button onClick={() => setHops((h) => Math.min(5, h + 1))} className="px-1.5 py-0.5 hover:bg-accent" aria-label="More hops">+</button>
              </div>
              <span className="flex-1" />
              <div className="inline-flex rounded-md border border-border p-0.5 text-[10.5px]">
                {(['joins', 'lineage'] as const).map((m) => (
                  <button key={m} onClick={() => setMode(m)} data-testid={`er-mode-${m}`}
                    className={cn('rounded px-1.5 py-0.5', mode === m ? 'bg-accent font-semibold text-foreground' : 'hover:text-foreground')}>{m}</button>
                ))}
              </div>
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

        <div className="flex items-center gap-2 text-[10px]">
          <span className="inline-flex items-center gap-1"><span className="inline-block h-0 w-3 border-t-[1.5px] border-primary" /> declared join</span>
          {mode === 'lineage' && <span className="inline-flex items-center gap-1"><span className="inline-block h-0 w-3 border-t-[1.5px] border-muted-foreground" /> lineage</span>}
          <label className="ml-auto inline-flex cursor-pointer items-center gap-1">
            <input type="checkbox" checked={showSuggestions} onChange={(e) => setShowSuggestions(e.target.checked)} data-testid="er-suggestions-toggle" className="h-3 w-3 accent-primary" />
            suggestions
          </label>
        </div>

        {showHelp && (
          <div className="rounded-md border border-border bg-muted/40 p-2 text-[10.5px] leading-relaxed">
            Drag from one entity to another to declare a join. Click a solid edge to remove it. Click an entity title to
            re-focus the graph on it. Declare a primary key from a dataset detail drawer in Workspace.
          </div>
        )}

        {loading && <span data-testid="er-catalog-loading">Loading…</span>}
        {error && (
          <span role="alert" className="text-destructive">
            Couldn't load: {error}{' '}
            <button onClick={refresh} data-testid="er-catalog-retry" className="font-semibold underline">Retry</button>
          </span>
        )}
        {relsError && (
          <span role="alert" className="text-destructive">
            Couldn't load declared relationships: {relsError}{' '}
            <button onClick={() => void loadRelationships()} data-testid="er-relationships-retry" className="font-semibold underline">Retry</button>
          </span>
        )}
        {capped && <span className="text-[10px] text-amber-600">Showing {visible.length} of {total} — focus a table or pick a folder.</span>}
      </div>

      {!loading && !error && visible.length === 0 && (
        <div className="pointer-events-none absolute inset-0 z-[5] grid place-items-center text-[13px] text-muted-foreground">
          {focus ? 'No neighbours at this hop distance.' : total === 0 ? (
            <span className="pointer-events-auto">No datasets registered yet — add some in <button onClick={() => openWorkspace('workspace')} className="underline">Workspace</button>.</span>
          ) : 'No datasets in this folder.'}
        </div>
      )}

      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange}
        onConnect={onConnect} onEdgeClick={onEdgeClick} fitView minZoom={0.2} colorMode={resolvedTheme()}
        proOptions={{ hideAttribution: true }}>
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--dots)" />
        <Controls showInteractive={false} />
      </ReactFlow>
      {pending && (
        <RelationshipDialog key={`${pending.left.id}|${pending.right.id}`}
          left={pending.left} right={pending.right} suggestions={pending.suggestions}
          suggestionsLoading={pending.suggestionsLoading} suggestionsError={pending.suggestionsError}
          onRetrySuggestions={() => void loadSuggestions(pending.left, pending.right)}
          onClose={() => setPending(null)}
          onDeclared={(next) => { setRels(next); setPending(null) }} />
      )}
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
