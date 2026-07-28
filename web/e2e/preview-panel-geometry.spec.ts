import { expect, test, type Page } from '@playwright/test'

type CatalogTable = { id: string; name: string; uri: string; registrationId: string }

async function seededEvents(page: Page): Promise<CatalogTable> {
  const response = await page.request.get('/api/catalog/search', {
    params: { q: 'events', mode: 'lexical', limit: 10 },
  })
  expect(response.ok()).toBeTruthy()
  const table = (await response.json() as CatalogTable[]).find((item) => item.name === 'events')
  if (!table) throw new Error('Missing seeded events catalog table')
  return table
}

async function openNodePreview(page: Page, suffix: string, nodeId = 'transform') {
  const events = await seededEvents(page)
  const canvasId = `preview-geometry-${suffix}-${Date.now()}`
  const created = await page.request.post('/api/canvas', { data: {
    id: canvasId, name: 'Preview geometry', version: 1, requirements: [],
    nodes: [
      { id: 'source', type: 'source', position: { x: 0, y: 80 }, data: {
        title: 'Events input', status: 'draft', config: {
          uri: events.uri, tableId: events.id, registrationId: events.registrationId,
        },
      } },
      { id: 'sample', type: 'sample', position: { x: 250, y: 80 }, data: {
        title: 'Preview sample', status: 'draft', config: { n: 50, seed: 42 },
      } },
      { id: 'transform', type: 'transform', position: { x: 500, y: 80 }, data: {
        title: 'Preview transform', status: 'draft', config: {
          source: 'adhoc', mode: 'map', onError: 'raise',
          code: "def fn(row):\n    return {**row, 'geometry_checked': True}",
        },
      } },
    ],
    edges: [
      { id: 'source-sample', source: 'source', sourceHandle: 'out', target: 'sample', targetHandle: 'in', data: { wire: 'dataset' } },
      { id: 'sample-transform', source: 'sample', sourceHandle: 'out', target: 'transform', targetHandle: 'in', data: { wire: 'sample' } },
    ],
  } })
  expect(created.ok(), await created.text()).toBeTruthy()
  await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=${encodeURIComponent(nodeId)}`)
  await expect(page.locator('.react-flow__node')).toHaveCount(3)
  await page.locator(`.react-flow__node[data-id="${nodeId}"]`).click()
  await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
  const panel = page.getByTestId('panel-data')
  await expect(panel).toBeVisible()
  return { canvasId, panel }
}

async function removeCanvas(page: Page, canvasId: string) {
  expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBeTruthy()
}

test('docks a rightmost Transform preview above the toolbar at 1280x720', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  const { canvasId, panel } = await openNodePreview(page, 'minimum')
  try {
    await expect(panel).toHaveAttribute('data-presentation', 'docked')
    const panelBox = await panel.boundingBox()
    const transformBox = await page.locator('.react-flow__node[data-id="transform"]').boundingBox()
    const toolbarBox = await page.getByTestId('toolbar').boundingBox()
    const chromeBoxes = await page.locator('[data-layout-region="canvas-top-chrome"]').evaluateAll((elements) => (
      elements.map((element) => {
        const rect = element.getBoundingClientRect()
        return { top: rect.top, bottom: rect.bottom }
      })
    ))
    const chromeBottom = Math.max(...chromeBoxes.map((box) => box.bottom))
    expect(panelBox).not.toBeNull()
    expect(transformBox).not.toBeNull()
    expect(toolbarBox).not.toBeNull()
    expect(panelBox!.height).toBeGreaterThanOrEqual(401)
    expect(panelBox!.x).toBeLessThan(transformBox!.x)
    expect(panelBox!.y).toBeGreaterThanOrEqual(chromeBottom)
    expect(panelBox!.y + panelBox!.height).toBeLessThanOrEqual(toolbarBox!.y + 0.5)
    const badge = page.getByTestId('kernel-badge')
    await badge.click()
    await expect(page.getByText('Execution kernel', { exact: true })).toBeVisible()
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'Fit view', exact: true }).click()
    await expect(page.getByTestId('toolbar')).toBeVisible()
  } finally {
    await removeCanvas(page, canvasId)
  }
})

test('keeps an anchored data preview and restores it after maximize at 1440x900', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const { canvasId, panel } = await openNodePreview(page, 'reference', 'source')
  try {
    await expect(panel).toHaveAttribute('data-presentation', 'anchored')
    await panel.getByRole('button', { name: 'Maximize' }).click()
    await expect(page.getByTestId('panel-data')).toHaveAttribute('data-presentation', 'maximized')
    await page.getByTestId('panel-data').getByRole('button', { name: 'Restore' }).click()
    await expect(page.getByTestId('panel-data')).toHaveAttribute('data-presentation', 'anchored')
  } finally {
    await removeCanvas(page, canvasId)
  }
})
