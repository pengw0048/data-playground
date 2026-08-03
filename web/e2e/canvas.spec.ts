import { copyFile, mkdir, unlink } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { test, expect, type APIRequestContext, type Page, type Locator } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import { backToWorkspace, createCanvasFromWorkspace, goToWorkspace, workspaceResource } from './support/workspace'

// These specs encode, as assertions, the interaction/visual invariants behind bugs a human had
// to find by hand (menu positioning, node overlap, disabled affordances, no forced popups, the
// minimap, autosave). If one regresses, CI fails instead of the user.

async function boxOf(loc: Locator) {
  const b = await loc.boundingBox()
  if (!b) throw new Error('element has no bounding box')
  return b
}

function overlaps(a: { x: number; y: number; width: number; height: number }, b: typeof a) {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y
}

function contains(outer: { x: number; y: number; width: number; height: number }, inner: typeof outer) {
  return inner.x >= outer.x && inner.y >= outer.y
    && inner.x + inner.width <= outer.x + outer.width
    && inner.y + inner.height <= outer.y + outer.height
}

async function confirmRun(page: Page, action: 'managed' | 'ordinary' = 'managed') {
  const runPanel = page.getByTestId('panel-run')
  await expect(runPanel.getByText('CONFIRM RUN')).toBeVisible()
  await runPanel.getByRole('button', {
    name: action === 'managed'
      ? 'Publish output'
      : /^(?:Run with unknown row count|Run [\d,]+ rows)$/,
  }).click()
}

// The first-run project executes before the other mutating E2E projects against a fresh kernel.
// Keep the assertion in one journey so later steps cannot depend on resetting shared metadata.
async function canvasesFor(page: Page): Promise<Array<{ id: string }>> {
  return page.evaluate(async () => {
    const userId = localStorage.getItem('dp-user')
    const response = await fetch('/api/canvas', {
      headers: userId ? { 'X-DP-User': userId } : {},
    })
    if (!response.ok) throw new Error(`Canvas list failed: ${response.status}`)
    return response.json()
  })
}

async function canvasFor(page: Page, canvasId: string): Promise<{ nodes: unknown[]; edges: unknown[] }> {
  return page.evaluate(async (canvas) => {
    const userId = localStorage.getItem('dp-user')
    const response = await fetch(`/api/canvas/${encodeURIComponent(canvas)}`, {
      headers: userId ? { 'X-DP-User': userId } : {},
    })
    if (!response.ok) throw new Error(`Canvas fetch failed: ${response.status}`)
    return response.json()
  }, canvasId)
}

async function useFreshFirstRunUser(page: Page, request: APIRequestContext, label: string) {
  const created = await request.post('/api/users', {
    data: { name: `${label} ${Date.now()} ${Math.random().toString(16).slice(2)}` },
    headers: { 'X-DP-User': 'local' },
  })
  expect(created.ok()).toBe(true)
  const userId = (await created.json() as { id: string }).id
  await page.addInitScript((id) => localStorage.setItem('dp-user', id), userId)
  return userId
}

async function canvasZoom(page: Page): Promise<number> {
  const style = await page.locator('.react-flow__viewport').getAttribute('style')
  const match = /scale\(([\d.]+)\)/.exec(style ?? '')
  if (!match) throw new Error(`Canvas viewport has no scale: ${style ?? '<missing style>'}`)
  return Number(match[1])
}

// Open a bottom-toolbar category by its aria-label and click a node kind inside the menu.
async function addNode(page: Page, category: string, kindTitle: string) {
  await page.getByRole('button', { name: category, exact: true }).click()
  const menu = page.locator('.dp-panel', { hasText: kindTitle }).last()
  await menu.getByText(kindTitle, { exact: true }).click()
}

async function openSettledAppMenu(page: Page) {
  const menu = page.getByRole('menu', { name: 'Data Playground menu' })
  // Wait for a preceding menu selection's closing portal to unmount before opening the next one,
  // or the locator can resolve to the stale animated copy.
  await expect(menu).toBeHidden()
  await page.getByTestId('app-menu').click()
  await expect(menu).toBeVisible()
  await expect(menu).toHaveAttribute('data-state', 'open')
  return menu
}

async function addFromOutput(page: Page, node: Locator, operation: string) {
  await node.locator('.react-flow__handle-right').click()
  const finder = page.getByRole('dialog', { name: 'Connect to an operation' })
  await finder.getByRole('textbox', { name: 'Search operations' }).fill(operation)
  await finder.getByRole('option', { name: new RegExp(operation, 'i') }).first().click()
  await expect(finder).toBeHidden()
}

async function connectHandles(page: Page, source: Locator, target: Locator) {
  const from = await boxOf(source.locator('.react-flow__handle-right'))
  const to = await boxOf(target)
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2)
  await page.mouse.down()
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 12 })
  await page.mouse.up()
}

async function edgeNodeCrossings(page: Page): Promise<string[]> {
  return page.locator('.react-flow__edge').evaluateAll((edges) => {
    const nodes = Array.from(document.querySelectorAll<HTMLElement>('.react-flow__node')).map((node) => ({
      id: node.dataset.id ?? '',
      rect: node.getBoundingClientRect(),
    }))
    const failures: string[] = []
    for (const edge of edges) {
      const label = edge.getAttribute('aria-label')
        ?? edge.querySelector('[aria-label^="Edge from "]')?.getAttribute('aria-label')
        ?? ''
      const endpoints = /^Edge from (.+) to (.+)$/.exec(label)
      const path = edge.querySelector<SVGPathElement>('.react-flow__edge-path')
      const matrix = path?.getScreenCTM()
      if (!endpoints || !path || !matrix) {
        failures.push(`unmeasurable edge: ${label || edge.id}`)
        continue
      }
      const length = path.getTotalLength()
      for (const node of nodes) {
        if (node.id === endpoints[1] || node.id === endpoints[2]) continue
        let crossing = false
        for (let distance = 2; distance < length - 2; distance += 3) {
          const pathPoint = path.getPointAtLength(distance)
          const point = new DOMPoint(pathPoint.x, pathPoint.y).matrixTransform(matrix)
          if (point.x > node.rect.left + 2 && point.x < node.rect.right - 2
              && point.y > node.rect.top + 2 && point.y < node.rect.bottom - 2) {
            crossing = true
            break
          }
        }
        if (crossing) failures.push(`${endpoints[1]} -> ${endpoints[2]} crosses ${node.id}`)
      }
    }
    return failures
  })
}

// Start each node-touching test on a FRESH empty canvas — the metadata DB persists canvases, so
// without this a prior test's nodes would leak in and break count assertions.
async function fresh(page: Page) {
  await createCanvasFromWorkspace(page)
  await expect(page.locator('.react-flow__node')).toHaveCount(0)
}

async function enablePipelineImporter(page: Page) {
  await page.route('**/api/kernel', (route) => route.fulfill({ json: {
    mode: 'local', backend: 'e2e', warm: false, version: 'test', adapters: [], runners: [], processors: [],
    capabilities: ['pipeline-importer'], capabilityViews: [], backends: [],
  } }))
}

// Workspace is bounded. Follow load-more pages before selecting a named dataset.
async function openWorkspaceDataset(page: Page, name: string) {
  await (await workspaceResource(page, 'dataset', name)).click()
}

async function addWorkspaceDatasetToCurrentCanvas(page: Page, name: string) {
  const canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
  await backToWorkspace(page)
  await openWorkspaceDataset(page, name)
  await page.getByTestId('detail-use').click()
  await page.getByRole('button', { name: /^Choose another Canvas/ }).click()
  await page.getByLabel('Target canvas').selectOption(canvasId)
  await page.getByRole('button', { name: 'Add and open' }).click()
  await expect(page.getByTestId('toolbar')).toBeVisible()
  await page.locator('.react-flow__node').getByText('DATASET', { exact: true }).click()
  await expect(page.getByTestId('inspector').getByRole('button', { name: 'View data' })).toBeVisible()
}

// Prove the app's collab socket has joined THIS canvas before driving an out-of-band edit. The
// autosave label only proves the HTTP canvas exists; it says nothing about websocket readiness. A
// short-lived peer waits for the app's presence frame, which the server can relay only after the app
// is registered in the room. This is an event handshake, not a timing delay.
async function waitForCollabRoom(page: Page, canvasId: string) {
  await page.evaluate((id) => new Promise<void>((resolve, reject) => {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
    const socket = new WebSocket(`${protocol}://${location.host}/ws/collab/${encodeURIComponent(id)}`)
    const deadline = window.setTimeout(() => {
      socket.close()
      reject(new Error(`app did not join collab room ${id}`))
    }, 8_000)
    socket.onopen = () => socket.send(JSON.stringify({
      type: 'presence', clientId: `e2e-probe-${crypto.randomUUID()}`, name: 'e2e probe', color: '#888',
    }))
    socket.onmessage = (event) => {
      let message: { type?: string } | null = null
      try { message = JSON.parse(String(event.data)) } catch { /* wait for a valid presence frame */ }
      if (message?.type !== 'presence') return
      window.clearTimeout(deadline)
      socket.close()
      resolve()
    }
    socket.onerror = () => {
      window.clearTimeout(deadline)
      reject(new Error(`could not join collab room ${id}`))
    }
  }), canvasId)
}

test.describe('Data Playground canvas', () => {
  test('direct first-entry example fits every node once at 1280x720 and preserves manual viewport control @first-run', async ({ page, request }) => {
    await useFreshFirstRunUser(page, request, 'First-run direct example')
    let exampleId: string | null = null
    try {
      await page.goto('/')
      expect(await canvasesFor(page)).toEqual([])
      const firstRunDataset = await workspaceResource(page, 'dataset', 'events')
      await firstRunDataset.click()
      const firstRunDetail = page.getByRole('region', { name: 'events' })
      const firstRunBack = firstRunDetail.getByRole('button', { name: 'Back to Workspace' })
      await expect(firstRunDetail).toBeVisible()
      await expect(firstRunBack).toBeFocused()
      await expect(page.getByTestId('first-run-canvas-choice')).toHaveCount(0)
      await expect(page.getByRole('form', { name: 'Workspace search' })).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Open dataset events' })).toHaveCount(0)
      await page.keyboard.press('Shift+Tab')
      await expect(page.getByTestId('first-run-canvas-choice')).toHaveCount(0)
      await expect(page.getByRole('form', { name: 'Workspace search' })).toHaveCount(0)
      await firstRunBack.click()
      await expect(page.getByTestId('first-run-canvas-choice')).toBeVisible()
      await page.getByRole('button', { name: 'Open example Purchases per user' }).click()
      const nodes = page.locator('.react-flow__node')
      await expect(nodes).toHaveCount(5)
      exampleId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
      const persisted = await canvasFor(page, exampleId)
      const source = persisted.nodes.find((node) => (node as { type?: string }).type === 'source') as {
        data?: { config?: { uri?: string; tableId?: string; registrationId?: string } }
      } | undefined
      expect(source?.data?.config?.uri).not.toBe('events')
      expect(source?.data?.config?.tableId).toBeTruthy()
      expect(source?.data?.config?.registrationId).toBeTruthy()

      const allNodesInsideFlow = async () => {
        const flow = await page.locator('.react-flow').boundingBox()
        const nodeBoxes = await nodes.evaluateAll((elements) => elements.map((element) => {
          const box = element.getBoundingClientRect()
          return { x: box.x, y: box.y, width: box.width, height: box.height }
        }))
        return !!flow && nodeBoxes.length === 5 && nodeBoxes.every((node) => (
          node.x >= flow.x && node.y >= flow.y
          && node.x + node.width <= flow.x + flow.width
          && node.y + node.height <= flow.y + flow.height
        ))
      }
      await expect.poll(allNodesInsideFlow).toBe(true)

      // The one-shot request is already consumed. A user zoom followed by an ordinary selection
      // rerender must not invoke fitView again or reset the chosen viewport.
      const viewport = page.locator('.react-flow__viewport')
      await page.getByRole('button', { name: 'Zoom in', exact: true }).click()
      const manualViewport = await viewport.getAttribute('style')
      await nodes.first().click()
      await page.waitForTimeout(500)
      expect(await viewport.getAttribute('style')).toBe(manualViewport)
    } finally {
      if (exampleId) await page.request.delete(`/api/canvas/${encodeURIComponent(exampleId)}`)
    }
  })

  test('a saved Canvas fits on Workspace reopen and reload without taking later viewport control @canvas-viewport', async ({ page }) => {
    const suffix = Date.now()
    const canvasId = `saved-overview-${suffix}`
    const canvasName = `Saved graph overview ${suffix}`
    const otherCanvasId = `${canvasId}-other`
    const otherCanvasName = `Saved graph overview other ${suffix}`
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: canvasName, version: 1,
      nodes: Array.from({ length: 5 }, (_, index) => ({
        id: `step-${index}`, type: 'filter', position: { x: 120 + index * 400, y: 160 },
        data: { title: `Step ${index + 1}`, status: 'idle', config: {} },
      })),
      edges: [],
    } })
    expect(created.ok()).toBe(true)
    const otherCreated = await page.request.post('/api/canvas', { data: {
      id: otherCanvasId, name: otherCanvasName, version: 1,
      nodes: Array.from({ length: 5 }, (_, index) => ({
        id: `other-step-${index}`, type: 'filter', position: { x: 220, y: 120 + index * 600 },
        data: { title: `Other step ${index + 1}`, status: 'idle', config: {} },
      })),
      edges: [],
    } })
    expect(otherCreated.ok()).toBe(true)
    await page.setViewportSize({ width: 1280, height: 720 })

    const nodes = page.locator('.react-flow__node')
    const allNodesInsideFlow = async () => {
      const flow = await page.locator('.react-flow').boundingBox()
      const nodeBoxes = await nodes.evaluateAll((elements) => elements.map((element) => {
        const box = element.getBoundingClientRect()
        return { x: box.x, y: box.y, width: box.width, height: box.height }
      }))
      return !!flow && nodeBoxes.length === 5 && nodeBoxes.every((node) => (
        node.x >= flow.x && node.y >= flow.y
        && node.x + node.width <= flow.x + flow.width
        && node.y + node.height <= flow.y + flow.height
      ))
    }

    try {
      await goToWorkspace(page)
      await (await workspaceResource(page, 'canvas', canvasName)).click()
      await expect(nodes).toHaveCount(5)
      await expect.poll(allNodesInsideFlow).toBe(true)

      const viewport = page.locator('.react-flow__viewport')
      await page.getByRole('button', { name: 'Zoom in', exact: true }).click()
      const manualViewport = await viewport.getAttribute('style')
      await nodes.first().click()
      await page.waitForTimeout(500)
      expect(await viewport.getAttribute('style')).toBe(manualViewport)

      // Jobs' ordinary Open canvas link returns to the document still held in the client store;
      // this is not a fetch/reopen path, but it needs the same useful initial overview.
      await page.evaluate(() => { location.hash = '#/jobs' })
      await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible()
      await page.evaluate((hash) => { location.hash = hash }, `#/canvas/${canvasId}`)
      await expect(nodes).toHaveCount(5)
      await expect.poll(allNodesInsideFlow).toBe(true)

      await page.reload()
      await expect(nodes).toHaveCount(5)
      await expect.poll(allNodesInsideFlow).toBe(true)

      // The fit uses React Flow's actual canvas region, not the browser window. Switch to a graph
      // with deliberately different geometry while the Inspector is collapsed, then back again.
      // That proves a same-count stale RF node set cannot consume the next document's request.
      await nodes.first().click()
      await page.getByRole('button', { name: 'Collapse Inspector', exact: true }).click()
      await backToWorkspace(page)
      await (await workspaceResource(page, 'canvas', otherCanvasName)).click()
      await expect(nodes).toHaveCount(5)
      await expect(nodes.first()).toHaveAttribute('data-id', 'other-step-0')
      await expect.poll(allNodesInsideFlow).toBe(true)
      await backToWorkspace(page)
      await (await workspaceResource(page, 'canvas', canvasName)).click()
      await expect(nodes).toHaveCount(5)
      await expect(nodes.first()).toHaveAttribute('data-id', 'step-0')
      await expect.poll(allNodesInsideFlow).toBe(true)
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
      await page.request.delete(`/api/canvas/${encodeURIComponent(otherCanvasId)}`)
    }
  })

  test('Fit view keeps dense graphs readable and reachable at both desktop sizes @canvas-viewport', async ({ page }) => {
    const canvasId = `dense-readable-fit-${Date.now()}`
    const nodes = Array.from({ length: 14 }, (_, index) => {
      const firstRow = index < 3
      return {
        id: `dense-${index}`,
        type: index === 0 ? 'source' : 'filter',
        position: {
          x: 80 + (firstRow ? index : index - 3) * 330,
          y: firstRow ? 100 : 460,
        },
        data: {
          title: index === 0 ? 'events' : `Filter stage ${String(index).padStart(2, '0')}`,
          status: 'idle',
          config: index === 0 ? { uri: 'events' } : { predicate: 'value IS NOT NULL' },
        },
      }
    })
    const edges = nodes.slice(1).map((node, index) => ({
      id: `dense-edge-${index + 1}`,
      source: nodes[index].id,
      target: node.id,
    }))
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Dense readable fit', version: 1, requirements: [], nodes, edges,
    } })
    expect(created.ok()).toBe(true)

    try {
      for (const viewport of [{ width: 1280, height: 720 }, { width: 1440, height: 900 }]) {
        await page.setViewportSize(viewport)
        await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
        await expect(page.locator('.react-flow__node')).toHaveCount(14)
        const fit = page.getByRole('button', { name: 'Fit view', exact: true })
        await fit.click()
        await page.waitForTimeout(350)

        expect(await canvasZoom(page)).toBeCloseTo(0.6, 5)
        const firstFit = await page.locator('.react-flow__viewport').getAttribute('style')
        await fit.click()
        await page.waitForTimeout(350)
        expect(await page.locator('.react-flow__viewport').getAttribute('style')).toBe(firstFit)

        const flow = await boxOf(page.locator('.react-flow'))
        const fittedBoxes = await page.locator('.react-flow__node').evaluateAll((elements) =>
          elements.map((element) => {
            const box = element.getBoundingClientRect()
            return { x: box.x, y: box.y, width: box.width, height: box.height }
          }))
        expect(
          fittedBoxes.some((box) => box.x < flow.x || box.x + box.width > flow.x + flow.width),
          'dense Fit should preserve readability instead of shrinking every card into the viewport',
        ).toBe(true)
        expect(fittedBoxes[6]?.width ?? 0).toBeGreaterThanOrEqual(139)
        await expect(page.locator('.react-flow__minimap')).toBeVisible()

        // Horizontal pan-on-scroll remains the direct way to reach cards outside the readable fit.
        await page.locator('.react-flow__pane').hover()
        await page.mouse.wheel(1200, 0)
        await page.waitForTimeout(350)
        const last = await boxOf(page.locator('[data-id="dense-13"]'))
        expect(last.x).toBeLessThan(flow.x + flow.width)
        expect(last.x + last.width).toBeGreaterThan(flow.x)
      }
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

  test('first-run choice preserves work, respects run-history safety, and never resets a manual viewport @first-run', async ({ page, request }) => {
    await useFreshFirstRunUser(page, request, 'First-run replacement safety')
    await page.goto('/')
    expect(await canvasesFor(page)).toEqual([])
    await expect(page.getByRole('button', { name: 'Start a blank Canvas' })).toBeVisible()
    await expect(page.getByRole('button', { name: /Open example/i }).first()).toBeVisible()
    const firstDataset = page.getByRole('button', { name: /^Open dataset / }).first()
    await expect(firstDataset).toBeVisible()
    const firstDatasetBox = await firstDataset.boundingBox()
    expect(firstDatasetBox).not.toBeNull()
    expect(firstDatasetBox!.y + firstDatasetBox!.height).toBeLessThanOrEqual(720)

    // Explicit blank + no durable run history is the sole in-place replacement case.
    await page.getByRole('button', { name: 'Start a blank Canvas' }).click()
    await expect(page.getByTestId('toolbar')).toBeVisible()
    const pristineId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
    expect(await canvasesFor(page)).toHaveLength(1)
    await page.getByRole('button', { name: 'Use example in this Canvas: Purchases per user' }).click()
    await expect(page.locator('.react-flow__node').first()).toBeVisible()
    expect(decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)).toBe(pristineId)
    expect(await canvasesFor(page)).toHaveLength(1)
    const viewport = page.locator('.react-flow__viewport')
    await page.getByRole('button', { name: 'Zoom in', exact: true }).click()
    const manual = await viewport.getAttribute('style')
    await page.waitForTimeout(500)
    expect(await viewport.getAttribute('style')).toBe(manual)

    // An edit made while the mutation revalidates run history must stay on this Canvas and reach
    // durable storage; cancelling this click gives the existing autosave debounce time to finish.
    await createCanvasFromWorkspace(page, 'untitled')
    await expect(page.locator('.react-flow__node')).toHaveCount(0)
    const blankId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
    const blankHash = await page.evaluate(() => location.hash)
    const afterBlankCount = (await canvasesFor(page)).length
    await expect(page.getByRole('button', { name: 'Use example in this Canvas: Purchases per user' })).toBeVisible()
    let historyRequestStarted = false
    let releaseHistory!: () => void
    await page.route(`**/api/canvas/${blankId}/runs`, async (route) => {
      historyRequestStarted = true
      await new Promise<void>((resolve) => { releaseHistory = resolve })
      await route.fulfill({ json: [] })
    })

    await page.getByRole('button', { name: 'Use example in this Canvas: Purchases per user' }).click()
    await expect.poll(() => historyRequestStarted).toBe(true)
    await page.getByRole('button', { name: 'Choose dataset' }).click()
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await expect(page.getByTestId('source-search')).toBeVisible()
    releaseHistory()
    await expect(page.getByText(/Canvas changed while preparing the example; your edit was kept/)).toBeVisible()
    expect((await page.evaluate(() => location.hash)).split('?')[0]).toBe(blankHash.split('?')[0])
    expect(decodeURIComponent(new URL(page.url()).hash.split('/').pop()!.split('?')[0])).toBe(blankId)
    expect(await canvasesFor(page)).toHaveLength(afterBlankCount)
    await expect.poll(async () => (await canvasFor(page, blankId)).nodes.length).toBe(1)

    // Examples remain on empty/first-run surfaces rather than leaking into current-Canvas actions.
    await page.unroute(`**/api/canvas/${blankId}/runs`)
    const currentCanvasMenu = await openSettledAppMenu(page)
    await expect(currentCanvasMenu.getByText('Purchases per user')).toHaveCount(0)
    await page.keyboard.press('Escape')

    // A lost PUT response retains a version-fenced local draft; it must not turn into a speculative create.
    await createCanvasFromWorkspace(page, 'untitled')
    await expect(page.locator('.react-flow__node')).toHaveCount(0)
    const responseLossId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
    let abortedPut = false
    await page.route(`**/api/canvas/${responseLossId}*`, (route) => {
      if (route.request().method() === 'PUT') {
        abortedPut = true
        return route.abort('connectionreset')
      }
      return route.continue()
    })

    await page.getByRole('button', { name: 'Use example in this Canvas: Purchases per user' }).click()
    await expect.poll(() => abortedPut).toBe(true)
    await expect(page.locator('.react-flow__node').first()).toBeVisible()
    expect(decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)).toBe(responseLossId)
    expect((await canvasFor(page, responseLossId)).nodes).toEqual([])
  })

  test('loads with no console errors', async ({ page }) => {
    const errors: string[] = []
    page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto('/')
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await page.waitForTimeout(500)
    expect(errors, errors.join('\n')).toEqual([])
  })

  test('toolbar category menu opens above the toolbar and does not jump', async ({ page }) => {
    await page.goto('/')
    const toolbar = page.getByTestId('toolbar')
    await page.getByRole('button', { name: 'Shape', exact: true }).click()
    const menu = page.locator('.dp-panel', { hasText: 'filter' }).last()
    await expect(menu).toBeVisible()
    await page.waitForTimeout(200) // let the .12s open animation (translateY -2px) settle before the baseline,
    const first = await boxOf(menu) // so this measures a re-position JUMP on a later tick, not the open transition
    await page.waitForTimeout(350) // if it re-positioned on a later tick, this would catch the shift
    const second = await boxOf(menu)
    expect(Math.abs(first.x - second.x)).toBeLessThan(2)
    expect(Math.abs(first.y - second.y)).toBeLessThan(2)
    // grows upward: the menu sits entirely above the toolbar
    const tb = await boxOf(toolbar)
    expect(second.y + second.height).toBeLessThanOrEqual(tb.y + 2)
  })

  test('the bottom toolbar uses direct categories without a redundant global add entry', async ({ page }) => {
    await fresh(page)
    await page.setViewportSize({ width: 1280, height: 720 })
    await expect(page.getByRole('button', { name: 'Add operation', exact: true })).toHaveCount(0)
    await addNode(page, 'Shape', 'filter')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await expect(page.getByRole('button', { name: 'Shape', exact: true })).toBeVisible()
    await addNode(page, 'Shape', 'sample')
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await page.getByRole('button', { name: 'Shape', exact: true }).click()
    await expect(page.locator('.dp-panel', { hasText: 'filter' }).last()).toBeVisible()
  })

  test('a toolbar operation continues from the sole selected compatible node', async ({ page }) => {
    await fresh(page)
    await page.setViewportSize({ width: 1280, height: 720 })
    await addNode(page, 'Sources & sinks', 'source')
    await expect(page.locator('.react-flow__node-source')).toHaveClass(/selected/)

    await addNode(page, 'Shape', 'filter')

    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await expect(page.locator('.react-flow__edge')).toHaveCount(1)
    const canvasId = decodeURIComponent(new URL(page.url()).hash.split('?')[0].split('/').pop()!)
    await expect.poll(async () => {
      const saved = await canvasFor(page, canvasId)
      const nodeTypes = Object.fromEntries((saved.nodes as Array<{ id: string; type: string }>).map((node) => [node.id, node.type]))
      return (saved.edges as Array<{ source: string; target: string }>).map((edge) => ({
        source: nodeTypes[edge.source], target: nodeTypes[edge.target],
      }))
    }).toEqual([{ source: 'source', target: 'filter' }])
  })

  test('selection keeps category add while the output port creates one atomic connected step', async ({ page }) => {
    const canvasId = `local-port-add-${Date.now()}`
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Local port add', version: 1, requirements: [], nodes: [], edges: [],
    } })
    expect(created.ok()).toBe(true)
    await page.goto(`/#/canvas/${canvasId}`)
    await page.setViewportSize({ width: 1280, height: 720 })
    await addNode(page, 'Sources & sinks', 'source')

    await expect(page.getByRole('button', { name: 'Add operation', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Sources & sinks', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Add next step' })).toHaveCount(0)
    await expect(page.getByTestId('toolbar-view-controls')).toHaveCount(0)

    const port = page.getByRole('button', { name: 'Add operation from dataset output' })
    await expect(port).toBeVisible()
    await port.press('Enter')
    const picker = page.getByRole('dialog', { name: 'Connect to an operation' })
    await expect(picker).not.toHaveAttribute('aria-modal')
    await expect(page.locator('.dp-modal-overlay')).toHaveCount(0)
    const search = picker.getByRole('textbox', { name: 'Search operations' })
    await expect(search).toBeFocused()
    await search.fill('filter')
    await search.press('Enter')
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await expect(page.locator('.react-flow__edge')).toHaveCount(1)

    await page.getByRole('button', { name: 'Undo', exact: true }).click()
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await expect(page.locator('.react-flow__edge')).toHaveCount(0)
    await page.getByRole('button', { name: 'Redo', exact: true }).click()
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await expect(page.locator('.react-flow__edge')).toHaveCount(1)
    await expect.poll(async () => {
      const saved = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      return {
        nodeTypes: saved.nodes.map((node: { type: string }) => node.type).sort(),
        edgeCount: saved.edges.length,
      }
    }).toEqual({ nodeTypes: ['filter', 'source'], edgeCount: 1 })
  })

  test('existing-node locator selects and centers an off-screen duplicate without mutating the graph', async ({ page }) => {
    const canvasId = `node-locator-${Date.now()}`
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Existing node locator', version: 1,
      nodes: [
        { id: 'duplicate-near', type: 'filter', position: { x: 80, y: 80 }, data: { title: 'Duplicate', status: 'stale', config: {} } },
        { id: 'duplicate-off-screen', type: 'filter', position: { x: 8000, y: 6000 }, data: { title: 'Duplicate', status: 'failed', config: {}, disabled: true } },
      ], edges: [],
    } })
    expect(created.ok()).toBe(true)
    await page.goto(`/#/canvas/${canvasId}`)
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    const viewport = page.locator('.react-flow__viewport')
    const beforeViewport = await viewport.getAttribute('style')
    const saves: string[] = []
    await page.route(`**/api/canvas/${canvasId}`, async (route) => {
      if (route.request().method() === 'PUT') saves.push(route.request().postData() ?? '')
      await route.continue()
    })

    await page.getByRole('button', { name: 'Locate existing node', exact: true }).click()
    const locator = page.getByRole('dialog', { name: 'Locate an existing node' })
    const search = locator.getByRole('textbox', { name: 'Search existing nodes' })
    await search.fill('duplicate-off-screen')
    await expect(locator.getByRole('option', { name: /duplicate-off-screen/i })).toContainText('stale · disabled')
    await search.press('Enter')

    await expect(locator).toBeHidden()
    const selected = page.locator('.react-flow__node[data-id="duplicate-off-screen"]')
    await expect(selected).toBeVisible()
    await expect(selected).toHaveClass(/selected/)
    await expect(page.getByTestId('inspector')).toContainText('Duplicate')
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await expect.poll(() => viewport.getAttribute('style')).not.toBe(beforeViewport)
    await expect.poll(async () => {
      const nodeBox = await selected.boundingBox()
      const canvasBox = await page.locator('.react-flow').boundingBox()
      return !!nodeBox && !!canvasBox
        && nodeBox.x >= canvasBox.x && nodeBox.y >= canvasBox.y
        && nodeBox.x + nodeBox.width <= canvasBox.x + canvasBox.width
        && nodeBox.y + nodeBox.height <= canvasBox.y + canvasBox.height
    }).toBe(true)
    await page.waitForTimeout(700) // longer than autosave debounce: locating must remain presentation-only
    expect(saves).toEqual([])
    const stored = await page.request.get(`/api/canvas/${canvasId}`)
    expect((await stored.json()).nodes).toEqual([
      { id: 'duplicate-near', type: 'filter', position: { x: 80, y: 80 }, data: { title: 'Duplicate', status: 'stale', config: {} } },
      { id: 'duplicate-off-screen', type: 'filter', position: { x: 8000, y: 6000 }, data: { title: 'Duplicate', status: 'failed', config: {}, disabled: true } },
    ])
    await page.unroute(`**/api/canvas/${canvasId}`)
  })

  test('a node deep link reveals once, preserves later viewport control, and handles a deleted node', async ({ page }) => {
    const canvasId = `node-deep-link-${Date.now()}`
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Node deep link', version: 1,
      nodes: [
        { id: 'near', type: 'filter', position: { x: 80, y: 80 }, data: { title: 'Near node', status: 'idle', config: {} } },
        { id: 'off-screen', type: 'filter', position: { x: 8000, y: 6000 }, data: { title: 'Off-screen node', status: 'idle', config: {} } },
      ], edges: [],
    } })
    expect(created.ok()).toBe(true)
    const saves: string[] = []
    await page.route(`**/api/canvas/${canvasId}`, async (route) => {
      if (route.request().method() === 'PUT') saves.push(route.request().postData() ?? '')
      await route.continue()
    })

    const near = page.locator('.react-flow__node[data-id="near"]')
    const offScreen = page.locator('.react-flow__node[data-id="off-screen"]')
    const isInCanvas = async (target = offScreen) => {
      const node = await target.boundingBox()
      const canvas = await page.locator('.react-flow').boundingBox()
      return !!node && !!canvas && node.x >= canvas.x && node.y >= canvas.y
        && node.x + node.width <= canvas.x + canvas.width && node.y + node.height <= canvas.y + canvas.height
    }

    await page.goto(`/#/canvas/${canvasId}?node=off-screen`)
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect(offScreen).toHaveClass(/selected/)
    await expect(page.getByTestId('inspector').getByRole('button', { name: 'View data' })).toBeVisible()
    await expect.poll(isInCanvas).toBe(true)

    // The route consumes its reveal once. A later user zoom remains in control rather than being
    // replaced by another route-driven center operation.
    const viewport = page.locator('.react-flow__viewport')
    await page.getByRole('button', { name: 'Zoom in', exact: true }).click()
    const afterUserZoom = await viewport.getAttribute('style')
    await page.waitForTimeout(500)
    expect(await viewport.getAttribute('style')).toBe(afterUserZoom)
    await page.waitForTimeout(700) // longer than autosave debounce: route presentation never saves
    expect(saves).toEqual([])

    await page.reload()
    await expect(offScreen).toHaveClass(/selected/)
    await expect.poll(isInCanvas).toBe(true)

    // Consuming the first request must not reuse its identity. A second valid node= route on the
    // same mounted Canvas selects and reveals the new target instead of looking already consumed.
    await page.evaluate((hash) => { location.hash = hash }, `#/canvas/${canvasId}?node=near`)
    await expect(near).toHaveClass(/selected/)
    await expect.poll(() => isInCanvas(near)).toBe(true)

    // The completed request must not survive a Canvas unmount. A later bare Canvas route uses the
    // user's latest viewport and must not replay the old off-screen center operation.
    await page.getByRole('button', { name: 'Locate existing node', exact: true }).click()
    const locator = page.getByRole('dialog', { name: 'Locate an existing node' })
    await locator.getByRole('textbox', { name: 'Search existing nodes' }).fill('near')
    await locator.getByRole('textbox', { name: 'Search existing nodes' }).press('Enter')
    await expect.poll(isInCanvas).toBe(false)
    await backToWorkspace(page)
    await (await workspaceResource(page, 'canvas', 'Node deep link')).click()
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect(offScreen).not.toHaveClass(/selected/)
    await page.waitForTimeout(500)
    expect(await isInCanvas()).toBe(false)

    await page.goto(`/#/canvas/${canvasId}?node=deleted-node`)
    await expect(page.getByText('The requested node is no longer in this Canvas.')).toBeVisible()
    await expect(page).toHaveURL(new RegExp(`#\\/canvas\\/${canvasId}$`))
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await page.unroute(`**/api/canvas/${canvasId}`)
  })

  test('ReactFlow click, shift, and rubber-band selections stay synchronized with Inspector', async ({ page }) => {
    const canvasId = `selection-sync-${Date.now()}`
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Selection sync', version: 1,
      nodes: [
        { id: 'select-a', type: 'filter', position: { x: 80, y: 80 }, data: { title: 'First', status: 'draft', config: {} } },
        { id: 'select-b', type: 'filter', position: { x: 420, y: 80 }, data: { title: 'Second', status: 'draft', config: {} } },
      ], edges: [],
    } })
    expect(created.ok()).toBe(true)
    await page.goto(`/#/canvas/${canvasId}`)
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    const first = page.locator('.react-flow__node[data-id="select-a"]')
    const second = page.locator('.react-flow__node[data-id="select-b"]')

    await first.click()
    await expect(first).toHaveClass(/selected/)
    await expect(page.getByTestId('inspector').getByRole('textbox', { name: 'Node title' })).toHaveValue('First')
    await second.click({ modifiers: ['Shift'] })
    await expect(first).toHaveClass(/selected/)
    await expect(second).toHaveClass(/selected/)
    await expect(page.getByTestId('inspector')).toHaveCount(0)

    const pane = page.locator('.react-flow__pane')
    await pane.click({ position: { x: 5, y: 5 } })
    await expect(first).not.toHaveClass(/selected/)
    await expect(second).not.toHaveClass(/selected/)
    await expect(page.getByTestId('inspector')).toHaveCount(0)
    const firstBox = await boxOf(first)
    const secondBox = await boxOf(second)
    await page.mouse.move(firstBox.x - 12, firstBox.y - 12)
    await page.mouse.down()
    await page.mouse.move(firstBox.x + firstBox.width + 12, firstBox.y + firstBox.height + 12, { steps: 5 })
    await expect(first).toHaveClass(/selected/) // selection has already reconciled through the store mid-drag
    await expect(second).not.toHaveClass(/selected/)
    await page.mouse.move(secondBox.x + secondBox.width + 12, secondBox.y + secondBox.height + 12, { steps: 5 })
    await page.mouse.up()
    await expect(first).toHaveClass(/selected/)
    await expect(second).toHaveClass(/selected/)
    await expect(page.getByTestId('inspector')).toHaveCount(0)
  })

  test('the Appearance submenu switches between light and dark (and flips the tokens)', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'light' })  // deterministic default (no OS 'dark' bleed-through)
    const canvasId = `appearance-${Date.now()}`
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'Appearance settings', version: 1, nodes: [], edges: [],
    } })
    expect(created.ok(), await created.text()).toBe(true)
    await page.goto(`/#/canvas/${canvasId}`)
    await expect(page.getByTestId('app-menu')).toBeVisible()
    const html = page.locator('html')
    await expect(html).not.toHaveAttribute('data-theme', 'dark')  // light is the default
    await page.getByTestId('app-menu').click()
    await page.getByRole('menuitem', { name: 'Appearance' }).hover()
    await page.getByRole('menuitemradio', { name: 'Dark' }).click()
    await expect(html).toHaveAttribute('data-theme', 'dark')
    // the shadcn token actually flips (not just the attribute) — proves the palette is wired
    const bg = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--background').trim())
    expect(bg).toBe('222 24% 10%')
    await expect(page.getByRole('menuitem', { name: 'Appearance' })).toBeHidden()
    await page.getByTestId('app-menu').click()
    await page.getByRole('menuitem', { name: 'Appearance' }).hover()
    await page.getByRole('menuitemradio', { name: 'Light' }).click()
    await expect(html).not.toHaveAttribute('data-theme', 'dark')
  })

  test('added nodes do not overlap each other', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    await addNode(page, 'Shape', 'filter')
    const nodes = page.locator('.react-flow__node')
    await expect(nodes).toHaveCount(2)
    const a = await boxOf(nodes.nth(0))
    const b = await boxOf(nodes.nth(1))
    expect(overlaps(a, b), 'two freshly added nodes overlap').toBe(false)
  })

  test('duplicating a node does not stack it on the original', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Query', 'sql')
    const nodes = page.locator('.react-flow__node')
    await expect(nodes).toHaveCount(1)
    await page.getByRole('button', { name: 'More' }).click()
    // scope to the ⋯ menu popover — the inspector also has a Duplicate action for the selected node
    await page.locator('.dp-panel').getByRole('button', { name: 'Duplicate' }).click()
    await expect(nodes).toHaveCount(2)
    expect(overlaps(await boxOf(nodes.nth(0)), await boxOf(nodes.nth(1))), 'duplicated node overlaps the original').toBe(false)
  })

  test('action tooltips escape the card (not clipped by overflow:hidden)', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Query', 'sql')
    await page.getByRole('button', { name: 'Connect a source to preview' }).hover()
    const tip = page.getByText('Connect a source to preview', { exact: true })
    await expect(tip).toBeVisible()
    // the fix: the tooltip is portaled to <body>, not rendered inside the (clipping) node card
    const insideCard = await tip.evaluate((el) => !!el.closest('.react-flow__node'))
    expect(insideCard, 'tooltip is still inside the node card and gets clipped').toBe(false)
  })

  test('the port picker stays local across supported viewports and creates one connected node', async ({ page }) => {
    test.setTimeout(60_000)
    await fresh(page)
    await addNode(page, 'Query', 'sql')
    const port = page.getByRole('button', { name: 'Add operation from dataset output' })
    const finder = page.getByRole('dialog', { name: 'Connect to an operation' })

    for (const viewport of [
      { width: 1024, height: 720 },
      { width: 1280, height: 720 },
      { width: 1440, height: 900 },
    ]) {
      await page.setViewportSize(viewport)
      for (const collapsed of [false, true]) {
        const collapse = page.getByRole('button', { name: 'Collapse Inspector', exact: true })
        const expand = page.getByRole('button', { name: 'Expand Inspector', exact: true })
        if (collapsed && await collapse.isVisible().catch(() => false)) await collapse.click()
        if (!collapsed && await expand.isVisible().catch(() => false)) await expand.click()
        await expect(collapsed ? expand : collapse).toBeVisible()

        await port.press('Enter')
        await expect(finder).toBeVisible()
        await expect(finder).not.toHaveAttribute('aria-modal')
        await expect(page.locator('.dp-modal-overlay')).toHaveCount(0)
        await expect(finder.getByRole('textbox', { name: 'Search operations' })).toBeFocused()
        const [portBox, finderBox, canvasBox, toolbarBox] = await Promise.all([
          boxOf(port), boxOf(finder), boxOf(page.locator('.react-flow')), boxOf(page.getByTestId('toolbar')),
        ])
        expect(finderBox.width).toBeGreaterThanOrEqual(360)
        expect(finderBox.width).toBeLessThanOrEqual(420)
        expect(contains(canvasBox, finderBox), `${viewport.width}px port picker left the Canvas with Inspector ${collapsed ? 'collapsed' : 'expanded'}`).toBe(true)
        expect(finderBox.y + finderBox.height).toBeLessThanOrEqual(toolbarBox.y - 7)
        const rightGap = Math.abs(finderBox.x - (portBox.x + portBox.width))
        const leftGap = Math.abs(finderBox.x + finderBox.width - portBox.x)
        // At 1024px neither full 400px side may fit; clamping may leave a small gap rather than
        // covering the source card or crossing into the Inspector.
        expect(Math.min(rightGap, leftGap)).toBeLessThanOrEqual(64)
        const portCenterY = portBox.y + portBox.height / 2
        expect(portCenterY).toBeGreaterThanOrEqual(finderBox.y)
        expect(portCenterY).toBeLessThanOrEqual(finderBox.y + finderBox.height)

        await finder.getByRole('textbox', { name: 'Search operations' }).press('Escape')
        await expect(finder).toBeHidden()
        await expect(port).toBeFocused()
      }
    }

    await port.press('Enter')
    const search = finder.getByRole('textbox', { name: 'Search operations' })
    await search.fill('transform')
    await expect(finder.getByRole('option', { name: /transform/i })).toHaveCount(1)
    await search.press('Enter')
    await expect(finder).toBeHidden()
    const nodes = page.locator('.react-flow__node')
    await expect(nodes).toHaveCount(2)
    await expect(page.locator('.react-flow__edge')).toHaveCount(1)
    const sourceBox = await boxOf(nodes.nth(0))
    const targetBox = await boxOf(nodes.nth(1))
    expect(targetBox.x, 'the connected target is downstream, not back through its source')
      .toBeGreaterThan(sourceBox.x + sourceBox.width + 80)

    await page.getByRole('button', { name: 'Undo', exact: true }).click()
    await expect(nodes).toHaveCount(1)
    await expect(page.locator('.react-flow__edge')).toHaveCount(0)
    await page.getByRole('button', { name: 'Redo', exact: true }).click()
    await expect(nodes).toHaveCount(2)
    await expect(page.locator('.react-flow__edge')).toHaveCount(1)

    await page.reload()
    const reloadedNodes = page.locator('.react-flow__node')
    await expect(reloadedNodes).toHaveCount(2)
    const reloadedSource = await boxOf(reloadedNodes.nth(0))
    const reloadedTarget = await boxOf(reloadedNodes.nth(1))
    expect(reloadedTarget.x, 'reload preserves the readable downstream order')
      .toBeGreaterThan(reloadedSource.x + reloadedSource.width + 80)
  })

  test('two Sources → Join → Sample → Transform stays readable at both supported desktop sizes', async ({ page }) => {
    test.setTimeout(60_000)
    for (const viewport of [{ width: 1280, height: 720 }, { width: 1440, height: 900 }]) {
      await page.setViewportSize(viewport)
      const canvasId = `readable-topology-${viewport.width}-${Date.now()}`
      const created = await page.request.post('/api/canvas', { data: {
        id: canvasId, name: `Readable topology ${viewport.width}`, version: 1,
        requirements: [], nodes: [], edges: [],
      } })
      expect(created.ok()).toBe(true)
      await page.goto(`/#/canvas/${canvasId}`)

      await addNode(page, 'Sources & sinks', 'source')
      await addNode(page, 'Sources & sinks', 'source')
      const sources = page.locator('.react-flow__node-source')
      await expect(sources).toHaveCount(2)
      await page.getByRole('button', { name: 'Fit view', exact: true }).click()

      await addFromOutput(page, sources.nth(0), 'join')
      const join = page.locator('.react-flow__node-join')
      await expect(join).toHaveCount(1)
      await expect(page.locator('[data-node-reveal-pending]'))
        .toHaveAttribute('data-node-reveal-pending', 'false')
      await connectHandles(page, sources.nth(1), join.locator('.react-flow__handle-left').nth(1))
      await expect(page.locator('.react-flow__edge')).toHaveCount(2)
      await page.getByRole('button', { name: 'Fit view', exact: true }).click()

      await addFromOutput(page, join, 'sample')
      const sample = page.locator('.react-flow__node-sample')
      await expect(sample).toHaveCount(1)
      await page.getByRole('button', { name: 'Fit view', exact: true }).click()
      await addFromOutput(page, sample, 'transform')
      const transform = page.locator('.react-flow__node-transform')
      await expect(transform).toHaveCount(1)
      await expect(page.locator('.react-flow__edge')).toHaveCount(4)
      await page.getByRole('button', { name: 'Fit view', exact: true }).click()
      await page.waitForTimeout(350) // fitView animates; measure one settled coordinate space

      const sourceBoxes = [await boxOf(sources.nth(0)), await boxOf(sources.nth(1))]
      const joinBox = await boxOf(join)
      const sampleBox = await boxOf(sample)
      const transformBox = await boxOf(transform)
      expect(joinBox.x).toBeGreaterThan(Math.max(...sourceBoxes.map((box) => box.x + box.width)))
      expect(sampleBox.x).toBeGreaterThan(joinBox.x + joinBox.width)
      expect(transformBox.x).toBeGreaterThan(sampleBox.x + sampleBox.width)
      expect(await edgeNodeCrossings(page)).toEqual([])

      const transformId = await transform.getAttribute('data-id')
      expect(transformId).toBeTruthy()
      await expect.poll(async () => {
        const saved = await page.request.get(`/api/canvas/${canvasId}`).then((response) => response.json())
        return saved.nodes.some((node: { id: string }) => node.id === transformId)
      }).toBe(true)
      const beforeDrag = await page.request.get(`/api/canvas/${canvasId}`).then((response) => response.json())
      const beforePosition = beforeDrag.nodes.find((node: { id: string }) => node.id === transformId).position
      const dragBox = await boxOf(transform)
      await page.mouse.move(dragBox.x + dragBox.width / 2, dragBox.y + 18)
      await page.mouse.down()
      await page.mouse.move(dragBox.x + dragBox.width / 2, dragBox.y + 78, { steps: 10 })
      await page.mouse.up()
      let draggedPosition: { x: number; y: number } | null = null
      await expect.poll(async () => {
        const response = await page.request.get(`/api/canvas/${canvasId}`)
        const doc = await response.json()
        const node = doc.nodes.find((candidate: { id: string }) => candidate.id === transformId)
        draggedPosition = node.position
        return node.data.autoPlaced === false && node.position.y !== beforePosition.y
      }).toBe(true)

      await page.reload()
      await expect(page.locator(`[data-id="${transformId}"]`)).toBeVisible()
      const reopened = await page.request.get(`/api/canvas/${canvasId}`).then((response) => response.json())
      expect(reopened.nodes.find((node: { id: string }) => node.id === transformId).position)
        .toEqual(draggedPosition)
      await page.getByRole('button', { name: 'Fit view', exact: true }).click()
      await page.waitForTimeout(350) // compare boxes after the fitted viewport settles
      const reopenedJoin = await boxOf(page.locator('.react-flow__node-join'))
      const reopenedSample = await boxOf(page.locator('.react-flow__node-sample'))
      const reopenedTransform = await boxOf(page.locator('.react-flow__node-transform'))
      expect(reopenedSample.x).toBeGreaterThan(reopenedJoin.x + reopenedJoin.width)
      expect(reopenedTransform.x).toBeGreaterThan(reopenedSample.x + reopenedSample.width)
    }
  })

  test('dragging from an output port and releasing shows no menu', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Query', 'sql')
    const handle = page.locator('.react-flow__node .react-flow__handle-right').first()
    const b = await boxOf(handle)
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2)
    await page.mouse.down()
    await page.mouse.move(b.x + 160, b.y + 120, { steps: 8 }) // a real drag onto empty pane
    await page.mouse.up()
    await expect(page.getByRole('dialog', { name: 'Connect to an operation' })).toHaveCount(0) // drag-release must not pop the picker
  })

  test('a node with no upstream source has Run disabled', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Query', 'sql')
    const run = page.getByRole('button', { name: 'Connect a source to run' })
    await expect(run).toBeVisible()
    await expect(run).toHaveAttribute('aria-disabled', 'true')
  })

  test('an auto-connected node says the source needs a dataset, not that it needs a source', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Sources & sinks', 'source')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    // the toolbar connects the new node to the selected one, so this filter has an upstream source
    await addNode(page, 'Shape', 'filter')
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await expect.poll(async () => page.locator('.react-flow__edge').count()).toBe(1)

    await expect(page.getByRole('button', { name: 'Connect a source to run' })).toHaveCount(0)
    const run = page.getByRole('button', { name: /^Choose a dataset in / }).last()
    await expect(run).toBeAttached()
    await expect(run).toHaveAttribute('aria-disabled', 'true')
  })

  test('there is no Save button — the canvas auto-saves', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('button', { name: /^save/i })).toHaveCount(0)
    await expect(page.getByTestId('autosave')).toHaveText(/saved|saving/)
  })

  test('an online Canvas edit inside the autosave debounce survives tab reload as a local draft', async ({ page }) => {
    const canvasId = `local-draft-overview-${Date.now()}`
    const created = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'untitled', version: 1,
      nodes: Array.from({ length: 5 }, (_, index) => ({
        id: `draft-step-${index}`, type: 'filter', position: { x: 120 + index * 400, y: 160 },
        data: { title: `Draft step ${index + 1}`, status: 'idle', config: {} },
      })),
      edges: [],
    } })
    expect(created.ok()).toBe(true)
    await page.goto(`/#/canvas/${canvasId}`)
    const nodes = page.locator('.react-flow__node')
    await expect(nodes).toHaveCount(5)
    await expect(page.getByTestId('autosave')).toHaveText(/saved/, { timeout: 8_000 })
    await waitForCollabRoom(page, canvasId)
    const name = `Close recovery ${Date.now()}`
    let unloadPuts = 0
    const canvasUrl = `**/api/canvas/${canvasId}`
    await page.route(canvasUrl, async (route) => {
      if (route.request().method() === 'PUT') {
        unloadPuts += 1
        await route.abort('connectionfailed')
        return
      }
      await route.continue()
    })

    await page.getByTestId('canvas-title').click()
    await page.getByRole('textbox', { name: 'Canvas name' }).fill(name)
    await page.reload()

    await expect(page).toHaveURL(new RegExp(`#\/canvas\/${canvasId}$`))
    await expect(page.getByTestId('canvas-title')).toContainText(name)
    await expect(page.getByTestId('autosave')).toHaveText(/saved locally/)
    await expect.poll(async () => {
      const flow = await page.locator('.react-flow').boundingBox()
      const nodeBoxes = await nodes.evaluateAll((elements) => elements.map((element) => {
        const box = element.getBoundingClientRect()
        return { x: box.x, y: box.y, width: box.width, height: box.height }
      }))
      return !!flow && nodeBoxes.length === 5 && nodeBoxes.every((node) => (
        node.x >= flow.x && node.y >= flow.y
        && node.x + node.width <= flow.x + flow.width
        && node.y + node.height <= flow.y + flow.height
      ))
    }).toBe(true)
    expect(unloadPuts).toBe(0)

    await page.unroute(canvasUrl)
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace', { exact: true }).click()
    await page.getByRole('button', { name: `Retry local draft ${name}` }).click()
    await expect(page.getByRole('button', { name: `Retry local draft ${name}` })).toHaveCount(0, { timeout: 8_000 })
    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${canvasId}`)
      return response.ok() ? ((await response.json()) as { name: string }).name : null
    }).toBe(name)
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    await expect(page.getByTestId('canvas-title')).toContainText(name)
    await expect(page.getByTestId('autosave')).toHaveText(/saved$/, { timeout: 8_000 })
  })

  test('icon-only viewport controls stay below the minimap without overlapping the toolbar', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Shape', 'filter') // minimap + viewport controls only mount once the canvas has a node to navigate
    const minimap = page.locator('.react-flow__minimap')
    const toolbar = page.getByTestId('toolbar')
    const viewportControls = page.getByTestId('canvas-viewport-controls')
    await expect(minimap).toBeVisible()
    await expect(toolbar).toBeVisible()
    await expect(viewportControls).toBeVisible()
    await expect(viewportControls.getByRole('button', { name: 'Fit view' })).toBeVisible()
    await expect(viewportControls.getByText('Fit view', { exact: true })).toHaveCount(0)

    const expectLayout = async (inspectorState: string) => {
      const minimapBox = await boxOf(minimap)
      const viewportControlsBox = await boxOf(viewportControls)
      const toolbarBox = await boxOf(toolbar)
      expect(overlaps(minimapBox, viewportControlsBox), `minimap overlaps viewport controls with Inspector ${inspectorState}`).toBe(false)
      expect(overlaps(minimapBox, toolbarBox), `minimap overlaps toolbar with Inspector ${inspectorState}`).toBe(false)
      expect(overlaps(viewportControlsBox, toolbarBox), `viewport controls overlap toolbar with Inspector ${inspectorState}`).toBe(false)
      expect(viewportControlsBox.x).toBeGreaterThanOrEqual(minimapBox.x)
      expect(viewportControlsBox.x + viewportControlsBox.width).toBeLessThanOrEqual(minimapBox.x + minimapBox.width)
      expect(viewportControlsBox.y).toBeGreaterThanOrEqual(minimapBox.y + minimapBox.height)
    }
    await expectLayout('expanded')

    await page.getByRole('button', { name: 'Collapse Inspector', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Expand Inspector', exact: true })).toBeVisible()
    await expectLayout('collapsed')
    await viewportControls.getByRole('button', { name: 'Fit view' }).hover()
    await expect(page.getByRole('tooltip')).toHaveText('Fit view')
  })

  test('toolbar stays global and keeps disclosure state out of category tooltips', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Shape', 'filter')

    const addControls = page.getByTestId('toolbar-add-controls')
    const viewportControls = page.getByTestId('canvas-viewport-controls')
    await expect(addControls).toHaveAttribute('role', 'group')
    await expect(viewportControls).toHaveAttribute('role', 'group')
    await expect(addControls.getByText('Add', { exact: true })).toHaveCount(0)
    await expect(page.getByTestId('toolbar-view-controls')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Add next step' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Add operation', exact: true })).toHaveCount(0)
    const fitView = viewportControls.getByRole('button', { name: 'Fit view' })
    await expect(fitView).toBeVisible()
    await expect(viewportControls.getByText('Fit view', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Collapse Inspector', exact: true })).toBeVisible()

    const shape = addControls.getByRole('button', { name: 'Shape', exact: true })
    await shape.hover()
    await expect(page.getByRole('tooltip')).toHaveText('Shape')
    await shape.click()
    await expect(shape).toHaveAttribute('aria-expanded', 'true')
    await expect(shape).toHaveAttribute('aria-pressed', 'true')
  })

  test('agent is unavailable without a configured model (no rule-based stand-in)', async ({ page }) => {
    await fresh(page)
    await page.getByRole('button', { name: 'Agent', exact: true }).click()
    // no provider key configured in CI → the agent is clearly unavailable, not a fake offline planner
    await expect(page.getByText('unavailable', { exact: true })).toBeVisible()
    await expect(page.getByText('Agent unavailable')).toBeVisible()
    await expect(page.getByTestId('agent-submit')).toBeDisabled()
    // and it offers a way to fix it rather than silently building junk
    await expect(page.getByTestId('agent-configure')).toBeVisible()
  })

  test('the top bar has Rerun all, not Export', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('button', { name: /rerun all/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /^export$/i })).toHaveCount(0)
  })

  test('a markdown note node renders markdown on the canvas', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Inspect', 'note')
    const node = page.locator('.react-flow__node')
    await expect(node).toHaveCount(1)
    // default content renders a "Note" heading (react-markdown), and double-click edits it
    await expect(node.getByText('Note', { exact: true })).toBeVisible()
    await node.dblclick()
    await expect(node.locator('textarea')).toBeVisible()
  })

  test('a node can be renamed (⋯ menu → Rename)', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Query', 'sql')
    await page.getByRole('button', { name: 'More' }).click()
    await page.getByRole('button', { name: 'Rename', exact: true }).click()
    const input = page.locator('.react-flow__node input')
    await expect(input).toBeVisible()
    await input.fill('my query')
    // Blur is also a valid commit and may remove the input immediately after fill. Page-level Enter
    // commits when it is still focused and is harmless when blur already committed the title.
    await page.keyboard.press('Enter')
    await expect(page.locator('.react-flow__node').getByText('my query', { exact: true })).toBeVisible()
  })

  test('code cells use the Monaco editor (highlighting + the SQL text)', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Query', 'sql')
    await page.getByRole('button', { name: 'Edit code' }).click() // opens the single (fullscreen) editor
    const editor = page.locator('.monaco-editor').first()
    await expect(editor).toBeVisible({ timeout: 15_000 }) // Monaco lazy-loads + its worker boots
    await expect(editor).toContainText('SELECT')
  })

  test('global-toolbar additions keep selected action shelves clickable above the toolbar @ux-smoke', async ({ page }) => {
    await fresh(page)
    await page.setViewportSize({ width: 1280, height: 720 })

    const toolbar = page.getByTestId('toolbar')
    for (const [category, title] of [
      ['Sources & sinks', 'source'],
      ['Shape', 'filter'],
      ['Compute', 'transform'],
    ] as const) {
      await addNode(page, category, title)
      const addedNode = page.locator('.react-flow__node').last()
      await expect(addedNode.getByText(title, { exact: true }).first()).toBeVisible()
      const action = title === 'source' ? 'More' : title === 'transform' ? 'Edit code' : 'Output versions'
      const shelf = addedNode.getByRole('button', { name: action }).locator('..')
      await expect(shelf).toBeVisible()
      await expect.poll(async () => contains(
        await boxOf(page.locator('.react-flow')),
        await boxOf(shelf),
      ), { message: `${title} action shelf is outside the visible Canvas` }).toBe(true)
      expect(overlaps(await boxOf(shelf), await boxOf(toolbar)), `${title} action shelf overlaps the toolbar`).toBe(false)
    }

    const nodes = page.locator('.react-flow__node')
    await expect(nodes).toHaveCount(3)
    await expect(page.locator('.react-flow__edge')).toHaveCount(2)

    await page.getByRole('button', { name: 'Edit code' }).click()
    await expect(page.locator('.monaco-editor').first()).toBeVisible({ timeout: 15_000 })
    await expect(page.getByRole('button', { name: 'Agent' })).toHaveAttribute('aria-pressed', 'false')

    await page.keyboard.press('Escape')
    await page.setViewportSize({ width: 1440, height: 900 })
    await fresh(page)
    for (const [category, title] of [
      ['Sources & sinks', 'source'],
      ['Shape', 'filter'],
      ['Compute', 'transform'],
    ] as const) {
      await addNode(page, category, title)
      const addedNode = page.locator('.react-flow__node').last()
      await expect(addedNode.getByText(title, { exact: true }).first()).toBeVisible()
      const action = title === 'source' ? 'More' : title === 'transform' ? 'Edit code' : 'Output versions'
      const shelf = addedNode.getByRole('button', { name: action }).locator('..')
      await expect(shelf).toBeVisible()
      const shelfBox = await boxOf(shelf)
      expect(contains(await boxOf(page.locator('.react-flow')), shelfBox), `${title} action shelf is outside the reference Canvas`).toBe(true)
      expect(overlaps(shelfBox, await boxOf(page.getByTestId('toolbar'))), `${title} action shelf overlaps the reference toolbar`).toBe(false)
    }
  })

  test('the app menu returns to Workspace, where a fresh Canvas is created', async ({ page }) => {
    await fresh(page) // start on a known-empty new file (shared DB persists canvases across tests)
    await addNode(page, 'Shape', 'filter')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    const menu = await openSettledAppMenu(page)
    await expect(menu.getByRole('menuitem', { name: 'New Canvas', exact: true })).toHaveCount(0)
    await menu.getByRole('menuitem', { name: 'Back to Workspace', exact: true }).click()
    await expect(page.getByRole('navigation', { name: 'Workspace path' })).toBeVisible()
    await createCanvasFromWorkspace(page, 'Canvas from Workspace')
    await expect(page.locator('.react-flow__node')).toHaveCount(0) // a new file is a fresh canvas
  })

  test('native Canvas upload validates and creates a separate Canvas while the optional foreign importer stays hidden', async ({ page }) => {
    await fresh(page)
    const original = await page.evaluate(() => location.hash)
    const canvasId = decodeURIComponent(original.split('/').pop()!)
    const exported = await page.request.get(`/api/canvas/${canvasId}/native-export`)
    expect(exported.ok()).toBe(true)
    const envelope = await exported.json()

    const menu = await openSettledAppMenu(page)
    await expect(menu.getByTestId('import-pipeline')).toHaveCount(0)
    await menu.getByTestId('import-native-canvas').click()
    await page.locator('input[type="file"]').setInputFiles({
      name: 'round-trip.dp-canvas.json', mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify(envelope)),
    })
    await expect(page.getByText(/0 nodes · 0 connections/)).toBeVisible()
    await page.getByRole('button', { name: 'Import as new Canvas' }).click()
    await expect.poll(() => page.evaluate(() => location.hash)).not.toBe(original)
    await expect(page.getByRole('heading', { name: 'Import native Canvas' })).toBeHidden()
  })

  test('saves the persisted Canvas as an independent owned copy', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    await expect(page.getByTestId('autosave')).toHaveText(/saved/, { timeout: 8_000 })
    const original = await page.evaluate(() => location.hash)
    const sourceId = decodeURIComponent(original.split('?')[0].split('/').pop()!)
    await page.getByTestId('app-menu').click()
    await page.getByTestId('copy-canvas').click()
    await page.getByLabel('New Canvas name').fill('E2E independent copy')
    await page.getByRole('button', { name: 'Review copy' }).click()
    await expect(page.getByText('1 nodes · 0 connections · 0 requirements')).toBeVisible()
    await page.getByRole('button', { name: 'Duplicate and open' }).click()
    await expect.poll(() => page.evaluate(() => location.hash)).not.toBe(original)
    const copyId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
    const copied = await (await page.request.get(`/api/canvas/${copyId}`)).json()
    expect(copied.name).toBe('E2E independent copy')
    expect(copied.nodes).toHaveLength(1)
    expect(copied.nodes[0].type).toBe('filter')
    expect(copied.nodes[0].data.autoPlaced).toBe(true)
    expect(copied._copiedFrom).toMatchObject({ kind: 'canvas', canvasId: sourceId })
    expect(copied._copiedFrom.canvasVersion).toBeGreaterThanOrEqual(1)
  })

  test('copies a resolved registered Source without a dependency acknowledgement at 1280x720', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 })
    await fresh(page)
    await page.getByRole('button', { name: /Purchases per user/ }).click()
    await expect(page.locator('.react-flow__node')).toHaveCount(5)

    const source = page.locator('.react-flow__node-source')
    await source.hover()
    await source.getByRole('button', { name: 'View data' }).click()
    const preview = page.getByTestId('panel-data')
    await expect(preview.getByText(/^rows \d+–\d+$/)).toBeVisible({ timeout: 15_000 })
    await preview.getByTitle('Close').click()
    await expect(page.getByTestId('autosave')).toHaveText(/saved/, { timeout: 8_000 })

    const original = await page.evaluate(() => location.hash)
    const menu = await openSettledAppMenu(page)
    await menu.getByTestId('copy-canvas').click()
    const dialog = page.getByRole('dialog', { name: 'Duplicate canvas' })
    await dialog.getByRole('button', { name: 'Review copy' }).click()
    await expect(dialog.getByText('5 nodes · 4 connections · 0 requirements')).toBeVisible()
    await expect(dialog.getByRole('checkbox')).toHaveCount(0)
    const create = dialog.getByRole('button', { name: 'Duplicate and open' })
    await expect(create).toBeEnabled()
    const dialogBox = await boxOf(dialog)
    expect(dialogBox.x).toBeGreaterThanOrEqual(0)
    expect(dialogBox.y).toBeGreaterThanOrEqual(0)
    expect(dialogBox.x + dialogBox.width).toBeLessThanOrEqual(1280)
    expect(dialogBox.y + dialogBox.height).toBeLessThanOrEqual(720)

    await create.click()
    await expect.poll(() => page.evaluate(() => location.hash)).not.toBe(original)
    const copyId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
    const copied = await (await page.request.get(`/api/canvas/${copyId}`)).json() as {
      nodes: Array<{ type: string; data: Record<string, unknown> }>
    }
    for (const node of copied.nodes) {
      expect(node.data.status).toBe('draft')
      expect(node.data).not.toHaveProperty('history')
      expect(node.data).not.toHaveProperty('lastRun')
      expect(node.data).not.toHaveProperty('currentOutputVersionId')
      expect(node.data).not.toHaveProperty('result')
    }
    expect(await (await page.request.get(`/api/canvas/${copyId}/runs`)).json()).toEqual([])
    const inbox = await (await page.request.get('/api/inbox?filter=all')).json() as {
      items: Array<{ canvasId?: string }>
    }
    expect(inbox.items.some((item) => item.canvasId === copyId)).toBe(false)
  })

  test('pipeline import lands a returned graph on its newly created canvas', async ({ page }) => {
    await enablePipelineImporter(page)
    await fresh(page)
    const previous = await page.evaluate(() => location.hash)
    await page.route('**/api/pipelines/import', (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        config: '{}', params: {}, inputColumns: [], outputColumns: [], stages: [], driverSteps: [],
        graph: {
          nodes: [{ id: 'imported-source', type: 'source', position: { x: 80, y: 80 }, data: { title: 'Imported source', config: {} } }],
          edges: [],
        },
      }),
    }))

    const menu = await openSettledAppMenu(page)
    await menu.getByTestId('import-pipeline').click()
    await page.getByPlaceholder(/my_table_or_uri/).fill('{"source":"x"}')
    await page.getByRole('button', { name: 'Import', exact: true }).click()

    await expect.poll(() => page.evaluate(() => location.hash)).not.toBe(previous)
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await expect(page.getByText('Imported source', { exact: true })).toBeVisible()
  })

  test('a rejected import destination preserves the active canvas', async ({ page }) => {
    await enablePipelineImporter(page)
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    const current = await page.evaluate(() => location.hash)
    await page.route('**/api/pipelines/import', (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        config: '{}', params: {}, inputColumns: [], outputColumns: [], stages: [], driverSteps: [],
        graph: {
          nodes: [{ id: 'imported-source', type: 'source', position: { x: 80, y: 80 }, data: { title: 'Imported source', config: {} } }],
          edges: [],
        },
      }),
    }))
    await page.route('**/api/canvas*', (route) => {
      if (new URL(route.request().url()).pathname !== '/api/canvas' || route.request().method() !== 'POST') return route.continue()
      return route.fulfill({ status: 403, contentType: 'application/json', body: JSON.stringify({ detail: 'forbidden' }) })
    })

    await page.getByTestId('app-menu').click()
    await page.getByTestId('import-pipeline').click()
    await page.getByPlaceholder(/my_table_or_uri/).fill('{"source":"x"}')
    await page.getByRole('button', { name: 'Import', exact: true }).click()

    await expect(page.getByTestId('toast').filter({ hasText: 'permission' })).toContainText('permission')
    await expect.poll(() => page.evaluate(() => location.hash)).toBe(current)
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await expect(page.getByRole('heading', { name: 'Import pipeline' })).toBeVisible()
  })

  test('navigation cancels a pending pipeline importer without creating or navigating to a canvas', async ({ page }) => {
    await enablePipelineImporter(page)
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    let destinationPosts = 0
    await page.route('**/api/canvas*', async (route) => {
      if (new URL(route.request().url()).pathname === '/api/canvas' && route.request().method() === 'POST') destinationPosts += 1
      await route.continue()
    })

    let releaseImport!: () => void
    const importHeld = new Promise<void>((resolve) => { releaseImport = resolve })
    let markImportStarted!: () => void
    const importStarted = new Promise<void>((resolve) => { markImportStarted = resolve })
    let markImportRouteDone!: () => void
    const importRouteDone = new Promise<void>((resolve) => { markImportRouteDone = resolve })
    await page.route('**/api/pipelines/import', async (route) => {
      markImportStarted()
      await importHeld
      try {
        await route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify({
            config: '{}', params: {}, inputColumns: [], outputColumns: [], stages: [], driverSteps: [],
            graph: {
              nodes: [{ id: 'late-import', type: 'source', position: { x: 80, y: 80 }, data: { title: 'Late import', config: {} } }],
              edges: [],
            },
          }),
        })
      } catch { /* the AbortController may dispose the intercepted request before this late reply */ }
      markImportRouteDone()
    })

    await page.getByTestId('app-menu').click()
    await page.getByTestId('import-pipeline').click()
    await page.getByPlaceholder(/my_table_or_uri/).fill('{"source":"slow"}')
    await page.getByRole('button', { name: 'Import', exact: true }).click()
    await importStarted

    await page.evaluate(() => { location.hash = '#/workspace' })
    await expect(page.getByRole('heading', { name: 'Import pipeline' })).toBeHidden()
    await expect.poll(() => page.evaluate(() => location.hash)).toBe('#/workspace')
    releaseImport()
    await importRouteDone
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))

    expect(destinationPosts).toBe(0)
    await expect.poll(() => page.evaluate(() => location.hash)).toBe('#/workspace')
  })

  test('an import destination ID collision never activates or deletes the existing canvas', async ({ page }) => {
    await enablePipelineImporter(page)
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    const current = await page.evaluate(() => location.hash)
    await page.route('**/api/pipelines/import', (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        config: '{}', params: {}, inputColumns: [], outputColumns: [], stages: [], driverSteps: [],
        graph: {
          nodes: [{ id: 'must-not-apply', type: 'source', position: { x: 80, y: 80 }, data: { title: 'Must not apply', config: {} } }],
          edges: [],
        },
      }),
    }))

    let collidedId = ''
    let destinationDeletes = 0
    await page.route('**/api/canvas/*', async (route) => {
      if (route.request().method() === 'DELETE') destinationDeletes += 1
      await route.continue()
    })
    await page.route('**/api/canvas*', async (route) => {
      if (new URL(route.request().url()).pathname !== '/api/canvas' || route.request().method() !== 'POST') return route.continue()
      const destination = route.request().postDataJSON() as { id: string }
      collidedId = destination.id
      const seed = await page.request.post('/api/canvas', {
        data: { ...destination, name: 'Existing collision canvas' },
      })
      expect(seed.ok()).toBe(true)
      expect((await seed.json()).created).toBe(true)
      const response = await route.fetch() // the browser's request now receives created:false
      await route.fulfill({ response })
    })

    await page.getByTestId('app-menu').click()
    await page.getByTestId('import-pipeline').click()
    await page.getByPlaceholder(/my_table_or_uri/).fill('{"source":"x"}')
    await page.getByRole('button', { name: 'Import', exact: true }).click()

    await expect.poll(() => collidedId).not.toBe('')
    await expect(page.getByRole('heading', { name: 'Import pipeline' })).toBeVisible()
    await expect.poll(() => page.evaluate(() => location.hash)).toBe(current)
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await expect(page.getByText('Must not apply', { exact: true })).toHaveCount(0)
    expect(destinationDeletes).toBe(0)
    const retained = await page.request.get(`/api/canvas/${collidedId}`)
    expect(retained.ok()).toBe(true)
    expect((await retained.json()).name).toBe('Existing collision canvas')
    await page.request.delete(`/api/canvas/${collidedId}`)
  })

  test('Cancel during destination creation cleans up a committed remote draft and preserves the canvas', async ({ page }) => {
    await enablePipelineImporter(page)
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    const current = await page.evaluate(() => location.hash)
    await page.route('**/api/pipelines/import', (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        config: '{}', params: {}, inputColumns: [], outputColumns: [], stages: [], driverSteps: [],
        graph: {
          nodes: [{ id: 'must-not-apply', type: 'source', position: { x: 80, y: 80 }, data: { title: 'Must not apply', config: {} } }],
          edges: [],
        },
      }),
    }))

    let createdId = ''
    let deletedId = ''
    await page.route('**/api/canvas/*', async (route) => {
      if (route.request().method() !== 'DELETE') return route.continue()
      deletedId = route.request().url().split('/').pop() ?? ''
      const response = await route.fetch()
      await route.fulfill({ response })
    })
    let releaseCreateResponse!: () => void
    const createResponseHeld = new Promise<void>((resolve) => { releaseCreateResponse = resolve })
    let markCanvasCommitted!: () => void
    const canvasCommitted = new Promise<void>((resolve) => { markCanvasCommitted = resolve })
    let markCreateRouteDone!: () => void
    const createRouteDone = new Promise<void>((resolve) => { markCreateRouteDone = resolve })
    await page.route('**/api/canvas*', async (route) => {
      if (new URL(route.request().url()).pathname !== '/api/canvas' || route.request().method() !== 'POST') return route.continue()
      createdId = (route.request().postDataJSON() as { id: string }).id
      const response = await route.fetch() // commit remotely, but hold the response from the browser
      markCanvasCommitted()
      await createResponseHeld
      try { await route.fulfill({ response }) } catch { /* canceled request */ }
      markCreateRouteDone()
    })

    await page.getByTestId('app-menu').click()
    await page.getByTestId('import-pipeline').click()
    await page.getByPlaceholder(/my_table_or_uri/).fill('{"source":"x"}')
    await page.getByRole('button', { name: 'Import', exact: true }).click()
    await canvasCommitted

    await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Import pipeline' })).toBeHidden()
    releaseCreateResponse()
    await createRouteDone

    await expect.poll(() => deletedId).toBe(createdId)
    await expect.poll(async () => page.evaluate(async (id) => (await fetch(`/api/canvas/${id}`)).status, createdId)).toBe(404)
    await expect.poll(() => page.evaluate(() => location.hash)).toBe(current)
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await expect(page.getByText('Must not apply', { exact: true })).toHaveCount(0)
  })

  test('Cancel retains a recoverable remote draft when the create response is lost', async ({ page }) => {
    await enablePipelineImporter(page)
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    const current = await page.evaluate(() => location.hash)
    await page.route('**/api/pipelines/import', (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        config: '{}', params: {}, inputColumns: [], outputColumns: [], stages: [], driverSteps: [],
        graph: {
          nodes: [{ id: 'must-not-apply', type: 'source', position: { x: 80, y: 80 }, data: { title: 'Must not apply', config: {} } }],
          edges: [],
        },
      }),
    }))

    let createdId = ''
    let destinationDeletes = 0
    await page.route('**/api/canvas/*', async (route) => {
      if (route.request().method() === 'DELETE') destinationDeletes += 1
      await route.continue()
    })
    let releaseLostResponse!: () => void
    const responseHeld = new Promise<void>((resolve) => { releaseLostResponse = resolve })
    let markCanvasCommitted!: () => void
    const canvasCommitted = new Promise<void>((resolve) => { markCanvasCommitted = resolve })
    let markCreateRouteDone!: () => void
    const createRouteDone = new Promise<void>((resolve) => { markCreateRouteDone = resolve })
    await page.route('**/api/canvas*', async (route) => {
      if (new URL(route.request().url()).pathname !== '/api/canvas' || route.request().method() !== 'POST') return route.continue()
      createdId = (route.request().postDataJSON() as { id: string }).id
      await route.fetch() // the insert committed, but its success response will never reach the browser
      markCanvasCommitted()
      await responseHeld
      try { await route.abort('failed') } finally { markCreateRouteDone() }
    })

    await page.getByTestId('app-menu').click()
    await page.getByTestId('import-pipeline').click()
    await page.getByPlaceholder(/my_table_or_uri/).fill('{"source":"x"}')
    await page.getByRole('button', { name: 'Import', exact: true }).click()
    await canvasCommitted

    await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Import pipeline' })).toBeHidden()
    releaseLostResponse()
    await createRouteDone

    await expect.poll(() => destinationDeletes).toBe(0)
    const retained = await page.request.get(`/api/canvas/${createdId}`)
    expect(retained.ok()).toBe(true)
    expect((await retained.json()).nodes).toEqual([])
    await expect.poll(() => page.evaluate(() => location.hash)).toBe(current)
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await expect(page.getByText('Must not apply', { exact: true })).toHaveCount(0)
    await expect(page.getByTestId('toast').filter({ hasText: 'Imported pipeline' })).toHaveCount(0)
    await page.request.delete(`/api/canvas/${createdId}`)
  })

  test('settings modal edits and saves the agent config', async ({ page }) => {
    await goToWorkspace(page)
    await page.getByTestId('rail-settings').click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    const model = page.getByPlaceholder('anthropic/claude-opus-4-8')
    await expect(model).toBeVisible()
    await model.fill('openai/gpt-4o')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText('Saved', { exact: true })).toBeVisible()
  })

  test('settings reports effective plugin activation and placement', async ({ page }) => {
    await goToWorkspace(page)
    await page.getByTestId('rail-settings').click()
    await page.getByRole('button', { name: 'Plugins' }).click()

    const builtin = page.getByTestId('plugin-status-default-catalog')
    await expect(builtin).toContainText('active')
    await expect(builtin).toContainText('Catalog')
    await expect(builtin).toContainText('Browse its data connections in Workspace')
    await builtin.getByText('Installation details').click()
    await expect(builtin).toContainText('Features: catalog')
    await expect(builtin).toContainText('Starts with: application')
    await expect(builtin).toContainText('Required when Data Playground starts.')
  })

  test('settings keeps dirty edits across owned dismissals and warns before unload', async ({ page }) => {
    await goToWorkspace(page)
    const settingsTrigger = page.getByTestId('rail-settings')
    await settingsTrigger.click()
    const settings = page.getByTestId('settings-modal')
    const model = page.getByPlaceholder('anthropic/claude-opus-4-8')
    await expect(model).toBeVisible()
    await model.fill('unsaved-settings-model')

    expect(await page.evaluate(() => {
      const event = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(event)
      return event.defaultPrevented
    })).toBe(true)

    await page.keyboard.press('Escape')
    const confirm = page.getByTestId('settings-discard-confirmation')
    await expect(confirm).toBeVisible()
    await confirm.getByRole('button', { name: 'Keep editing' }).click()
    await expect(model).toBeFocused()
    await expect(model).toHaveValue('unsaved-settings-model')

    // Click the Dialog overlay, outside the centered Settings surface.
    await page.mouse.click(5, 300)
    await expect(confirm).toBeVisible()
    await confirm.getByRole('button', { name: 'Keep editing' }).click()
    await expect(model).toHaveValue('unsaved-settings-model')

    await settings.getByRole('button', { name: 'Close' }).click()
    await expect(confirm).toBeVisible()
    await confirm.getByRole('button', { name: 'Discard' }).click()
    await expect(settings).toHaveCount(0)
    await expect(settingsTrigger).toBeFocused()

    // A clean modal still closes immediately with no confirmation.
    await settingsTrigger.click()
    await expect(settings).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(settings).toHaveCount(0)
    await expect(confirm).toHaveCount(0)
  })

  test('settings manages destinations', async ({ page }) => {
    const listedRoot = resolve(process.cwd(), '.e2e-workspace/data/ux-fixtures')
    const emptyRoot = resolve(process.cwd(), `.e2e-workspace/destination-empty-${Date.now()}`)
    await mkdir(emptyRoot, { recursive: true })
    await goToWorkspace(page)
    await page.getByTestId('rail-settings').click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await page.getByRole('button', { name: 'Destinations' }).click()  // master-detail: switch to the Destinations pane
    await expect(page.getByLabel('Destination name')).toBeVisible()
    await expect(page.getByLabel('Destination root or prefix')).toBeVisible()
    const addDestination = async (name: string, root: string) => {
      await page.getByLabel('Destination name').fill(name)
      await page.getByLabel('Destination root or prefix').fill(root)
      await page.getByRole('button', { name: 'Add', exact: true }).click()
      await expect(page.getByText(name, { exact: true })).toBeVisible()
      await expect(page.getByText('Save to preview', { exact: true }).last()).toBeVisible()
    }
    await addDestination('fixture files', listedRoot)
    await addDestination('empty scratch', emptyRoot)

    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Preview files in fixture files' })).toBeVisible()
    await page.getByRole('button', { name: 'Preview files in fixture files' }).click()
    await expect(page.getByRole('status').filter({ hasText: '1 item · manifest.json' }))
      .toBeVisible()

    await page.getByRole('button', { name: 'Preview files in empty scratch' }).click()
    await expect(page.getByRole('status').filter({ hasText: 'No files found.' }))
      .toBeVisible()
  })

  test('settings Execution explains selectable modes without exposing local worker ids', async ({ page }) => {
    await goToWorkspace(page)
    await page.getByTestId('rail-settings').click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await page.getByRole('button', { name: 'Compute defaults' }).click()
    await expect(page.getByText('Default compute target', { exact: true })).toBeVisible()
    const automatic = page.getByRole('button', { name: 'Use Automatic execution' })
    await automatic.click()
    await expect(automatic).toHaveAttribute('aria-pressed', 'true')
    await expect(page.getByText(/streams larger data through this machine/i)).toBeVisible()
    await expect(page.getByText(/separate process.*does not interrupt the app/i)).toBeVisible()
    await expect(page.getByText(/reusable worker for each Canvas/i)).toBeVisible()
    // Implementation-specific worker ids and capacity are not researcher-facing choices.
    await expect(page.getByText('local-out-of-core:local')).toHaveCount(0)
    await expect(page.getByText(/\d+ cpu/)).toHaveCount(0)
  })

  test('settings Members creates a user', async ({ page }) => {
    await goToWorkspace(page)
    await page.getByTestId('rail-settings').click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await page.getByRole('button', { name: 'Members' }).click()
    const name = `Member ${Date.now()}`
    await page.getByPlaceholder('Name').fill(name)
    await page.getByRole('button', { name: 'Add member' }).click()
    await expect(page.getByText(name, { exact: true })).toBeVisible() // new member appears in the roster
  })

  test('a section editor uses canvas containment instead of inline nodes', async ({ page }) => {
    await fresh(page)
    const canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
    await addNode(page, 'Compute', 'section')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await page.getByText('Edit script →').click()
    await expect(page.getByText('driver script (Python)')).toBeVisible()
    await expect(page.getByText('contained nodes (on the canvas)')).toBeVisible()
    await expect(page.getByText(/Drop nodes onto the section frame/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'add node' })).toHaveCount(0)
    await expect.poll(async () => (await canvasFor(page, canvasId)).nodes.length).toBe(1)

    await page.reload()
    const section = page.locator('.react-flow__node').filter({ hasText: 'SECTION' })
    await expect(section).toBeVisible()
    await section.getByText('Edit script →').click()
    await expect(page.getByText(/Drop nodes onto the section frame/)).toBeVisible()
  })

  test('a section can declare multiple output ports (multi-output)', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Compute', 'section')
    const node = page.locator('.react-flow__node')
    await expect(node.locator('.react-flow__handle-right')).toHaveCount(1) // default: one "out" port
    await page.getByText('Edit script →').click()
    await page.getByPlaceholder('out').fill('passed, failed') // declare two named output ports
    await expect(node.locator('.react-flow__handle-right')).toHaveCount(2) // card now shows both ports
  })

  test('removing an output port prunes edges that left it (no dangling orphan)', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Compute', 'section')
    const sec = page.locator('.react-flow__node').first()
    await sec.getByText('Edit script →').click()
    await page.getByPlaceholder('out').fill('passed, failed')
    await expect(sec.locator('.react-flow__handle-right')).toHaveCount(2)
    const sectionPanel = page.getByTestId('panel-section')
    await sectionPanel.getByTitle('Close').click()
    await expect(sectionPanel).toHaveCount(0)
    // Wire a downstream filter off the SECOND port ("failed") via the shared click-from-port picker.
    await sec.locator('.react-flow__handle-right').nth(1).click()
    const finder = page.getByRole('dialog', { name: 'Connect to an operation' })
    const search = finder.getByRole('textbox', { name: 'Search operations' })
    await expect(finder).toBeVisible()
    await expect(search).toBeFocused()
    await search.fill('filter')
    await finder.getByRole('option', { name: /filter/i }).first().click()
    await expect(page.locator('.react-flow__edge')).toHaveCount(1)
    // drop "failed" — the edge that left it must be pruned, not left as an unselectable orphan
    await sec.getByText('Edit script →').click()
    await page.getByPlaceholder('out').fill('passed')
    await expect(sec.locator('.react-flow__handle-right')).toHaveCount(1)
    await expect(page.locator('.react-flow__edge')).toHaveCount(0)
  })

  test('a section renders as a container frame that invites dropping nodes in', async ({ page }) => {
    // The visual-containment UI: a section is a titled frame with a drop zone. Dragging a node onto
    // it makes it a parentId child (run by the section) — the drag interaction is exercised by hand;
    // the backend running parentId children is covered by the kernel suite.
    await fresh(page)
    await addNode(page, 'Compute', 'section')
    const section = page.locator('.react-flow__node').filter({ hasText: 'SECTION' })
    await expect(section).toBeVisible()
    await expect(section.getByText(/Drop nodes here/)).toBeVisible() // empty frame invites containment
    await expect(section.getByText('Edit script →')).toBeVisible()
  })

  test('the right inspector shows and edits the selected node', async ({ page }) => {
    await fresh(page)
    const inspector = page.getByTestId('inspector')
    await expect(inspector).toHaveCount(0)
    await addNode(page, 'Shape', 'filter') // a newly added node is auto-selected
    await expect(inspector).toBeVisible()
    await expect(inspector.getByText('FILTER')).toBeVisible()
    await expect(inspector.getByText('Properties')).toBeVisible()
    // the node's param is editable from the inspector (reused generic param editor)
    const pred = inspector.locator('label').filter({ hasText: 'predicate' }).locator('input')
    await pred.fill('amount > 0')
    await expect(pred).toHaveValue('amount > 0')
  })

  test('the Inspector uses Automatic compute and can clear a legacy Transform resource override', async ({ page }) => {
    const canvasId = `legacy-transform-compute-${Date.now()}`
    try {
      const created = await page.request.post('/api/canvas', { data: {
        id: canvasId, name: 'Legacy Transform compute', version: 1, requirements: [], edges: [], nodes: [{
          id: 'transform', type: 'transform', position: { x: 280, y: 180 }, data: {
            title: 'Legacy Transform', status: 'draft', config: {
              source: 'adhoc', code: 'def fn(row):\n    return row',
              requires: { gpu: 8, gpuType: 'a100' },
            },
          },
        }],
      } })
      expect(created.ok()).toBeTruthy()
      await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
      await page.getByText('Legacy Transform', { exact: true }).click()
      const inspector = page.getByTestId('inspector')
      await expect(inspector.getByText('Runtime requirement · Automatic', { exact: true })).toBeVisible()
      await expect(inspector.getByText('Legacy override · 8 GPUs · a100', { exact: true })).toBeVisible()
      await expect(inspector.getByText('Output columns', { exact: true })).toBeVisible()
      await expect(inspector.getByText('Run behavior', { exact: true })).toHaveCount(0)

      await inspector.getByRole('button', { name: 'Use runtime default' }).click()
      await expect(inspector.getByText('Runtime requirement · Automatic', { exact: true })).toBeVisible()
      await expect(inspector.getByText('Legacy override · 8 GPUs · a100', { exact: true })).toHaveCount(0)
      await expect(inspector.getByRole('button', { name: 'Use runtime default' })).toHaveCount(0)
      await expect.poll(async () => {
        const saved = await canvasFor(page, canvasId)
        const nodes = saved.nodes as Array<{
          id: string; data: { config: { requires?: unknown } }
        }>
        return nodes.find((node) => node.id === 'transform')?.data.config.requires
      }).toBeUndefined()
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

  test('the Inspector summarizes configured output schemas and exposes stale ones for review', async ({ page }) => {
    const configuredId = `output-schema-configured-${Date.now()}`
    const staleId = `output-schema-stale-${Date.now()}`
    try {
      for (const canvas of [{
        id: configuredId, name: 'Configured output schema',
        config: {
          code: 'return input',
          outputSchema: [{ name: 'clean_id', type: 'int', capabilities: [] }],
        },
      }, {
        id: staleId, name: 'Stale output schema',
        config: {
          code: 'return current_input',
          outputSchema: [{ name: 'clean_id', type: 'int', capabilities: [] }],
          outputSchemaCodeHash: 'outdated-contract-hash',
        },
      }]) {
        const created = await page.request.post('/api/canvas', { data: {
          id: canvas.id, name: canvas.name, version: 1, requirements: [], edges: [], nodes: [{
            id: 'transform', type: 'transform', position: { x: 160, y: 120 },
            data: { title: 'Transform', status: 'draft', config: canvas.config },
          }],
        } })
        expect(created.ok()).toBe(true)
      }

      await page.goto(`/#/canvas/${configuredId}`)
      const inspector = page.getByTestId('inspector')
      const configuredNode = page.getByTestId('rf__node-transform')
      await expect(configuredNode.getByRole('button', { name: 'return input', exact: true })).toBeVisible()
      await configuredNode.getByText('TRANSFORM', { exact: true }).click()
      await inspector.getByRole('button', { name: '1 cols' }).click()
      await expect(inspector.locator('input[value="clean_id"]')).toBeVisible()

      await page.goto(`/#/canvas/${staleId}`)
      const staleNode = page.getByTestId('rf__node-transform')
      await expect(staleNode.getByRole('button', { name: 'return current_input', exact: true })).toBeVisible()
      await staleNode.getByText('TRANSFORM', { exact: true }).click()
      await expect(inspector.getByText(/code changed after these output columns were saved/i)).toBeVisible()
      await expect(inspector.locator('input[value="clean_id"]')).toBeVisible()
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(configuredId)}`)
      await page.request.delete(`/api/canvas/${encodeURIComponent(staleId)}`)
    }
  })

  test('checkpoint controls prevent unsupported graphs and allow the linear checkpoint route', async ({ page }) => {
    const unsupportedId = `checkpoint-unsupported-${Date.now()}`
    const supportedId = `checkpoint-supported-${Date.now()}`
    try {
      for (const canvas of [{
        id: unsupportedId, name: 'Unsupported checkpoint',
        nodes: [
          { id: 'source', type: 'source', position: { x: 80, y: 80 }, data: { title: 'Source', status: 'draft', config: {} } },
          { id: 'filter', type: 'filter', position: { x: 320, y: 80 }, data: { title: 'Filter', status: 'draft', config: {} } },
          { id: 'transform', type: 'transform', position: { x: 560, y: 80 }, data: { title: 'Transform', status: 'draft', config: {} } },
        ],
        edges: [
          { id: 'source-filter', source: 'source', target: 'filter' },
          { id: 'filter-transform', source: 'filter', target: 'transform' },
        ],
      }, {
        id: supportedId, name: 'Linear checkpoint',
        nodes: [
          { id: 'source', type: 'source', position: { x: 80, y: 80 }, data: { title: 'Source', status: 'draft', config: {} } },
          { id: 'select', type: 'select', position: { x: 320, y: 80 }, data: { title: 'Select', status: 'draft', config: { select: '*' } } },
          { id: 'write', type: 'write', position: { x: 560, y: 80 }, data: { title: 'Write', status: 'draft', config: {} } },
        ],
        edges: [
          { id: 'source-select', source: 'source', sourceHandle: 'out', target: 'select', targetHandle: 'in' },
          { id: 'select-write', source: 'select', sourceHandle: 'out', target: 'write', targetHandle: 'in' },
        ],
      }]) {
        const created = await page.request.post('/api/canvas', { data: { ...canvas, version: 1, requirements: [] } })
        expect(created.ok()).toBe(true)
      }

      await page.goto(`/#/canvas/${unsupportedId}`)
      const inspector = page.getByTestId('inspector')
      await page.getByText('TRANSFORM', { exact: true }).click()
      await expect(inspector.getByText('TRANSFORM', { exact: true })).toBeVisible()
      await expect(inspector.getByText('Run behavior')).toHaveCount(0)
      await expect(inspector.getByTestId('checkpoint-toggle')).toHaveCount(0)

      await page.goto(`/#/canvas/${supportedId}`)
      await page.getByText('SELECT', { exact: true }).click()
      await expect(inspector.getByText('SELECT', { exact: true })).toBeVisible()
      const runBehavior = inspector.getByText('Run behavior')
      await runBehavior.click()
      await expect(inspector.getByTestId('checkpoint-toggle')).toBeEnabled()
      await inspector.getByTestId('checkpoint-toggle').click()
      await expect(page.locator('.react-flow__node').getByTitle(/saved for reuse/)).toBeVisible()
      await expect(inspector.getByText(/Saved result/).locator('..')).toContainText('Reused by later runs')
      await runBehavior.click()
      await inspector.getByRole('button', { name: 'Change' }).click()
      await expect(inspector.getByTestId('checkpoint-toggle')).toBeVisible()
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(unsupportedId)}`)
      await page.request.delete(`/api/canvas/${encodeURIComponent(supportedId)}`)
    }
  })

  test('a code block lives on the canvas and opens the fullscreen editor on double-click', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Inspect', 'code')
    const node = page.locator('.react-flow__node')
    await expect(node).toHaveCount(1)
    await expect(node.getByText('python', { exact: true })).toBeVisible() // language chip
    await node.dblclick()
    await expect(page.locator('.monaco-editor').first()).toBeVisible({ timeout: 15_000 })
    await page.keyboard.press('Escape')
    await expect(page.locator('.monaco-editor')).toHaveCount(0)
  })

  test('a code node opens a fullscreen editor from the inspector', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Query', 'sql') // auto-selected → inspector shows it
    await page.getByTestId('inspector').getByText('Open fullscreen editor').click()
    const editor = page.locator('.monaco-editor').first()
    await expect(editor).toBeVisible({ timeout: 15_000 })
    await expect(editor).toContainText('SELECT') // the node's default SQL, editable full-screen
    await page.keyboard.press('Escape')
    await expect(page.locator('.monaco-editor')).toHaveCount(0) // Esc closes it
  })

  test('the app menu goes to Workspace and the rail destinations remain operable', async ({ page }) => {
    await fresh(page)
    await backToWorkspace(page)
    await expect(page.getByRole('button', { name: 'Create canvas' })).toBeEnabled()
    await expect(page.getByRole('button', { name: 'Add dataset' })).toHaveCount(0)
    await expect(await workspaceResource(page, 'dataset', 'images')).toBeVisible()
    await page.getByTestId('rail-transforms').click()
    await expect(page.getByRole('heading', { name: 'Transforms' })).toBeVisible()
    await page.getByTestId('rail-workspace').click()
    await expect(page.getByRole('navigation', { name: 'Workspace path' })).toBeVisible()
  })

  test('the relationships graph preserves Dataset lineage context and widens to the catalog', async ({ page }) => {
    test.setTimeout(75_000)
    await page.setViewportSize({ width: 1280, height: 720 })
    const lineageCanvasId = `lineage-navigation-${Date.now()}`
    const lineageOutput = `lineage-navigation-${Date.now()}.parquet`
    const created = await page.request.post('/api/canvas', { data: {
      id: lineageCanvasId,
      name: 'Lineage navigation fixture',
      version: 1,
      requirements: [],
      nodes: [
        { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'events', config: { uri: 'events' } } },
        { id: 'write', type: 'write', position: { x: 420, y: 120 }, data: { title: lineageOutput, config: { filename: lineageOutput, writeMode: 'overwrite' } } },
      ],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    } })
    expect(created.ok()).toBe(true)
    await page.goto(`/#/canvas/${lineageCanvasId}`)
    const writeCard = page.locator('.react-flow__node[data-id="write"]')
    await writeCard.locator('[title="Click (when selected) or double-click to rename"]').click()
    const publication = page.getByTestId('inspector').getByLabel('Write publication')
    await page.getByTestId('inspector').getByRole('button', { name: 'Run', exact: true }).click()
    await confirmRun(page)
    await expect(publication.getByLabel('Published result')).toContainText('Published', { timeout: 30_000 })
    await backToWorkspace(page)
    await openWorkspaceDataset(page, 'events')
    await page.getByTestId('detail-relationships').click()
    // Dataset lineage owns a stable, shareable route rather than opening the global join graph.
    await expect(page.getByText('Lineage', { exact: true }).first()).toBeVisible({ timeout: 10_000 })
    await expect(page.getByTestId('er-mode-lineage')).toHaveClass(/bg-accent/)
    await expect(page).toHaveURL(/#\/relationships\?.*focus=.*&mode=lineage.*returnResource=/)
    const entities = page.locator('.react-flow__node')
    const eventField = entities.filter({ hasText: 'events' }).first().getByText('user_id')
    for (let attempt = 0; attempt < 5 && !(await eventField.isVisible()); attempt += 1) {
      const zoomIn = page.getByRole('button', { name: 'Zoom In' })
      if (await zoomIn.isDisabled()) break
      await zoomIn.click()
    }
    await expect(eventField).toBeVisible({ timeout: 10_000 })

    const focusedHash = new URL(page.url()).hash
    await page.reload()
    await expect(page).toHaveURL((url) => url.hash === focusedHash)
    await expect(page.getByTestId('er-mode-lineage')).toHaveClass(/bg-accent/)
    for (let attempt = 0; attempt < 5 && !(await eventField.isVisible()); attempt += 1) {
      const zoomIn = page.getByRole('button', { name: 'Zoom In' })
      if (await zoomIn.isDisabled()) break
      await zoomIn.click()
    }
    await expect(eventField).toBeVisible({ timeout: 10_000 })

    // Lineage is navigation, not a static poster: opening a neighbouring card lands on that
    // dataset's normal detail page, and browser Back restores the graph and its route context.
    await page.getByRole('button', { name: /^Open dataset / }).first().click()
    await expect(page.getByTestId('dataset-viewer')).toBeVisible()
    await page.goBack()
    await expect(page.getByTestId('er-mode-lineage')).toHaveClass(/bg-accent/)

    await page.getByRole('button', { name: 'Back to dataset' }).click()
    await expect(page.getByTestId('dataset-viewer')).toBeVisible()
    await expect(page.getByRole('region', { name: 'events' })).toBeVisible()

    await page.getByTestId('detail-relationships').click()
    await expect(page.getByTestId('er-mode-lineage')).toHaveClass(/bg-accent/)
    // Switch to ER before widening to the catalog: a lineage graph always retains a concrete root.
    await page.getByTestId('er-mode-joins').click()
    await page.getByTestId('er-clear-focus').click()
    const expandedTable = process.env.DP_E2E_FIXTURE_PROFILE === 'full' ? 'catalog_000' : 'images'
    await expect(entities.filter({ hasText: expandedTable }).first()).toBeVisible({ timeout: 10_000 })
    await page.request.delete(`/api/canvas/${lineageCanvasId}`)
  })

  test('relationship fitting keeps show-all entities clear of controls at compact and reference viewports', async ({ page }) => {
    test.skip(process.env.DP_E2E_FIXTURE_PROFILE === 'full', 'the full profile intentionally exceeds this four-entity geometry fixture')
    const longName = `events_with_a_deliberately_long_relationship_entity_name_${Date.now()}`
    const uploaded = await page.request.post('/api/catalog/upload', {
      data: Buffer.from('id,owner_id,metric_a,metric_b,metric_c,metric_d,metric_e,metric_f,metric_g,metric_h\n1,1,1,1,1,1,1,1,1,1\n'),
      headers: { 'X-Upload-Filename': `${longName}.csv` },
    })
    const uploadText = await uploaded.text()
    expect(uploaded.ok(), uploadText).toBe(true)
    const extra = JSON.parse(uploadText) as { id: string; registrationId: string; metadataRevision: string }
    const canvasId = `er-fit-${Date.now()}`
    const canvas = await page.request.post('/api/canvas', { data: {
      id: canvasId, name: 'ER fit geometry', version: 1, requirements: [], nodes: [], edges: [],
    } })
    expect(canvas.ok()).toBe(true)

    try {
      await page.setViewportSize({ width: 1280, height: 720 })
      await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
      await expect(page.getByTestId('toolbar')).toBeVisible()
      await backToWorkspace(page)
      await openWorkspaceDataset(page, 'events')
      await page.getByTestId('detail-relationships').click()
      await page.getByTestId('er-mode-joins').click()
      await expect(page.getByTestId('er-clear-focus')).toBeVisible({ timeout: 10_000 })
      const focusedEntity = page.locator('.react-flow__node').filter({ hasText: 'events' }).first()
      await expect.poll(async () => (await boxOf(focusedEntity)).width).toBeGreaterThanOrEqual(200)
      await page.getByTestId('er-clear-focus').click()

      const entities = page.locator('.react-flow__node')
      const expectedEntities = ['events', 'movies', 'images', longName].map((name) =>
        entities.filter({ hasText: name }).first())
      for (const entity of expectedEntities) await expect(entity).toBeVisible({ timeout: 10_000 })

      const staysInSafeViewport = async () => {
        const flow = await boxOf(page.locator('.react-flow'))
        const panel = await boxOf(page.getByTestId('er-controls-panel'))
        const controls = await boxOf(page.locator('.react-flow__controls'))
        const boxes = await Promise.all(expectedEntities.map(boxOf))
        return boxes.every((box) =>
          box.width >= 120 && box.x >= flow.x && box.y >= flow.y
          && box.x + box.width <= flow.x + flow.width
          && box.y + box.height <= flow.y + flow.height
          && !overlaps(box, panel) && !overlaps(box, controls))
      }

      // `show all` must fit its replacement node set without requiring a manual pan or fit.
      await expect.poll(staysInSafeViewport).toBe(true)
      await page.getByTestId('er-suggestions-toggle').check()
      await page.getByRole('button', { name: 'Fit view', exact: true }).click()
      await expect.poll(staysInSafeViewport).toBe(true)

      // Resize uses the same insets, rather than reverting to React Flow's pane-only fit.
      await page.setViewportSize({ width: 1440, height: 900 })
      await expect.poll(staysInSafeViewport).toBe(true)
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
      await page.request.delete(`/api/catalog/tables/${encodeURIComponent(extra.id)}`, {
        params: { expected_registration_id: extra.registrationId, expected_revision: extra.metadataRevision },
      })
    }
  })

  test('a failing run surfaces an error toast (not a silent failure)', async ({ page }) => {
    await fresh(page)
    await addWorkspaceDatasetToCurrentCanvas(page, 'events')
    const inspector = page.getByTestId('inspector')
    const failure = 'Deterministic estimate failure'
    let exactRunReadiness: unknown
    await page.route('**/api/run/estimate', async (route) => {
      const response = await route.fetch()
      expect(response.ok(), await response.text()).toBeTruthy()
      const estimate = await response.json() as {
        exactRunReadiness?: { ready: boolean; reason: string }
      }
      exactRunReadiness = estimate.exactRunReadiness
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: failure }),
      })
    })

    await inspector.getByRole('button', { name: 'Count rows' }).click()
    const errorToast = page.getByTestId('toast').filter({ hasText: failure })
    await expect(errorToast).toBeVisible({ timeout: 15_000 })
    await expect(errorToast).toHaveClass(/text-destructive/)
    expect(exactRunReadiness).toMatchObject({ ready: true, reason: 'ready' })
    // #118 error-state axe gate — colocated with the stable toast path (the duplicate a11y.spec
    // copy flaked under CI parallelism even though this test passed in the same job).
    const axe = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).disableRules(['color-contrast']).analyze()
    const gated = axe.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')
    expect(gated, gated.map((v) => `${v.id} (${v.impact})`).join('; ') || 'ok').toEqual([])
  })

  test('two clients on the same canvas see each other (realtime presence)', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByTestId('toolbar')).toBeVisible()
    // a second client in the same session opens the same (last-active) canvas → same collab room
    const b = await page.context().newPage()
    await b.goto('/')
    await expect(b.getByTestId('toolbar')).toBeVisible()
    await page.mouse.move(420, 320) // A broadcasts presence/cursor
    // B shows A as a present collaborator (avatar stack titled "… other(s) here")
    await expect(b.locator('[title*="other"]')).toBeVisible({ timeout: 12_000 })
    await b.close()
  })

  test('two clients co-edit the same canvas (Yjs CRDT)', async ({ page }) => {
    await fresh(page) // A: a fresh empty canvas, now the last-opened file
    const b = await page.context().newPage()
    await b.goto('/') // B opens the same last-opened canvas → same collab room
    await expect(b.getByTestId('toolbar')).toBeVisible()
    await expect(b.locator('.react-flow__node')).toHaveCount(0)
    // A adds a node; B sees it appear over the CRDT (no reload)
    await addNode(page, 'Shape', 'filter')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await expect(b.locator('.react-flow__node')).toHaveCount(1, { timeout: 12_000 })
    // and an edit on A propagates to B (rename via the node title)
    await b.close()
  })

  test('an MCP (HTTP) edit appears live in the open canvas — watch your agent build', async ({ page }) => {
    // The user's own Claude Code drives this workspace over the in-process /mcp endpoint; an edit it
    // makes must show up in the ALREADY-OPEN tab with no reload (the collab external-edit nudge).
    await fresh(page)
    await expect(page.getByTestId('autosave')).toHaveText(/saved/, { timeout: 8_000 }) // persisted → MCP can load it
    const cid = (await page.evaluate(() => location.hash)).replace('#/canvas/', '')
    expect(cid).toBeTruthy()
    await waitForCollabRoom(page, cid)
    // add a node purely via MCP (no browser interaction) — the request is the agent's tool call
    const res = await page.request.post('/mcp', {
      headers: { 'X-DP-User': 'local' },
      data: { jsonrpc: '2.0', id: 1, method: 'tools/call',
              params: { name: 'add_node', arguments: { canvasId: cid, kind: 'filter' } } },
    })
    expect(res.ok()).toBeTruthy()
    expect((await res.json()).result?.isError).not.toBe(true)
    // the node materializes live, and the user is told their agent changed the canvas
    await expect(page.locator('.react-flow__node')).toHaveCount(1, { timeout: 12_000 })
    await expect(page.getByText('Canvas updated by your agent')).toBeVisible({ timeout: 8_000 })
  })

  test('undo is CRDT-scoped — it never erases a peer\'s concurrent node', async ({ page }) => {
    // regression: undo used to push a stale full-doc snapshot into the CRDT, deleting any node a peer
    // added after the snapshot — for everyone. Undo must now revert only the local user's own edit.
    await fresh(page) // A
    const b = await page.context().newPage()
    await b.goto('/')
    await expect(b.getByTestId('toolbar')).toBeVisible()
    await addNode(page, 'Shape', 'filter')       // A adds a node
    await expect(b.locator('.react-flow__node')).toHaveCount(1, { timeout: 12_000 }) // B sees it
    await addNode(b, 'Shape', 'sort')            // B adds a node concurrently
    await expect(page.locator('.react-flow__node')).toHaveCount(2, { timeout: 12_000 }) // A sees both
    // A undoes ITS add — B's node must survive on BOTH clients (old bug: both dropped to 0)
    await page.locator('.react-flow__pane').click({ position: { x: 12, y: 12 } }) // focus A's canvas
    await page.keyboard.press('ControlOrMeta+z')
    await expect(page.locator('.react-flow__node')).toHaveCount(1, { timeout: 12_000 }) // A: only B's node left
    await expect(b.locator('.react-flow__node')).toHaveCount(1, { timeout: 12_000 })    // B: its node preserved
    await b.close()
  })

  test('clipboard: select-all, copy/paste and multi-duplicate the selection', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    await addNode(page, 'Shape', 'sort')
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await page.locator('.react-flow__pane').click({ position: { x: 12, y: 12 } }) // focus the canvas
    await page.keyboard.press('ControlOrMeta+a') // select all
    await page.keyboard.press('ControlOrMeta+c') // copy the selection
    await page.keyboard.press('ControlOrMeta+v') // paste → 2 more nodes (ids remapped, no collision)
    await expect(page.locator('.react-flow__node')).toHaveCount(4)
    await page.keyboard.press('ControlOrMeta+d') // duplicate the (pasted) selection → 2 more
    await expect(page.locator('.react-flow__node')).toHaveCount(6)
  })

  test('the Share dialog sets visibility and adds a collaborator', async ({ page }) => {
    // seed a collaborator via the API (there's no in-app user switching anymore) — bootstrap picks it up
    await page.request.post('/api/users', { data: { name: 'Dana' }, headers: { 'X-DP-User': 'local' } })
    await fresh(page)
    await page.getByTestId('share-btn').click()
    await expect(page.getByText('Share this canvas')).toBeVisible()
    // a read-only workspace tier is offered alongside the editable one
    await expect(page.getByRole('button', { name: 'Everyone in workspace (view-only)' })).toBeVisible()
    // flip visibility to workspace (exact — 'view-only' shares the prefix)
    await page.getByRole('button', { name: 'Everyone in workspace', exact: true }).click()
    // add Dana as a collaborator (the collaborator picker is the first combobox; a role picker sits beside it)
    const select = page.getByRole('combobox').first()
    await select.selectOption({ label: 'Dana' })
    const addBtn = page.locator('button', { hasText: 'Add' }).last()
    await expect(addBtn).toBeEnabled()
    await addBtn.click()
    await expect(page.getByText('Dana', { exact: false })).toBeVisible() // added to collaborators
    await expect(page.locator('option[value="viewer"]').first()).toBeAttached() // viewer role is assignable end-to-end
  })

  test('the Canvas menu opens version history with a restore action', async ({ page }) => {
    await fresh(page)
    // A server-created blank Canvas is already saved and must not be echoed back as a second
    // version. Make one real user edit so Version history has an honest snapshot to restore.
    await addNode(page, 'Shape', 'filter')
    const canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!.split('?')[0])
    await expect.poll(async () => (await canvasFor(page, canvasId)).nodes.length).toBe(1)
    await page.getByTestId('app-menu').click()
    await page.getByText('Version history').click()
    await expect(page.getByRole('heading', { name: 'Version history' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Restore' }).first()).toBeVisible({ timeout: 8000 }) // a snapshot to restore
  })

  test('reopening persisted transient badges settles them without an autosave loop', async ({ page }) => {
    const canvasId = `settle-transient-${Date.now()}`
    const persisted = {
      id: canvasId, name: 'Persisted transient badges', version: 1, nodes: [
        { id: 'queued', type: 'source', position: { x: 80, y: 80 }, data: {
          title: 'Persisted queued', status: 'queued', config: {}, history: [],
        } },
        { id: 'running', type: 'filter', position: { x: 400, y: 80 }, data: {
          title: 'Persisted running', status: 'running', config: {}, history: [],
        } },
      ], edges: [],
    }
    const created = await page.request.post('/api/canvas', { data: persisted })
    expect(created.ok()).toBe(true)
    // Capture this exact transient document in Version history so the post-bootstrap restore path is
    // exercised too. The in-memory settlement must not be PUT back as a new authoritative document.
    const snapshotted = await page.request.put(`/api/canvas/${canvasId}`, { data: persisted })
    expect(snapshotted.ok()).toBe(true)
    await page.goto(`/#/canvas/${canvasId}`)
    const queued = page.locator('.react-flow__node', { hasText: 'Persisted queued' })
    const running = page.locator('.react-flow__node', { hasText: 'Persisted running' })
    await expect(page.locator('.dp-running-glyph')).toHaveCount(0)
    await page.waitForTimeout(900) // isolate any bootstrap debounce before observing the restore
    const beforeRestore = await page.request.get(`/api/canvas/${canvasId}`)
    expect((await beforeRestore.json()).nodes.map((node: { data: { status: string } }) => node.data.status)).toEqual([
      'queued', 'running',
    ])

    const saves: string[] = []
    await page.route(`**/api/canvas/${canvasId}`, async (route) => {
      if (route.request().method() === 'PUT') saves.push(route.request().postData() ?? '')
      await route.continue()
    })
    try {
      await page.getByTestId('app-menu').click()
      await page.getByText('Version history').click()
      await page.getByRole('button', { name: 'Restore' }).first().click()
      await expect(queued).toBeVisible()
      await expect(running).toBeVisible()
      await expect(page.locator('.dp-running-glyph')).toHaveCount(0)
      await page.waitForTimeout(900) // longer than the local autosave debounce
      expect(saves).toEqual([])
      const stored = await page.request.get(`/api/canvas/${canvasId}`)
      expect((await stored.json()).nodes.map((node: { data: { status: string } }) => node.data.status)).toEqual([
        'queued', 'running',
      ])
    } finally {
      await page.unroute(`**/api/canvas/${canvasId}`)
      await page.request.delete(`/api/canvas/${canvasId}`)
    }
  })

  test('the Canvas menu opens persisted run history', async ({ page }) => {
    await fresh(page)
    await page.getByTestId('app-menu').click()
    await page.getByText('Run history').click()
    await expect(page.getByRole('heading', { name: 'Run history' })).toBeVisible()
    // a brand-new file has no runs yet — the empty state renders (proves the modal + API wired)
    await expect(page.getByText(/No runs yet/)).toBeVisible()
  })

  test('run history can create a Canvas without exposing the retained execution manifest @ux-smoke', async ({ page }) => {
    await fresh(page)
    const canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
    const digest = 'c'.repeat(64)
    await page.route(`**/api/canvas/${canvasId}/runs`, async (route) => {
      await route.fulfill({ json: [{
        id: 'history-manifest', runId: 'run-manifest', jobType: 'run', status: 'failed',
        targetNodeId: 'source', outputs: [],
        executionManifestSha256: digest, executionManifestSchemaVersion: 1,
        executionManifestAvailability: 'available', executionManifestReconstructable: true,
      }] })
    })
    await page.route(`**/api/canvas/${canvasId}/runs/history-manifest/manifest`, async (route) => {
      await route.fulfill({ json: {
        sha256: digest, schemaVersion: 1, availability: 'available',
        document: {
          schemaVersion: 1,
          graph: { nodes: [{ id: 'source', type: 'source', data: { config: {} } }], edges: [], requirements: [] },
          target: { nodeId: 'source', portId: null },
          admittedInputs: [{ nodeId: 'source', datasetId: 'events', revisionId: 'revision-1', provider: 'local' }],
          writeIntent: null,
          descriptors: { core: { apiVersion: '1' }, nodes: [], plugins: [] },
        },
      } })
    })
    let cloneRequest: Record<string, unknown> | null = null
    await page.route('**/api/canvas/copy/validate', async (route) => {
      cloneRequest = route.request().postDataJSON()
      await route.fulfill({ json: {
        name: 'Historical copy', nodeCount: 1, edgeCount: 0, requirements: [], parameters: [],
        diagnostics: [], canImport: true, requiresConfirmation: false,
        validationDigest: 'd'.repeat(64), copyIntentDigest: 'e'.repeat(64),
      } })
    })

    await page.getByTestId('app-menu').click()
    await page.getByText('Run history', { exact: true }).click()
    await expect(page.getByText(digest)).toHaveCount(0)
    await expect(page.getByText('Saved run setup')).toHaveCount(0)
    await expect(page.getByText('Submitted graph')).toHaveCount(0)
    await expect(page.getByText(/events@revision-1/)).toHaveCount(0)
    await page.getByRole('button', { name: 'Create Canvas from run' }).click()
    await page.getByRole('button', { name: 'Review copy' }).click()
    await expect(page.getByText('1 nodes · 0 connections · 0 requirements')).toBeVisible()
    expect(cloneRequest).toMatchObject({
      sourceCanvasId: canvasId, sourceSubjectId: 'history-manifest',
    })
  })

  test('session context lives on the Workspace shell, not the canvas chrome — and no user switching', async ({ page }) => {
    await fresh(page)
    // the canvas top-right no longer carries an account avatar (identity/logout belong on the shell)
    await expect(page.getByTitle(/Signed in as/)).toHaveCount(0)
    await backToWorkspace(page)
    await expect(page.getByRole('complementary', { name: 'Primary navigation' }).getByText('Local', { exact: true })).toBeVisible()
    await expect(page.getByText('Switch user (dev)')).toHaveCount(0) // no switcher anywhere
    await expect(page.getByPlaceholder('new user…')).toHaveCount(0)
  })

  test('a sort node needs an order-by before it can run (required-param validation)', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Shape', 'sort')
    // empty required param → the Inspector explains why it can't run
    await expect(page.getByTestId('inspector').getByText('order by is required')).toBeVisible()
    // and the structured sort builder offers to add a key (Phase-3 field, not a blind text box)
    await expect(page.locator('.react-flow__node').getByText('add sort key')).toBeVisible()
  })

  test('disabling a node marks it DISABLED (Bypass vs Disable)', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Query', 'sql') // auto-selected → its ⋯ menu is reachable
    await page.getByRole('button', { name: 'More' }).click()
    await page.locator('.dp-panel').getByText('Disable (+ downstream)').click()
    await expect(page.locator('.react-flow__node').getByText('DISABLED', { exact: true })).toBeVisible()
  })

  test('the URL reflects the open canvas + view (deep-linkable; back button works)', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect.poll(() => page.evaluate(() => location.hash)).toMatch(/#\/canvas\//) // editor URL is a canvas deep link
    const canvasHash = await page.evaluate(() => location.hash)
    // navigate to Workspace → URL updates
    await backToWorkspace(page)
    await expect.poll(() => page.evaluate(() => location.hash))
      .toBe('#/workspace')
    // browser Back returns to the canvas editor
    await page.goBack()
    await expect(page.getByTestId('toolbar')).toBeVisible()
    // a deep link opens straight into that specific canvas
    await page.goto('/' + canvasHash)
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect.poll(() => page.evaluate(() => location.hash)).toBe(canvasHash)
  })

  test('the Share dialog offers a copyable canvas link', async ({ page }) => {
    await fresh(page)
    await page.getByTestId('share-btn').click()
    await expect(page.getByTestId('copy-link')).toBeVisible()
    await expect(page.locator('input[readonly]').first()).toHaveValue(/#\/canvas\//)
  })

  test('the data viewer opens a row detail and paginates', async ({ page }) => {
    await fresh(page)
    // start a pipeline from the seeded 'events' dataset via Workspace
    await addWorkspaceDatasetToCurrentCanvas(page, 'events')
    const source = page.locator('.react-flow__node-source')
    await expect(source).toHaveCount(1) // the events source landed
    await expect(source.getByRole('button', { name: 'Change dataset' })).toHaveAttribute('title', /Click to change dataset/)
    // Source uses the same hover/selection action shelf as downstream nodes, without gaining Run.
    await source.hover()
    await expect(source.getByRole('button', { name: 'Run up to here' })).toHaveCount(0)
    await source.getByRole('button', { name: 'View data' }).click()
    // the data viewer shows rows, then Next paginates, then clicking a row opens its detail
    const panel = page.getByTestId('panel-data')
    await expect(panel.getByText(/^rows \d+–\d+$/)).toBeVisible({ timeout: 15_000 })
    await panel.getByRole('button', { name: 'Next page' }).click()
    await expect(panel.getByRole('button', { name: 'Previous page' })).toBeEnabled()
    await panel.locator('table tbody tr').first().click()
    await expect(panel.getByRole('button', { name: /^Row / })).toBeVisible() // detail back-button
  })

  test('editing a graph blocks rows from the previous preview until it is refreshed', async ({ page }) => {
    await fresh(page)
    await addWorkspaceDatasetToCurrentCanvas(page, 'events')
    await page.route('**/api/run/preview', (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        columns: [{ name: 'event', type: 'VARCHAR', capabilities: [] }],
        rows: [{ event: 'purchase' }], rowCount: 1, hasMore: false, truncated: false,
      }),
    }))

    const inspector = page.getByTestId('inspector')
    await expect(inspector.locator('[title^="Datasets · Current version"]')).toBeVisible()
    await expect(inspector.getByLabel('Dataset URI')).toHaveCount(0)
    await expect(inspector.getByLabel('CSV delimiter')).toHaveCount(0)
    await inspector.getByRole('button', { name: 'View data' }).click()
    await expect(page.getByText('purchase', { exact: true })).toBeVisible()
    const source = page.locator('.react-flow__node-source')
    await source.getByRole('button', { name: 'Change dataset' }).click()
    const picker = page.locator('.dp-panel').filter({ has: page.getByTestId('source-search') })
    await picker.getByTestId('source-search').fill('movies')
    await picker.getByRole('button', { name: /^movies\b/i }).click()
    await expect(source.getByRole('button', { name: 'Change dataset' })).toContainText('movies')

    await expect(page.getByRole('status')).toContainText('Preview out of date')
    await expect(page.getByRole('button', { name: 'Refresh preview' })).toBeVisible()
    await expect(page.getByText('purchase', { exact: true })).toHaveCount(0)
  })

  test('a write node picks an output destination via the save dialog', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Sources & sinks', 'write') // auto-selected → destination lives in the inspector
    const inspector = page.getByTestId('inspector')
    await inspector.getByRole('button', { name: 'Choose destination…' }).click()
    await expect(page.getByText('Choose output destination', { exact: true })).toBeVisible()
    const dialog = page.getByRole('dialog', { name: 'Choose output destination' })
    await expect(dialog.getByText('Dataset name', { exact: true })).toBeVisible()
    const folder = `experiment-${Date.now()}`
    await dialog.getByRole('button', { name: 'New folder' }).click()
    await dialog.getByRole('textbox', { name: 'New folder name' }).fill(folder)
    await dialog.getByRole('button', { name: 'Create', exact: true }).click()
    await expect(dialog.getByRole('button', { name: folder, exact: true })).toBeVisible()
    await dialog.locator('input').fill('my_output.parquet')
    await dialog.getByRole('button', { name: 'Save here', exact: true }).click()
    await expect(page.getByText('Choose output destination', { exact: true })).toHaveCount(0)
    const publication = inspector.getByLabel('Write publication')
    await expect(publication).toContainText('my_output.parquet')
    await expect(publication).toContainText('Workspace outputs')
  })

  test('an exact Source viewer returns to the same selected Source @ux-smoke', async ({ page }) => {
    const canvasId = `exact-source-return-${Date.now()}`
    try {
      const catalogResponse = await page.request.get('/api/catalog/tables?q=events&limit=50')
      expect(catalogResponse.ok()).toBeTruthy()
      const catalog = await catalogResponse.json() as {
        items: Array<{ id: string; registrationId?: string; name: string; uri: string }>
      }
      const table = catalog.items.find((item) => item.name === 'events')
      expect(table).toBeTruthy()
      const revisionResponse = await page.request.get(
        `/api/catalog/tables/${encodeURIComponent(table!.id)}/revisions/resolve`,
      )
      expect(revisionResponse.ok()).toBeTruthy()
      const exact = await revisionResponse.json() as { datasetId: string; revisionId: string }
      const alternateRevisionId = `${exact.revisionId}-alternate`
      const exactDetail = {
        datasetId: exact.datasetId,
        revisionId: exact.revisionId,
        committedAt: '2026-07-30T12:00:00Z',
        retentionOwner: 'provider',
        parentRevisionId: null,
        producerOperation: 'fixture',
        summary: { rowCount: 1, dataFileCount: 1, totalBytes: 8, fragmentCount: 1 },
        preview: {
          columns: [{
            fieldId: 'value', name: 'value', type: 'int64', nullable: false,
            provenance: 'provider', capabilities: [],
          }],
          rows: [{ value: 1 }],
          hasMore: false,
          rowLimit: 100,
        },
      }
      await page.route(/\/api\/catalog\/tables\/.+\/revisions\?limit=/, async (route) => {
        await route.fulfill({ json: {
          items: [
            {
              datasetId: exact.datasetId,
              revisionId: alternateRevisionId,
              committedAt: exactDetail.committedAt,
              retentionOwner: exactDetail.retentionOwner,
            },
            {
              datasetId: exact.datasetId,
              revisionId: exact.revisionId,
              committedAt: exactDetail.committedAt,
              retentionOwner: exactDetail.retentionOwner,
            },
          ],
          nextCursor: null,
          hasMore: false,
        } })
      })
      await page.route('**/api/catalog/revision-details', async (route) => {
        const requested = route.request().postDataJSON() as { datasetId: string; revisionId: string }
        if (requested.datasetId !== exact.datasetId
            || ![exact.revisionId, alternateRevisionId].includes(requested.revisionId)) {
          await route.continue()
          return
        }
        await route.fulfill({ json: {
          ...exactDetail,
          revisionId: requested.revisionId,
          parentRevisionId: requested.revisionId === alternateRevisionId ? exact.revisionId : null,
        } })
      })
      const created = await page.request.post('/api/canvas', { data: {
        id: canvasId, name: 'Exact Source return', version: 1, requirements: [], nodes: [{
          id: 'source', type: 'source', position: { x: 280, y: 180 }, data: {
            title: 'Exact events', config: {
              uri: table!.uri, tableId: table!.id,
              ...(table!.registrationId ? { registrationId: table!.registrationId } : {}),
              datasetRef: { kind: 'exact', datasetId: exact.datasetId, revisionId: exact.revisionId },
            },
          },
        }], edges: [],
      } })
      expect(created.ok()).toBeTruthy()
      // Keep the edit demonstrably inside the local autosave window. Any same-Canvas GET during the
      // viewer detour would therefore reload the server's old title and fail the assertions below.
      await page.route((url) => url.pathname === `/api/canvas/${canvasId}`, async (route) => {
        if (route.request().method() === 'PUT') {
          await route.abort('failed')
          return
        }
        await route.continue()
      })

      await page.goto(`/#/canvas/${canvasId}?node=source`)
      await page.setViewportSize({ width: 1280, height: 720 })
      const sourceCard = page.locator('.react-flow__node[data-id="source"]')
      await sourceCard.locator('[title="Click (when selected) or double-click to rename"]').dblclick()
      await sourceCard.getByRole('textbox', { name: 'Node title' }).fill('Unsaved researcher title')
      await sourceCard.getByRole('textbox', { name: 'Node title' }).press('Enter')
      await expect(sourceCard).toContainText('Unsaved researcher title')
      const serverCanvas = await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
      expect(serverCanvas.ok()).toBeTruthy()
      const serverDoc = await serverCanvas.json() as { nodes: Array<{ id: string; data: { title: string } }> }
      expect(serverDoc.nodes.find((node) => node.id === 'source')?.data.title).toBe('Exact events')
      await expect(sourceCard.getByRole('link', { name: 'Open dataset' })).toHaveCount(0)
      const openDataset = page.getByTestId('inspector').getByRole('link', { name: 'Open dataset' })
      await expect(openDataset).toBeVisible()
      await expect(openDataset).toHaveAttribute(
        'href',
        new RegExp(`returnCanvas=${encodeURIComponent(canvasId)}&returnNode=source`),
      )

      await openDataset.click()
      const viewer = page.getByTestId('dataset-viewer')
      await expect(viewer.getByLabel('Dataset preview scope')).toContainText('from this selected version')
      const viewerBack = viewer.getByRole('button', { name: 'Back to Canvas' })
      await expect(viewerBack).toBeFocused()
      await expect(page.getByTestId('catalog-search')).toHaveCount(0)
      await expect(page.getByTestId('register-dataset')).toHaveCount(0)
      await expect(page.getByRole('button', { name: `Use dataset ${table!.name}` })).toHaveCount(0)
      await page.keyboard.press('Shift+Tab')
      await expect(page.getByTestId('catalog-search')).toHaveCount(0)
      await expect(page.getByRole('button', { name: `Use dataset ${table!.name}` })).toHaveCount(0)
      const alternateRevision = viewer.getByTestId(`revision-open-${alternateRevisionId}`)
      await expect(alternateRevision).toHaveAttribute(
        'href',
        new RegExp(
          `revision=${encodeURIComponent(alternateRevisionId)}`
          + `&revisionDataset=${encodeURIComponent(exact.datasetId)}`
          + `&returnCanvas=${encodeURIComponent(canvasId)}&returnNode=source$`,
        ),
      )
      await alternateRevision.click()
      await expect(viewer.getByTestId('dataset-version-context')).toHaveText('Previous version')
      await expect(viewer.getByLabel('Dataset preview scope')).toContainText('from this selected version')
      await expect(viewer).not.toContainText(`${exact.datasetId}@${alternateRevisionId}`)
      await viewer.getByRole('button', { name: 'Back to Canvas' }).click()

      await expect(page).toHaveURL(new RegExp(`#\\/canvas\\/${encodeURIComponent(canvasId)}\\?node=source$`))
      const inspector = page.getByTestId('inspector')
      await expect(inspector).toContainText('DATASET')
      await expect(inspector).toContainText('Saved version')
      await expect(inspector).not.toContainText(exact.revisionId)
      const returnedSource = page.locator('.react-flow__node[data-id="source"]')
      await expect(returnedSource).toHaveClass(/selected/)
      await expect(returnedSource).toContainText('Unsaved researcher title')
      await page.waitForTimeout(700)
      await expect(returnedSource).toContainText('Unsaved researcher title')

      await page.goBack()
      await expect(page).toHaveURL(
        new RegExp(
          `#\\/workspace\\/dataset%3A${encodeURIComponent(exact.datasetId)}`
          + `\\?revision=${encodeURIComponent(alternateRevisionId)}`
          + `&revisionDataset=${encodeURIComponent(exact.datasetId)}`
          + `&returnCanvas=${encodeURIComponent(canvasId)}&returnNode=source$`,
        ),
      )
      await expect(page.getByTestId('dataset-viewer').getByRole('button', { name: 'Back to Canvas' })).toBeVisible()

      await page.goForward()
      await expect(page).toHaveURL(new RegExp(`#\\/canvas\\/${encodeURIComponent(canvasId)}\\?node=source$`))
      await expect(page.getByTestId('inspector')).toContainText('Saved version')
      await expect(page.getByTestId('inspector')).not.toContainText(exact.revisionId)
      await expect(page.locator('.react-flow__node[data-id="source"]')).toContainText('Unsaved researcher title')

      const staleViewerHash = `#/workspace/${encodeURIComponent(`dataset:${exact.datasetId}`)}?${new URLSearchParams({
        scope: 'datasets',
        revision: alternateRevisionId,
        revisionDataset: exact.datasetId,
        returnCanvas: canvasId,
        returnNode: 'deleted-node',
      })}`
      await page.evaluate((hash) => { window.location.hash = hash }, staleViewerHash)
      const staleViewer = page.getByTestId('dataset-viewer')
      await expect(staleViewer.getByRole('button', { name: 'Back to Canvas' })).toBeVisible()
      await staleViewer.getByRole('button', { name: 'Back to Canvas' }).click()

      await expect(page).toHaveURL(new RegExp(`#\\/canvas\\/${encodeURIComponent(canvasId)}$`))
      await expect(page.getByTestId('inspector')).toHaveCount(0)
      await expect(page.getByText('The requested node is no longer in this Canvas.')).toBeVisible()
      await expect(page.locator('.react-flow__node[data-id="source"]')).toContainText('Unsaved researcher title')
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

  test('a default-local Write keeps task-first output and an exact receipt @ux-smoke', async ({ page }) => {
    const settings = await page.request.get('/api/settings')
    const previousBackend = (await settings.json()).global?.backend ?? ''
    await page.request.put('/api/settings', { data: {
      scope: 'global', key: 'backend', value: 'local-out-of-core',
    } })
    const canvasId = `issue-688-write-${Date.now()}`
    try {
      const created = await page.request.post('/api/canvas', { data: {
        id: canvasId, name: 'Issue 688 task-first Write', version: 1, requirements: [], nodes: [
          { id: 'source', type: 'source', position: { x: 80, y: 120 }, data: { title: 'Events', config: { uri: 'events' } } },
          { id: 'write', type: 'write', position: { x: 420, y: 120 }, data: { title: 'Write', config: { filename: 'output.parquet' } } },
        ], edges: [{ id: 'source-write', source: 'source', target: 'write' }],
      } })
      expect(created.ok()).toBeTruthy()
      await page.goto(`/#/canvas/${canvasId}`)
      await page.setViewportSize({ width: 1280, height: 720 })
      const writeCard = page.locator('.react-flow__node[data-id="write"]')
      const outputInput = writeCard.locator('input[placeholder="output"]')
      const modeSelect = writeCard.locator('select')
      await expect(outputInput).toHaveValue('output.parquet')
      await expect(modeSelect.locator('option:checked')).toHaveText('Create or replace')
      const [outputBox, modeBox] = await Promise.all([outputInput.boundingBox(), modeSelect.boundingBox()])
      expect(outputBox).not.toBeNull()
      expect(modeBox).not.toBeNull()
      expect(modeBox!.y).toBeGreaterThan(outputBox!.y + outputBox!.height)
      expect(modeBox!.width).toBeGreaterThan(190)
      await writeCard.locator('[title="Click (when selected) or double-click to rename"]').click()
      const inspector = page.getByTestId('inspector')
      const filename = `demo-${Date.now().toString(36)}.parquet`
      const chooseDestination = inspector.getByRole('button', { name: 'Choose destination…' })
      await expect(chooseDestination).toBeVisible()
      await chooseDestination.click()
      const dialog = page.getByRole('dialog', { name: 'Choose output destination' })
      await dialog.locator('input').fill(filename)
      await dialog.getByRole('button', { name: 'Save here', exact: true }).click()

      const publication = inspector.getByLabel('Write publication')
      await expect(publication).toContainText(filename)
      await expect(publication).toContainText('Create a new dataset')
      await expect(publication).toContainText('Ready to run')
      await expect(publication.getByText('Diagnostics', { exact: true })).toHaveCount(0)
      await inspector.getByRole('button', { name: 'Run', exact: true }).click()
      await confirmRun(page)
      const firstReceipt = publication.getByRole('link', { name: 'Open dataset' })
      await expect(firstReceipt).toBeVisible({ timeout: 20_000 })
      await expect(publication).toContainText(/Published.*rows/)
      const publishedText = await publication.getByLabel('Published result').locator('div').first().textContent()
      const publishedName = publishedText?.match(/^Published · (.+) · [\d,]+ rows/)?.[1]
      expect(publishedName).toBeTruthy()
      const cardSummary = writeCard.getByTestId('node-meta')
      await expect(cardSummary).toHaveText(`Published · ${publishedName}`)
      expect(await cardSummary.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBeTruthy()

      const firstHref = await firstReceipt.getAttribute('href')
      expect(firstHref).toBeTruthy()
      const firstRevision = new URLSearchParams(firstHref!.split('?', 2)[1]).get('revision')
      expect(firstRevision).toBeTruthy()
      await expect(firstReceipt).toHaveAttribute('href', new RegExp(`revision=${encodeURIComponent(firstRevision!)}`))
      const summaryMode = publication.getByText('Mode', { exact: true }).locator('..')
      await expect(summaryMode).toContainText('Create a new dataset')

      await inspector.getByRole('button', { name: 'Run', exact: true }).click()
      const secondReceipt = publication.getByRole('link', { name: 'Open dataset' })
      await expect(secondReceipt).not.toHaveAttribute('href', firstHref!, { timeout: 20_000 })
      const secondHref = await secondReceipt.getAttribute('href')
      const secondRevision = new URLSearchParams(secondHref!.split('?', 2)[1]).get('revision')
      expect(secondRevision).toBeTruthy()
      expect(secondRevision).not.toBe(firstRevision)
      await expect(secondReceipt).toHaveAttribute('href', new RegExp(`revision=${encodeURIComponent(secondRevision!)}`))
      await expect(secondReceipt).toHaveAttribute('href', new RegExp(`returnCanvas=${encodeURIComponent(canvasId)}`))
      await expect(secondReceipt).toHaveAttribute('href', /returnNode=write/)
      await expect(summaryMode).toContainText('Replace the selected dataset')
      await secondReceipt.click()
      const viewer = page.getByTestId('dataset-viewer')
      await expect(viewer).toBeVisible()
      await expect(viewer.getByLabel('Dataset preview scope')).toContainText('from this selected version')
      await expect(viewer.getByRole('button', { name: 'Back to Canvas' })).toBeVisible()
      await viewer.getByRole('button', { name: 'Back to Canvas' }).click()
      await expect(page).toHaveURL(new RegExp(`#\\/canvas\\/${encodeURIComponent(canvasId)}\\?node=write$`))
      await expect(page.getByTestId('inspector').getByLabel('Write publication')).toContainText(publishedName)
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
      await page.request.put('/api/settings', { data: {
        scope: 'global', key: 'backend', value: previousBackend,
      } })
    }
  })

  test('a managed-local write retry adopts the original receipt after response loss', async ({ page }) => {
    const settings = await page.request.get('/api/settings')
    const previousBackend = (await settings.json()).global?.backend ?? ''
    await page.request.put('/api/settings', { data: {
      scope: 'global', key: 'backend', value: 'local-out-of-core',
    } })
    try {
      await fresh(page)
      await addWorkspaceDatasetToCurrentCanvas(page, 'events')
      await page.locator('.react-flow__node .react-flow__handle-right').first().click()
      const finder = page.getByRole('dialog', { name: 'Connect to an operation' })
      const search = finder.getByRole('textbox', { name: 'Search operations' })
      await expect(finder).toBeVisible()
      await expect(search).toBeFocused()
      await search.fill('write')
      await finder.getByRole('option', { name: /write/i }).first().click()
      const inspector = page.getByTestId('inspector')
      await inspector.getByRole('button', { name: 'Choose destination…' }).click()
      const dialog = page.getByRole('dialog', { name: 'Choose output destination' })
      await dialog.locator('input').fill(`issue399-recovery-${Date.now()}.parquet`)
      await dialog.getByRole('button', { name: 'Save here', exact: true }).click()
      await expect(inspector.getByLabel('Write publication')).toContainText('Create a new dataset')

      await page.route('**/api/run/estimate', async (route) => {
        const response = await route.fetch()
        const estimate = await response.json()
        await route.fulfill({ response, json: { ...estimate, needsConfirm: true } })
      })
      const submissionIds: string[] = []
      await page.route('**/api/run', async (route) => {
        const request = route.request().postDataJSON() as { submissionId: string }
        submissionIds.push(request.submissionId)
        const response = await route.fetch()
        if (submissionIds.length <= 3) {
          await route.abort('connectionfailed')
          return
        }
        await route.fulfill({ response })
      })

      await inspector.getByRole('button', { name: 'Run', exact: true }).click()
      const runPanel = page.getByTestId('panel-run')
      await expect(runPanel.getByText('CONFIRM RUN')).toBeVisible()
      await runPanel.getByRole('button', { name: 'Publish output', exact: true }).click()
      await expect(runPanel.getByText('run failed')).toBeVisible({ timeout: 15_000 })
      await runPanel.getByRole('button', { name: 'Retry', exact: true }).click()

      await expect(inspector.getByLabel('Write publication').getByRole('link', { name: 'Open dataset' })).toBeVisible({ timeout: 20_000 })
      expect(submissionIds).toHaveLength(4)
      expect(new Set(submissionIds).size).toBe(1)
    } finally {
      await page.unrouteAll({ behavior: 'wait' })
      await page.request.put('/api/settings', { data: {
        scope: 'global', key: 'backend', value: previousBackend,
      } })
    }
  })

  test('an existing local Lance destination certifies append, stale conflict, retry, and history recovery', async ({ page }) => {
    test.setTimeout(60_000)
    const settings = await page.request.get('/api/settings')
    const previousBackend = (await settings.json()).global?.backend ?? ''
    await page.request.put('/api/settings', { data: {
      scope: 'global', key: 'backend', value: 'local-out-of-core',
    } })
    try {
      await fresh(page)
      await addWorkspaceDatasetToCurrentCanvas(page, 'events')
      await page.locator('.react-flow__node .react-flow__handle-right').first().click()
      const finder = page.getByRole('dialog', { name: 'Connect to an operation' })
      const search = finder.getByRole('textbox', { name: 'Search operations' })
      await expect(finder).toBeVisible()
      await expect(search).toBeFocused()
      await search.fill('write')
      await finder.getByRole('option', { name: /write/i }).first().click()
      const inspector = page.getByTestId('inspector')
      const filename = `issue401-${Date.now()}.lance`
      await inspector.getByRole('button', { name: 'Choose destination…' }).click()
      const dialog = page.getByRole('dialog', { name: 'Choose output destination' })
      await dialog.locator('input').fill(filename)
      await dialog.getByRole('button', { name: 'Save here', exact: true }).click()

      // Lance create/replace is deliberately provider-neutral; it only prepares an existing registered
      // destination for the typed append journey below.
      const providerPublication = inspector.getByLabel('Write publication')
      await expect(providerPublication.getByText('Mode', { exact: true }).locator('..'))
        .toContainText('Replace output')
      await expect(providerPublication.getByLabel('Write readiness'))
        .toContainText('Ready to run')
      let fixtureRunId: string | undefined
      page.on('response', async (response) => {
        if (!response.url().endsWith('/api/run') || response.request().method() !== 'POST') return
        const body = await response.json().catch(() => null)
        if (body?.runId) fixtureRunId = body.runId
      })
      await inspector.getByRole('button', { name: 'Run', exact: true }).click()
      await confirmRun(page, 'ordinary')
      await expect.poll(async () => {
        if (!fixtureRunId) return 'starting'
        const response = await page.request.get(`/api/run/${fixtureRunId}`)
        const status = await response.json()
        if (status.status === 'failed') throw new Error(status.error)
        return status.status
      }, { timeout: 20_000 }).toBe('done')

      let captured: { graph: unknown; nodeId: string } | undefined
      await page.route('**/api/run/write-admission', async (route) => {
        const request = route.request().postDataJSON() as { graph: unknown; nodeId: string }
        const response = await route.fetch()
        const body = await response.json()
        if (body.provider === 'managed-local-lance' && body.intent) {
          captured = { graph: request.graph, nodeId: request.nodeId }
        }
        await route.fulfill({ response, json: body })
      })
      await page.getByRole('combobox', { name: 'write mode' }).selectOption('append')
      const appendPublication = inspector.getByLabel('Write publication')
      await expect(appendPublication.getByText('Mode', { exact: true }).locator('..'))
        .toContainText('Replace output')
      await expect(appendPublication.getByLabel('Write readiness'))
        .toContainText('Run finished. Output was written.')
      await expect(appendPublication).not.toContainText('Completed run setup')
      await expect(appendPublication).not.toContainText('Next run setup')
      await expect.poll(() => captured).toBeTruthy()

      // Hold the UI request only after it contains its frozen intent. A competing admission from the
      // same head wins, then the original request resumes with the now-stale intent.
      let injectedStaleWinner = false
      await page.route('**/api/run', async (route) => {
        if (injectedStaleWinner) {
          await route.continue()
          return
        }
        injectedStaleWinner = true
        const competingSubmission = globalThis.crypto.randomUUID()
        const competingAdmissionResponse = await page.request.post('/api/run/write-admission', { data: {
          graph: captured!.graph, nodeId: captured!.nodeId, submissionId: competingSubmission,
        } })
        expect(competingAdmissionResponse.ok()).toBeTruthy()
        const competingAdmission = await competingAdmissionResponse.json()
        const competingRunResponse = await page.request.post('/api/run', { data: {
          graph: captured!.graph, targetNodeId: captured!.nodeId, confirmed: true,
          submissionId: competingSubmission, writeIntent: competingAdmission.intent,
          confirmedWriteIntent: competingAdmission.intent,
        } })
        expect(competingRunResponse.ok()).toBeTruthy()
        const competingRun = await competingRunResponse.json()
        await expect.poll(async () => {
          const response = await page.request.get(`/api/run/${competingRun.runId}`)
          return (await response.json()).status
        }, { timeout: 20_000 }).toBe('done')
        await route.continue()
      })
      await inspector.getByRole('button', { name: 'Run', exact: true }).click()
      const staleAdmission = page.getByText(
        'Destination changed before this run started. Review the latest version and try again.',
        { exact: true },
      ).last()
      const confirmation = page.getByTestId('panel-run').getByText('CONFIRM RUN')
      await expect(confirmation.or(staleAdmission).first()).toBeVisible({ timeout: 15_000 })
      if (await confirmation.isVisible()) await confirmRun(page)
      await expect(staleAdmission).toBeVisible({ timeout: 15_000 })
      await page.unroute('**/api/run')

      // Re-admission gets the new head. Lose every automatic POST response, then retry explicitly;
      // all requests must retain one submission identity and recover the original exact receipt.
      await page.route('**/api/run/estimate', async (route) => {
        const response = await route.fetch()
        const estimate = await response.json()
        await route.fulfill({ response, json: { ...estimate, needsConfirm: true } })
      })
      const submissionIds: string[] = []
      await page.route('**/api/run', async (route) => {
        const request = route.request().postDataJSON() as { submissionId: string }
        submissionIds.push(request.submissionId)
        const response = await route.fetch()
        if (submissionIds.length <= 3) {
          await route.abort('connectionfailed')
          return
        }
        await route.fulfill({ response })
      })
      await inspector.getByRole('button', { name: 'Run', exact: true }).click()
      const runPanel = page.getByTestId('panel-run')
      await expect(runPanel.getByText('CONFIRM RUN')).toBeVisible()
      await runPanel.getByRole('button', { name: 'Publish output', exact: true }).click()
      await expect(runPanel.getByText('run failed')).toBeVisible({ timeout: 15_000 })
      await runPanel.getByRole('button', { name: 'Retry', exact: true }).click()

      const publication = inspector.getByLabel('Write publication')
      const receipt = publication.getByRole('link', { name: 'Open dataset' })
      await expect(receipt).toBeVisible({ timeout: 20_000 })
      await expect(receipt).toHaveAttribute('href', /revision=3/)
      expect(submissionIds).toHaveLength(4)
      expect(new Set(submissionIds).size).toBe(1)

      await page.reload()
      await page.getByTestId('app-menu').click()
      await page.getByText('Run history', { exact: true }).click()
      const historyOutput = page.getByText('6,000 rows written', { exact: true }).locator('..')
      await expect(historyOutput).toContainText('published')
      await expect(historyOutput).toContainText(filename.replace(/\.lance$/, ''))
      await expect(historyOutput.getByRole('button', { name: 'Open dataset' })).toBeVisible()
    } finally {
      await page.unrouteAll({ behavior: 'wait' })
      await page.request.put('/api/settings', { data: {
        scope: 'global', key: 'backend', value: previousBackend,
      } })
    }
  })

  test('the source node can browse files (open dialog)', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Sources & sinks', 'source')
    await page.locator('.react-flow__node').getByRole('button', { name: /Select dataset/ }).click()
    await page.getByText('Register accessible path / URI…').click()
    await page.getByRole('button', { name: 'Browse storage' }).click()
    await expect(page.getByText('Open a dataset')).toBeVisible() // the open dialog over destinations
    const dialog = page.getByRole('dialog', { name: 'Open a dataset' })
    await expect(dialog.getByRole('button', { name: 'Workspace outputs' }).first()).toBeVisible()
  })

  test('Add menus describe built-in operations by their research task at 1280x720 @ux-smoke', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 })
    await fresh(page)
    const expectedByCategory: Array<[string, string[]]> = [
      ['Sources & sinks', ['Choose a registered dataset', 'Save data to a file or managed dataset — scans all rows']],
      ['Shape', [
        'Take a repeatable sample of rows', 'Keep rows that match a condition', 'Choose, rename, or derive columns',
        'Sort rows by selected columns', 'Remove duplicate rows', 'Fill missing values with a chosen value or summary',
        'Expand each list item into its own row — can expand rows', 'Turn selected columns into name/value rows — can expand rows',
      ]],
      ['Compute', [
        'Apply a Python transform to rows', 'Combine two datasets by matching rows', 'Stack datasets into one table',
        'Group rows and calculate summaries — scans all rows',
        'Add rankings, running totals, or comparisons within groups',
        'Turn values into columns with summaries — scans all rows', 'Run a workflow with loops or branches',
      ]],
      ['Query', ['Query input datasets with SQL', 'Find the nearest rows to a query vector']],
      ['Inspect', ['Calculate one summary value', 'Check every row against a rule — error severity blocks downstream writes', 'Create a chart from selected columns']],
    ]

    for (const [category, blurbs] of expectedByCategory) {
      await page.getByRole('button', { name: category, exact: true }).click()
      for (const blurb of blurbs) await expect(page.getByText(blurb, { exact: true })).toBeVisible()
      await page.keyboard.press('Escape')
    }
  })

  test('a draft Source Inspector leads with data entry and keeps manual parsing advanced @ux-smoke', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 })
    await fresh(page)
    await addNode(page, 'Sources & sinks', 'source')
    const inspector = page.getByTestId('inspector')

    await expect(inspector.getByRole('button', { name: 'Select dataset' })).toBeVisible()
    await expect(inspector.getByRole('button', { name: 'Upload a file…' })).toBeVisible()
    await expect(inspector.getByRole('button', { name: 'Register or browse an accessible path…' })).toBeVisible()
    await expect(inspector.getByRole('button', { name: 'View data' })).toHaveCount(0)
    await expect(inspector.getByRole('button', { name: 'Count rows' })).toHaveCount(0)
    await expect(inspector.getByRole('button', { name: 'Delete' })).toBeVisible()
    await expect(inspector.getByLabel('Dataset URI')).toBeHidden()

    await inspector.getByText('Manual source settings', { exact: true }).click()
    const manualUri = inspector.getByLabel('Dataset URI')
    await expect(manualUri).toBeVisible()
    await expect(inspector.getByLabel('CSV delimiter')).toHaveCount(0)
    await expect(inspector.getByLabel('CSV header row')).toHaveCount(0)
    await manualUri.fill('file:///data/manual-input.csv')
    await expect(inspector.getByLabel('CSV delimiter')).toBeVisible()
    await expect(inspector.getByLabel('CSV header row')).toBeVisible()
    await manualUri.fill('')
    await expect(inspector.getByLabel('CSV delimiter')).toHaveCount(0)
    await expect(inspector.getByLabel('CSV header row')).toHaveCount(0)

    await inspector.getByRole('button', { name: 'Register or browse an accessible path…' }).click()
    const registerDialog = page.getByRole('dialog', { name: 'Register path or URL' })
    await expect(registerDialog).toBeVisible()
    await expect(registerDialog.getByRole('button', { name: 'Browse storage' })).toBeVisible()
    await expect(inspector).toBeVisible()
    await registerDialog.getByRole('button', { name: 'Cancel' }).click()
    await expect(registerDialog).toHaveCount(0)
    await expect(inspector).toBeVisible()

    await inspector.getByRole('button', { name: 'Select dataset' }).click()
    await page.getByText('events', { exact: true }).first().click()
    await expect(inspector.getByRole('button', { name: 'Count rows' })).toBeVisible()
    await expect(inspector.getByText('Related data', { exact: true })).toBeVisible()
    await expect(inspector.getByText('Ports')).toBeVisible()
  })

  test('a post-startup local input registers and retries its formal run without restart @ux-smoke', async ({ page }) => {
    const filename = `issue-956-${Date.now()}.parquet`
    const uri = resolve('.e2e-workspace/outputs', filename)
    let registration: {
      id: string
      registrationId: string
      metadataRevision: string
    } | null = null
    let canvasId = ''
    await mkdir(dirname(uri), { recursive: true })
    await copyFile(resolve('.e2e-workspace/data/images.parquet'), uri)

    try {
      await fresh(page)
      canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
      await addNode(page, 'Sources & sinks', 'source')
      const source = page.locator('.react-flow__node-source')
      const inspector = page.getByTestId('inspector')
      await inspector.getByText('Manual source settings', { exact: true }).click()
      await inspector.getByLabel('Dataset URI').fill(uri)

      const blockedEstimatePromise = page.waitForResponse((response) =>
        new URL(response.url()).pathname === '/api/run/estimate'
        && response.request().method() === 'POST')
      await inspector.getByRole('button', { name: 'Count rows' }).click()
      const blockedEstimate = await blockedEstimatePromise
      expect(blockedEstimate.ok(), await blockedEstimate.text()).toBeTruthy()
      expect(await blockedEstimate.json()).toMatchObject({
        exactRunReadiness: { ready: false, reason: 'registration_required' },
      })

      const runPanel = page.getByTestId('panel-run')
      await expect(runPanel.getByLabel('Run readiness')).toContainText('Not ready to run')
      await expect(runPanel.getByRole('button', {
        name: 'Register inputs to run',
      })).toBeDisabled()
      await runPanel.getByRole('button', { name: 'Close' }).click()

      await source.getByRole('button', { name: 'Select dataset' }).click()
      await page.getByText('Register accessible path / URI…', { exact: true }).click()
      const registerDialog = page.getByRole('dialog', { name: 'Register path or URL' })
      await expect(registerDialog).toBeVisible()
      await registerDialog.getByRole('button', { name: 'Browse storage' }).click()
      await expect(page.getByRole('dialog', { name: 'Open a dataset' })).toBeVisible()
      const registrationPromise = page.waitForResponse((response) =>
        new URL(response.url()).pathname === '/api/catalog/register'
        && response.request().method() === 'POST')
      await page.getByText(filename, { exact: true }).click()
      const registrationResponse = await registrationPromise
      expect(registrationResponse.ok(), await registrationResponse.text()).toBeTruthy()
      registration = await registrationResponse.json()

      const readyEstimatePromise = page.waitForResponse((response) =>
        new URL(response.url()).pathname === '/api/run/estimate'
        && response.request().method() === 'POST')
      const runPromise = page.waitForResponse((response) =>
        new URL(response.url()).pathname === '/api/run'
        && response.request().method() === 'POST')
      await inspector.getByRole('button', { name: 'Count rows' }).click()
      const readyEstimate = await readyEstimatePromise
      expect(readyEstimate.ok(), await readyEstimate.text()).toBeTruthy()
      expect(await readyEstimate.json()).toMatchObject({
        exactRunReadiness: { ready: true, reason: 'ready' },
      })
      const started = await runPromise
      expect(started.ok(), await started.text()).toBeTruthy()
      const { runId } = await started.json() as { runId: string }
      await expect.poll(async () => {
        const response = await page.request.get(`/api/run/${encodeURIComponent(runId)}`)
        expect(response.ok(), await response.text()).toBeTruthy()
        const status = await response.json() as { status: string; error?: string | null }
        if (status.status === 'failed') throw new Error(status.error ?? 'formal run failed')
        return status.status
      }, { timeout: 20_000 }).toBe('done')
    } finally {
      if (registration) {
        await page.request.delete(`/api/catalog/tables/${encodeURIComponent(registration.id)}`, {
          params: {
            expected_registration_id: registration.registrationId,
            expected_revision: registration.metadataRevision,
          },
        })
      }
      if (canvasId) await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
      await unlink(uri).catch(() => {})
    }
  })

  test('a Workspace dataset is added to the canvas from its preserved detail surface', async ({ page }) => {
    await fresh(page) // empty new canvas is the current doc
    await addWorkspaceDatasetToCurrentCanvas(page, 'images')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
  })

  test('catalog edits retain drafts on conflict and protect dirty dismissal', async ({ page }) => {
    const filename = `atomic-catalog-${Date.now()}.csv`
    const uploaded = await page.request.post('/api/catalog/upload', {
      headers: { 'X-Upload-Filename': filename, 'Content-Type': 'text/csv' },
      data: 'id,value\n1,alpha\n2,beta\n',
    })
    expect(uploaded.ok()).toBeTruthy()
    const created = await uploaded.json()
    const current = await page.request.get(`/api/catalog/tables/${encodeURIComponent(created.id)}`)
    expect(current.ok()).toBeTruthy()
    const original = await current.json()
    let testError: unknown
    try {
      await goToWorkspace(page)
      await openWorkspaceDataset(page, original.name)

      await page.getByTestId('detail-name').fill('my staged catalog edit')
      await page.getByTestId('detail-pk-id').click()
      const concurrent = await page.request.put(`/api/catalog/tables/${encodeURIComponent(original.id)}/edit`, {
        data: {
          expectedRevision: original.metadataRevision,
          name: original.name,
          folder: original.folder ?? '',
          tags: original.tags ?? [],
          owner: original.owner ?? null,
          description: 'saved by another editor',
          declaredKey: [],
        },
      })
      expect(concurrent.ok(), await concurrent.text()).toBeTruthy()

      await page.getByTestId('detail-save').click()
      await expect(page.getByText('Another editor saved changes first.')).toBeVisible()
      await expect(page.getByTestId('detail-name')).toHaveValue('my staged catalog edit')
      await page.getByRole('button', { name: 'Reapply', exact: true }).click()
      await expect(page.getByText('Unsaved changes')).toHaveCount(0)

      await page.getByTestId('detail-name').fill('dirty draft')
      page.once('dialog', async (dialog) => {
        expect(dialog.message()).toBe('Discard unsaved catalog edits?')
        await dialog.dismiss()
      })
      await page.keyboard.press('Escape')
      await expect(page.getByTestId('dataset-viewer')).toBeVisible()

      const saved = await page.request.get(`/api/catalog/tables/${encodeURIComponent(original.id)}`)
      expect(saved.ok()).toBeTruthy()
      const body = await saved.json()
      expect(body.name).toBe('my staged catalog edit')
      expect(body.keys.some((key: { confidence: string; columns: string[] }) =>
        key.confidence === 'declared' && key.columns.join(',') === 'id')).toBeTruthy()
    } catch (error) {
      testError = error
    } finally {
      try {
        const latest = await page.request.get(`/api/catalog/tables/${encodeURIComponent(original.id)}`)
        if (latest.ok()) {
          const table = await latest.json()
          const deleted = await page.request.delete(`/api/catalog/tables/${encodeURIComponent(original.id)}`, { params: {
            expected_registration_id: table.registrationId,
            expected_revision: table.metadataRevision,
          } })
          expect(deleted.ok(), await deleted.text()).toBeTruthy()
        }
      } catch (cleanupError) {
        if (!testError) throw cleanupError
        console.error('Catalog test cleanup failed after the primary test error:', cleanupError)
      }
    }
    if (testError) throw testError
  })
})
