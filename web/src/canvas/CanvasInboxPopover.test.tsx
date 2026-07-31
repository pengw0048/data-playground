import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  inboxUnreadCount: vi.fn(),
  inboxList: vi.fn(),
  inboxMarkRead: vi.fn(),
  inboxMarkAllRead: vi.fn(),
  setInboxQuery: vi.fn(),
  setJobsQuery: vi.fn(),
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      inboxUnreadCount: mocks.inboxUnreadCount,
      inboxList: mocks.inboxList,
      inboxMarkRead: mocks.inboxMarkRead,
      inboxMarkAllRead: mocks.inboxMarkAllRead,
    },
  }
})

vi.mock('../store/graph', () => ({
  useStore: (selector: (state: {
    setInboxQuery: typeof mocks.setInboxQuery
    setJobsQuery: typeof mocks.setJobsQuery
  }) => unknown) => selector({
    setInboxQuery: mocks.setInboxQuery,
    setJobsQuery: mocks.setJobsQuery,
  }),
}))

import { CanvasInboxPopover } from './CanvasInboxPopover'
import type { InboxItemDto } from '../api/client'

function item(overrides: Partial<InboxItemDto> = {}): InboxItemDto {
  return {
    id: 'item-1',
    taskId: 'task-1',
    canvasId: 'canvas-1',
    canvasName: 'Climate analysis',
    taskKind: 'managed_local_write',
    outcome: 'completed',
    diagnosticCode: null,
    completedWrite: { outputName: 'annual-results', rowCount: 42 },
    terminalAt: '2026-07-17T12:00:00Z',
    readAt: null,
    jobAvailable: true,
    ...overrides,
  }
}

describe('CanvasInboxPopover', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.inboxUnreadCount.mockResolvedValue({ count: 2 })
    mocks.inboxList.mockResolvedValue({
      items: [
        item(),
        item({
          id: 'item-2',
          taskId: 'task-2',
          canvasName: 'Archive analysis',
          completedWrite: { outputName: 'archive-results', rowCount: 8 },
        }),
      ],
      nextCursor: null,
      hasMore: false,
    })
    mocks.inboxMarkRead.mockImplementation(async (id: string) =>
      item({ id, readAt: '2026-07-17T12:05:00Z' }))
    mocks.inboxMarkAllRead.mockResolvedValue({
      markedCount: 2,
      readAt: '2026-07-17T12:05:00Z',
    })
  })

  it('opens recent outcomes in place and uses an explicit full-Inbox action', async () => {
    const user = userEvent.setup()
    render(<CanvasInboxPopover />)

    const trigger = await screen.findByRole('button', { name: 'Inbox, 2 unread outcomes' })
    expect(trigger).toHaveTextContent('2')
    await user.click(trigger)

    const preview = await screen.findByRole('dialog', { name: 'Inbox preview' })
    expect(within(preview).getByText('Climate analysis')).toBeInTheDocument()
    expect(within(preview).getByText('“annual-results” written · 42 rows')).toBeInTheDocument()
    expect(mocks.setInboxQuery).not.toHaveBeenCalled()

    await user.click(within(preview).getByRole('button', { name: 'View all Inbox' }))
    expect(mocks.setInboxQuery).toHaveBeenCalledWith('')
  })

  it('keeps the notification control visible when there is no unread work', async () => {
    mocks.inboxUnreadCount.mockResolvedValue({ count: 0 })
    mocks.inboxList.mockResolvedValue({ items: [], nextCursor: null, hasMore: false })
    const user = userEvent.setup()
    render(<CanvasInboxPopover />)

    const trigger = await screen.findByRole('button', { name: 'Inbox, no unread outcomes' })
    expect(screen.queryByTestId('canvas-inbox-unread-badge')).not.toBeInTheDocument()
    await user.click(trigger)
    expect(await screen.findByText('You’re all caught up.')).toBeInTheDocument()
  })

  it('marks all unread outcomes with one atomic request without leaving Canvas', async () => {
    const user = userEvent.setup()
    render(<CanvasInboxPopover />)
    await user.click(await screen.findByRole('button', { name: 'Inbox, 2 unread outcomes' }))
    const preview = await screen.findByRole('dialog', { name: 'Inbox preview' })

    await user.click(within(preview).getByRole('button', { name: 'Mark all read' }))

    await waitFor(() => expect(mocks.inboxMarkAllRead).toHaveBeenCalledTimes(1))
    expect(mocks.inboxMarkRead).not.toHaveBeenCalled()
    expect(within(preview).getByText('You’re all caught up.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Inbox, no unread outcomes' })).toBeInTheDocument()
    expect(mocks.setInboxQuery).not.toHaveBeenCalled()
  })

  it('opens an authorized job while marking its outcome read', async () => {
    const user = userEvent.setup()
    render(<CanvasInboxPopover />)
    await user.click(await screen.findByRole('button', { name: 'Inbox, 2 unread outcomes' }))
    const preview = await screen.findByRole('dialog', { name: 'Inbox preview' })

    const openJobs = within(preview).getAllByRole('button', { name: 'Open job' })
    await user.click(openJobs[0]!)

    expect(mocks.setJobsQuery).toHaveBeenCalledWith('run=task-1')
    await waitFor(() => expect(mocks.inboxMarkRead).toHaveBeenCalledWith('item-1'))
  })

  it('keeps the last confirmed count when loading the preview fails', async () => {
    mocks.inboxList.mockRejectedValue(new Error('network lost'))
    const user = userEvent.setup()
    render(<CanvasInboxPopover />)

    const trigger = await screen.findByRole('button', { name: 'Inbox, 2 unread outcomes' })
    await user.click(trigger)

    expect(await screen.findByRole('alert')).toHaveTextContent('Couldn’t load Inbox: network lost')
    expect(screen.getByRole('button', { name: 'Inbox, 2 unread outcomes' })).toBeInTheDocument()
  })
})
