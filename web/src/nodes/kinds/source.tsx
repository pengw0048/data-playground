import { useEffect, useRef, useState } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { roleCanEdit, useStore } from '../../store/graph'
import { Icon } from '../../ui/Icon'
import { Popover } from '../../ui/Popover'
import { FileDialog } from '../../ui/FileDialog'
import { api } from '../../api/client'
import type { CatalogTable } from '../../types/api'

function Source({ id, data }: NodeComponentProps) {
  const [open, setOpen] = useState(false)
  const [dialog, setDialog] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [q, setQ] = useState('')
  const [results, setResults] = useState<CatalogTable[] | null>(null)  // null = not yet searched
  const [resultsError, setResultsError] = useState<string | null>(null)
  const [searchRevision, setSearchRevision] = useState(0)
  const btnRef = useRef<HTMLButtonElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const catalog = useStore((s) => s.catalog)
  const kernelUp = useStore((s) => s.kernelUp)
  const uploadDataset = useStore((s) => s.uploadDataset)
  const rememberTables = useStore((s) => s.rememberTables)
  const updateConfig = useStore((s) => s.updateConfig)
  const rename = useStore((s) => s.rename)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  // show the bound dataset even when the source was configured by tableId or a bare catalog NAME (an
  // agent/example/programmatic source), not only by an exact uri match.
  const tid = data.config.tableId
  const ref = String(data.config.uri ?? '')
  const table = catalog.find((t) => (tid && t.id === tid) || t.uri === ref || t.name === ref)

  useEffect(() => {
    if (!canEdit) { setOpen(false); setDialog(false) }
  }, [canEdit])

  // Server-side search picker — the catalog can be thousands of tables, so we never render them all.
  // Empty query shows the working-set recents PLUS a top-usage page from the server (a fresh session
  // has an empty working set — without the fetch a full catalog would look empty); typing searches
  // the whole catalog.
  useEffect(() => {
    if (!open) return
    const term = q.trim()
    setResults(null); setResultsError(null)
    let live = true
    const timer = setTimeout(async () => {
      try {
        const r = await api.tablesPage({ q: term || undefined, limit: 12, sort: 'usage', order: 'desc' })
        if (live) setResults(Array.isArray(r.items) ? r.items : [])
      } catch (e) {
        if (live) setResultsError(e instanceof Error ? e.message : String(e))
      }
    }, term ? 200 : 0)
    return () => { live = false; clearTimeout(timer) }
  }, [q, open, searchRevision])

  const recentIds = new Set(catalog.map((t) => t.id))
  const shown = (q.trim()
    ? (results ?? [])
    : [...catalog, ...(results ?? []).filter((t) => !recentIds.has(t.id))]  // recents first, deduped
  ).slice(0, 12)
  const pick = (t: CatalogTable) => {
    if (!canEdit) return
    rememberTables([t])  // warm the cache so the card resolves this immediately
    updateConfig(id, { uri: t.uri, tableId: t.id })
    rename(id, t.name)
    setOpen(false); setQ('')
  }

  // upload a local file → store it + bind this source to it
  const onUpload = async (f: File | undefined) => {
    if (!f || !canEdit) return
    setOpen(false); setUploading(true)
    const t = await uploadDataset(f)  // uploads + refreshes catalog; toasts on failure
    setUploading(false)
    if (t) { updateConfig(id, { uri: t.uri, tableId: t.id }); rename(id, t.name) }
  }

  // pick a file from a destination (local dir / object store) → register it + use it as this source
  const pickFile = async (uri: string) => {
    if (!canEdit) return
    const t = await api.registerFile(uri)
    rememberTables([t]); updateConfig(id, { uri: t.uri, tableId: t.id }); rename(id, t.name)
    setDialog(false); setOpen(false)
  }

  const meta = table
    // an UNKNOWN count (null/undefined) shows "—", not a fake "0 rows" (UX-14)
    ? `${table.rowCount == null ? '—' : table.rowCount.toLocaleString()} rows · ${table.columns.length} cols · ${table.version ?? 'v1'}`
    : 'pick a table'

  return (
    <NodeCard id={id} data={data} metaOverride={meta}>
      {table ? (
        // show the BOUND dataset name (the node title is separately editable, so it can't be relied on
        // to say what's bound); the row itself is the "change" affordance, uri in the tooltip
        <button
          ref={btnRef}
          title={`${table.name} · ${String(data.config.uri ?? '')}\nClick to change dataset`}
          onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
          className="flex w-full items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1.5 text-[11.5px] text-muted-foreground"
        >
          <Icon name="db" size={13} />
          <span className="flex-1 truncate text-left font-medium text-foreground">{table.name}</span>
          <Icon name="chevronDown" size={12} />
        </button>
      ) : (
        <button
          ref={btnRef}
          onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
          className="flex w-full items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1.5 text-[11.5px] text-muted-foreground"
        >
          <Icon name="db" size={13} />
          <span className="flex-1 text-left">Select dataset</span>
          <Icon name="chevronDown" size={12} />
        </button>
      )}

      <Popover anchorRef={btnRef} open={open} onClose={() => setOpen(false)} width={250}>
        {/* search the whole catalog server-side (it can be thousands of tables) */}
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} onClick={(e) => e.stopPropagation()}
          placeholder="Search datasets…" data-testid="source-search"
          className="mb-1 w-full rounded-md border border-border bg-card px-2 py-1.5 text-[11.5px] outline-none focus:border-primary" />
        {resultsError && kernelUp && (
          <div role="alert" className="m-1 flex items-center justify-between gap-2 rounded-md border border-destructive/30 px-2 py-1.5 text-[11px] text-destructive">
            <span>Couldn't load catalog: {resultsError}{shown.length ? ' (showing recent datasets)' : ''}</span>
            <button onClick={(e) => { e.stopPropagation(); setSearchRevision((v) => v + 1) }} data-testid="source-search-retry"
              className="shrink-0 font-semibold underline">Retry</button>
          </div>
        )}
        {shown.length === 0 && (
          // distinguish a healthy-but-empty result from a down kernel (UX-14) — don't cry "offline" on
          // a fresh install with zero datasets
          <div className="p-2 text-[11.5px] text-muted-foreground">
            {!kernelUp ? 'Kernel offline — no catalog'
              : resultsError ? 'Catalog results unavailable'
              : q.trim() ? (results === null ? 'Searching…' : 'No matches')
              : results === null ? 'Loading…'
              : 'Catalog is empty — upload or browse below'}
          </div>
        )}
        {shown.map((t) => (
          <button
            key={t.id}
            onClick={(e) => { e.stopPropagation(); pick(t) }}
            className="flex w-full flex-col gap-px rounded-md px-[9px] py-[7px] text-left hover:bg-accent"
          >
            <span className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
              <span className="truncate">{t.name}</span>
              {t.folder && <span className="truncate text-[9.5px] font-normal text-muted-foreground">📁 {t.folder}</span>}
            </span>
            <span className="text-[10px] text-muted-foreground">
              {t.rowCount == null ? '—' : t.rowCount.toLocaleString()} rows · {t.columns.length} cols
            </span>
          </button>
        ))}
        <div className="my-1 h-px bg-border" />
        <button onClick={(e) => { e.stopPropagation(); setOpen(false); setDialog(true) }}
          className="flex w-full items-center gap-[7px] rounded-md px-[9px] py-[7px] text-left text-xs text-primary hover:bg-accent">
          <Icon name="search" size={12} /> Browse files…
        </button>
        <button onClick={(e) => { e.stopPropagation(); fileRef.current?.click() }}
          className="flex w-full items-center gap-[7px] rounded-md px-[9px] py-[7px] text-left text-xs text-primary hover:bg-accent">
          <Icon name="export" size={12} /> Upload a file…
        </button>
      </Popover>
      {uploading && <div className="mt-1 text-[10.5px] text-muted-foreground">Uploading…</div>}
      <input ref={fileRef} type="file" accept=".parquet,.pq,.csv,.tsv,.json,.ndjson,.arrow,.feather,.ipc" style={{ display: 'none' }}
        onChange={(e) => { void onUpload(e.target.files?.[0]); e.target.value = '' }} />
      {dialog && <FileDialog mode="open" title="Open a dataset" onClose={() => setDialog(false)} onPick={(r) => pickFile(r.uri)} />}
    </NodeCard>
  )
}

register(
  {
    kind: 'source',
    title: 'source',
    category: 'io',
    tag: 'dataset',
    inputs: [],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'read a registered dataset',
    defaultData: () => ({ title: 'source', status: 'draft', config: {}, meta: 'pick a table' }),
  },
  Source,
)
