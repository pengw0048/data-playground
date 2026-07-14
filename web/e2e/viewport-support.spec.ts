import { test, expect, type Page, type Locator } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { MIN_VIEWPORT } from '../support/min-viewport'

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

async function goToFilesShell(page: Page) {
  await page.goto('/')
  await expect(page.getByTestId('toolbar')).toBeVisible()
  await page.getByTestId('app-menu').click()
  await page.getByText('Back to files').click()
  await expect(page.getByTestId('rail-files')).toBeVisible()
}

async function openCanvasWithSource(page: Page) {
  await page.getByTestId('rail-tables').click()
  await expect(page.getByRole('heading', { name: 'Tables' })).toBeVisible()
  await page.getByText('events', { exact: true }).click()
  await page.getByTestId('detail-use').click()
  await expect(page.getByTestId('toolbar')).toBeVisible()
  await expect(page.locator('.react-flow__node')).toHaveCount(1)
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

  test('core surfaces stay visible and operable at the declared minimum', async ({ page }) => {
    const vp = page.viewportSize()
    expect(vp, 'Playwright project must pin MIN_VIEWPORT').toEqual(MIN_VIEWPORT)

    await goToFilesShell(page)

    // Navigation rail: four destinations plus Settings.
    for (const id of ['rail-files', 'rail-tables', 'rail-transforms', 'rail-relationships', 'rail-settings'] as const) {
      await expectFullyInViewport(page, page.getByTestId(id), id)
    }

    // Rail destinations are operable (click each, land on its surface, return).
    await page.getByTestId('rail-tables').click()
    await expect(page.getByRole('heading', { name: 'Tables' })).toBeVisible()
    await page.getByTestId('rail-transforms').click()
    await expect(page.getByRole('heading', { name: 'Transforms' })).toBeVisible()
    await page.getByTestId('rail-relationships').click()
    await expect(page.getByText('Relationships (ER)')).toBeVisible()
    await page.getByTestId('rail-files').click()
    await expect(page.getByRole('heading', { name: 'Recents' })).toBeVisible()

    // Settings from the rail.
    await page.getByTestId('rail-settings').click()
    const settings = page.getByTestId('settings-modal')
    await expectFullyInViewport(page, settings, 'settings modal (rail)')
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(settings).toHaveCount(0)

    // Canvas with at least one node, inspector, data panel, run controls.
    await openCanvasWithSource(page)
    const node = page.locator('.react-flow__node').first()
    await expectFullyInViewport(page, node, 'canvas node')
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
    // Seeded events preview paints a row-count label once the kernel returns.
    await expect(dataPanel.getByText(/^rows /)).toBeVisible({ timeout: 15_000 })
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

    // Settings from the canvas app menu as well.
    await page.getByTestId('app-menu').click()
    await page.getByText('Settings', { exact: true }).click()
    await expectFullyInViewport(page, page.getByTestId('settings-modal'), 'settings modal (canvas)')
  })
})
