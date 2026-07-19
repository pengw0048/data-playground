import { randomUUID } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

type WriteReceipt = {
  datasetId: string
  revisionId: string
  schema: Array<{ name: string }>
}

type ExactBase = {
  uri: string
  tableId: string
  datasetId: string
  revisionId: string
}

type RunStatus = {
  runId: string
  status: string
  error?: string | null
  outputs: Array<{ nodeId?: string; uri?: string; writeReceipt?: WriteReceipt | null }>
}

async function json<T>(response: { ok(): boolean; status(): number; text(): Promise<string>; json(): Promise<unknown> }, label: string): Promise<T> {
  expect(response.ok(), `${label}: ${response.status()} ${await response.text()}`).toBeTruthy()
  return response.json() as Promise<T>
}

async function bootstrapManagedBase(page: Page, canvasId: string, filename: string): Promise<ExactBase> {
  const graph = {
    id: canvasId, name: 'Issue 585 managed-local bootstrap', version: 1, requirements: [],
    nodes: [
      { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'Starter events', config: { uri: 'events' } } },
      { id: 'select', type: 'select', position: { x: 350, y: 120 }, data: { title: 'Full-width base', config: { select: 'id, event AS untouched_text, amount AS untouched_numeric, event AS replace_me' } } },
      { id: 'write', type: 'write', position: { x: 620, y: 120 }, data: { title: filename, config: { filename, writeMode: 'overwrite' } } },
    ],
    edges: [
      { id: 'source-select', source: 'source', target: 'select' },
      { id: 'select-write', source: 'select', target: 'write' },
    ],
  }
  await json(await page.request.post('/api/canvas', { data: graph }), 'save managed-local base canvas')
  const submissionId = randomUUID()
  const admission = await json<{ intent: unknown }>(await page.request.post('/api/run/write-admission', {
    data: { graph, nodeId: 'write', submissionId },
  }), 'admit managed-local base')
  const started = await json<RunStatus>(await page.request.post('/api/run', {
    data: { graph, targetNodeId: 'write', confirmed: true, submissionId, writeIntent: admission.intent },
  }), 'write managed-local base')
  await expect.poll(async () => {
    const status = await json<RunStatus>(await page.request.get(`/api/run/${encodeURIComponent(started.runId)}`), 'load managed-local base')
    if (status.status === 'failed') throw new Error(status.error ?? 'managed-local base failed')
    return status.status
  }, { timeout: 20_000 }).toBe('done')
  const done = await json<RunStatus>(await page.request.get(`/api/run/${encodeURIComponent(started.runId)}`), 'read managed-local base receipt')
  const output = done.outputs.find((item) => item.nodeId === 'write')
  if (!output?.uri) throw new Error(`managed-local base omitted catalog output: ${JSON.stringify(done)}`)
  const tables = await json<{ items: Array<{ id: string; uri: string }> }>(
    await page.request.get(`/api/catalog/tables?uris=${encodeURIComponent(output.uri)}`),
    'find managed-local base registration',
  )
  const table = tables.items.find((item) => item.uri === output.uri)
  if (!table) throw new Error('managed-local base was not registered')
  const revision = await json<{ datasetId: string; revisionId: string }>(
    await page.request.get(`/api/catalog/tables/${encodeURIComponent(table.id)}/revisions/resolve`),
    'resolve exact managed-local base revision',
  )
  return {
    uri: output.uri, tableId: table.id, ...revision,
  }
}

test('certifies the real Write Inspector merge journey and exact revision history @ux-smoke', async ({ page }) => {
  test.setTimeout(60_000)
  const stamp = Date.now()
  const bootstrapCanvasId = `issue-585-bootstrap-${stamp}`
  const canvasId = `issue-585-merge-${stamp}`
  const filename = `issue-585-full-width-${stamp}.parquet`
  let base: ExactBase | null = null
  let bootstrapCanvasSaved = false
  let mergeCanvasSaved = false
  let backendRestoreValue = ''
  let backendChanged = false
  try {
    const settings = await json<{ global?: { backend?: string } }>(
      await page.request.get('/api/settings'), 'load local execution setting',
    )
    backendRestoreValue = typeof settings.global?.backend === 'string' ? settings.global.backend : ''
    await json(await page.request.put('/api/settings', { data: {
      scope: 'global', key: 'backend', value: 'local-out-of-core',
    } }), 'select supported local execution')
    backendChanged = true
    // Bootstrap is intentionally ordinary managed-local product behavior. The merge itself below
    // is submitted only by the shipped Write Inspector -- no merge endpoint is mocked or called here.
    base = await bootstrapManagedBase(page, bootstrapCanvasId, filename)
    bootstrapCanvasSaved = true
    const mergeCanvas = {
      id: canvasId, name: 'Issue 585 exact merge canvas', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'Exact base', config: {
          uri: base.uri, tableId: base.tableId,
          datasetRef: { kind: 'exact', datasetId: base.datasetId, revisionId: base.revisionId },
        } } },
        { id: 'select', type: 'select', position: { x: 350, y: 120 }, data: { title: 'Replacement sidecar', config: {
          select: 'id, upper(replace_me) AS replacement, untouched_numeric + 100 AS addition',
        } } },
        { id: 'write', type: 'write', position: { x: 620, y: 120 }, data: { title: filename, config: {
          filename, writeMode: 'overwrite', mergeColumns: {
            identityColumns: ['id'], rules: [
              { source: 'replacement', target: 'replace_me', mode: 'replace' },
              { source: 'addition', target: 'added_numeric', mode: 'add' },
            ],
          },
        } } },
      ],
      edges: [
        { id: 'source-select', source: 'source', target: 'select' },
        { id: 'select-write', source: 'select', target: 'write' },
      ],
    }
    await json(await page.request.post('/api/canvas', { data: mergeCanvas }), 'save exact merge canvas')
    mergeCanvasSaved = true

    await page.goto(`/#/canvas/${canvasId}`)
    await page.locator('.react-flow__node[data-id="write"]').click()
    const inspector = page.getByTestId('inspector')
    await expect(inspector.getByLabel('Certified column merge')).toBeVisible()
    await inspector.getByRole('button', { name: 'Check eligibility' }).click()
    const preflight = inspector.getByLabel('Merge preflight')
    await expect(preflight).toContainText('Eligible exact merge')
    await expect(preflight).toContainText('replacement → replace_me (replace); addition → added_numeric (add)')
    await expect(preflight).toContainText('Output schema: id: int, untouched_text: string, untouched_numeric: float, replace_me: string, added_numeric: float')

    const submitted = page.waitForResponse((response) => response.url().endsWith('/api/merge-columns')
      && response.request().method() === 'POST')
    await inspector.getByRole('button', { name: 'Run column merge' }).click()
    const task = await json<{ taskId: string }>(await submitted, 'submit browser column merge')
    await expect(inspector.getByText('Published exact revision')).toBeVisible({ timeout: 20_000 })
    await inspector.getByRole('button', { name: 'Open in Jobs' }).click()

    await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible()
    const job = page.getByRole('button', { name: `Open run ${task.taskId} in Issue 585 exact merge canvas` })
    await expect(job).toBeVisible()
    await expect(page.getByText('Column merge:', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Open exact revision' }).click()
    await expect(page.getByLabel('Exact revision detail')).toContainText(`Parent ${base.revisionId}`)

    // Reopen exactly the immutable base and final results; these assertions intentionally use the
    // ordinary revision APIs, not an in-process storage path or a moving catalog head.
    const jobs = await json<{ items: Array<{ outputReceipt?: WriteReceipt | null }> }>(
      await page.request.get(`/api/jobs?runId=${encodeURIComponent(task.taskId)}&limit=1`),
      'reopen browser merge in Jobs API',
    )
    const final = jobs.items[0]?.outputReceipt
    expect(final).toBeTruthy()
    const baseDetail = await json<{ revisionId: string; preview: { columns: Array<{ name: string }>; rows: Array<Record<string, unknown>> } }>(
      await page.request.get(`/api/catalog/revisions/${encodeURIComponent(base.datasetId)}/${encodeURIComponent(base.revisionId)}`),
      'reopen exact base revision',
    )
    expect(baseDetail.revisionId).toBe(base.revisionId)
    expect(baseDetail.preview.columns.map((column) => column.name)).toEqual([
      'id', 'untouched_text', 'untouched_numeric', 'replace_me',
    ])
    const finalDetail = await json<{ revisionId: string; parentRevisionId: string; preview: { columns: Array<{ name: string }>; rows: Array<Record<string, unknown>> } }>(
      await page.request.get(`/api/catalog/revisions/${encodeURIComponent(final!.datasetId)}/${encodeURIComponent(final!.revisionId)}`),
      'reopen exact merged revision',
    )
    expect(finalDetail).toMatchObject({ revisionId: final!.revisionId, parentRevisionId: base.revisionId })
    expect(finalDetail.preview.columns.map((column) => column.name)).toEqual([
      'id', 'untouched_text', 'untouched_numeric', 'replace_me', 'added_numeric',
    ])
    const baseRow = baseDetail.preview.rows[0]!
    const finalRow = finalDetail.preview.rows[0]!
    expect(finalRow).toMatchObject({
      id: baseRow.id,
      untouched_text: baseRow.untouched_text,
      untouched_numeric: baseRow.untouched_numeric,
      replace_me: String(baseRow.replace_me).toUpperCase(),
      added_numeric: Number(baseRow.untouched_numeric) + 100,
    })
  } finally {
    try {
      if (mergeCanvasSaved) {
        expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBeTruthy()
      }
      if (bootstrapCanvasSaved) {
        expect((await page.request.delete(`/api/canvas/${encodeURIComponent(bootstrapCanvasId)}`)).ok()).toBeTruthy()
      }
      if (base) {
        // The merge legitimately advances this metadata CAS token. Reload it rather than using
        // bootstrap-era preconditions, then remove the one current logical registration.
        const current = await json<{
          id: string; uri: string; registrationId?: string | null; metadataRevision?: string | null
        }>(await page.request.get(`/api/catalog/tables/${encodeURIComponent(base.tableId)}`),
          'reload issue-585 dataset unregister preconditions')
        expect(current.id).toBe(base.tableId)
        if (!current.registrationId || !current.metadataRevision) {
          throw new Error('issue-585 dataset omitted unregister preconditions')
        }
        const removed = await json<{ ok: true }>(await page.request.delete(
          `/api/catalog/tables/${encodeURIComponent(base.tableId)}`, { params: {
            expected_registration_id: current.registrationId,
            expected_revision: current.metadataRevision,
          } }), 'unregister managed-local issue-585 dataset')
        expect(removed).toEqual({ ok: true })
        const active = await json<{ items: Array<{ uri: string }> }>(
          await page.request.get(`/api/catalog/tables?uris=${encodeURIComponent(current.uri)}`),
          'verify issue-585 dataset is no longer active',
        )
        expect(active.items).not.toContainEqual(expect.objectContaining({ uri: current.uri }))
        const revision = await page.request.get(
          `/api/catalog/tables/${encodeURIComponent(base.tableId)}/revisions/resolve`,
        )
        expect(revision.status()).toBe(404)
      }
    } finally {
      if (backendChanged) {
        // There is no setting-delete endpoint. An empty backend is the product's canonical
        // unset selection and preserves the original fallback semantics when the key was absent.
        await json(await page.request.put('/api/settings', { data: {
          scope: 'global', key: 'backend', value: backendRestoreValue,
        } }), 'restore global backend selection')
        const restored = await json<{ global?: { backend?: unknown } }>(
          await page.request.get('/api/settings'), 'verify restored global backend selection',
        )
        expect(restored.global?.backend ?? '').toBe(backendRestoreValue)
      }
    }
  }
})
