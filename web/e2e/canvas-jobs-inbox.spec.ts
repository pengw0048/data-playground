import { expect, test, type Locator, type Page } from '@playwright/test'

async function json<T>(response: { ok(): boolean; status(): number; text(): Promise<string>; json(): Promise<unknown> }, label: string): Promise<T> {
  expect(response.ok(), `${label}: ${response.status()} ${await response.text()}`).toBeTruthy()
  return response.json() as Promise<T>
}

async function expectFullyInsideCanvas(page: Page, node: Locator): Promise<void> {
  const [nodeBox, canvasBox] = await Promise.all([
    node.boundingBox(), page.locator('.react-flow').boundingBox(),
  ])
  expect(nodeBox, 'selected node has a bounding box').not.toBeNull()
  expect(canvasBox, 'Canvas has a bounding box').not.toBeNull()
  expect(nodeBox!.x).toBeGreaterThanOrEqual(canvasBox!.x)
  expect(nodeBox!.y).toBeGreaterThanOrEqual(canvasBox!.y)
  expect(nodeBox!.x + nodeBox!.width).toBeLessThanOrEqual(canvasBox!.x + canvasBox!.width)
  expect(nodeBox!.y + nodeBox!.height).toBeLessThanOrEqual(canvasBox!.y + canvasBox!.height)
}

test('a real managed Write retains one bounded cross-surface evidence chain @ux-smoke', async ({ page }) => {
  test.setTimeout(60_000)
  await page.setViewportSize({ width: 1280, height: 720 })
  const stamp = Date.now()
  const canvasId = `canvas-jobs-inbox-${stamp}`
  const filename = `canvas-jobs-inbox-${stamp}.parquet`
  const canvas = {
    id: canvasId, name: 'Canvas Jobs and Inbox', version: 1, requirements: [], nodes: [
      { id: 'source', type: 'source', position: { x: 80, y: 80 }, data: { title: 'Starter events', status: 'idle', config: { uri: 'events' } } },
      { id: 'select', type: 'select', position: { x: 320, y: 80 }, data: { title: 'Projection', status: 'idle', config: { select: 'id, user_id, amount' } } },
      { id: 'write', type: 'write', position: { x: 560, y: 80 }, data: { title: 'Managed Write', status: 'idle', config: { filename, writeMode: 'overwrite' } } },
    ], edges: [{ id: 'source-select', source: 'source', target: 'select' }, { id: 'select-write', source: 'select', target: 'write' }],
  }
  try {
    const created = await page.request.post('/api/canvas', { data: canvas })
    expect(created.ok()).toBe(true)

    await page.goto(`/#/canvas/${canvasId}`)
    await page.getByTestId('app-menu').click()
    await expect(page.getByText('Jobs', { exact: true })).toBeVisible()
    await expect(page.getByText('Inbox', { exact: true })).toBeVisible()
    await expect(page.getByText('Run history', { exact: true })).toBeVisible()
    await page.getByText('Jobs', { exact: true }).click()
    await expect(page).toHaveURL(/#\/jobs$/)
    await page.goto(`/#/canvas/${canvasId}`)
    await page.getByTestId('app-menu').click()
    await page.getByText('Inbox', { exact: true }).click()
    await expect(page).toHaveURL(/#\/inbox$/)
    await page.goto(`/#/canvas/${canvasId}`)

    await page.locator('.react-flow__node[data-id="write"]').click()
    const inspector = page.getByTestId('inspector')
    const expandInspector = inspector.getByRole('button', { name: 'Expand Inspector' })
    if (await expandInspector.isVisible()) await expandInspector.click()
    await inspector.getByRole('button', { name: /Change destination/ }).click()
    const dialog = page.locator('.dp-modal-overlay')
    await dialog.locator('input').fill(filename)
    await dialog.getByRole('button', { name: 'Save', exact: true }).click()
    const publication = inspector.getByLabel('Write publication')
    await expect(publication.getByText('Publication mode').locator('..')).toContainText('Create a new dataset')
    await expect(publication.getByLabel('Write readiness')).toContainText('Ready to publish')
    const publicationDetails = publication.locator('details')
    await expect(publicationDetails).not.toHaveAttribute('open')
    await publicationDetails.locator('summary').click()
    await expect(publicationDetails).toContainText(/Admission:.*node write.*mode create/)
    await expect(publicationDetails).toContainText('managed-local-file')
    const runResponse = page.waitForResponse((response) => response.url().endsWith('/api/run')
      && response.request().method() === 'POST')
    await inspector.getByRole('button', { name: 'Run', exact: true }).click()
    const started = await json<{ runId: string; status: 'queued' | 'running' | 'done' }>(await runResponse, 'start real managed Write')
    const runId = started.runId
    type Input = { node_id: string; dataset_id: string; revision_id: string; provider: string }
    type Receipt = { datasetId: string; revisionId: string; rows: number }
    type Job = { runId: string; status: string; targetNodeId: string; inputManifest: Input[]; outputReceipt: Receipt | null }
    let job: Job | null = null
    await expect.poll(async () => {
      const jobs = await json<{ items: Job[] }>(
        await page.request.get(`/api/jobs?run_id=${encodeURIComponent(runId)}&limit=1`), 'read real managed Job')
      const matches = jobs.items.filter((item) => item.runId === runId)
      if (matches.length !== 1) return `matched ${matches.length} Jobs records`
      const [exactJob] = matches
      job = exactJob ?? null
      return job.status
    }, { timeout: 30_000 }).toBe('done')
    expect(job).not.toBeNull()
    expect(job!.targetNodeId).toBe('write')
    expect(job!.outputReceipt).not.toBeNull()
    expect(job!.inputManifest).toEqual([
      expect.objectContaining({ node_id: 'source', dataset_id: expect.any(String), revision_id: expect.any(String), provider: expect.any(String) }),
    ])
    const [admittedInput] = job!.inputManifest
    expect(admittedInput).toBeTruthy()

    await page.locator('.react-flow__node[data-id="write"]').click()
    await page.locator('.react-flow__node[data-id="write"]').getByRole('button', { name: 'More' }).click()
    await page.getByText('Run details', { exact: true }).click()
    await expect(page.getByTestId('panel-run').getByRole('button', { name: 'View in Jobs' })).toBeVisible()
    await page.getByTestId('panel-run').getByRole('button', { name: 'View in Jobs' }).click()
    await expect(page).toHaveURL(new RegExp(`#\\/jobs\\?run=${runId}$`))
    const jobRow = page.getByRole('button', { name: `Open run ${runId} in Canvas Jobs and Inbox` })
    await expect(jobRow).toHaveAttribute('aria-expanded', 'true')
    const openNode = page.getByRole('link', { name: 'Open node' })
    await expect(openNode).toHaveAttribute('href', `#/canvas/${canvasId}?node=write`)
    await openNode.click()
    await expect(page).toHaveURL(new RegExp(`#\\/canvas/${canvasId}\\?node=write$`))
    const selectedWrite = page.locator('.react-flow__node.selected[data-id="write"]')
    await expect(selectedWrite).toBeVisible()
    await expect(selectedWrite).toBeInViewport({ ratio: 1 })
    await expectFullyInsideCanvas(page, selectedWrite)

    await page.goto(`/#/canvas/${canvasId}`)
    await page.reload()
    await page.getByTestId('app-menu').click()
    await page.getByText('Run history', { exact: true }).click()
    const history = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: 'Run history' }) })
    await expect(history.getByRole('button', { name: 'View in Jobs' })).toHaveCount(1)
    await history.getByRole('button', { name: /Admitted inputs/ }).click()
    await expect(history.getByText(`Exact revision ${admittedInput!.revision_id}`)).toBeVisible()
    await history.getByRole('button', { name: 'View in Jobs' }).click()
    await expect(page).toHaveURL(new RegExp(`#\\/jobs\\?run=${runId}$`))

    // The real owner-scoped Inbox projection is deliberately tiny: it carries only the human
    // output name and exact row count, never the receipt/path/revision identity used by Jobs.
    const inbox = await json<{ items: Array<{
      taskId: string; completedWrite?: { outputName: string; rowCount: number }; [key: string]: unknown
    }> }>(await page.request.get('/api/inbox?filter=unread'), 'read real Inbox outcome')
    const outcomes = inbox.items.filter((item) => item.taskId === runId)
    expect(outcomes).toHaveLength(1)
    const [outcome] = outcomes
    expect(outcome?.completedWrite?.outputName).toBeTruthy()
    expect(outcome?.completedWrite?.rowCount).toBe(job!.outputReceipt!.rows)
    expect(outcome).not.toHaveProperty('outputReceipt')
    expect(outcome).not.toHaveProperty('executionManifestSha256')
    await page.goto('/#/inbox?filter=unread')
    await expect(page.getByText(
      `“${outcome!.completedWrite!.outputName}” written · ${outcome!.completedWrite!.rowCount} rows`,
    )).toBeVisible()
  } finally { /* no shared execution settings are changed by this journey */ }
})

test('Canvas Inbox count omits failures and refreshes from confirmed mark-read state @ux-smoke', async ({ page }) => {
  const canvasId = 'canvas-inbox-count'
  const created = await page.request.post('/api/canvas', { data: {
    id: canvasId, name: 'Inbox count', version: 1,
    nodes: [{ id: 'source', type: 'source', position: { x: 80, y: 80 }, data: { title: 'Source', status: 'idle', config: { uri: 'events' } } }], edges: [],
  } })
  expect(created.ok()).toBe(true)
  let unread: number | 'failure' = 'failure'
  await page.route('**/api/inbox/unread-count', (route) => unread === 'failure'
    ? route.fulfill({ status: 503, json: { detail: 'unavailable' } })
    : route.fulfill({ json: { count: unread } }))
  await page.route('**/api/inbox?*', (route) => route.fulfill({ json: { items: [{
    id: 'inbox-item', taskId: 'managed-write-terminal', canvasId, canvasName: 'Inbox count',
    taskKind: 'managed_local_write', outcome: 'completed', terminalAt: '2026-07-21T19:00:00Z', readAt: null, jobAvailable: true,
  }], nextCursor: null, hasMore: false } }))
  await page.route('**/api/inbox/inbox-item/read', (route) => {
    unread = 1
    return route.fulfill({ json: {
      id: 'inbox-item', taskId: 'managed-write-terminal', canvasId, canvasName: 'Inbox count',
      taskKind: 'managed_local_write', outcome: 'completed', terminalAt: '2026-07-21T19:00:00Z', readAt: '2026-07-21T19:01:00Z', jobAvailable: true,
    } })
  })

  await page.goto(`/#/canvas/${canvasId}`)
  await expect(page.getByTestId('canvas-inbox-unread-badge')).toHaveCount(0)
  unread = 2
  let releaseCanvas!: () => void
  const canvasHydration = new Promise<void>((resolve) => { releaseCanvas = resolve })
  await page.route(`**/api/canvas/${canvasId}`, async (route) => {
    await canvasHydration
    await route.continue()
  })
  await page.reload()
  await expect(page.getByTestId('canvas-inbox-unread-badge')).toHaveText('2')
  await page.getByTestId('canvas-inbox-unread-badge').click()
  await expect(page).toHaveURL(/#\/inbox$/)
  releaseCanvas()
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  await page.getByRole('button', { name: 'Mark read' }).click()
  await expect(page).toHaveURL(/#\/inbox$/)
  await page.unroute(`**/api/canvas/${canvasId}`)
  await page.goto(`/#/canvas/${canvasId}`)
  await expect(page.getByTestId('canvas-inbox-unread-badge')).toHaveText('1')
})
