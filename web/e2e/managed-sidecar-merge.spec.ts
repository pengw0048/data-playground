import { randomUUID } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

type Exact = { uri: string; tableId: string; datasetId: string; revisionId: string }
type Run = { runId: string; status: string; error?: string | null; outputs: Array<{ nodeId?: string; uri?: string }> }

async function json<T>(response: { ok(): boolean; status(): number; text(): Promise<string>; json(): Promise<unknown> }, label: string): Promise<T> {
  expect(response.ok(), `${label}: ${response.status()} ${await response.text()}`).toBeTruthy()
  return response.json() as Promise<T>
}

async function publish(page: Page, graph: any, filename: string): Promise<Exact> {
  await json(await page.request.post('/api/canvas', { data: graph }), `save ${filename} canvas`)
  const submissionId = randomUUID()
  const admission = await json<{ intent: unknown }>(await page.request.post('/api/run/write-admission', {
    data: { graph, nodeId: 'write', submissionId },
  }), `admit ${filename}`)
  const started = await json<Run>(await page.request.post('/api/run', {
    data: { graph, targetNodeId: 'write', confirmed: true, submissionId, writeIntent: admission.intent },
  }), `run ${filename}`)
  await expect.poll(async () => {
    const run = await json<Run>(await page.request.get(`/api/run/${encodeURIComponent(started.runId)}`), `read ${filename} task`)
    if (run.status === 'failed') throw new Error(run.error ?? `${filename} failed`)
    return run.status
  }, { timeout: 20_000 }).toBe('done')
  const done = await json<Run>(await page.request.get(`/api/run/${encodeURIComponent(started.runId)}`), `read ${filename} receipt`)
  const uri = done.outputs.find((output) => output.nodeId === 'write')?.uri
  if (!uri) throw new Error(`${filename} omitted its managed-local output`)
  const catalog = await json<{ items: Array<{ id: string; uri: string }> }>(await page.request.get(`/api/catalog/tables?uris=${encodeURIComponent(uri)}`), `find ${filename} registration`)
  const table = catalog.items.find((item) => item.uri === uri)
  if (!table) throw new Error(`${filename} was not registered`)
  const revision = await json<{ datasetId: string; revisionId: string }>(await page.request.get(`/api/catalog/tables/${encodeURIComponent(table.id)}/revisions/resolve`), `resolve ${filename}`)
  return { uri, tableId: table.id, ...revision }
}

async function remove(page: Page, exact: Exact | null) {
  if (!exact) return
  const table = await json<{ registrationId?: string; metadataRevision?: string }>(await page.request.get(`/api/catalog/tables/${encodeURIComponent(exact.tableId)}`), 'reload temporary registration')
  if (!table.registrationId || !table.metadataRevision) throw new Error('temporary registration omitted CAS preconditions')
  await json(await page.request.delete(`/api/catalog/tables/${encodeURIComponent(exact.tableId)}`, { params: {
    expected_registration_id: table.registrationId, expected_revision: table.metadataRevision,
  } }), 'remove temporary registration')
}

test('uses the installed sidecar fixture through the Write inspector and reopens exact results @ux-smoke', async ({ page }) => {
  test.setTimeout(60_000)
  const stamp = Date.now()
  const baseCanvas = `issue-769-base-${stamp}`
  const sidecarCanvas = `issue-769-sidecar-${stamp}`
  const mergeCanvas = `issue-769-merge-${stamp}`
  let base: Exact | null = null
  let sidecar: Exact | null = null
  try {
    const plugins = await json<Array<{ name: string; state: string }>>(await page.request.get('/api/plugins'), 'list installed plugins')
    expect(plugins).toContainEqual(expect.objectContaining({ name: 'dp-sidecar-fixture', state: 'active' }))

    base = await publish(page, { id: baseCanvas, name: 'managed-sidecar base', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'events', config: { uri: 'events' } } },
        { id: 'select', type: 'select', position: { x: 200, y: 0 }, data: { title: 'base columns', config: { select: 'id, event, CAST(amount AS DOUBLE) AS replace_me' } } },
        { id: 'write', type: 'write', position: { x: 400, y: 0 }, data: { title: 'base', config: { filename: `issue-769-base-${stamp}.parquet`, writeMode: 'overwrite' } } },
      ], edges: [{ id: 'a', source: 'source', target: 'select' }, { id: 'b', source: 'select', target: 'write' }],
    }, `issue-769-base-${stamp}.parquet`)

    sidecar = await publish(page, { id: sidecarCanvas, name: 'installed sidecar fixture', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'exact base', config: { uri: base.uri, datasetRef: { kind: 'exact', datasetId: base.datasetId, revisionId: base.revisionId } } } },
        { id: 'derive', type: 'derive_sidecar_column', position: { x: 200, y: 0 }, data: { title: 'derive sidecar column', config: { identity: 'id', value: 'replace_me', output: 'replacement', outputSchema: [{ name: 'id', type: 'int', rowReference: { target: { kind: 'exact', datasetId: base.datasetId, revisionId: base.revisionId }, keyFields: ['id'], semanticType: 'row', provenance: 'declared' } }, { name: 'replacement', type: 'float' }] } } },
        { id: 'write', type: 'write', position: { x: 400, y: 0 }, data: { title: 'sidecar', config: { filename: `issue-769-sidecar-${stamp}.parquet`, writeMode: 'overwrite' } } },
      ], edges: [{ id: 'a', source: 'source', target: 'derive' }, { id: 'b', source: 'derive', target: 'write' }],
    }, `issue-769-sidecar-${stamp}.parquet`)

    const graph = { id: mergeCanvas, name: 'managed sidecar browser journey', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'exact fixture sidecar', config: { uri: sidecar.uri, datasetRef: { kind: 'exact', datasetId: sidecar.datasetId, revisionId: sidecar.revisionId } } } },
        { id: 'write', type: 'write', position: { x: 420, y: 120 }, data: { title: 'merge fixture output', config: { managedSidecarMerge: { identityColumns: [], rules: [] } } } },
      ], edges: [{ id: 'direct-sidecar', source: 'source', target: 'write' }],
    }
    await json(await page.request.post('/api/canvas', { data: graph }), 'save merge canvas')
    await page.goto(`/#/canvas/${mergeCanvas}`)
    const write = page.locator('.react-flow__node[data-id="write"]')
    await expect(write).toBeVisible()
    await write.click({ position: { x: 40, y: 20 } })
    const inspector = page.getByTestId('inspector')
    await expect(inspector.getByLabel('Managed sidecar column merge')).toBeVisible()
    await inspector.getByLabel('Search destination bases').fill(`issue-769-base-${stamp}`)
    await inspector.getByRole('button', { name: new RegExp(`issue-769-base-${stamp}`) }).click()
    await inspector.getByRole('button', { name: 'Use id' }).click()
    await inspector.getByRole('button', { name: 'Add suggested rules' }).click()
    await inspector.getByLabel('Managed sidecar mode 1').selectOption('replace')
    await inspector.getByLabel('Managed sidecar target column 1').fill('replace_me')
    await inspector.getByRole('button', { name: 'Check eligibility' }).click()
    await expect(inspector.getByLabel('Managed sidecar preflight')).toContainText('Eligible exact sidecar merge')
    const submitted = page.waitForResponse((response) => response.url().endsWith('/api/managed-sidecar-merge') && response.request().method() === 'POST')
    await inspector.getByRole('button', { name: 'Start managed merge' }).click()
    const task = await json<{ taskId: string }>(await submitted, 'submit browser managed sidecar merge')
    await expect(inspector.getByRole('link', { name: 'Open published child' })).toBeVisible({ timeout: 20_000 })
    await expect(page.getByText(/saved$/)).toBeVisible()
    await inspector.getByRole('button', { name: 'Open in Jobs' }).click()
    await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible()
    await expect(page.getByRole('button', { name: new RegExp(`Open run ${task.taskId} in Column merge`) })).toBeVisible()
    const jobs = await json<{ items: Array<{ outputReceipt?: { datasetId: string; revisionId: string } }> }>(
      await page.request.get(`/api/jobs?run_id=${encodeURIComponent(task.taskId)}&limit=1`), 'read managed merge receipt',
    )
    const child = jobs.items[0]?.outputReceipt
    if (!child) throw new Error('managed merge omitted an exact publication receipt')
    const [baseRevision, sidecarRevision, childRevision] = await Promise.all([
      json<{ preview: { rows: Array<Record<string, unknown>> } }>(await page.request.get(`/api/catalog/revisions/${encodeURIComponent(base.datasetId)}/${encodeURIComponent(base.revisionId)}`), 'read exact base'),
      json<{ preview: { columns: Array<{ name: string }> } }>(await page.request.get(`/api/catalog/revisions/${encodeURIComponent(sidecar.datasetId)}/${encodeURIComponent(sidecar.revisionId)}`), 'read exact sidecar'),
      json<{ parentRevisionId: string; preview: { rows: Array<Record<string, unknown>> } }>(await page.request.get(`/api/catalog/revisions/${encodeURIComponent(child.datasetId)}/${encodeURIComponent(child.revisionId)}`), 'read exact child'),
    ])
    expect(sidecarRevision.preview.columns.map((column) => column.name)).toEqual(['id', 'replacement'])
    expect(childRevision.parentRevisionId).toBe(base.revisionId)
    const childById = new Map(childRevision.preview.rows.map((row) => [row.id, row]))
    expect(childById.size).toBe(baseRevision.preview.rows.length)
    for (const baseRow of baseRevision.preview.rows) {
      expect(childById.get(baseRow.id)).toMatchObject({
        id: baseRow.id, event: baseRow.event, replace_me: Number(baseRow.replace_me) * 2,
      })
    }
    await page.goto(`/#/canvas/${mergeCanvas}`)
    await expect(page.locator('.react-flow__node[data-id="write"]')).toBeVisible()
    await page.locator('.react-flow__node[data-id="write"]').click({ position: { x: 40, y: 20 } })
    const reopened = page.getByTestId('inspector')
    const childLink = reopened.getByRole('link', { name: 'Open published child' })
    const baseLink = reopened.getByRole('link', { name: 'Open exact base' })
    await expect(childLink).toBeVisible()
    await expect(baseLink).toBeVisible()
    const childHref = await childLink.getAttribute('href')
    const baseHref = await baseLink.getAttribute('href')
    if (!childHref || !baseHref) throw new Error('exact revision links omitted their routes')
    await page.goto(childHref)
    await expect.poll(() => new URL(page.url()).hash).toContain(`revision=${child.revisionId}`)
    await expect.poll(() => new URL(page.url()).hash).toContain(`revisionDataset=${child.datasetId}`)
    await page.reload()
    await expect(page.getByTestId('revision-detail')).toContainText(child.revisionId)
    await page.goto(baseHref)
    await expect.poll(() => new URL(page.url()).hash).toContain(`revision=${base.revisionId}`)
    await expect.poll(() => new URL(page.url()).hash).toContain(`revisionDataset=${base.datasetId}`)
    await page.reload()
    await expect(page.getByTestId('revision-detail')).toContainText(base.revisionId)
  } finally {
    await page.request.delete(`/api/canvas/${encodeURIComponent(mergeCanvas)}`)
    await page.request.delete(`/api/canvas/${encodeURIComponent(sidecarCanvas)}`)
    await page.request.delete(`/api/canvas/${encodeURIComponent(baseCanvas)}`)
    await remove(page, sidecar)
    await remove(page, base)
  }
})
