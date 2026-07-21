/**
 * Release acceptance journey for issue #639: certify the delivered keyed-upsert path (#636 service,
 * #637 API, #638 Write inspector) end to end — a fresh managed-local base + payload → the shipped
 * Write Inspector preflight/run → one exact child revision + receipt evidence, cross-checked against
 * independently recomputed expectations — plus durability (response-loss replay, hub-restart recovery)
 * and headless-API parity.
 *
 * Runs under the scheduled/on-demand acceptance policy (docs/CI.md), not per-PR: gated on the `full`
 * fixture profile like default-write-journey.spec.ts, so the required PR e2e job skips it and the daily
 * ux-acceptance workflow exercises it. It captures named 1440x900 light/dark screenshots and a
 * machine-readable visual-review.json into docs/acceptance/issue-639.
 */
import { spawn, type ChildProcess } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { createServer } from 'node:net'
import { mkdir, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const fullProfile = process.env.DP_E2E_FIXTURE_PROFILE === 'full'
const REPO_ROOT = path.resolve(process.cwd(), '..')
const KERNEL_DIR = path.join(REPO_ROOT, 'kernel')
const EVIDENCE_DIR = path.join(REPO_ROOT, 'docs', 'acceptance', 'issue-639')
const SCREENSHOT_DIR = path.join(EVIDENCE_DIR, 'screenshots')
const VIEWPORT = { width: 1440, height: 900 }

type Theme = 'light' | 'dark'
type ExactBase = { uri: string; tableId: string; datasetId: string; revisionId: string; filename: string }
type Receipt = { datasetId: string; revisionId: string; parentHead?: { revisionId: string } | null }
type UpsertTask = {
  taskId: string; status: string; childRevisionId?: string | null; diagnosticCode?: string | null
  receipt?: Receipt | null; evidence?: Record<string, number> | null
}
const visualReview: Array<{ viewport: string; theme: Theme; consoleOrPageErrors: string[] }> = []

test.describe('keyed-upsert release acceptance @acceptance-keyed-upsert', () => {
  test.skip(!fullProfile, 'keyed-upsert acceptance runs under the scheduled full-profile workflow, not per-PR')
  test.describe.configure({ mode: 'serial' })

  async function ok<T>(response: { ok(): boolean; status(): number; text(): Promise<string>; json(): Promise<unknown> }, label: string): Promise<T> {
    expect(response.ok(), `${label}: ${response.status()} ${await response.text()}`).toBeTruthy()
    return response.json() as Promise<T>
  }

  async function setTheme(page: Page, theme: Theme): Promise<void> {
    const html = page.locator('html')
    const isDark = (await html.getAttribute('data-theme')) === 'dark'
    if (theme === 'dark' && !isDark) await page.getByRole('button', { name: 'Switch to dark theme' }).click()
    if (theme === 'light' && isDark) await page.getByRole('button', { name: 'Switch to light theme' }).click()
    if (theme === 'dark') await expect(html).toHaveAttribute('data-theme', 'dark')
    else await expect(html).not.toHaveAttribute('data-theme', 'dark')
  }

  async function shoot(page: Page, theme: Theme, surface: string): Promise<void> {
    await mkdir(SCREENSHOT_DIR, { recursive: true })
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, `1440x900-${theme}-${surface}.png`) })
  }

  // Build one managed-local exact revision from a bounded events slice via ordinary product APIs.
  async function bootstrap(request: APIRequestContext, base: string, canvasId: string, filename: string, predicate: string): Promise<ExactBase> {
    const graph = {
      id: canvasId, name: 'Issue 639 bootstrap', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'events', config: { uri: 'events' } } },
        { id: 'filter', type: 'filter', position: { x: 300, y: 120 }, data: { title: 'slice', config: { predicate } } },
        { id: 'select', type: 'select', position: { x: 520, y: 120 }, data: { title: 'columns', config: { select: 'id, event AS value' } } },
        { id: 'write', type: 'write', position: { x: 740, y: 120 }, data: { title: filename, config: { filename, writeMode: 'overwrite' } } },
      ],
      edges: [
        { id: 'source-filter', source: 'source', target: 'filter' },
        { id: 'filter-select', source: 'filter', target: 'select' },
        { id: 'select-write', source: 'select', target: 'write' },
      ],
    }
    await ok(await request.post(`${base}/api/canvas`, { data: graph }), 'save bootstrap canvas')
    const submissionId = randomUUID()
    const admission = await ok<{ intent: unknown }>(await request.post(`${base}/api/run/write-admission`, { data: { graph, nodeId: 'write', submissionId } }), 'admit bootstrap')
    const started = await ok<{ runId: string }>(await request.post(`${base}/api/run`, { data: { graph, targetNodeId: 'write', confirmed: true, submissionId, writeIntent: admission.intent } }), 'run bootstrap')
    await expect.poll(async () => {
      const status = await ok<{ status: string; error?: string | null }>(await request.get(`${base}/api/run/${encodeURIComponent(started.runId)}`), 'poll bootstrap')
      if (status.status === 'failed') throw new Error(status.error ?? 'bootstrap failed')
      return status.status
    }, { timeout: 30_000 }).toBe('done')
    const done = await ok<{ outputs: Array<{ nodeId?: string; uri?: string }> }>(await request.get(`${base}/api/run/${encodeURIComponent(started.runId)}`), 'read bootstrap output')
    const uri = done.outputs.find((item) => item.nodeId === 'write')?.uri
    if (!uri) throw new Error('bootstrap omitted a catalog output')
    const tables = await ok<{ items: Array<{ id: string; uri: string }> }>(await request.get(`${base}/api/catalog/tables?uris=${encodeURIComponent(uri)}`), 'find bootstrap registration')
    const table = tables.items.find((item) => item.uri === uri)!
    const revision = await ok<{ datasetId: string; revisionId: string }>(await request.get(`${base}/api/catalog/tables/${encodeURIComponent(table.id)}/revisions/resolve`), 'resolve bootstrap revision')
    return { uri, tableId: table.id, filename, ...revision }
  }

  async function unregister(request: APIRequestContext, base: string, dataset: ExactBase): Promise<void> {
    const current = await ok<{ id: string; registrationId?: string | null; metadataRevision?: string | null }>(
      await request.get(`${base}/api/catalog/tables/${encodeURIComponent(dataset.tableId)}`), 'reload unregister preconditions')
    if (!current.registrationId || !current.metadataRevision) return
    await request.delete(`${base}/api/catalog/tables/${encodeURIComponent(dataset.tableId)}`, {
      params: { expected_registration_id: current.registrationId, expected_revision: current.metadataRevision } })
  }

  function upsertCanvas(canvasId: string, targetFile: string, payload: ExactBase) {
    return {
      id: canvasId, name: 'Issue 639 keyed upsert', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'Exact payload', config: {
          uri: payload.uri, tableId: payload.tableId,
          datasetRef: { kind: 'exact', datasetId: payload.datasetId, revisionId: payload.revisionId } } } },
        { id: 'write', type: 'write', position: { x: 420, y: 120 }, data: { title: targetFile, config: {
          filename: targetFile, writeMode: 'overwrite', keyedUpsert: { keys: ['id'] } } } },
      ],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    }
  }

  // Independent expectation: target ids {0,1,2} ∪ payload ids {2,3,4}, keyed on id.
  const EXPECTED = { matched: 1, inserted: 2, unchanged: 2, union: [0, 1, 2, 3, 4] }

  test('certifies the Write Inspector keyed-upsert journey against independently recomputed evidence', async ({ page }) => {
    test.setTimeout(120_000)
    await page.setViewportSize(VIEWPORT)
    const consoleErrors: string[] = []
    page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()) })
    page.on('pageerror', (error) => consoleErrors.push(String(error)))

    const stamp = Date.now()
    const targetFile = `issue-639-target-${stamp}.parquet`
    const payloadFile = `issue-639-payload-${stamp}.parquet`
    const canvasId = `issue-639-upsert-${stamp}`
    const target = await bootstrap(page.request, '', `issue-639-target-canvas-${stamp}`, targetFile, 'id < 3')
    const payload = await bootstrap(page.request, '', `issue-639-payload-canvas-${stamp}`, payloadFile, 'id >= 2 AND id < 5')
    await ok(await page.request.post('/api/canvas', { data: upsertCanvas(canvasId, targetFile, payload) }), 'save upsert canvas')

    try {
      await page.goto(`/#/canvas/${canvasId}`)
      await page.locator('.react-flow__node[data-id="write"]').click()
      const control = page.getByTestId('inspector').getByLabel('Certified keyed upsert')
      await expect(control).toBeVisible()
      await control.getByRole('button', { name: 'Check eligibility' }).click()
      const projection = control.getByLabel('Upsert projection')
      await expect(projection).toContainText('Eligible keyed upsert')
      await expect(projection).toContainText(`${EXPECTED.matched} matched · ${EXPECTED.inserted} inserted · ${EXPECTED.unchanged} unchanged`)
      await shoot(page, 'light', 'canvas')

      const submitted = page.waitForResponse((r) => r.url().endsWith('/api/catalog/upsert') && r.request().method() === 'POST')
      await control.getByRole('button', { name: 'Run keyed upsert' }).click()
      await ok<UpsertTask>(await submitted, 'submit keyed upsert')
      await expect(control.getByText('Published exact revision')).toBeVisible({ timeout: 30_000 })
      await expect(control).toContainText(`${EXPECTED.matched} matched · ${EXPECTED.inserted} inserted · ${EXPECTED.unchanged} unchanged`)
      await control.getByRole('button', { name: 'Open exact revision' }).click()
      await expect(control.getByLabel('Exact revision detail')).toContainText(`Parent ${target.revisionId}`)
      await shoot(page, 'light', 'revision')

      // Independent evidence: recompute matched/inserted/unchanged from the immutable base + payload
      // key sets, and the final head from their union, entirely through the ordinary revision APIs.
      const ids = async (datasetId: string, revisionId: string): Promise<number[]> => {
        const detail = await ok<{ preview: { rows: Array<{ id: number }> } }>(
          await page.request.get(`/api/catalog/revisions/${encodeURIComponent(datasetId)}/${encodeURIComponent(revisionId)}`), 'reopen revision')
        return detail.preview.rows.map((row) => Number(row.id))
      }
      const baseIds = new Set(await ids(target.datasetId, target.revisionId))
      const payloadIds = new Set(await ids(payload.datasetId, payload.revisionId))
      const matched = [...payloadIds].filter((id) => baseIds.has(id)).length
      const inserted = [...payloadIds].filter((id) => !baseIds.has(id)).length
      const unchanged = [...baseIds].filter((id) => !payloadIds.has(id)).length
      expect({ matched, inserted, unchanged }).toEqual({ matched: EXPECTED.matched, inserted: EXPECTED.inserted, unchanged: EXPECTED.unchanged })

      const head = await ok<{ datasetId: string; revisionId: string }>(
        await page.request.get(`/api/catalog/tables/${encodeURIComponent(target.tableId)}/revisions/resolve`), 'resolve upserted head')
      expect(head.revisionId).not.toBe(target.revisionId)
      const finalDetail = await ok<{ parentRevisionId: string; preview: { columns: Array<{ name: string }>; rows: Array<{ id: number }> } }>(
        await page.request.get(`/api/catalog/revisions/${encodeURIComponent(head.datasetId)}/${encodeURIComponent(head.revisionId)}`), 'reopen upserted revision')
      expect(finalDetail.parentRevisionId).toBe(target.revisionId)
      expect(finalDetail.preview.columns.map((c) => c.name)).toEqual(['id', 'value'])
      expect(finalDetail.preview.rows.map((r) => Number(r.id)).sort((a, b) => a - b)).toEqual(EXPECTED.union)

      visualReview.push({ viewport: '1440x900', theme: 'light', consoleOrPageErrors: [...consoleErrors] })
      const darkStart = consoleErrors.length
      await page.goto(`/#/canvas/${canvasId}`)
      await setTheme(page, 'dark')
      await page.locator('.react-flow__node[data-id="write"]').click()
      await expect(control.getByText('Published exact revision')).toBeVisible({ timeout: 30_000 })
      await shoot(page, 'dark', 'canvas')
      await control.getByRole('button', { name: 'Open exact revision' }).click()
      await expect(control.getByLabel('Exact revision detail')).toBeVisible()
      await shoot(page, 'dark', 'revision')
      visualReview.push({ viewport: '1440x900', theme: 'dark', consoleOrPageErrors: consoleErrors.slice(darkStart) })
      await mkdir(EVIDENCE_DIR, { recursive: true })
      await writeFile(path.join(EVIDENCE_DIR, 'visual-review.json'), JSON.stringify(visualReview, null, 2) + '\n')
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
      await page.request.delete(`/api/canvas/${encodeURIComponent(`issue-639-target-canvas-${stamp}`)}`)
      await page.request.delete(`/api/canvas/${encodeURIComponent(`issue-639-payload-canvas-${stamp}`)}`)
      await unregister(page.request, '', target)
      await unregister(page.request, '', payload)
    }
  })

  test('headless API parity and response-loss replay converge on one exact revision', async ({ request }) => {
    test.setTimeout(90_000)
    const stamp = Date.now()
    const targetFile = `issue-639-hl-target-${stamp}.parquet`
    const payloadFile = `issue-639-hl-payload-${stamp}.parquet`
    const target = await bootstrap(request, '', `issue-639-hl-target-canvas-${stamp}`, targetFile, 'id < 3')
    const payload = await bootstrap(request, '', `issue-639-hl-payload-canvas-${stamp}`, payloadFile, 'id >= 2 AND id < 5')
    const submissionId = randomUUID()
    const body = {
      submissionId, datasetId: target.datasetId, expectedHeadRevisionId: target.revisionId,
      payloadDatasetId: payload.datasetId, payloadRevisionId: payload.revisionId, keys: ['id'],
    }
    try {
      const poll = async (taskId: string): Promise<UpsertTask> => {
        let task: UpsertTask = { taskId, status: 'queued' }
        await expect.poll(async () => {
          task = await ok<UpsertTask>(await request.get(`/api/keyed-upsert/${encodeURIComponent(taskId)}`), 'poll headless upsert')
          if (task.status === 'failed') throw new Error(task.diagnosticCode ?? 'headless upsert failed')
          return task.status
        }, { timeout: 30_000 }).toBe('done')
        return task
      }
      // Headless parity: the API alone produces the same receipt shape + evidence the browser sees.
      const first = await ok<UpsertTask>(await request.post('/api/catalog/upsert', { data: body }), 'submit headless upsert')
      const done = await poll(first.taskId)
      expect(done.evidence).toMatchObject({ matched: EXPECTED.matched, inserted: EXPECTED.inserted, unchanged: EXPECTED.unchanged, rejected: 0, duplicate: 0, conflict: 0 })
      expect(done.receipt?.revisionId).toBeTruthy()
      expect(done.receipt?.parentHead?.revisionId).toBe(target.revisionId)
      expect(done.childRevisionId).toBe(done.receipt?.revisionId)
      const childRevision = done.childRevisionId

      // Response-loss replay: the SAME submission converges on the same task/receipt; the head moves once.
      const replay = await ok<UpsertTask>(await request.post('/api/catalog/upsert', { data: body }), 'replay headless upsert')
      expect(replay.taskId).toBe(first.taskId)
      const replayed = await poll(replay.taskId)
      expect(replayed.childRevisionId).toBe(childRevision)
      const history = await ok<{ items: Array<{ revisionId: string }> }>(
        await request.get(`/api/catalog/tables/${encodeURIComponent(target.tableId)}/revisions?limit=100`), 'list target revisions')
      // Exactly two revisions: the bootstrap base and the one upserted head.
      expect(history.items.length).toBe(2)
    } finally {
      await request.delete(`/api/canvas/${encodeURIComponent(`issue-639-hl-target-canvas-${stamp}`)}`)
      await request.delete(`/api/canvas/${encodeURIComponent(`issue-639-hl-payload-canvas-${stamp}`)}`)
      await unregister(request, '', target)
      await unregister(request, '', payload)
    }
  })

  test('recovers the keyed-upsert task to a terminal receipt after a hub restart', async ({ page }) => {
    test.setTimeout(120_000)
    const workspace = path.join(REPO_ROOT, 'web', '.e2e-upsert-restart')
    await rm(workspace, { recursive: true, force: true })
    await mkdir(path.join(workspace, 'data'), { recursive: true })
    const port = await freePort()
    const base = `http://127.0.0.1:${port}`
    const dbUrl = `sqlite:///${path.join(workspace, 'restart-meta.db')}`
    const pythonBin = path.join(KERNEL_DIR, '.venv', 'bin', 'python')

    let hub: ChildProcess | null = null
    const startHub = async (): Promise<void> => {
      hub = spawn(pythonBin, ['-m', 'hub.cli', '--host', '127.0.0.1', '--port', String(port),
        '--workspace', workspace, '--data-dir', path.join(workspace, 'data'), '--no-open'],
        { cwd: KERNEL_DIR, env: { ...process.env, DP_DATABASE_URL: dbUrl }, detached: true, stdio: 'ignore' })
      await waitForLive(page.request, base)
    }
    const killHub = async (): Promise<void> => {
      if (hub?.pid) { try { process.kill(-hub.pid, 'SIGKILL') } catch { /* group already gone */ } }
      await expect.poll(async () => (await isLive(page.request, base)) ? 'up' : 'down', { timeout: 15_000 }).toBe('down')
    }

    try {
      await startHub()
      const stamp = Date.now()
      const target = await bootstrap(page.request, base, `issue-639-rs-target-${stamp}`, `issue-639-rs-target-${stamp}.parquet`, 'id < 3')
      const payload = await bootstrap(page.request, base, `issue-639-rs-payload-${stamp}`, `issue-639-rs-payload-${stamp}.parquet`, 'id >= 2 AND id < 5')
      const submissionId = randomUUID()
      const submitted = await ok<UpsertTask>(await page.request.post(`${base}/api/catalog/upsert`, { data: {
        submissionId, datasetId: target.datasetId, expectedHeadRevisionId: target.revisionId,
        payloadDatasetId: payload.datasetId, payloadRevisionId: payload.revisionId, keys: ['id'],
      } }), 'submit restart upsert')

      // Kill the hub right after submission, then bring a replacement up on the same DB.
      await killHub()
      await startHub()

      // The durable owner recovers the submitted keyed upsert to a terminal receipt after restart.
      let recovered: UpsertTask = submitted
      await expect.poll(async () => {
        recovered = await ok<UpsertTask>(await page.request.get(`${base}/api/keyed-upsert/${encodeURIComponent(submitted.taskId)}`), 'poll recovered upsert')
        if (recovered.status === 'failed') throw new Error(recovered.diagnosticCode ?? 'recovered upsert failed')
        return recovered.status
      }, { timeout: 40_000 }).toBe('done')
      expect(recovered.receipt?.revisionId, 'exactly one recovered receipt with a revision').toBeTruthy()
      expect(recovered.receipt?.parentHead?.revisionId).toBe(target.revisionId)
    } finally {
      await killHub()
      await rm(workspace, { recursive: true, force: true })
    }
  })
})

async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const value = typeof address === 'object' && address ? address.port : 0
      server.close(() => resolve(value))
    })
  })
}

async function isLive(request: APIRequestContext, base: string): Promise<boolean> {
  try {
    const response = await request.get(`${base}/api/livez`, { timeout: 2_000 })
    return response.ok()
  } catch {
    return false
  }
}

async function waitForLive(request: APIRequestContext, base: string): Promise<void> {
  await expect.poll(async () => (await isLive(request, base)) ? 'up' : 'down', { timeout: 60_000 }).toBe('up')
}
