import { expect, test, type APIRequestContext, type Page, type Route } from '@playwright/test'

const fullProfile = process.env.DP_E2E_FIXTURE_PROFILE === 'full'
const catalogTables = /\/api\/catalog\/tables(?:\?|$)/

async function goToTables(page: Page) {
  await page.goto('/#/files')
  await page.getByTestId('rail-tables').click()
  await expect(page.getByRole('heading', { name: 'Tables' })).toBeVisible()
}

async function openCatalogTable(page: Page, name: string) {
  await page.getByTestId('catalog-search').fill(name)
  const table = page.getByRole('button', { name: `Open table ${name}`, exact: true })
  await expect(table).toBeVisible({ timeout: 15_000 })
  await table.click()
}

async function namedTable(request: APIRequestContext, name: string) {
  const response = await request.get(`/api/catalog/tables?q=${encodeURIComponent(name)}`)
  expect(response.ok(), `catalog query for ${name}`).toBeTruthy()
  const page = await response.json()
  const table = page.items.find((candidate: { name: string }) => candidate.name === name)
  expect(table, `fixture table ${name}`).toBeTruthy()
  return table as { id: string; name: string; uri: string }
}

async function freshSource(page: Page, uri: string) {
  await page.goto('/')
  const previous = await page.evaluate(() => location.hash)
  await page.getByTestId('file-menu').click()
  await page.getByText('New file').click()
  await expect.poll(() => page.evaluate(() => location.hash)).not.toBe(previous)
  await page.getByRole('button', { name: 'Sources & sinks', exact: true }).click()
  await page.locator('.dp-panel', { hasText: 'source' }).last().getByText('source', { exact: true }).click()
  const inspector = page.getByTestId('inspector')
  await inspector.locator('label').filter({ hasText: 'uri' }).locator('input').fill(uri)
  return inspector
}

test.describe('full researcher acceptance matrix', () => {
  test.skip(!fullProfile, 'full fixtures are exercised by the scheduled, release, and matrix-changing PR workflow')

  test('uses the large catalog, relationship-dense, and temporal/multimodal fixtures', async ({ page }) => {
    await goToTables(page)
    await openCatalogTable(page, 'catalog_119')
    await expect(page.getByRole('dialog', { name: 'catalog_119' })).toBeVisible()

    // Search the real catalog rather than assuming fixture order. The three synchronized streams all
    // need to remain discoverable after the 120-entry catalog is present.
    for (const name of ['episodes', 'frames', 'audio_windows']) {
      await page.getByRole('button', { name: 'Close' }).click()
      await openCatalogTable(page, name)
      await expect(page.getByRole('dialog', { name })).toBeVisible()
    }

    const [left, right] = await Promise.all([
      namedTable(page.request, 'relationship_dense_00'),
      namedTable(page.request, 'relationship_dense_01'),
    ])
    const relation = {
      leftUri: left.uri, leftColumns: ['right_id'], rightUri: right.uri, rightColumns: ['id'], cardinality: 'N:1',
    }
    const declared = await page.request.post('/api/catalog/relationships', { data: relation })
    expect(declared.ok()).toBeTruthy()

    await page.getByRole('button', { name: 'Close' }).click()
    await openCatalogTable(page, left.name)
    await page.getByTestId('detail-relationships').click()
    await expect(page.getByText('Relationships', { exact: true })).toBeVisible()
    await expect(page.locator('.react-flow__node', { hasText: left.name })).toBeVisible()
    await expect(page.locator('.react-flow__node', { hasText: right.name })).toBeVisible()
  })

  test('injects the declared slow, unavailable, permission, stale, partial-failure, and recovery states', async ({ page }) => {
    let releaseSlow: (() => void) | undefined
    const slow = new Promise<void>((resolve) => { releaseSlow = resolve })
    await page.route(catalogTables, async (route) => { await slow; await route.continue() }, { times: 1 })
    await goToTables(page)
    await expect(page.getByText('Loading…', { exact: true }).last()).toBeVisible()
    releaseSlow!()
    await page.getByTestId('catalog-search').fill('catalog_119')
    await expect(page.getByRole('button', { name: 'Open table catalog_119', exact: true })).toBeVisible({ timeout: 15_000 })

    await page.route(catalogTables, (route) => route.fulfill({ status: 503, body: 'unavailable' }), { times: 1 })
    await page.getByTestId('catalog-search').fill('episodes')
    await expect(page.getByText(/Couldn't load the catalog: Service Unavailable/i)).toBeVisible()
    await page.getByTestId('catalog-retry').click()
    await expect(page.getByRole('button', { name: 'Open table episodes', exact: true })).toBeVisible({ timeout: 15_000 })

    const inspector = await freshSource(page, 'events')
    await page.route('**/api/run/preview', (route) => route.fulfill({
      contentType: 'application/json', body: JSON.stringify({ error: true, reason: 'partial failure: one partition is unavailable' }),
    }), { times: 1 })
    await inspector.getByRole('button', { name: 'View data' }).click()
    await expect(page.getByText('partial failure: one partition is unavailable')).toBeVisible()
    await page.getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByTestId('panel-data').getByText(/^rows \d+–\d+$/)).toBeVisible({
      timeout: 15_000,
    })

    // The same full workflow also executes ux-golden-workflows.spec.ts, which mutates a graph after
    // preview and asserts the stale-reference state before allowing refresh.

    const denyNewCanvas = (route: Route) => {
      if (route.request().method() !== 'POST') return route.continue()
      return route.fulfill({ status: 403, body: JSON.stringify({ detail: 'forbidden' }) })
    }
    await page.route('**/api/canvas', denyNewCanvas)
    await page.getByTestId('file-menu').click()
    await page.getByText('New file').click()
    await expect(page.getByTestId('toast').filter({ hasText: 'permission' })).toContainText('permission')
    await page.unroute('**/api/canvas', denyNewCanvas)
  })
})
