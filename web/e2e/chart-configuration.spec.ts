import { expect, test } from '@playwright/test'

test('Chart starts from schema defaults and keeps SQL expressions explicit', async ({ page }) => {
  const canvasId = `chart-configuration-${Date.now()}`
  try {
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Chart configuration', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 160 }, data: {
          title: 'Events', status: 'draft', config: { uri: 'events' }, history: [],
        } },
        { id: 'chart', type: 'chart', position: { x: 390, y: 160 }, data: {
          title: 'Events chart', status: 'draft', config: { chartType: 'bar', agg: 'count' }, history: [],
        } },
      ],
      edges: [{ id: 'source-chart', source: 'source', target: 'chart', data: { wire: 'dataset' } }],
    } })
    expect(created.ok(), await created.text()).toBe(true)

    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    const chart = page.locator('.react-flow__node-chart')
    await expect(chart).toBeVisible()

    const xColumn = chart.getByRole('combobox', { name: 'X column' })
    await expect(xColumn).toHaveValue('event', { timeout: 15_000 })
    await expect(chart.getByLabel('Summary')).toHaveValue('count')
    await expect(chart.getByRole('textbox')).toHaveCount(0)

    await xColumn.selectOption('user_id')
    await expect(xColumn).toHaveValue('user_id')
    await xColumn.selectOption('event')

    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
      const graph = await response.json() as { nodes: Array<{ id: string; data: { config: Record<string, unknown> } }> }
      return graph.nodes.find((node) => node.id === 'chart')?.data.config.x
    }).toBe('event')

    await chart.getByText('Events chart', { exact: true }).click()
    await chart.getByRole('button', { name: 'Run up to here' }).click()
    await page.getByRole('button', { name: 'Run with unknown row count', exact: true }).click()
    await expect(chart).toContainText('4 rows', { timeout: 15_000 })
    await page.getByRole('button', { name: 'Close' }).click()
    await chart.getByRole('button', { name: 'View chart result' }).click()
    await expect(page.getByRole('img', { name: 'bar chart, saved result' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Preview sample' })).toHaveCount(0)
    await expect(page.getByTestId('panel-data').getByTitle('Refresh')).toHaveCount(0)
    await expect(chart.locator('[title="latest"]')).toBeVisible()
    await page.getByRole('button', { name: 'Close' }).click()

    await chart.getByLabel('Summary').selectOption('sum')
    await expect(chart.getByRole('combobox', { name: 'Y column' })).toHaveValue('amount')

    await chart.getByRole('button', { name: 'Use SQL expression for X' }).click()
    const xExpression = chart.getByRole('textbox', { name: 'X SQL expression' })
    await expect(xExpression).toHaveValue('event')
    await xExpression.fill('upper(event)')
    await chart.getByRole('button', { name: 'Use SQL expression for Y' }).click()
    await chart.getByRole('textbox', { name: 'Y SQL expression' }).fill('amount * 2')

    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
      const graph = await response.json() as { nodes: Array<{ id: string; data: { config: Record<string, unknown> } }> }
      const config = graph.nodes.find((node) => node.id === 'chart')?.data.config
      return [config?.xMode, config?.x, config?.yMode, config?.y]
    }).toEqual(['expression', 'upper(event)', 'expression', 'amount * 2'])

    await chart.getByRole('button', { name: 'Choose X from input columns' }).click()
    await expect(chart.getByRole('combobox', { name: 'X column' })).toHaveValue('event')
  } finally {
    await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
  }
})
