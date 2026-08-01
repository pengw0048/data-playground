import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { InboxView, mergeMonotonic } from './InboxView'
import { useStore } from '../store/graph'

const mocks = vi.hoisted(() => ({
  inboxList: vi.fn(),
  inboxMarkAllRead: vi.fn(),
  inboxMarkRead: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    inboxList: mocks.inboxList,
    inboxMarkAllRead: mocks.inboxMarkAllRead,
    inboxMarkRead: mocks.inboxMarkRead,
  },
}))

function item(overrides: Record<string, unknown> = {}) {
  return {
    id: 'item-1',
    taskId: 'task-1',
    canvasId: 'canvas-1',
    canvasName: 'Climate analysis',
    taskKind: 'managed_local_write',
    outcome: 'completed',
    diagnosticCode: null,
    terminalAt: '2026-07-17T12:00:00Z',
    readAt: null,
    jobAvailable: true,
    ...overrides,
  }
}

describe('InboxView', () => {
  beforeEach(() => {
    mocks.inboxList.mockReset()
    mocks.inboxMarkAllRead.mockReset()
    mocks.inboxMarkRead.mockReset()
    mocks.inboxList.mockResolvedValue({ items: [item()], hasMore: false, nextCursor: null })
    mocks.inboxMarkAllRead.mockResolvedValue({
      markedCount: 1, readAt: '2026-07-17T12:05:00Z',
    })
    useStore.setState({ view: 'inbox', inboxQuery: '', jobsQuery: '', toasts: [] } as never)
  })

  it('describes Inbox as results from work the current user started', () => {
    render(<InboxView />)
    expect(screen.getByText(/Only results from background work you started/)).toBeVisible()
    expect(screen.getByText(/other people’s activity is not shown/)).toBeVisible()
    expect(screen.queryByText(/durable/i)).toBeNull()
  })

  it('keeps the empty-state promise limited to background tasks', async () => {
    mocks.inboxList.mockResolvedValue({ items: [], hasMore: false, nextCursor: null })
    render(<InboxView />)
    expect(await screen.findByText('No background task results yet.')).toBeInTheDocument()
    expect(screen.queryByText(/finished runs/i)).toBeNull()
  })

  it('loads items, marks read, and opens an authorized job', async () => {
    const user = userEvent.setup()
    mocks.inboxMarkRead.mockResolvedValue(item({ readAt: '2026-07-17T12:05:00Z' }))
    render(<InboxView />)
    await screen.findByText('Climate analysis')
    await user.click(screen.getByRole('button', { name: 'Open job' }))
    await waitFor(() => expect(mocks.inboxMarkRead).toHaveBeenCalledWith('item-1'))
    expect(useStore.getState().view).toBe('jobs')
    expect(useStore.getState().jobsQuery).toContain('run=task-1')
  })

  it('renders only the bounded committed Write facts and omits an unknown summary', async () => {
    mocks.inboxList.mockResolvedValue({ items: [
      item({ completedWrite: { outputName: 'annual-results', rowCount: 42 } }),
      item({ id: 'unknown-write', canvasName: 'Archive', completedWrite: null }),
    ], hasMore: false, nextCursor: null })
    render(<InboxView />)
    expect(await screen.findByText('“annual-results” written · 42 rows')).toBeInTheDocument()
    expect(screen.queryByText(/undefined written/i)).toBeNull()
  })

  it('disables Open job when authorization is unavailable and redacts failures', async () => {
    mocks.inboxList.mockResolvedValue({
      items: [item({
        outcome: 'failed',
        diagnosticCode: 'external_wait_deadline',
        canvasName: null,
        jobAvailable: false,
        taskKind: 'external_wait',
      })],
      hasMore: false,
      nextCursor: null,
    })
    render(<InboxView />)
    await screen.findByText('external wait deadline')
    expect(screen.queryByText(/secret|traceback|boom/i)).toBeNull()
    expect(screen.getByRole('button', { name: 'Open job' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Open job' })).toHaveAttribute('title', 'The linked Job is no longer available')
    expect(screen.getByText('Original Canvas unavailable')).toBeInTheDocument()
    expect(screen.getByText(/Deleted or no longer shared with you/)).toBeVisible()
  })

  it('never describes a failed item without a diagnostic as successful', async () => {
    mocks.inboxList.mockResolvedValue({
      items: [item({ outcome: 'failed', diagnosticCode: null })],
      hasMore: false,
      nextCursor: null,
    })
    render(<InboxView />)
    expect(await screen.findByText('Work failed')).toBeInTheDocument()
    expect(screen.queryByText('Finished successfully')).toBeNull()
  })

  it('labels every declared task kind for each terminal outcome and diagnoses unknown runtime kinds', async () => {
    const kinds = [
      ['managed_local_write', 'Managed local write'],
      ['external_wait', 'External wait'],
      ['linear_checkpoint_write', 'Checkpointed write'],
      ['bounded_fanout_write', 'Bounded fan-out write'],
      ['merge_columns_write', 'Merge columns write'],
    ] as const
    const outcomes = ['completed', 'failed', 'cancelled'] as const
    const rows = kinds.flatMap(([taskKind, label]) => outcomes.map((outcome) => item({
      id: `${taskKind}-${outcome}`,
      taskKind,
      outcome,
      diagnosticCode: outcome === 'failed' ? `${taskKind}_failed` : null,
    })))
    rows.push(item({
      id: 'future-kind',
      taskKind: 'future_task_kind' as never,
      outcome: 'completed',
    }))
    mocks.inboxList.mockResolvedValue({ items: rows, hasMore: false, nextCursor: null })

    render(<InboxView />)
    await screen.findByTestId('inbox-item-future-kind')

    for (const [taskKind, label] of kinds) {
      for (const outcome of outcomes) {
        const row = within(screen.getByTestId(`inbox-item-${taskKind}-${outcome}`))
        expect(row.getByText(label)).toBeInTheDocument()
        expect(row.getByText(outcome === 'completed' ? 'Completed' : outcome === 'failed'
          ? 'Failed' : 'Cancelled')).toBeInTheDocument()
      }
    }
    expect(within(screen.getByTestId('inbox-item-future-kind')).getByText('Unknown task type: future_task_kind')).toBeInTheDocument()
  })

  it('keeps a locally read item read when a stale list response arrives', async () => {
    let finish!: (page: unknown) => void
    mocks.inboxList
      .mockResolvedValueOnce({ items: [item()], hasMore: false, nextCursor: null })
      .mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    const user = userEvent.setup()
    mocks.inboxMarkRead.mockResolvedValue(item({ readAt: '2026-07-17T12:05:00Z' }))
    render(<InboxView />)
    await screen.findByText('Unread', { selector: 'span' })
    await user.click(screen.getByRole('button', { name: 'Mark read' }))
    await waitFor(() => expect(screen.queryByText('Unread', { selector: 'span' })).toBeNull())
    await user.click(screen.getByRole('button', { name: /Refresh/i }))
    finish({ items: [item({ readAt: null })], hasMore: false, nextCursor: null })
    await waitFor(() => expect(mocks.inboxList).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('Unread', { selector: 'span' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Mark read' })).toBeNull()
  })

  it('keeps the item unread until mark-read succeeds', async () => {
    let resolveMark!: (value: ReturnType<typeof item>) => void
    const onUnreadChange = vi.fn()
    mocks.inboxMarkRead.mockReturnValueOnce(new Promise((resolve) => { resolveMark = resolve }))
    render(<InboxView onUnreadChange={onUnreadChange} />)
    await screen.findByText('Climate analysis')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Mark read' }))
    expect(screen.getByText('Unread', { selector: 'span' })).toBeInTheDocument()
    resolveMark(item({ readAt: '2026-07-17T12:05:00Z' }))
    await waitFor(() => expect(screen.queryByText('Unread', { selector: 'span' })).toBeNull())
    expect(onUnreadChange).toHaveBeenCalledTimes(1)
  })

  it('refreshes server truth and the badge callback when mark-read fails', async () => {
    const onUnreadChange = vi.fn()
    mocks.inboxList
      .mockResolvedValueOnce({ items: [item()], hasMore: false, nextCursor: null })
      .mockResolvedValueOnce({ items: [item({ readAt: '2026-07-17T12:06:00Z' })], hasMore: false, nextCursor: null })
    mocks.inboxMarkRead.mockRejectedValueOnce(new Error('network lost'))
    render(<InboxView onUnreadChange={onUnreadChange} />)
    await screen.findByText('Climate analysis')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Mark read' }))
    await waitFor(() => expect(mocks.inboxList).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Mark read' })).toBeNull())
    expect(onUnreadChange).toHaveBeenCalledTimes(1)
  })

  it('marks every visible item read with one request and preserves All history', async () => {
    const onUnreadChange = vi.fn()
    const rows = [
      item(),
      item({ id: 'item-2', taskId: 'task-2', canvasName: 'Archive analysis' }),
    ]
    mocks.inboxList
      .mockResolvedValueOnce({ items: rows, hasMore: true, nextCursor: 'older-page' })
      .mockResolvedValueOnce({ items: rows, hasMore: true, nextCursor: 'older-page' })
    mocks.inboxMarkAllRead.mockResolvedValueOnce({
      markedCount: 8, readAt: '2026-07-17T12:10:00Z',
    })
    render(<InboxView onUnreadChange={onUnreadChange} />)
    await screen.findByText('Archive analysis')

    await userEvent.setup().click(screen.getByRole('button', { name: 'Mark all as read' }))

    await waitFor(() => expect(mocks.inboxList).toHaveBeenCalledTimes(2))
    expect(mocks.inboxMarkAllRead).toHaveBeenCalledTimes(1)
    expect(mocks.inboxMarkRead).not.toHaveBeenCalled()
    expect(screen.getByText('Climate analysis')).toBeInTheDocument()
    expect(screen.getByText('Archive analysis')).toBeInTheDocument()
    expect(screen.queryByText('Unread', { selector: 'span' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Mark read' })).toBeNull()
    expect(onUnreadChange).toHaveBeenCalledTimes(1)
  })

  it('empties the Unread filter after a confirmed mark-all response', async () => {
    const onUnreadChange = vi.fn()
    useStore.setState({ inboxQuery: 'filter=unread' } as never)
    mocks.inboxList
      .mockResolvedValueOnce({ items: [item()], hasMore: false, nextCursor: null })
      .mockResolvedValueOnce({ items: [], hasMore: false, nextCursor: null })
    render(<InboxView onUnreadChange={onUnreadChange} />)
    await screen.findByText('Climate analysis')

    await userEvent.setup().click(screen.getByRole('button', { name: 'Mark all as read' }))

    expect(await screen.findByText('You’re all caught up.')).toBeInTheDocument()
    expect(screen.queryByText('Climate analysis')).toBeNull()
    expect(mocks.inboxMarkAllRead).toHaveBeenCalledTimes(1)
    expect(onUnreadChange).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Mark all as read' })).toBeDisabled()
  })

  it('disables mark-all when the complete loaded result is already read', async () => {
    mocks.inboxList.mockResolvedValue({
      items: [item({ readAt: '2026-07-17T12:05:00Z' })], hasMore: false, nextCursor: null,
    })
    render(<InboxView />)

    await screen.findByText('Climate analysis')
    expect(screen.getByRole('button', { name: 'Mark all as read' })).toBeDisabled()
    expect(mocks.inboxMarkAllRead).not.toHaveBeenCalled()
  })

  it('keeps mark-all available when older pages may still contain unread items', async () => {
    mocks.inboxList.mockResolvedValue({
      items: [item({ readAt: '2026-07-17T12:05:00Z' })], hasMore: true, nextCursor: 'older',
    })
    render(<InboxView />)

    await screen.findByText('Climate analysis')
    expect(screen.getByRole('button', { name: 'Mark all as read' })).toBeEnabled()
  })

  it('keeps rows unread and refreshes server truth when mark-all fails', async () => {
    const onUnreadChange = vi.fn()
    mocks.inboxList
      .mockResolvedValueOnce({
        items: [item()], hasMore: false, nextCursor: null,
      })
      .mockResolvedValueOnce({
        items: [item()], hasMore: false, nextCursor: null,
      })
    mocks.inboxMarkAllRead.mockRejectedValueOnce(new Error('network lost'))
    render(<InboxView onUnreadChange={onUnreadChange} />)
    await screen.findByText('Climate analysis')

    await userEvent.setup().click(screen.getByRole('button', { name: 'Mark all as read' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Couldn’t mark all as read: network lost',
    )
    expect(screen.getByText('Unread', { selector: 'span' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Mark read' })).toBeInTheDocument()
    expect(mocks.inboxMarkAllRead).toHaveBeenCalledTimes(1)
    expect(mocks.inboxMarkRead).not.toHaveBeenCalled()
    expect(onUnreadChange).toHaveBeenCalledTimes(1)
  })

  it('renders a canvas-less dataset item with an exact dataset-viewer deep-link', async () => {
    useStore.setState({ inboxQuery: 'filter=unread' } as never)
    mocks.inboxList.mockResolvedValue({ items: [item({
      taskKind: 'keyed_upsert_write', canvasId: null, canvasName: null, readAt: null,
      datasetContext: { taskKind: 'keyed_upsert_write', datasetId: 'ds-logical-7', revisionId: 'rev-7', name: 'Sensor upserts' },
    })], hasMore: false, nextCursor: null })
    render(<InboxView />)
    await screen.findByText('Sensor upserts')
    expect(screen.getByText('Keyed upsert')).toBeInTheDocument()
    expect(screen.getByText('Revision upserted')).toBeInTheDocument()
    expect(screen.queryByText('Dataset ds-logical-7')).toBeNull()
    expect(screen.queryByText(/authorization revoked/i)).toBeNull()
    const link = screen.getByRole('link', { name: 'Open dataset' })
    expect(link).toHaveAttribute(
      'href',
      '#/workspace/dataset%3Ads-logical-7?scope=datasets&revision=rev-7&revisionDataset=ds-logical-7&returnView=inbox&returnQuery=filter%3Dunread',
    )
  })

})

describe('mergeMonotonic (load-more ordering)', () => {
  const mk = (id: string) => item({ id }) as never
  const ids = (rows: unknown[]) => rows.map((row) => (row as { id: string }).id)
  it('appends an older page after the newer one, preserving terminal_at DESC order', () => {
    expect(ids(mergeMonotonic([mk('b9'), mk('b8')], [mk('b7'), mk('b6')]))).toEqual(['b9', 'b8', 'b7', 'b6'])
  })
  it('dedupes an overlapping boundary item without reordering', () => {
    expect(ids(mergeMonotonic([mk('b9'), mk('b8')], [mk('b8'), mk('b7')]))).toEqual(['b9', 'b8', 'b7'])
  })
})
