import { expect, test } from '@playwright/test'

async function json<T>(response: { ok(): boolean; status(): number; text(): Promise<string>; json(): Promise<unknown> }, label: string): Promise<T> {
  expect(response.ok(), `${label}: ${response.status()} ${await response.text()}`).toBeTruthy()
  return response.json() as Promise<T>
}

test('a real managed Write opens its exact Job from Canvas and after reload @ux-smoke', async ({ page }) => {
  test.setTimeout(60_000)
  await page.setViewportSize({ width: 1024, height: 768 })
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
    await expect(inspector.getByLabel('Write admission')).toContainText('create · managed-local-file')
    const runResponse = page.waitForResponse((response) => response.url().endsWith('/api/run')
      && response.request().method() === 'POST')
    await inspector.getByRole('button', { name: 'Run', exact: true }).click()
    const started = await json<{ runId: string; status: 'queued' | 'running' | 'done' }>(await runResponse, 'start real managed Write')
    const runId = started.runId
    await expect.poll(async () => {
      const job = await json<{ items: Array<{ status: string }> }>(
        await page.request.get(`/api/jobs?run_id=${encodeURIComponent(runId)}&limit=1`), 'read real managed Job')
      return job.items[0]?.status
    }, { timeout: 30_000 }).toBe('done')

    await page.locator('.react-flow__node[data-id="write"]').click()
    await page.locator('.react-flow__node[data-id="write"]').getByRole('button', { name: 'More' }).click()
    await page.getByText('Run details', { exact: true }).click()
    await expect(page.getByTestId('panel-run').getByRole('button', { name: 'View in Jobs' })).toBeVisible()
    await page.getByTestId('panel-run').getByRole('button', { name: 'View in Jobs' }).click()
    await expect(page).toHaveURL(new RegExp(`#\\/jobs\\?run=${runId}$`))

    await page.goto(`/#/canvas/${canvasId}`)
    await page.reload()
    await page.getByTestId('app-menu').click()
    await page.getByText('Run history', { exact: true }).click()
    const history = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: 'Run history' }) })
    await expect(history.getByRole('button', { name: 'View in Jobs' })).toHaveCount(1)
    await history.getByRole('button', { name: 'View in Jobs' }).click()
    await expect(page).toHaveURL(new RegExp(`#\\/jobs\\?run=${runId}$`))

    // The real owner-scoped Inbox projection is deliberately tiny: it carries only the human
    // output name and exact row count, never the receipt/path/revision identity used by Jobs.
    const inbox = await json<{ items: Array<{
      taskId: string; completedWrite?: { outputName: string; rowCount: number }; [key: string]: unknown
    }> }>(await page.request.get('/api/inbox?filter=unread'), 'read real Inbox outcome')
    const outcome = inbox.items.find((item) => item.taskId === runId)
    expect(outcome?.completedWrite?.outputName).toBeTruthy()
    expect(outcome?.completedWrite?.rowCount).toBeGreaterThanOrEqual(0)
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
  await page.reload()
  await expect(page.getByTestId('canvas-inbox-unread-badge')).toHaveText('2')
  await page.getByTestId('canvas-inbox-unread-badge').click()
  await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
  await page.getByRole('button', { name: 'Mark read' }).click()
  await page.goto(`/#/canvas/${canvasId}`)
  await expect(page.getByTestId('canvas-inbox-unread-badge')).toHaveText('1')
})
