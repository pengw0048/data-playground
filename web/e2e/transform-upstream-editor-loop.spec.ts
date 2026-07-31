import { expect, test } from '@playwright/test'

type CatalogTable = { id: string; name: string; uri: string; registrationId: string }
type PromotedTransform = { id: string; version: string }

async function seededEvents(page: import('@playwright/test').Page): Promise<CatalogTable> {
  const response = await page.request.get('/api/catalog/search', {
    params: { q: 'events', mode: 'lexical', limit: 10 },
  })
  expect(response.ok()).toBeTruthy()
  const table = (await response.json() as CatalogTable[]).find((item) => item.name === 'events')
  if (!table) throw new Error('Missing seeded events catalog table')
  return table
}

async function openUnprimedEditor(
  page: import('@playwright/test').Page,
  suffix: string,
  promoted?: PromotedTransform,
): Promise<string> {
  await page.setViewportSize({ width: 1280, height: 720 })
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
          ...(promoted ? {
            source: 'library', processor: promoted.id, version: promoted.version,
            mode: 'map', code: null,
          } : {
            source: 'adhoc', mode: 'map', onError: 'raise',
            code: "def fn(row):\n    return {**row, 'tested_in_editor': True}",
          }),
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
  await page.getByRole('button', {
    name: promoted ? 'View processor definition' : 'Edit code',
  }).last().click()
  await expect(page.getByRole('button', { name: 'Run upstream' })).toBeVisible()
  await expect(page.getByRole('button', {
    name: promoted ? 'Test transform' : 'Test code',
  })).toBeDisabled()
  return canvasId
}

async function promoteReusableTransform(
  page: import('@playwright/test').Page,
): Promise<PromotedTransform> {
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2)}`
  const response = await page.request.post('/api/processors/promote', { data: {
    id: `e2e.reusable-transform-${suffix}`,
    title: `Reusable transform ${suffix}`,
    blurb: 'Adds one deterministic field for bounded editor testing.',
    category: 'testing',
    mode: 'map',
    code: "def fn(row):\n    return {**row, 'tested_in_editor': True}",
    inputColumns: ['id'],
    inputSchema: [{ name: 'id', type: 'bigint' }],
    outputSchema: [{ name: 'tested_in_editor', type: 'bool' }],
    requirements: [],
  } })
  expect(response.ok(), await response.text()).toBeTruthy()
  return await response.json() as PromotedTransform
}

async function removeCanvas(page: import('@playwright/test').Page, canvasId: string): Promise<void> {
  expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBeTruthy()
}

test('Run upstream stays in the fullscreen Transform editor and selects its fresh retained result', async ({ page }) => {
  test.setTimeout(45_000)
  const canvasId = await openUnprimedEditor(page, 'success')
  try {
    await page.getByRole('button', { name: 'Run upstream' }).click()
    await expect(page.getByRole('button', { name: 'Test code' })).toBeVisible()
    await expect(page.getByRole('status', { name: 'Upstream run cancelled' })).toHaveCount(0)
    await expect(page.getByText('Test result · using Editor sample · 8 input rows', { exact: true })).toBeVisible({ timeout: 20_000 })
    await expect(page.getByRole('status', { name: 'Upstream result ready' })).toHaveCount(0)
    await expect(page.getByText('Preview uses up to 2,000 rows from each input; output may differ from a full run.')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Test code' })).toBeEnabled()
    await expect(page.getByText('true', { exact: true }).first()).toBeVisible({ timeout: 20_000 })
  } finally {
    await removeCanvas(page, canvasId)
  }
})

test('Library Transform stays reusable after its fresh upstream result is tested', async ({ page }) => {
  test.setTimeout(45_000)
  const promoted = await promoteReusableTransform(page)
  const canvasId = await openUnprimedEditor(page, 'library-retest', promoted)
  try {
    const automaticRefresh = page.waitForResponse((response) => (
      response.url().includes('/api/run/editor-preview')
      && response.request().method() === 'POST'
    ))
    await page.getByRole('button', { name: 'Run upstream' }).click()
    expect((await automaticRefresh).ok()).toBeTruthy()
    const testTransform = page.getByRole('button', { name: 'Test transform' })
    await expect(page.getByText(
      'Test result · using Editor sample · 8 input rows', { exact: true },
    )).toBeVisible({ timeout: 20_000 })
    await expect(testTransform).toBeEnabled()

    const firstRetest = page.waitForResponse((response) => (
      response.url().includes('/api/run/editor-preview')
      && response.request().method() === 'POST'
    ))
    await testTransform.click()
    expect((await firstRetest).ok()).toBeTruthy()
    await expect(page.getByText(
      'Test result · using Editor sample · 8 input rows', { exact: true },
    )).toBeVisible()
    await expect(testTransform).toBeEnabled()
  } finally {
    await removeCanvas(page, canvasId)
    expect((await page.request.delete(
      `/api/processors/${encodeURIComponent(promoted.id)}/versions/${encodeURIComponent(promoted.version)}`,
    )).ok()).toBeTruthy()
  }
})

test('fullscreen Transform confirms an upstream run without leaving the code-test loop', async ({ page }) => {
  test.setTimeout(45_000)
  const canvasId = await openUnprimedEditor(page, 'confirmation')
  try {
    const editor = page.locator('.monaco-editor').first()
    await expect(editor).toContainText('tested_in_editor')
    await page.getByRole('button', { name: 'Example rows', exact: true }).click()
    const fixture = page.getByRole('textbox', { name: 'Example rows JSON' })
    await fixture.fill('[{"event":"confirmation-sentinel"}]')
    await page.getByRole('button', { name: 'Upstream result' }).click()

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
    await expect(page.getByRole('button', { name: 'Test code' })).toBeDisabled()
    await confirmation.getByRole('button', { name: 'Cancel' }).click()
    await expect(page.getByRole('status', { name: 'Upstream run cancelled' })).toBeVisible()
    await expect(editor).toContainText('tested_in_editor')
    await page.getByRole('button', { name: 'Example rows', exact: true }).click()
    await expect(fixture).toHaveValue('[{"event":"confirmation-sentinel"}]')
    await page.getByRole('button', { name: 'Upstream result' }).click()

    await page.getByRole('button', { name: 'Run upstream' }).click()
    const retriedConfirmation = page.getByRole('region', { name: 'Confirm upstream run' })
    await expect(retriedConfirmation).toBeVisible()
    await retriedConfirmation.getByRole('button', { name: 'Run upstream' }).click()
    await expect(page.getByRole('button', { name: 'Test code' })).toBeVisible()
    await expect(page.getByText('Test result · using Editor sample · 8 input rows', { exact: true })).toBeVisible({ timeout: 20_000 })
    await expect(page.getByRole('button', { name: 'Test code' })).toBeEnabled()
    await expect(editor).toContainText('tested_in_editor')
    await page.getByRole('button', { name: 'Example rows', exact: true }).click()
    await expect(fixture).toHaveValue('[{"event":"confirmation-sentinel"}]')
  } finally {
    await removeCanvas(page, canvasId)
  }
})

test('fullscreen Transform keeps the input-cap warning when retained input metadata exceeds the cap', async ({ page }) => {
  test.setTimeout(45_000)
  const canvasId = await openUnprimedEditor(page, 'capped-input')
  try {
    await page.route('**/api/run/editor-preview', async (route) => {
      const response = await route.fetch()
      const body = await response.json()
      body.editorTestInput.rows = 2_001
      await route.fulfill({ response, json: body })
    })
    await page.getByRole('button', { name: 'Run upstream' }).click()
    await expect(page.getByText('Test result · using Editor sample · 2,001 input rows', { exact: true })).toBeVisible({ timeout: 20_000 })
    await expect(page.getByText('Preview uses up to 2,000 rows from each input; output may differ from a full run.')).toBeVisible()
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
    await expect(page.getByRole('button', { name: 'Test code' })).toBeDisabled()
    await expect(page.locator('.monaco-editor').first()).toContainText('tested_in_editor')
  } finally {
    await removeCanvas(page, canvasId)
  }
})
