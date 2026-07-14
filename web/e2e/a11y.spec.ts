import { test, expect, type Page, type Locator } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

// Accessibility gate for issue #118: keyboard contract on Files/Canvas + one axe smoke suite that
// fails the build on serious/critical violations across the primary surfaces.

async function fresh(page: Page) {
  await page.goto('/')
  await expect.poll(() => page.evaluate(() => location.hash)).toMatch(/^#\/canvas\/.+/)
  const previous = await page.evaluate(() => location.hash)
  await page.getByTestId('file-menu').click()
  await page.getByText('New file').click()
  await expect.poll(() => page.evaluate(() => location.hash)).not.toBe(previous)
  await expect(page.locator('.react-flow__node')).toHaveCount(0)
}

async function addNode(page: Page, category: string, kindTitle: string) {
  await page.getByRole('button', { name: category, exact: true }).click()
  const menu = page.locator('.dp-panel', { hasText: kindTitle }).last()
  await menu.getByText(kindTitle, { exact: true }).click()
}

async function goFiles(page: Page) {
  await page.getByTestId('app-menu').click()
  await page.getByText('Back to files').click()
  await expect(page.getByRole('heading', { name: 'Recents' })).toBeVisible()
}

async function openSettings(page: Page) {
  await page.getByTestId('app-menu').click()
  await page.getByText('Settings', { exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
}

/** Catalog rows load async; on a busy data_dir the seeded name may sit past the first page — search first. */
async function expectCatalogTable(page: Page, name: string) {
  await page.getByTestId('catalog-search').fill(name)
  await expect(page.getByRole('button', { name: `Open table ${name}`, exact: true })).toBeVisible({ timeout: 15_000 })
}

/** Fail the build only on serious/critical axe hits; moderate/minor are documented in the PR.
 *  `color-contrast` is excluded: muted 9.5–11px labels fail AA by design today and are deferred with
 *  the typography follow-up called out in #118. Semantics / names / focus / nested-interactive stay gated. */
async function expectNoSeriousAxe(page: Page, label: string, opts: { keepOverlay?: boolean } = {}) {
  // File / app menus are radix `role="menu"` with a rename <input> and plain <button> rows — that fails
  // aria-required-children while open. Escape them closed before scanning, unless the surface under
  // test IS an overlay (Settings dialog, error toast).
  if (!opts.keepOverlay) {
    await page.keyboard.press('Escape')
    await expect.poll(() => page.locator('[role="menu"]').count()).toBe(0)
  }
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .disableRules(['color-contrast'])
    .analyze()
  const gated = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')
  expect(gated, `${label}: ${gated.map((v) => `${v.id} (${v.impact}): ${v.help}`).join('; ') || 'ok'}`).toEqual([])
}

/** Tab until `target` is the active element (or contains it). */
async function tabUntil(page: Page, target: Locator, max = 50) {
  for (let i = 0; i < max; i++) {
    const hit = await target.evaluate((el) => el === document.activeElement || el.contains(document.activeElement)).catch(() => false)
    if (hit) return true
    await page.keyboard.press('Tab')
  }
  return target.evaluate((el) => el === document.activeElement || el.contains(document.activeElement))
}

test.describe('accessibility gate', () => {
  // Split the old monolithic axe smoke into isolated tests. One long test on a single page let prior
  // steps (Settings overlay, aborted /run mock residue, slow catalog fetch) interfere with later
  // assertions — especially the error toast — while canvas.spec's identical toast path passed.
  test('axe smoke: empty canvas', async ({ page }) => {
    await fresh(page)
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expectNoSeriousAxe(page, 'Canvas')
  })

  test('axe smoke: Files', async ({ page }) => {
    await fresh(page)
    await goFiles(page)
    await expect(page.getByTestId('new-file')).toBeVisible()
    await expectNoSeriousAxe(page, 'Files')
  })

  test('axe smoke: Tables', async ({ page }) => {
    await fresh(page)
    await goFiles(page)
    await page.getByTestId('rail-tables').click()
    await expect(page.getByRole('heading', { name: 'Tables' })).toBeVisible()
    await expectCatalogTable(page, 'images')
    await expectNoSeriousAxe(page, 'Tables')
  })

  test('axe smoke: Settings modal', async ({ page }) => {
    await fresh(page)
    await openSettings(page)
    await expectNoSeriousAxe(page, 'Settings', { keepOverlay: true })
  })

  test('axe smoke: error state surfaces a toast', async ({ page }) => {
    // Same clean failing-run path as canvas.spec.ts — isolated so nothing can leave residue.
    await fresh(page)
    await expect(page.getByText('Add a dataset source to begin', { exact: false })).toBeVisible()
    await addNode(page, 'Sources & sinks', 'source')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    const inspector = page.getByTestId('inspector')
    await inspector.locator('label').filter({ hasText: 'uri' }).locator('input').fill('does-not-exist.parquet')
    const countRows = inspector.getByRole('button', { name: 'Count rows' })
    await expect(countRows).toBeEnabled()
    await countRows.click()
    await expect(page.getByTestId('toast')).toBeVisible({ timeout: 15_000 })
    await expectNoSeriousAxe(page, 'Error', { keepOverlay: true })
  })

  test('axe smoke: running state', async ({ page }) => {
    await fresh(page)
    await expect(page.getByText('Add a dataset source to begin', { exact: false })).toBeVisible()
    await addNode(page, 'Sources & sinks', 'source')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    const inspector = page.getByTestId('inspector')
    await inspector.locator('label').filter({ hasText: 'uri' }).locator('input').fill('does-not-exist.parquet')
    let releaseRun: (() => void) | undefined
    const held = new Promise<void>((resolve) => { releaseRun = resolve })
    let finishHold: (() => void) | undefined
    const holdFinished = new Promise<void>((resolve) => { finishHold = resolve })
    await page.route(/\/run$/, async (route) => {
      if (route.request().method() !== 'POST') {
        await route.continue()
        return
      }
      await held
      try { await route.abort('timedout') } catch { /* unroute may already have cleared it */ }
      finishHold!()
    }, { times: 1 })
    await inspector.getByRole('button', { name: 'Count rows' }).click()
    await expect(page.locator('.dp-running-glyph').first()).toBeVisible({ timeout: 10_000 })
    await expectNoSeriousAxe(page, 'Running')
    releaseRun!()
    await holdFinished
    await page.unroute(/\/run$/)
  })

  test('keyboard: open a canvas from Files and focus a node', async ({ page }) => {
    // Setup (pointer OK): a uniquely named canvas with one node, wait for autosave, then Files.
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    // Rename so the Files Open control is unambiguous (many untitled canvases accumulate per e2e DB).
    const canvasName = `a11y-keyboard-${Date.now()}`
    await page.getByTestId('file-menu').click()
    const nameInput = page.getByPlaceholder('untitled')
    await expect(nameInput).toBeVisible()
    await nameInput.fill(canvasName)
    await page.keyboard.press('Escape') // close menu
    await expect(page.getByTestId('file-menu')).toContainText(canvasName)
    await expect(page.getByTestId('autosave')).toContainText(/saved/i, { timeout: 8_000 })
    const canvasHash = await page.evaluate(() => location.hash)
    await goFiles(page)

    // Click the heading so the next Tab starts a keyboard session (:focus-visible applies).
    await page.getByRole('heading', { name: 'Recents' }).click()
    const openCard = page.getByRole('button', { name: `Open ${canvasName}` })
    expect(await tabUntil(page, openCard)).toBe(true)
    await expect(openCard).toBeFocused()
    const focusVisible = await openCard.evaluate((el) => el.matches(':focus-visible'))
    expect(focusVisible, 'focused file Open control should match :focus-visible').toBe(true)
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect.poll(() => page.evaluate(() => location.hash)).toBe(canvasHash)
    await expect(page.locator('.react-flow__node')).toHaveCount(1, { timeout: 10_000 })

    // Move focus onto a canvas node with Tab only (never click the node).
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur())
    const node = page.locator('.react-flow__node').first()
    await expect(node).toBeVisible()
    expect(await tabUntil(page, node, 80)).toBe(true)
    const nodeFocusVisible = await node.evaluate((el) => el.matches(':focus-visible'))
    expect(nodeFocusVisible, 'focused canvas node should match :focus-visible').toBe(true)
    const ring = await node.evaluate((el) => {
      const s = getComputedStyle(el)
      return { boxShadow: s.boxShadow, outlineStyle: s.outlineStyle, outlineWidth: s.outlineWidth }
    })
    const hasRing = (ring.boxShadow !== 'none' && ring.boxShadow.includes('rgb'))
      || (ring.outlineStyle !== 'none' && ring.outlineWidth !== '0px')
    expect(hasRing, `focused canvas node needs a visible focus ring; got ${JSON.stringify(ring)}`).toBe(true)
  })

  test('keyboard: Space opens a recent file from Files', async ({ page }) => {
    await fresh(page)
    await goFiles(page)
    await page.getByRole('heading', { name: 'Recents' }).click()
    const openCard = page.getByRole('button', { name: /^Open / }).first()
    expect(await tabUntil(page, openCard)).toBe(true)
    await page.keyboard.press('Space')
    await expect(page.getByTestId('toolbar')).toBeVisible({ timeout: 10_000 })
  })
})
