import { spawn, type ChildProcess } from 'node:child_process'
import { createServer } from 'node:net'
import { mkdir, rm } from 'node:fs/promises'
import path from 'node:path'
import { expect, test, type APIRequestContext } from '@playwright/test'

const REPO_ROOT = path.resolve(process.cwd(), '..')
const KERNEL_DIR = path.join(REPO_ROOT, 'kernel')

test('reports a stopped hub within 5s and recovers the local draft after restart', async ({ page }) => {
  test.setTimeout(90_000)
  const workspace = path.join(REPO_ROOT, 'web', '.e2e-liveness-workspace')
  await rm(workspace, { recursive: true, force: true })
  await mkdir(path.join(workspace, 'data'), { recursive: true })
  const port = await freePort()
  const base = `http://127.0.0.1:${port}`
  const dbUrl = `sqlite:///${path.join(workspace, 'liveness-meta.db')}`
  const pythonBin = path.join(KERNEL_DIR, '.venv', 'bin', 'python')
  let hub: ChildProcess | null = null

  const startHub = async () => {
    hub = spawn(pythonBin, [
      '-m', 'hub.cli', '--host', '127.0.0.1', '--port', String(port),
      '--workspace', workspace, '--data-dir', path.join(workspace, 'data'), '--no-open',
    ], {
      cwd: KERNEL_DIR,
      env: { ...process.env, DP_DATABASE_URL: dbUrl },
      detached: true,
      stdio: 'ignore',
    })
    await waitForLive(page.request, base)
  }
  const killHub = async () => {
    if (hub?.pid) {
      try { process.kill(-hub.pid, 'SIGKILL') } catch { /* process group already stopped */ }
    }
    await expect.poll(
      async () => (await isLive(page.request, base)) ? 'up' : 'down',
      { timeout: 15_000 },
    ).toBe('down')
    hub = null
  }

  try {
    await startHub()
    const canvasId = `issue-844-liveness-${Date.now()}`
    const graph = {
      id: canvasId, name: 'Issue 844 liveness', version: 1, requirements: [],
      nodes: [{
        id: 'source', type: 'source', position: { x: 160, y: 160 },
        data: { title: 'Liveness source', status: 'draft', config: { uri: 'events' }, history: [] },
      }],
      edges: [],
    }
    const created = await page.request.post(`${base}/api/canvas`, { data: graph })
    expect(created.ok(), await created.text()).toBe(true)

    await page.goto(`${base}/#/canvas/${encodeURIComponent(canvasId)}`)
    const source = page.locator('.react-flow__node', { hasText: 'Liveness source' })
    await expect(source).toBeVisible()
    await source.click()
    await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
    await expect(page.getByTestId('panel-data')).toBeVisible()
    const badge = page.getByTestId('kernel-badge')
    await badge.click()
    await expect(badge).toHaveText(/worker · warm/, { timeout: 8_000 })
    await expect(page.getByTestId('autosave')).toHaveText(/saved$/)

    await killHub()

    await expect(page.getByText('Kernel offline — your work is cached locally.')).toBeVisible({ timeout: 5_000 })
    await expect(badge).toHaveText(/worker · offline/)
    await expect(page.getByTestId('autosave')).toContainText(/offline/i)
    await expect(page.getByTestId('autosave')).not.toContainText(/saved/i)
    await expect(page.getByRole('button', { name: 'Rerun all' })).toBeDisabled()
    await expect(page.getByTestId('inspector').getByRole('button', { name: 'Hub offline — run unavailable' }))
      .toHaveAttribute('aria-disabled', 'true')

    const recoveredName = `Issue 844 recovered ${Date.now()}`
    await page.getByTestId('canvas-title').click()
    await page.getByRole('textbox', { name: 'Canvas name' }).fill(recoveredName)
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('autosave')).toContainText(/offline/i)
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace', { exact: true }).click()
    const retry = page.getByRole('button', { name: `Retry local draft ${recoveredName}` })
    await expect(retry).toBeDisabled()

    await startHub()

    await expect(retry).toBeEnabled()
    await retry.click()
    await expect(retry).toHaveCount(0, { timeout: 8_000 })
    await expect.poll(async () => {
      const response = await page.request.get(`${base}/api/canvas/${canvasId}`)
      return response.ok() ? ((await response.json()) as { name: string }).name : null
    }).toBe(recoveredName)
    await page.goto(`${base}/#/canvas/${encodeURIComponent(canvasId)}`)
    await expect(page.getByTestId('canvas-title')).toContainText(recoveredName)
    await expect(page.getByTestId('autosave')).toHaveText(/saved$/, { timeout: 8_000 })
    await expect(page.getByRole('button', { name: 'Rerun all' })).toBeEnabled()

    await page.reload()
    await expect(page.getByTestId('canvas-title')).toContainText(recoveredName)
    await expect(source).toBeVisible()
  } finally {
    await killHub()
    await rm(workspace, { recursive: true, force: true })
  }
})

async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : 0
      server.close(() => resolve(port))
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
  await expect.poll(
    async () => (await isLive(request, base)) ? 'up' : 'down',
    { timeout: 60_000 },
  ).toBe('up')
}
