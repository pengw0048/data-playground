import { expect, test, type APIResponse } from '@playwright/test'
import type { CatalogTable, SampleResult } from '../src/types/api'
import type { CanvasDoc } from '../src/types/graph'
import { canvasIdFromLocation } from './support/canvasRoute'

async function json<T>(response: APIResponse, label: string): Promise<T> {
  expect(response.ok(), `${label}: ${response.status()} ${await response.text()}`).toBe(true)
  return response.json() as Promise<T>
}

test('starts from an uploaded dataset and edits a quoted numeric column without running or writing', async ({ page }, testInfo) => {
  test.setTimeout(90_000)
  const token = `own-data-starter-${Date.now()}-${testInfo.workerIndex}`
  const canvasId = `${token}-canvas`
  const column = 'sale "USD"'
  const csv = [
    'id,"sale ""USD""",quantity,description',
    '1,9.75,1,below',
    '2,10.5,2,boundary',
    '3,10.75,3,above',
    '4,20.25,4,high',
    '5,,5,missing',
    '6,"",6,empty',
    '7,-2.5,7,negative',
    '',
  ].join('\n')
  let dataset: CatalogTable | undefined
  let created = false
  const executions: string[] = []
  let previewRequests = 0
  page.on('request', (request) => {
    if (request.method() !== 'POST') return
    const path = new URL(request.url()).pathname
    if (path === '/api/run' || path === '/api/run/write-admission') executions.push(path)
    if (path === '/api/run/preview') previewRequests += 1
  })
  const saved = () => page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
    .then((response) => json<CanvasDoc>(response, 'read saved starter Canvas'))
  const filter = page.locator('.react-flow__node[data-id="flt"]')
  const inspector = page.getByTestId('inspector')
  const panel = page.getByTestId('panel-data')
  const preview = async (ids: number[]) => {
    const sampled = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/run/preview'
      && response.request().method() === 'POST')
    await inspector.getByRole('button', { name: 'View data', exact: true }).click()
    const response = await sampled
    const result = await json<SampleResult>(response, 'preview the selected Filter')
    expect(response.request().postDataJSON()).toMatchObject({ nodeId: 'flt' })
    expect(result.rows.map((row) => Number(row.id)).sort((a, b) => a - b)).toEqual(ids)
    await expect(panel.getByRole('columnheader', { name: column })).toBeVisible()
    await expect(panel.locator('tbody tr')).toHaveCount(ids.length)
    await expect(panel.getByText('high', { exact: true })).toBeVisible()
    await panel.getByTitle('Close', { exact: true }).click()
    return result
  }

  try {
    dataset = await json<CatalogTable>(await page.request.post('/api/catalog/upload', {
      headers: { 'X-Upload-Filename': `${token}.csv`, 'Content-Type': 'text/csv' }, data: csv,
    }), 'upload the researcher CSV')
    expect(dataset.registrationId).toBeTruthy()
    await json(await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'untitled', version: 1, requirements: [], nodes: [], edges: [],
    } }), 'create an empty Canvas')
    created = true

    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    await page.getByRole('button', { name: 'Filter my data', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Filter your data', exact: true })
    await dialog.getByLabel('Search starter datasets', { exact: true }).fill(dataset.name)
    await dialog.getByRole('button', { name: `Choose starter dataset ${dataset.name}`, exact: true }).click()
    const columns = dialog.getByLabel('Starter numeric column', { exact: true })
    await expect(columns).toBeEnabled()
    expect(await columns.locator('option').evaluateAll((options) => options.map((option) => option.getAttribute('value'))))
      .toEqual(['', 'id', column, 'quantity'])
    await columns.selectOption(column)
    await dialog.getByLabel('Starter threshold', { exact: true }).fill('10.5')
    await dialog.getByRole('button', { name: 'Use in this Canvas', exact: true }).click()
    await expect(dialog).toHaveCount(0)
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await expect(filter).toHaveClass(/selected/)
    await expect(inspector.getByLabel('Node title', { exact: true })).toHaveValue('Filter rows')
    expect(canvasIdFromLocation(page.url())).toBe(canvasId)
    await expect.poll(async () => (await saved()).nodes.map((node) => node.type).sort())
      .toEqual(['filter', 'source'])
    const initial = await saved()
    expect(initial.nodes.find((node) => node.id === 'src')?.data.config).toMatchObject({
      uri: dataset.uri, tableId: dataset.id, registrationId: dataset.registrationId,
    })
    expect(initial.nodes.find((node) => node.id === 'flt')?.data.config).toMatchObject({
      predicate: '"sale ""USD""" > 10.5',
      filterBuilder: { conditions: [{ col: column, op: '>', val: '10.5' }] },
    })
    expect(initial.edges).toHaveLength(1)
    expect(initial.edges[0]).toMatchObject({ source: 'src', target: 'flt', sourceHandle: 'out', targetHandle: 'in' })
    expect(executions).toEqual([])
    expect(previewRequests).toBe(0)

    // Reopen the persisted Canvas before inspecting or changing the ordinary node controls.
    await page.reload()
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await filter.getByText('Filter rows', { exact: true }).click()
    await expect(filter.getByPlaceholder('column', { exact: true })).toHaveValue(column)
    await expect(filter.getByPlaceholder('value', { exact: true })).toHaveValue('10.5')
    expect((await saved()).nodes.find((node) => node.id === 'src')?.data.config)
      .toEqual(initial.nodes.find((node) => node.id === 'src')?.data.config)
    expect(previewRequests).toBe(0)
    const first = await preview([3, 4])
    expect(first.rows.map((row) => Number(row[column])).sort((a, b) => a - b)).toEqual([10.75, 20.25])

    // Edit the FilterBuilder value on the node itself, preserving the embedded quote in its column.
    await filter.getByPlaceholder('value', { exact: true }).fill('10.75')
    await expect.poll(async () => (await saved()).nodes.find((node) => node.id === 'flt')?.data.config.predicate)
      .toBe('"sale ""USD""" > 10.75')
    const second = await preview([4])
    expect(second.rows[0][column]).toBe(20.25)
    await page.reload()
    await filter.getByText('Filter rows', { exact: true }).click()
    await expect(filter.getByPlaceholder('column', { exact: true })).toHaveValue(column)
    await expect(filter.getByPlaceholder('value', { exact: true })).toHaveValue('10.75')
    await preview([4])

    expect(executions).toEqual([])
    expect((await saved()).nodes.map((node) => node.type).sort()).toEqual(['filter', 'source'])
    const runs = await json<unknown[]>(await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}/runs`), 'read Canvas runs')
    expect(runs).toEqual([])
    await testInfo.attach('own-data-starter-results', {
      body: JSON.stringify({ canvasId, registrationId: dataset.registrationId, column, first: first.rows, second: second.rows }, null, 2),
      contentType: 'application/json',
    })
  } finally {
    if (!page.isClosed()) {
      await page.goto('about:blank')
      if (created) await json(await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`, { timeout: 10_000 }), 'remove starter Canvas')
      if (dataset) {
        const current = await json<CatalogTable>(await page.request.get(`/api/catalog/tables/${encodeURIComponent(dataset.id)}`), 'read upload registration')
        await json(await page.request.delete(`/api/catalog/tables/${encodeURIComponent(dataset.id)}`, {
          params: { expected_registration_id: current.registrationId!, expected_revision: current.metadataRevision! }, timeout: 10_000,
        }), 'unregister the uploaded fixture')
      }
    }
  }
})
