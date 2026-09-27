import { expect, test, type APIResponse } from '@playwright/test'
import type { CatalogTable, SampleResult } from '../src/types/api'
import type { CanvasDoc } from '../src/types/graph'

async function json<T>(response: APIResponse, label: string): Promise<T> {
  expect(response.ok(), `${label}: ${response.status()} ${await response.text()}`).toBe(true)
  return response.json() as Promise<T>
}

test('counts every uploaded row by a quoted category, including missing values, and reopens the result', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const token = `group-count-${Date.now()}-${testInfo.workerIndex}`
  const canvasId = `${token}-canvas`
  const column = 'kind, category, "label"'
  // Exceed the preview input cap, so this proves a complete count rather than a sample summary.
  const csv = ['id,"kind, category, ""label""",row_count', ...Array.from({ length: 2103 }, (_, i) =>
    `${i},${i < 2001 ? 'common' : i < 2101 ? 'rare' : ''},${i % 2}`), ''].join('\n')
  let dataset: CatalogTable | undefined
  let created = false
  const executions: string[] = []
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && ['/api/run', '/api/run/preview', '/api/run/write-admission'].includes(path)) executions.push(path)
  })
  const saved = () => page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
    .then((response) => json<CanvasDoc>(response, 'read the saved count Canvas'))
  try {
    dataset = await json<CatalogTable>(await page.request.post('/api/catalog/upload', {
      headers: { 'X-Upload-Filename': `${token}.csv`, 'Content-Type': 'text/csv' }, data: csv,
    }), 'upload categories')
    await json(await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'untitled', version: 1, requirements: [], nodes: [], edges: [],
    } }), 'create empty Canvas')
    created = true
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    await page.getByRole('button', { name: 'Count by group', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Count rows by group', exact: true })
    await dialog.getByLabel('Search starter datasets', { exact: true }).fill(dataset.name)
    await dialog.getByRole('button', { name: `Choose starter dataset ${dataset.name}`, exact: true }).click()
    await dialog.getByLabel('Starter grouping column', { exact: true }).selectOption(column)
    await dialog.getByRole('button', { name: 'Use in this Canvas', exact: true }).click()
    await expect(dialog).toHaveCount(0)
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await expect(page.locator('.react-flow__node[data-id="agg"]')).toHaveClass(/selected/)
    await expect.poll(async () => (await saved()).nodes.find((node) => node.id === 'agg')?.data.config)
      .toEqual({ groupBy: '"kind, category, ""label"""', aggs: 'count(*) AS row_count' })
    expect((await saved()).nodes.find((node) => node.id === 'src')?.data.config).toMatchObject({
      uri: dataset.uri, tableId: dataset.id, registrationId: dataset.registrationId,
    })
    expect(executions).toEqual([])
    await page.reload()
    const aggregate = page.locator('.react-flow__node[data-id="agg"]')
    await aggregate.getByText('Count rows by group', { exact: true }).click()
    await expect(aggregate.getByPlaceholder('category', { exact: true })).toHaveValue('"kind, category, ""label"""')
    await expect(aggregate.getByText(/unknown column:/)).toHaveCount(0)
    expect(executions).toEqual([])

    const started = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/run'
      && response.request().method() === 'POST' && response.ok())
    await page.getByTestId('inspector').getByRole('button', { name: 'Run', exact: true }).click()
    const runPanel = page.getByTestId('panel-run')
    const confirmation = runPanel.getByRole('button', { name: /^(?:Run with unknown row count|Run [\d,]+ rows)$/ })
    await expect.poll(async () => await confirmation.isVisible()
      || await runPanel.getByText('DONE', { exact: true }).isVisible()
      || await runPanel.getByRole('button', { name: 'Stop', exact: true }).isVisible()).toBe(true)
    if (await confirmation.isVisible()) await confirmation.click()
    const response = await started
    expect(response.request().postDataJSON()).toMatchObject({ targetNodeId: 'agg' })
    const { runId } = await response.json() as { runId: string }
    await expect(runPanel.getByText('DONE', { exact: true })).toBeVisible({ timeout: 30_000 })
    await runPanel.getByRole('button', { name: 'Close', exact: true }).click()
    // Reopening must recover the stored full result, including the null category.
    await page.reload()
    await page.getByRole('button', { name: 'Runs & results', exact: true }).click()
    const results = page.getByRole('complementary', { name: 'Canvas runs and results' })
    const sampled = page.waitForResponse((res) => res.url().endsWith(`/api/run/${encodeURIComponent(runId)}/sample`)
      && res.request().method() === 'POST')
    await results.getByRole('button', { name: 'Open full result', exact: true }).click()
    const result = await json<SampleResult>(await sampled, 'read complete category counts')
    expect(result.rows).toHaveLength(3)
    expect(Object.fromEntries(result.rows.map((row) => [String(row[column]), Number(row.row_count)])))
      .toEqual({ common: 2001, rare: 100, null: 2 })
    await expect(results.getByRole('columnheader', { name: column })).toBeVisible()
    await expect(results.locator('tbody tr')).toHaveCount(3)
    expect(executions.filter((path) => path === '/api/run')).toHaveLength(1)
    expect(executions).not.toContain('/api/run/write-admission')
  } finally {
    await page.goto('about:blank')
    if (created) await json(await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`), 'remove count Canvas')
    if (dataset) {
      const current = await json<CatalogTable>(await page.request.get(`/api/catalog/tables/${encodeURIComponent(dataset.id)}`), 'read upload registration')
      await json(await page.request.delete(`/api/catalog/tables/${encodeURIComponent(dataset.id)}`, {
        params: { expected_registration_id: current.registrationId!, expected_revision: current.metadataRevision! },
      }), 'unregister uploaded categories')
    }
  }
})
