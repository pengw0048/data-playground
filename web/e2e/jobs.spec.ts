import { expect, test, type Page } from '@playwright/test'

const failedJob = {
  id: 'history-failed', runId: 'run-failed', jobType: 'run', status: 'failed',
  canvasId: 'canvas-jobs', canvasName: 'Climate analysis', targetNodeId: 'publish',
  nodeLabel: 'Publish results', backend: 'local', placement: 'local', attempt: 'run-failed',
  rows: null, ms: 1200, error: 'destination unavailable', outputs: [],
  executionManifestSha256: 'a'.repeat(64), executionManifestSchemaVersion: 1,
  executionManifestAvailability: 'available', executionManifestReconstructable: true,
  createdAt: '2026-07-16T12:00:00Z',
}

const jobFilterLabels = [
  'Filter jobs by status',
  'Filter jobs by canvas',
  'Filter jobs by node',
  'Filter jobs by backend',
  'Filter jobs from time',
  'Filter jobs to time',
  'Filter jobs by text',
]

async function expectJobsFiltersToFit(page: Page) {
  const filters = page.getByLabel('Job filters')
  await expect(filters).toBeVisible()
  const viewport = page.viewportSize()
  if (!viewport) throw new Error('viewport is unavailable')
  const boxes = await Promise.all(jobFilterLabels.map(async (label) => {
    const control = page.getByLabel(label, { exact: true })
    await expect(control, `${label} should remain visible`).toBeVisible()
    const box = await control.boundingBox()
    if (!box) throw new Error(`${label} has no bounding box`)
    expect(box.width, `${label} should remain usable`).toBeGreaterThan(0)
    expect(box.x + box.width, `${label} should not overflow the viewport`).toBeLessThanOrEqual(viewport.width + 0.5)
    return { label, ...box }
  }))
  for (let left = 0; left < boxes.length; left += 1) {
    for (let right = left + 1; right < boxes.length; right += 1) {
      const a = boxes[left]
      const b = boxes[right]
      const intersects = a.x < b.x + b.width && b.x < a.x + a.width
        && a.y < b.y + b.height && b.y < a.y + a.height
      expect(intersects, `${a.label} overlaps ${b.label}`).toBe(false)
    }
  }
  expect(await filters.evaluate((element) => element.scrollWidth <= element.clientWidth), 'Job filters should not require horizontal scrolling').toBe(true)
}

test('filters, deep-links, and preserves a partial Jobs page at the supported viewport @ux-smoke', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 })
  let continuationAttempts = 0
  await page.route('**/api/canvas', async (route) => {
    await route.fulfill({ json: [{ id: 'canvas-jobs', name: 'Climate analysis', version: 1, role: 'viewer' }] })
  })
  await page.route('**/api/jobs?*', async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get('cursor')
    if (cursor) {
      continuationAttempts += 1
      if (continuationAttempts === 1) {
        await route.fulfill({ status: 503, json: { detail: 'history store temporarily unavailable' } })
        return
      }
      await route.fulfill({ json: {
        items: [{ ...failedJob, id: 'history-older', runId: 'run-older', attempt: 'run-older', createdAt: '2026-07-15T12:00:00Z' }],
        nextCursor: null, hasMore: false,
      } })
      return
    }
    await route.fulfill({ json: { items: [failedJob], nextCursor: 'opaque-next', hasMore: true } })
  })
  await page.route('**/api/canvas/canvas-jobs/runs/history-failed/manifest', async (route) => {
    await route.fulfill({ json: {
      sha256: 'a'.repeat(64), schemaVersion: 1, availability: 'available',
      document: {
        schemaVersion: 1,
        graph: { nodes: [{ id: 'publish', type: 'write', data: { config: {} } }], edges: [], requirements: [] },
        target: { nodeId: 'publish', portId: null }, admittedInputs: [],
        writeIntent: { mode: 'create', destination: { name: 'results' } },
        descriptors: { core: { apiVersion: '1' }, nodes: [], plugins: [] },
      },
    } })
  })

  await page.goto('/#/jobs')
  await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open run run-failed in Climate analysis', expanded: false })).toBeVisible()
  for (const width of [1024, 1280, 1440]) {
    await page.setViewportSize({ width, height: 720 })
    await expectJobsFiltersToFit(page)
  }
  await page.getByLabel('Filter jobs by canvas', { exact: true }).selectOption('canvas-jobs')
  await expect(page).toHaveURL(/canvas=canvas-jobs/)
  await page.getByLabel('Filter jobs by node', { exact: true }).selectOption(JSON.stringify(['canvas-jobs', 'publish']))
  await expect(page).toHaveURL(/canvas=canvas-jobs&node=publish/)
  await page.getByLabel('Filter jobs by backend', { exact: true }).selectOption('local')
  await expect(page).toHaveURL(/backend=local/)
  await page.getByLabel('Filter jobs by node', { exact: true }).selectOption('')
  await page.getByLabel('Filter jobs by canvas', { exact: true }).selectOption('')
  await page.getByLabel('Filter jobs by backend', { exact: true }).selectOption('')
  await page.getByLabel('Filter jobs by status').selectOption('failed')
  await expect(page).toHaveURL(/#\/jobs\?status=failed/)

  await page.getByRole('button', { name: 'Open run run-failed in Climate analysis', expanded: false }).click()
  await expect(page.getByRole('alert')).toContainText('destination unavailable')
  await expect(page.getByRole('link', { name: 'Open node' })).toHaveAttribute(
    'href', '#/canvas/canvas-jobs?node=publish')
  await expect(page).toHaveURL(/run=run-failed/)
  await page.getByRole('button', { name: /Execution manifest/ }).click()
  await expect(page.getByText('Submitted graph')).toBeVisible()
  await expect(page.getByText('No declared parameter bindings were recorded.')).toBeVisible()
  await page.goBack()
  await expect(page).toHaveURL(/#\/jobs\?status=failed$/)
  await page.getByRole('button', { name: 'Open run run-failed in Climate analysis', expanded: false }).click()
  await page.reload()
  await expect(page.getByRole('alert')).toContainText('destination unavailable')

  await page.getByRole('button', { name: 'Load more' }).click()
  await expect(page.getByText(/Couldn’t load more Jobs/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open run run-failed in Climate analysis' })).toBeVisible()
  await page.getByRole('button', { name: 'Retry load more' }).click()
  await expect(page.getByText('run-older')).toBeVisible()
})

test('reopens a certified column merge from Jobs and opens only its exact published revision @ux-smoke', async ({ page }) => {
  const mergeJob = {
    id: 'merge-task-1', runId: 'merge-task-1', taskId: 'merge-task-1', jobType: 'run', status: 'done',
    canvasId: 'canvas-merge', canvasName: 'Column enrichment', targetNodeId: 'write',
    nodeLabel: 'Write enrichment', backend: 'local', placement: 'local', attempt: 'merge-task-1',
    rows: null, ms: 20, outputs: [], taskAttempts: [], canRetry: false, canCancel: false,
    mergeColumns: { phase: 'done', baseDatasetId: 'dataset-1', baseRevisionId: 'rev-base', candidate: 'committed', reused: false, candidateRows: 2, candidateBytes: 120, canRetry: false, canCancel: false },
    outputReceipt: { datasetId: 'dataset-1', revisionId: 'rev-published', rows: 2, bytes: 120, durable: true, head: { datasetId: 'dataset-1', revisionId: 'rev-published', retentionOwner: 'core' }, schema: [], partitions: [], publication: { provider: 'managed-local-file', logicalUri: 'managed://dataset-1', artifactUri: 'redacted', publishSequence: 1, idempotencyKey: 'merge-task-1' } },
    createdAt: '2026-07-19T12:00:00Z', updatedAt: '2026-07-19T12:01:00Z',
  }
  await page.route('**/api/canvas', async (route) => route.fulfill({ json: [{ id: 'canvas-merge', name: 'Column enrichment', version: 1, role: 'editor' }] }))
  await page.route('**/api/jobs?*', async (route) => route.fulfill({ json: { items: [mergeJob], nextCursor: null, hasMore: false } }))
  await page.route('**/api/canvas/canvas-merge/runs/merge-task-1/manifest', async (route) => route.fulfill({ json: { availability: 'not_recorded' } }))
  await page.route('**/api/catalog/revisions/dataset-1/rev-published', async (route) => route.fulfill({ json: {
    datasetId: 'dataset-1', revisionId: 'rev-published', committedAt: '2026-07-19T12:01:00Z', retentionOwner: 'core', parentRevisionId: 'rev-base', producerOperation: 'merge-columns',
    summary: { rowCount: 2, dataFileCount: 1, totalBytes: 120, fragmentCount: 1 }, preview: { columns: [{ name: 'id', type: 'BIGINT' }, { name: 'score', type: 'DOUBLE' }], rows: [{ id: 1, score: 0.8 }], hasMore: true, rowLimit: 100 },
  } }))

  await page.goto('/#/jobs')
  await expect(page.getByRole('button', { name: 'Open run merge-task-1 in Column enrichment' })).toBeVisible()
  await page.getByRole('button', { name: 'Open run merge-task-1 in Column enrichment' }).click()
  await expect(page.getByText('Column merge:', { exact: true })).toBeVisible()
  await expect(page.getByText('rev-published')).toBeVisible()
  await page.getByRole('button', { name: 'Open exact revision' }).click()
  await expect(page.getByLabel('Exact revision detail')).toContainText('Parent rev-base')
  await expect(page.getByText('Preview is bounded; this remains the exact published revision.')).toBeVisible()
})
