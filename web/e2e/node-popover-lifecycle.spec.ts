import { test, expect, type Locator, type Page } from '@playwright/test'
import { MIN_VIEWPORT } from '../support/min-viewport'
import { backToWorkspace, workspaceResource } from './support/workspace'

const REFERENCE_VIEWPORT = { width: 1440, height: 900 }

async function boxOf(locator: Locator) {
  const box = await locator.boundingBox()
  if (!box) throw new Error('element has no bounding box')
  return box
}

async function expectInCanvasAndClearOfToolbar(page: Page, locator: Locator, label: string) {
  await expect(locator).toBeVisible()
  const [box, toolbar] = await Promise.all([boxOf(locator), boxOf(page.getByTestId('toolbar'))])
  const viewport = page.viewportSize()
  if (!viewport) throw new Error('page has no viewport')
  expect(box.x, `${label} is clipped on the left`).toBeGreaterThanOrEqual(-0.5)
  expect(box.y, `${label} is clipped at the top`).toBeGreaterThanOrEqual(-0.5)
  expect(box.x + box.width, `${label} is clipped on the right`).toBeLessThanOrEqual(viewport.width + 0.5)
  expect(box.y + box.height, `${label} is clipped at the bottom`).toBeLessThanOrEqual(viewport.height + 0.5)
  const overlapsToolbar = box.x < toolbar.x + toolbar.width
    && box.x + box.width > toolbar.x
    && box.y < toolbar.y + toolbar.height
    && box.y + box.height > toolbar.y
  expect(overlapsToolbar, `${label} overlaps the fixed toolbar`).toBe(false)
}

async function openCanvasWithSource(page: Page) {
  const canvasId = `node-popover-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  const created = await page.request.post('/api/canvas', { data: {
    id: canvasId, name: 'Node popover lifecycle', version: 1, nodes: [], edges: [],
  } })
  expect(created.ok()).toBe(true)
  await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
  await expect(page.getByTestId('toolbar')).toBeVisible()
  await backToWorkspace(page)
  const table = process.env.DP_E2E_FIXTURE_PROFILE === 'full' ? 'catalog_000' : 'events'
  await (await workspaceResource(page, 'dataset', table)).click()
  await page.getByTestId('detail-use').click()
  await page.getByRole('button', { name: /^Choose another Canvas/ }).click()
  await page.getByLabel('Target canvas').selectOption(canvasId)
  await page.getByRole('button', { name: 'Add and open' }).click()
  const node = page.locator('.react-flow__node-source')
  await expect(node).toHaveCount(1)
  await node.getByText('DATASET', { exact: true }).click()
  return node
}

test('node transient surfaces replace each other and stay clear of the toolbar', async ({ page }, testInfo) => {
  const expectedViewport = testInfo.project.name === 'chromium-reference-viewport' ? REFERENCE_VIEWPORT : MIN_VIEWPORT
  expect(page.viewportSize()).toEqual(expectedViewport)
  const node = await openCanvasWithSource(page)
  const picker = page.locator('.dp-panel').filter({ has: page.getByTestId('source-search') })
  const menu = page.getByRole('menu')

  // Picker → More: opening More replaces the picker.
  await node.getByRole('button', { name: 'Change dataset' }).click()
  await expect(picker).toBeVisible()
  await node.getByRole('button', { name: 'More' }).click()
  await expect(picker).toHaveCount(0)
  await expectInCanvasAndClearOfToolbar(page, menu, 'More menu')

  // More → picker: the reverse order has the same single-surface lifecycle.
  await menu.press('Escape')
  await expect(menu).toHaveCount(0)
  await node.getByRole('button', { name: 'More' }).click()
  await expect(menu).toBeVisible()
  await node.getByRole('button', { name: 'Change dataset' }).click()
  await expect(menu).toHaveCount(0)
  await expectInCanvasAndClearOfToolbar(page, picker, 'dataset picker')

  // Escape and one outside click each close the active surface without reopening another one.
  await page.keyboard.press('Escape')
  await expect(picker).toHaveCount(0)
  await node.getByRole('button', { name: 'More' }).click()
  await expect(menu).toBeVisible()
  await page.getByTestId('toolbar').click({ position: { x: 2, y: 2 } })
  await expect(menu).toHaveCount(0)
})
