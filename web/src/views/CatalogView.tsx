import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useStore } from '../store/graph'
import { api } from '../api/client'
import { color, radius } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { VirtualList } from '../ui/VirtualList'
import { FileDialog } from '../ui/FileDialog'
import type { CatalogQueryParams, CatalogTable, Facets, FolderNode, LineageResult } from '../types/api'

// The Tables catalog — built to browse thousands of datasets. Nothing is loaded up front: a left
// FOLDER TREE (lazy), a center VIRTUALIZED list fed by a server-side filtered/sorted/paginated query
// (infinite scroll), and a right FACET RAIL (tags/owners with counts). A search box (debounced) and a
// sort control drive the same query; clicking a row opens a detail drawer to inspect columns + lineage
// and curate the dataset's folder/tags/owner/description.

const PAGE = 50
const ROW_H = 58
type Sort = NonNullable<CatalogQueryParams['sort']>

export function CatalogView() {
  const addToCanvas = useStore((s) => s.addToCanvas)
  const uploadDataset = useStore((s) => s.uploadDataset)
  const pushToast = useStore((s) => s.pushToast)
  const fileRef = useRef<HTMLInputElement>(null)

  // query state
  const [rawQ, setRawQ] = useState('')
  const [q, setQ] = useState('')
  const [folder, setFolder] = useState('')
  const [tags, setTags] = useState<string[]>([])
  const [owner, setOwner] = useState('')
  const [sort, setSort] = useState<Sort>('name')
  const [order, setOrder] = useState<'asc' | 'desc'>('asc')

  // results + facets
  const [items, setItems] = useState<CatalogTable[]>([])
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [facets, setFacets] = useState<Facets>({ folders: [], tags: [], owners: [] })
  const [selected, setSelected] = useState<CatalogTable | null>(null)
  const [dialog, setDialog] = useState(false)
  const [uri, setUri] = useState('')
  const seq = useRef(0)

  // debounce the search box into the query
  useEffect(() => { const t = setTimeout(() => setQ(rawQ.trim()), 250); return () => clearTimeout(t) }, [rawQ])

  const params = useMemo<CatalogQueryParams>(
    () => ({ q: q || undefined, folder: folder || undefined, tags, owner: owner || undefined, sort, order, limit: PAGE }),
    [q, folder, tags, owner, sort, order])

  const loadFirst = useCallback(async () => {
    const s = ++seq.current
    setLoading(true)
    try {
      const [page, fc] = await Promise.all([
        api.tablesPage({ ...params, offset: 0 }),
        api.facets(params),
      ])
      if (s !== seq.current) return  // a newer query superseded this one
      setItems(page.items); setTotal(page.total); setHasMore(page.hasMore); setFacets(fc)
    } catch (e) {
      if (s === seq.current) pushToast(`Catalog: ${(e as Error).message}`, 'error')
    } finally {
      if (s === seq.current) setLoading(false)
    }
  }, [params, pushToast])

  useEffect(() => { void loadFirst() }, [loadFirst])

  const loadMore = useCallback(async () => {
    if (!hasMore || loading) return
    const s = seq.current
    try {
      const page = await api.tablesPage({ ...params, offset: items.length })
      if (s !== seq.current) return
      setItems((cur) => [...cur, ...page.items]); setHasMore(page.hasMore)
    } catch { /* keep what we have */ }
  }, [hasMore, loading, params, items.length])

  const toggleTag = (t: string) => setTags((cur) => cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t])
  const clearFilters = () => { setFolder(''); setTags([]); setOwner(''); setRawQ(''); setQ('') }
  const hasFilters = !!(folder || tags.length || owner || q)

  const register = async () => {
    const u = uri.trim(); if (!u) return
    try { await api.registerFile(u); setUri(''); await loadFirst() }
    catch (e) { pushToast((e as Error).message, 'error') }
  }
  const onUpload = async (f?: File) => { if (f && await uploadDataset(f)) await loadFirst() }
  const use = (t: CatalogTable) => addToCanvas('source', { uri: t.uri, tableId: t.id }, t.name)

  return (
    <div className="flex h-full flex-col">
      {/* header: title + register / upload */}
      <div className="flex items-center gap-3 px-7 pb-3 pt-5">
        <h1 className="text-[20px] font-bold text-foreground">Tables</h1>
        <span className="text-[12px] text-muted-foreground">{total.toLocaleString()} datasets</span>
        <span className="flex-1" />
        <input value={uri} onChange={(e) => setUri(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') register() }}
          data-testid="register-dataset" placeholder="Register a path / uri…"
          className="w-[260px] rounded-lg border border-border bg-card px-3 py-1.5 text-[12.5px] outline-none focus:border-primary" />
        <button onClick={register} className="rounded-lg bg-foreground px-3.5 py-1.5 text-[12.5px] font-semibold text-background">Register</button>
        <button onClick={() => fileRef.current?.click()} title="Upload a dataset file"
          className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-3.5 py-1.5 text-[12.5px] font-semibold text-foreground">
          <Icon name="export" size={13} /> Upload
        </button>
        <input ref={fileRef} type="file" accept=".parquet,.pq,.csv,.tsv,.json,.ndjson,.arrow,.feather,.ipc" className="hidden"
          onChange={(e) => { void onUpload(e.target.files?.[0]); e.target.value = '' }} />
      </div>

      {/* search + sort + active filters */}
      <div className="flex items-center gap-2 px-7 pb-2">
        <div className="relative flex-1">
          <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"><Icon name="search" size={13} /></span>
          <input value={rawQ} onChange={(e) => setRawQ(e.target.value)} data-testid="catalog-search"
            placeholder="Search by name, folder, description, or column…"
            className="w-full rounded-lg border border-border bg-card py-1.5 pl-8 pr-3 text-[13px] outline-none focus:border-primary" />
        </div>
        <select value={`${sort}:${order}`} onChange={(e) => { const [s, o] = e.target.value.split(':'); setSort(s as Sort); setOrder(o as 'asc' | 'desc') }}
          className="rounded-lg border border-border bg-card px-2 py-1.5 text-[12.5px] outline-none" data-testid="catalog-sort">
          <option value="name:asc">Name A–Z</option>
          <option value="name:desc">Name Z–A</option>
          <option value="rows:desc">Most rows</option>
          <option value="usage:desc">Most used</option>
          <option value="updated:desc">Recently updated</option>
          <option value="folder:asc">Folder</option>
        </select>
      </div>
      {hasFilters && (
        <div className="flex flex-wrap items-center gap-1.5 px-7 pb-2 text-[11.5px]">
          {folder && <Chip label={`📁 ${folder}`} onClear={() => setFolder('')} />}
          {tags.map((t) => <Chip key={t} label={`#${t}`} onClear={() => toggleTag(t)} />)}
          {owner && <Chip label={`@${owner}`} onClear={() => setOwner('')} />}
          {q && <Chip label={`"${q}"`} onClear={() => { setRawQ(''); setQ('') }} />}
          <button onClick={clearFilters} className="text-[11px] text-muted-foreground underline">clear all</button>
        </div>
      )}

      {/* body: folder tree | list | facets */}
      <div className="flex min-h-0 flex-1 border-t border-border">
        <div className="w-[220px] flex-[0_0_220px] overflow-y-auto border-r border-border p-2">
          <FolderTree selected={folder} onSelect={setFolder} />
        </div>

        <div className="flex min-w-0 flex-1 flex-col">
          <VirtualList
            items={items}
            rowHeight={ROW_H}
            onEndReached={loadMore}
            className="flex-1 px-3 py-2"
            emptyNote={<div className="grid h-full place-items-center text-[13px] text-muted-foreground">
              {loading ? 'Loading…' : hasFilters ? 'No datasets match these filters.' : 'No datasets registered — add one above.'}
            </div>}
            renderRow={(t) => <TableRow t={t} onOpen={() => setSelected(t)} onUse={() => use(t)} onFolder={setFolder} />}
          />
          <div className="border-t border-border px-4 py-1.5 text-[11px] text-muted-foreground">
            Showing {items.length.toLocaleString()} of {total.toLocaleString()}{hasMore ? ' — scroll for more' : ''}
          </div>
        </div>

        <div className="w-[220px] flex-[0_0_220px] overflow-y-auto border-l border-border p-3">
          <FacetGroup title="Tags">
            {facets.tags.map((f) => (
              <FacetRow key={f.value} label={`#${f.value}`} count={f.count}
                active={tags.includes(f.value)} onClick={() => toggleTag(f.value)} />
            ))}
            {!facets.tags.length && <Empty />}
          </FacetGroup>
          <FacetGroup title="Owners">
            {facets.owners.map((f) => (
              <FacetRow key={f.value} label={`@${f.value}`} count={f.count}
                active={owner === f.value} onClick={() => setOwner(owner === f.value ? '' : f.value)} />
            ))}
            {!facets.owners.length && <Empty />}
          </FacetGroup>
        </div>
      </div>

      {selected && (
        <CatalogDetail table={selected} onClose={() => setSelected(null)} onUse={use}
          onChanged={(t) => { setSelected(t); void loadFirst() }} onFolder={(f) => { setFolder(f); setSelected(null) }} />
      )}
      {dialog && <FileDialog mode="open" title="Open a dataset" onClose={() => setDialog(false)}
        onPick={async (r) => { setDialog(false); try { await api.registerFile(r.uri); await loadFirst() } catch { /* noop */ } }} />}
    </div>
  )
}

function Chip({ label, onClear }: { label: string; onClear: () => void }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-accent px-2 py-0.5 text-accent-foreground">
      {label}
      <button onClick={onClear} className="opacity-60 hover:opacity-100"><Icon name="close" size={10} /></button>
    </span>
  )
}

function Empty() { return <div className="px-1 py-1 text-[11px] text-muted-foreground">—</div> }

function FacetGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-3">
      <div className="mb-1 px-1 text-[10px] font-bold uppercase tracking-wide text-muted-foreground">{title}</div>
      <div className="flex flex-col gap-px">{children}</div>
    </div>
  )
}

function FacetRow({ label, count, active, onClick }: { label: string; count: number; active: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick}
      className={`flex items-center justify-between gap-2 rounded-md px-2 py-1 text-left text-[12px] hover:bg-accent ${active ? 'bg-accent font-semibold text-accent-foreground' : 'text-muted-foreground'}`}>
      <span className="truncate">{label}</span>
      <span className="text-[10.5px] tabular-nums opacity-70">{count.toLocaleString()}</span>
    </button>
  )
}

function TableRow({ t, onOpen, onUse, onFolder }: { t: CatalogTable; onOpen: () => void; onUse: () => void; onFolder: (f: string) => void }) {
  return (
    <div onClick={onOpen}
      className="group mx-1 flex h-[54px] cursor-pointer items-center gap-3 rounded-lg border border-border bg-card px-3 hover:border-primary/40 hover:bg-accent"
      style={{ opacity: t.missing ? 0.55 : 1 }}>
      <Icon name="db" size={16} style={{ color: color.text3 }} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-[13px] font-semibold text-foreground">{t.name}</span>
          {t.missing && <span className="rounded bg-destructive/10 px-1.5 text-[9.5px] font-semibold text-destructive">missing</span>}
          {(t.tags ?? []).slice(0, 3).map((tag) => (
            <span key={tag} className="rounded-full bg-muted px-1.5 text-[9.5px] text-muted-foreground">#{tag}</span>
          ))}
        </div>
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          {t.folder
            ? <button onClick={(e) => { e.stopPropagation(); onFolder(t.folder!) }} className="truncate hover:text-foreground hover:underline">📁 {t.folder}</button>
            : <span className="truncate">{t.uri}</span>}
        </div>
      </div>
      <span className="text-[11px] text-muted-foreground">{t.columns?.length ?? 0} cols</span>
      {t.rowCount != null && <span className="text-[11px] text-muted-foreground">· {t.rowCount.toLocaleString()} rows</span>}
      {t.owner && <span className="hidden text-[11px] text-muted-foreground lg:inline">· @{t.owner}</span>}
      <button onClick={(e) => { e.stopPropagation(); onUse() }}
        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-semibold text-primary opacity-0 group-hover:opacity-100">
        <Icon name="plus" size={12} /> Use
      </button>
    </div>
  )
}

// ---- folder tree (lazy) -----------------------------------------------------
function FolderTree({ selected, onSelect }: { selected: string; onSelect: (f: string) => void }) {
  const [root, setRoot] = useState<FolderNode[] | null>(null)
  useEffect(() => { api.catalogTree('').then((b) => setRoot(b.folders)).catch(() => setRoot([])) }, [])
  return (
    <div className="flex flex-col gap-px text-[12.5px]">
      <button onClick={() => onSelect('')}
        className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-left hover:bg-accent ${!selected ? 'bg-accent font-semibold text-accent-foreground' : 'text-muted-foreground'}`}>
        <Icon name="db" size={13} /> All tables
      </button>
      {root === null && <div className="px-2 py-1 text-[11px] text-muted-foreground">Loading…</div>}
      {root?.map((f) => <FolderBranch key={f.path} node={f} depth={0} selected={selected} onSelect={onSelect} />)}
      {root?.length === 0 && <div className="px-2 py-1 text-[11px] text-muted-foreground">No folders yet</div>}
    </div>
  )
}

function FolderBranch({ node, depth, selected, onSelect }: { node: FolderNode; depth: number; selected: string; onSelect: (f: string) => void }) {
  const [open, setOpen] = useState(false)
  const [kids, setKids] = useState<FolderNode[] | null>(null)
  const isSel = selected === node.path
  const expand = async () => {
    const next = !open; setOpen(next)
    if (next && kids === null) {
      try { setKids((await api.catalogTree(node.path)).folders) } catch { setKids([]) }
    }
  }
  return (
    <div>
      <div className={`flex items-center rounded-md hover:bg-accent ${isSel ? 'bg-accent' : ''}`} style={{ paddingLeft: depth * 12 }}>
        <button onClick={expand} className="grid h-6 w-5 place-items-center text-muted-foreground">
          <Icon name={open ? 'chevronDown' : 'chevronRight'} size={12} />
        </button>
        <button onClick={() => onSelect(node.path)}
          className={`flex flex-1 items-center justify-between gap-1.5 px-1 py-1 text-left ${isSel ? 'font-semibold text-accent-foreground' : 'text-muted-foreground'}`}>
          <span className="truncate">📁 {node.name}</span>
          <span className="text-[10px] tabular-nums opacity-60">{node.tableCount.toLocaleString()}</span>
        </button>
      </div>
      {open && kids?.map((k) => <FolderBranch key={k.path} node={k} depth={depth + 1} selected={selected} onSelect={onSelect} />)}
    </div>
  )
}

// ---- detail drawer: columns + metadata editor + lineage ---------------------
function CatalogDetail({ table, onClose, onUse, onChanged, onFolder }: {
  table: CatalogTable; onClose: () => void; onUse: (t: CatalogTable) => void
  onChanged: (t: CatalogTable) => void; onFolder: (f: string) => void
}) {
  const pushToast = useStore((s) => s.pushToast)
  const [folder, setFolder] = useState(table.folder ?? '')
  const [tags, setTags] = useState((table.tags ?? []).join(', '))
  const [owner, setOwner] = useState(table.owner ?? '')
  const [description, setDescription] = useState(table.description ?? '')
  const [lin, setLin] = useState<LineageResult | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => { api.lineage(table.uri, 4, 60).then(setLin).catch(() => setLin(null)) }, [table.uri])

  const save = async () => {
    setBusy(true)
    try {
      const next = await api.setTableMetadata(table.id, {
        folder: folder.trim(), owner: owner.trim() || null, description: description.trim() || null,
        tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
      })
      pushToast('Saved', 'success'); onChanged(next)
    } catch (e) { pushToast((e as Error).message, 'error') }
    finally { setBusy(false) }
  }
  const parents = (lin?.edges ?? []).filter((e) => e.child === table.uri)
  const children = (lin?.edges ?? []).filter((e) => e.parent === table.uri)
  const nameOf = (u: string) => lin?.nodes.find((n) => n.uri === u)?.name ?? u.split('/').slice(-1)[0]

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose}>
      <div className="flex h-full w-[420px] flex-col overflow-y-auto border-l border-border bg-card shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <Icon name="db" size={16} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-[14px] font-bold text-foreground">{table.name}</div>
            <div className="truncate text-[10.5px] text-muted-foreground">{table.uri}</div>
          </div>
          <button onClick={() => onUse(table)} data-testid="detail-use" className="inline-flex items-center gap-1 rounded-md bg-primary/10 px-2.5 py-1 text-[11.5px] font-semibold text-primary"><Icon name="plus" size={12} /> Use</button>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground"><Icon name="close" size={15} /></button>
        </div>

        <div className="flex flex-col gap-4 p-4 text-[12.5px]">
          <div className="flex flex-wrap gap-3 text-[11.5px] text-muted-foreground">
            <span>{table.rowCount == null ? '—' : table.rowCount.toLocaleString()} rows</span>
            <span>· {table.columns?.length ?? 0} cols</span>
            <span>· {table.version ?? 'v1'}</span>
            {table.usage ? <span>· used {table.usage}×</span> : null}
          </div>

          {/* organization editor */}
          <section className="flex flex-col gap-2 rounded-lg border border-border p-3">
            <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Organization</div>
            <Field label="Folder"><input value={folder} onChange={(e) => setFolder(e.target.value)} placeholder="prod/images" className="dp-input" data-testid="detail-folder" /></Field>
            <Field label="Tags"><input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="gold, pii (comma-separated)" className="dp-input" /></Field>
            <Field label="Owner"><input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="team or person" className="dp-input" /></Field>
            <Field label="Description"><textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} className="dp-input resize-y" /></Field>
            <div className="flex justify-end">
              <button onClick={save} disabled={busy} className="rounded-md bg-foreground px-3 py-1.5 text-[12px] font-semibold text-background disabled:opacity-50" data-testid="detail-save">{busy ? 'Saving…' : 'Save'}</button>
            </div>
          </section>

          {/* columns */}
          <section>
            <div className="mb-1 text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Columns</div>
            <div className="max-h-[220px] overflow-y-auto rounded-lg border border-border">
              {table.columns.map((c) => (
                <div key={c.name} className="flex items-center gap-2 border-b border-border/60 px-3 py-1 last:border-0">
                  <span className="w-3 text-center text-[10px]">{c.capabilities?.includes('key') ? '🔑' : ''}</span>
                  <span className="dp-mono flex-1 truncate text-[11.5px]">{c.name}</span>
                  <span className="text-[10px] text-muted-foreground">{c.type}</span>
                </div>
              ))}
            </div>
          </section>

          {/* lineage */}
          <section>
            <div className="mb-1 flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-muted-foreground"><Icon name="lineage" size={12} /> Lineage{lin?.truncated ? ' (truncated)' : ''}</div>
            <LineageMini label="Parents" empty="no upstream datasets" rows={parents.map((e) => ({ name: nameOf(e.parent), sub: e.pipeline ?? undefined }))} />
            <LineageMini label="Children" empty="no downstream datasets" rows={children.map((e) => ({ name: nameOf(e.child), sub: e.pipeline ?? undefined }))} />
          </section>

          {table.folder && (
            <button onClick={() => onFolder(table.folder!)} className="self-start text-[11.5px] text-primary hover:underline">Browse folder “{table.folder}” →</button>
          )}
        </div>
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10.5px] text-muted-foreground">{label}</span>
      {children}
    </label>
  )
}

function LineageMini({ label, empty, rows }: { label: string; empty: string; rows: { name: string; sub?: string }[] }) {
  return (
    <div className="mb-1.5">
      <div className="text-[9.5px] font-bold uppercase tracking-wide text-muted-foreground">{label}</div>
      {rows.length
        ? rows.map((r, i) => <div key={i} className="flex items-center gap-1.5 py-0.5 text-[12px] text-foreground"><Icon name="arrow" size={11} /> {r.name}{r.sub && <span className="text-[10px] text-muted-foreground">· {r.sub}</span>}</div>)
        : <div className="py-0.5 text-[11px] text-muted-foreground">{empty}</div>}
    </div>
  )
}
