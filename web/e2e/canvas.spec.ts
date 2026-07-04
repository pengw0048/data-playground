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
    const first = await boxOf(menu)
    await page.waitForTimeout(350) // if it re-positioned on a later tick, this would catch the shift
    const second = await boxOf(menu)
    expect(Math.abs(first.x - second.x)).toBeLessThan(2)
    expect(Math.abs(first.y - second.y)).toBeLessThan(2)
    // grows upward: the menu sits entirely above the toolbar
    const tb = await boxOf(toolbar)
    expect(second.y + second.height).toBeLessThanOrEqual(tb.y + 2)
  })

  test('added nodes do not overlap each other', async ({ page }) => {
    await page.goto('/')
    await addNode(page, 'Shape', 'filter')
    await addNode(page, 'Shape', 'filter')
    const nodes = page.locator('.react-flow__node')
    await expect(nodes).toHaveCount(2)
    const a = await boxOf(nodes.nth(0))
    const b = await boxOf(nodes.nth(1))
    expect(overlaps(a, b), 'two freshly added nodes overlap').toBe(false)
  })

  test('duplicating a node does not stack it on the original', async ({ page }) => {
    await page.goto('/')
    await addNode(page, 'Query', 'sql')
    const nodes = page.locator('.react-flow__node')
    await expect(nodes).toHaveCount(1)
    await page.getByRole('button', { name: 'More' }).click()
    await page.getByRole('button', { name: 'Duplicate' }).click()
    await expect(nodes).toHaveCount(2)
    expect(overlaps(await boxOf(nodes.nth(0)), await boxOf(nodes.nth(1))), 'duplicated node overlaps the original').toBe(false)
  })

  test('action tooltips escape the card (not clipped by overflow:hidden)', async ({ page }) => {
    await page.goto('/')
    await addNode(page, 'Query', 'sql')
    await page.getByRole('button', { name: 'Connect a source to preview' }).hover()
    const tip = page.getByText('Connect a source to preview', { exact: true })
    await expect(tip).toBeVisible()
    // the fix: the tooltip is portaled to <body>, not rendered inside the (clipping) node card
    const insideCard = await tip.evaluate((el) => !!el.closest('.react-flow__node'))
    expect(insideCard, 'tooltip is still inside the node card and gets clipped').toBe(false)
  })

  test('clicking an output port opens the node menu; sql can connect out', async ({ page }) => {
    await page.goto('/')
    await addNode(page, 'Query', 'sql')
    // a plain click (no drag) on the sql output handle opens the connect-from-port menu…
    await page.locator('.react-flow__node .react-flow__handle-right').first().click()
    await expect(page.getByText('accepts dataset')).toBeVisible()
    // …and it is NOT empty — proves sql (a SQL view) can feed downstream dataset nodes
    await expect(page.locator('.dp-panel').getByText('filter', { exact: true })).toBeVisible()
    await expect(page.getByText('no compatible node')).toHaveCount(0)
  })

  test('dragging from an output port and releasing shows no menu', async ({ page }) => {
    await page.goto('/')
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
    await page.goto('/')
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
    await page.goto('/')
    const minimap = page.locator('.react-flow__minimap')
    const controls = page.locator('.react-flow__controls')
    await expect(minimap).toBeVisible()
    await expect(controls).toBeVisible()
    expect(overlaps(await boxOf(minimap), await boxOf(controls)), 'minimap overlaps zoom controls').toBe(false)
  })

  test('agent dock shows its mode and builds real nodes (offline planner in CI)', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Agent', exact: true }).click()
    // no provider key configured in CI → the dock advertises the offline planner
    await expect(page.getByText('offline planner')).toBeVisible()
    await page.getByPlaceholder('Describe an outcome…').fill('sample images then write a table')
    await page.getByTestId('agent-submit').click() // Build (mode is Build by default)
    // offline planner materializes real, inspectable nodes on the canvas
    await expect(page.locator('.react-flow__node').first()).toBeVisible({ timeout: 12_000 })
    expect(await page.locator('.react-flow__node').count()).toBeGreaterThan(0)
  })

  test('the top bar has Rerun all, not Export', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('button', { name: /rerun all/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /^export$/i })).toHaveCount(0)
  })

  test('a markdown note node renders markdown on the canvas', async ({ page }) => {
    await page.goto('/')
    await addNode(page, 'Inspect', 'note')
    const node = page.locator('.react-flow__node')
    await expect(node).toHaveCount(1)
    // default content renders a "Note" heading (react-markdown), and double-click edits it
    await expect(node.getByText('Note', { exact: true })).toBeVisible()
    await node.dblclick()
    await expect(node.locator('textarea')).toBeVisible()
  })

  test('a node can be renamed (⋯ menu → Rename)', async ({ page }) => {
    await page.goto('/')
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
    await page.goto('/')
    await addNode(page, 'Query', 'sql')
    await page.getByRole('button', { name: 'Code' }).click()
    const editor = page.locator('.monaco-editor').first()
    await expect(editor).toBeVisible({ timeout: 15_000 }) // Monaco lazy-loads + its worker boots
    await expect(editor).toContainText('SELECT')
  })

  test('the file menu opens a fresh (empty) canvas as a new file', async ({ page }) => {
    await page.goto('/')
    await addNode(page, 'Shape', 'filter')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    await page.getByRole('button', { name: /untitled/ }).click()
    await expect(page.getByText('New file')).toBeVisible()
    await page.getByText('New file').click()
    await expect(page.locator('.react-flow__node')).toHaveCount(0) // a new file is a fresh canvas
  })

  test('the user switcher creates and switches users', async ({ page }) => {
    await page.goto('/')
    const chip = page.getByTitle('Switch user')
    await expect(chip).toContainText('local') // default seeded user
    await chip.click()
    await page.getByPlaceholder('new user…').fill('Alice')
    await page.getByRole('button', { name: 'Add', exact: true }).click()
    await expect(page.getByTitle('Switch user')).toContainText('Alice') // now acting as Alice
  })
})
