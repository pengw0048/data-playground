import { expect, test } from '@playwright/test'

test('keeps execution placement concise while warnings stay visible', async ({ page }) => {
  const canvasId = `execution-plan-hierarchy-${Date.now()}`
  const created = await page.request.post('/api/canvas', { data: {
    id: canvasId, name: 'Execution plan hierarchy', version: 1, requirements: [], edges: [],
    nodes: [{
      id: 'transform', type: 'transform', position: { x: 180, y: 160 },
      data: {
        title: 'Transform', status: 'draft', history: [],
        config: { mode: 'map', source: 'adhoc', code: 'def fn(row):\n    return row' },
      },
    }],
  } })
  expect(created.ok(), await created.text()).toBeTruthy()

  await page.route('**/api/graph/plan', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      regions: [
        {
          id: 'source-9342868352a9', outputNode: 'source-9342868352a9',
          backend: 'default', tier: 'object', rows: 2_000, confidence: 'exact',
          preflight: ['Pinned source revision is unavailable.'],
        },
        {
          id: 'join-5-33741', outputNode: 'join-5-33741',
          backend: 'ray-data', tier: null, rows: 500, confidence: 'bounded', requires: '8GB',
        },
      ],
    }) })
  })

  try {
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=transform`)
    const inspector = page.getByTestId('inspector')
    await expect(inspector.getByTestId('run-plan-summary'))
      .toHaveText('2 execution groups · local + Ray')
    await expect(inspector.getByRole('alert'))
      .toHaveText('Pinned source revision is unavailable.')

    const details = inspector.getByTestId('run-plan-details')
    await expect(details).not.toHaveAttribute('open', '')
    await expect(details.getByText('source-9342868352a9')).toBeHidden()
    await expect(inspector.getByText(/handing off via a tier/i)).toHaveCount(0)

    await details.getByText('Run plan', { exact: true }).click()
    await expect(details).toHaveAttribute('open', '')
    await expect(details.getByText('source-9342868352a9')).toBeVisible()
    await expect(details.getByText('join-5-33741')).toBeVisible()
    await expect(details.getByText('ray-data')).toBeVisible()
    await expect(details.getByTitle('storage used between execution regions')).toHaveText('→ object')
    await expect(details.getByTitle('declared resource requirement')).toHaveText('needs 8GB')
  } finally {
    await page.unroute('**/api/graph/plan')
    expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBeTruthy()
  }
})
