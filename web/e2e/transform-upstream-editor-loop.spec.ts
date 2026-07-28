import { expect, test } from '@playwright/test'

type CatalogTable = { id: string; name: string; uri: string; registrationId: string }

async function seededEvents(page: import('@playwright/test').Page): Promise<CatalogTable> {
  const response = await page.request.get('/api/catalog/search', {
    params: { q: 'events', mode: 'lexical', limit: 10 },
  })
  expect(response.ok()).toBeTruthy()
  const table = (await response.json() as CatalogTable[]).find((item) => item.name === 'events')
  if (!table) throw new Error('Missing seeded events catalog table')
  return table
}

async function openUnprimedEditor(page: import('@playwright/test').Page, suffix: string): Promise<string> {
  const events = await seededEvents(page)
  const canvasId = `upstream-editor-${suffix}-${Date.now()}`
  const graph = {
    id: canvasId, name: `Upstream editor ${suffix}`, version: 1, requirements: [],
    nodes: [
      { id: 'source', type: 'source', position: { x: 80, y: 180 }, data: {
        title: 'Events input', status: 'draft', config: {
          uri: events.uri, tableId: events.id, registrationId: events.registrationId,
        },
      } },
      { id: 'sample', type: 'sample', position: { x: 390, y: 180 }, data: {
        title: 'Editor sample', status: 'draft', config: { n: 8, seed: 42 },
      } },
      { id: 'transform', type: 'transform', position: { x: 700, y: 180 }, data: {
        title: 'Editor test', status: 'draft', config: {
          source: 'adhoc', mode: 'map', onError: 'raise',
          code: "def fn(row):\n    return {**row, 'tested_in_editor': True}",
        },
      } },
    ],
    edges: [
      { id: 'source-sample', source: 'source', sourceHandle: 'out', target: 'sample', targetHandle: 'in', data: { wire: 'dataset' } },
      { id: 'sample-transform', source: 'sample', sourceHandle: 'out', target: 'transform', targetHandle: 'in', data: { wire: 'sample' } },
    ],
  }
  const created = await page.request.post('/api/canvas', { data: graph })
  expect(created.ok(), await created.text()).toBeTruthy()
  await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=transform`)
  await expect(page.locator('.react-flow__node')).toHaveCount(3)
  await page.getByRole('button', { name: 'Edit code' }).last().click()
  await expect(page.getByRole('button', { name: 'Run upstream' })).toBeVisible()
  return canvasId
}

async function removeCanvas(page: import('@playwright/test').Page, canvasId: string): Promise<void> {
  expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBeTruthy()
}

test('Run upstream stays in the fullscreen Transform editor and selects its fresh retained result', async ({ page }) => {
  test.setTimeout(45_000)
  const canvasId = await openUnprimedEditor(page, 'success')
  try {
    await page.setViewportSize({ width: 1280, height: 720 })
    await page.getByRole('button', { name: 'Run upstream' }).click()
    await expect(page.getByRole('button', { name: 'Test code' })).toBeVisible()
    await expect(page.getByRole('status', { name: 'Upstream result ready' })).toBeVisible({ timeout: 20_000 })
    await expect(page.getByRole('button', { name: 'Test code' })).toBeEnabled()
    await expect(page.getByText('true', { exact: true }).first()).toBeVisible({ timeout: 20_000 })
  } finally {
    await removeCanvas(page, canvasId)
  }
})

test('fullscreen Transform confirms an upstream run without leaving the code-test loop', async ({ page }) => {
  test.setTimeout(45_000)
  const canvasId = await openUnprimedEditor(page, 'confirmation')
  try {
    await page.route('**/api/run/estimate', async (route) => {
      await route.fulfill({ json: {
        rows: 2_001, bytes: null, placement: 'local', needsConfirm: true,
        confirmationReasons: ['large_rows'], breakdown: 'confirmation required for test',
      } })
    })
    await page.getByRole('button', { name: 'Run upstream' }).click()
    const confirmation = page.getByRole('region', { name: 'Confirm upstream run' })
    await expect(confirmation).toBeVisible()
    await expect(confirmation).toContainText('2,001 rows')
    await expect(page.getByRole('button', { name: 'Test code' })).toBeVisible()
    await confirmation.getByRole('button', { name: 'Run upstream' }).click()
    await expect(page.getByRole('button', { name: 'Test code' })).toBeVisible()
    await expect(page.getByRole('status', { name: 'Upstream result ready' })).toBeVisible({ timeout: 20_000 })
  } finally {
    await removeCanvas(page, canvasId)
  }
})

test('fullscreen Transform reports an upstream failure without closing the editor', async ({ page }) => {
  const canvasId = await openUnprimedEditor(page, 'failure')
  try {
    await page.route('**/api/run/estimate', async (route) => {
      await route.fulfill({ status: 503, json: { detail: 'forced upstream estimate failure' } })
    })
    await page.getByRole('button', { name: 'Run upstream' }).click()
    await expect(page.getByRole('alert', { name: 'Upstream run failed' })).toContainText('forced upstream estimate failure')
    await expect(page.getByRole('button', { name: 'Test code' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Test code' })).toBeEnabled()
  } finally {
    await removeCanvas(page, canvasId)
  }
})
