import { expect, test } from '@playwright/test'

test('Chart starts from schema defaults and keeps SQL expressions explicit', async ({ page }) => {
  test.setTimeout(60_000)
  await page.setViewportSize({ width: 1440, height: 900 })
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
    const resultChart = page.getByRole('img', { name: 'bar chart, saved result' })
    await expect(resultChart).toBeVisible()
    const bars = await resultChart.locator('rect').evaluateAll((elements) => elements.map((element) => {
      const rect = element as SVGRectElement
      return { x: Number(rect.getAttribute('x')), width: Number(rect.getAttribute('width')) }
    }))
    expect(bars.length).toBeGreaterThan(0)
    expect(Math.min(...bars.map((bar) => bar.x))).toBeGreaterThanOrEqual(48)
    expect(Math.max(...bars.map((bar) => bar.x + bar.width))).toBeLessThanOrEqual(624)
    await expect(page.getByRole('button', { name: 'Preview sample' })).toHaveCount(0)
    await expect(page.getByTestId('panel-data').getByTitle('Refresh')).toHaveCount(0)
    await expect(chart.locator('[title="latest"]')).toBeVisible()
    await page.getByRole('button', { name: 'Close' }).click()

    const reopenRetainedPosts: string[] = []
    const reopenPreviewPosts: string[] = []
    await page.route('**/api/run**', async (route) => {
      const request = route.request()
      if (request.method() === 'POST') {
        const path = new URL(request.url()).pathname
        if (path === '/api/run/retained-result') reopenRetainedPosts.push(request.url())
        if (path === '/api/run/preview') reopenPreviewPosts.push(request.url())
      }
      await route.continue()
    })
    await page.reload()
    await expect(chart).toBeVisible()
    await expect(chart.locator('[title="latest"]')).toBeVisible({ timeout: 15_000 })
    await chart.getByRole('button', { name: 'View chart result' }).click()
    await expect(page.getByRole('img', { name: 'bar chart, saved result' })).toBeVisible()
    await expect.poll(() => reopenRetainedPosts.length).toBe(1)
    await page.waitForTimeout(1_200)
    expect(reopenRetainedPosts).toHaveLength(1)
    expect(reopenPreviewPosts).toEqual([])
    await page.getByRole('button', { name: 'Close' }).click()
    await expect(page.getByTestId('panel-data')).toHaveCount(0)
    await page.waitForTimeout(600)
    expect(reopenRetainedPosts).toHaveLength(1)
    expect(reopenPreviewPosts).toEqual([])
    await page.unroute('**/api/run**')

    const freshPage = await page.context().newPage()
    const freshRetainedPosts: string[] = []
    const freshPreviewPosts: string[] = []
    try {
      await freshPage.setViewportSize({ width: 1440, height: 900 })
      await freshPage.route('**/api/run**', async (route) => {
        const request = route.request()
        if (request.method() === 'POST') {
          const path = new URL(request.url()).pathname
          if (path === '/api/run/retained-result') freshRetainedPosts.push(request.url())
          if (path === '/api/run/preview') freshPreviewPosts.push(request.url())
        }
        await route.continue()
      })
      await freshPage.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
      const freshChart = freshPage.locator('.react-flow__node-chart')
      await expect(freshChart.locator('[title="latest"]')).toBeVisible({ timeout: 15_000 })
      await freshChart.getByText('Events chart', { exact: true }).click()
      await freshChart.getByRole('button', { name: 'View chart result' }).click()
      await expect(freshPage.getByRole('img', { name: 'bar chart, saved result' })).toBeVisible()
      await expect.poll(() => freshRetainedPosts.length).toBe(1)
      await freshPage.waitForTimeout(1_200)
      expect(freshRetainedPosts).toHaveLength(1)
      expect(freshPreviewPosts).toEqual([])
      await freshPage.getByRole('button', { name: 'Close' }).click()
      await expect(freshPage.getByTestId('panel-data')).toHaveCount(0)
      await freshPage.waitForTimeout(600)
      expect(freshRetainedPosts).toHaveLength(1)
      expect(freshPreviewPosts).toEqual([])
      await freshPage.close()
      expect(freshRetainedPosts).toHaveLength(1)
      expect(freshPreviewPosts).toEqual([])
    } finally {
      if (!freshPage.isClosed()) {
        await freshPage.unroute('**/api/run**')
        await freshPage.close()
      }
    }

    await chart.getByLabel('Summary').selectOption('sum')
    await expect(chart.getByRole('combobox', { name: 'Y column', exact: true })).toHaveValue('amount')

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

test('Chart presentation type redraws without invalidating or rerunning', async ({ page }) => {
  const canvasId = `chart-presentation-${Date.now()}`
  try {
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Chart presentation', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 160 }, data: {
          title: 'Events', status: 'draft', config: { uri: 'events' }, history: [],
        } },
        { id: 'chart', type: 'chart', position: { x: 390, y: 160 }, data: {
          title: 'Events chart', status: 'draft',
          config: { chartType: 'bar', agg: 'count', xMode: 'column', x: 'event' }, history: [],
        } },
      ],
      edges: [{ id: 'source-chart', source: 'source', target: 'chart', data: { wire: 'dataset' } }],
    } })
    expect(created.ok(), await created.text()).toBe(true)

    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    const chart = page.locator('.react-flow__node-chart')
    await expect(chart).toBeVisible()
    await expect(chart.getByRole('combobox', { name: 'X column' })).toHaveValue('event', { timeout: 15_000 })

    await chart.getByText('Events chart', { exact: true }).click()
    await chart.getByRole('button', { name: 'Run up to here' }).click()
    await page.getByRole('button', { name: 'Run with unknown row count', exact: true }).click()
    await expect(chart).toContainText('4 rows', { timeout: 15_000 })
    await page.getByRole('button', { name: 'Close' }).click()
    await chart.getByRole('button', { name: 'View chart result' }).click()
    await expect(page.getByRole('img', { name: 'bar chart, saved result' })).toBeVisible()
    await expect(chart.locator('[title="latest"]')).toBeVisible()
    await page.waitForTimeout(700) // settle metadata work attributable to opening the result

    const runPosts: string[] = []
    const previewPosts: string[] = []
    const planPosts: string[] = []
    const graphPosts: string[] = []
    await page.route('**/api/run**', async (route) => {
      const request = route.request()
      if (request.method() !== 'POST') {
        await route.continue()
        return
      }
      const url = request.url()
      const path = new URL(url).pathname
      if (path.includes('/api/run/preview')) previewPosts.push(url)
      else if (
        path.includes('/api/run/estimate')
        || path.includes('/api/run/write-admission')
        || path.includes('/api/run/retained-result')
        || path.includes('/api/execution-manifest')
      ) {
        planPosts.push(url)
      } else if (path === '/api/run' || path === '/api/run/') {
        runPosts.push(url)
      }
      await route.continue()
    })
    await page.route('**/api/graph/**', async (route) => {
      const request = route.request()
      if (request.method() === 'POST') {
        const path = new URL(request.url()).pathname
        if (['/api/graph/plan', '/api/graph/schema', '/api/graph/estimate'].includes(path)) {
          graphPosts.push(request.url())
        }
      }
      await route.continue()
    })

    for (const [value, label] of [
      ['line', 'line chart, saved result'],
      ['scatter', 'scatter chart, saved result'],
      ['area', 'area chart, saved result'],
      ['bar', 'bar chart, saved result'],
    ] as const) {
      await chart.getByLabel('Chart').selectOption(value)
      await expect(page.getByRole('img', { name: label })).toBeVisible()
      await expect(chart.locator('[title="latest"]')).toBeVisible()
      await expect(chart).toContainText('4 rows')
    }
    // A presentation edit must not enqueue delayed Inspector planning (350 ms) or schema/size
    // refresh (500 ms). Observe past both debounces before accepting the zero-request window.
    await page.waitForTimeout(700)

    expect(runPosts).toEqual([])
    expect(previewPosts).toEqual([])
    expect(planPosts).toEqual([])
    expect(graphPosts).toEqual([])

    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
      const graph = await response.json() as {
        nodes: Array<{ id: string; data: { status: string; config: Record<string, unknown> } }>
      }
      const node = graph.nodes.find((item) => item.id === 'chart')
      return [node?.data.status, node?.data.config.chartType]
    }).toEqual(['latest', 'bar'])
  } finally {
    await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
  }
})

test('Chart time buckets group a temporal X in UTC with readable chronological ticks', async ({ page }) => {
  const canvasId = `chart-time-bucket-${Date.now()}`
  try {
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Chart time bucket', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 160 }, data: {
          title: 'Events', status: 'draft', config: { uri: 'events' }, history: [],
        } },
        { id: 'stamped', type: 'sql', position: { x: 300, y: 160 }, data: {
          title: 'Stamped', status: 'draft', config: {
            // Hourly stamps from Jan 30 22:00 across the Jan→Feb month boundary.
            sql: "SELECT id, event, amount, TIMESTAMP '2024-01-30 22:00:00' + INTERVAL (id % 96) HOUR AS created_at FROM input",
          }, history: [],
        } },
        { id: 'chart', type: 'chart', position: { x: 560, y: 160 }, data: {
          title: 'Events over time', status: 'draft',
          config: { chartType: 'bar', agg: 'count', xMode: 'column', x: 'created_at' }, history: [],
        } },
      ],
      edges: [
        { id: 'source-stamped', source: 'source', target: 'stamped', data: { wire: 'dataset' } },
        { id: 'stamped-chart', source: 'stamped', target: 'chart', data: { wire: 'dataset' } },
      ],
    } })
    expect(created.ok(), await created.text()).toBe(true)

    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    const chart = page.locator('.react-flow__node-chart')
    await expect(chart).toBeVisible()
    await expect(chart.getByRole('combobox', { name: 'X column' })).toHaveValue('created_at', { timeout: 15_000 })

    // A typed timestamp X offers the bucket selector; an expression X hides it.
    const bucket = chart.getByRole('combobox', { name: 'X time bucket' })
    await expect(bucket).toBeVisible()
    await expect(bucket).toHaveValue('none')
    await chart.getByRole('button', { name: 'Use SQL expression for X' }).click()
    await expect(chart.getByRole('combobox', { name: 'X time bucket' })).toHaveCount(0)
    await chart.getByRole('button', { name: 'Choose X from input columns' }).click()
    await bucket.selectOption('day')
    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
      const graph = await response.json() as { nodes: Array<{ id: string; data: { config: Record<string, unknown> } }> }
      return graph.nodes.find((node) => node.id === 'chart')?.data.config.timeBucket
    }).toBe('day')

    await chart.getByText('Events over time', { exact: true }).click()
    await chart.getByRole('button', { name: 'Run up to here' }).click()
    await page.getByRole('button', { name: 'Run with unknown row count', exact: true }).click()
    await expect(chart.locator('[title="latest"]')).toBeVisible({ timeout: 15_000 })
    await page.getByRole('button', { name: 'Close' }).click()
    await chart.getByRole('button', { name: 'View chart result' }).click()
    const resultChart = page.getByRole('img', { name: 'bar chart, saved result' })
    await expect(resultChart).toBeVisible()
    // Five UTC day groups (Jan 30 … Feb 3) on a labeled chronological axis.
    await expect(resultChart.locator('rect')).toHaveCount(5)
    await expect(resultChart.getByText('created_at · by day (UTC)', { exact: true })).toBeVisible()
    await expect(resultChart.getByText('Jan 30', { exact: true })).toBeVisible()
    await expect(resultChart.getByText('Feb 3', { exact: true })).toBeVisible()

    // Presentation-only chart type reuses the same bucketed result without a new run.
    const runPosts: string[] = []
    await page.route('**/api/run', async (route) => {
      if (route.request().method() === 'POST') runPosts.push(route.request().url())
      await route.continue()
    })
    await chart.getByLabel('Chart').selectOption('line')
    await expect(page.getByRole('img', { name: 'line chart, saved result' })).toBeVisible()
    await expect(chart.locator('[title="latest"]')).toBeVisible()
    expect(runPosts).toEqual([])
    await page.unroute('**/api/run')
    await page.getByRole('button', { name: 'Close' }).click()

    // Changing the bucket is semantic: the Chart invalidates and recomputes to month groups.
    await bucket.selectOption('month')
    await expect(chart.locator('[title="stale"]')).toBeVisible()
    await chart.getByText('Events over time', { exact: true }).click()
    await chart.getByRole('button', { name: 'Run up to here' }).click()
    await page.getByRole('button', { name: 'Run with unknown row count', exact: true }).click()
    await expect(chart.locator('[title="latest"]')).toBeVisible({ timeout: 15_000 })
    await page.getByRole('button', { name: 'Close' }).click()
    await chart.getByRole('button', { name: 'View chart result' }).click()
    const monthly = page.getByRole('img', { name: 'line chart, saved result' })
    await expect(monthly).toBeVisible()
    await expect(monthly.getByText('created_at · by month (UTC)', { exact: true })).toBeVisible()
    await expect(monthly.getByText('Jan 2024', { exact: true })).toBeVisible()
    await expect(monthly.getByText('Feb 2024', { exact: true })).toBeVisible()
  } finally {
    await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
  }
})

test('Chart Series / Color by aggregates, legends, and keeps chartType presentation-only', async ({ page }) => {
  const canvasId = `chart-series-${Date.now()}`
  try {
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Chart series', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 160 }, data: {
          title: 'Events', status: 'draft', config: { uri: 'events' }, history: [],
        } },
        { id: 'chart', type: 'chart', position: { x: 390, y: 160 }, data: {
          title: 'Events chart', status: 'draft',
          config: { chartType: 'bar', agg: 'count', xMode: 'column', x: 'user_id' }, history: [],
        } },
      ],
      edges: [{ id: 'source-chart', source: 'source', target: 'chart', data: { wire: 'dataset' } }],
    } })
    expect(created.ok(), await created.text()).toBe(true)

    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    const chart = page.locator('.react-flow__node-chart')
    await expect(chart).toBeVisible()
    await expect(chart.getByRole('combobox', { name: 'X column' })).toHaveValue('user_id', { timeout: 15_000 })

    await chart.getByText('Events chart', { exact: true }).click()
    await chart.getByRole('button', { name: 'Run up to here' }).click()
    await page.getByRole('button', { name: 'Run with unknown row count', exact: true }).click()
    await expect(chart.locator('[title="latest"]')).toBeVisible({ timeout: 15_000 })
    await page.getByRole('button', { name: 'Close' }).click()

    await chart.getByLabel('Series / Color by column').selectOption('event')
    await expect(chart.locator('[title="stale"]')).toBeVisible()
    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
      const graph = await response.json() as {
        nodes: Array<{ id: string; data: { status: string; config: Record<string, unknown> } }>
      }
      const node = graph.nodes.find((item) => item.id === 'chart')
      return [node?.data.config.series, node?.data.status]
    }).toEqual(['event', 'stale'])

    await chart.getByText('Events chart', { exact: true }).click()
    await chart.getByRole('button', { name: 'Run up to here' }).click()
    await page.getByRole('button', { name: 'Run with unknown row count', exact: true }).click()
    await expect(chart.locator('[title="latest"]')).toBeVisible({ timeout: 15_000 })
    await page.getByRole('button', { name: 'Close' }).click()
    await chart.getByRole('button', { name: 'View chart result' }).click()
    await expect(page.getByRole('img', { name: /bar chart, saved result.*series/i })).toBeVisible()
    await expect(page.getByRole('list', { name: 'Chart series legend' })).toBeVisible()
    await expect(page.getByRole('list', { name: 'Chart series legend' }).getByText('view')).toBeVisible()

    const runPosts: string[] = []
    await page.route('**/api/run', async (route) => {
      if (route.request().method() === 'POST') runPosts.push(route.request().url())
      await route.continue()
    })
    await chart.getByLabel('Chart').selectOption('line')
    await expect(page.getByRole('img', { name: /line chart, saved result.*series/i })).toBeVisible()
    await expect(chart.locator('[title="latest"]')).toBeVisible()
    expect(runPosts).toEqual([])

    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
      const graph = await response.json() as {
        nodes: Array<{ id: string; data: { config: Record<string, unknown> } }>
      }
      return graph.nodes.find((item) => item.id === 'chart')?.data.config.chartType
    }).toBe('line')
    const historyResponse = await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}/runs`)
    const history = await historyResponse.json() as Array<{ runId?: string; outputs: Array<{ nodeId: string; portId: string }> }>
    const chartRun = history.find((item) => item.runId && item.outputs.some((output) => output.nodeId === 'chart'))
    expect(chartRun?.runId).toBeTruthy()
    await page.goto(`/#/jobs?run=${encodeURIComponent(chartRun!.runId!)}&output=${encodeURIComponent('chart:out')}`)
    await expect(page.getByRole('complementary', { name: 'Saved result' })).toBeVisible()
    await expect(page.getByRole('img', { name: /line chart, saved result.*series/i })).toBeVisible()
    await expect(page.getByRole('list', { name: 'Chart series legend' })).toBeVisible()
  } finally {
    await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
  }
})
