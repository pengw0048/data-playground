import { useEffect, useMemo, useRef, useState } from 'react'
import { api, KernelError, type CanvasFile } from '../api/client'
import { roleCanEdit, useStore } from '../store/graph'
import type { SchemaCompatibility, TransformLibraryDetail, TransformLibraryEntry, WorkspaceResource } from '../types/api'
import type { CanvasDoc, CanvasNode } from '../types/graph'
import { compareSchemas } from '../lib/schemaCompatibility'
import { Icon } from '../ui/Icon'

const LOCAL_ROOT_ID = 'workspace-local-root'
const PAGE_SIZE = 25

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)
const identity = (resource: WorkspaceResource) => resource.id.slice(resource.id.indexOf(':') + 1)
const retained = (entry: TransformLibraryEntry) => (
  entry.retention.canvas + entry.retention.canvasVersion + entry.retention.executionManifest
)

function queryValue(query: string, key: string): string {
  return new URLSearchParams(query).get(key) ?? ''
}

export function TransformsLibrary() {
  const routeQuery = useStore((state) => state.transformLibraryQuery)
  const selectedId = useStore((state) => state.transformResourceId)
  const selectedVersion = useStore((state) => state.transformVersion)
  const upgradeCanvasId = useStore((state) => state.transformUpgradeCanvasId)
  const upgradeNodeId = useStore((state) => state.transformUpgradeNodeId)
  const setRouteQuery = useStore((state) => state.setTransformLibraryQuery)
  const setResource = useStore((state) => state.setTransformResource)
  const [items, setItems] = useState<TransformLibraryEntry[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [detail, setDetail] = useState<TransformLibraryDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [useEntry, setUseEntry] = useState<TransformLibraryEntry | null>(null)
  const [refreshEpoch, setRefreshEpoch] = useState(0)

  const filters = useMemo(() => ({
    q: queryValue(routeQuery, 'q'),
    source: (queryValue(routeQuery, 'source') || 'all') as 'all' | 'promoted' | 'plugin',
    mode: queryValue(routeQuery, 'mode'),
    category: queryValue(routeQuery, 'category'),
  }), [routeQuery])
  const listSignature = JSON.stringify([
    filters.q, filters.source, filters.mode, filters.category, refreshEpoch,
  ])
  const listSignatureRef = useRef(listSignature)
  listSignatureRef.current = listSignature

  const setFilter = (key: string, value: string) => {
    const params = new URLSearchParams(routeQuery)
    if (value && !(key === 'source' && value === 'all')) params.set(key, value)
    else params.delete(key)
    setRouteQuery(params.toString())
  }

  useEffect(() => {
    let live = true
    setLoading(true); setLoadingMore(false); setError(null); setItems([]); setNextCursor(null)
    void api.transformLibrary({ ...filters, limit: PAGE_SIZE }).then((page) => {
      if (!live) return
      setItems(page.items); setNextCursor(page.nextCursor ?? null)
    }).catch((caught) => { if (live) setError(errorMessage(caught)) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [filters.q, filters.source, filters.mode, filters.category, refreshEpoch])

  useEffect(() => {
    let live = true
    if (!selectedId) { setDetail(null); setDetailError(null); return () => { live = false } }
    setDetail(null); setDetailError(null)
    void api.transformLibraryDetail(selectedId, selectedVersion ?? undefined)
      .then((value) => { if (live) setDetail(value) })
      .catch((caught) => { if (live) setDetailError(errorMessage(caught)) })
    return () => { live = false }
  }, [selectedId, selectedVersion, refreshEpoch])

  const loadMore = async () => {
    if (!nextCursor || loadingMore) return
    const requestSignature = listSignature
    setLoadingMore(true); setError(null)
    try {
      const page = await api.transformLibrary({ ...filters, limit: PAGE_SIZE, cursor: nextCursor })
      if (listSignatureRef.current !== requestSignature) return
      setItems((current) => [...current, ...page.items])
      setNextCursor(page.nextCursor ?? null)
    } catch (caught) {
      if (listSignatureRef.current === requestSignature) setError(errorMessage(caught))
    } finally {
      if (listSignatureRef.current === requestSignature) setLoadingMore(false)
    }
  }

  const activeVersion = detail?.versions.find((version) => (
    version.version === (selectedVersion ?? detail.versions[0]?.version)
  )) ?? null
  const requestedMissing = !!selectedVersion && !!detail
    && !detail.versions.some((version) => version.version === selectedVersion)

  return <div className="mx-auto flex min-h-full w-full max-w-[1440px] flex-col px-5 py-5 sm:px-7">
    <header className="flex flex-wrap items-end gap-3 border-b border-border pb-4">
      <div className="min-w-[220px] flex-1">
        <h1 className="text-xl font-bold text-foreground">Transforms</h1>
        <p className="mt-1 text-[12px] text-muted-foreground">Pinned, immutable compute definitions for repeatable data work.</p>
      </div>
      <input aria-label="Search Transforms" value={filters.q} onChange={(event) => setFilter('q', event.target.value)} placeholder="Search title, description, category…" className="dp-input w-full sm:w-[300px]" />
      <select aria-label="Transform source" value={filters.source} onChange={(event) => setFilter('source', event.target.value)} className="dp-input w-[130px]">
        <option value="all">All sources</option><option value="promoted">Promoted</option><option value="plugin">Plugin</option>
      </select>
      <input aria-label="Transform mode" value={filters.mode} onChange={(event) => setFilter('mode', event.target.value)} placeholder="Mode" className="dp-input w-[105px]" />
      <input aria-label="Transform category" value={filters.category} onChange={(event) => setFilter('category', event.target.value)} placeholder="Category" className="dp-input w-[120px]" />
    </header>

    <div className="grid min-h-0 flex-1 grid-cols-1 gap-5 pt-5 lg:grid-cols-[minmax(360px,0.9fr)_minmax(460px,1.1fr)]">
      <section aria-label="Transform library" className="min-w-0">
        {loading && <div className="rounded-lg border border-border p-5 text-sm text-muted-foreground">Loading Transform library…</div>}
        {!loading && !items.length && !error && <div className="rounded-lg border border-dashed border-border p-6 text-sm text-muted-foreground">
          No matching Transforms. Promote a tested ad-hoc Transform from a Canvas, or adjust these filters.
        </div>}
        <div className="grid gap-2">
          {items.map((item) => <button key={`${item.id}@${item.version}`} onClick={() => setResource(item.id, item.version)}
            className={`grid w-full grid-cols-[minmax(0,1fr)_auto] gap-3 rounded-xl border p-3.5 text-left transition-colors hover:bg-accent ${selectedId === item.id ? 'border-primary bg-primary/5' : 'border-border bg-card'}`}>
            <span className="min-w-0">
              <span className="flex flex-wrap items-center gap-1.5"><strong className="truncate text-[13px] text-foreground">{item.title}</strong>
                <span className="rounded bg-muted px-1.5 py-0.5 text-[9.5px] font-semibold uppercase text-muted-foreground">{item.provenance}</span>
                {item.availability !== 'active' && <span className="rounded bg-destructive/10 px-1.5 py-0.5 text-[9.5px] font-semibold text-destructive">{item.availability}</span>}
              </span>
              <span className="mt-1 block line-clamp-2 text-[11px] leading-4 text-muted-foreground">{item.blurb || 'No description supplied.'}</span>
              <span className="mt-1.5 block text-[10.5px] text-muted-foreground">{item.category} · {item.mode} · {item.version}{item.versionCount > 1 ? ` · ${item.versionCount} versions` : ''}</span>
            </span>
            <Icon name="chevronRight" size={14} style={{ marginTop: 4 }} />
          </button>)}
        </div>
        {error && <div role="alert" className="mt-3 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-[12px] text-destructive">{error}</div>}
        {nextCursor && <button onClick={() => void loadMore()} disabled={loadingMore} className="mt-3 w-full rounded-lg border border-border px-3 py-2 text-[12px] font-semibold hover:bg-accent disabled:opacity-50">{loadingMore ? 'Loading…' : 'Load more'}</button>}
      </section>

      <section aria-label="Transform detail" className="min-w-0 rounded-xl border border-border bg-card p-5 lg:sticky lg:top-5 lg:self-start">
        {!selectedId && <div className="py-12 text-center text-sm text-muted-foreground">Select a Transform to inspect its exact versions and use it.</div>}
        {selectedId && !detail && !detailError && <div className="py-12 text-center text-sm text-muted-foreground">Loading exact versions…</div>}
        {detailError && <div role="alert" className="text-sm text-destructive">{detailError}</div>}
        {detail && <TransformDetail detail={detail} selected={activeVersion} requestedMissing={requestedMissing}
          onSelectVersion={(version) => setResource(
            detail.id, version,
            upgradeCanvasId && upgradeNodeId
              ? { canvasId: upgradeCanvasId, nodeId: upgradeNodeId } : null,
          )} onUse={setUseEntry}
          onRefresh={() => setRefreshEpoch((value) => value + 1)} />}
      </section>
    </div>
    {useEntry && <TransformUseDialog entry={useEntry} onClose={() => setUseEntry(null)} />}
  </div>
}

function TransformDetail({ detail, selected, requestedMissing, onSelectVersion, onUse, onRefresh }: {
  detail: TransformLibraryDetail; selected: TransformLibraryEntry | null; requestedMissing: boolean
  onSelectVersion: (version: string) => void; onUse: (entry: TransformLibraryEntry) => void
  onRefresh: () => void
}) {
  const upgradeCanvasId = useStore((state) => state.transformUpgradeCanvasId)
  const upgradeNodeId = useStore((state) => state.transformUpgradeNodeId)
  const refreshFiles = useStore((state) => state.refreshFiles)
  const openFile = useStore((state) => state.openFile)
  const selectNode = useStore((state) => state.select)
  const pushToast = useStore((state) => state.pushToast)
  const [target, setTarget] = useState<{ doc: CanvasDoc; node: CanvasNode; file: CanvasFile } | null>(null)
  const [targetError, setTargetError] = useState<string | null>(null)
  const [targetEpoch, setTargetEpoch] = useState(0)
  useEffect(() => {
    let live = true
    setTarget(null); setTargetError(null)
    if (!upgradeCanvasId || !upgradeNodeId) return () => { live = false }
    void (async () => {
      try {
        const doc = await api.getCanvas(upgradeCanvasId)
        await refreshFiles()
        if (!live) return
        const file = useStore.getState().files.find((candidate) => candidate.id === upgradeCanvasId)
        const node = doc.nodes.find((candidate) => candidate.id === upgradeNodeId)
        if (!file || !roleCanEdit(file.role)) throw new Error('The upgrade target is no longer editable')
        if (!node || node.type !== 'transform' || node.data.config.source !== 'library'
            || node.data.config.processor !== detail.id) {
          throw new Error('The exact upgrade target no longer matches this Transform')
        }
        setTarget({ doc, node, file })
      } catch (caught) { if (live) setTargetError(errorMessage(caught)) }
    })()
    return () => { live = false }
  }, [upgradeCanvasId, upgradeNodeId, detail.id, targetEpoch])
  const cfg = target?.node.data.config
  const upgradeFrom = target?.node.type === 'transform' && cfg?.source === 'library'
    && cfg.processor === detail.id ? detail.versions.find((version) => version.version === cfg.version) ?? null : null
  const inputDiff = upgradeFrom && selected ? compareSchemas(upgradeFrom.inputSchema, selected.inputSchema) : null
  const outputDiff = upgradeFrom && selected ? compareSchemas(upgradeFrom.outputSchema, selected.outputSchema) : null
  const showUpgrade = !!selected && !!target
    && cfg?.source === 'library' && cfg.processor === detail.id && cfg.version !== selected.version
  const canUpgrade = showUpgrade && inputDiff?.status === 'compatible'
    && outputDiff?.status === 'compatible' && selected?.availability === 'active'
  const [upgrading, setUpgrading] = useState(false)
  const [upgradeError, setUpgradeError] = useState<string | null>(null)

  const upgrade = async () => {
    if (!canUpgrade || !target || !selected) return
    setUpgrading(true); setUpgradeError(null)
    let result: Awaited<ReturnType<typeof api.workspaceAddTransform>>
    try {
      result = await api.workspaceAddTransform(target.doc.id, {
        transformId: selected.id, transformVersion: selected.version,
        expectedCanvasVersion: target.doc.version, replaceNodeId: target.node.id,
      })
    } catch (caught) {
      setUpgradeError(errorMessage(caught))
      setUpgrading(false)
      return
    }
    // The server mutation is committed. Clear the old target before any fallible local refresh so
    // a navigation failure can never make the exact upgrade look retryable.
    setTarget(null)
    setTargetEpoch((value) => value + 1)
    let opened = false
    try { await refreshFiles() } catch { /* committed; exact refetch below remains authoritative */ }
    try { opened = await openFile(result.id, { serverCopy: true }) } catch { /* committed */ }
    if (opened) {
      selectNode(result.nodeId)
      pushToast(`Upgraded to ${selected.version}; downstream results are stale`, 'success')
    } else {
      pushToast(`Upgraded to ${selected.version}, but the Canvas could not be opened. Open it from Workspace.`, 'info')
    }
    setUpgrading(false)
  }

  if (requestedMissing) return <div>
    <button onClick={() => history.back()} className="mb-4 text-[12px] font-semibold text-primary">Back</button>
    <h2 className="text-lg font-bold">Exact version unavailable</h2>
    <p className="mt-2 text-[12px] leading-5 text-muted-foreground">This deep link names a version that does not exist or is no longer accessible. No newer version was substituted.</p>
    <VersionList versions={detail.versions} selected={null} onSelect={onSelectVersion} />
  </div>
  if (!selected) return null
  const totalRetention = retained(selected)
  return <div>
    <div className="flex flex-wrap items-start gap-3">
      <div className="min-w-0 flex-1"><h2 className="truncate text-lg font-bold text-foreground">{selected.title}</h2>
        <p className="mt-1 text-[12px] leading-5 text-muted-foreground">{selected.blurb || 'No description supplied.'}</p></div>
      <button onClick={() => onUse(selected)} disabled={selected.availability !== 'active'} className="rounded-md bg-foreground px-3 py-2 text-[12px] font-semibold text-background disabled:opacity-40">Use exact {selected.version}</button>
    </div>
    <div className="mt-3 flex flex-wrap gap-1.5 text-[10.5px] text-muted-foreground">
      <span className="rounded bg-muted px-2 py-1">{selected.provenance}</span><span className="rounded bg-muted px-2 py-1">{selected.category}</span><span className="rounded bg-muted px-2 py-1">{selected.mode}</span>
      <span className={`rounded px-2 py-1 ${selected.availability === 'active' ? 'bg-emerald-500/10 text-emerald-700' : 'bg-destructive/10 text-destructive'}`}>{selected.availability}</span>
    </div>
    {upgradeCanvasId && upgradeNodeId && <section className="mt-4 rounded-lg border border-border bg-background p-3">
      <h3 className="text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Explicit upgrade target</h3>
      {target ? <p className="mt-1 text-[12px]"><strong>{target.file.name}</strong> · {target.node.data.title || target.node.id} <span className="font-mono text-[10px] text-muted-foreground">{target.node.id}</span></p>
        : targetError ? <div role="alert" className="mt-1 text-[11px] text-destructive">{targetError}</div>
          : <p className="mt-1 text-[11px] text-muted-foreground">Confirming Canvas, node, and edit role…</p>}
      <button onClick={() => setTargetEpoch((value) => value + 1)} className="mt-2 text-[11px] font-semibold text-primary">Reload exact target</button>
    </section>}
    <VersionList versions={detail.versions} selected={selected.version} onSelect={onSelectVersion} />
    <SchemaBlock label="Input schema" columns={selected.inputSchema} />
    <SchemaBlock label="Output schema" columns={selected.outputSchema} />
    <section className="mt-4"><h3 className="text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Requirements</h3>
      <div className="mt-1.5 rounded-md border border-border bg-background p-2 font-mono text-[11px] text-foreground">{selected.requirements.length ? selected.requirements.join('\n') : 'None'}</div></section>
    {detail.provenance === 'promoted' && <section className="mt-4"><h3 className="text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Retention</h3>
      <p className="mt-1 text-[11px] text-muted-foreground">{totalRetention ? `${totalRetention} durable references prevent deletion` : 'No retained Canvas, snapshot, or execution manifest references.'}</p>
      <div className="mt-1 grid grid-cols-3 gap-1 text-center text-[10px]"><span className="rounded bg-muted p-1.5">Canvas {selected.retention.canvas}</span><span className="rounded bg-muted p-1.5">Snapshots {selected.retention.canvasVersion}</span><span className="rounded bg-muted p-1.5">Runs {selected.retention.executionManifest}</span></div>
      {selected.availability === 'active' && !totalRetention && <DeleteVersion entry={selected} onDeleted={onRefresh} />}
    </section>}
    {showUpgrade && <section className="mt-4 rounded-lg border border-primary/30 bg-primary/5 p-3">
      <h3 className="text-[12px] font-bold">Upgrade selected node from {String(cfg?.version)} to {selected.version}</h3>
      <p className="mt-1 text-[11px] text-muted-foreground">This changes only the selected node's exact reference. Downstream results will be invalidated.</p>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <SchemaDiff label="Input" diff={inputDiff} />
        <SchemaDiff label="Output" diff={outputDiff} />
      </div>
      {!canUpgrade && <p className="mt-2 text-[11px] font-medium text-destructive">Upgrade is blocked because both schema transitions must be proven compatible; unknown evidence is not treated as compatible.</p>}
      {upgradeError && <div role="alert" className="mt-2 text-[11px] text-destructive">{upgradeError}</div>}
      <button onClick={() => void upgrade()} disabled={upgrading || !canUpgrade} className="mt-3 rounded-md bg-primary px-3 py-1.5 text-[11px] font-semibold text-primary-foreground disabled:opacity-50">{upgrading ? 'Upgrading…' : `Confirm exact upgrade to ${selected.version}`}</button>
    </section>}
  </div>
}

function SchemaDiff({ label, diff }: { label: string; diff: SchemaCompatibility | null }) {
  const changes = diff?.fields.filter((field) => field.kind !== 'unchanged') ?? []
  return <div className="rounded-md border border-border bg-background p-2 text-[10.5px]">
    <div className="flex items-center justify-between gap-2"><strong>{label} schema</strong><span className="font-semibold">{diff?.status ?? 'unknown'}</span></div>
    {!changes.length && <p className="mt-1 text-muted-foreground">No field changes.</p>}
    {changes.map((field, index) => {
      const before = field.oldName ?? field.fieldId ?? 'unknown field'
      const after = field.newName ?? field.fieldId ?? 'unknown field'
      const name = field.kind === 'added' ? `+ ${after}`
        : field.kind === 'removed' ? `− ${before}`
          : field.kind === 'renamed' ? `${before} → ${after}` : after
      return <div key={`${field.fieldId ?? name}-${index}`} className="mt-1.5 border-t border-border pt-1.5">
        <div className="flex flex-wrap items-baseline justify-between gap-1"><span className="font-mono font-semibold">{name}</span><span className="text-muted-foreground">{field.kind} · {field.status}</span></div>
        <p className="mt-0.5 leading-4 text-muted-foreground">{field.reason}</p>
      </div>
    })}
  </div>
}

function VersionList({ versions, selected, onSelect }: { versions: TransformLibraryEntry[]; selected: string | null; onSelect: (version: string) => void }) {
  return <section className="mt-4"><h3 className="text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Versions</h3>
    <div className="mt-1.5 flex flex-wrap gap-1.5">{versions.map((version) => <button key={version.version} onClick={() => onSelect(version.version)} className={`rounded-md border px-2 py-1 text-[11px] ${selected === version.version ? 'border-primary bg-primary/10 text-primary' : 'border-border'} ${version.availability !== 'active' ? 'line-through text-muted-foreground' : ''}`}>{version.version}{version.availability === 'deleted' ? ' · deleted' : ''}</button>)}</div>
  </section>
}

function SchemaBlock({ label, columns }: { label: string; columns: TransformLibraryEntry['inputSchema'] }) {
  return <section className="mt-4"><h3 className="text-[11px] font-bold uppercase tracking-wide text-muted-foreground">{label}</h3>
    <div className="mt-1.5 max-h-40 overflow-auto rounded-md border border-border bg-background">{columns.length ? columns.map((column) => <div key={column.fieldId ?? column.name} className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-b border-border px-2 py-1.5 text-[11px] last:border-b-0"><span className="truncate font-mono">{column.name}</span><span className="text-muted-foreground">{column.type}{column.nullable === false ? ' · required' : ''}</span></div>) : <div className="p-2 text-[11px] text-muted-foreground">No schema declared.</div>}</div>
  </section>
}

function DeleteVersion({ entry, onDeleted }: { entry: TransformLibraryEntry; onDeleted: () => void }) {
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const remove = async () => {
    setBusy(true); setError(null)
    try { await api.deleteTransformVersion(entry.id, entry.version); onDeleted() }
    catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
  }
  return <div className="mt-2">{confirm ? <div className="rounded-md border border-destructive/30 p-2 text-[11px]"><p>This tombstones {entry.version}; its number and definition cannot be reused.</p><div className="mt-2 flex gap-2"><button onClick={() => void remove()} disabled={busy} className="rounded bg-destructive px-2 py-1 font-semibold text-destructive-foreground">{busy ? 'Deleting…' : 'Delete exact version'}</button><button onClick={() => setConfirm(false)} disabled={busy}>Cancel</button></div></div> : <button onClick={() => setConfirm(true)} className="text-[11px] font-semibold text-destructive">Delete unreferenced version…</button>}{error && <div role="alert" className="mt-1 text-[11px] text-destructive">{error}</div>}</div>
}

function TransformUseDialog({ entry, onClose }: { entry: TransformLibraryEntry; onClose: () => void }) {
  const files = useStore((state) => state.files)
  const refreshFiles = useStore((state) => state.refreshFiles)
  const openFile = useStore((state) => state.openFile)
  const selectNode = useStore((state) => state.select)
  const pushToast = useStore((state) => state.pushToast)
  const [mode, setMode] = useState<'new' | 'existing'>('new')
  const [name, setName] = useState(`${entry.title} exploration`)
  const [canvasId, setCanvasId] = useState('')
  const [root, setRoot] = useState<WorkspaceResource | null>(null)
  const [destinationError, setDestinationError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const editable = files.filter((file) => file.role === 'owner' || file.role === 'editor')
  const refreshDestinations = async () => {
    setDestinationError(null)
    try {
      const [, page] = await Promise.all([
        refreshFiles(), api.workspaceBrowse(LOCAL_ROOT_ID, { limit: 1 }),
      ])
      if (!page.container) throw new Error('Workspace root is unavailable')
      setRoot(page.container)
    } catch (caught) {
      setRoot(null)
      setDestinationError(errorMessage(caught))
    }
  }
  useEffect(() => { void refreshDestinations() }, [])
  useEffect(() => { if (!editable.some((file) => file.id === canvasId)) setCanvasId(editable[0]?.id ?? '') }, [files, canvasId])
  const submit = async () => {
    if (busy) return
    setBusy(true); setError(null)
    let targetId: string
    let nodeId: string | null | undefined
    try {
      if (mode === 'new') {
        if (!root || root.version == null) throw new Error('Load the exact Workspace destination first')
        const result = await api.workspaceCreateCanvas({
          containerId: identity(root), expectedContainerVersion: root.version, name: name.trim(),
          transformId: entry.id, transformVersion: entry.version,
        })
        targetId = result.id; nodeId = result.nodeId
      } else {
        const target = editable.find((file) => file.id === canvasId)
        if (!target) throw new Error('Choose an editable target Canvas')
        const result = await api.workspaceAddTransform(target.id, {
          transformId: entry.id, transformVersion: entry.version,
          expectedCanvasVersion: target.version,
        })
        targetId = result.id; nodeId = result.nodeId
      }
    } catch (caught) {
      const message = errorMessage(caught)
      setError(caught instanceof KernelError && caught.status === 409
        ? `${message} Refresh the target list and retry; no other Canvas was changed.` : message)
      if (caught instanceof KernelError && caught.status === 409) await refreshDestinations()
      setBusy(false)
      return
    }
    try {
      await refreshFiles()
      if (await openFile(targetId, { serverCopy: true })) {
        if (nodeId) selectNode(nodeId)
      } else {
        pushToast(`Added ${entry.title}@${entry.version}, but the Canvas could not be opened. Open it from Workspace.`, 'info')
      }
    } catch {
      pushToast(`Added ${entry.title}@${entry.version}, but the Canvas could not be opened. Open it from Workspace.`, 'info')
    }
    onClose()
    setBusy(false)
  }
  const close = () => { if (!busy) onClose() }
  return <div className="fixed inset-0 z-50 grid place-items-center bg-black/30 p-4" onClick={close}>
    <div role="dialog" aria-modal="true" aria-label={`Use ${entry.title}`} className="grid w-[500px] max-w-full gap-3 rounded-xl border border-border bg-card p-5 shadow-xl" onClick={(event) => event.stopPropagation()}>
      <div className="flex items-center gap-2"><h2 className="flex-1 text-[15px] font-bold">Use {entry.title} · {entry.version}</h2><button onClick={close} disabled={busy} aria-label="Close"><Icon name="close" size={15} /></button></div>
      <p className="text-[11px] text-muted-foreground">The Canvas stores this exact immutable version. It will never follow a newer release automatically.</p>
      <div className="grid grid-cols-2 gap-2"><button onClick={() => setMode('new')} disabled={busy} aria-pressed={mode === 'new'} className={`rounded-lg border p-3 text-left ${mode === 'new' ? 'border-primary bg-primary/5' : 'border-border'}`}><strong className="block text-[12px]">Create new Canvas</strong><span className="text-[10.5px] text-muted-foreground">Create in Workspace</span></button><button onClick={() => setMode('existing')} disabled={busy} aria-pressed={mode === 'existing'} className={`rounded-lg border p-3 text-left ${mode === 'existing' ? 'border-primary bg-primary/5' : 'border-border'}`}><strong className="block text-[12px]">Add to Canvas</strong><span className="text-[10.5px] text-muted-foreground">Choose an exact editable target</span></button></div>
      {mode === 'new' ? <label className="grid gap-1 text-[11px] text-muted-foreground">Canvas name<input aria-label="New Canvas name" value={name} onChange={(event) => setName(event.target.value)} disabled={busy} className="dp-input" /></label> : editable.length ? <label className="grid gap-1 text-[11px] text-muted-foreground">Target Canvas<select aria-label="Target Canvas" value={canvasId} onChange={(event) => setCanvasId(event.target.value)} disabled={busy} className="dp-input">{editable.map((file) => <option key={file.id} value={file.id}>{file.name} · {file.id}</option>)}</select></label> : <div role="status" className="text-[12px] text-muted-foreground">No editable Canvas is available. Create a new Canvas instead.</div>}
      {destinationError && mode === 'new' && <div role="alert" className="text-[12px] text-destructive">Couldn't load Workspace: {destinationError}</div>}
      {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
      <div className="flex justify-end gap-2"><button onClick={close} disabled={busy} className="rounded-md border border-border px-3 py-1.5 text-[12px]">Cancel</button><button onClick={() => void submit()} disabled={busy || entry.availability !== 'active' || (mode === 'new' ? !name.trim() || !root : !canvasId)} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50">{busy ? 'Applying…' : mode === 'new' ? 'Create and open' : 'Add and open'}</button></div>
    </div>
  </div>
}
