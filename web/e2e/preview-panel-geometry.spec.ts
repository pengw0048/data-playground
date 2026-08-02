import { expect, test, type Page, type TestInfo } from '@playwright/test'
import { goldenCanvas, installCanvas } from './support/ux-fixtures'

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

async function openFullResult(page: Page, suffix: string) {
  const doc = goldenCanvas(`result-maximize-${suffix}-${Date.now()}`, 'Result maximize', 'Result maximize source')
  await installCanvas(page.request, doc)
  const graph = {
    id: doc.id, version: doc.version, requirements: doc.requirements ?? [],
    nodes: doc.nodes.map((node) => ({
      id: node.id, type: node.type, position: node.position, parentId: node.parentId ?? null,
      data: { title: node.data.title, config: node.data.config, status: node.data.status,
        bypassed: node.data.bypassed, disabled: node.data.disabled },
    })),
    edges: doc.edges,
  }
  const started = await page.request.post('/api/run', { data: { graph, targetNodeId: 'filter', confirmed: true } })
  expect(started.ok(), started.ok() ? '' : await started.text()).toBe(true)
  const runId = (await started.json()).runId as string
  await expect.poll(async () => {
    const response = await page.request.get(`/api/run/${encodeURIComponent(runId)}`)
    return (await response.json()).status
  }, { timeout: 30_000 }).toBe('done')
  await page.goto(`/#/canvas/${doc.id}`)
  const filter = page.locator('.react-flow__node', { hasText: 'UX golden filter' })
  await filter.click()
  await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
  const panel = page.getByTestId('panel-data')
  await expect(panel.getByTestId('full-result-status')).toBeVisible()
  return { canvasId: doc.id, panel }
}

async function expectMaximizedResultGeometry(page: Page, panel: ReturnType<Page['getByTestId']>, testInfo: TestInfo) {
  await panel.getByRole('button', { name: 'Maximize' }).click()
  await expect(panel).toHaveAttribute('data-presentation', 'maximized')
  await expect(panel.getByRole('button', { name: 'Restore' })).toBeVisible()
  const geometry = await panel.evaluate((element) => {
    const card = element.firstElementChild as HTMLElement | null
    const body = element.querySelector<HTMLElement>('[data-testid="full-result-body"]')
    const table = body?.querySelector('table')
    if (!card || !body || !table) return null
    const overlay = element.getBoundingClientRect()
    const cardBox = card.getBoundingClientRect()
    const bodyBox = body.getBoundingClientRect()
    const tableBox = table.getBoundingClientRect()
    return {
      overlay: { left: overlay.left, top: overlay.top, right: overlay.right, bottom: overlay.bottom },
      card: { left: cardBox.left, top: cardBox.top, right: cardBox.right, bottom: cardBox.bottom },
      body: { top: bodyBox.top, bottom: bodyBox.bottom, clientHeight: body.clientHeight, scrollHeight: body.scrollHeight },
      table: { top: tableBox.top, bottom: tableBox.bottom },
    }
  })
  expect(geometry).not.toBeNull()
  expect(geometry!.overlay).toEqual({ left: 0, top: 0, right: page.viewportSize()!.width, bottom: page.viewportSize()!.height })
  expect(geometry!.card.left).toBeGreaterThan(0)
  expect(geometry!.card.right).toBeLessThan(page.viewportSize()!.width)
  expect(geometry!.body.clientHeight).toBeGreaterThan(500)
  expect(Math.abs(geometry!.body.bottom - geometry!.card.bottom)).toBeLessThanOrEqual(1)
  expect(Math.abs(geometry!.table.top - geometry!.body.top)).toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath(`result-maximized-${page.viewportSize()!.width}x${page.viewportSize()!.height}.png`) })
  await panel.getByRole('button', { name: 'Restore' }).click()
  await expect(panel).not.toHaveAttribute('data-presentation', 'maximized')
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
    await page.getByRole('button', { name: /Execution target:/ }).click()
    await expect(page.getByText('Run this Canvas on', { exact: true })).toBeVisible()
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

test('maximizes a full result to the viewport and gives its table the remaining height', async ({ page }, testInfo) => {
  const { canvasId, panel } = await openFullResult(page, 'full')
  try {
    for (const viewport of [{ width: 1280, height: 720 }, { width: 1440, height: 900 }]) {
      await page.setViewportSize(viewport)
      await expectMaximizedResultGeometry(page, panel, testInfo)
    }
  } finally {
    await removeCanvas(page, canvasId)
  }
})
