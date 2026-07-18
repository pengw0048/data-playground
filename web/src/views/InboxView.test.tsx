import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { InboxView, mergeMonotonic } from './InboxView'
import { useStore } from '../store/graph'

const mocks = vi.hoisted(() => ({
  inboxList: vi.fn(),
  inboxMarkRead: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    inboxList: mocks.inboxList,
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
    mocks.inboxMarkRead.mockReset()
    mocks.inboxList.mockResolvedValue({ items: [item()], hasMore: false, nextCursor: null })
    useStore.setState({ view: 'inbox', inboxQuery: '', jobsQuery: '', toasts: [] } as never)
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
    expect(screen.getByText('Canvas unavailable')).toBeInTheDocument()
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
