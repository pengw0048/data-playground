import { expect, test } from '@playwright/test'

test('a Sort after an Aggregate explains the target Sort preview semantics', async ({ page }) => {
  const canvasId = `sort-preview-reason-${Date.now()}`
  const graph = {
    id: canvasId,
    name: 'Sort preview reason',
    version: 1,
    requirements: [],
    nodes: [
      {
        id: 'source', type: 'source', position: { x: 80, y: 160 },
        data: {
          title: 'Purchases', status: 'draft', config: { uri: 'events' }, history: [],
        },
      },
      {
        id: 'aggregate', type: 'aggregate', position: { x: 390, y: 160 },
        data: {
          title: 'Purchases per user', status: 'draft',
          config: {
            groupBy: 'user_id',
            aggs: 'sum(amount) AS total_purchase_amount',
          },
          history: [],
        },
      },
      {
        id: 'sort', type: 'sort', position: { x: 700, y: 160 },
        data: {
          title: 'Order purchases', status: 'draft',
          config: { by: 'total_purchase_amount DESC' }, history: [],
        },
      },
    ],
    edges: [
      {
        id: 'source-aggregate', source: 'source', target: 'aggregate',
        data: { wire: 'dataset' },
      },
      {
        id: 'aggregate-sort', source: 'aggregate', target: 'sort',
        data: { wire: 'dataset' },
      },
    ],
  }

  try {
    const created = await page.request.post('/api/canvas', { data: graph })
    expect(created.ok(), await created.text()).toBe(true)

    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    const sort = page.locator('.react-flow__node', { hasText: 'Order purchases' })
    await expect(sort).toBeVisible()
    await sort.click()
    await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()

    const panel = page.getByTestId('panel-data')
    await expect(panel).toBeVisible()
    await expect(panel.getByText(
      'Sorting needs all input rows to determine the final order. Run this step to see the result.',
    )).toBeVisible()
    await expect(panel.getByText(/Grouped aggregation needs all input rows/)).toHaveCount(0)
  } finally {
    await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
  }
})
