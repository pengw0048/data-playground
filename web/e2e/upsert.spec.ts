import { randomUUID } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

type WriteReceipt = { datasetId: string; revisionId: string; schema: Array<{ name: string }> }
type ExactBase = { uri: string; tableId: string; datasetId: string; revisionId: string; filename: string }
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

// Ordinary managed-local product behavior: build one exact revision from a bounded events slice.
async function bootstrapManagedBase(page: Page, canvasId: string, filename: string, predicate: string): Promise<ExactBase> {
  const graph = {
    id: canvasId, name: 'Issue 638 managed-local bootstrap', version: 1, requirements: [],
    nodes: [
      { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'Starter events', config: { uri: 'events' } } },
      { id: 'filter', type: 'filter', position: { x: 300, y: 120 }, data: { title: 'Slice', config: { predicate } } },
      { id: 'select', type: 'select', position: { x: 520, y: 120 }, data: { title: 'Base columns', config: { select: 'id, event AS value' } } },
      { id: 'write', type: 'write', position: { x: 740, y: 120 }, data: { title: filename, config: { filename, writeMode: 'overwrite' } } },
    ],
    edges: [
      { id: 'source-filter', source: 'source', target: 'filter' },
      { id: 'filter-select', source: 'filter', target: 'select' },
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
    await page.request.get(`/api/catalog/tables?uris=${encodeURIComponent(output.uri)}`), 'find managed-local base registration')
  const table = tables.items.find((item) => item.uri === output.uri)
  if (!table) throw new Error('managed-local base was not registered')
  const revision = await json<{ datasetId: string; revisionId: string }>(
    await page.request.get(`/api/catalog/tables/${encodeURIComponent(table.id)}/revisions/resolve`), 'resolve exact managed-local base revision')
  return { uri: output.uri, tableId: table.id, filename, ...revision }
}

async function unregister(page: Page, dataset: ExactBase, label: string): Promise<void> {
  const current = await json<{ id: string; uri: string; registrationId?: string | null; metadataRevision?: string | null }>(
    await page.request.get(`/api/catalog/tables/${encodeURIComponent(dataset.tableId)}`), `reload ${label} unregister preconditions`)
  if (!current.registrationId || !current.metadataRevision) throw new Error(`${label} omitted unregister preconditions`)
  const removed = await json<{ ok: true }>(await page.request.delete(
    `/api/catalog/tables/${encodeURIComponent(dataset.tableId)}`,
    { params: { expected_registration_id: current.registrationId, expected_revision: current.metadataRevision } }), `unregister ${label}`)
  expect(removed).toEqual({ ok: true })
}

test('certifies the real Write Inspector keyed-upsert journey and exact revision history @ux-smoke', async ({ page }) => {
  test.setTimeout(60_000)
  const stamp = Date.now()
  const targetCanvasId = `issue-638-target-${stamp}`
  const payloadCanvasId = `issue-638-payload-${stamp}`
  const canvasId = `issue-638-upsert-${stamp}`
  const targetFile = `issue-638-target-${stamp}.parquet`
  const payloadFile = `issue-638-payload-${stamp}.parquet`
  let target: ExactBase | null = null
  let payload: ExactBase | null = null
  const saved: string[] = []
  let backendRestoreValue = ''
  let backendChanged = false
  try {
    const settings = await json<{ global?: { backend?: string } }>(await page.request.get('/api/settings'), 'load local execution setting')
    backendRestoreValue = typeof settings.global?.backend === 'string' ? settings.global.backend : ''
    await json(await page.request.put('/api/settings', { data: { scope: 'global', key: 'backend', value: 'local-out-of-core' } }), 'select supported local execution')
    backendChanged = true
    // Target head ids {0,1,2}; payload ids {2,3,4}: keyed on id → 1 matched, 2 inserted, 2 unchanged.
    target = await bootstrapManagedBase(page, targetCanvasId, targetFile, 'id < 3')
    saved.push(targetCanvasId)
    payload = await bootstrapManagedBase(page, payloadCanvasId, payloadFile, 'id >= 2 AND id < 5')
    saved.push(payloadCanvasId)

    const upsertCanvas = {
      id: canvasId, name: 'Issue 638 keyed upsert canvas', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'Exact payload', config: {
          uri: payload.uri, tableId: payload.tableId,
          datasetRef: { kind: 'exact', datasetId: payload.datasetId, revisionId: payload.revisionId },
        } } },
        { id: 'write', type: 'write', position: { x: 420, y: 120 }, data: { title: targetFile, config: {
          filename: targetFile, writeMode: 'overwrite', keyedUpsert: { keys: ['id'] },
        } } },
      ],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    }
    await json(await page.request.post('/api/canvas', { data: upsertCanvas }), 'save keyed upsert canvas')
    saved.push(canvasId)

    await page.goto(`/#/canvas/${canvasId}`)
    await page.locator('.react-flow__node[data-id="write"]').click()
    const inspector = page.getByTestId('inspector')
    const control = inspector.getByLabel('Certified keyed upsert')
    await expect(control).toBeVisible()
    await control.getByRole('button', { name: 'Check eligibility' }).click()
    const projection = control.getByLabel('Upsert projection')
    await expect(projection).toContainText('Eligible keyed upsert')
    await expect(projection).toContainText('1 matched · 2 inserted · 2 unchanged')
    await expect(projection).toContainText('Output schema: id: int, value: string')

    const submitted = page.waitForResponse((response) => response.url().endsWith('/api/catalog/upsert')
      && response.request().method() === 'POST')
    await control.getByRole('button', { name: 'Run keyed upsert' }).click()
    await json<{ taskId: string }>(await submitted, 'submit browser keyed upsert')
    await expect(control.getByText('Published exact revision')).toBeVisible({ timeout: 20_000 })
    await expect(control).toContainText('1 matched · 2 inserted · 2 unchanged')
    await control.getByRole('button', { name: 'Open exact revision' }).click()
    await expect(control.getByLabel('Exact revision detail')).toContainText(`Parent ${target.revisionId}`)

    // Reopen exactly the immutable base and upserted head through the ordinary revision APIs.
    const finalHead = await json<{ datasetId: string; revisionId: string }>(
      await page.request.get(`/api/catalog/tables/${encodeURIComponent(target.tableId)}/revisions/resolve`), 'resolve upserted head')
    expect(finalHead.revisionId).not.toBe(target.revisionId)
    const finalDetail = await json<{ revisionId: string; parentRevisionId: string; preview: { columns: Array<{ name: string }>; rows: Array<Record<string, unknown>> } }>(
      await page.request.get(`/api/catalog/revisions/${encodeURIComponent(finalHead.datasetId)}/${encodeURIComponent(finalHead.revisionId)}`), 'reopen upserted revision')
    expect(finalDetail).toMatchObject({ revisionId: finalHead.revisionId, parentRevisionId: target.revisionId })
    expect(finalDetail.preview.columns.map((column) => column.name)).toEqual(['id', 'value'])
    // Base {0,1,2} upserted with payload {2,3,4} → the five-key union.
    const ids = finalDetail.preview.rows.map((row) => Number(row.id)).sort((a, b) => a - b)
    expect(ids).toEqual([0, 1, 2, 3, 4])
  } finally {
    try {
      for (const id of saved.reverse()) {
        expect((await page.request.delete(`/api/canvas/${encodeURIComponent(id)}`)).ok()).toBeTruthy()
      }
      if (target) await unregister(page, target, 'issue-638 target dataset')
      if (payload) await unregister(page, payload, 'issue-638 payload dataset')
    } finally {
      if (backendChanged) {
        await json(await page.request.put('/api/settings', { data: { scope: 'global', key: 'backend', value: backendRestoreValue } }), 'restore global backend selection')
      }
    }
  }
})
