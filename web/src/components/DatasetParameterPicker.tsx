import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { datasetRevisionTimeLabel } from '../lib/revisionTime'
import type { CatalogTable, DatasetRevisionCapabilities, DatasetRevisionDetail, DatasetRevisionPage, DatasetRevisionResolution } from '../types/api'

export type DatasetParameterValue = { kind: 'latest'; datasetId: string }
  | { kind: 'exact'; datasetId: string; revisionId: string }

type Context = {
  table: CatalogTable
  capabilities: DatasetRevisionCapabilities
  latest: DatasetRevisionResolution
  history: DatasetRevisionPage
}
const PAGE_SIZE = 12
const message = (error: unknown) => error instanceof Error ? error.message : String(error)
const field = 'w-full rounded-md border border-border bg-background px-2 py-1.5 disabled:opacity-60'


async function loadContext(table: CatalogTable, expectedDatasetId?: string): Promise<Context> {
  const capabilities = await api.datasetRevisionCapabilities(table.id)
  if (!capabilities.selectors.includes('latest') || !capabilities.selectors.includes('exact')) {
    throw new Error('This dataset does not support saved versions for run parameters. Choose a versioned dataset.')
  }
  const latest = await api.resolveDatasetRevision(table.id)
  if (expectedDatasetId && latest.datasetId !== expectedDatasetId) {
    throw new Error('The catalog entry now points to another dataset. Your saved selection is unchanged.')
  }
  const history = await api.datasetRevisions(table.id, { limit: PAGE_SIZE })
  return { table, capabilities, latest, history }
}

function exactError(error: unknown): string {
  const status = (error as { status?: number } | null)?.status
  const reason = status === 404 || status === 410 ? 'Selected version is missing or no longer retained.'
    : status === 403 ? 'Permission to read the selected version was lost.'
      : status === 503 ? 'The data source is offline; the selected version could not be verified.'
        : `Could not verify the selected version: ${message(error)}`
  return `${reason} Your selection is unchanged; it will not switch to latest automatically.`
}

/** DatasetRef identities come only from revision APIs, never from catalog/registration IDs. */
export function DatasetParameterPicker({ label, value, disabled = false, onChange }: {
  label: string
  value: unknown
  disabled?: boolean
  onChange: (value: DatasetParameterValue) => void
}) {
  const raw = value && typeof value === 'object' ? value as Partial<DatasetParameterValue> : undefined
  const datasetId = typeof raw?.datasetId === 'string' ? raw.datasetId : ''
  const kind = raw?.kind === 'exact' ? 'exact' : 'latest'
  const revisionId = raw?.kind === 'exact' && typeof raw.revisionId === 'string' ? raw.revisionId : ''
  const identity = JSON.stringify([label, datasetId, kind, revisionId])
  const [query, setQuery] = useState('')
  const [searchRetry, setSearchRetry] = useState(0)
  const [page, setPage] = useState<{ items: CatalogTable[]; hasMore: boolean; offset: number; key: string } | null>(null)
  const [searchError, setSearchError] = useState('')
  const [searchMore, setSearchMore] = useState(false)
  const [context, setContext] = useState<Context | null>(null)
  const [contextError, setContextError] = useState<{ message: string; selection?: { identity: string; searchKey: string } } | null>(null)
  const [contextRetry, setContextRetry] = useState(0)
  const [busy, setBusy] = useState(false)
  const [historyMore, setHistoryMore] = useState(false)
  const [detail, setDetail] = useState<{ identity: string; value?: DatasetRevisionDetail; error?: string } | null>(null)
  const [detailRetry, setDetailRetry] = useState(0)
  const generation = useRef(0)
  const searchKey = JSON.stringify([label, query.trim(), searchRetry, disabled])
  const current = useRef({ identity, searchKey, disabled, onChange })
  current.current = { identity, searchKey, disabled, onChange }
  const visibleContext = context?.latest.datasetId === datasetId ? context : null
  const visibleDetail = detail?.identity === identity ? detail : null
  const visiblePage = page?.key === searchKey ? page : null

  useEffect(() => () => { generation.current += 1 }, [])

  useEffect(() => {
    let live = true
    setSearchError(''); setSearchMore(false)
    if (disabled) return () => { live = false }
    const timer = window.setTimeout(() => {
      void api.tablesPage({ q: query.trim() || undefined, limit: PAGE_SIZE, offset: 0, sort: 'name', order: 'asc' })
        .then((next) => { if (live) setPage({ ...next, offset: 0, key: searchKey }) })
        .catch((error) => { if (live) setSearchError(message(error)) })
    }, query.trim() ? 150 : 0)
    return () => { live = false; window.clearTimeout(timer) }
  }, [searchKey])

  useEffect(() => {
    const ticket = ++generation.current
    setContextError(null); setHistoryMore(false)
    if (!datasetId) { setContext(null); setBusy(false); return }
    setBusy(true)
    void api.tableByRegistration(datasetId).then((table) => loadContext(table, datasetId))
      .then((next) => { if (ticket === generation.current) setContext(next) })
      .catch((error) => { if (ticket === generation.current) { setContext(null); setContextError({ message: message(error) }) } })
      .finally(() => { if (ticket === generation.current) setBusy(false) })
    return () => { generation.current += 1 }
  }, [datasetId, label, contextRetry])

  useEffect(() => {
    let live = true
    setDetail(null)
    if (kind !== 'exact' || !datasetId || !revisionId) return () => { live = false }
    void api.datasetRevision(datasetId, revisionId)
      .then((result) => {
        if (!live) return
        if (result.datasetId !== datasetId || result.revisionId !== revisionId) {
          setDetail({ identity, error: 'The server returned a different version. Your selection is unchanged.' })
        } else setDetail({ identity, value: result })
      })
      .catch((error) => { if (live) setDetail({ identity, error: exactError(error) }) })
    return () => { live = false }
  }, [identity, detailRetry])

  const choose = async (table: CatalogTable) => {
    if (disabled) return
    const ticket = ++generation.current
    const requestedIdentity = identity
    const requestedSearch = searchKey
    setBusy(true); setContextError(null)
    try {
      const next = await loadContext(table)
      if (ticket !== generation.current || current.current.disabled
          || current.current.identity !== requestedIdentity || current.current.searchKey !== requestedSearch) return
      setContext(next)
      current.current.onChange(kind === 'exact'
        ? { kind: 'exact', datasetId: next.latest.datasetId, revisionId: next.latest.revisionId }
        : { kind: 'latest', datasetId: next.latest.datasetId })
      setQuery('')
    } catch (error) {
      if (ticket === generation.current && current.current.identity === requestedIdentity
          && current.current.searchKey === requestedSearch && !current.current.disabled) {
        setContextError({ message: message(error), selection: { identity: requestedIdentity, searchKey: requestedSearch } })
      }
    } finally {
      if (ticket === generation.current) setBusy(false)
    }
  }

  const moreDatasets = async () => {
    if (!visiblePage || searchMore || disabled) return
    setSearchMore(true); setSearchError('')
    try {
      const offset = visiblePage.offset + PAGE_SIZE
      const next = await api.tablesPage({ q: query.trim() || undefined, limit: PAGE_SIZE, offset, sort: 'name', order: 'asc' })
      if (current.current.searchKey !== searchKey) return
      setPage({ ...next, items: [...visiblePage.items, ...next.items.filter((item) => !visiblePage.items.some((old) => old.id === item.id))], offset, key: searchKey })
    } catch (error) {
      if (current.current.searchKey === searchKey) setSearchError(message(error))
    } finally { if (current.current.searchKey === searchKey) setSearchMore(false) }
  }

  const moreVersions = async () => {
    if (!visibleContext?.history.nextCursor || historyMore || disabled) return
    const ticket = generation.current
    setHistoryMore(true); setContextError(null)
    try {
      const next = await api.datasetRevisions(visibleContext.table.id, { limit: PAGE_SIZE, cursor: visibleContext.history.nextCursor })
      if (ticket !== generation.current || current.current.identity !== identity) return
      setContext({ ...visibleContext, history: { ...next, items: [...visibleContext.history.items, ...next.items.filter((item) => !visibleContext.history.items.some((old) => old.datasetId === item.datasetId && old.revisionId === item.revisionId))] } })
    } catch (error) { if (ticket === generation.current) setContextError({ message: message(error) }) }
    finally { if (ticket === generation.current) setHistoryMore(false) }
  }

  const versions = visibleContext?.history.items.filter((item) => item.datasetId === datasetId) ?? []
  const selectedListed = versions.some((item) => item.revisionId === revisionId)
  const name = visibleContext?.table.name || visibleDetail?.value?.name
  const visibleContextError = contextError?.selection
    && (contextError.selection.identity !== identity || contextError.selection.searchKey !== searchKey)
    ? null : contextError
  return <div className="grid gap-1.5 text-[11px]" aria-label={`${label} dataset binding`}>
    {datasetId && <div className="rounded-md bg-muted/40 px-2 py-1.5">
      <strong className="block break-words">{name || 'Saved dataset'}</strong>
      {!name && <span className="block break-all text-[10px] text-muted-foreground">{datasetId}</span>}
      {visibleContext?.table.description && <span className="block text-muted-foreground">{visibleContext.table.description}</span>}
      <span className="block text-muted-foreground">{kind === 'latest' ? 'Follow latest: resolved again for each run.' : 'Selected version: stays fixed until you change it.'}</span>
    </div>}
    <select aria-label={`${label} selection`} value={kind} disabled={disabled || busy || !visibleContext}
      onChange={(event) => {
        if (!visibleContext) return
        onChange(event.target.value === 'latest' ? { kind: 'latest', datasetId }
          : { kind: 'exact', datasetId, revisionId: visibleContext.latest.revisionId })
      }} className={field}>
      <option value="latest">Follow latest</option><option value="exact">Selected version</option>
    </select>
    {kind === 'exact' && <select aria-label={`${label} version`} value={revisionId} disabled={disabled || busy || !visibleContext}
      onChange={(event) => { if (event.target.value) onChange({ kind: 'exact', datasetId, revisionId: event.target.value }) }} className={field}>
      {!selectedListed && <option value={revisionId}>{revisionId ? `Saved selection · ${revisionId}` : 'Choose a saved version'}</option>}
      {versions.map((item) => <option key={item.revisionId} value={item.revisionId}>{datasetRevisionTimeLabel(item.committedAt, item.retentionOwner) ?? 'Time unknown'} · {item.revisionId}</option>)}
    </select>}
    {kind === 'exact' && visibleContext?.history.hasMore && <button type="button" disabled={disabled || historyMore} onClick={() => void moreVersions()} className="text-left font-semibold text-primary disabled:opacity-50">{historyMore ? 'Loading versions…' : 'Load more versions'}</button>}
    {kind === 'latest' && visibleContext && <span className="text-muted-foreground">Current version: {datasetRevisionTimeLabel(visibleContext.latest.committedAt, visibleContext.latest.retentionOwner) ?? 'Time unknown'}</span>}
    {kind === 'exact' && datasetId && revisionId && !visibleDetail && <span role="status" className="text-muted-foreground">Checking selected version…</span>}
    {visibleDetail?.value && <span className="text-muted-foreground">{datasetRevisionTimeLabel(visibleDetail.value.committedAt, visibleDetail.value.retentionOwner) ?? 'Time unknown'}{visibleDetail.value.producerOperation ? ` · ${visibleDetail.value.producerOperation}` : ''}{visibleDetail.value.summary.rowCount != null ? ` · ${visibleDetail.value.summary.rowCount.toLocaleString()} rows` : ''}</span>}
    {visibleDetail?.error && <div role="alert" className="text-destructive">{visibleDetail.error}{' '}<button type="button" onClick={() => setDetailRetry((old) => old + 1)} className="underline">Retry version</button></div>}
    <input aria-label={`${label} dataset search`} placeholder="Search registered datasets by name…" disabled={disabled}
      value={query} onChange={(event) => setQuery(event.target.value)} className={field} />
    {!disabled && <span className="text-[10px] text-muted-foreground">Run parameters need saved versions. Publish an output to create a versioned dataset, or choose a source with version history.</span>}
    {!disabled && <div aria-label={`${label} dataset results`} className="max-h-32 overflow-y-auto rounded-md border border-border">
      {!visiblePage && !searchError && <p role="status" className="p-2 text-muted-foreground">Loading datasets…</p>}
      {visiblePage?.items.length === 0 && <p className="p-2 text-muted-foreground">No registered datasets match this search.</p>}
      {visiblePage?.items.map((table) => <button type="button" key={table.id} aria-label={`Choose dataset ${table.name}`} disabled={busy || table.missing || !table.registrationId}
        onClick={() => void choose(table)} className="block w-full px-2 py-1.5 text-left hover:bg-accent disabled:opacity-50">
        <strong className="block">{table.name}</strong>
        {table.description && <span className="block text-[10px] text-muted-foreground">{table.description}</span>}
        <span className="block truncate text-[10px] text-muted-foreground" title={table.uri}>{table.missing ? 'Dataset unavailable · ' : ''}{table.folder || table.uri} · {table.rowCount == null ? 'Rows unknown' : `${table.rowCount.toLocaleString()} rows`}</span>
      </button>)}
      {visiblePage?.hasMore && <button type="button" disabled={searchMore} onClick={() => void moreDatasets()} className="w-full p-2 font-semibold text-primary">{searchMore ? 'Loading…' : 'Load more datasets'}</button>}
    </div>}
    {busy && <span role="status" className="text-muted-foreground">Checking dataset versions…</span>}
    {visibleContextError && <div role="alert" className="text-destructive">Could not load dataset choices: {visibleContextError.message} Your binding is unchanged.{' '}
      {visibleContextError.selection ? 'Select the dataset again to retry.' : <button type="button" onClick={() => setContextRetry((old) => old + 1)} className="underline">Retry dataset</button>}
    </div>}
    {searchError && <div role="alert" className="text-destructive">Could not load datasets: {searchError}{' '}<button type="button" onClick={() => visiblePage ? void moreDatasets() : setSearchRetry((old) => old + 1)} className="underline">Retry search</button></div>}
  </div>
}
