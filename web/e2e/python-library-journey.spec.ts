import { expect, test, type Page } from '@playwright/test'
import { canvasIdFromLocation } from './support/canvasRoute'
import { backToWorkspace } from './support/workspace'

async function editPython(page: Page, code: string): Promise<void> {
  const editor = page.locator('.monaco-editor').first()
  await expect(editor).toBeVisible()
  const input = editor.getByRole('textbox', { name: 'Editor content' })
  await input.focus()
  // Monaco selects platform bindings from the browser UA. Desktop Chrome's emulated Windows
  // UA can differ from the macOS host used by Playwright's ControlOrMeta shortcut.
  const macBindings = await page.evaluate(() => navigator.userAgent.includes('Macintosh'))
  await input.press(macBindings ? 'Meta+a' : 'Control+a')
  await page.keyboard.insertText(code)
}

test('edits Python before preparing input, fixes it, and reuses its named Library version with a dataset', async ({ page }, testInfo) => {
  test.setTimeout(90_000)
  const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  const canvasId = `python-library-journey-${suffix}`
  const title = `Acceptance value ${suffix}`
  const finalCode = "def fn(row):\n    return {'acceptance_value': 23}"
  const catalogResponse = await page.request.get('/api/catalog/search', {
    params: { q: 'events', mode: 'lexical', limit: 10 },
  })
  expect(catalogResponse.ok()).toBe(true)
  const events = (await catalogResponse.json() as Array<{
    id: string; name: string; uri: string; registrationId: string
  }>).find((table) => table.name === 'events')
  expect(events, 'the built-in events dataset is available').toBeTruthy()

  // Start with an ordinary connected graph whose upstream output has never been retained.
  const created = await page.request.post('/api/canvas', { data: {
    id: canvasId, name: 'Python to reusable Library journey', version: 1, requirements: [],
    nodes: [
      { id: 'source', type: 'source', position: { x: 80, y: 180 }, data: {
        title: 'Events input', status: 'draft', config: {
          uri: events!.uri, tableId: events!.id, registrationId: events!.registrationId,
        },
      } },
      { id: 'filter', type: 'filter', position: { x: 390, y: 180 }, data: {
        title: 'Purchase events', status: 'draft', config: { predicate: "event = 'purchase'" },
      } },
      { id: 'python', type: 'transform', position: { x: 700, y: 180 }, data: {
        title: 'Python draft', status: 'draft', config: {
          source: 'adhoc', mode: 'map', onError: 'raise', code: 'def fn(row):\n    return row',
        },
      } },
    ],
    edges: [
      { id: 'source-filter', source: 'source', sourceHandle: 'out', target: 'filter', targetHandle: 'in', data: { wire: 'dataset' } },
      { id: 'filter-python', source: 'filter', sourceHandle: 'out', target: 'python', targetHandle: 'in', data: { wire: 'dataset' } },
    ],
  } })
  expect(created.ok(), await created.text()).toBe(true)
  let reusedCanvasId: string | undefined
  let promoted: { id: string; version: string } | undefined
  try {
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=python`)
    await page.getByRole('button', { name: 'Edit code', exact: true }).last().click()
    const runUpstream = page.getByRole('button', { name: 'Run upstream', exact: true })
    await expect(runUpstream).toBeVisible()
    await expect(page.getByRole('button', { name: 'Test code', exact: true })).toBeDisabled()

    // Editing the downstream code must not hide the action that can produce its missing input.
    await editPython(page, "def fn(row):\n    return {'acceptance_value': 17}")
    await expect(runUpstream).toBeVisible()
    await expect(page.getByRole('button', { name: 'Test code', exact: true })).toBeDisabled()
    await runUpstream.click()
    const upstreamConfirmation = page.getByRole('region', { name: 'Confirm upstream run' })
    await expect(upstreamConfirmation).toBeVisible()
    await upstreamConfirmation.getByRole('button', { name: 'Run upstream', exact: true }).click()
    await expect(page.getByRole('status', {
      name: /^Test result: using Purchase events · [\d,]+ input rows$/,
    })).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('17', { exact: true }).first()).toBeVisible()

    await editPython(page, "def fn(row)\n    return {'acceptance_value': 23}")
    await page.getByRole('button', { name: 'Test code', exact: true }).click()
    await expect(page.getByText('Fix the Python syntax', { exact: true })).toBeVisible()
    await expect(page.getByText("Line 1: expected ':'", { exact: true })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('python-error-can-be-fixed.png') })

    await editPython(page, finalCode)
    const correctedResponse = page.waitForResponse((response) =>
      response.url().endsWith('/api/run/editor-preview') && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Test code', exact: true }).click()
    const corrected = await correctedResponse
    expect(corrected.ok()).toBe(true)
    const correctedResult = await corrected.json() as { rows: Array<{ acceptance_value: number }> }
    expect(correctedResult.rows.length).toBeGreaterThan(0)
    expect(correctedResult.rows.every((row) => row.acceptance_value === 23)).toBe(true)
    await expect(page.getByText('23', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('Fix the Python syntax', { exact: true })).toHaveCount(0)
    await page.screenshot({ path: testInfo.outputPath('python-corrected-result.png') })

    await page.getByRole('button', { name: 'Promote to library' }).click()
    const promotion = page.getByRole('dialog', { name: /Promote .* to the Library/ })
    await promotion.getByLabel('Name', { exact: true }).fill(title)
    await promotion.getByLabel('Description', { exact: true }).fill('Produces acceptance_value from each input row; verified against purchase events.')
    const promotedResponse = page.waitForResponse((response) =>
      response.url().endsWith('/api/processors/promote') && response.request().method() === 'POST')
    await promotion.getByRole('button', { name: 'Promote', exact: true }).click()
    const saved = await promotedResponse
    expect(saved.ok(), await saved.text()).toBe(true)
    promoted = await saved.json()
    await expect(promotion).toHaveCount(0)
    await expect(page.locator('.react-flow__node[data-id="python"]')).toContainText(title)
    await expect(page.locator('.monaco-editor')).toHaveCount(0)
    await backToWorkspace(page)
    await page.getByTestId('rail-transforms').click()
    await page.getByLabel('Search Transforms').fill(title)
    await page.getByRole('region', { name: 'Transform library', exact: true })
      .getByRole('button').filter({ hasText: title }).click()
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible()

    // Reuse the exact saved version with a chosen dataset, without assembling or repairing edges.
    await page.getByRole('button', { name: `Use ${promoted!.version}`, exact: true }).click()
    const useDialog = page.getByRole('dialog', { name: `Use ${title}`, exact: true })
    await useDialog.getByLabel('New Canvas name').fill(`Reuse ${title}`)
    await useDialog.getByRole('button', { name: 'Choose input dataset…' }).click()
    await useDialog.getByLabel('Search input datasets').fill('events')
    await useDialog.getByRole('region', { name: 'Choose Transform input' })
      .getByRole('button', { name: /^events / }).click()
    await expect(useDialog.getByRole('region', { name: 'Transform input dataset' })).toContainText('events')
    await useDialog.getByRole('button', { name: 'Create and open', exact: true }).click()
    await expect(page.getByTestId('toolbar')).toBeVisible()
    reusedCanvasId = canvasIdFromLocation(page.url())
    expect(reusedCanvasId).not.toBe(canvasId)
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    const canvas = await (await page.request.get(`/api/canvas/${encodeURIComponent(reusedCanvasId)}`)).json() as {
      nodes: Array<{ id: string; type: string; data: { config: Record<string, unknown> } }>
      edges: Array<{ source: string; target: string }>
    }
    const source = canvas.nodes.find((node) => node.type === 'source')!
    const transform = canvas.nodes.find((node) => node.type === 'transform')!
    expect(source).toBeTruthy()
    expect(transform.data.config).toMatchObject({
      source: 'library', processor: promoted!.id, version: promoted!.version,
    })
    expect(canvas.edges).toEqual([expect.objectContaining({ source: source.id, target: transform.id })])

    const runResponse = page.waitForResponse((response) =>
      response.url().endsWith('/api/run') && response.request().method() === 'POST')
    await page.getByTestId('inspector').getByRole('button', { name: 'Run', exact: true }).click()
    const runPanel = page.getByTestId('panel-run')
    // A large or unknown estimate may need the existing ordinary-run confirmation.
    const confirm = runPanel.getByRole('button', { name: /^(?:Run with unknown row count|Run [\d,]+ rows)$/ })
    await expect.poll(async () => (await confirm.isVisible()) || (await runPanel.getByText('DONE', { exact: true }).isVisible())
      || (await runPanel.getByRole('button', { name: 'Stop', exact: true }).isVisible())).toBe(true)
    if (await confirm.isVisible()) await confirm.click()
    const started = await runResponse
    expect(started.ok(), await started.text()).toBe(true)
    const { runId } = await started.json() as { runId: string }
    await expect(runPanel.getByText('DONE', { exact: true })).toBeVisible({ timeout: 30_000 })
    const sampled = await page.request.post(`/api/run/${encodeURIComponent(runId)}/sample`, {
      data: { nodeId: transform.id, portId: 'out', k: 50, offset: 0 },
    })
    expect(sampled.ok(), await sampled.text()).toBe(true)
    const output = await sampled.json() as { rows: Array<{ acceptance_value: number }> }
    expect(output.rows.length).toBeGreaterThan(0)
    expect(output.rows.every((row) => row.acceptance_value === 23)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath('library-input-connected-run-done.png') })
    await testInfo.attach('user-journey-result', {
      body: JSON.stringify({ canvasId, title, promoted, reusedCanvasId, runId, correctedResult, output }, null, 2),
      contentType: 'application/json',
    })
  } finally {
    await page.goto('about:blank')
    for (const id of [reusedCanvasId, canvasId]) {
      if (id) expect((await page.request.delete(`/api/canvas/${encodeURIComponent(id)}`)).ok()).toBe(true)
    }
    if (promoted) expect((await page.request.delete(
      `/api/processors/${encodeURIComponent(promoted.id)}/versions/${encodeURIComponent(promoted.version)}`,
    )).ok()).toBe(true)
  }
})
