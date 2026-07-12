import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow, Background, BackgroundVariant, Controls, Handle, Position, MarkerType,
  type Node, type Edge, type Connection, type NodeChange,
} from '@xyflow/react'
import { useStore } from '../store/graph'
import { api } from '../api/client'
import { resolvedTheme } from '../theme/mode'
import { MiniSelect } from '../ui/controls'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { CatalogTable, Relationship, JoinSuggestion, Cardinality } from '../types/api'
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

// The ER view renders one ENTITY per table + O(n²) join hints, so it operates on a BOUNDED set — a
// single folder (or all), capped — rather than the whole catalog (which can be thousands of tables).
const ER_CAP = 60

export function ERDiagram() {
  const pushToast = useStore((s) => s.pushToast)
  const [catalog, setCatalog] = useState<CatalogTable[]>([])
  const [folder, setFolder] = useState('')
  const [folders, setFolders] = useState<string[]>([])
  const [total, setTotal] = useState(0)
  const [rels, setRels] = useState<Relationship[]>([])
  const [catalogLoading, setCatalogLoading] = useState(true)
  const [catalogError, setCatalogError] = useState<string | null>(null)
  const [foldersLoading, setFoldersLoading] = useState(true)
  const [foldersError, setFoldersError] = useState<string | null>(null)
  const [relsLoading, setRelsLoading] = useState(true)
  const [relsError, setRelsError] = useState<string | null>(null)
  const [pending, setPending] = useState<{
    left: CatalogTable; right: CatalogTable; suggestions: JoinSuggestion[]
    suggestionsLoading: boolean; suggestionsError: string | null
  } | null>(null)
  // node layout survives navigation (the view unmounts) via localStorage, keyed by table id
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>(loadPositions)
  const catalogRequest = useRef(0)
  const foldersRequest = useRef(0)
  const relsRequest = useRef(0)
  const loadedFolder = useRef<string | null>(null)

  const reload = useCallback(async () => {
    const s = ++catalogRequest.current
    // Rows from another folder are not a valid stale view for the newly selected folder. A same-
    // folder refresh failure (for example after declaring a key) keeps the last graph and labels it.
    if (loadedFolder.current !== folder) { setCatalog([]); setTotal(0) }
    setCatalogLoading(true); setCatalogError(null)
    try {
      const page = await api.tablesPage({ folder: folder || undefined, limit: ER_CAP, sort: 'usage', order: 'desc' })
      if (s !== catalogRequest.current) return
      setCatalog(page.items); setTotal(page.total); loadedFolder.current = folder
    } catch (e) {
      if (s === catalogRequest.current) setCatalogError(errorMessage(e))
    } finally {
      if (s === catalogRequest.current) setCatalogLoading(false)
    }
  }, [folder])

  const loadRelationships = useCallback(async () => {
    const s = ++relsRequest.current
    setRelsLoading(true); setRelsError(null)
    try {
      const next = await api.relationships()
      if (s === relsRequest.current) setRels(next)
    } catch (e) {
      if (s === relsRequest.current) setRelsError(errorMessage(e))
    } finally {
      if (s === relsRequest.current) setRelsLoading(false)
    }
  }, [])

  const loadFolders = useCallback(async () => {
    const s = ++foldersRequest.current
    setFoldersLoading(true); setFoldersError(null)
    try {
      const next = await api.facets()
      if (s === foldersRequest.current) setFolders(next.folders.map((x) => x.value))
    } catch (e) {
      if (s === foldersRequest.current) setFoldersError(errorMessage(e))
    } finally {
      if (s === foldersRequest.current) setFoldersLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadRelationships(); void loadFolders()
    return () => { relsRequest.current += 1; foldersRequest.current += 1 }
  }, [loadRelationships, loadFolders])
  useEffect(() => {
    void reload()
    return () => { catalogRequest.current += 1 }
  }, [reload])
  const refreshCatalog = reload  // local reload after a key/relationship edit
  const visibleCatalog = loadedFolder.current === folder ? catalog : []

  const byUri = useMemo(() => Object.fromEntries(visibleCatalog.map((t) => [t.uri, t.id])), [visibleCatalog])
  const pkOf = (t: CatalogTable) => t.keys?.find((k) => k.confidence === 'declared')?.columns ?? []

  // clicking a column toggles its membership in the declared primary key — so clicking several
  // columns builds a COMPOSITE key (in click order); clicking a member again removes it.
  const toggleCol = useCallback(async (tableId: string, col: string) => {
    const t = visibleCatalog.find((x) => x.id === tableId)
    if (!t) return
    const cur = t.keys?.find((k) => k.confidence === 'declared')?.columns ?? []
    const next = cur.includes(col) ? cur.filter((c) => c !== col) : [...cur, col]
    try {
      await api.declareKey(tableId, next)
      await refreshCatalog()
    } catch (e) { pushToast(String((e as Error).message || e), 'error') }
  }, [visibleCatalog, refreshCatalog, pushToast])

  const nodes: Node[] = useMemo(() => visibleCatalog.map((t, i) => ({
    id: t.id, type: 'entity',
    position: positions[t.id] ?? { x: (i % 3) * 300, y: Math.floor(i / 3) * 300 },
    data: { table: t, pk: pkOf(t), onToggle: (col: string) => toggleCol(t.id, col) } satisfies EntityData,
  })), [visibleCatalog, positions, toggleCol])

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
    for (let a = 0; a < visibleCatalog.length; a++)
      for (let b = a + 1; b < visibleCatalog.length; b++) {
        const ta = visibleCatalog[a], tb = visibleCatalog[b]
        if (declared.has([ta.id, tb.id].sort().join('|')) || !sharesKey(ta, tb)) continue
        out.push({
          id: `c-${ta.id}-${tb.id}`, source: ta.id, target: tb.id, selectable: false,
          style: { stroke: 'var(--muted-foreground)', strokeDasharray: '4 3', opacity: 0.45 },
        })
      }
    return out
  }, [rels, visibleCatalog, byUri])

  // dragging between two entities opens the picker (choose a suggested key or set columns/cardinality
  // by hand) rather than blindly declaring the top suggestion — needed for tables with several FKs.
  const loadSuggestions = useCallback(async (left: CatalogTable, right: CatalogTable) => {
    setPending((cur) => cur?.left.id === left.id && cur.right.id === right.id
      ? { ...cur, suggestionsLoading: true, suggestionsError: null }
      : { left, right, suggestions: [], suggestionsLoading: true, suggestionsError: null })
    try {
      const suggestions = await api.joinSuggestions(left.uri, right.uri)
      setPending((cur) => cur?.left.id === left.id && cur.right.id === right.id
        ? { ...cur, suggestions, suggestionsLoading: false }
        : cur)
    } catch (e) {
      setPending((cur) => cur?.left.id === left.id && cur.right.id === right.id
        ? { ...cur, suggestionsLoading: false, suggestionsError: errorMessage(e) }
        : cur)
    }
  }, [])
  const onConnect = useCallback((c: Connection) => {
    const s = visibleCatalog.find((t) => t.id === c.source), t = visibleCatalog.find((x) => x.id === c.target)
    if (!s || !t || s.id === t.id) return
    void loadSuggestions(s, t)
  }, [visibleCatalog, loadSuggestions])

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

  const capped = total > visibleCatalog.length

  return (
    <div className="relative h-full w-full">
      <div className="absolute left-3 top-3 z-10 flex max-w-[360px] flex-col gap-1.5 rounded-md border border-border bg-card/90 px-3 py-2 text-[10.5px] leading-relaxed text-muted-foreground backdrop-blur">
        <div className="flex items-center gap-2">
          <span className="text-[12px] font-semibold text-foreground">Relationships (ER)</span>
          <select value={folder} onChange={(e) => setFolder(e.target.value)}
            className="ml-auto rounded border border-border bg-card px-1.5 py-0.5 text-[10.5px] outline-none" data-testid="er-folder">
            <option value="">All folders</option>
            {folders.map((f) => <option key={f} value={f}>{f}</option>)}
          </select>
        </div>
        🔑 click column(s) to declare the primary key · drag between two tables to declare a join · click a solid edge to remove · dashed = possible join
        {catalogLoading && <span data-testid="er-catalog-loading">Loading datasets…</span>}
        {catalogError && (
          <span role="alert" className="text-destructive">
            Couldn't load datasets: {catalogError}{visibleCatalog.length ? ' (showing stale graph)' : ''}{' '}
            <button onClick={() => void reload()} data-testid="er-catalog-retry" className="font-semibold underline">Retry</button>
          </span>
        )}
        {foldersLoading && <span>Loading folders…</span>}
        {foldersError && (
          <span role="alert" className="text-destructive">
            Couldn't load folders: {foldersError}{folders.length ? ' (showing stale folders)' : ''}{' '}
            <button onClick={() => void loadFolders()} data-testid="er-folders-retry" className="font-semibold underline">Retry</button>
          </span>
        )}
        {relsLoading && <span>Loading declared relationships…</span>}
        {relsError && (
          <span role="alert" className="text-destructive">
            Couldn't load declared relationships: {relsError}{rels.length ? ' (showing stale relationships)' : ''}{' '}
            <button onClick={() => void loadRelationships()} data-testid="er-relationships-retry" className="font-semibold underline">Retry</button>
          </span>
        )}
        {capped && <span className="text-[10px] text-amber-600">Showing {visibleCatalog.length} of {total} — pick a folder to focus the diagram.</span>}
      </div>
      {!catalogLoading && !catalogError && visibleCatalog.length === 0 && (
        <div className="pointer-events-none absolute inset-0 z-[5] grid place-items-center text-[13px] text-muted-foreground">
          {total === 0 && !folder ? 'No datasets registered yet — add some in Tables.' : 'No datasets in this folder.'}
        </div>
      )}
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange}
        onConnect={onConnect} onEdgeClick={onEdgeClick} fitView minZoom={0.2} colorMode={resolvedTheme()}
        proOptions={{ hideAttribution: true }}>
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--dots)" />
        <Controls showInteractive={false} />
      </ReactFlow>
      {pending && (
        // keyed by the pair so switching pending pairs REMOUNTS (re-seeds lc/rc/card) — otherwise a
        // rapid second drag would reuse the dialog with the first pair's stale columns
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
const errorMessage = (e: unknown) => e instanceof Error ? e.message : String(e)

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

  // Suggestions now load inside the open dialog. Preserve the old top-suggestion seeding behavior,
  // but never overwrite columns/cardinality the user already chose while that request was pending.
  useEffect(() => {
    if (!top || keysTouched) return
    setLc(top.leftColumns); setRc(top.rightColumns); setCard(top.cardinality)
  }, [top, keysTouched])

  const toggle = (arr: string[], set: (v: string[]) => void, col: string) => {
    setKeysTouched(true)
    set(arr.includes(col) ? arr.filter((c) => c !== col) : [...arr, col])
    setCard('unknown')  // hand-editing the key invalidates a cardinality that was MEASURED for another key
  }
  const pick = (s: JoinSuggestion) => { setKeysTouched(true); setLc(s.leftColumns); setRc(s.rightColumns); setCard(s.cardinality) }
  const ok = lc.length > 0 && lc.length === rc.length
  const declare = async () => {
    setBusy(true)
    try {
      onDeclared(await api.addRelationship({ leftUri: left.uri, leftColumns: lc, rightUri: right.uri, rightColumns: rc, cardinality: card, confidence: 'declared' }))
      pushToast(`declared ${left.name} → ${right.name} (${card})`, 'success')
    } catch (e) { pushToast(String((e as Error).message || e), 'error'); setBusy(false) }
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
