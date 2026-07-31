import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type InboxItemDto } from '../api/client'
import { routeHash } from '../router'
import { useStore } from '../store/graph'
import { Icon } from '../ui/Icon'
import {
  inboxKindLabel,
  inboxOutcomeLabel,
  inboxOutcomeSummary,
  inboxRelativeTime,
} from '../views/inboxPresentation'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

const PREVIEW_LIMIT = 5

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function subject(item: InboxItemDto): string {
  if (item.datasetContext) return item.datasetContext.name || item.datasetContext.datasetId
  return item.canvasName ?? 'Canvas unavailable'
}

export function CanvasInboxPopover() {
  const setInboxQuery = useStore((state) => state.setInboxQuery)
  const setJobsQuery = useStore((state) => state.setJobsQuery)
  const [open, setOpen] = useState(false)
  const [count, setCount] = useState<number | null>(null)
  const [items, setItems] = useState<InboxItemDto[]>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [markError, setMarkError] = useState('')
  const [marking, setMarking] = useState('')
  const [markingAll, setMarkingAll] = useState(false)
  const countRequest = useRef(0)
  const listRequest = useRef(0)

  const refreshCount = useCallback(async () => {
    const request = ++countRequest.current
    try {
      const result = await api.inboxUnreadCount()
      if (request === countRequest.current) setCount(result.count)
    } catch {
      // Keep the last confirmed count. An unavailable refresh is not evidence of zero unread items.
    }
  }, [])

  const loadItems = useCallback(async () => {
    const request = ++listRequest.current
    setLoading(true)
    setLoadError('')
    try {
      const result = await api.inboxList({ limit: PREVIEW_LIMIT, filter: 'unread' })
      if (request === listRequest.current) {
        setItems(result.items.filter((item) => !item.readAt).slice(0, PREVIEW_LIMIT))
      }
    } catch (error) {
      if (request === listRequest.current) setLoadError(errorMessage(error))
    } finally {
      if (request === listRequest.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshCount()
    return () => {
      countRequest.current += 1
      listRequest.current += 1
    }
  }, [refreshCount])

  useEffect(() => {
    if (!open) return
    void loadItems()
    void refreshCount()
  }, [loadItems, open, refreshCount])

  const markRead = async (item: InboxItemDto) => {
    if (item.readAt || marking || markingAll) return
    setMarking(item.id)
    setMarkError('')
    try {
      const updated = await api.inboxMarkRead(item.id)
      if (!updated.readAt) throw new Error('the server did not confirm the read state')
      listRequest.current += 1
      countRequest.current += 1
      setItems((current) => current.filter((row) => row.id !== item.id))
      setCount((current) => current == null ? current : Math.max(0, current - 1))
      void refreshCount()
    } catch (error) {
      setMarkError(`Couldn’t mark read: ${errorMessage(error)}`)
    } finally {
      setMarking('')
    }
  }

  const markAllRead = async () => {
    if (marking || markingAll) return
    setMarkingAll(true)
    setMarkError('')
    try {
      await api.inboxMarkAllRead()
      listRequest.current += 1
      countRequest.current += 1
      setItems([])
      setCount(0)
    } catch (error) {
      setMarkError(`Couldn’t mark all as read: ${errorMessage(error)}`)
      void refreshCount()
    } finally {
      setMarkingAll(false)
    }
  }

  const markReadForNavigation = (item: InboxItemDto) => {
    if (!item.readAt) void api.inboxMarkRead(item.id).catch(() => {})
  }

  const openJob = (item: InboxItemDto) => {
    if (!item.jobAvailable || markingAll) return
    markReadForNavigation(item)
    setOpen(false)
    setJobsQuery(new URLSearchParams({ run: item.taskId }).toString())
  }

  const accessibleLabel = count == null
    ? 'Inbox'
    : count === 0
      ? 'Inbox, no unread outcomes'
      : `Inbox, ${count} unread outcomes`

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="canvas-inbox"
          aria-label={accessibleLabel}
          title="Inbox"
          className="relative grid h-7 w-7 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <Icon name="bell" size={16} />
          {count != null && count > 0 && (
            <span
              data-testid="canvas-inbox-unread-badge"
              aria-hidden
              className="absolute -right-1 -top-1 grid h-4 min-w-4 place-items-center rounded-full bg-foreground px-1 text-[9px] font-bold leading-none text-background"
            >
              {count > 99 ? '99+' : count}
            </span>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent
        role="dialog"
        aria-label="Inbox preview"
        align="start"
        sideOffset={8}
        className="w-[min(390px,calc(100vw-24px))] overflow-hidden p-0"
      >
        <div className="flex items-center gap-2 border-b border-border px-3 py-2.5">
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-semibold text-foreground">Inbox</div>
            <div className="text-[10.5px] text-muted-foreground">Recent unread outcomes</div>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void markAllRead()}
            disabled={markingAll || Boolean(marking) || count === 0 || (count == null && items.length === 0)}
            className="h-7 px-2 text-[11px]"
          >
            <Icon name="check" size={12} />
            {markingAll ? 'Marking…' : 'Mark all read'}
          </Button>
        </div>

        <div className="max-h-[360px] overflow-y-auto p-2">
          {markError && (
            <div role="alert" className="mb-2 rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-[11px] text-destructive">
              {markError}
            </div>
          )}
          {loading && items.length === 0 && (
            <div className="px-2 py-5 text-center text-[11.5px] text-muted-foreground">Loading Inbox…</div>
          )}
          {!loading && loadError && (
            <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-[11px] text-destructive">
              Couldn’t load Inbox: {loadError}{' '}
              <button type="button" className="font-semibold underline" onClick={() => void loadItems()}>Retry</button>
            </div>
          )}
          {!loading && !loadError && items.length === 0 && (
            <div className="px-2 py-5 text-center text-[11.5px] text-muted-foreground">You’re all caught up.</div>
          )}
          {items.length > 0 && (
            <ul className="flex flex-col gap-1.5" aria-label="Recent unread Inbox items">
              {items.map((item) => (
                <li key={item.id} className="rounded-md border border-border bg-card px-2.5 py-2">
                  <div className="flex items-center gap-1.5">
                    <Badge variant={item.outcome === 'completed' ? 'secondary' : 'destructive'} className="text-[9.5px]">
                      {inboxOutcomeLabel(item)}
                    </Badge>
                    <span className="truncate text-[10px] text-muted-foreground">{inboxKindLabel(item.taskKind)}</span>
                    <span className="ml-auto shrink-0 text-[10px] text-muted-foreground">{inboxRelativeTime(item.terminalAt)}</span>
                  </div>
                  <div className="mt-1 truncate text-[12px] font-medium text-foreground">{subject(item)}</div>
                  <div className="mt-0.5 truncate text-[10.5px] text-muted-foreground">{inboxOutcomeSummary(item)}</div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={Boolean(marking) || markingAll}
                      onClick={() => void markRead(item)}
                      className="h-7 px-2 text-[10.5px]"
                    >
                      Mark read
                    </Button>
                    {item.datasetContext && (
                      <Button variant="outline" size="sm" asChild className="h-7 px-2 text-[10.5px]">
                        <a
                          href={item.datasetContext.deepLink ?? routeHash('workspace', undefined, `dataset:${item.datasetContext.datasetId}`)}
                          onClick={() => {
                            markReadForNavigation(item)
                            setOpen(false)
                          }}
                        >
                          Open dataset
                        </a>
                      </Button>
                    )}
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={!item.jobAvailable || markingAll}
                      title={item.jobAvailable ? undefined : 'Job is unavailable with current authorization'}
                      onClick={() => openJob(item)}
                      className="h-7 px-2 text-[10.5px]"
                    >
                      Open job
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="border-t border-border p-2">
          <Button
            variant="ghost"
            size="sm"
            className="h-8 w-full justify-between text-[11.5px]"
            onClick={() => {
              setOpen(false)
              setInboxQuery('')
            }}
          >
            View all Inbox
            <Icon name="chevronRight" size={12} />
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  )
}
