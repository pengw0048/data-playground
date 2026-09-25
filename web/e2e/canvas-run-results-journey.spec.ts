import { expect, test, type Page } from '@playwright/test'

type SavedRun = { id: string; runId: string; status: string }
type ResultRow = { id: number; doubled_amount: number }
const derivedCode = "def fn(row):\n    return {'id': row['id'], 'doubled_amount': row['amount'] * 2}"

async function runSelectedNode(page: Page, outcome: 'done' | 'failed'): Promise<string> {
  await expect(page.getByTestId('inspector').getByLabel('Node title', { exact: true }))
    .toHaveValue('Double amount')
  const started = page.waitForResponse((response) => response.url().endsWith('/api/run')
    && response.request().method() === 'POST' && response.ok())
  await page.getByTestId('inspector').getByRole('button', { name: 'Run', exact: true }).click()
  const panel = page.getByTestId('panel-run')
  const confirmation = panel.getByRole('button', {
    name: /^(?:Run with unknown row count|Run [\d,]+ rows)$/,
  })
  await expect.poll(async () => await confirmation.isVisible()
    || await panel.getByText(outcome.toUpperCase(), { exact: true }).isVisible()
    || await panel.getByRole('button', { name: 'Stop', exact: true }).isVisible()).toBe(true)
  if (await confirmation.isVisible()) await confirmation.click()
  const response = await started
  expect(response.request().postDataJSON()).toMatchObject({ targetNodeId: 'python' })
  const { runId } = await response.json() as { runId: string }
  await expect(panel.getByText(outcome.toUpperCase(), { exact: true })).toBeVisible({ timeout: 30_000 })
  await panel.getByRole('button', { name: 'Close', exact: true }).click()
  return runId
}

async function replacePython(page: Page, code: string): Promise<void> {
  const input = page.locator('.monaco-editor').first().getByRole('textbox', { name: 'Editor content' })
  await input.focus()
  const macBindings = await page.evaluate(() => navigator.userAgent.includes('Macintosh'))
  await input.press(macBindings ? 'Meta+a' : 'Control+a')
  await page.keyboard.insertText(code)
}

async function editPython(page: Page, code: string): Promise<void> {
  await page.getByRole('button', { name: 'Edit code', exact: true }).last().click()
  await replacePython(page, code)
  await page.getByTitle('Close (Esc)', { exact: true }).click()
}

test('reopens exact saved results after edits, a failed run, and a Canvas reload', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const canvasId = `run-results-journey-${Date.now()}`
  const catalog = await page.request.get('/api/catalog/search', {
    params: { q: 'events', mode: 'lexical', limit: 10 },
  })
  expect(catalog.ok()).toBe(true)
  const events = (await catalog.json() as Array<{
    id: string; name: string; uri: string; registrationId: string
  }>).find((table) => table.name === 'events')!
  expect(events).toBeTruthy()
  const created = await page.request.post('/api/canvas', { data: {
    id: canvasId, name: 'Canvas saved results journey', version: 1, requirements: [],
    // This journey intentionally keeps both successful generations for historical comparison.
    resultRetention: { history: 'recent', maxVersions: 10 },
    nodes: [
      { id: 'source', type: 'source', position: { x: 80, y: 180 }, data: {
        title: 'Events input', status: 'draft', config: {
          uri: events.uri, tableId: events.id, registrationId: events.registrationId,
        },
      } },
      { id: 'filter', type: 'filter', position: { x: 390, y: 180 }, data: {
        title: 'Selected events', status: 'draft', config: { predicate: 'id < 3' },
      } },
      { id: 'python', type: 'transform', position: { x: 700, y: 180 }, data: {
        title: 'Double amount', status: 'draft', config: {
          source: 'adhoc', mode: 'map', onError: 'raise',
          code: derivedCode,
        },
      } },
    ],
    edges: [
      { id: 'source-filter', source: 'source', sourceHandle: 'out', target: 'filter', targetHandle: 'in', data: { wire: 'dataset' } },
      { id: 'filter-python', source: 'filter', sourceHandle: 'out', target: 'python', targetHandle: 'in', data: { wire: 'dataset' } },
    ],
  } })
  expect(created.ok(), await created.text()).toBe(true)
  const results = page.getByRole('complementary', { name: 'Canvas runs and results' })
  const openResults = async () => {
    await page.getByRole('button', { name: 'Runs & results', exact: true }).click()
    await expect(results).toBeVisible()
  }
  const selectSaved = async (runId: string): Promise<SavedRun> => {
    let saved: SavedRun | undefined
    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}/runs`)
      expect(response.ok()).toBe(true)
      saved = (await response.json() as SavedRun[]).find((run) => run.runId === runId)
      return !!saved
    }).toBe(true)
    await expect(results.getByLabel('Choose saved run').locator(`option[value="${saved!.id}"]`)).toHaveCount(1)
    await results.getByLabel('Choose saved run').selectOption(saved!.id)
    return saved!
  }
  const readSelected = async (runId: string, ids: number[]) => {
    const sampled = page.waitForResponse((response) => response.url().endsWith(`/api/run/${encodeURIComponent(runId)}/sample`)
      && response.request().method() === 'POST')
    await results.getByRole('button', { name: 'Open full result', exact: true }).click()
    const response = await sampled
    expect(response.ok(), await response.text()).toBe(true)
    expect(response.request().postDataJSON()).toMatchObject({ nodeId: 'python', portId: 'out' })
    const data = await response.json() as { rows: ResultRow[]; rowCount: number }
    expect(data.rowCount).toBe(ids.length)
    expect(data.rows.map((row) => row.id).sort((a, b) => a - b)).toEqual(ids)
    expect(data.rows.every((row) => row.doubled_amount === row.id * 3)).toBe(true)
    await expect(results.getByRole('columnheader', { name: /doubled_amount/ })).toBeVisible()
    await expect(results.locator('tbody tr')).toHaveCount(ids.length)
    return data
  }
  const checkSavedPredicate = async (predicate: string) => {
    await results.getByText('Settings used for this run', { exact: true }).click()
    const step = results.locator('details').filter({ has: page.locator('dt', { hasText: /^predicate$/ }) }).last()
    await step.locator(':scope > summary').click()
    await expect(step.getByText(predicate, { exact: true })).toBeVisible()
  }
  try {
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=python`)
    const firstRun = await runSelectedNode(page, 'done')
    await openResults()
    await selectSaved(firstRun)
    const first = await readSelected(firstRun, [0, 1, 2])
    await results.getByRole('button', { name: 'Close runs and results' }).click()

    // The starter amount column is a real DECIMAL, so its Python input must stay Decimal rather
    // than being reconstructed from the rounded JSON output table. Prepare this upstream once.
    await page.getByRole('button', { name: 'Edit code', exact: true }).last().click()
    await page.getByRole('button', { name: 'Run upstream', exact: true }).click()
    const upstreamConfirmation = page.getByRole('region', { name: 'Confirm upstream run' })
    await expect(upstreamConfirmation).toBeVisible()
    await upstreamConfirmation.getByRole('button', { name: 'Run upstream', exact: true }).click()
    const inputSample = page.getByRole('region', { name: 'Prepared input sample' })
    await expect(inputSample).toBeVisible({ timeout: 30_000 })
    await expect(inputSample).toContainText('First 3 input rows')
    await expect(inputSample).toContainText('Selected events')
    const inputTable = inputSample.getByRole('table', { name: 'Python input values and types' })
    await expect(inputTable.locator('tbody tr')).toHaveCount(3)
    const firstInput = inputTable.locator('tbody tr').first()
    await expect(firstInput.getByRole('cell').nth(0)).toHaveText('0builtins.int')
    await expect(firstInput.getByRole('cell').nth(3)).toHaveText("Decimal('0.0')decimal.Decimal")
    await expect(inputTable.locator('tbody tr').nth(1).getByRole('cell').nth(3))
      .toHaveText("Decimal('1.5')decimal.Decimal")

    await replacePython(page, "def fn(row):\n    return row['amount'] * 0.1")
    const failedPreview = page.waitForResponse((response) => response.url().endsWith('/api/run/editor-preview')
      && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Test code', exact: true }).click()
    const previewFailure = await (await failedPreview).json()
    expect(previewFailure.error).toBe(true)
    expect(previewFailure.userCodeException.exceptionType).toBe('TypeError')
    await expect(page.getByText(/TypeError:.*decimal.Decimal.*float/)).toBeVisible()
    await expect(inputSample).toBeVisible()
    await expect(firstInput.getByRole('cell').nth(3)).toHaveText("Decimal('0.0')decimal.Decimal")

    await replacePython(page, derivedCode)
    await expect(inputSample).toBeVisible()
    const correctedPreview = page.waitForResponse((response) => response.url().endsWith('/api/run/editor-preview')
      && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Test code', exact: true }).click()
    const previewSuccess = await (await correctedPreview).json()
    expect(previewSuccess.error).not.toBe(true)
    expect(previewSuccess.editorTestInput.runId).toBe(previewFailure.editorTestInput.runId)
    await expect(inputSample).toBeVisible()
    await expect(firstInput.getByRole('cell').nth(3)).toHaveText("Decimal('0.0')decimal.Decimal")
    await expect(page.getByRole('status', { name: 'Test result: using Selected events · 3 input rows' })).toBeVisible()
    await expect(page.getByText(/TypeError:.*decimal.Decimal.*float/)).toHaveCount(0)
    await page.getByTitle('Close (Esc)', { exact: true }).click()
    // The fullscreen editor owns upstream confirmation and progress; returning to the graph must
    // not leave a duplicate upstream Run panel covering the node the user was editing.
    await expect(page.getByTestId('panel-run')).toHaveCount(0)

    // Closing the results panel must leave the graph editable through the ordinary Inspector.
    await page.locator('.react-flow__node[data-id="filter"]').getByText('Selected events', { exact: true }).click()
    await page.getByTestId('inspector').getByLabel('Predicate (SQL)', { exact: true }).fill('id >= 3 AND id < 7')
    await page.locator('.react-flow__node[data-id="python"]').getByText('Double amount', { exact: true }).click()
    const secondRun = await runSelectedNode(page, 'done')
    expect(secondRun).not.toBe(firstRun)
    await openResults()
    await selectSaved(secondRun)
    const second = await readSelected(secondRun, [3, 4, 5, 6])
    await checkSavedPredicate('id >= 3 AND id < 7')
    await selectSaved(firstRun)
    await readSelected(firstRun, [0, 1, 2])
    await checkSavedPredicate('id < 3')
    await expect(results.getByText(/Saved result — not verified as the current output/)).toBeVisible()
    await results.getByRole('button', { name: 'Close runs and results' }).click()

    await editPython(page, "def fn(row):\n    raise ValueError('Please correct the derived value')")
    const failedRun = await runSelectedNode(page, 'failed')
    await openResults()
    await selectSaved(failedRun)
    await expect(results.getByText('Run failed', { exact: true })).toBeVisible()
    await results.getByRole('button', { name: 'View last successful result', exact: true }).click()
    await readSelected(secondRun, [3, 4, 5, 6])

    await page.reload()
    await openResults()
    await selectSaved(failedRun)
    await expect(results.getByText('Run failed', { exact: true })).toBeVisible()
    await results.getByRole('button', { name: 'View last successful result', exact: true }).click()
    await readSelected(secondRun, [3, 4, 5, 6])
    await selectSaved(firstRun)
    await readSelected(firstRun, [0, 1, 2])
    await testInfo.attach('saved-result-journey', {
      body: JSON.stringify({ canvasId, firstRun, secondRun, failedRun, first, second,
        editorInputSample: previewSuccess.editorInputSample,
        editorInputRunId: previewSuccess.editorTestInput.runId,
      }, null, 2),
      contentType: 'application/json',
    })
  } finally {
    await page.goto('about:blank')
    const deleted = await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`, { timeout: 10_000 })
    expect(deleted.ok(), await deleted.text()).toBe(true)
  }
})
