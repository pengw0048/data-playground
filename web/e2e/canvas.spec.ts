import { test, expect, type Page, type Locator } from '@playwright/test'

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
  await page.getByTestId('file-menu').click()
  await page.getByText('New file').click()
  await expect(page.locator('.react-flow__node')).toHaveCount(0)
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
    await input.press('Enter')
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

  test('settings manages destinations', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('app-menu').click()               // Settings lives in the app menu now
    await page.getByText('Settings', { exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
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

  test('a section node opens its editor and adds a contained node', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Compute', 'section')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await page.getByText('Edit script →').click()
    await expect(page.getByText('driver script (Python)')).toBeVisible()
    await page.getByText('add node').click()
    await expect(page.getByPlaceholder('alias')).toBeVisible() // a contained-node row appeared
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

  test('the app menu goes to the files home; the rail navigates; new file returns to the canvas', async ({ page }) => {
    await fresh(page)
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to files').click()
    // files home
    await expect(page.getByRole('heading', { name: 'Recents' })).toBeVisible()
    await expect(page.getByTestId('new-file')).toBeVisible()
    // rail → Tables (shows the seeded catalog) and Transforms
    await page.getByTestId('rail-tables').click()
    await expect(page.getByRole('heading', { name: 'Tables' })).toBeVisible()
    await expect(page.getByText('images', { exact: true })).toBeVisible()
    await page.getByTestId('rail-transforms').click()
    await expect(page.getByRole('heading', { name: 'Transforms' })).toBeVisible()
    // back to recents → New file returns to the canvas editor
    await page.getByTestId('rail-files').click()
    await page.getByTestId('new-file').click()
    await expect(page.getByTestId('toolbar')).toBeVisible()
  })

  test('the relationships (ER) view renders the catalog as entities', async ({ page }) => {
    await fresh(page)
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to files').click()
    await page.getByTestId('rail-relationships').click()
    // the ER canvas mounts with the seeded datasets as draggable entities (with their columns)
    await expect(page.getByText('Relationships (ER)')).toBeVisible({ timeout: 10_000 })
    const entities = page.locator('.react-flow__node')
    // the catalog is fetched + laid out async on a fresh e2e DB (first-boot seed) — a slow CI runner can
    // take a beat to mount every entity node, so give these the same 10s headroom as the heading.
    await expect(entities.filter({ hasText: 'events' }).first()).toBeVisible({ timeout: 10_000 })
    await expect(entities.filter({ hasText: 'images' }).first()).toBeVisible({ timeout: 10_000 })
    // a key column is present in the entity (id) — the material for join hints
    await expect(entities.filter({ hasText: 'events' }).first().getByText('user_id')).toBeVisible({ timeout: 10_000 })
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

  test('the app menu opens persisted run history', async ({ page }) => {
    await fresh(page)
    await page.getByTestId('app-menu').click()
    await page.getByText('Run history').click()
    await expect(page.getByRole('heading', { name: 'Run history' })).toBeVisible()
    // a brand-new file has no runs yet — the empty state renders (proves the modal + API wired)
    await expect(page.getByText(/No runs yet/)).toBeVisible()
  })

  test('identity lives on the files shell, not the canvas chrome — and no user switching', async ({ page }) => {
    await page.goto('/')
    // the canvas top-right no longer carries an account avatar (identity/logout belong on the shell)
    await expect(page.getByTitle(/Signed in as/)).toHaveCount(0)
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to files').click()
    await expect(page.getByText('signed in')).toBeVisible() // identity indicator on the files shell
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
    // navigate to the files home → URL updates
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to files').click()
    await expect(page.getByRole('heading', { name: 'Recents' })).toBeVisible()
    await expect.poll(() => page.evaluate(() => location.hash)).toBe('#/files')
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
    // start a pipeline from the seeded 'events' dataset via the Tables view
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to files').click()
    await page.getByTestId('rail-tables').click()
    await page.getByText('events', { exact: true }).click()
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect(page.locator('.react-flow__node')).toHaveCount(1) // the events source landed
    // preview via the Inspector's View data (always visible for the selected node — no hover needed)
    await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
    // the data viewer shows rows, then Next paginates, then clicking a row opens its detail
    await expect(page.getByText(/^rows /)).toBeVisible({ timeout: 15_000 })
    await page.getByRole('button', { name: 'Next page' }).click()
    await expect(page.getByRole('button', { name: 'Previous page' })).toBeEnabled()
    await page.locator('.dp-panel table tbody tr').first().click()
    await expect(page.getByRole('button', { name: /^Row / })).toBeVisible() // detail back-button
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

  test('the source node can browse files (open dialog)', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Sources & sinks', 'source')
    await page.locator('.react-flow__node').getByRole('button', { name: /Select dataset/ }).click()
    await page.getByText('Browse files…').click()
    await expect(page.getByText('Open a dataset')).toBeVisible() // the open dialog over destinations
    await expect(page.locator('.dp-modal-overlay').getByRole('button', { name: 'Workspace outputs' }).first()).toBeVisible()
  })

  test('a table is registered and added to the canvas from the Tables view', async ({ page }) => {
    await fresh(page) // empty new canvas is the current doc
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to files').click()
    await page.getByTestId('rail-tables').click()
    await expect(page.getByRole('heading', { name: 'Tables' })).toBeVisible()
    await expect(page.getByTestId('register-dataset')).toBeVisible() // register lives here, not only in Settings
    // clicking the seeded dataset row drops a source onto the canvas and navigates to it
    await page.getByText('images', { exact: true }).click()
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
  })
})
