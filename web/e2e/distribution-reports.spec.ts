import { randomUUID } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

type DatasetView = { id: string; name: string; definitionSha256: string }
type ReportEnvelope = { reportId: string; task: { id: string; status: string } }
type ReportEstimate = {
  estimatedScanRows: number | null
  estimatedScanBytes: number | null
  needsConfirmation: boolean
  reason: 'unknown_size' | 'large_scan' | null
}
type RegisteredDataset = {
  id: string
  registrationId: string
  metadataRevision: string
  uri: string
}
type WriteReceipt = {
  datasetId: string
  revisionId: string
  schema: Array<{ name: string }>
  publication: { logicalUri: string }
}
type RunStatus = {
  runId: string
  status: string
  error?: string | null
  outputs: Array<{ writeReceipt?: WriteReceipt | null }>
}

async function json<T>(response: { ok(): boolean; status(): number; text(): Promise<string>; json(): Promise<unknown> }, label: string): Promise<T> {
  expect(response.ok(), `${label}: ${response.status()} ${await response.text()}`).toBeTruthy()
  return response.json() as Promise<T>
}

async function openView(page: Page, view: DatasetView) {
  await page.goto(`/#/workspace/${encodeURIComponent(`dataset_view:${view.id}`)}`)
  await expect(page.getByRole('dialog', { name: view.name })).toBeVisible()
  await expect(page.getByText('Distribution reports', { exact: true })).toBeVisible()
}

async function createManagedRevision(page: Page, canvasId: string, filename: string, sourceUri = 'events'): Promise<WriteReceipt> {
  const graph = {
    id: canvasId,
    name: 'Issue 430 managed revision fixture',
    version: 1,
    requirements: [],
    nodes: [
      { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'Fixture source', config: { uri: sourceUri } } },
      { id: 'write', type: 'write', position: { x: 380, y: 120 }, data: { title: filename, config: { filename, writeMode: 'overwrite' } } },
    ],
    edges: [{ id: 'source-write', source: 'source', target: 'write' }],
  }
  await json(await page.request.post('/api/canvas', { data: graph }), 'save managed-local fixture canvas')
  const submissionId = randomUUID()
  const admission = await json<{ managed: boolean; provider: string; intent: unknown }>(
    await page.request.post('/api/run/write-admission', { data: {
      graph, nodeId: 'write', submissionId,
    } }),
    'admit managed-local fixture write',
  )
  expect(admission).toMatchObject({ managed: true, provider: 'managed-local-file' })
  expect(admission.intent).toBeTruthy()

  const started = await json<RunStatus>(await page.request.post('/api/run', { data: {
    graph, targetNodeId: 'write', confirmed: true, submissionId, writeIntent: admission.intent,
  } }), 'start managed-local fixture write')
  await expect.poll(async () => {
    const status = await json<RunStatus>(
      await page.request.get(`/api/run/${encodeURIComponent(started.runId)}`),
      'load managed-local fixture write',
    )
    if (status.status === 'failed') throw new Error(status.error ?? 'managed-local fixture write failed')
    return status.status
  }, { timeout: 20_000 }).toBe('done')
  const completed = await json<RunStatus>(
    await page.request.get(`/api/run/${encodeURIComponent(started.runId)}`),
    'load completed managed-local fixture write',
  )
  const receipt = completed.outputs.find((output) => output.writeReceipt)?.writeReceipt
  expect(receipt).toBeTruthy()
  return receipt!
}

async function openTerminalReport(page: Page, reportId: string, viewName: string) {
  await page.getByRole('link', { name: 'Open report' }).first().click()
  await expect(page).toHaveURL(new RegExp(`#\/distribution-reports\/${reportId}$`))
  await expect(page.getByRole('heading', { name: viewName })).toBeVisible()
  await expect(page.getByText('done', { exact: true })).toBeVisible({ timeout: 20_000 })
  await expect(page.getByText('Coverage before distributions')).toBeVisible()
  await expect(page.getByText(/complete for this view/)).toBeVisible()

  await page.goto(`/#/distribution-reports/${encodeURIComponent(reportId)}`)
  await expect(page).toHaveURL(new RegExp(`#\/distribution-reports\/${reportId}$`))
  await expect(page.getByRole('heading', { name: viewName })).toBeVisible()
  await expect(page.getByText('done', { exact: true })).toBeVisible()
  await expect(page.getByText('Coverage before distributions')).toBeVisible()
}

test('runs known-small and confirmed retained reports, then reopens the exact terminal deep link', async ({ page }) => {
  test.setTimeout(60_000)
  const viewName = `Issue 430 exact distributions ${Date.now()}`
  const fixtureCanvasId = `issue-430-fixture-${Date.now()}`
  let view: DatasetView | null = null
  let largeView: DatasetView | null = null
  let largeSource: RegisteredDataset | null = null
  let previousBackend = ''
  try {
    const settings = await json<{ global?: { backend?: string } }>(
      await page.request.get('/api/settings'), 'load execution settings',
    )
    previousBackend = settings.global?.backend ?? ''
    await json(await page.request.put('/api/settings', { data: {
      scope: 'global', key: 'backend', value: 'local-out-of-core',
    } }), 'select local fixture backend')
    const revision = await createManagedRevision(
      page, fixtureCanvasId, `issue-430-distribution-${Date.now()}.parquet`,
    )
    view = await json<DatasetView>(await page.request.post('/api/dataset-views', { data: {
      submissionId: randomUUID(),
      name: viewName,
      datasetRef: { kind: 'exact', datasetId: revision.datasetId, revisionId: revision.revisionId },
      selectedColumns: revision.schema.map((column) => column.name),
      predicate: null,
      sampling: { kind: 'all' },
    } }), 'create exact DatasetView')

    await openView(page, view)
    const knownSmallSubmission = page.waitForResponse((response) =>
      response.url().endsWith(`/api/dataset-views/${view!.id}/distribution-reports`)
      && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Inspect distributions' }).click()
    const knownSmallResponse = await knownSmallSubmission
    const knownSmall = await json<ReportEnvelope>(knownSmallResponse, 'submit known-small report')
    expect(JSON.parse(knownSmallResponse.request().postData() ?? '{}')).toMatchObject({ confirmed: false })
    await expect(page.getByRole('dialog', { name: 'Confirm distribution report' })).toHaveCount(0)
    await openTerminalReport(page, knownSmall.reportId, viewName)

    largeSource = await json<RegisteredDataset>(await page.request.post('/api/catalog/upload', {
      headers: {
        'X-Upload-Filename': `issue-430-large-${Date.now()}.csv`,
        'Content-Type': 'text/csv',
      },
      data: `value\n${'1\n'.repeat(1_000_001)}`,
    }), 'upload large retained source')
    const largeRevision = await createManagedRevision(
      page,
      `${fixtureCanvasId}-large`,
      `issue-430-distribution-large-${Date.now()}.parquet`,
      largeSource.uri,
    )
    const largeViewName = `${viewName} confirmation`
    largeView = await json<DatasetView>(await page.request.post('/api/dataset-views', { data: {
      submissionId: randomUUID(),
      name: largeViewName,
      datasetRef: { kind: 'exact', datasetId: largeRevision.datasetId, revisionId: largeRevision.revisionId },
      selectedColumns: largeRevision.schema.map((column) => column.name),
      predicate: null,
      sampling: { kind: 'all' },
    } }), 'create large exact DatasetView')

    await openView(page, largeView)
    const largeEstimateResponse = page.waitForResponse((response) =>
      response.url().endsWith(`/api/dataset-views/${largeView!.id}/distribution-reports/estimate`)
      && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Inspect distributions' }).click()
    const largeEstimate = await json<ReportEstimate>(await largeEstimateResponse, 'estimate large retained report')
    expect(largeEstimate).toMatchObject({
      estimatedScanRows: 1_000_001,
      needsConfirmation: true,
      reason: 'large_scan',
    })
    await expect(page.getByRole('dialog', { name: 'Confirm distribution report' })).toContainText('exceeds the confirmation scan threshold')
    const confirmedSubmission = page.waitForResponse((response) =>
      response.url().endsWith(`/api/dataset-views/${largeView!.id}/distribution-reports`)
      && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Confirm and start' }).click()
    const confirmedResponse = await confirmedSubmission
    const confirmed = await json<ReportEnvelope>(confirmedResponse, 'submit confirmed report')
    expect(JSON.parse(confirmedResponse.request().postData() ?? '{}')).toMatchObject({ confirmed: true })
    await openTerminalReport(page, confirmed.reportId, largeViewName)
  } finally {
    if (view) await page.request.delete(`/api/dataset-views/${encodeURIComponent(view.id)}`)
    if (largeView) await page.request.delete(`/api/dataset-views/${encodeURIComponent(largeView.id)}`)
    await page.request.delete(`/api/canvas/${encodeURIComponent(fixtureCanvasId)}`)
    await page.request.delete(`/api/canvas/${encodeURIComponent(`${fixtureCanvasId}-large`)}`)
    if (largeSource) await page.request.delete(`/api/catalog/tables/${encodeURIComponent(largeSource.id)}`, { params: {
      expected_registration_id: largeSource.registrationId,
      expected_revision: largeSource.metadataRevision,
    } })
    await page.request.put('/api/settings', { data: {
      scope: 'global', key: 'backend', value: previousBackend,
    } })
  }
})
