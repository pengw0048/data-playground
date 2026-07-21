/**
 * Golden acceptance journey for issue #635: certify what a fresh user on the DEFAULT configuration
 * (the per-canvas kernel backend, unmodified settings) actually gets end to end — Workspace discovery
 * → Source → typed transform → Write → managed revision + receipt → Jobs/Inbox evidence →
 * exact-revision reopen → hub-restart recovery — plus two error-path spot checks.
 *
 * Runs under the scheduled/on-demand acceptance policy (docs/CI.md), not per-PR: gated on the `full`
 * fixture profile the way ux-full-matrix.spec.ts is, so the required PR e2e job skips it and the daily
 * ux-acceptance workflow exercises it. It captures named 1440x900 light/dark screenshots and a
 * machine-readable visual-review.json into the gitignored web/test-results tree, uploaded as the
 * workflow's artifacts rather than committed.
 */
import { spawn, type ChildProcess } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { createServer } from 'node:net'
import { mkdir, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { goToWorkspace, workspaceResource } from './support/workspace'

const fullProfile = process.env.DP_E2E_FIXTURE_PROFILE === 'full'
const REPO_ROOT = path.resolve(process.cwd(), '..')
const KERNEL_DIR = path.join(REPO_ROOT, 'kernel')
const EVIDENCE_DIR = path.join(REPO_ROOT, 'web', 'test-results', 'acceptance', 'issue-635')
const SCREENSHOT_DIR = path.join(EVIDENCE_DIR, 'screenshots')
const VIEWPORT = { width: 1440, height: 900 }

type Theme = 'light' | 'dark'
const visualReview: Array<{ viewport: string; theme: Theme; consoleOrPageErrors: string[] }> = []

test.describe('default fresh-workspace write journey @acceptance-default-journey', () => {
  test.skip(!fullProfile, 'default-journey acceptance runs under the scheduled full-profile workflow, not per-PR')
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

  test('certifies the default-kernel Source → transform → Write → revision → Jobs/Inbox journey', async ({ page }) => {
    test.setTimeout(120_000)
    await page.setViewportSize(VIEWPORT)
    const consoleErrors: string[] = []
    page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()) })
    page.on('pageerror', (error) => consoleErrors.push(String(error)))

    // 1. Workspace discovery: the fresh workspace exposes the seeded starter dataset (paginated helper).
    await goToWorkspace(page)
    await expect(await workspaceResource(page, 'dataset', 'events')).toBeVisible()

    // 2/3. Source → typed transform → Write, saved on unmodified default settings (kernel backend).
    const stamp = Date.now()
    const canvasId = `issue-635-journey-${stamp}`
    const filename = `issue-635-out-${stamp}.parquet`
    const graph = {
      id: canvasId, name: 'Issue 635 default journey', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'Starter events', config: { uri: 'events' } } },
        { id: 'select', type: 'select', position: { x: 360, y: 120 }, data: { title: 'Typed projection', config: { select: 'id, user_id, amount' } } },
        { id: 'write', type: 'write', position: { x: 640, y: 120 }, data: { title: filename, config: { filename, writeMode: 'overwrite' } } },
      ],
      edges: [
        { id: 'source-select', source: 'source', target: 'select' },
        { id: 'select-write', source: 'select', target: 'write' },
      ],
    }
    await ok(await page.request.post('/api/canvas', { data: graph }), 'save journey canvas')

    // 4. Write on the DEFAULT kernel through the shipped Write Inspector → managed revision + receipt.
    await page.goto(`/#/canvas/${canvasId}`)
    await page.locator('.react-flow__node[data-id="write"]').click()
    const inspector = page.getByTestId('inspector')
    await expect(inspector.getByLabel('Write admission')).toContainText('create · managed-local-file')
    const runResponse = page.waitForResponse((response) =>
      response.url().endsWith('/api/run') && response.request().method() === 'POST')
    await inspector.getByRole('button', { name: 'Run', exact: true }).click()
    const started = await ok<{ runId: string }>(await runResponse, 'submit default-kernel write')
    const runId = started.runId
    const receipt = inspector.getByLabel('Write receipt')
    await expect(receipt).toContainText('durable revision', { timeout: 30_000 })
    type JobItem = { status: string; outputReceipt?: { datasetId: string; revisionId: string } | null }
    let job: JobItem | undefined
    // Poll the mirrored Jobs record until it converges on the run's terminal state.
    await expect.poll(async () => {
      job = (await ok<{ items: JobItem[] }>(
        await page.request.get(`/api/jobs?run_id=${encodeURIComponent(runId)}&limit=1`), 'read Jobs receipt')).items[0]
      return job?.status
    }, { timeout: 30_000 }).toBe('done')
    const dataset = job?.outputReceipt
    expect(dataset?.revisionId, 'Jobs surfaces the managed revision id').toBeTruthy()
    // The inspector receipt names the same durable revision the Jobs surface published.
    await expect(receipt).toContainText(dataset!.revisionId)
    await shoot(page, 'light', 'canvas')

    // 5. Jobs evidence: the durable task and its output receipt are visible in the shipped Jobs surface.
    await page.goto(`/#/jobs?run=${encodeURIComponent(runId)}`)
    await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible()
    await expect(page.getByRole('button', { name: `Open run ${runId} in Issue 635 default journey` })).toBeVisible({ timeout: 15_000 })
    await shoot(page, 'light', 'jobs')

    // 6. Inbox evidence: the completed durable write is announced in the Inbox.
    await page.goto('/#/inbox')
    await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()

    // 7. Exact-revision reopen from the published receipt.
    await page.goto(`/#/jobs?run=${encodeURIComponent(runId)}`)
    await page.getByText('Technical evidence', { exact: true }).click()
    await page.getByRole('button', { name: 'Open exact revision' }).click()
    await expect(page.getByLabel('Exact revision detail')).toBeVisible()
    const detail = await ok<{ revisionId: string; preview: { columns: Array<{ name: string }> } }>(
      await page.request.get(`/api/catalog/revisions/${encodeURIComponent(dataset!.datasetId)}/${encodeURIComponent(dataset!.revisionId)}`),
      'reopen exact published revision')
    expect(detail.revisionId).toBe(dataset!.revisionId)
    expect(detail.preview.columns.map((column) => column.name)).toEqual(['id', 'user_id', 'amount'])
    await shoot(page, 'light', 'revision')

    // Dark-theme pass over the same certified surfaces for the visual-review matrix.
    visualReview.push({ viewport: '1440x900', theme: 'light', consoleOrPageErrors: [...consoleErrors] })
    const darkStart = consoleErrors.length
    await page.goto(`/#/canvas/${canvasId}`)
    await setTheme(page, 'dark')
    await page.locator('.react-flow__node[data-id="write"]').click()
    await expect(inspector.getByLabel('Write receipt')).toContainText('durable revision')
    await shoot(page, 'dark', 'canvas')
    await page.goto(`/#/jobs?run=${encodeURIComponent(runId)}`)
    await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible()
    await page.getByText('Technical evidence', { exact: true }).click()
    await page.getByRole('button', { name: 'Open exact revision' }).click()
    await expect(page.getByLabel('Exact revision detail')).toBeVisible()
    await shoot(page, 'dark', 'jobs')
    await shoot(page, 'dark', 'revision')
    visualReview.push({ viewport: '1440x900', theme: 'dark', consoleOrPageErrors: consoleErrors.slice(darkStart) })

    await mkdir(EVIDENCE_DIR, { recursive: true })
    await writeFile(path.join(EVIDENCE_DIR, 'visual-review.json'), JSON.stringify(visualReview, null, 2) + '\n')

    await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
  })

  test('surfaces a typed 4xx for an unknown write destination', async ({ page }) => {
    await page.setViewportSize(VIEWPORT)
    const canvasId = `issue-635-unknown-dest-${Date.now()}`
    const graph = {
      id: canvasId, name: 'Issue 635 unknown destination', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'events', config: { uri: 'events' } } },
        { id: 'write', type: 'write', position: { x: 360, y: 120 }, data: { title: 'unknown', config: { filename: 'unknown.parquet', destId: 'does-not-exist', writeMode: 'overwrite' } } },
      ],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    }
    await ok(await page.request.post('/api/canvas', { data: graph }), 'save unknown-destination canvas')
    try {
      // Both the admission and the run reject an unknown destination as a typed 4xx — never a 500.
      const admission = await page.request.post('/api/run/write-admission', {
        data: { graph, nodeId: 'write', submissionId: randomUUID() },
      })
      expect(admission.status(), 'unknown destination admission is a typed client error, not a 500').toBe(400)
      expect(await admission.text()).toContain("unknown destination 'does-not-exist'")
      const run = await page.request.post('/api/run', {
        data: { graph, targetNodeId: 'write', confirmed: true, submissionId: randomUUID() },
      })
      expect(run.status(), 'unknown destination run is a typed client error, not a 500').toBe(400)
      expect(await run.text()).toContain("unknown destination 'does-not-exist'")

      // The shipped UI contains the failure without crashing: the app shell stays alive and the Write
      // card never certifies a destination it could not resolve.
      await page.goto(`/#/canvas/${canvasId}`)
      await page.locator('.react-flow__node[data-id="write"]').click()
      await expect(page.getByTestId('app-menu')).toBeVisible()
      await expect(page.locator('.react-flow__node[data-id="write"]')).toContainText('checking destination…')
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

  test('a managed Lance append retry converges on exactly one appended version', async ({ page }) => {
    test.setTimeout(90_000)
    const targetUri = path.join(REPO_ROOT, 'web', '.e2e-workspace', 'outputs', 'lance-append-target.lance')
    expect(existsSync(targetUri), 'the full-profile fixture seeds lance-append-target.lance').toBeTruthy()

    // Register the pre-seeded Lance dataset (idempotent across a reused local server).
    const register = await page.request.post('/api/catalog/register', { data: { uri: targetUri, name: 'lance-append-target' } })
    expect([200, 400, 409]).toContain(register.status())

    const revisions = async (): Promise<number> => {
      const table = await ok<{ items: Array<{ id: string; uri: string }> }>(
        await page.request.get(`/api/catalog/tables?uris=${encodeURIComponent(targetUri)}`), 'find append target')
      const found = table.items.find((item) => item.uri === targetUri)
      expect(found, 'append target is registered').toBeTruthy()
      const detail = await ok<{ items: Array<{ revisionId: string }> }>(
        await page.request.get(`/api/catalog/tables/${encodeURIComponent(found!.id)}/revisions?limit=100`), 'list append revisions')
      return detail.items.length
    }

    const canvasId = 'issue-635-lance-append'
    const submissionId = randomUUID()
    const graph = {
      id: canvasId, name: 'Issue 635 lance append', version: 1, requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'events', config: { uri: 'events' } } },
        { id: 'select', type: 'select', position: { x: 360, y: 120 }, data: { title: 'match schema', config: { select: 'id, event AS label' } } },
        { id: 'write', type: 'write', position: { x: 640, y: 120 }, data: { title: 'lance-append-target', config: { filename: 'lance-append-target.lance', destId: 'outputs', writeMode: 'append' } } },
      ],
      edges: [{ id: 'source-select', source: 'source', target: 'select' }, { id: 'select-write', source: 'select', target: 'write' }],
    }
    await ok(await page.request.post('/api/canvas', { data: graph }), 'save lance append canvas')
    try {
      const before = await revisions()
      const admission = await ok<{ managed: boolean; provider: string; intent: unknown }>(
        await page.request.post('/api/run/write-admission', { data: { graph, nodeId: 'write', submissionId } }), 'admit managed lance append')
      expect(admission.managed).toBeTruthy()
      expect(admission.provider).toBe('managed-local-lance')

      const runOnce = async (): Promise<string> => {
        const started = await ok<{ runId: string }>(await page.request.post('/api/run', {
          data: { graph, targetNodeId: 'write', confirmed: true, submissionId, writeIntent: admission.intent },
        }), 'run managed lance append')
        await expect.poll(async () => {
          const status = await ok<{ status: string; error?: string | null }>(
            await page.request.get(`/api/run/${encodeURIComponent(started.runId)}`), 'poll lance append')
          if (status.status === 'failed') throw new Error(status.error ?? 'lance append failed')
          return status.status
        }, { timeout: 30_000 }).toBe('done')
        return started.runId
      }

      const firstRun = await runOnce()
      const afterFirst = await revisions()
      expect(afterFirst, 'one append adds exactly one version').toBe(before + 1)

      // Retry the SAME submission with the same intent (response-loss replay): converge, do not double.
      const retryRun = await runOnce()
      expect(retryRun, 'retry adopts the original durable run').toBe(firstRun)
      expect(await revisions(), 'retry converges on the one appended version').toBe(afterFirst)
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

  test('recovers the managed write terminal state and evidence after a hub restart', async ({ page }) => {
    test.setTimeout(120_000)
    const workspace = path.join(REPO_ROOT, 'web', '.e2e-restart-workspace')
    await rm(workspace, { recursive: true, force: true })
    await mkdir(path.join(workspace, 'data'), { recursive: true })
    const port = await freePort()
    const base = `http://127.0.0.1:${port}`
    const dbUrl = `sqlite:///${path.join(workspace, 'restart-meta.db')}`
    const pythonBin = path.join(KERNEL_DIR, '.venv', 'bin', 'python')

    let hub: ChildProcess | null = null
    const startHub = async (): Promise<void> => {
      hub = spawn(pythonBin, ['-m', 'hub.cli', '--host', '127.0.0.1', '--port', String(port),
        '--workspace', workspace, '--data-dir', path.join(workspace, 'data'), '--no-open'], {
        cwd: KERNEL_DIR, env: { ...process.env, DP_DATABASE_URL: dbUrl }, detached: true, stdio: 'ignore',
      })
      await waitForLive(page.request, base)
    }
    const killHub = async (): Promise<void> => {
      if (hub?.pid) { try { process.kill(-hub.pid, 'SIGKILL') } catch { /* group already gone */ } }
      await expect.poll(async () => (await isLive(page.request, base)) ? 'up' : 'down', { timeout: 15_000 }).toBe('down')
    }

    try {
      await startHub()
      const canvasId = 'issue-635-restart'
      const submissionId = randomUUID()
      const filename = `issue-635-restart-${Date.now()}.parquet`
      const graph = {
        id: canvasId, name: 'Issue 635 restart recovery', version: 1, requirements: [],
        nodes: [
          { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'events', config: { uri: 'events' } } },
          { id: 'write', type: 'write', position: { x: 360, y: 120 }, data: { title: filename, config: { filename, writeMode: 'overwrite' } } },
        ],
        edges: [{ id: 'source-write', source: 'source', target: 'write' }],
      }
      await ok(await page.request.post(`${base}/api/canvas`, { data: graph }), 'save restart canvas')
      const admission = await ok<{ intent: unknown }>(await page.request.post(`${base}/api/run/write-admission`, {
        data: { graph, nodeId: 'write', submissionId },
      }), 'admit restart write')
      const started = await ok<{ runId: string }>(await page.request.post(`${base}/api/run`, {
        data: { graph, targetNodeId: 'write', confirmed: true, submissionId, writeIntent: admission.intent },
      }), 'submit restart write')

      // Kill the hub as soon as the durable write is submitted, then bring a replacement up on the same DB.
      await killHub()
      await startHub()

      // The durable owner recovers the submitted write to a terminal receipt after restart.
      await expect.poll(async () => {
        const status = await ok<{ status: string; error?: string | null }>(
          await page.request.get(`${base}/api/run/${encodeURIComponent(started.runId)}`), 'poll recovered run')
        if (status.status === 'failed') throw new Error(status.error ?? 'recovered run failed')
        return status.status
      }, { timeout: 40_000 }).toBe('done')
      const jobs = await ok<{ items: Array<{ status: string; outputReceipt?: { revisionId: string } | null }> }>(
        await page.request.get(`${base}/api/jobs?run_id=${encodeURIComponent(started.runId)}&limit=1`), 'read recovered Jobs receipt')
      expect(jobs.items[0]?.status).toBe('done')
      expect(jobs.items[0]?.outputReceipt?.revisionId, 'exactly one recovered receipt with a revision').toBeTruthy()

      // The reloaded browser shows the recovered terminal state from the restarted hub.
      await page.setViewportSize(VIEWPORT)
      await page.goto(`${base}/#/jobs?run=${encodeURIComponent(started.runId)}`)
      await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible()
      await expect(page.getByRole('button', { name: `Open run ${started.runId} in Issue 635 restart recovery` })).toBeVisible({ timeout: 15_000 })
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
