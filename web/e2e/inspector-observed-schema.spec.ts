import { expect, test } from '@playwright/test'

test('Inspector shows current observed columns for a dynamic Transform output', async ({ page }) => {
  const canvasId = `inspector-observed-schema-${Date.now()}`
  const graph = {
    id: canvasId, name: 'Observed Transform schema', version: 1, requirements: [],
    nodes: [
      {
        id: 'source', type: 'source', position: { x: 80, y: 160 },
        data: { title: 'Starter events', status: 'idle', config: { uri: 'events' } },
      },
      {
        id: 'transform', type: 'transform', position: { x: 420, y: 160 },
        data: { title: 'Add observed field', status: 'idle', config: {
          source: 'adhoc', mode: 'map',
          code: "def fn(row):\n    return {**row, 'observed_field': True}",
          onError: 'raise',
        } },
      },
    ],
    edges: [{
      id: 'source-transform', source: 'source', target: 'transform',
      sourceHandle: 'out', targetHandle: 'in', data: { wire: 'dataset' },
    }],
  }
  const created = await page.request.post('/api/canvas', { data: graph })
  expect(created.ok(), await created.text()).toBe(true)
  try {
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=transform`)
    const inspector = page.getByTestId('inspector')
    await expect(inspector.getByText('TRANSFORM', { exact: true })).toBeVisible()
    const outPort = inspector.getByText('OUT', { exact: true }).locator('..')
    await expect(outPort.getByRole('button', { name: 'untyped' })).toBeVisible()

    await inspector.getByRole('button', { name: 'View data' }).click()
    const observedOut = outPort.getByRole('button', { name: /\d+ cols/ })
    await expect(observedOut).toBeVisible({ timeout: 15_000 })
    await observedOut.click()
    await expect(inspector.getByText('observed_field', { exact: true })).toBeVisible()
  } finally {
    expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBe(true)
  }
})
