import { expect, test, type APIRequestContext, type Page, type Route } from '@playwright/test'
import { goToWorkspace, workspaceResource } from './support/workspace'

const fullProfile = process.env.DP_E2E_FIXTURE_PROFILE === 'full'
const workspaceRoot = /\/api\/workspace\/containers\/workspace-local-root(?:\?|$)/

async function openWorkspaceTable(page: Page, name: string) {
  await (await workspaceResource(page, 'dataset', name)).click()
  await expect(page.getByRole('dialog', { name })).toBeVisible()
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

  test('uses the large catalog and relationship-dense fixtures', async ({ page }) => {
    await goToWorkspace(page)
    await openWorkspaceTable(page, 'catalog_119')

    const [left, right] = await Promise.all([
      namedTable(page.request, 'relationship_dense_00'),
      namedTable(page.request, 'relationship_dense_01'),
    ])
    const relation = {
      leftUri: left.uri, leftColumns: ['right_id'], rightUri: right.uri, rightColumns: ['id'], cardinality: 'N:1',
    }
    const declared = await page.request.post('/api/catalog/relationships', { data: relation })
    expect(declared.ok()).toBeTruthy()

    await page.getByRole('button', { name: 'Close', exact: true }).click()
    await openWorkspaceTable(page, left.name)
    await page.getByTestId('detail-relationships').click()
    await expect(page.getByText('Relationships', { exact: true })).toBeVisible()
    await expect(page.locator('.react-flow__node', { hasText: left.name })).toBeVisible()
    await expect(page.locator('.react-flow__node', { hasText: right.name })).toBeVisible()
  })

  test('injects the declared slow, unavailable, permission, stale, partial-failure, and recovery states', async ({ page }) => {
    let releaseSlow: (() => void) | undefined
    const slow = new Promise<void>((resolve) => { releaseSlow = resolve })
    await page.route(workspaceRoot, async (route) => { await slow; await route.continue() }, { times: 1 })
    const workspaceReady = goToWorkspace(page)
    await expect(page.getByText('Loading Workspace…', { exact: true })).toBeVisible()
    releaseSlow!()
    await workspaceReady
    await expect(await workspaceResource(page, 'dataset', 'catalog_119')).toBeVisible()

    await page.route(workspaceRoot, (route) => route.fulfill({ status: 503, body: 'unavailable' }), { times: 1 })
    await page.getByTestId('workspace-reload').click()
    await expect(page.getByText(/Couldn't load this Workspace location: Service Unavailable/i)).toBeVisible()
    await page.getByRole('button', { name: 'Retry' }).click()
    await expect(await workspaceResource(page, 'dataset', 'catalog_119')).toBeVisible()

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
