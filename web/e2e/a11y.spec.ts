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

/** Fail the build only on serious/critical axe hits; moderate/minor are documented in the PR.
 *  `color-contrast` is excluded: muted 9.5–11px labels fail AA by design today and are deferred with
 *  the typography follow-up called out in #118. Semantics / names / focus / nested-interactive stay gated. */
async function expectNoSeriousAxe(page: Page, label: string) {
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
  test('axe smoke: Files, Canvas, Tables, Settings, running, and error states', async ({ page }) => {
    test.setTimeout(60_000)
    await fresh(page)

    // Canvas (empty editor chrome)
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expectNoSeriousAxe(page, 'Canvas')

    // Files
    await goFiles(page)
    await expect(page.getByTestId('new-file')).toBeVisible()
    await expectNoSeriousAxe(page, 'Files')

    // Tables
    await page.getByTestId('rail-tables').click()
    await expect(page.getByRole('heading', { name: 'Tables' })).toBeVisible()
    await expect(page.getByText('images', { exact: true })).toBeVisible()
    await expectNoSeriousAxe(page, 'Tables')

    // Settings (modal over the canvas)
    await page.getByTestId('rail-files').click()
    await page.getByTestId('new-file').click()
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await openSettings(page)
    await expectNoSeriousAxe(page, 'Settings')
    await page.keyboard.press('Escape')

    // Running state — hold POST /run while we scan, then abort + unroute before the error step.
    // Error toasts auto-dismiss in 7s; keep running/error as separate setups so a slow CI axe pass
    // cannot race that window (the coupled setup failed in GH Actions).
    await addNode(page, 'Sources & sinks', 'source')
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
    })
    await inspector.getByRole('button', { name: 'Count rows' }).click()
    await expect(page.locator('.dp-running-glyph').first()).toBeVisible({ timeout: 10_000 })
    await expectNoSeriousAxe(page, 'Running')
    releaseRun!()
    await holdFinished
    await page.unroute(/\/run$/)

    // Error state — dedicated failing run (same path as canvas.spec.ts).
    await fresh(page)
    await addNode(page, 'Sources & sinks', 'source')
    const errInsp = page.getByTestId('inspector')
    await errInsp.locator('label').filter({ hasText: 'uri' }).locator('input').fill('does-not-exist.parquet')
    await errInsp.getByRole('button', { name: 'Count rows' }).click()
    await expect(page.getByTestId('toast')).toBeVisible({ timeout: 15_000 })
    await expectNoSeriousAxe(page, 'Error')
  })

  test('keyboard: open a canvas from Files and focus a node', async ({ page }) => {
    // Setup (pointer OK): a uniquely named canvas with one node, wait for autosave, then Files.
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    // Rename so the Files Open control is unambiguous (many untitled canvases accumulate per e2e DB).
    await page.getByTestId('file-menu').click()
    const nameInput = page.getByPlaceholder('untitled')
    await expect(nameInput).toBeVisible()
    await nameInput.fill('a11y-keyboard-canvas')
    await page.keyboard.press('Escape') // close menu
    await expect(page.getByTestId('file-menu')).toContainText('a11y-keyboard-canvas')
    await expect(page.getByTestId('autosave')).toContainText(/saved/i, { timeout: 8_000 })
    const canvasHash = await page.evaluate(() => location.hash)
    await goFiles(page)

    // Click the heading so the next Tab starts a keyboard session (:focus-visible applies).
    await page.getByRole('heading', { name: 'Recents' }).click()
    const openCard = page.getByRole('button', { name: 'Open a11y-keyboard-canvas' })
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
