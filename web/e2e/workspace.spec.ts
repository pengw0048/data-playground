import { test, expect } from '@playwright/test'

test.describe('local Workspace golden journey @ux-smoke', () => {
  test('shows normal canvas and catalog lifecycles through stable Workspace navigation', async ({ page }) => {
    const catalog = await page.request.get('/api/catalog/tables?limit=1')
    expect(catalog.ok()).toBe(true)
    const dataset = (await catalog.json()).items[0] as { id: string; name: string }
    expect(dataset).toBeTruthy()

    const canvasId = `workspace-golden-${Date.now()}`
    const canvasName = 'Workspace golden canvas'
    const created = await page.request.post('/api/canvas', {
      data: { id: canvasId, name: canvasName, version: 1, nodes: [], edges: [] },
    })
    expect(created.ok()).toBe(true)

    await page.goto('/#/workspace')
    await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible()
    await expect(page.getByRole('button', { name: `Open canvas ${canvasName}` })).toBeVisible()
    await expect(page.getByRole('button', { name: `Open dataset ${dataset.name}` })).toBeVisible()

    await page.getByRole('button', { name: `Open dataset ${dataset.name}` }).click()
    await expect(page.getByRole('dialog', { name: dataset.name })).toBeVisible()
    await page.getByRole('button', { name: 'Close' }).click()

    await page.getByRole('button', { name: `Open canvas ${canvasName}` }).click()
    await expect(page).toHaveURL(new RegExp(`/#/canvas/${canvasId}$`))
    await page.goBack()
    await expect(page).toHaveURL(/#\/workspace\/container%3Aworkspace-local-root$/)
    await expect(page.getByRole('button', { name: `Open canvas ${canvasName}` })).toBeVisible()
  })
})

test('browses and opens one exact retained dataset revision without drifting to latest', async ({ page }) => {
  const catalog = await page.request.get('/api/catalog/tables?limit=1')
  expect(catalog.ok()).toBe(true)
  const dataset = (await catalog.json()).items[0] as { id: string; name: string }
  expect(dataset).toBeTruthy()

  let historyRequests = 0
  await page.route('**/api/catalog/tables/*/revisions*', async (route) => {
    historyRequests += 1
    const cursor = new URL(route.request().url()).searchParams.get('cursor')
    await route.fulfill({ json: cursor
      ? { items: [{ datasetId: 'stable-dataset', revisionId: 'rev-1', committedAt: '2026-07-15T12:00:00Z', retentionOwner: 'provider' }], nextCursor: null, hasMore: false }
      : { items: [{ datasetId: 'stable-dataset', revisionId: 'rev-2', committedAt: '2026-07-16T12:00:00Z', retentionOwner: 'provider' }], nextCursor: 'opaque-page-2', hasMore: true },
    })
  })
  await page.route('**/api/catalog/revisions/**', async (route) => {
    const revisionId = decodeURIComponent(new URL(route.request().url()).pathname.split('/').pop()!)
    await route.fulfill({ json: revisionId === 'rev-2' ? {
      datasetId: 'stable-dataset', revisionId: 'rev-2', committedAt: '2026-07-16T12:00:00Z',
      retentionOwner: 'provider', parentRevisionId: 'rev-1', producerOperation: 'append',
      summary: { rowCount: 2, dataFileCount: 2, totalBytes: 32, fragmentCount: 2 },
      preview: {
        columns: [{ fieldId: 'amount', name: 'amount', type: 'int', nullable: false, provenance: 'provider', capabilities: [] }],
        rows: [{ amount: 2 }], hasMore: true, rowLimit: 100,
      },
    } : {
      datasetId: 'stable-dataset', revisionId: 'rev-1', committedAt: '2026-07-15T12:00:00Z',
      retentionOwner: 'provider', parentRevisionId: null, producerOperation: 'create',
      summary: { rowCount: 1, dataFileCount: 1, totalBytes: 16, fragmentCount: 1 },
      preview: {
        columns: [{ fieldId: 'amount', name: 'amount', type: 'bigint', nullable: false, provenance: 'provider', capabilities: [] }],
        rows: [{ amount: 1 }], hasMore: false, rowLimit: 100,
      },
    } })
  })

  await page.goto('/#/workspace')
  await page.getByRole('button', { name: `Open dataset ${dataset.name}` }).click()
  await expect(page.getByTestId('dataset-revision-history')).toBeVisible()
  await expect(page.getByText('rev-2')).toBeVisible()
  await page.getByTestId('revision-history-load-more').click()
  await expect(page.getByText('rev-1')).toBeVisible()

  await page.getByRole('button', { name: 'Open revision rev-2' }).click()
  await expect(page.getByText('Exact revision rev-2')).toBeVisible()
  await expect(page.getByText(/Parent rev-1 · producer append/)).toBeVisible()
  await expect(page.getByText('breaking')).toBeVisible()
  await expect(page.getByText(/Preview truncated at 100 rows.*exact revision/i)).toBeVisible()
  expect(historyRequests).toBeGreaterThanOrEqual(2)

  await page.reload()
  await expect(page.getByRole('dialog', { name: dataset.name })).toBeVisible()
  await expect(page.getByTestId('dataset-revision-history')).toBeVisible()
})
