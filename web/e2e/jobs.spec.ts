import { expect, test } from '@playwright/test'

const failedJob = {
  id: 'history-failed', runId: 'run-failed', jobType: 'run', status: 'failed',
  canvasId: 'canvas-jobs', canvasName: 'Climate analysis', targetNodeId: 'publish',
  nodeLabel: 'Publish results', backend: 'local', placement: 'local', attempt: 'run-failed',
  rows: null, ms: 1200, error: 'destination unavailable', outputs: [],
  createdAt: '2026-07-16T12:00:00Z',
}

test('filters, deep-links, and preserves a partial Jobs page at the supported viewport @ux-smoke', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 })
  let continuationAttempts = 0
  await page.route('**/api/jobs?*', async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get('cursor')
    if (cursor) {
      continuationAttempts += 1
      if (continuationAttempts === 1) {
        await route.fulfill({ status: 503, json: { detail: 'history store temporarily unavailable' } })
        return
      }
      await route.fulfill({ json: {
        items: [{ ...failedJob, id: 'history-older', runId: 'run-older', attempt: 'run-older', createdAt: '2026-07-15T12:00:00Z' }],
        nextCursor: null, hasMore: false,
      } })
      return
    }
    await route.fulfill({ json: { items: [failedJob], nextCursor: 'opaque-next', hasMore: true } })
  })

  await page.goto('/#/jobs')
  await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible()
  await expect(page.getByText('Climate analysis')).toBeVisible()
  await page.getByLabel('Filter jobs by status').selectOption('failed')
  await expect(page).toHaveURL(/#\/jobs\?status=failed/)

  await page.getByRole('button', { name: 'Open run run-failed in Climate analysis', expanded: false }).click()
  await expect(page.getByRole('alert')).toContainText('destination unavailable')
  await expect(page.getByRole('link', { name: 'Open node' })).toHaveAttribute(
    'href', '#/canvas/canvas-jobs?node=publish')
  await expect(page).toHaveURL(/run=run-failed/)
  await page.goBack()
  await expect(page).toHaveURL(/#\/jobs\?status=failed$/)
  await page.getByRole('button', { name: 'Open run run-failed in Climate analysis', expanded: false }).click()
  await page.reload()
  await expect(page.getByRole('alert')).toContainText('destination unavailable')

  await page.getByRole('button', { name: 'Load more' }).click()
  await expect(page.getByText(/Couldn’t load more Jobs/)).toBeVisible()
  await expect(page.getByText('Climate analysis')).toBeVisible()
  await page.getByRole('button', { name: 'Retry load more' }).click()
  await expect(page.getByText('run-older')).toBeVisible()
})
