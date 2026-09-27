import { expect, test, type APIResponse } from '@playwright/test'
import type { CatalogTable, SampleResult } from '../src/types/api'
import type { CanvasDoc } from '../src/types/graph'

async function json<T>(response: APIResponse, label: string): Promise<T> {
  expect(response.ok(), `${label}: ${response.status()} ${await response.text()}`).toBe(true)
  return response.json() as Promise<T>
}

test('sorts uploaded quoted and keyword columns, preserving direction and null placement after reopening', async ({ page }, testInfo) => {
  test.setTimeout(90_000)
  page.setDefaultTimeout(10_000)
  const token = `sort-columns-${Date.now()}-${testInfo.workerIndex}`
  const canvasId = `${token}-canvas`
  const group = 'team, "label"'
  const csv = ['"team, ""label""",DESC,id', 'b,2,1', 'a,1,2', 'a,2,3', 'a,,4', ',3,5', ''].join('\n')
  let dataset: CatalogTable | undefined
  let created = false
  const sort = page.locator('.react-flow__node[data-id="sort"]')
  const inspector = page.getByTestId('inspector')
  const panel = page.getByTestId('panel-data')
  const saved = () => page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
    .then((response) => json<CanvasDoc>(response, 'read saved sort Canvas'))

  const runAndInspect = async (expectedIds: number[]) => {
    const started = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/run'
      && response.request().method() === 'POST' && response.ok(), { timeout: 15_000 })
    await inspector.getByRole('button', { name: 'Run', exact: true }).click()
    const runPanel = page.getByTestId('panel-run')
    const confirmation = runPanel.getByRole('button', { name: /^(?:Run with unknown row count|Run [\d,]+ rows)$/ })
    await expect.poll(async () => await confirmation.isVisible()
      || await runPanel.getByText('DONE', { exact: true }).isVisible()
      || await runPanel.getByRole('button', { name: 'Stop', exact: true }).isVisible()).toBe(true)
    if (await confirmation.isVisible()) await confirmation.click()
    const response = await started
    expect(response.request().postDataJSON()).toMatchObject({ targetNodeId: 'sort' })
    const { runId } = await response.json() as { runId: string }
    await expect(runPanel.getByText('DONE', { exact: true })).toBeVisible({ timeout: 30_000 })
    await runPanel.getByRole('button', { name: 'Close', exact: true }).click()
    const sampled = page.waitForResponse((result) => result.url().endsWith(`/api/run/${encodeURIComponent(runId)}/sample`)
      && result.request().method() === 'POST', { timeout: 15_000 })
    await inspector.getByRole('button', { name: 'View data', exact: true }).click()
    const result = await json<SampleResult>(await sampled, 'read the saved sorted rows')
    expect(result.rows.map((row) => Number(row.id))).toEqual(expectedIds)
    await expect(panel.getByTestId('full-result-status')).toHaveText('Complete · 5 rows')
    await expect(panel.getByRole('columnheader', { name: group })).toBeVisible()
    await expect(panel.locator('tbody tr')).toHaveCount(5)
    await expect(panel.locator('tbody tr td:nth-child(3)')).toHaveText(expectedIds.map(String))
    await panel.getByTitle('Close', { exact: true }).click()
  }

  try {
    dataset = await json<CatalogTable>(await page.request.post('/api/catalog/upload', {
      headers: { 'X-Upload-Filename': `${token}.csv`, 'Content-Type': 'text/csv' }, data: csv,
    }), 'upload columns with punctuation and ordering keywords')
    await json(await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Sort unusual columns', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 100, y: 160 }, data: {
          title: 'Uploaded rows', status: 'draft', config: {
            uri: dataset.uri, tableId: dataset.id, registrationId: dataset.registrationId,
          },
        } },
        { id: 'sort', type: 'sort', position: { x: 440, y: 160 },
          data: { title: 'Order uploaded rows', status: 'draft', config: { by: '' } } },
      ],
      edges: [{ id: 'source-sort', source: 'source', target: 'sort', sourceHandle: 'out', targetHandle: 'in', data: { wire: 'dataset' } }],
    } }), 'create the sorting Canvas')
    created = true
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    await sort.getByText('Order uploaded rows', { exact: true }).click()
    await sort.getByText('add sort key', { exact: true }).click()
    const keys = sort.getByPlaceholder('column', { exact: true })
    // Clearing the default choice must leave a field in which to select or type the exact name.
    await keys.nth(0).fill('')
    await keys.nth(0).fill(group)
    await sort.getByLabel('Null placement for sort key 1', { exact: true }).selectOption('LAST')
    await sort.getByText('add sort key', { exact: true }).click()
    await keys.nth(1).fill('DESC')
    await sort.getByTitle('Toggle direction', { exact: true }).nth(1).click()
    await sort.getByLabel('Null placement for sort key 2', { exact: true }).selectOption('FIRST')
    await expect.poll(async () => (await saved()).nodes.find((node) => node.id === 'sort')?.data.config.by)
      .toBe('"team, ""label""" ASC NULLS LAST, "DESC" DESC NULLS FIRST')
    await expect(sort.getByText(/unknown column:/)).toHaveCount(0)
    await runAndInspect([4, 3, 2, 1, 5])

    await sort.getByTitle('Toggle direction', { exact: true }).nth(0).click()
    await expect.poll(async () => (await saved()).nodes.find((node) => node.id === 'sort')?.data.config.by)
      .toBe('"team, ""label""" DESC NULLS LAST, "DESC" DESC NULLS FIRST')
    await page.reload()
    await sort.getByText('Order uploaded rows', { exact: true }).click()
    await expect(keys.nth(0)).toHaveValue(group)
    await expect(keys.nth(1)).toHaveValue('DESC')
    await expect(sort.getByLabel('Null placement for sort key 1', { exact: true })).toHaveValue('LAST')
    await expect(sort.getByLabel('Null placement for sort key 2', { exact: true })).toHaveValue('FIRST')
    await runAndInspect([1, 4, 3, 2, 5])
  } finally {
    if (!page.isClosed()) {
      await page.goto('about:blank')
      if (created) await json(await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`, { timeout: 10_000 }), 'remove sorting Canvas')
      if (dataset) {
        const current = await json<CatalogTable>(await page.request.get(`/api/catalog/tables/${encodeURIComponent(dataset.id)}`, { timeout: 10_000 }), 'read upload registration')
        await json(await page.request.delete(`/api/catalog/tables/${encodeURIComponent(dataset.id)}`, {
          params: { expected_registration_id: current.registrationId!, expected_revision: current.metadataRevision! }, timeout: 10_000,
        }), 'unregister uploaded sorting rows')
      }
    }
  }
})
