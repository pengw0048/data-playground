import { test, expect } from '@playwright/test'
import { workspaceResource } from './support/workspace'

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

  test('creates, explores, and adds by exact local targets across reload', async ({ page }) => {
    const catalog = await page.request.get('/api/catalog/tables?limit=1')
    expect(catalog.ok()).toBe(true)
    const dataset = (await catalog.json()).items[0] as { id: string; name: string }
    const suffix = Date.now()
    const emptyName = `Workspace exact target ${suffix}`
    const exploreName = `Workspace exploration ${suffix}`
    let emptyCanvasId = ''
    let exploreCanvasId = ''

    try {
      await page.goto('/#/workspace')
      await page.getByRole('button', { name: 'New canvas here' }).click()
      await page.getByLabel('Canvas name').fill(emptyName)
      await page.getByRole('button', { name: 'Create canvas' }).click()
      await expect(page).toHaveURL(/#\/canvas\//)
      emptyCanvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)

      await page.getByTestId('app-menu').click()
      await page.getByText('Back to Workspace').click()
      await (await workspaceResource(page, 'dataset', dataset.name)).click()
      await page.getByTestId('detail-use').click()
      await page.getByLabel('New canvas name').fill(exploreName)
      await page.getByRole('button', { name: 'Create and open' }).click()
      await expect(page).toHaveURL(/#\/canvas\//)
      exploreCanvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
      await expect(page.locator('.react-flow__node', { hasText: dataset.name })).toBeVisible()

      await page.reload()
      await expect(page.locator('.react-flow__node', { hasText: dataset.name })).toBeVisible()
      await page.getByTestId('app-menu').click()
      await page.getByText('Back to Workspace').click()
      await (await workspaceResource(page, 'dataset', dataset.name)).click()
      await page.getByTestId('detail-use').click()
      await page.getByRole('button', { name: /^Add to canvas/ }).click()
      await page.getByLabel('Target canvas').selectOption({ label: `${emptyName} · ${emptyCanvasId}` })
      await page.getByRole('button', { name: 'Add and open' }).click()
      await expect(page).toHaveURL(new RegExp(`/#/canvas/${emptyCanvasId}$`))
      await expect(page.locator('.react-flow__node', { hasText: dataset.name })).toBeVisible()
      await page.reload()
      await expect(page.locator('.react-flow__node', { hasText: dataset.name })).toBeVisible()
    } finally {
      if (emptyCanvasId) await page.request.delete(`/api/canvas/${emptyCanvasId}`)
      if (exploreCanvasId) await page.request.delete(`/api/canvas/${exploreCanvasId}`)
    }
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

test('pins a Source revision, persists it across reload, and keeps the control in the supported viewport @ux-smoke', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 })
  const canvasId = `source-pin-${Date.now()}`
  const table = {
    id: 'pin-table', name: 'Pinned Lance source', uri: '/mock/pinned-source.lance',
    rowCount: 2, version: 'v2', columns: [{ name: 'value', type: 'bigint', capabilities: [] }],
  }
  const created = await page.request.post('/api/canvas', { data: {
    id: canvasId, name: 'Source pin viewport', version: 1,
    nodes: [{ id: 'source', type: 'source', position: { x: 80, y: 80 }, data: {
      title: 'Pinned source', status: 'draft', config: { uri: table.uri, tableId: table.id },
    } }], edges: [],
  } })
  expect(created.ok()).toBe(true)

  await page.route('**/api/catalog/tables?*', async (route) => {
    await route.fulfill({ json: { items: [table], total: 1, offset: 0, limit: 50, hasMore: false } })
  })
  await page.route('**/api/catalog/tables/pin-table/revisions*', async (route) => {
    await route.fulfill({ json: { items: [
      { datasetId: 'opaque-dataset', revisionId: '2', committedAt: '2026-07-16T12:00:00Z', retentionOwner: 'provider' },
      { datasetId: 'opaque-dataset', revisionId: '1', committedAt: '2026-07-15T12:00:00Z', retentionOwner: 'provider' },
    ], nextCursor: null, hasMore: false } })
  })
  await page.route('**/api/catalog/tables/pin-table/revisions/capabilities', async (route) => {
    await route.fulfill({ json: { selectors: ['exact', 'latest'], asOfOrdering: null, timezone: null } })
  })
  await page.route('**/api/catalog/revisions/opaque-dataset/1', async (route) => {
    await route.fulfill({ json: {
      datasetId: 'opaque-dataset', revisionId: '1', committedAt: '2026-07-15T12:00:00Z',
      retentionOwner: 'provider', parentRevisionId: null, producerOperation: 'create',
      summary: { rowCount: 1, dataFileCount: 1, totalBytes: 8, fragmentCount: 1 },
      preview: { columns: table.columns, rows: [{ value: 1 }], hasMore: false, rowLimit: 100 },
    } })
  })

  try {
    await page.goto(`/#/canvas/${canvasId}`)
    const node = page.locator('.react-flow__node', { hasText: 'Pinned source' })
    await expect(node).toBeVisible()
    await page.getByRole('button', { name: 'Pin exact revision' }).click()
    await page.getByText('1', { exact: true }).click()
    await expect(page.getByText(/Pinned exact revision 1 · 1 rows/)).toBeVisible()
    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${canvasId}`)
      return (await response.json()).nodes[0].data.config.datasetRef
    }).toEqual({
      kind: 'exact', datasetId: 'opaque-dataset', revisionId: '1',
      lastKnown: { committedAt: '2026-07-15T12:00:00Z' },
    })

    await page.reload()
    const control = page.getByRole('button', { name: 'Change pinned revision 1' })
    await expect(control).toBeVisible()
    await expect(page.getByText(/Pinned exact revision 1 · 1 rows/)).toBeVisible()
    const box = await control.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.y).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(1024)
    expect(box!.y + box!.height).toBeLessThanOrEqual(768)
  } finally {
    await page.request.delete(`/api/canvas/${canvasId}`)
  }
})
