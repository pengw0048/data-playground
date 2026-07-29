import { expect, test } from '@playwright/test'

type CatalogTable = { id: string; name: string; uri: string; registrationId: string }

async function eventsTable(page: import('@playwright/test').Page): Promise<CatalogTable> {
  const response = await page.request.get('/api/catalog/search', {
    params: { q: 'events', mode: 'lexical', limit: 10 },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  const table = (await response.json() as CatalogTable[]).find((item) => item.name === 'events')
  if (!table) throw new Error('Missing seeded events catalog table')
  return table
}

test('a current retained Transform result types Join keys across reload and invalidates after an edit', async ({ page }) => {
  test.setTimeout(45_000)
  const events = await eventsTable(page)
  const canvasId = `retained-transform-schema-${Date.now()}`
  const graph = {
    id: canvasId, name: 'Retained Transform schema', version: 1, requirements: [],
    nodes: [
      { id: 'left', type: 'source', position: { x: 60, y: 80 }, data: {
        title: 'Left events', status: 'draft', config: {
          uri: events.uri, tableId: events.id, registrationId: events.registrationId,
        },
      } },
      { id: 'transform', type: 'transform', position: { x: 310, y: 80 }, data: {
        title: 'Derived amount', status: 'draft', config: {
          source: 'adhoc', mode: 'map', onError: 'raise',
          code: "def fn(row):\n    return {**row, 'amount_doubled': row['amount'] * 2}",
        },
      } },
      { id: 'right', type: 'source', position: { x: 310, y: 290 }, data: {
        title: 'Right events', status: 'draft', config: {
          uri: events.uri, tableId: events.id, registrationId: events.registrationId,
        },
      } },
      { id: 'join', type: 'join', position: { x: 590, y: 150 }, data: {
        title: 'Compare amounts', status: 'draft', config: { how: 'inner', on: '' },
      } },
    ],
    edges: [
      { id: 'left-transform', source: 'left', sourceHandle: 'out', target: 'transform', targetHandle: 'in', data: { wire: 'dataset' } },
      { id: 'transform-join', source: 'transform', sourceHandle: 'out', target: 'join', targetHandle: 'a', data: { wire: 'dataset' } },
      { id: 'right-join', source: 'right', sourceHandle: 'out', target: 'join', targetHandle: 'b', data: { wire: 'dataset' } },
    ],
  }
  const created = await page.request.post('/api/canvas', { data: graph })
  expect(created.ok(), await created.text()).toBeTruthy()
  try {
    const started = await page.request.post('/api/run', { data: {
      graph, targetNodeId: 'transform', confirmed: true, submissionId: crypto.randomUUID(),
    } })
    expect(started.ok(), await started.text()).toBeTruthy()
    const { runId } = await started.json() as { runId: string }
    await expect.poll(async () => (await (await page.request.get(`/api/run/${runId}`)).json()).status,
      { timeout: 20_000 }).toBe('done')

    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    await expect(page.locator('.react-flow__node')).toHaveCount(4)
    const leftKey = page.getByLabel('Left key 1')
    const rightKey = page.getByLabel('Right key 1')
    await expect(leftKey.getByRole('option', { name: 'amount_doubled' })).toHaveCount(1)
    await leftKey.selectOption('amount_doubled')
    await rightKey.selectOption('amount')

    await expect.poll(async () => {
      const canvas = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      return canvas.nodes.find((node: { id: string }) => node.id === 'join').data.config.condition
    }).toBe('a.amount_doubled = b.amount')

    await page.reload()
    await expect(leftKey).toHaveValue('amount_doubled')
    await expect(rightKey).toHaveValue('amount')
    await expect(leftKey.getByRole('option', { name: 'amount_doubled' })).toHaveCount(1)

    const saved = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
    const edited = structuredClone(saved)
    edited.nodes.find((node: { id: string }) => node.id === 'transform').data.config.code = (
      "def fn(row):\n    return {**row, 'amount_tripled': row['amount'] * 3}")
    const updated = await page.request.put(
      `/api/canvas/${encodeURIComponent(canvasId)}?expectedVersion=${saved.version}`, { data: edited })
    expect(updated.ok(), await updated.text()).toBeTruthy()

    await page.reload()
    await expect(leftKey.getByRole('option', { name: 'amount_doubled' })).toHaveCount(0)
    await expect(leftKey.getByRole('option', { name: 'amount_doubled (schema unavailable)' })).toHaveCount(1)
  } finally {
    expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBeTruthy()
  }
})
