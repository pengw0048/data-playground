import { expect, test } from '@playwright/test'

test('a structured Filter condition must be corrected before it can run', async ({ page }) => {
  const canvasId = `filter-validation-${Date.now()}`
  try {
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Filter validation', version: 1,
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 160 }, data: {
          title: 'Events', status: 'draft', config: { uri: 'events' },
        } },
        { id: 'filter', type: 'filter', position: { x: 380, y: 160 }, data: {
          title: 'Filter events', status: 'draft', config: { predicate: '' },
        } },
      ],
      edges: [{ id: 'source-filter', source: 'source', target: 'filter', data: { wire: 'dataset' } }],
    } })
    expect(created.ok()).toBe(true)

    await page.goto(`/#/canvas/${canvasId}`)
    const filter = page.locator('.react-flow__node-filter')
    await expect(filter).toBeVisible()
    await filter.click()
    const inspector = page.getByTestId('inspector')
    await expect(inspector.getByRole('button', { name: 'Run', exact: true })).toBeVisible()

    await filter.getByText('add condition', { exact: true }).click()
    const value = filter.getByPlaceholder('value')
    await expect(value).toBeVisible({ timeout: 15_000 })
    await expect(filter).toContainText('Enter a number for id')
    await expect(inspector).toContainText('Enter a number for id')
    await expect(inspector.getByRole('button', { name: 'Run', exact: true })).toBeDisabled()

    await value.fill('7')
    await expect(filter).not.toContainText('Enter a number for id')
    await expect(inspector).not.toContainText('Enter a number for id')

    let runId: string | undefined
    page.on('response', async (response) => {
      if (!response.url().endsWith('/api/run') || response.request().method() !== 'POST') return
      runId = (await response.json().catch(() => ({}))).runId as string | undefined
    })
    await inspector.getByRole('button', { name: 'Run', exact: true }).click()
    const runPanel = page.getByTestId('panel-run')
    await expect(runPanel.getByText('CONFIRM RUN')).toBeVisible()
    await runPanel.getByRole('button', { name: /^(?:Run with unknown row count|Run [\d,]+ rows)$/ }).click()
    await expect.poll(async () => {
      if (!runId) return 'starting'
      return (await (await page.request.get(`/api/run/${runId}`)).json()).status
    }, { timeout: 30_000 }).toBe('done')
  } finally {
    await page.request.delete(`/api/canvas/${canvasId}`)
  }
})
