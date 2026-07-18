import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type InboxItemDto, type InboxTaskKind } from '../api/client'
import { useStore } from '../store/graph'
import { Icon } from '../ui/Icon'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

const PAGE_SIZE = 50
const FILTERS = ['all', 'unread'] as const

function outcomeLabel(item: InboxItemDto): string {
  if (item.outcome === 'completed') return 'Completed'
  if (item.outcome === 'cancelled') return 'Cancelled'
  if (item.diagnosticCode) {
    return item.diagnosticCode.replace(/_/g, ' ')
  }
  return 'Failed'
}

const TASK_KIND_LABELS: Record<InboxTaskKind, string> = {
  managed_local_write: 'Managed local write',
  external_wait: 'External wait',
  linear_checkpoint_write: 'Checkpointed write',
  bounded_fanout_write: 'Bounded fan-out write',
}

export function kindLabel(kind: InboxItemDto['taskKind'] | string): string {
  return TASK_KIND_LABELS[kind as InboxTaskKind] ?? `Unknown task type: ${kind}`
}

function relTime(iso: string): string {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  const s = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  if (s < 604800) return `${Math.round(s / 86400)}d ago`
  return `${Math.round(s / 604800)}w ago`
}

export function mergeMonotonic(current: InboxItemDto[], incoming: InboxItemDto[]): InboxItemDto[] {
  const byId = new Map(current.map((item) => [item.id, item]))
  for (const item of incoming) {
    const prior = byId.get(item.id)
    if (!prior) {
      byId.set(item.id, item)
      continue
    }
    // Stale responses must never regress a read item back to unread.
    byId.set(item.id, {
      ...item,
      readAt: prior.readAt ?? item.readAt ?? null,
    })
  }
  const order = current.map((item) => item.id)
  const seen = new Set(order)
  for (const id of incoming.map((item) => item.id)) {
    if (!seen.has(id)) order.push(id)
  }
  return order.map((id) => byId.get(id)!).filter(Boolean)
}

export function InboxView({ onUnreadChange }: { onUnreadChange?: () => void }) {
  const inboxQuery = useStore((state) => state.inboxQuery)
  const setInboxQuery = useStore((state) => state.setInboxQuery)
  const setJobsQuery = useStore((state) => state.setJobsQuery)
  const params = useMemo(() => new URLSearchParams(inboxQuery), [inboxQuery])
  const filter = FILTERS.includes(params.get('filter') as typeof FILTERS[number])
    ? params.get('filter') as typeof FILTERS[number]
    : 'all'
  const [items, setItems] = useState<InboxItemDto[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const [loadMoreError, setLoadMoreError] = useState('')
  const [markError, setMarkError] = useState('')
  const [marking, setMarking] = useState('')
  const request = useRef(0)
  const knownRead = useRef(new Map<string, string>())

  const hydrate = (rows: InboxItemDto[]) => rows.map((row) => {
    const remembered = knownRead.current.get(row.id)
    const readAt = remembered ?? row.readAt ?? null
    if (typeof readAt === 'string' && readAt) knownRead.current.set(row.id, readAt)
    return { ...row, readAt }
  })

  const load = useCallback(async (nextCursor?: string) => {
    const sequence = ++request.current
    if (nextCursor) { setLoadingMore(true); setLoadMoreError('') }
    else { setLoading(true); setError('') }
    try {
      const page = await api.inboxList({ limit: PAGE_SIZE, cursor: nextCursor, filter })
      if (sequence !== request.current) return
      if (!nextCursor) setError('')
      const incoming = hydrate(page.items).filter((row) => filter !== 'unread' || !row.readAt)
      setItems((current) => nextCursor ? mergeMonotonic(current, incoming) : incoming)
      setCursor(page.nextCursor ?? null)
      setHasMore(page.hasMore)
    } catch (caught) {
      if (sequence !== request.current) return
      const message = caught instanceof Error ? caught.message : String(caught)
      if (nextCursor) setLoadMoreError(message)
      else setError(message)
    } finally {
      if (sequence === request.current) { setLoading(false); setLoadingMore(false) }
    }
  }, [filter])

  useEffect(() => {
    void load()
    return () => { request.current += 1 }
  }, [load])

  const setFilter = (value: string) => {
    const next = new URLSearchParams(params)
    if (value && value !== 'all') next.set('filter', value)
    else next.delete('filter')
    setInboxQuery(next.toString())
  }

  const markRead = async (item: InboxItemDto) => {
    if (item.readAt) return
    setMarking(item.id)
    setMarkError('')
    try {
      const updated = await api.inboxMarkRead(item.id)
      if (updated.readAt) knownRead.current.set(item.id, updated.readAt)
      setItems((current) => {
        const next = current.map((row) => (
          row.id === item.id
            ? { ...row, ...updated, readAt: row.readAt ?? updated.readAt ?? null }
            : row
        ))
        return filter === 'unread' ? next.filter((row) => !row.readAt) : next
      })
      onUnreadChange?.()
    } catch (caught) {
      setMarkError(caught instanceof Error ? caught.message : String(caught))
      await load()
      onUnreadChange?.()
    } finally {
      setMarking('')
    }
  }

  const openJob = (item: InboxItemDto) => {
    if (!item.jobAvailable) return
    void markRead(item)
    setJobsQuery(new URLSearchParams({ run: item.taskId }).toString())
  }

  return (
    <div className="flex h-full min-w-0 flex-col" data-testid="inbox-view">
      <header className="flex min-h-[68px] flex-wrap items-center gap-3 border-b border-border px-4 py-3 sm:px-7">
        <div>
          <h1 className="text-[20px] font-bold text-foreground">Inbox</h1>
          <p className="text-[11.5px] text-muted-foreground">Personal durable Task outcomes</p>
        </div>
        <span className="flex-1" />
        <label className="grid gap-1 text-[10.5px] text-muted-foreground">Filter
          <select
            aria-label="Filter inbox items"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            className="h-8 rounded-md border border-border bg-background px-2 text-[12px] text-foreground"
          >
            <option value="all">All</option>
            <option value="unread">Unread</option>
          </select>
        </label>
        <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading || loadingMore}>
          <Icon name="refresh" size={13} /> Refresh
        </Button>
      </header>

      <div className="min-h-0 flex-1 overflow-auto px-3 py-3 sm:px-7">
        {markError && (
          <div role="alert" className="mb-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-[12px] text-destructive">
            Couldn’t mark read: {markError}
          </div>
        )}
        {loading && <div className="p-5 text-[12.5px] text-muted-foreground">Loading Inbox…</div>}
        {!loading && error && (
          <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-4 text-[12.5px] text-destructive">
            Couldn’t load Inbox: {error}{' '}
            <button type="button" className="ml-2 font-semibold underline" onClick={() => void load()}>Retry</button>
          </div>
        )}
        {!loading && !error && items.length === 0 && (
          <div className="rounded-lg border border-dashed border-border p-8 text-center text-[12.5px] text-muted-foreground">
            {filter === 'unread' ? 'No unread outcomes.' : 'No durable Task outcomes yet.'}
          </div>
        )}
        {items.length > 0 && (
          <ul className="flex flex-col gap-2" aria-label="Inbox items">
            {items.map((item) => (
              <li
                key={item.id}
                data-testid={`inbox-item-${item.id}`}
                className={cn(
                  'rounded-lg border border-border bg-card px-4 py-3',
                  !item.readAt && 'border-l-[3px] border-l-foreground',
                )}
              >
                <div className="flex flex-wrap items-start gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant={item.outcome === 'completed' ? 'secondary' : 'destructive'}>
                        {outcomeLabel(item)}
                      </Badge>
                      {!item.readAt && <span className="text-[10.5px] font-semibold uppercase tracking-wide text-foreground">Unread</span>}
                      <span className="text-[11px] text-muted-foreground">{kindLabel(item.taskKind)}</span>
                    </div>
                    <div className="mt-1 text-[13px] font-medium text-foreground">
                      {item.canvasName ?? 'Canvas unavailable'}
                    </div>
                    <div className="mt-0.5 text-[11.5px] text-muted-foreground">
                      {relTime(item.terminalAt)}
                      {item.canvasName == null && ' · authorization revoked or canvas missing'}
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {!item.readAt && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={marking === item.id}
                        onClick={() => void markRead(item)}
                      >
                        Mark read
                      </Button>
                    )}
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={!item.jobAvailable}
                      title={item.jobAvailable ? undefined : 'Job is unavailable with current authorization'}
                      onClick={() => openJob(item)}
                    >
                      Open job
                    </Button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
        {loadMoreError && (
          <div role="alert" className="mt-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-[12px] text-destructive">
            Couldn’t load more Inbox items: {loadMoreError}{' '}
            <button type="button" className="ml-2 font-semibold underline" onClick={() => cursor && void load(cursor)}>
              Retry load more
            </button>
          </div>
        )}
        {hasMore && !loadMoreError && (
          <Button
            variant="outline"
            className="mt-3 w-full"
            disabled={loadingMore || !cursor}
            onClick={() => cursor && void load(cursor)}
          >
            {loadingMore ? 'Loading…' : 'Load more'}
          </Button>
        )}
      </div>
    </div>
  )
}
