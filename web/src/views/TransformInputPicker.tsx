import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'

export type TransformInput = {
  kind: 'registered' | 'provider'
  id: string
  name: string
  /** Absent when this catalog result does not provide a known, nonempty schema. */
  columnNames?: string[]
}

type Choice = TransformInput & { key: string; detail: string; unavailable?: string }
type ChoicePage = { items: Choice[]; cursor: string | null; notices: string[] }
const PAGE_SIZE = 12
const message = (error: unknown) => error instanceof Error ? error.message : String(error)

async function loadChoices(kind: TransformInput['kind'], query: string, cursor?: string): Promise<ChoicePage> {
  if (kind === 'registered') {
    const offset = cursor ? Number(cursor) : 0
    const page = await api.tablesPage({
      q: query || undefined, limit: PAGE_SIZE, offset, sort: 'usage', order: 'desc',
    })
    return {
      items: page.items.map((table) => ({
        kind, key: table.id, id: table.registrationId ?? '', name: table.name,
        columnNames: table.columns.length ? table.columns.map((column) => column.name) : undefined,
        detail: `${table.folder ? `${table.folder} · ` : ''}${table.rowCount == null ? 'Unknown' : table.rowCount.toLocaleString()} rows · ${table.columns.length} columns`,
        unavailable: table.missing ? 'Source file is unavailable'
          : !table.registrationId ? 'Register this dataset in Workspace first' : undefined,
      })),
      cursor: page.hasMore ? String(offset + PAGE_SIZE) : null,
      notices: [],
    }
  }
  // Match the Source picker's Workspace admission rules. Only the opaque resource reference is
  // submitted; the server resolves provider identity, URI, and the admitted revision together.
  const page = await api.workspaceSearch(query, { limit: PAGE_SIZE, kinds: ['dataset'], ...(cursor ? { cursor } : {}) })
  return {
    items: page.groups.flatMap((group) => group.items
      .filter((item) => group.source.kind === 'provider' && ['complete', 'page'].includes(group.source.completeness)
        && item.source === 'provider' && item.kind === 'dataset' && !item.detached
        && item.referenceState === 'current' && !item.lastKnown
        && (!item.canonicalReferenceState || item.canonicalReferenceState === 'current'))
      .map((item) => ({
        kind, key: item.id, id: item.id, name: item.name,
        detail: `${group.source.provider ?? item.provider ?? 'Connected catalog'}${item.providerDatasetId ? ` · ${item.providerDatasetId}` : ''}`,
      }))),
    cursor: page.hasMore ? page.nextCursor ?? null : null,
    notices: page.groups.filter((group) => group.source.kind === 'provider'
      && !['complete', 'page'].includes(group.source.completeness))
      .map((group) => `${group.source.provider ?? 'Connected catalog'}: ${group.source.error ?? group.source.completeness}`),
  }
}

/** A single optional input, using the same registered and Workspace catalogs as Source. */
export function TransformInputPicker({ disabled, onSelect, onCancel }: {
  disabled: boolean
  onSelect: (input: TransformInput) => void
  onCancel: () => void
}) {
  const [kind, setKind] = useState<TransformInput['kind']>('registered')
  const [query, setQuery] = useState('')
  const [retry, setRetry] = useState(0)
  const [page, setPage] = useState<ChoicePage | null>(null)
  const [error, setError] = useState('')
  const [loadingMore, setLoadingMore] = useState(false)
  const [resolvedSignature, setResolvedSignature] = useState('')
  const signature = JSON.stringify([kind, query.trim(), retry])
  const currentSignature = useRef(signature)
  currentSignature.current = signature
  const needsQuery = kind === 'provider' && !query.trim()
  const visiblePage = resolvedSignature === signature ? page : null

  useEffect(() => {
    let live = true
    setPage(null); setError(''); setLoadingMore(false)
    if (needsQuery) return () => { live = false }
    const timer = window.setTimeout(() => {
      void loadChoices(kind, query.trim()).then((next) => {
        if (live) { setPage(next); setResolvedSignature(signature) }
      }).catch((caught) => { if (live) setError(message(caught)) })
    }, query.trim() ? 150 : 0)
    return () => { live = false; window.clearTimeout(timer) }
  }, [signature])

  const loadMore = async () => {
    if (!visiblePage?.cursor || loadingMore) return
    const requestSignature = signature
    setLoadingMore(true); setError('')
    try {
      const next = await loadChoices(kind, query.trim(), visiblePage.cursor)
      if (currentSignature.current !== requestSignature) return
      setPage((previous) => ({
        ...next,
        items: [...(previous?.items ?? []), ...next.items.filter((item) => !previous?.items.some((old) => old.key === item.key))],
        notices: [...new Set([...(previous?.notices ?? []), ...next.notices])],
      }))
    } catch (caught) {
      if (currentSignature.current === requestSignature) setError(message(caught))
    } finally {
      if (currentSignature.current === requestSignature) setLoadingMore(false)
    }
  }

  return <section aria-label="Choose Transform input" className="grid gap-2 rounded-lg border border-border p-3">
    <div className="flex items-center gap-2">
      <label className="flex-1 text-[11px] text-muted-foreground">Browse
        <select aria-label="Input dataset catalog" value={kind} disabled={disabled}
          onChange={(event) => { setKind(event.target.value as TransformInput['kind']); setQuery('') }} className="dp-input mt-1 w-full">
          <option value="registered">Registered datasets</option>
          <option value="provider">Connected catalogs</option>
        </select>
      </label>
      <button type="button" onClick={onCancel} disabled={disabled} className="text-[11px] font-semibold text-muted-foreground">Cancel selection</button>
    </div>
    <input aria-label="Search input datasets" placeholder={kind === 'provider' ? 'Search connected datasets…' : 'Search registered datasets…'}
      value={query} disabled={disabled} onChange={(event) => setQuery(event.target.value)} className="dp-input" />
    <div className="max-h-44 overflow-auto" aria-label="Input dataset results">
      {needsQuery ? <p className="p-2 text-[11px] text-muted-foreground">Enter a name to search connected catalogs.</p>
        : !visiblePage && !error ? <p role="status" className="p-2 text-[11px] text-muted-foreground">Loading datasets…</p>
          : visiblePage?.items.length === 0 && !visiblePage.notices.length && <p className="p-2 text-[11px] text-muted-foreground">No datasets found. Choose another search, or connect an input later.</p>}
      {visiblePage?.items.map((item) => <button key={item.key} type="button" disabled={disabled || !!item.unavailable}
        onClick={() => onSelect({ kind: item.kind, id: item.id, name: item.name, columnNames: item.columnNames })}
        className="block w-full rounded-md px-2 py-2 text-left hover:bg-accent disabled:opacity-50">
        <strong className="block truncate text-[12px]">{item.name}</strong>
        <span className="block truncate text-[10.5px] text-muted-foreground">{item.unavailable ?? item.detail}</span>
      </button>)}
      {visiblePage?.notices.map((notice) => <p key={notice} role="status" className="p-2 text-[11px] text-muted-foreground">{notice}</p>)}
      {visiblePage?.cursor && <button type="button" disabled={disabled || loadingMore} onClick={() => void loadMore()}
        className="w-full p-2 text-[11px] font-semibold text-primary">{loadingMore ? 'Loading…' : 'Load more datasets'}</button>}
    </div>
    {error && <div role="alert" className="text-[11px] text-destructive">Could not load datasets: {error}{' '}
      <button type="button" disabled={disabled} onClick={() => visiblePage ? void loadMore() : setRetry((value) => value + 1)} className="font-semibold underline">Retry dataset search</button>
    </div>}
  </section>
}
