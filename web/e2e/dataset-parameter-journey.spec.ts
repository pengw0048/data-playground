import { randomUUID } from 'node:crypto'
import { expect, test, type APIResponse, type Locator, type Page } from '@playwright/test'

type Dataset = { tableId: string; name: string; datasetId: string; revisionId: string }
type RunStatus = {
  runId: string; status: string; error?: string | null
  outputs: Array<{ nodeId?: string; uri?: string }>
}
type Binding = { kind: 'latest' | 'exact'; datasetId: string; revisionId?: string }
type SavedCanvas = {
  parameters: Array<{ name: string; default?: Binding }>
  nodes: Array<{ id: string; data: { config: unknown } }>
}

async function json<T>(response: APIResponse, label: string): Promise<T> {
  expect(response.ok(), `${label}: ${response.status()} ${await response.text()}`).toBe(true)
  return response.json() as Promise<T>
}

// Publish through the real managed Write contract; no private catalog or revision mutations.
async function publish(page: Page, canvasId: string, filename: string, predicate: string): Promise<Dataset> {
  const graph = {
    id: canvasId, name: 'Dataset parameter fixture publication', version: 1, requirements: [],
    nodes: [
      { id: 'source', type: 'source', position: { x: 80, y: 160 }, data: { title: 'Starter events', config: { uri: 'events' } } },
      { id: 'filter', type: 'filter', position: { x: 360, y: 160 }, data: { title: 'Fixture rows', config: { predicate } } },
      { id: 'write', type: 'write', position: { x: 640, y: 160 }, data: { title: filename, config: { filename, writeMode: 'overwrite' } } },
    ],
    edges: [{ id: 'source-filter', source: 'source', target: 'filter' }, { id: 'filter-write', source: 'filter', target: 'write' }],
  }
  await json(await page.request.post('/api/canvas', { data: graph }), 'save publication Canvas')
  const submissionId = randomUUID()
  const admission = await json<{ intent: unknown }>(await page.request.post('/api/run/write-admission', {
    data: { graph, nodeId: 'write', submissionId },
  }), 'admit publication')
  const started = await json<RunStatus>(await page.request.post('/api/run', {
    data: { graph, targetNodeId: 'write', confirmed: true, submissionId,
      writeIntent: admission.intent, confirmedWriteIntent: admission.intent },
  }), 'publish fixture')
  let done: RunStatus = started
  await expect.poll(async () => {
    done = await json<RunStatus>(await page.request.get(`/api/run/${encodeURIComponent(started.runId)}`), 'poll publication')
    if (done.status === 'failed') throw new Error(done.error ?? 'Fixture publication failed')
    return done.status
  }, { timeout: 30_000 }).toBe('done')
  const uri = done.outputs.find((output) => output.nodeId === 'write')?.uri
  expect(uri).toBeTruthy()
  const tables = await json<{ items: Array<{ id: string; name: string; uri: string }> }>(
    await page.request.get('/api/catalog/tables', { params: { uris: uri! } }), 'find published dataset')
  const table = tables.items.find((item) => item.uri === uri)!
  expect(table).toBeTruthy()
  const revision = await json<{ datasetId: string; revisionId: string }>(
    await page.request.get(`/api/catalog/tables/${encodeURIComponent(table.id)}/revisions/resolve`), 'resolve published version')
  return { tableId: table.id, name: table.name, ...revision }
}

async function chooseDataset(scope: Locator, label: string, dataset: Dataset): Promise<void> {
  await scope.getByRole('textbox', { name: `${label} dataset search`, exact: true }).fill(dataset.name)
  await scope.getByRole('button', { name: `Choose dataset ${dataset.name}`, exact: true }).click()
  await expect(scope.getByLabel(`${label} dataset binding`, { exact: true }).locator('strong').first()).toHaveText(dataset.name)
}

test('chooses named dataset defaults and overrides, pins a version, and follows a later publication', async ({ page }, testInfo) => {
  test.setTimeout(150_000)
  const stamp = Date.now()
  const canvasId = `dataset-parameter-journey-${stamp}`
  const alphaFile = `Parameter alpha ${stamp}.parquet`
  const betaFile = `Parameter beta ${stamp}.parquet`
  const canvases: string[] = []
  const datasets = new Map<string, Dataset>()
  const evidence: unknown[] = []
  const sourceConfig = { datasetRef: { parameterRef: 'input' } }
  const panel = page.getByTestId('panel-run')
  const publishFixture = async (suffix: string, filename: string, predicate: string) => {
    const id = `${canvasId}-${suffix}`
    canvases.push(id)
    const dataset = await publish(page, id, filename, predicate)
    datasets.set(dataset.tableId, dataset)
    return dataset
  }
  const savedCanvas = () => page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
    .then((response) => json<SavedCanvas>(response, 'read saved parameter Canvas'))
  const execute = async (dataset: Dataset, binding: Binding | undefined, ids: number[]) => {
    // Observe the ordinary UI submission; identities are not supplied by the test to the run API.
    let submitted = false
    const started = page.waitForResponse((response) => {
      const match = response.url().endsWith('/api/run') && response.request().method() === 'POST'
      if (match) submitted = true
      return match
    })
    await panel.getByRole('button', { name: 'Continue', exact: true }).click()
    const action = panel.getByRole('button', { name: /^(?:Run|Run with unknown row count|Run [\d,]+ rows)$/ })
    await expect.poll(async () => submitted || await action.isVisible()).toBe(true)
    if (!submitted) await action.click()
    const response = await started
    expect(response.ok(), await response.text()).toBe(true)
    const request = response.request().postDataJSON() as {
      targetNodeId: string; graph: SavedCanvas; parameterBindings?: Array<{ name: string; value: Binding }>
    }
    expect(request.targetNodeId).toBe('result')
    expect(request.graph.nodes.find((node) => node.id === 'source')?.data.config).toEqual(sourceConfig)
    expect(request.parameterBindings ?? []).toEqual(binding ? [{ name: 'input', value: binding }] : [])
    const { runId } = await response.json() as { runId: string }
    await expect(panel.getByText('DONE', { exact: true })).toBeVisible({ timeout: 30_000 })
    const sample = await json<{ rowCount: number; rows: Array<{ id: number }> }>(
      await page.request.post(`/api/run/${encodeURIComponent(runId)}/sample`, {
        data: { nodeId: 'result', portId: 'out', k: 50, offset: 0 },
      }), 'read actual selected input result')
    expect(sample.rowCount).toBe(ids.length)
    expect(sample.rows.map((row) => Number(row.id)).sort((a, b) => a - b)).toEqual(ids)
    let saved: { id: string; runId: string } | undefined
    await expect.poll(async () => {
      const history = await json<Array<{ id: string; runId: string }>>(
        await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}/runs`), 'load durable run')
      saved = history.find((run) => run.runId === runId)
      return !!saved
    }).toBe(true)
    const manifest = await json<{ availability: string; document: {
      admittedInputs: Array<{ nodeId: string; datasetId: string; revisionId: string }>
      parameters: Array<{ name: string; type: string; value: Binding & { resolvedRevisionId?: string } }>
    } }>(await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}/runs/${encodeURIComponent(saved!.id)}/manifest`), 'read admitted input version')
    expect(manifest.availability).toBe('available')
    expect(manifest.document.admittedInputs).toContainEqual(expect.objectContaining({
      nodeId: 'source', datasetId: dataset.datasetId, revisionId: dataset.revisionId,
    }))
    const expectedValue = binding?.kind === 'exact' ? binding
      : { kind: 'latest', datasetId: dataset.datasetId, resolvedRevisionId: dataset.revisionId }
    expect(manifest.document.parameters).toContainEqual(expect.objectContaining({ name: 'input', type: 'dataset', value: expectedValue }))
    const current = await savedCanvas()
    expect(current.nodes.find((node) => node.id === 'source')?.data.config).toEqual(sourceConfig)
    evidence.push({ runId, selection: binding ?? 'declared default', dataset, ids, parameters: manifest.document.parameters })
  }

  try {
    const alpha = await publishFixture('alpha-first', alphaFile, 'id < 3')
    const beta = await publishFixture('beta', betaFile, 'id >= 100 AND id < 104')
    canvases.push(canvasId)
    await json(await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Choose inputs for the same Canvas', version: 1, requirements: [],
      parameters: [{ name: 'input', type: 'dataset', label: 'Input data', required: false }],
      nodes: [
        { id: 'source', type: 'source', position: { x: 100, y: 160 }, data: { title: 'Chosen input', config: sourceConfig } },
        { id: 'result', type: 'select', position: { x: 440, y: 160 }, data: { title: 'Selected rows', config: { select: 'id, event, amount' } } },
      ],
      edges: [{ id: 'source-result', source: 'source', sourceHandle: 'out', target: 'result', targetHandle: 'in', data: { wire: 'dataset' } }],
    } }), 'save parameterized Canvas without a baked-in source URI')
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=result`)
    await page.getByTestId('app-menu').click()
    await page.getByRole('menuitem', { name: 'Canvas settings…' }).click()
    const settings = page.getByRole('dialog', { name: 'Canvas settings' })
    await settings.getByRole('checkbox', { name: 'Default', exact: true }).check()
    await chooseDataset(settings, 'input default', alpha)
    await settings.getByRole('combobox', { name: 'input default selection', exact: true }).selectOption('latest')
    await settings.getByRole('button', { name: 'Close', exact: true }).click()
    const defaultValue: Binding = { kind: 'latest', datasetId: alpha.datasetId }
    await expect.poll(async () => (await savedCanvas()).parameters.find((item) => item.name === 'input')?.default)
      .toEqual(defaultValue)

    const run = page.getByTestId('inspector').getByRole('button', { name: 'Run', exact: true })
    await expect(run).toBeEnabled()
    await run.click()
    await expect(panel.getByText('Using declared default.', { exact: true })).toBeVisible()
    await expect(panel.getByLabel('Input data dataset binding', { exact: true }).locator('strong').first()).toHaveText(alpha.name)
    await execute(alpha, undefined, [0, 1, 2])

    await panel.getByRole('button', { name: 'Edit parameters', exact: true }).click()
    await panel.getByRole('button', { name: 'Override default', exact: true }).click()
    await chooseDataset(panel, 'Input data', beta)
    await panel.getByRole('combobox', { name: 'Input data selection', exact: true }).selectOption('latest')
    await execute(beta, { kind: 'latest', datasetId: beta.datasetId }, [100, 101, 102, 103])

    await panel.getByRole('button', { name: 'Edit parameters', exact: true }).click()
    await chooseDataset(panel, 'Input data', alpha)
    await panel.getByRole('combobox', { name: 'Input data selection', exact: true }).selectOption('exact')
    await panel.getByRole('combobox', { name: 'Input data version', exact: true }).selectOption(alpha.revisionId)
    const fixed: Binding = { kind: 'exact', datasetId: alpha.datasetId, revisionId: alpha.revisionId }
    await execute(alpha, fixed, [0, 1, 2])

    const newer = await publishFixture('alpha-later', alphaFile, 'id >= 10 AND id < 15')
    expect(newer.datasetId).toBe(alpha.datasetId)
    expect(newer.revisionId).not.toBe(alpha.revisionId)
    await panel.getByRole('button', { name: 'Edit parameters', exact: true }).click()
    await expect(panel.getByRole('combobox', { name: 'Input data selection', exact: true })).toHaveValue('exact')
    await expect(panel.getByRole('combobox', { name: 'Input data version', exact: true })).toHaveValue(alpha.revisionId)
    await execute(alpha, fixed, [0, 1, 2])

    await panel.getByRole('button', { name: 'Edit parameters', exact: true }).click()
    await panel.getByRole('combobox', { name: 'Input data selection', exact: true }).selectOption('latest')
    await execute(newer, defaultValue, [10, 11, 12, 13, 14])
    expect((await savedCanvas()).parameters.find((item) => item.name === 'input')?.default).toEqual(defaultValue)
    await testInfo.attach('dataset-parameter-journey', { body: JSON.stringify(evidence, null, 2), contentType: 'application/json' })
  } finally {
    // An interrupted worker has already disposed its browser and request context.
    if (!page.isClosed()) {
      await page.goto('about:blank')
      for (const id of canvases.reverse()) {
        await json(await page.request.delete(`/api/canvas/${encodeURIComponent(id)}`, { timeout: 10_000 }), 'remove fixture Canvas')
      }
      for (const dataset of datasets.values()) {
        const current = await json<{ registrationId: string; metadataRevision: string }>(
          await page.request.get(`/api/catalog/tables/${encodeURIComponent(dataset.tableId)}`), 'load unregister preconditions')
        await json(await page.request.delete(`/api/catalog/tables/${encodeURIComponent(dataset.tableId)}`, {
          params: { expected_registration_id: current.registrationId, expected_revision: current.metadataRevision }, timeout: 10_000,
        }), 'unregister fixture dataset')
      }
    }
  }
})
