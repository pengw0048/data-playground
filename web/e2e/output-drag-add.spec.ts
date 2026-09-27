import { expect, test, type Locator, type Page } from '@playwright/test'

async function box(locator: Locator) {
  const bounds = await locator.boundingBox()
  if (!bounds) throw new Error('Visible drag target has no bounds')
  return bounds
}

async function dragTo(page: Page, origin: Locator, target: { x: number; y: number }) {
  const bounds = await box(origin)
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2)
  await page.mouse.down()
  await page.mouse.move(target.x, target.y, { steps: 12 })
  await page.mouse.up()
}

test('an output drag onto blank Canvas adds a compatible step at the drop and undoes atomically', async ({ page }, testInfo) => {
  const canvasId = `output-drag-add-${Date.now()}`
  const created = await page.request.post('/api/canvas', { data: {
    id: canvasId, name: 'Drag to add a connected step', version: 1, requirements: [],
    nodes: [{ id: 'source', type: 'source', position: { x: 100, y: 180 },
      data: { title: 'Input data', status: 'draft', config: {} } }],
    edges: [],
  } })
  expect(created.ok(), await created.text()).toBeTruthy()
  try {
    await page.goto(`/#/canvas/${canvasId}`)
    const source = page.locator('.react-flow__node[data-id="source"]')
    const output = source.getByRole('button', { name: 'Add operation from dataset output' })
    await expect(output).toBeVisible()
    const finder = page.getByRole('dialog', { name: 'Connect to an operation' })

    // The existing plain-click path remains a single picker and can still be dismissed.
    await output.click()
    await expect(finder).toHaveCount(1)
    await page.keyboard.press('Escape')
    await expect(finder).toHaveCount(0)

    const canvas = await box(page.locator('.react-flow'))
    const sourceBox = await box(source)
    const drop = { x: Math.min(canvas.x + canvas.width - 90, sourceBox.x + sourceBox.width + 240),
      y: Math.min(canvas.y + canvas.height - 200, sourceBox.y + 140) }
    expect(await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.classList.contains('react-flow__pane'), drop)).toBe(true)
    const expectedPosition = await page.locator('.react-flow__viewport').evaluate((element, point) => {
      const transform = new DOMMatrix(getComputedStyle(element).transform)
      const surface = element.closest('.react-flow')!.getBoundingClientRect()
      return { x: (point.x - surface.left - transform.e) / transform.a,
        y: (point.y - surface.top - transform.f) / transform.d - 40 }
    }, drop)
    await dragTo(page, output, drop)
    await expect(finder).toHaveCount(1)
    const search = finder.getByRole('textbox', { name: 'Search operations' })
    await expect(finder.getByRole('option', { name: /^source(?:\s|$)/i })).toHaveCount(0)
    await search.fill('filter')
    await finder.getByRole('option', { name: /^filter/i }).click()
    await expect(finder).toHaveCount(0)
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await expect(page.locator('.react-flow__edge')).toHaveCount(1)
    await expect.poll(async () => {
      const graph = await (await page.request.get(`/api/canvas/${canvasId}`)).json()
      const filter = graph.nodes.find((node: { type: string }) => node.type === 'filter')
      if (!filter) return null
      return { source: graph.edges[0]?.source, target: graph.edges[0]?.target,
        filterId: filter.id, manualPosition: filter.data.autoPlaced === false,
        nearDrop: Math.abs(filter.position.x - expectedPosition.x) < 2 && Math.abs(filter.position.y - expectedPosition.y) < 2 }
    }).toMatchObject({ source: 'source', target: expect.any(String), filterId: expect.any(String), manualPosition: true, nearDrop: true })
    const connected = await (await page.request.get(`/api/canvas/${canvasId}`)).json()
    expect(connected.edges[0].target).toBe(connected.nodes.find((node: { type: string }) => node.type === 'filter').id)
    await testInfo.attach('connected-filter-at-drop', { body: await page.screenshot(), contentType: 'image/png' })

    await page.getByRole('button', { name: 'Undo', exact: true }).click()
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await expect(page.locator('.react-flow__edge')).toHaveCount(0)
    await expect.poll(async () => {
      const graph = await (await page.request.get(`/api/canvas/${canvasId}`)).json()
      return { nodes: graph.nodes.map((node: { id: string }) => node.id), edges: graph.edges }
    }).toEqual({ nodes: ['source'], edges: [] })
  } finally {
    if (!page.isClosed()) await page.goto('about:blank')
    expect((await page.request.delete(`/api/canvas/${canvasId}`)).ok()).toBeTruthy()
  }
})
