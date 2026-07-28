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
  await expect(page.getByRole('heading', { name: 'Runs and background tasks' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open run run-failed in Climate analysis', expanded: false })).toBeVisible()
  await page.getByText('Advanced filters', { exact: true }).click()
  for (const width of [1024, 1280, 1440]) {
    await page.setViewportSize({ width, height: 720 })
    await expectJobsFiltersToFit(page)
  }
  await page.setViewportSize({ width: 1280, height: 720 })
  await expect(page.getByRole('button', { name: /retired-identity-task/ })).toHaveCount(0)
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
  await page.getByText('Technical evidence', { exact: true }).click()
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
  await expect(page.getByRole('button', { name: 'Open run run-older in Climate analysis' })).toBeVisible()
})

test('a completed Jobs row stays concise and opens human-named retained results @ux-smoke', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  const completed = {
    ...failedJob,
    id: 'history-complete',
    runId: 'run-complete',
    status: 'done',
    progress: 1,
    rows: 1,
    error: null,
    outputs: [
      {
        nodeId: 'transform', portId: 'clean', portLabel: 'Clean rows', wire: 'dataset',
        publicationKind: 'result', outcome: 'committed', uri: 'file:///clean.parquet', rows: 1,
      },
      {
        nodeId: 'transform', portId: 'rejected', portLabel: 'Rejected rows', wire: 'dataset',
        publicationKind: 'result', outcome: 'committed', uri: 'file:///rejected.parquet', rows: 0,
      },
    ],
  }
  await page.route('**/api/canvas', async (route) => route.fulfill({
    json: [{ id: 'canvas-jobs', name: 'Climate analysis', version: 1, role: 'viewer' }],
  }))
  await page.route('**/api/jobs?*', async (route) => route.fulfill({
    json: { items: [completed], nextCursor: null, hasMore: false },
  }))

  await page.goto('/#/jobs')
  const row = page.getByRole('button', {
    name: 'Open run run-complete in Climate analysis', expanded: false,
  })
  await expect(row).toContainText('done')
  await expect(row).toContainText('2 outputs available')
  await expect(row).toContainText('1 row')
  await expect(row).not.toContainText('100%')
  await expect(row).not.toContainText('●')

  await row.click()
  await expect(page.getByRole('button', { name: 'Open result 1' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open result 2' })).toBeVisible()
  await expect(page.getByText(/transform:clean/)).toBeHidden()
  await page.getByText('Technical evidence', { exact: true }).click()
  await expect(page.getByText(/Result 1 · transform:clean, Result 2 · transform:rejected/)).toBeVisible()
})

test('long shared-prefix Canvas names keep their suffixes visible without widening Jobs @ux-smoke', async ({ page }) => {
  const prefix = 'Robotics preprocessing experiment with shared discovery and filtering context — '
  const leftName = `${prefix}left-camera-pass`
  const rightName = `${prefix}right-camera-pass`
  const unicodePrefix = '多语言研究画布😀共享前缀数据清洗实验阶段一二三四五六七八九十—'
  const unicodeLeftName = `${unicodePrefix}左侧相机`
  const unicodeRightName = `${unicodePrefix}右侧相机`
  const combiningName = `${'a'.repeat(10)}e\u0301${'x'.repeat(17)}`
  const zwjName = `${'a'.repeat(10)}👩🏽‍🔬${'x'.repeat(17)}`
  const jobs = [
    { ...failedJob, id: 'history-left', runId: 'run-left', canvasId: 'canvas-left', canvasName: leftName },
    { ...failedJob, id: 'history-right', runId: 'run-right', canvasId: 'canvas-right', canvasName: rightName },
    { ...failedJob, id: 'history-unicode-left', runId: 'run-unicode-left', canvasId: 'canvas-unicode-left', canvasName: unicodeLeftName },
    { ...failedJob, id: 'history-unicode-right', runId: 'run-unicode-right', canvasId: 'canvas-unicode-right', canvasName: unicodeRightName },
    { ...failedJob, id: 'history-combining', runId: 'run-combining', canvasId: 'canvas-combining', canvasName: combiningName },
    { ...failedJob, id: 'history-zwj', runId: 'run-zwj', canvasId: 'canvas-zwj', canvasName: zwjName },
  ]
  await page.route('**/api/canvas', async (route) => route.fulfill({
    json: jobs.map((job) => ({
      id: job.canvasId, name: job.canvasName, version: 1, role: 'viewer',
    })),
  }))
  await page.route('**/api/jobs?*', async (route) => route.fulfill({
    json: { items: jobs, nextCursor: null, hasMore: false },
  }))

  await page.goto('/#/jobs')
  const left = page.getByTitle(leftName)
  const right = page.getByTitle(rightName)
  const unicodeLeft = page.getByTitle(unicodeLeftName)
  const unicodeRight = page.getByTitle(unicodeRightName)
  const combining = page.getByTitle(combiningName)
  const zwj = page.getByTitle(zwjName)
  await expect(left).toHaveText(leftName)
  await expect(right).toHaveText(rightName)
  expect(Array.from(unicodeLeftName)).toHaveLength(35)
  await expect(unicodeLeft).toHaveText(unicodeLeftName)
  await expect(unicodeRight).toHaveText(unicodeRightName)
  expect(Array.from(combiningName)).toHaveLength(29)
  expect(Array.from(zwjName)).toHaveLength(31)
  await expect(combining).toHaveText(combiningName)
  await expect(zwj).toHaveText(zwjName)
  for (const [width, height] of [[1280, 720], [1440, 900]] as const) {
    await page.setViewportSize({ width, height })
    const recorded = left.locator('xpath=ancestor::button').locator(':scope > span').last()
    await expect(recorded).toBeVisible()
    expect(await recorded.evaluate((element) => getComputedStyle(element).whiteSpace),
      `Recorded timestamp should stay on one line at ${width}px`).toBe('nowrap')
    for (const subject of [left, right, unicodeLeft, unicodeRight, combining, zwj]) {
      await expect(subject).toBeVisible()
      const row = subject.locator('xpath=ancestor::button')
      expect(await row.evaluate((element) => element.scrollWidth <= element.clientWidth),
        `Jobs row should not overflow at ${width}px`).toBe(true)
      const [subjectBox, rowBox] = await Promise.all([subject.boundingBox(), row.boundingBox()])
      if (!subjectBox || !rowBox) throw new Error('Jobs subject has no bounding box')
      expect(subjectBox.x + subjectBox.width).toBeLessThanOrEqual(rowBox.x + rowBox.width + 0.5)
    }
    await expect(left.locator('span').last()).toContainText('left-camera-pass')
    await expect(right.locator('span').last()).toContainText('right-camera-pass')
    await expect(unicodeLeft.locator('span').last()).toContainText('左侧相机')
    await expect(unicodeRight.locator('span').last()).toContainText('右侧相机')
    await expect(combining.locator('span').last()).toHaveText(`e\u0301${'x'.repeat(17)}`)
    await expect(zwj.locator('span').last()).toHaveText(`👩🏽‍🔬${'x'.repeat(17)}`)
    for (const subject of [unicodeLeft, unicodeRight]) {
      expect(await subject.evaluate((element) => {
        const suffix = element.lastElementChild?.lastElementChild
        const text = suffix?.firstChild
        if (!(suffix instanceof HTMLElement) || !(text instanceof Text) || text.length === 0) return false
        const finalCharacter = document.createRange()
        finalCharacter.setStart(text, text.length - 1)
        finalCharacter.setEnd(text, text.length)
        const characterBox = finalCharacter.getBoundingClientRect()
        const subjectBox = element.getBoundingClientRect()
        return characterBox.left >= subjectBox.left && characterBox.right <= subjectBox.right
      }), `Canvas suffix ending should remain visible at ${width}px`).toBe(true)
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      `Jobs should not create page overflow at ${width}px`).toBe(true)
  }
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
  await page.route('**/api/catalog/revision-details', async (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().postDataJSON()).toEqual({ datasetId: 'dataset-1', revisionId: 'rev-published' })
    await route.fulfill({ json: {
      datasetId: 'dataset-1', revisionId: 'rev-published', committedAt: '2026-07-19T12:01:00Z', retentionOwner: 'core', parentRevisionId: 'rev-base', producerOperation: 'merge-columns',
      summary: { rowCount: 2, dataFileCount: 1, totalBytes: 120, fragmentCount: 1 }, preview: { columns: [{ name: 'id', type: 'BIGINT' }, { name: 'score', type: 'DOUBLE' }], rows: [{ id: 1, score: 0.8 }], hasMore: true, rowLimit: 100 },
    } })
  })

  await page.goto('/#/jobs')
  await expect(page.getByRole('button', { name: 'Open run merge-task-1 in Column enrichment' })).toBeVisible()
  await page.getByRole('button', { name: 'Open run merge-task-1 in Column enrichment' }).click()
  await page.getByText('Technical evidence', { exact: true }).click()
  await expect(page.getByText('Column merge:', { exact: true })).toBeVisible()
  await expect(page.getByText('rev-published')).toBeVisible()
  await page.getByRole('button', { name: 'Open exact revision' }).click()
  await expect(page.getByLabel('Exact revision detail')).toContainText('Parent rev-base')
  await expect(page.getByText('Preview is bounded; this remains the exact published revision.')).toBeVisible()
})
