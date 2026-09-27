import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { groupCountStarterReason, numericFilterStarterReason, numericStarterColumn, type OwnDataStarter } from '../starters'
import { useStore, type CanvasCreationResult } from '../store/graph'
import type { ExampleCreationIntent } from '../store/exampleReplacement'
import type { CatalogTable } from '../types/api'
import type { CanvasDoc } from '../types/graph'
import { Button } from '../components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '../components/ui/dialog'

const PAGE_SIZE = 12
const message = (error: unknown) => error instanceof Error ? error.message : String(error)
const unavailable = (table: CatalogTable) => table.missing ? 'Source file is unavailable'
  : !table.registrationId || table.uri.startsWith('workspace-provider://') ? 'Register this dataset in Workspace first' : null

type Props = {
  open: boolean
  kind?: OwnDataStarter['kind']
  onOpenChange: (open: boolean) => void
  intent?: ExampleCreationIntent
  onCreate?: (starter: OwnDataStarter) => Promise<CanvasCreationResult>
}
type Page = { items: CatalogTable[]; hasMore: boolean; offset: number; key: string }

export function OwnDataStarterModal({ open, ...props }: Props) {
  return open ? <StarterForm {...props} /> : null
}

function StarterForm({ onOpenChange, intent = 'create-separate', onCreate, kind = 'numeric-filter' }: Omit<Props, 'open'>) {
  const counting = kind === 'group-count'
  const [query, setQuery] = useState('')
  const [retry, setRetry] = useState(0)
  const [page, setPage] = useState<Page | null>(null)
  const [searchError, setSearchError] = useState('')
  const [loadingMore, setLoadingMore] = useState(false)
  const [choice, setChoice] = useState<CatalogTable | null>(null)
  const [table, setTable] = useState<CatalogTable | null>(null)
  const [column, setColumn] = useState('')
  const [threshold, setThreshold] = useState('')
  const [checking, setChecking] = useState(false)
  const [schemaError, setSchemaError] = useState('')
  const [creating, setCreating] = useState(false)
  const [creationError, setCreationError] = useState('')
  const generation = useRef(0)
  const submitting = useRef(false)
  const live = useRef(true)
  const searchKey = JSON.stringify([query.trim(), retry])
  const currentSearch = useRef(searchKey)
  currentSearch.current = searchKey
  const visiblePage = page?.key === searchKey ? page : null

  useEffect(() => { live.current = true; return () => { live.current = false; generation.current += 1 } }, [])

  useEffect(() => {
    let current = true
    setSearchError(''); setLoadingMore(false)
    const timer = window.setTimeout(() => {
      void api.tablesPage({ q: query.trim() || undefined, limit: PAGE_SIZE, offset: 0, sort: 'name', order: 'asc' })
        .then((next) => { if (current) setPage({ ...next, offset: 0, key: searchKey }) })
        .catch((error) => { if (current) setSearchError(message(error)) })
    }, query.trim() ? 150 : 0)
    return () => { current = false; window.clearTimeout(timer) }
  }, [searchKey])

  const loadMore = async () => {
    if (!visiblePage || loadingMore || creating) return
    setLoadingMore(true); setSearchError('')
    try {
      const offset = visiblePage.offset + PAGE_SIZE
      const next = await api.tablesPage({ q: query.trim() || undefined, limit: PAGE_SIZE, offset, sort: 'name', order: 'asc' })
      if (!live.current || currentSearch.current !== searchKey) return
      setPage({ ...next, offset, key: searchKey,
        items: [...visiblePage.items, ...next.items.filter((item) => !visiblePage.items.some((old) => old.id === item.id))] })
    } catch (error) {
      if (live.current && currentSearch.current === searchKey) setSearchError(message(error))
    } finally {
      if (live.current && currentSearch.current === searchKey) setLoadingMore(false)
    }
  }

  const choose = async (next: CatalogTable) => {
    if (submitting.current || unavailable(next)) return
    const ticket = ++generation.current
    setChoice(next); setTable(null); setColumn(''); setSchemaError(''); setCreationError(''); setChecking(true)
    try {
      const current = await api.tableByRegistration(next.registrationId!)
      if (!live.current || ticket !== generation.current) return
      if (current.registrationId !== next.registrationId) throw new Error('The selected dataset has changed. Choose it again.')
      const reason = unavailable(current)
      if (reason) throw new Error(reason)
      const source: CanvasDoc = {
        id: 'own-data-starter-schema', name: counting ? 'Count rows by group' : 'Filter your data', version: 1, edges: [],
        nodes: [{ id: 'src', type: 'source', position: { x: 0, y: 0 }, data: {
          title: current.name, status: 'draft', config: {
            uri: current.uri, tableId: current.id, registrationId: current.registrationId!,
          },
        } }],
      }
      const schemas = await api.schema(source, 'src')
      if (!live.current || ticket !== generation.current) return
      const columns = schemas.src?.out
      if (!Array.isArray(columns)) throw new Error('The dataset columns could not be read. Retry or choose another dataset.')
      setTable({ ...current, columns })
    } catch (error) {
      if (live.current && ticket === generation.current) setSchemaError(message(error))
    } finally {
      if (live.current && ticket === generation.current) setChecking(false)
    }
  }

  const selectableColumns = table?.columns.filter((item) => counting || numericStarterColumn(item)) ?? []
  const invalidReason = !table ? 'Choose a dataset' : counting
    ? groupCountStarterReason(table, column) : numericFilterStarterReason(table, column, threshold)
  const create = async () => {
    if (!table || invalidReason || submitting.current || checking) return
    submitting.current = true
    setCreating(true); setCreationError('')
    try {
      const starter: OwnDataStarter = counting
        ? { kind: 'group-count', table, column } : { kind: 'numeric-filter', table, column, threshold }
      const result = await (onCreate ? onCreate(starter) : useStore.getState().newFromStarter(starter, intent))
      if (!live.current) return
      if (result.ok) onOpenChange(false)
      else setCreationError('Could not create the Canvas. Your selections are kept; try again.')
    } catch (error) {
      if (live.current) setCreationError(message(error))
    } finally {
      submitting.current = false
      if (live.current) setCreating(false)
    }
  }

  return <Dialog open onOpenChange={(open) => { if (!creating) onOpenChange(open) }}>
    <DialogContent closeDisabled={creating} className="max-h-[90vh] max-w-xl gap-3 overflow-y-auto">
      <DialogTitle>{counting ? 'Count rows by group' : 'Filter your data'}</DialogTitle>
      <DialogDescription>{counting
        ? 'Choose a dataset and a column to count each category. Open the Source → Aggregate steps, then run to count all rows.'
        : 'Choose a dataset and keep rows above a number. Open the Source → Filter steps to review and run when ready.'}</DialogDescription>
      <form className="grid gap-3" onSubmit={(event) => { event.preventDefault(); void create() }}>
        <label className="grid gap-1 text-[12px] font-medium">Search datasets
          <input aria-label="Search starter datasets" className="dp-input" value={query} disabled={creating}
            placeholder="Search registered datasets…" onChange={(event) => setQuery(event.target.value)} />
        </label>
        <div className="max-h-36 overflow-y-auto rounded-md border border-border" aria-label="Starter datasets">
          {!visiblePage && !searchError && <p role="status" className="p-2 text-[12px] text-muted-foreground">Loading datasets…</p>}
          {visiblePage?.items.length === 0 && <p className="p-2 text-[12px] text-muted-foreground">No datasets found. Try another search, or add data in Workspace.</p>}
          {visiblePage?.items.map((item) => <button key={item.id} type="button" disabled={creating || !!unavailable(item)}
            aria-label={`Choose starter dataset ${item.name}`} aria-pressed={choice?.registrationId === item.registrationId && !!item.registrationId}
            className="block w-full border-b border-border px-3 py-2 text-left last:border-0 hover:bg-accent disabled:opacity-50 aria-pressed:bg-accent"
            onClick={() => void choose(item)}>
            <span className="block text-[12px] font-medium">{item.name}</span>
            <span className="block text-[11px] text-muted-foreground">{unavailable(item) ?? [item.folder, item.rowCount == null ? 'Rows unknown' : `${item.rowCount.toLocaleString()} rows`].filter(Boolean).join(' · ')}</span>
          </button>)}
          {visiblePage?.hasMore && <button type="button" disabled={creating || loadingMore} onClick={() => void loadMore()}
            className="w-full p-2 text-[12px] font-medium text-primary">{loadingMore ? 'Loading…' : 'Load more datasets'}</button>}
        </div>
        {searchError && <div role="alert" className="text-[12px] text-destructive">Could not load datasets: {searchError}{' '}
          <button type="button" disabled={creating} className="font-semibold underline" onClick={() => visiblePage ? void loadMore() : setRetry((value) => value + 1)}>Retry dataset search</button>
        </div>}
        {choice && <div className="text-[12px]">Dataset: <strong>{table?.name ?? choice.name}</strong></div>}
        {checking && <p role="status" className="text-[12px] text-muted-foreground">Checking dataset columns…</p>}
        {schemaError && <div role="alert" className="text-[12px] text-destructive">{schemaError}{' '}
          <button type="button" disabled={creating} className="font-semibold underline" onClick={() => choice && void choose(choice)}>Retry dataset columns</button>
        </div>}
        {table && !selectableColumns.length && <p role="status" className="text-[12px] text-muted-foreground">{table.columns.length
          ? 'This dataset has no numeric columns. Choose another dataset.'
          : 'This dataset has no columns. Choose another dataset.'}</p>}
        <label className="grid gap-1 text-[12px] font-medium">{counting ? 'Group by column' : 'Numeric column'}
          <select aria-label={counting ? 'Starter grouping column' : 'Starter numeric column'} className="dp-input" value={column} disabled={creating || checking || !selectableColumns.length}
            onChange={(event) => { setColumn(event.target.value); setCreationError('') }}>
            <option value="">{counting ? 'Choose a grouping column' : 'Choose a numeric column'}</option>
            {selectableColumns.map((item) => <option key={item.name} value={item.name}>{item.name} ({item.type})</option>)}
          </select>
        </label>
        {!counting && <label className="grid gap-1 text-[12px] font-medium">Keep values greater than
          <input aria-label="Starter threshold" className="dp-input" type="text" inputMode="decimal" placeholder="For example, 100"
            value={threshold} disabled={creating || !table || !selectableColumns.length}
            onChange={(event) => { setThreshold(event.target.value); setCreationError('') }} />
        </label>}
        <p className="text-[11px] text-muted-foreground">{counting
          ? 'Missing values form their own group. Counts require all input rows; creating the Canvas does not run or publish data.'
          : 'Missing values are excluded. You can edit this condition in Filter.'}</p>
        {column && (counting || threshold) && invalidReason && <p role="alert" className="text-[12px] text-destructive">{invalidReason}</p>}
        {creationError && <p role="alert" className="text-[12px] text-destructive">{creationError}</p>}
        <div className="flex items-center justify-end gap-2 pt-1">
          <Button type="button" variant="outline" disabled={creating} onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" disabled={creating || checking || !!invalidReason}>{creating ? 'Creating…' : intent === 'replace-pristine' ? 'Use in this Canvas' : counting ? 'Create count Canvas' : 'Create filter Canvas'}</Button>
        </div>
      </form>
    </DialogContent>
  </Dialog>
}
