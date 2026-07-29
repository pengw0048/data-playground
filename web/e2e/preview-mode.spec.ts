import { expect, test } from '@playwright/test'
import { goldenCanvas, installCanvas } from './support/ux-fixtures'

test('keeps a retried Preview sample separate from a later Full result', async ({ page }) => {
  const doc = goldenCanvas(`preview-mode-${Date.now()}`, 'Preview mode', 'Preview mode source')
  doc.nodes = doc.nodes.map((node) => node.id === 'filter'
    ? { ...node, data: { ...node.data, status: 'stale' } }
    : node)
  await installCanvas(page.request, doc)

  let previewRequests = 0
  let fullRunRequests = 0
  await page.route('**/api/run/preview', async (route) => {
    previewRequests += 1
    if (previewRequests === 1) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          columns: [], rows: [], rowCount: null, hasMore: false, truncated: false,
          completeness: 'unknown', notPreviewable: true, suggestedAction: 'run',
          reason: 'Retry the bounded preview.', wire: 'dataset',
        }),
      })
      return
    }
    await route.continue()
  })
  await page.route('**/api/run', async (route) => {
    if (route.request().method() === 'POST') fullRunRequests += 1
    await route.continue()
  })

  try {
    await page.goto(`/#/canvas/${doc.id}`)
    const filter = page.locator('.react-flow__node[data-id="filter"]')
    await filter.click()
    const inspector = page.getByTestId('inspector')
    await inspector.getByRole('button', { name: 'View data' }).click()
    const panel = page.getByTestId('panel-data')
    await expect(panel.getByText('Run this step to see results')).toBeVisible()

    await panel.getByRole('button', { name: 'Run this step' }).click()
    await expect(panel.getByText('rows 1–50', { exact: true })).toBeVisible()
    expect(previewRequests).toBe(2)
    expect(fullRunRequests).toBe(0)
    await expect(panel.getByRole('button', { name: 'Preview sample' })).toHaveCount(0)

    const runResponse = page.waitForResponse((response) => (
      response.url().endsWith('/api/run') && response.request().method() === 'POST'
    ))
    await inspector.getByRole('button', { name: 'Run', exact: true }).click()
    await runResponse

    const fullResult = panel.getByRole('button', { name: 'Full result', exact: true })
    await expect(fullResult).toBeVisible({ timeout: 30_000 })
    await fullResult.click()
    await expect(panel.getByTestId('full-result-status')).toHaveText(/Complete · [\d,]+ rows/)

    await panel.getByRole('button', { name: 'Preview sample', exact: true }).click()
    await expect(panel.getByText('rows 1–50', { exact: true })).toBeVisible()
  } finally {
    // The isolated E2E workspace is discarded after the run. Do not let a collab-room teardown
    // delay the browser assertion result when best-effort fixture cleanup races that shutdown.
    void page.request.delete(`/api/canvas/${doc.id}`).catch(() => {})
  }
})
