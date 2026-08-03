import { test, expect, type Page, type Locator } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import { backToWorkspace, createCanvasFromWorkspace, goToWorkspace, workspaceResource } from './support/workspace'

// Accessibility gate for issue #118: keyboard contract on Workspace/Canvas + one axe smoke suite that
// fails the build on serious/critical violations across the primary surfaces.

async function fresh(page: Page) {
  await createCanvasFromWorkspace(page)
  await expect(page.locator('.react-flow__node')).toHaveCount(0)
}

async function addNode(page: Page, category: string, kindTitle: string) {
  await page.getByRole('button', { name: category, exact: true }).click()
  const menu = page.locator('.dp-panel', { hasText: kindTitle }).last()
  await menu.getByText(kindTitle, { exact: true }).click()
}

async function openSettings(page: Page) {
  await page.getByTestId('app-menu').click()
  await page.getByText('Settings', { exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
}

/** Fail the build only on serious/critical axe hits; moderate/minor are documented in the PR.
 *  `color-contrast` is excluded: muted 9.5–11px labels fail AA by design today and are deferred with
 *  the typography follow-up called out in #118. Semantics / names / focus / nested-interactive stay gated. */
async function expectNoSeriousAxe(page: Page, label: string, opts: { keepOverlay?: boolean } = {}) {
  // Radix menus are transient overlays. Escape them closed before scanning, unless the surface under
  // test is itself an overlay (Settings dialog, error toast).
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

test.describe('accessibility gate @ux-smoke', () => {
  // Run serially — parallel e2e workers hammering the single kernel can leave the error-toast
  // run hanging past 15s even though canvas.spec's identical path passes in the same job.
  test.describe.configure({ mode: 'serial' })

  // Split the old monolithic axe smoke into isolated tests. One long test on a single page let prior
  // steps (Settings overlay, aborted /run mock residue, slow catalog fetch) interfere with later
  // assertions — especially the error toast — while canvas.spec's identical toast path passed.
  test('axe smoke: empty canvas', async ({ page }) => {
    await fresh(page)
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expectNoSeriousAxe(page, 'Canvas')
  })

  test('axe smoke: Workspace', async ({ page }) => {
    await fresh(page)
    await backToWorkspace(page)
    await expect(page.getByRole('button', { name: 'Create canvas' })).toBeEnabled()
    await expectNoSeriousAxe(page, 'Workspace')
  })

  test('axe smoke: Workspace dataset detail', async ({ page }) => {
    await fresh(page)
    await backToWorkspace(page)
    await (await workspaceResource(page, 'dataset', 'images')).click()
    await expect(page.getByRole('region', { name: 'images' })).toBeVisible()
    const detailContent = page.getByTestId('dataset-detail-content')
    await detailContent.focus()
    await expect(detailContent).toBeFocused()
    const previewScroll = page.getByTestId('detail-preview-scroll')
    await previewScroll.focus()
    await expect(previewScroll).toBeFocused()
    await expectNoSeriousAxe(page, 'Workspace dataset detail', { keepOverlay: true })
  })

  test('axe smoke: Settings modal', async ({ page }) => {
    await fresh(page)
    await openSettings(page)
    await expectNoSeriousAxe(page, 'Settings', { keepOverlay: true })
  })

  test('axe smoke: running state', async ({ page }) => {
    await fresh(page)
    await expect(page.getByText('Add a dataset source to begin', { exact: false })).toBeVisible()
    await addNode(page, 'Sources & sinks', 'source')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    const inspector = page.getByTestId('inspector')
    const catalogResponse = await page.request.get('/api/catalog/tables', {
      params: { q: 'images', limit: '10' },
    })
    expect(catalogResponse.ok(), await catalogResponse.text()).toBeTruthy()
    const registered = (await catalogResponse.json() as {
      items: Array<{ name: string; uri: string }>
    }).items.find((item) => item.name === 'images')
    expect(registered, 'the running-state fixture requires the registered images source').toBeTruthy()
    await inspector.getByText('Manual source settings', { exact: true }).click()
    await inspector.getByLabel('Dataset URI').fill(registered!.uri)
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

  test('keyboard: open a canvas from Workspace and focus a node', async ({ page }) => {
    // Setup (pointer OK): a uniquely named canvas with one node, wait for autosave, then Workspace.
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
    // Rename so the Workspace Open control is unambiguous (many untitled canvases accumulate per e2e DB).
    const canvasName = `a11y-keyboard-${Date.now()}`
    await page.getByTestId('canvas-title').click()
    const nameInput = page.getByRole('textbox', { name: 'Canvas name' })
    await expect(nameInput).toBeVisible()
    await nameInput.fill(canvasName)
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('canvas-title')).toContainText(canvasName)
    await expect(page.getByTestId('autosave')).toContainText(/saved/i, { timeout: 8_000 })
    const canvasHash = await page.evaluate(() => location.hash)
    await backToWorkspace(page)

    // Focus the root breadcrumb so the next Tab starts a keyboard session (:focus-visible applies).
    await page.getByRole('navigation', { name: 'Workspace path' })
      .getByRole('button', { name: 'Workspace', exact: true }).focus()
    const openCard = await workspaceResource(page, 'canvas', canvasName)
    // A full accumulated Workspace page can contain a checkbox, Open control, and actions menu
    // for each of 50 items before this Canvas. Keep the keyboard proof bounded to one page, not
    // to the smaller catalog that happens to exist in an isolated run.
    expect(await tabUntil(page, openCard, 200)).toBe(true)
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

  test('forced colours: focus and selection survive without box-shadow', async ({ page }) => {
    await page.emulateMedia({ forcedColors: 'active' })
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    const node = page.locator('.react-flow__node').first()
    await expect(node).toBeVisible()
    // Focus first: clicking the node would select it and add the shelf controls to the tab order.
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur())
    expect(await tabUntil(page, node, 80)).toBe(true)
    expect(await node.evaluate((el) => el.matches(':focus-visible'))).toBe(true)
    const focusOutline = await node.evaluate((el) => {
      const s = getComputedStyle(el)
      return { style: s.outlineStyle, width: s.outlineWidth }
    })
    expect(focusOutline.style, 'focused node needs an outline in forced colours').not.toBe('none')
    expect(focusOutline.width).not.toBe('0px')

    await node.click()
    const card = node.locator('[data-dp-card][data-selected]')
    await expect(card).toHaveCount(1)
    const selectionOutline = await card.evaluate((el) => {
      const s = getComputedStyle(el)
      return { style: s.outlineStyle, width: s.outlineWidth }
    })
    expect(selectionOutline.style, 'selected node needs an outline in forced colours').not.toBe('none')
    expect(selectionOutline.width).not.toBe('0px')
  })

  test('keyboard: a Workspace dialog traps focus and returns it on Escape', async ({ page }) => {
    await fresh(page)
    await backToWorkspace(page)
    const newFolder = page.getByRole('button', { name: 'New folder' })
    await newFolder.focus()
    await page.keyboard.press('Enter')
    const dialog = page.getByRole('dialog', { name: 'New folder' })
    await expect(dialog).toBeVisible()

    // Tab past the end of the dialog; focus must wrap inside it, never reach the page behind.
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press('Tab')
      const inside = await dialog.evaluate((el) => el.contains(document.activeElement))
      expect(inside, `Tab ${i + 1} left the dialog`).toBe(true)
    }

    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(newFolder).toBeFocused()
  })

  test('contrast: the primary button and the canvas focus ring pass in light mode', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    const node = page.locator('.react-flow__node').first()
    await expect(node).toBeVisible()
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur())
    expect(await tabUntil(page, node, 80)).toBe(true)

    const measured = await page.evaluate(() => {
      const parse = (value: string) => value.match(/[\d.]+/g)!.slice(0, 3).map(Number)
      const channel = (v: number) => (v / 255 <= 0.03928 ? v / 255 / 12.92 : (((v / 255) + 0.055) / 1.055) ** 2.4)
      const luminance = ([r, g, b]: number[]) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
      const contrast = (a: string, b: string) => {
        const [x, y] = [luminance(parse(a)), luminance(parse(b))]
        return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)
      }
      const share = getComputedStyle(document.querySelector('[data-testid="share-btn"]')!)
      const focused = getComputedStyle(document.activeElement!)
      const canvas = getComputedStyle(document.querySelector('.react-flow')!)
      return {
        shareLabel: contrast(share.color, share.backgroundColor),
        focusRing: contrast(focused.boxShadow.match(/rgb\([^)]*\)/)![0], canvas.backgroundColor),
      }
    })

    expect(measured.shareLabel, 'Share button label on its own fill').toBeGreaterThanOrEqual(4.5)
    expect(measured.focusRing, 'canvas focus ring against the canvas').toBeGreaterThanOrEqual(3)
  })

  test('focus: the Workspace search box and a focused edge are visibly indicated', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Sources & sinks', 'source')
    await addNode(page, 'Shape', 'filter')
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await page.locator('.react-flow__node').first().hover()
    // Wire the two nodes so there is an edge to focus.
    const from = page.locator('.react-flow__node').first().locator('.react-flow__handle-right')
    const to = page.locator('.react-flow__node').nth(1).locator('.react-flow__handle-left')
    await from.dragTo(to)
    const edge = page.locator('.react-flow__edge').first()
    await expect(edge).toBeVisible()

    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur())
    expect(await tabUntil(page, edge, 80)).toBe(true)
    const edgeStroke = await edge.evaluate((element) => {
      const path = element.querySelector('.react-flow__edge-path')!
      const style = getComputedStyle(path)
      return { stroke: style.stroke, width: style.strokeWidth, focusVisible: element.matches(':focus-visible') }
    })
    expect(edgeStroke.focusVisible).toBe(true)
    expect(parseFloat(edgeStroke.width), 'a focused edge must be drawn thicker than a resting one').toBeGreaterThan(1.5)

    await backToWorkspace(page)
    const search = page.getByRole('textbox', { name: /Search views, datasets/ })
    await search.focus()
    const ring = await search.evaluate((element) => {
      const box = element.closest('form')!
      const style = getComputedStyle(box)
      return { boxShadow: style.boxShadow, outlineStyle: style.outlineStyle, ownOutline: getComputedStyle(element).outlineStyle }
    })
    const indicated = (ring.boxShadow !== 'none' && ring.boxShadow.includes('rgb'))
      || ring.outlineStyle !== 'none' || ring.ownOutline !== 'none'
    expect(indicated, `focused search box needs an indicator; got ${JSON.stringify(ring)}`).toBe(true)
  })

  test('zoom: the minimap never covers the canvas at 200%', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await fresh(page)
    await addNode(page, 'Shape', 'filter')
    const node = page.locator('.react-flow__node').first()
    await expect(node).toBeVisible()
    await expect(page.locator('.react-flow__minimap')).toBeVisible()

    // 200% browser zoom halves the CSS viewport; the minimap kept its fixed size and sat on the graph.
    await page.setViewportSize({ width: 720, height: 450 })
    await expect(page.locator('.react-flow__minimap')).toHaveCount(0)
    await node.click()
    await expect(page.getByTestId('inspector')).toBeVisible()

    await page.setViewportSize({ width: 1440, height: 900 })
    await expect(page.locator('.react-flow__minimap')).toBeVisible()
  })

  test('an edge is named by the steps it connects, not their internal ids', async ({ page }) => {
    await fresh(page)
    await addNode(page, 'Sources & sinks', 'source')
    await addNode(page, 'Shape', 'filter')
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await expect.poll(async () => page.locator('.react-flow__edge').count()).toBe(1)

    const name = await page.locator('.react-flow__edge').first().getAttribute('aria-label')
    expect(name).toBe('Edge from source to filter')
  })

  test('a prefilled name dialog replaces its suggestion instead of appending to it', async ({ page }) => {
    await fresh(page)
    await backToWorkspace(page)
    await page.getByRole('button', { name: 'Create canvas' }).first().click()
    const dialog = page.getByRole('dialog', { name: 'Create canvas' })
    await expect(dialog).toBeVisible()

    const name = dialog.getByRole('textbox')
    await expect(name).toHaveValue('untitled')
    await page.keyboard.type('Quarterly revenue')
    await expect(name).toHaveValue('Quarterly revenue')

    await dialog.getByRole('button', { name: 'Cancel' }).focus()
    await name.focus()
    await page.keyboard.type(' v2')
    await expect(name).toHaveValue('Quarterly revenue v2')
  })

  test('zoom: the Workspace title is not clipped at 200%', async ({ page }) => {
    await fresh(page)
    await backToWorkspace(page)
    const title = page.getByRole('navigation', { name: 'Workspace path' })
      .getByRole('button', { name: 'Workspace', exact: true })
    await expect(title).toBeVisible()

    // 200% browser zoom on a supported 1440x900 display
    await page.setViewportSize({ width: 720, height: 450 })
    const clipped = await title.evaluate((element) => {
      const box = element.getBoundingClientRect()
      let node: Element | null = element.parentElement
      while (node) {
        const parent = node.getBoundingClientRect()
        if (getComputedStyle(node).overflowX !== 'visible' && box.right > parent.right + 0.5) return true
        node = node.parentElement
      }
      return element.scrollWidth > element.clientWidth + 0.5
    })
    expect(clipped, 'the Workspace title is cut off by an overflow ancestor').toBe(false)
  })

  test('keyboard: Space opens a canvas from Workspace', async ({ page }) => {
    // Build the target Canvas via the API so this test stays focused on Workspace keyboard behavior.
    await page.goto('/')
    const firstRun = page.getByRole('button', { name: 'Start a blank Canvas' })
    if (await firstRun.isVisible().catch(() => false)) await firstRun.click()
    await expect.poll(() => page.evaluate(() => location.hash)).toMatch(/^#\/canvas\/.+/)
    const canvasName = `a11y-space-${Date.now()}`
    expect((await page.request.post('/api/canvas', { data: {
      id: canvasName, name: canvasName, version: 1, requirements: [], nodes: [], edges: [],
    } })).ok()).toBeTruthy()
    await goToWorkspace(page)
    await page.getByRole('navigation', { name: 'Workspace path' })
      .getByRole('button', { name: 'Workspace', exact: true }).focus()
    const openCard = await workspaceResource(page, 'canvas', canvasName)
    expect(await tabUntil(page, openCard, 200)).toBe(true)
    await page.keyboard.press('Space')
    await expect(page.getByTestId('toolbar')).toBeVisible({ timeout: 10_000 })
  })
})
