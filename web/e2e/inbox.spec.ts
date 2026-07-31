import { expect, test } from '@playwright/test'

const completedLocal = {
  id: 'inbox-local',
  taskId: 'task-local',
  canvasId: 'canvas-inbox',
  canvasName: 'Climate analysis',
  taskKind: 'managed_local_write',
  outcome: 'completed',
  diagnosticCode: null,
  completedWrite: { outputName: 'annual-results', rowCount: 12 },
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
  await page.setViewportSize({ width: 1280, height: 720 })
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
  await expect(page.getByTestId('inbox-unread-badge')).toHaveCount(1)
  await expect(page.getByTestId('inbox-unread-badge')).toHaveText('3')
  await page.getByTestId('rail-inbox').click()
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  await expect(page.getByText('Climate analysis').first()).toBeVisible()
  await expect(page.getByText('“annual-results” written · 12 rows')).toBeVisible()
  await expect(page.getByText('external wait deadline')).toBeVisible()
  await expect(page.getByText('Cancelled', { exact: true })).toBeVisible()
  await expect(page.getByText(/traceback|secret boom/i)).toHaveCount(0)

  const disabledOpen = page.getByRole('button', { name: 'Open job' }).nth(2)
  await expect(disabledOpen).toBeDisabled()
  await page.getByRole('button', { name: 'Open job' }).first().click()
  await expect(page).toHaveURL(/#\/jobs\?run=task-local/)

  await page.goto('/#/inbox?filter=unread')
  await page.getByTestId('rail-workspace').click()
  await page.getByTestId('rail-inbox').click()
  await expect(page).toHaveURL(/#\/inbox\?filter=unread/)
  await page.reload()
  await expect(page.getByRole('combobox', { name: 'Filter inbox items' })).toHaveValue('unread')
})

test('returns from an exact Inbox dataset to the originating filter @ux-smoke', async ({ page }) => {
  const catalog = await page.request.get('/api/catalog/tables?limit=1')
  expect(catalog.ok()).toBe(true)
  const template = (await catalog.json()).items[0] as Record<string, unknown>
  let readAt: string | null = null
  const datasetItem = {
    id: 'inbox-dataset', taskId: 'upsert-task-1', canvasId: null, canvasName: null,
    datasetContext: {
      taskKind: 'keyed_upsert_write', datasetId: 'dataset-inbox', revisionId: 'rev-inbox',
      name: 'Inbox upserts',
    },
    taskKind: 'keyed_upsert_write', outcome: 'completed', diagnosticCode: null,
    terminalAt: '2026-07-20T12:00:00Z', readAt, jobAvailable: true,
  }
  await page.route('**/api/inbox/unread-count', async (route) => route.fulfill({ json: { count: readAt ? 0 : 1 } }))
  await page.route('**/api/inbox?*', async (route) => {
    const filter = new URL(route.request().url()).searchParams.get('filter')
    const items = filter === 'unread' && readAt ? [] : [{ ...datasetItem, readAt }]
    await route.fulfill({ json: { items, nextCursor: null, hasMore: false } })
  })
  await page.route('**/api/inbox/inbox-dataset/read', async (route) => {
    readAt = '2026-07-20T12:05:00Z'
    await route.fulfill({ json: { ...datasetItem, readAt } })
  })
  await page.route('**/api/catalog/tables/dataset-inbox?registration=true', async (route) => route.fulfill({
    json: { ...template, id: 'dataset-inbox', name: 'Inbox upserts' },
  }))
  await page.route('**/api/catalog/revision-details', async (route) => {
    const request = route.request().postDataJSON() as { datasetId: string; revisionId: string }
    expect(request).toEqual({ datasetId: 'dataset-inbox', revisionId: 'rev-inbox' })
    await route.fulfill({ json: {
      datasetId: 'dataset-inbox', revisionId: 'rev-inbox', committedAt: '2026-07-20T12:00:00Z',
      retentionOwner: 'core', parentRevisionId: null, producerOperation: 'keyed-upsert',
      summary: { rowCount: 1, dataFileCount: 1, totalBytes: 64, fragmentCount: 1 },
      preview: {
        columns: [{ name: 'id', type: 'BIGINT', capabilities: [] }], rows: [{ id: 7 }],
        hasMore: false, rowLimit: 100,
      },
    } })
  })

  await page.goto('/#/inbox?filter=unread')
  const exactDataset = page.getByRole('link', { name: 'Open dataset' })
  await expect(exactDataset).toHaveAttribute(
    'href',
    '#/workspace/dataset%3Adataset-inbox?scope=datasets&revision=rev-inbox&revisionDataset=dataset-inbox&returnView=inbox&returnQuery=filter%3Dunread',
  )
  await exactDataset.click()
  const viewer = page.getByTestId('dataset-viewer')
  await expect(viewer.getByRole('row', { name: '7' })).toBeVisible()
  await viewer.getByRole('button', { name: 'Back to Inbox' }).click()

  await expect(page).toHaveURL(/#\/inbox\?filter=unread$/)
  await expect(page.getByRole('combobox', { name: 'Filter inbox items' })).toHaveValue('unread')
  await expect(page.getByText('You’re all caught up.')).toBeVisible()
})
