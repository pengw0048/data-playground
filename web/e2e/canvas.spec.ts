import { test, expect, type Page, type Locator } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import { backToWorkspace, goToWorkspace, workspaceResource } from './support/workspace'

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

// Open a bottom-toolbar category by its aria-label and click a node kind inside the menu.
async function addNode(page: Page, category: string, kindTitle: string) {
  await page.getByRole('button', { name: category, exact: true }).click()
  const menu = page.locator('.dp-panel', { hasText: kindTitle }).last()
  await menu.getByText(kindTitle, { exact: true }).click()
}

// Start each node-touching test on a FRESH empty canvas — the metadata DB persists canvases, so
// without this a prior test's nodes would leak in and break count assertions.
async function fresh(page: Page) {
  await page.goto('/')
  await expect.poll(() => page.evaluate(() => location.hash)).toMatch(/^#\/canvas\/.+/)
  const previous = await page.evaluate(() => location.hash)
  await page.getByTestId('file-menu').click()
  await page.getByText('New file').click()
  // The previous canvas is often empty too. Waiting only for zero rendered nodes can therefore return
  // before async create + file refresh + navigation finish, and the test would mutate the old canvas.
  await expect.poll(() => page.evaluate(() => location.hash)).not.toBe(previous)
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
  await page.getByRole('button', { name: /^Add to canvas/ }).click()
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

  test('operation search is explicit about adding and keeps category browsing available', async ({ page }) => {
    await fresh(page)
    await page.getByRole('button', { name: 'Add operation', exact: true }).click()
    const finder = page.getByRole('dialog', { name: 'Add an operation' })
    const search = finder.getByRole('textbox', { name: 'Search operations' })
    await expect(search).toBeFocused()
    await search.fill('descriptor_contract')
    await expect(finder.getByRole('option', { name: /descriptor_contract/i }).first()).toContainText('Plugin · dp-descriptor-contract')
    await search.fill('filter')
    await expect(finder.getByRole('option', { name: /filter/i }).first()).toContainText('in dataset/sample · out dataset')
    await search.press('Enter')
    await expect(finder).toBeHidden()
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await page.getByRole('button', { name: 'Shape', exact: true }).click()
    await expect(page.locator('.dp-panel', { hasText: 'filter' }).last()).toBeVisible()
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
    await expect(locator.getByRole('option', { name: /duplicate-off-screen/i })).toContainText('failed · disabled')
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

    const offScreen = page.locator('.react-flow__node[data-id="off-screen"]')
    const isInCanvas = async () => {
      const node = await offScreen.boundingBox()
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
    await page.locator('.react-flow__controls-zoomin').click()
    const afterUserZoom = await viewport.getAttribute('style')
    await page.waitForTimeout(500)
    expect(await viewport.getAttribute('style')).toBe(afterUserZoom)
    await page.waitForTimeout(700) // longer than autosave debounce: route presentation never saves
    expect(saves).toEqual([])

    await page.reload()
    await expect(offScreen).toHaveClass(/selected/)
    await expect.poll(isInCanvas).toBe(true)

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
    await expect(page.getByTestId('inspector')).toContainText('2 nodes selected')

    const pane = page.locator('.react-flow__pane')
    await pane.click({ position: { x: 5, y: 5 } })
    await expect(first).not.toHaveClass(/selected/)
    await expect(second).not.toHaveClass(/selected/)
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
    await expect(page.getByTestId('inspector')).toContainText('2 nodes selected')
  })

  test('the theme toggle switches between light and dark (and flips the tokens)', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'light' })  // deterministic default (no OS 'dark' bleed-through)
    await page.goto('/')
    const html = page.locator('html')
    await expect(html).not.toHaveAttribute('data-theme', 'dark')  // light is the default
    await page.getByRole('button', { name: 'Switch to dark theme' }).click()
    await expect(html).toHaveAttribute('data-theme', 'dark')
    // the shadcn token actually flips (not just the attribute) — proves the palette is wired
    const bg = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--background').trim())
    expect(bg).toBe('222 24% 10%')
    await page.getByRole('button', { name: 'Switch to light theme' }).click()
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

  test('clicking an output port opens the node menu; sql can connect out', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Query', 'sql')
    // a plain click (no drag) on the sql output handle opens the connect-from-port menu…
    await page.locator('.react-flow__node .react-flow__handle-right').first().click()
    await expect(page.getByText('accepts dataset')).toBeVisible()
    // …and it is NOT empty — proves sql (a SQL view) can feed downstream dataset nodes
    await expect(page.locator('.dp-panel').getByText('filter', { exact: true })).toBeVisible()
    await expect(page.getByText('no compatible node')).toHaveCount(0)
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
    await expect(page.getByText('accepts dataset')).toHaveCount(0) // drag-release must not pop the picker
  })

  test('a node with no upstream source has Run disabled', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Query', 'sql')
    const run = page.getByRole('button', { name: 'Connect a source to run' })
    await expect(run).toBeVisible()
    await expect(run).toHaveAttribute('aria-disabled', 'true')
  })

  test('there is no Save button — the canvas auto-saves', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('button', { name: /^save/i })).toHaveCount(0)
    await expect(page.getByTestId('autosave')).toHaveText(/saved|saving/)
  })

  test('an online Canvas edit inside the autosave debounce survives tab reload as a local draft', async ({ page }) => {
    await fresh(page)
    const canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
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

    await page.getByTestId('file-menu').click()
    await page.getByPlaceholder('untitled').fill(name)
    await page.reload()

    await expect(page).toHaveURL(new RegExp(`#\/canvas\/${canvasId}$`))
    await expect(page.getByTestId('file-menu')).toContainText(name)
    await expect(page.getByTestId('autosave')).toHaveText(/saved locally/)
    expect(unloadPuts).toBe(0)

    await page.unroute(canvasUrl)
    await page.getByTestId('file-menu').click()
    await page.getByRole('button', { name: `Retry local draft ${name}` }).click()
    await expect(page.getByTestId('autosave')).toHaveText(/saved$/, { timeout: 8_000 })
  })

  test('minimap and zoom controls are both present and do not overlap', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Shape', 'filter') // minimap + zoom controls only mount once the canvas has a node to navigate
    const minimap = page.locator('.react-flow__minimap')
    const controls = page.locator('.react-flow__controls')
    await expect(minimap).toBeVisible()
    await expect(controls).toBeVisible()
    expect(overlaps(await boxOf(minimap), await boxOf(controls)), 'minimap overlaps zoom controls').toBe(false)
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
    await page.getByRole('button', { name: 'Rename' }).click()
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

  test('the file menu opens a fresh (empty) canvas as a new file', async ({ page }) => {
    await fresh(page) // start on a known-empty new file (shared DB persists canvases across tests)
    await addNode(page, 'Shape', 'filter')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await page.getByTestId('file-menu').click()
    await expect(page.getByText('New file')).toBeVisible()
    await page.getByText('New file').click()
    await expect(page.locator('.react-flow__node')).toHaveCount(0) // a new file is a fresh canvas
  })

  test('native Canvas upload validates and creates a separate Canvas while the optional foreign importer stays hidden', async ({ page }) => {
    await fresh(page)
    const original = await page.evaluate(() => location.hash)
    const canvasId = decodeURIComponent(original.split('/').pop()!)
    const exported = await page.request.get(`/api/canvas/${canvasId}/native-export`)
    expect(exported.ok()).toBe(true)
    const envelope = await exported.json()

    await page.getByTestId('app-menu').click()
    await expect(page.getByTestId('import-pipeline')).toHaveCount(0)
    await page.getByTestId('import-native-canvas').click()
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
    const original = await page.evaluate(() => location.hash)
    const sourceId = decodeURIComponent(original.split('/').pop()!)
    await page.getByTestId('app-menu').click()
    await page.getByTestId('copy-canvas').click()
    await page.getByLabel('New Canvas name').fill('E2E independent copy')
    await page.getByRole('button', { name: 'Review copy' }).click()
    await expect(page.getByText('0 nodes · 0 connections · 0 requirements')).toBeVisible()
    await page.getByRole('button', { name: 'Create and open' }).click()
    await expect.poll(() => page.evaluate(() => location.hash)).not.toBe(original)
    const copyId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
    const copied = await (await page.request.get(`/api/canvas/${copyId}`)).json()
    expect(copied.name).toBe('E2E independent copy')
    expect(copied._copiedFrom).toMatchObject({ kind: 'canvas', canvasId: sourceId })
    expect(copied._copiedFrom.canvasVersion).toBeGreaterThanOrEqual(1)
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

    await page.getByTestId('app-menu').click()
    await page.getByTestId('import-pipeline').click()
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
    await page.route('**/api/canvas', (route) => {
      if (route.request().method() !== 'POST') return route.continue()
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
    await page.route('**/api/canvas', async (route) => {
      if (route.request().method() === 'POST') destinationPosts += 1
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
    await page.route('**/api/canvas', async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
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
    await page.route('**/api/canvas', async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
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
    await page.route('**/api/canvas', async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
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
    await page.goto('/')
    await page.getByTestId('app-menu').click()               // Settings lives in the app menu now
    await page.getByText('Settings', { exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    const model = page.getByPlaceholder('anthropic/claude-opus-4-8')
    await expect(model).toBeVisible()
    await model.fill('openai/gpt-4o')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText('Saved', { exact: true })).toBeVisible()
  })

  test('settings reports effective plugin activation and placement', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('app-menu').click()
    await page.getByText('Settings', { exact: true }).click()
    await page.getByRole('button', { name: 'Plugins' }).click()

    const builtin = page.getByTestId('plugin-status-default-catalog')
    await expect(builtin).toContainText('active')
    await expect(builtin).toContainText('catalog')
    await expect(builtin).toContainText('required at startup')
    await expect(builtin).toContainText('Placement: application')
  })

  test('settings keeps dirty edits across owned dismissals and warns before unload', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('app-menu').click()
    await page.getByText('Settings', { exact: true }).click()
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
    await expect(page.getByTestId('app-menu')).toBeFocused()

    // A clean modal still closes immediately with no confirmation.
    await page.getByTestId('app-menu').click()
    await page.getByText('Settings', { exact: true }).click()
    await expect(settings).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(settings).toHaveCount(0)
    await expect(confirm).toHaveCount(0)
  })

  test('settings manages destinations', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('app-menu').click()               // Settings lives in the app menu now
    await page.getByText('Settings', { exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await page.getByRole('button', { name: 'Destinations' }).click()  // master-detail: switch to the Destinations pane
    // datasets live on the Tables page now, not in Settings — add an output destination (a real, consumed setting)
    await page.getByPlaceholder('e.g. S3 exports').fill('scratch')
    await page.getByPlaceholder('/path/to/dir').fill('/tmp/dp-scratch')
    await page.getByPlaceholder('/path/to/dir').press('Enter')
    await expect(page.getByText('scratch', { exact: true })).toBeVisible() // destination added to the list
  })

  test('settings Execution shows the real compute topology', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('app-menu').click()
    await page.getByText('Settings', { exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await page.getByRole('button', { name: 'Execution' }).click()
    await expect(page.getByText('Compute', { exact: true })).toBeVisible()
    // the local backend reports a worker with real host capacity (e.g. "N cpu")
    await expect(page.getByText('local-out-of-core:local')).toBeVisible()
    await expect(page.getByText(/\d+ cpu/).first()).toBeVisible()
  })

  test('settings Members creates a user', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('app-menu').click()
    await page.getByText('Settings', { exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await page.getByRole('button', { name: 'Members' }).click()
    const name = `Member ${Date.now()}`
    await page.getByPlaceholder('Name').fill(name)
    await page.getByRole('button', { name: 'Add member' }).click()
    await expect(page.getByText(name, { exact: true })).toBeVisible() // new member appears in the roster
  })

  test('a section editor uses canvas containment instead of inline nodes', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Compute', 'section')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await page.getByText('Edit script →').click()
    await expect(page.getByText('driver script (Python)')).toBeVisible()
    await expect(page.getByText('contained nodes (on the canvas)')).toBeVisible()
    await expect(page.getByText(/Drop nodes onto the section frame/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'add node' })).toHaveCount(0)
    await expect(page.getByTestId('autosave')).toHaveText(/saved/, { timeout: 8_000 })

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
    await sec.getByText('Edit script →').click() // close the panel so it doesn't cover the output handles
    // wire a downstream filter off the SECOND port ("failed") via the click-from-port add menu
    await sec.locator('.react-flow__handle-right').nth(1).click()
    await page.locator('.dp-panel').getByText('filter', { exact: true }).click()
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
    await expect(inspector).toBeVisible()
    await expect(inspector.getByText(/Select a node/)).toBeVisible() // empty state
    await addNode(page, 'Shape', 'filter') // a newly added node is auto-selected
    await expect(inspector.getByText('FILTER')).toBeVisible()
    await expect(inspector.getByText('Properties')).toBeVisible()
    // the node's param is editable from the inspector (reused generic param editor)
    const pred = inspector.locator('label').filter({ hasText: 'predicate' }).locator('input')
    await pred.fill('amount > 0')
    await expect(pred).toHaveValue('amount > 0')
  })

  test('the inspector edits a step resource requirement (placement)', async ({ page }) => {
    await fresh(page)
    const inspector = page.getByTestId('inspector')
    await addNode(page, 'Compute', 'transform') // auto-selected; transform can declare compute needs
    await expect(inspector.getByText('Resources (placement)')).toBeVisible()
    const gpus = inspector.locator('label').filter({ hasText: 'GPUs' }).locator('input')
    await gpus.fill('8')
    await expect(gpus).toHaveValue('8') // written to config.requires → routes to a GPU worker at run time
  })

  test('checkpointing a node marks it materialized on the graph', async ({ page }) => {
    await fresh(page)
    const inspector = page.getByTestId('inspector')
    await addNode(page, 'Shape', 'filter') // auto-selected
    await inspector.getByTestId('checkpoint-toggle').click()
    // the card now shows the materialized ● (output persisted → inspectable + reused across runs)
    await expect(page.locator('.react-flow__node').getByTitle(/Checkpointed/)).toBeVisible()
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
    await expect(page.getByRole('button', { name: 'New canvas here' })).toBeEnabled()
    await expect(page.getByRole('button', { name: 'Add dataset' })).toHaveCount(0)
    await expect(await workspaceResource(page, 'dataset', 'images')).toBeVisible()
    await page.getByTestId('rail-transforms').click()
    await expect(page.getByRole('heading', { name: 'Transforms' })).toBeVisible()
    await page.getByTestId('rail-workspace').click()
    await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible()
  })

  test('the relationships graph opens focused from a table and widens to the catalog', async ({ page }) => {
    await fresh(page)
    await backToWorkspace(page)
    await openWorkspaceDataset(page, 'events')
    await page.getByTestId('detail-relationships').click()
    // the graph mounts focused on that table (its columns are visible on the entity)
    await expect(page.getByText('Relationships', { exact: true })).toBeVisible({ timeout: 10_000 })
    const entities = page.locator('.react-flow__node')
    await expect(entities.filter({ hasText: 'events' }).first().getByText('user_id')).toBeVisible({ timeout: 10_000 })
    // "show all" widens to the whole catalog. Assert against the selected fixture profile so the
    // normal PR smoke keeps its starter-data contract while the full matrix proves the large catalog.
    await page.getByTestId('er-clear-focus').click()
    const expandedTable = process.env.DP_E2E_FIXTURE_PROFILE === 'full' ? 'catalog_000' : 'images'
    await expect(entities.filter({ hasText: expandedTable }).first()).toBeVisible({ timeout: 10_000 })
  })

  test('a failing run surfaces an error toast (not a silent failure)', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Sources & sinks', 'source') // auto-selected → editable in the inspector
    const inspector = page.getByTestId('inspector')
    // point the source at a dataset that doesn't exist → the run fails and must surface a toast
    await inspector.locator('label').filter({ hasText: 'uri' }).locator('input').fill('does-not-exist.parquet')
    // a source's run is a full count/scan — the Inspector labels it "Count rows"
    await inspector.getByRole('button', { name: 'Count rows' }).click()
    await expect(page.getByTestId('toast')).toBeVisible({ timeout: 15_000 })
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

  test('the app menu opens canvas version history with a restore action', async ({ page }) => {
    await fresh(page)               // creating the file autosaves it → a first snapshot is captured
    await page.waitForTimeout(700)  // let the autosave (~400ms) persist server-side
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
    await expect(queued.locator('[title="stale"]')).toBeVisible()
    await expect(running.locator('[title="stale"]')).toBeVisible()
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
      await expect(queued.locator('[title="stale"]')).toBeVisible()
      await expect(running.locator('[title="stale"]')).toBeVisible()
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

  test('the app menu opens persisted run history', async ({ page }) => {
    await fresh(page)
    await page.getByTestId('app-menu').click()
    await page.getByText('Run history').click()
    await expect(page.getByRole('heading', { name: 'Run history' })).toBeVisible()
    // a brand-new file has no runs yet — the empty state renders (proves the modal + API wired)
    await expect(page.getByText(/No runs yet/)).toBeVisible()
  })

  test('run history lazily inspects the retained execution manifest @ux-smoke', async ({ page }) => {
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
    await page.getByRole('button', { name: /Execution manifest/ }).click()

    await expect(page.getByText('Submitted graph')).toBeVisible()
    await expect(page.getByText(/events@revision-1/)).toBeVisible()
    await expect(page.getByText('No declared parameter bindings were recorded.')).toBeVisible()
    await page.getByRole('button', { name: 'Clone as new Canvas…' }).click()
    await page.getByRole('button', { name: 'Review copy' }).click()
    await expect(page.getByText('1 nodes · 0 connections · 0 requirements')).toBeVisible()
    expect(cloneRequest).toMatchObject({
      sourceCanvasId: canvasId, sourceSubjectId: 'history-manifest',
    })
  })

  test('identity lives on the Workspace shell, not the canvas chrome — and no user switching', async ({ page }) => {
    await page.goto('/')
    // the canvas top-right no longer carries an account avatar (identity/logout belong on the shell)
    await expect(page.getByTitle(/Signed in as/)).toHaveCount(0)
    await backToWorkspace(page)
    await expect(page.getByText('signed in')).toBeVisible() // identity indicator on the Workspace shell
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
    await expect.poll(() => page.evaluate(() => location.hash)).toBe('#/workspace')
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
    await expect(page.locator('.react-flow__node')).toHaveCount(1) // the events source landed
    // preview via the Inspector's View data (always visible for the selected node — no hover needed)
    await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
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
    await inspector.getByRole('button', { name: 'View data' }).click()
    await expect(page.getByText('purchase', { exact: true })).toBeVisible()
    await inspector.locator('label').filter({ hasText: 'uri' }).locator('input').fill('another-events.parquet')

    await expect(page.getByRole('status')).toContainText('Preview out of date')
    await expect(page.getByRole('button', { name: 'Refresh preview' })).toBeVisible()
    await expect(page.getByText('purchase', { exact: true })).toHaveCount(0)
  })

  test('a write node picks an output destination via the save dialog', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Sources & sinks', 'write') // auto-selected → destination lives in the inspector
    const inspector = page.getByTestId('inspector')
    await inspector.getByRole('button', { name: /Change destination/ }).click()
    await expect(page.getByText('Save output', { exact: true })).toBeVisible()
    const dialog = page.locator('.dp-modal-overlay')
    await expect(dialog.getByRole('button', { name: 'Workspace outputs' }).first()).toBeVisible() // default place in the sidebar
    await dialog.locator('input').fill('my_output.parquet')
    await dialog.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText('Save output', { exact: true })).toHaveCount(0) // dialog closed on save
    await expect(inspector.getByText('Workspace outputs')).toBeVisible() // target shown in the inspector
  })

  test('a default-local write certifies create then replace and exposes exact receipts', async ({ page }) => {
    const settings = await page.request.get('/api/settings')
    const previousBackend = (await settings.json()).global?.backend ?? ''
    await page.request.put('/api/settings', { data: {
      scope: 'global', key: 'backend', value: 'local-out-of-core',
    } })
    try {
      await fresh(page)
      await addWorkspaceDatasetToCurrentCanvas(page, 'events')
      await page.locator('.react-flow__node .react-flow__handle-right').first().click()
      await page.locator('.dp-panel').getByText('write', { exact: true }).click()
      const inspector = page.getByTestId('inspector')
      const filename = `issue399-${Date.now()}.parquet`
      await inspector.getByRole('button', { name: /Change destination/ }).click()
      const dialog = page.locator('.dp-modal-overlay')
      await dialog.locator('input').fill(filename)
      await dialog.getByRole('button', { name: 'Save', exact: true }).click()

      const admission = inspector.getByLabel('Write admission')
      await expect(admission).toContainText('create · managed-local-file')
      await expect(admission).toContainText(/schema field/)
      await expect(admission).toContainText('unpartitioned')
      await inspector.getByRole('button', { name: 'Run', exact: true }).click()
      const firstReceipt = inspector.getByLabel('Write receipt')
      await expect(firstReceipt).toContainText('durable revision', { timeout: 20_000 })
      const firstRevision = (await firstReceipt.textContent())?.match(/durable revision\s+(\S+)/)?.[1]
      expect(firstRevision).toBeTruthy()

      await inspector.getByRole('button', { name: 'Run', exact: true }).click()
      await expect.poll(async () => {
        const text = await inspector.getByLabel('Write receipt').textContent()
        return text?.match(/durable revision\s+(\S+)/)?.[1]
      }, { timeout: 20_000 }).not.toBe(firstRevision)
      await expect(inspector.getByLabel('Write receipt')).toContainText(/dataset .* rows .* bytes/)
    } finally {
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
      await page.locator('.dp-panel').getByText('write', { exact: true }).click()
      const inspector = page.getByTestId('inspector')
      await inspector.getByRole('button', { name: /Change destination/ }).click()
      const dialog = page.locator('.dp-modal-overlay')
      await dialog.locator('input').fill(`issue399-recovery-${Date.now()}.parquet`)
      await dialog.getByRole('button', { name: 'Save', exact: true }).click()
      await expect(inspector.getByLabel('Write admission')).toContainText('create · managed-local-file')

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
      await expect(runPanel.getByText('HEADS UP')).toBeVisible()
      await runPanel.getByRole('button', { name: 'Run', exact: true }).click()
      await expect(runPanel.getByText('run failed')).toBeVisible({ timeout: 15_000 })
      await runPanel.getByRole('button', { name: 'Retry', exact: true }).click()

      await expect(inspector.getByLabel('Write receipt')).toContainText(
        'durable revision', { timeout: 20_000 },
      )
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
      await page.locator('.dp-panel').getByText('write', { exact: true }).click()
      const inspector = page.getByTestId('inspector')
      const filename = `issue401-${Date.now()}.lance`
      await inspector.getByRole('button', { name: /Change destination/ }).click()
      const dialog = page.locator('.dp-modal-overlay')
      await dialog.locator('input').fill(filename)
      await dialog.getByRole('button', { name: 'Save', exact: true }).click()

      // Lance create/replace is deliberately provider-neutral; it only prepares an existing registered
      // destination for the typed append journey below.
      await expect(inspector.getByLabel('Write admission')).toContainText('overwrite · provider-neutral')
      let fixtureRunId: string | undefined
      page.on('response', async (response) => {
        if (!response.url().endsWith('/api/run') || response.request().method() !== 'POST') return
        const body = await response.json().catch(() => null)
        if (body?.runId) fixtureRunId = body.runId
      })
      await inspector.getByRole('button', { name: 'Run', exact: true }).click()
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
      await page.getByRole('combobox', { name: 'mode' }).selectOption('append')
      const appendAdmission = inspector.getByLabel('Write admission')
      await expect(appendAdmission).toContainText('append · managed-local-lance')
      await expect(appendAdmission).toContainText('expected revision 1')
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
      await expect(page.getByText(/write admission is stale/i).last()).toBeVisible({ timeout: 15_000 })
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
      await expect(runPanel.getByText('HEADS UP')).toBeVisible()
      await runPanel.getByRole('button', { name: 'Run', exact: true }).click()
      await expect(runPanel.getByText('run failed')).toBeVisible({ timeout: 15_000 })
      await runPanel.getByRole('button', { name: 'Retry', exact: true }).click()

      const receipt = inspector.getByLabel('Write receipt')
      await expect(receipt).toContainText('durable revision 3', { timeout: 20_000 })
      await expect(receipt).toContainText('parent revision 2')
      await expect(receipt).toContainText(/backend \d/)
      expect(submissionIds).toHaveLength(4)
      expect(new Set(submissionIds).size).toBe(1)

      await page.reload()
      await page.getByTestId('app-menu').click()
      await page.getByText('Run history', { exact: true }).click()
      const historyReceipt = page.getByLabel(/Write receipt for run/).first()
      await expect(historyReceipt).toContainText('durable revision 3')
      await expect(historyReceipt).toContainText('parent 2')
      await expect(historyReceipt).toContainText(/backend \d/)
      await expect(historyReceipt).not.toContainText('/outputs/')
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
    await page.getByText('Browse files…').click()
    await expect(page.getByText('Open a dataset')).toBeVisible() // the open dialog over destinations
    await expect(page.locator('.dp-modal-overlay').getByRole('button', { name: 'Workspace outputs' }).first()).toBeVisible()
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
      await expect(page.getByRole('dialog')).toBeVisible()

      const saved = await page.request.get(`/api/catalog/tables/${encodeURIComponent(original.id)}`)
      expect(saved.ok()).toBeTruthy()
      const body = await saved.json()
      expect(body.name).toBe('my staged catalog edit')
      expect(body.keys.some((key: { confidence: string; columns: string[] }) =>
        key.confidence === 'declared' && key.columns.join(',') === 'id')).toBeTruthy()
    } finally {
      const latest = await page.request.get(`/api/catalog/tables/${encodeURIComponent(original.id)}`)
      if (latest.ok()) {
        const table = await latest.json()
        await page.request.delete(`/api/catalog/tables/${encodeURIComponent(original.id)}`, { params: {
          expected_registration_id: table.registrationId,
          expected_revision: table.metadataRevision,
        } })
      }
    }
  })
})
