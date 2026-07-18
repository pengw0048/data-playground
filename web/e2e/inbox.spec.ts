import { expect, test } from '@playwright/test'

const completedLocal = {
  id: 'inbox-local',
  taskId: 'task-local',
  canvasId: 'canvas-inbox',
  canvasName: 'Climate analysis',
  taskKind: 'managed_local_write',
  outcome: 'completed',
  diagnosticCode: null,
  terminalAt: '2026-07-17T12:00:00Z',
  readAt: null,
  jobAvailable: true,
}

const failedWait = {
  id: 'inbox-wait',
  taskId: 'task-wait',
  canvasId: 'canvas-inbox',
  canvasName: 'Climate analysis',
  taskKind: 'external_wait',
  outcome: 'failed',
  diagnosticCode: 'external_wait_deadline',
  terminalAt: '2026-07-17T11:00:00Z',
  readAt: null,
  jobAvailable: true,
}

const cancelledLocal = {
  id: 'inbox-cancel',
  taskId: 'task-cancel',
  canvasId: 'canvas-inbox',
  canvasName: null,
  taskKind: 'managed_local_write',
  outcome: 'cancelled',
  diagnosticCode: null,
  terminalAt: '2026-07-17T10:00:00Z',
  readAt: null,
  jobAvailable: false,
}

test('Inbox badge, filter, open job, and redacted outcomes @ux-smoke', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 })
  let unread = 3
  await page.route('**/api/inbox/unread-count', async (route) => {
    await route.fulfill({ json: { count: unread } })
  })
  await page.route('**/api/inbox?*', async (route) => {
    const filter = new URL(route.request().url()).searchParams.get('filter')
    const items = filter === 'unread'
      ? [completedLocal, failedWait, cancelledLocal]
      : [completedLocal, failedWait, cancelledLocal]
    await route.fulfill({ json: { items, nextCursor: null, hasMore: false } })
  })
  await page.route('**/api/inbox/*/read', async (route) => {
    unread = Math.max(0, unread - 1)
    const id = route.request().url().split('/').at(-2)
    const source = [completedLocal, failedWait, cancelledLocal].find((row) => row.id === id) ?? completedLocal
    await route.fulfill({ json: { ...source, readAt: '2026-07-17T12:30:00Z' } })
  })
  await page.route('**/api/jobs?*', async (route) => {
    await route.fulfill({ json: { items: [], nextCursor: null, hasMore: false } })
  })

  await page.goto('/#/workspace')
  await expect(page.getByTestId('inbox-unread-badge')).toHaveText('3')
  await page.getByTestId('rail-inbox').click()
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  await expect(page.getByText('Climate analysis').first()).toBeVisible()
  await expect(page.getByText('external wait deadline')).toBeVisible()
  await expect(page.getByText('Cancelled')).toBeVisible()
  await expect(page.getByText(/traceback|secret boom/i)).toHaveCount(0)

  const disabledOpen = page.getByRole('button', { name: 'Open job' }).nth(2)
  await expect(disabledOpen).toBeDisabled()
  await page.getByRole('button', { name: 'Open job' }).first().click()
  await expect(page).toHaveURL(/#\/jobs\?run=task-local/)
})
