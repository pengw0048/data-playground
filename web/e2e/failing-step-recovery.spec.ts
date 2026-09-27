import { expect, test, type APIResponse, type Page } from '@playwright/test'
import type { CatalogTable, RunStatus, SampleResult, WriteReceipt } from '../src/types/api'

type SavedRun = { id: string; runId: string; status: string }
type RevisionPage = { items: Array<{ revisionId: string }> }
const workingCode = (factor: number) => `def fn(row):\n    return {'id': row['id'], 'calculated_amount': row['amount'] * ${factor}}`
const brokenCode = "def fn(row):\n    return {'id': row['id'], 'calculated_amount': row['missing_amount'] * 2}"

async function json<T>(response: APIResponse, label: string): Promise<T> {
  expect(response.ok(), `${label}: ${response.status()} ${await response.text()}`).toBe(true)
  return response.json() as Promise<T>
}

async function replacePython(page: Page, code: string): Promise<void> {
  const input = page.locator('.monaco-editor').first().getByRole('textbox', { name: 'Editor content' })
  await input.focus()
  const mac = await page.evaluate(() => navigator.userAgent.includes('Macintosh'))
  await input.press(mac ? 'Meta+a' : 'Control+a')
  await page.keyboard.insertText(code)
}

function amounts(sample: SampleResult): number[] {
  return sample.rows.map((row) => Number(row.calculated_amount)).sort((a, b) => a - b)
}

test('repairs the failing Python step and retries its original Write while preserving the last publication', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const token = `failing-step-${Date.now()}-${testInfo.workerIndex}`
  const canvasId = `${token}-canvas`
  const filename = `${token}-result.parquet`
  const datasets = new Map<string, CatalogTable>()
  let created = false
  const submissions: Array<{ targetNodeId?: string }> = []
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/run') {
      submissions.push(request.postDataJSON() as { targetNodeId?: string })
    }
  })
  const inspector = page.getByTestId('inspector')
  const panel = page.getByTestId('panel-run')
  const python = page.locator('.react-flow__node[data-id="python"]')
  const write = page.locator('.react-flow__node[data-id="write"]')
  const results = page.getByRole('complementary', { name: 'Canvas runs and results' })
  const selectWrite = async () => {
    await write.getByText('Publish amounts', { exact: true }).click()
    await expect(inspector.getByLabel('Node title', { exact: true })).toHaveValue('Publish amounts')
  }
  const runWrite = async (outcome: 'done' | 'failed', retry = false): Promise<RunStatus> => {
    const submittedBefore = submissions.length
    const started = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/run'
      && response.request().method() === 'POST')
    if (retry) await panel.getByRole('button', { name: 'Retry', exact: true }).click()
    else await inspector.getByRole('button', { name: 'Run', exact: true }).click()
    const publish = panel.getByRole('button', { name: 'Publish output', exact: true })
    // Prepared upstream inputs can make a retry cheap enough to start without confirmation.
    await expect.poll(async () => submissions.length > submittedBefore || await publish.isVisible()).toBe(true)
    if (submissions.length === submittedBefore) await publish.click()
    const response = await started
    expect(response.request().postDataJSON()).toMatchObject({ targetNodeId: 'write' })
    const status = await json<RunStatus>(response, 'submit the original Write')
    await expect(panel.getByText(outcome === 'done' ? 'DATASET PUBLISHED' : 'FAILED', { exact: true }))
      .toBeVisible({ timeout: 30_000 })
    return json<RunStatus>(await page.request.get(`/api/run/${encodeURIComponent(status.runId)}`), 'read terminal Write attempt')
  }
  const receipt = (status: RunStatus): WriteReceipt => {
    expect(status.status).toBe('done')
    const value = status.outputs.find((output) => output.nodeId === 'write')?.writeReceipt
    expect(value).toBeTruthy()
    return value!
  }
  const revisions = async (tableId: string): Promise<string[]> => {
    const response = await json<RevisionPage>(await page.request.get(`/api/catalog/tables/${encodeURIComponent(tableId)}/revisions`, {
      params: { limit: 100 },
    }), 'read published versions')
    return response.items.map((item) => item.revisionId).sort()
  }
  const openSavedPublication = async (runId: string, expected: number[]) => {
    await page.getByRole('button', { name: 'Runs & results', exact: true }).click()
    await expect(results).toBeVisible()
    let saved: SavedRun | undefined
    await expect.poll(async () => {
      const runs = await json<SavedRun[]>(await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}/runs`), 'read saved Canvas runs')
      saved = runs.find((run) => run.runId === runId)
      return saved?.status
    }).toBe('done')
    await expect(results.getByLabel('Choose saved run').locator(`option[value="${saved!.id}"]`)).toHaveCount(1)
    await results.getByLabel('Choose saved run').selectOption(saved!.id)
    const sampled = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/run/${encodeURIComponent(runId)}/sample`
      && response.request().method() === 'POST')
    await results.getByRole('button', { name: 'Open dataset', exact: true }).click()
    const response = await sampled
    expect(response.request().postDataJSON()).toMatchObject({ nodeId: 'write', portId: 'out' })
    const sample = await json<SampleResult>(response, 'open this successful run’s exact publication')
    expect(amounts(sample)).toEqual(expected)
    await expect(results.getByRole('columnheader', { name: /calculated_amount/ })).toBeVisible()
    await expect(results.locator('tbody tr')).toHaveCount(3)
    await results.getByRole('button', { name: 'Close runs and results' }).click()
    return sample
  }

  try {
    const source = await json<CatalogTable>(await page.request.post('/api/catalog/upload', {
      headers: { 'X-Upload-Filename': `${token}-input.csv`, 'Content-Type': 'text/csv' },
      data: 'id,amount\n1,10\n2,20\n3,30\n',
    }), 'upload the three input rows')
    datasets.set(source.id, source)
    await json(await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Repair the failing step', version: 1, requirements: [],
      resultRetention: { history: 'recent', maxVersions: 10 },
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 160 }, data: { title: 'Input amounts', config: {
          uri: source.uri, tableId: source.id, registrationId: source.registrationId,
        } } },
        { id: 'python', type: 'transform', position: { x: 390, y: 160 }, data: { title: 'Calculate amount', config: {
          source: 'adhoc', mode: 'map', onError: 'raise', code: workingCode(2),
        } } },
        { id: 'write', type: 'write', position: { x: 700, y: 160 }, data: { title: 'Publish amounts', config: {
          filename, writeMode: 'overwrite',
        } } },
      ],
      edges: [
        { id: 'source-python', source: 'source', sourceHandle: 'out', target: 'python', targetHandle: 'in', data: { wire: 'dataset' } },
        { id: 'python-write', source: 'python', sourceHandle: 'out', target: 'write', targetHandle: 'in', data: { wire: 'dataset' } },
      ],
    } }), 'save the Source, Python, and Write workflow')
    created = true
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=write`)
    await expect(inspector.getByLabel('Node title', { exact: true })).toHaveValue('Publish amounts')
    const first = await runWrite('done')
    const initial = receipt(first)
    const publishedUri = first.outputs.find((output) => output.nodeId === 'write')!.uri!
    const catalog = await json<{ items: CatalogTable[] }>(await page.request.get('/api/catalog/tables', {
      params: { uris: publishedUri },
    }), 'find the published dataset')
    const published = catalog.items.find((table) => table.uri === publishedUri)
    expect(published).toBeTruthy()
    datasets.set(published!.id, published!)
    const before = await revisions(published!.id)
    expect(before).toEqual([initial.revisionId])
    await panel.getByTitle('Close', { exact: true }).click()

    await python.getByText('Calculate amount', { exact: true }).click()
    await page.getByRole('button', { name: 'Edit code', exact: true }).last().click()
    await replacePython(page, brokenCode)
    await page.getByTitle('Close (Esc)', { exact: true }).click()
    await selectWrite()
    const failed = await runWrite('failed')
    expect(failed.status).toBe('failed')
    expect(failed.perNode.filter((step) => step.status === 'failed' && step.error).map((step) => step.nodeId)).toEqual(['python'])
    expect(failed.error).toContain('KeyError')
    await expect(panel.getByLabel('Failed step')).toHaveText('Failed step: Calculate amount')
    await expect(panel.getByText(/^Calculate amount:/)).toBeVisible()
    await expect(panel.getByText(/^Publish amounts:/)).toHaveCount(0)
    expect(await revisions(published!.id)).toEqual(before)
    const headAfterFailure = await json<{ revisionId: string }>(await page.request.get(
      `/api/catalog/tables/${encodeURIComponent(published!.id)}/revisions/resolve`,
    ), 'read the unchanged published version after failure')
    expect(headAfterFailure.revisionId).toBe(initial.revisionId)
    await testInfo.attach('python-failure', { body: await page.screenshot(), contentType: 'image/png' })
    await panel.getByTitle('Close', { exact: true }).click()
    const oldSample = await openSavedPublication(first.runId, [20, 40, 60])

    await selectWrite()
    await write.getByRole('button', { name: 'Fix error', exact: true }).click()
    const submittedBeforeReveal = submissions.length
    await panel.getByRole('button', { name: 'Show failing step', exact: true }).click()
    await expect(python).toHaveClass(/selected/)
    await expect(inspector.getByLabel('Node title', { exact: true })).toHaveValue('Calculate amount')
    await expect(panel).toHaveCount(0)
    expect(submissions).toHaveLength(submittedBeforeReveal)
    await page.getByRole('button', { name: 'Edit code', exact: true }).last().click()
    await replacePython(page, workingCode(3))

    // Prepare the real Source result for the code editor; this does not publish a Write version.
    const automaticTest = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/run/editor-preview'
      && response.request().method() === 'POST')
    let tested = false
    void automaticTest.then(() => { tested = true })
    await page.getByRole('button', { name: 'Run upstream', exact: true }).click()
    const confirmation = page.getByRole('region', { name: 'Confirm upstream run' })
    await expect.poll(async () => tested || await confirmation.isVisible(), { timeout: 30_000 }).toBe(true)
    if (await confirmation.isVisible()) await confirmation.getByRole('button', { name: 'Run upstream', exact: true }).click()
    expect((await json<SampleResult>(await automaticTest, 'prepare and test the corrected code')).error).not.toBe(true)
    const retested = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/run/editor-preview'
      && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Test code', exact: true }).click()
    const corrected = await json<SampleResult>(await retested, 'preview the corrected Python step')
    expect(corrected.error).not.toBe(true)
    expect(amounts(corrected)).toEqual([30, 60, 90])
    await expect(page.getByRole('status', { name: 'Test result: using Input amounts · 3 input rows' })).toBeVisible()
    expect(await revisions(published!.id)).toEqual(before)
    await page.getByTitle('Close (Esc)', { exact: true }).click()

    await selectWrite()
    await write.getByRole('button', { name: 'Fix error', exact: true }).click()
    await expect(panel.getByText('FAILED', { exact: true })).toBeVisible()
    const retried = await runWrite('done', true)
    const final = receipt(retried)
    expect(final.datasetId).toBe(initial.datasetId)
    expect(final.revisionId).not.toBe(initial.revisionId)
    expect(final.parentHead?.revisionId).toBe(initial.revisionId)
    expect(await revisions(published!.id)).toEqual([initial.revisionId, final.revisionId].sort())
    expect(submissions.filter((submission) => submission.targetNodeId === 'write')).toHaveLength(3)
    await panel.getByTitle('Close', { exact: true }).click()
    await openSavedPublication(retried.runId, [30, 60, 90])
    await openSavedPublication(first.runId, [20, 40, 60])
    await testInfo.attach('failing-step-recovery-results', {
      body: JSON.stringify({ canvasId, failedRun: failed.runId, failedStep: 'python', initial, final,
        oldRows: oldSample.rows, correctedRows: corrected.rows, writeAttempts: submissions.filter((submission) => submission.targetNodeId === 'write').length,
      }, null, 2), contentType: 'application/json',
    })
  } finally {
    if (!page.isClosed()) {
      await page.goto('about:blank')
      if (created) await json(await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`, { timeout: 10_000 }), 'remove recovery Canvas')
      for (const table of datasets.values()) {
        const current = await json<CatalogTable>(await page.request.get(`/api/catalog/tables/${encodeURIComponent(table.id)}`), 'read cleanup registration')
        await json(await page.request.delete(`/api/catalog/tables/${encodeURIComponent(table.id)}`, {
          params: { expected_registration_id: current.registrationId!, expected_revision: current.metadataRevision! }, timeout: 10_000,
        }), 'unregister recovery fixture')
      }
    }
  }
})
