import { test, expect, type Page, type Locator } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { MIN_VIEWPORT } from '../support/min-viewport'
import { backToWorkspace, workspaceResource } from './support/workspace'

const REFERENCE_VIEWPORT = { width: 1440, height: 900 }

// Smoke that every core desktop surface remains visible, unclipped, and operable at the declared
// minimum viewport (docs/BROWSER_SUPPORT.md ↔ web/support/min-viewport.ts).

async function boxOf(loc: Locator) {
  const b = await loc.boundingBox()
  if (!b) throw new Error('element has no bounding box')
  return b
}

async function expectFullyInViewport(page: Page, loc: Locator, label: string) {
  await expect(loc, `${label} should be visible`).toBeVisible()
  const box = await boxOf(loc)
  const vp = page.viewportSize()
  if (!vp) throw new Error('page has no viewport size')
  expect(box.width, `${label} has no width`).toBeGreaterThan(0)
  expect(box.height, `${label} has no height`).toBeGreaterThan(0)
  expect(box.x, `${label} clipped on the left`).toBeGreaterThanOrEqual(-0.5)
  expect(box.y, `${label} clipped on the top`).toBeGreaterThanOrEqual(-0.5)
  expect(box.x + box.width, `${label} clipped on the right`).toBeLessThanOrEqual(vp.width + 0.5)
  expect(box.y + box.height, `${label} clipped on the bottom`).toBeLessThanOrEqual(vp.height + 0.5)
}

async function expectToolbarGroupsDoNotOverlap(page: Page, label: string) {
  const toolbar = page.getByTestId('toolbar')
  const addControls = page.getByTestId('toolbar-add-controls')
  const viewControls = page.getByTestId('toolbar-view-controls')
  await expectFullyInViewport(page, toolbar, `${label} toolbar`)
  await expectFullyInViewport(page, addControls, `${label} add controls`)
  await expectFullyInViewport(page, viewControls, `${label} view controls`)
  const addBox = await boxOf(addControls)
  const viewBox = await boxOf(viewControls)
  expect(addBox.x + addBox.width, `${label} add and view controls overlap`).toBeLessThanOrEqual(viewBox.x + 0.5)
}

async function goToWorkspaceShell(page: Page) {
  await page.goto('/')
  await expect(page.getByTestId('toolbar')).toBeVisible()
  await backToWorkspace(page)
  await expect(page.getByTestId('rail-workspace')).toBeVisible()
}

async function openCanvasWithSource(page: Page) {
  // Full-profile workflows create named canvases before this spec. Start fresh so the visibility
  // assertions exercise one source node rather than inheriting their graph.
  await page.goto('/')
  await expect(page.getByTestId('toolbar')).toBeVisible()
  const previous = await page.evaluate(() => location.hash)
  await page.getByTestId('file-menu').click()
  await page.getByText('New file').click()
  await expect.poll(() => page.evaluate(() => location.hash)).not.toBe(previous)
  await expect(page.locator('.react-flow__node')).toHaveCount(0)
  const canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
  await backToWorkspace(page)

  // The full UX fixture replaces the small smoke catalog, so follow bounded load-more pages.
  const starterTable = process.env.DP_E2E_FIXTURE_PROFILE === 'full' ? 'catalog_000' : 'events'
  await (await workspaceResource(page, 'dataset', starterTable)).click()
  await page.getByTestId('detail-use').click()
  const chooseCanvas = page.getByRole('button', { name: /^Choose a Canvas/ })
  await expect(chooseCanvas).toBeEnabled()
  await chooseCanvas.click()
  await page.getByLabel('Target canvas').selectOption(canvasId)
  await page.getByRole('button', { name: 'Add and open' }).click()
  await expect(page.getByTestId('toolbar')).toBeVisible()
  await expect(page.locator('.react-flow__node')).toHaveCount(1)
  await page.locator('.react-flow__node').getByText('DATASET', { exact: true }).click()
}

test.describe('minimum viewport support', () => {
  test('docs quote the shared MIN_VIEWPORT constant', () => {
    const docPath = fileURLToPath(new URL('../../docs/BROWSER_SUPPORT.md', import.meta.url))
    const doc = readFileSync(docPath, 'utf8')
    const quoted = `${MIN_VIEWPORT.width}×${MIN_VIEWPORT.height}`
    expect(
      doc,
      `docs/BROWSER_SUPPORT.md must quote ${quoted} from web/support/min-viewport.ts`,
    ).toContain(quoted)
    expect(doc).toContain('web/support/min-viewport.ts')
  })

  test('core surfaces stay visible and operable at the tested desktop viewport', async ({ page }, testInfo) => {
    const vp = page.viewportSize()
    const expectedViewport = testInfo.project.name === 'chromium-reference-viewport'
      ? REFERENCE_VIEWPORT
      : MIN_VIEWPORT
    expect(vp, 'Playwright project must pin an exercised desktop viewport').toEqual(expectedViewport)

    await goToWorkspaceShell(page)

    // Navigation rail: Workspace, Transforms, and Settings (Relationships is reached from a dataset drawer).
    for (const id of ['rail-workspace', 'rail-transforms', 'rail-settings'] as const) {
      await expectFullyInViewport(page, page.getByTestId(id), id)
    }

    // Rail destinations are operable (click each, land on its surface, return).
    await page.getByTestId('rail-transforms').click()
    await expect(page.getByRole('heading', { name: 'Transforms' })).toBeVisible()
    await page.getByTestId('rail-workspace').click()
    await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible()

    // Settings from the rail.
    await page.getByTestId('rail-settings').click()
    const settings = page.getByTestId('settings-modal')
    await expectFullyInViewport(page, settings, 'settings modal (rail)')
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(settings).toHaveCount(0)

    // Browse and inspect a dataset without the detail drawer hiding its close/use actions.
    await (await workspaceResource(page, 'dataset', process.env.DP_E2E_FIXTURE_PROFILE === 'full' ? 'catalog_000' : 'events')).click()
    const detail = page.getByRole('dialog', { name: process.env.DP_E2E_FIXTURE_PROFILE === 'full' ? 'catalog_000' : 'events' })
    await expectFullyInViewport(page, detail, 'dataset detail')
    await expectFullyInViewport(page, detail.getByTestId('detail-use'), 'dataset use action')
    await expectFullyInViewport(page, detail.getByRole('button', { name: 'Close' }), 'dataset detail close')
    await detail.getByRole('button', { name: 'Close' }).click()

    // Canvas with at least one node, inspector, data panel, run controls.
    await openCanvasWithSource(page)
    const node = page.locator('.react-flow__node').first()
    await expectFullyInViewport(page, node, 'canvas node')
    const addControls = page.getByTestId('toolbar-add-controls')
    const viewControls = page.getByTestId('toolbar-view-controls')
    await expectToolbarGroupsDoNotOverlap(page, 'Canvas')
    await expect(viewControls.getByText('Fit view', { exact: true })).toBeVisible()
    await expect(viewControls.getByRole('group', { name: 'Viewport controls' })).toBeVisible()
    await expect(viewControls.getByRole('group', { name: 'Panel controls' })).toBeVisible()
    for (const name of ['Zoom in', 'Zoom out', 'Fit view', 'Hide Inspector']) {
      await expectFullyInViewport(page, viewControls.getByRole('button', { name }), `Canvas ${name}`)
    }
    await expectFullyInViewport(page, page.getByTestId('inspector'), 'inspector')
    // Inspector run control stays reachable at the minimum viewport (sources label it Count rows).
    await expectFullyInViewport(
      page,
      page.getByTestId('inspector').getByRole('button', { name: 'Count rows' }),
      'inspector run control',
    )

    await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
    const dataPanel = page.getByTestId('panel-data')
    await expectFullyInViewport(page, dataPanel, 'data panel')
    // The seeded source preview paints a row-count label once the kernel returns.
    await expect(dataPanel.getByText(/^rows \d+–\d+$/)).toBeVisible({ timeout: 15_000 })
    await dataPanel.getByTitle('Close').click()
    await expect(dataPanel).toHaveCount(0)

    // Cheap runs do not auto-open the floating run panel — open Run details from the node menu.
    await node.click()
    await node.getByRole('button', { name: 'More' }).click()
    const runDetails = page.getByText('Run details', { exact: true })
    await expect(runDetails).toBeVisible()
    await runDetails.click()
    const runPanel = page.getByTestId('panel-run')
    await expectFullyInViewport(page, runPanel, 'run panel')
    await expect(runPanel.getByText(/estimating|rows|ESTIMATE|DONE|FAILED/i).first()).toBeVisible()
    await runPanel.getByTitle('Close').click()
    await expect(runPanel).toHaveCount(0)

    await page.getByTestId('app-menu').click()
    await page.getByText('Run history', { exact: true }).click()
    const runHistory = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: 'Run history' }) })
    await expectFullyInViewport(page, runHistory, 'run history')
    await runHistory.getByRole('button', { name: 'Close' }).click()

    // Settings from the canvas app menu as well.
    await page.getByTestId('app-menu').click()
    await page.getByText('Settings', { exact: true }).click()
    await expectFullyInViewport(page, page.getByTestId('settings-modal'), 'settings modal (canvas)')
  })

  test('panel choices persist and the canvas tracks the real remaining viewport', async ({ page }) => {
    await goToWorkspaceShell(page)
    const rail = page.getByTestId('workspace-rail')
    expect((await boxOf(rail)).width).toBeCloseTo(232, 0)
    await page.getByTestId('rail-collapse').click()
    await expect(page.getByRole('button', { name: 'Expand navigation' })).toBeVisible()
    expect((await boxOf(rail)).width).toBeCloseTo(64, 0)
    await page.reload()
    await expect(page.getByRole('button', { name: 'Expand navigation' })).toBeVisible()
    expect((await boxOf(rail)).width).toBeCloseTo(64, 0)

    await openCanvasWithSource(page)
    const inspector = page.getByTestId('inspector')
    expect((await boxOf(inspector)).width).toBeCloseTo(300, 0)
    await page.getByTestId('inspector-collapse').click()
    await expect(page.getByRole('button', { name: 'Expand Inspector' })).toBeVisible()
    expect((await boxOf(inspector)).width).toBeCloseTo(44, 0)

    const flowBox = await boxOf(page.locator('.react-flow'))
    const inspectorBox = await boxOf(inspector)
    expect(flowBox.x + flowBox.width).toBeCloseTo(inspectorBox.x, 0)

    await page.reload()
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Expand Inspector' })).toBeVisible()
    expect((await boxOf(inspector)).width).toBeCloseTo(44, 0)
    await page.getByTestId('inspector-collapse').click()
    await expect(page.getByRole('button', { name: 'Collapse Inspector' })).toBeVisible()

    // React Flow's Fit View control operates inside the flexed canvas, whose right edge is the
    // current Inspector edge. The fitted node must remain inside that real region.
    await page.getByRole('button', { name: 'Fit view', exact: true }).click()
    const fittedFlow = await boxOf(page.locator('.react-flow'))
    const fittedNode = await boxOf(page.locator('.react-flow__node').first())
    expect(fittedNode.x).toBeGreaterThanOrEqual(fittedFlow.x)
    expect(fittedNode.x + fittedNode.width).toBeLessThanOrEqual(fittedFlow.x + fittedFlow.width)

    // An already-open floating panel must be re-anchored after the Inspector changes width.
    const node = page.locator('.react-flow__node').first()
    await node.getByText('DATASET', { exact: true }).click()
    await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
    const dataPanel = page.getByTestId('panel-data')
    await expect(dataPanel).toBeVisible()
    await page.getByTestId('inspector-collapse').click()
    await page.getByTestId('inspector-collapse').click()
    await expect.poll(async () => {
      const expandedInspector = await boxOf(inspector)
      const repositionedPanel = await boxOf(dataPanel)
      return repositionedPanel.x + repositionedPanel.width - expandedInspector.x
    }).toBeLessThanOrEqual(0)
  })

  test('1024px browse and inspect stays navigable with compact chrome', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 720 })
    await goToWorkspaceShell(page)

    const rail = page.getByTestId('workspace-rail')
    expect((await boxOf(rail)).width).toBeCloseTo(64, 0)
    const main = page.getByRole('main')
    expect((await boxOf(main)).width).toBeGreaterThan(900)
    for (const id of ['rail-workspace', 'rail-transforms', 'rail-settings'] as const) {
      await expectFullyInViewport(page, page.getByTestId(id), id)
    }

    const tableName = process.env.DP_E2E_FIXTURE_PROFILE === 'full' ? 'catalog_000' : 'events'
    await (await workspaceResource(page, 'dataset', tableName)).click()
    const detail = page.getByRole('dialog', { name: tableName })
    await expectFullyInViewport(page, detail, '1024px dataset detail')
    await expectFullyInViewport(page, detail.getByTestId('detail-use'), '1024px dataset use action')
    await detail.getByRole('button', { name: 'Close' }).click()

    await page.getByTestId('rail-collapse').click()
    await expect(page.getByRole('button', { name: 'Collapse navigation' })).toBeVisible()
    await page.reload()
    await expect(page.getByRole('button', { name: 'Collapse navigation' })).toBeVisible()
    await page.getByTestId('rail-settings').click()
    await expectFullyInViewport(page, page.getByTestId('settings-modal'), '1024px settings')

    await openCanvasWithSource(page)
    const viewControls = page.getByTestId('toolbar-view-controls')
    await expectToolbarGroupsDoNotOverlap(page, '1024px Canvas (Inspector collapsed)')
    for (const name of ['Zoom in', 'Zoom out', 'Fit view', 'Show Inspector']) {
      await expectFullyInViewport(page, viewControls.getByRole('button', { name }), `1024px Canvas ${name}`)
    }
    await page.getByRole('button', { name: 'Show Inspector', exact: true }).click()
    await expect.poll(() => viewControls.getByText('Fit view', { exact: true }).count()).toBe(0)
    await expectToolbarGroupsDoNotOverlap(page, '1024px Canvas (Inspector expanded)')
  })
})
