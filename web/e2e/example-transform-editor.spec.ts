import { expect, test, type Page } from '@playwright/test'

test('Example rows stay local to the fullscreen Transform editor', async ({ page }) => {
  const canvasId = `example-transform-${Date.now()}`
  const sourceUri = 'e2e-full-run-only://events'
  const registered = await page.request.post('/api/catalog/register', {
    data: { uri: sourceUri, name: 'E2E Example rows source' },
  })
  const registrationText = await registered.text()
  expect(registered.ok(), registrationText).toBe(true)
  const registeredTable = JSON.parse(registrationText)
  let canvasCreated = false
  try {
    const graph = {
      id: canvasId,
      name: 'Example rows editor',
      version: 1,
      requirements: [],
      nodes: [
        {
          id: 'source', type: 'source', position: { x: 80, y: 180 },
          data: { title: 'Remote events', status: 'latest', config: {
            uri: sourceUri,
            datasetRef: {
              kind: 'exact', datasetId: registeredTable.registrationId, revisionId: 'fixture-v1',
            },
          } },
        },
        {
          id: 'transform', type: 'transform', position: { x: 430, y: 180 },
          data: { title: 'Test Example rows', status: 'draft', config: {
            source: 'adhoc', mode: 'map',
            code: "def fn(row):\n    return {**row, 'code_output': True}",
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
    expect(created.ok()).toBe(true)
    canvasCreated = true
    const before = await (await page.request.get(
      `/api/canvas/${encodeURIComponent(canvasId)}`)).json()

    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=transform`)
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await page.getByRole('button', { name: 'Edit code' }).last().click()
    await page.getByRole('button', { name: 'Example rows', exact: true }).click()
    await expect(page.getByText('Test only')).toBeVisible()

    const fixture = page.getByRole('textbox', { name: 'Example rows JSON' })
    await fixture.fill('[1]')
    await expect(page.getByText('Every example row must be a JSON object.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Test code' })).toBeDisabled()

    await fixture.fill('[{"event":"fixture","amount":4,"fixtureOnly":"sentinel"}]')
    await page.getByRole('button', { name: 'Test code' }).click()
    await expect(page.getByText('sentinel', { exact: true })).toBeVisible()
    await expect(page.getByText('true', { exact: true }).first()).toBeVisible()
    await expect(page.getByRole('button', { name: 'Stats' })).toHaveCount(0)
    await expect(page.getByRole('status', {
      name: 'Test result: 1 output row from Example rows',
    })).toHaveText('Test result · 1 output row from Example rows')
    await expect(page.getByText('Preview prefix', { exact: true })).toHaveCount(0)
    await expect(page.getByText(/Full dataset not scanned/)).toHaveCount(0)

    const after = await (await page.request.get(
      `/api/canvas/${encodeURIComponent(canvasId)}`)).json()
    expect(after).toEqual(before)
    expect(await (await page.request.get(
      `/api/canvas/${encodeURIComponent(canvasId)}/runs`)).json()).toEqual([])
    expect(await (await page.request.get(
      `/api/canvas/${encodeURIComponent(canvasId)}/profile-jobs`)).json()).toEqual([])

    const started = await page.request.post('/api/run', { data: {
      graph: after, targetNodeId: 'transform', confirmed: true,
      submissionId: crypto.randomUUID(),
    } })
    const startedText = await started.text()
    expect(started.ok(), startedText).toBe(true)
    const { runId } = JSON.parse(startedText)
    await expect.poll(async () => {
      const response = await page.request.get(`/api/run/${encodeURIComponent(runId)}`)
      return (await response.json()).status
    }, { timeout: 15_000 }).toBe('done')
    const formal = await page.request.post(
      `/api/run/${encodeURIComponent(runId)}/sample`,
      { data: { nodeId: 'transform', portId: 'out', k: 50, offset: 0 } },
    )
    const formalText = await formal.text()
    expect(formal.ok(), formalText).toBe(true)
    const formalRows = JSON.parse(formalText).rows as Record<string, unknown>[]
    expect(formalRows.length).toBeGreaterThan(0)
    expect(formalRows.every((row) => row.code_output === true)).toBe(true)
    expect(formalRows.every((row) => !('fixtureOnly' in row))).toBe(true)
  } finally {
    if (canvasCreated) {
      expect((await page.request.delete(
        `/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBe(true)
    }
    const query = new URLSearchParams({
      expected_registration_id: registeredTable.registrationId,
      expected_revision: registeredTable.metadataRevision,
    })
    expect((await page.request.delete(
      `/api/catalog/tables/${encodeURIComponent(registeredTable.id)}?${query}`)).ok()).toBe(true)
  }
})

async function assertSyntaxFeedback(page: Page, viewportWidth: number) {
  const canvasId = `transform-syntax-${viewportWidth}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  const graph = {
    id: canvasId, name: 'Transform syntax feedback', version: 1, requirements: [], edges: [],
    nodes: [{
      id: 'transform', type: 'transform', position: { x: 240, y: 160 },
      data: { title: 'Syntax feedback', status: 'draft', config: {
        source: 'adhoc', mode: 'map',
        code: 'def helper(row):\n    return row\ndef fn(row)\n    return row',
        onError: 'raise',
      } },
    }],
  }
  const created = await page.request.post('/api/canvas', { data: graph })
  expect(created.ok(), await created.text()).toBe(true)
  try {
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=transform`)
    await page.getByRole('button', { name: 'Edit code' }).last().click()
    await page.getByRole('button', { name: 'Example rows', exact: true }).click()
    const fixture = page.getByRole('textbox', { name: 'Example rows JSON' })
    await fixture.fill('[{"value":1}]')
    await page.getByRole('button', { name: 'Test code' }).click()
    await expect(page.getByText('Fix the Python syntax')).toBeVisible()
    await expect(page.getByText("Line 3: expected ':'")).toBeVisible()
    await expect(page.getByRole('button', { name: 'Test again' })).toHaveCount(0)
    const editor = page.locator('.monaco-editor')
    await expect(editor).toBeVisible()
    await expect(editor).toHaveAttribute('data-cursor-line-number', '3')
  } finally {
    await page.goto('about:blank')
    expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBe(true)
  }
}

for (const viewport of [{ width: 1280, height: 720 }, { width: 1440, height: 900 }]) {
  test.describe(`Transform syntax feedback at ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport })

    test('identifies and focuses the editable line', async ({ page }) => {
      await assertSyntaxFeedback(page, viewport.width)
    })
  })
}
