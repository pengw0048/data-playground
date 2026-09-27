import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { rmSync } from 'node:fs'
import { goldenCanvas, installCanvas } from './support/ux-fixtures'
import type { CanvasDoc } from '../src/types/graph'

async function savedRun(request: APIRequestContext, doc: CanvasDoc) {
  const started = await request.post('/api/run', { data: {
    graph: {
      id: doc.id, version: doc.version, requirements: doc.requirements ?? [], edges: doc.edges,
      nodes: doc.nodes.map((node) => ({
        id: node.id, type: node.type, position: node.position, parentId: node.parentId ?? null,
        data: { title: node.data.title, config: node.data.config, status: node.data.status },
      })),
    },
    targetNodeId: 'filter', confirmed: true,
  } })
  expect(started.ok(), await started.text()).toBe(true)
  const { runId } = await started.json() as { runId: string }
  await expect.poll(async () => {
    const response = await request.get(`/api/run/${runId}`)
    return (await response.json()).status
  }, { timeout: 30_000 }).toBe('done')
  return runId
}

async function openCanvasResult(page: Page, canvasId: string) {
  await page.goto(`/#/canvas/${canvasId}`)
  await page.locator('.react-flow__node[data-id="filter"]').click()
  await page.getByTestId('inspector').getByRole('button', { name: 'View data', exact: true }).click()
  const panel = page.getByTestId('panel-data')
  await expect(panel.getByRole('button', { name: 'Full result', exact: true })).toHaveAttribute('aria-pressed', 'true')
  return panel
}

async function openJobResult(page: Page, runId: string) {
  await page.goto(`/#/jobs?run=${encodeURIComponent(runId)}`)
  await expect(page.getByTestId(`job-row-${runId}`)).toHaveAttribute('aria-expanded', 'true')
  await page.getByRole('button', { name: 'Open result', exact: true }).click()
  return page.getByRole('complementary', { name: 'Saved result', exact: true })
}

test('continues the exact saved page across Canvas and Jobs without executing the graph', async ({ page }, testInfo) => {
  const doc = goldenCanvas(`saved-navigation-${randomUUID()}`, 'Saved result navigation', 'Saved navigation source')
  await installCanvas(page.request, doc)
  const runId = await savedRun(page.request, doc)
  const executions: string[] = []
  const reads: Array<{ runId: string; offset: number }> = []
  page.on('request', (request) => {
    if (request.method() !== 'POST') return
    const path = new URL(request.url()).pathname
    if (path === '/api/run' || path === '/api/run/preview') executions.push(path)
    const sample = path.match(/^\/api\/run\/([^/]+)\/sample$/)
    if (sample) reads.push({ runId: sample[1], offset: request.postDataJSON().offset })
  })

  const panel = await openCanvasResult(page, doc.id)
  await expect(panel.getByText('rows 1–50', { exact: true })).toBeVisible()
  await panel.getByRole('button', { name: 'Next page' }).click()
  await expect(panel.getByText('rows 51–100', { exact: true })).toBeVisible()
  const secondPage = await panel.locator('tbody').textContent() ?? ''
  expect(secondPage).not.toBe('')
  await panel.getByTitle('Close', { exact: true }).click()
  await page.getByTestId('inspector').getByRole('button', { name: 'View data', exact: true }).click()
  await expect(panel.getByText('rows 51–100', { exact: true })).toBeVisible()
  await expect(panel.locator('tbody')).toHaveText(secondPage)

  let saved = await openJobResult(page, runId)
  await expect(saved.getByText('rows 51–100', { exact: true })).toBeVisible()
  await expect(saved.locator('tbody')).toHaveText(secondPage)
  await saved.getByRole('button', { name: 'Previous page' }).click()
  await expect(saved.getByText('rows 1–50', { exact: true })).toBeVisible()
  await saved.getByRole('button', { name: 'Next page' }).click()
  await expect(saved.getByText('rows 51–100', { exact: true })).toBeVisible()
  await saved.getByRole('button', { name: 'Close saved result' }).click()
  await page.getByRole('button', { name: 'Open result', exact: true }).click()
  await expect(saved.getByText('rows 51–100', { exact: true })).toBeVisible()

  const reopened = await openCanvasResult(page, doc.id)
  await expect(reopened.getByText('rows 51–100', { exact: true })).toBeVisible()
  await expect(reopened.locator('tbody')).toHaveText(secondPage)
  expect(executions).toEqual([])
  await testInfo.attach('continued-saved-page', { body: await reopened.screenshot(), contentType: 'image/png' })

  const newRunId = await savedRun(page.request, doc)
  expect(newRunId).not.toBe(runId)
  saved = await openJobResult(page, newRunId)
  await expect(saved.getByText('rows 1–50', { exact: true })).toBeVisible()
  saved = await openJobResult(page, runId)
  await expect(saved.getByText('rows 51–100', { exact: true })).toBeVisible()
  await expect(saved.locator('tbody')).toHaveText(secondPage)

  const status = await (await page.request.get(`/api/run/${runId}`)).json()
  const output = status.outputs.find((candidate: { nodeId: string; portId: string }) => (
    candidate.nodeId === 'filter' && candidate.portId === 'out'
  ))
  expect(output?.uri).toBeTruthy()
  await saved.getByRole('button', { name: 'Close saved result' }).click()
  rmSync(output.uri)
  await page.getByRole('button', { name: 'Open result', exact: true }).click()
  await expect(saved.getByText('Full result expired or removed', { exact: true })).toBeVisible()
  await expect(saved.locator('table')).toHaveCount(0)
  expect(executions).toEqual([])
  expect(reads.filter((read) => read.runId === runId).map((read) => read.offset))
    .toEqual([0, 50, 50, 50, 0, 50, 50, 50, 50, 50])
  expect(reads.filter((read) => read.runId === newRunId).map((read) => read.offset)).toEqual([0])
  await testInfo.attach('saved-result-requests', {
    body: Buffer.from(JSON.stringify({ executions, reads }, null, 2)), contentType: 'application/json',
  })
})
