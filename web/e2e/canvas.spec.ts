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
})
