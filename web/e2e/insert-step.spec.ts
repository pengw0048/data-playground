import { expect, test, type APIResponse, type Locator } from '@playwright/test'
import type { CatalogTable, SampleResult } from '../src/types/api'
import type { CanvasDoc } from '../src/types/graph'

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()}: ${await response.text()}`).toBe(true)
  return response.json() as Promise<T>
}

async function midpoint(edge: Locator) {
  return edge.locator('.react-flow__edge-path').evaluate((element) => {
    const path = element as SVGPathElement
    const point = path.getPointAtLength(path.getTotalLength() / 2)
    const screen = new DOMPoint(point.x, point.y).matrixTransform(path.getScreenCTM()!)
    return { x: screen.x, y: screen.y }
  })
}

test('inserts a compatible Select into a connection, undoes once, and runs the reopened workflow', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const canvasId = `insert-step-${Date.now()}-${testInfo.workerIndex}`
  let dataset: CatalogTable | undefined
  let created = false
  const executions: string[] = []
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && ['/api/run', '/api/run/preview', '/api/run/write-admission'].includes(path)) executions.push(path)
  })
  const saved = () => page.request.get(`/api/canvas/${canvasId}`).then(json<CanvasDoc>)
  try {
    dataset = await json<CatalogTable>(await page.request.post('/api/catalog/upload', {
      headers: { 'X-Upload-Filename': `${canvasId}.csv`, 'Content-Type': 'text/csv' },
      data: 'category,score,unused\nA,5,first\nA,20,second\nA,30,third\nB,15,fourth\n',
    }))
    const initial: CanvasDoc = {
      id: canvasId, name: 'Insert into an existing workflow', version: 1,
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 180 }, data: { title: 'Rows', status: 'draft', config: {
          uri: dataset.uri, tableId: dataset.id, registrationId: dataset.registrationId,
        } } },
        { id: 'filter', type: 'filter', position: { x: 560, y: 180 }, data: { title: 'Scores above ten', status: 'draft', config: { predicate: 'score > 10' } } },
        { id: 'aggregate', type: 'aggregate', position: { x: 1160, y: 180 }, data: { title: 'Counts', status: 'draft', config: { groupBy: 'category', aggs: 'count(*) AS row_count' } } },
      ],
      edges: [
        { id: 'source-filter', source: 'source', sourceHandle: 'out', target: 'filter', targetHandle: 'in', data: { wire: 'dataset' } },
        { id: 'filter-aggregate', source: 'filter', sourceHandle: 'out', target: 'aggregate', targetHandle: 'in', data: { wire: 'dataset' } },
      ],
    }
    await json(await page.request.post('/api/canvas', { data: initial }))
    created = true
    await page.goto(`/#/canvas/${canvasId}`)
    const oldEdge = page.locator('.react-flow__edge[data-id="filter-aggregate"]')
    await expect(oldEdge).toBeVisible()
    const originalPoint = await midpoint(oldEdge)
    await page.mouse.dblclick(originalPoint.x, originalPoint.y)
    await expect(page.locator('.react-flow__edge')).toHaveCount(1)
    await page.getByRole('button', { name: 'Undo', exact: true }).click()
    await expect(oldEdge).toBeVisible()
    const point = await midpoint(oldEdge)
    await page.mouse.click(point.x, point.y, { button: 'right' })
    await page.getByRole('menuitem', { name: 'Insert step', exact: true }).click()
    const finder = page.getByRole('dialog', { name: 'Insert step', exact: true })
    const search = finder.getByRole('textbox', { name: 'Search operations' })
    await expect(search).toBeFocused()
    for (const kind of ['source', 'join', 'metric', 'sample']) {
      await search.fill(kind)
      // Search also matches compatible port types (for example steps accepting a sample).
      // The excluded operation itself must stay absent, regardless of those metadata matches.
      await expect(finder.getByRole('option').filter({
        has: page.getByText(new RegExp(`^${kind}$`, 'i')),
      })).toHaveCount(0)
    }
    await search.fill('select')
    await search.press('Enter')
    await expect(finder).toHaveCount(0)
    await expect(page.locator('.react-flow__node')).toHaveCount(4)
    await expect.poll(async () => (await saved()).nodes.length).toBe(4)
    const inserted = await saved()
    const select = inserted.nodes.find((node) => node.type === 'select')!
    expect(select).toBeTruthy()
    expect(inserted.edges).toEqual([
      initial.edges[0],
      expect.objectContaining({ source: 'filter', sourceHandle: 'out', target: select.id, targetHandle: 'in' }),
      expect.objectContaining({ source: select.id, sourceHandle: 'out', target: 'aggregate', targetHandle: 'in' }),
    ])
    expect(inserted.nodes.slice(0, 3).map((node) => node.position)).toEqual(initial.nodes.map((node) => node.position))
    expect(executions).toEqual([])
    await testInfo.attach('inserted-select-connections', { body: await page.screenshot(), contentType: 'image/png' })
    await page.getByRole('button', { name: 'Undo', exact: true }).click()
    await expect(page.locator('.react-flow__node')).toHaveCount(3)
    await expect.poll(async () => (await saved()).edges).toEqual(initial.edges)
    await page.getByRole('button', { name: 'Redo', exact: true }).click()
    await expect(page.locator('.react-flow__node')).toHaveCount(4)
    const selectCard = page.locator(`.react-flow__node[data-id="${select.id}"]`)
    await selectCard.getByPlaceholder('id, lower(name) AS name, a*b AS area', { exact: true }).fill('category, score')
    await expect.poll(async () => (await saved()).nodes.find((node) => node.id === select.id)?.data.config.select).toBe('category, score')
    await page.reload()
    await expect(selectCard.getByPlaceholder('id, lower(name) AS name, a*b AS area', { exact: true })).toHaveValue('category, score')
    await expect(page.locator('.react-flow__edge')).toHaveCount(3)
    expect(executions).toEqual([])

    const aggregate = page.locator('.react-flow__node[data-id="aggregate"]')
    await aggregate.getByText('Counts', { exact: true }).click()
    const started = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/run'
      && response.request().method() === 'POST' && response.ok())
    await page.getByTestId('inspector').getByRole('button', { name: 'Run', exact: true }).click()
    const runPanel = page.getByTestId('panel-run')
    const confirmation = runPanel.getByRole('button', { name: /^(?:Run with unknown row count|Run [\d,]+ rows)$/ })
    await expect.poll(async () => await confirmation.isVisible()
      || await runPanel.getByText('DONE', { exact: true }).isVisible()
      || await runPanel.getByRole('button', { name: 'Stop', exact: true }).isVisible()).toBe(true)
    if (await confirmation.isVisible()) await confirmation.click()
    const { runId } = await json<{ runId: string }>(await started)
    await expect(runPanel.getByText('DONE', { exact: true })).toBeVisible({ timeout: 30_000 })
    await runPanel.getByRole('button', { name: 'Close', exact: true }).click()
    await page.getByRole('button', { name: 'Runs & results', exact: true }).click()
    const results = page.getByRole('complementary', { name: 'Canvas runs and results' })
    const sampled = page.waitForResponse((response) => response.url().endsWith(`/api/run/${encodeURIComponent(runId)}/sample`)
      && response.request().method() === 'POST')
    await results.getByRole('button', { name: 'Open full result', exact: true }).click()
    const result = await json<SampleResult>(await sampled)
    expect(Object.fromEntries(result.rows.map((row) => [String(row.category), Number(row.row_count)]))).toEqual({ A: 2, B: 1 })
    await expect(results.locator('tbody tr')).toHaveCount(2)
    await testInfo.attach('inserted-step-result', { body: await page.screenshot(), contentType: 'image/png' })
  } finally {
    await page.goto('about:blank')
    if (created) await json(await page.request.delete(`/api/canvas/${canvasId}`))
    if (dataset) {
      const current = await json<CatalogTable>(await page.request.get(`/api/catalog/tables/${encodeURIComponent(dataset.id)}`))
      await json(await page.request.delete(`/api/catalog/tables/${encodeURIComponent(dataset.id)}`, {
        params: { expected_registration_id: current.registrationId!, expected_revision: current.metadataRevision! },
      }))
    }
  }
})
