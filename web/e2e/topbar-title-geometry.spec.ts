import { expect, test, type Locator, type Page } from '@playwright/test'

const LONG_CANVAS_NAME = 'Quarterly customer acquisition and retention cohort analysis with regional attribution — July 2026 final review'
const VIEWPORTS = [
  { width: 1280, height: 720 },
  { width: 1440, height: 900 },
] as const

async function boxOf(locator: Locator) {
  const box = await locator.boundingBox()
  if (!box) throw new Error('element has no bounding box')
  return box
}

async function paintedRightOf(locator: Locator) {
  return locator.evaluate((element) => {
    const box = element.getBoundingClientRect()
    const range = document.createRange()
    range.selectNodeContents(element)
    const contentBox = range.getBoundingClientRect()
    return getComputedStyle(element).overflowX === 'visible'
      ? Math.max(box.right, contentBox.right)
      : box.right
  })
}

async function createCanvas(page: Page, name: string, suffix: string) {
  const canvasId = `topbar-title-${suffix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  const response = await page.request.post('/api/canvas', { data: {
    id: canvasId,
    name,
    version: 1,
    requirements: [],
    nodes: [],
    edges: [],
  } })
  expect(response.ok(), await response.text()).toBeTruthy()
  return canvasId
}

async function openCanvas(page: Page, canvasId: string) {
  await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
  await expect(page.getByTestId('inspector')).toHaveCount(0)
  await expect(page.getByTestId('canvas-title')).toBeVisible()
  await expect(page.getByTestId('kernel-badge')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Rerun all' })).toBeVisible()
  await expect(page.getByTestId('share-btn')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
}

async function expectRunControlsOperable(page: Page, fullName: string) {
  await page.getByTestId('canvas-title').click()
  await expect(page.getByRole('textbox', { name: 'Canvas name' })).toHaveValue(fullName)
  await page.keyboard.press('Escape')

  await page.getByTestId('kernel-badge').click()
  await expect(page.getByText('Canvas worker', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')

  const rerun = page.getByRole('button', { name: 'Rerun all' })
  await expect(rerun).toBeEnabled()
  await rerun.click()

  await page.getByTestId('share-btn').click()
  const shareDialog = page.getByRole('dialog')
    .filter({ has: page.getByRole('heading', { name: 'Share this canvas' }) })
  await expect(shareDialog).toBeVisible()
  await shareDialog.getByRole('button', { name: 'Close' }).click()
}

test('keeps long Canvas titles clear of independently operable run controls at desktop widths', async ({ page }) => {
  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport)
    const canvasId = await createCanvas(page, LONG_CANVAS_NAME, `${viewport.width}`)
    try {
      await openCanvas(page, canvasId)
      const title = page.getByTestId('canvas-title')
      const runControls = page.getByTestId('canvas-run-controls')
      const titleBox = await boxOf(title)
      const runBox = await boxOf(runControls)
      const titlePaintedRight = await paintedRightOf(title)

      expect(
        titlePaintedRight,
        `${viewport.width} title must end before run controls begin`,
      ).toBeLessThanOrEqual(runBox.x + 0.5)
      expect(titleBox.x + titleBox.width).toBeLessThanOrEqual(runBox.x + 0.5)
      await expect(title).toHaveAttribute('title', `${LONG_CANVAS_NAME} — click to rename`)
      await expectRunControlsOperable(page, LONG_CANVAS_NAME)
    } finally {
      expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBeTruthy()
    }
  }
})

test('preserves the compact short-title layout', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  const shortName = 'Short canvas'
  const canvasId = await createCanvas(page, shortName, 'short')
  try {
    await openCanvas(page, canvasId)
    const title = page.getByTestId('canvas-title')
    const titleMetrics = await title.evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
    }))
    expect(titleMetrics.scrollWidth).toBe(titleMetrics.clientWidth)
    await expect(title).toContainText(shortName)
    await expect(title).toHaveAttribute('title', `${shortName} — click to rename`)
  } finally {
    expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBeTruthy()
  }
})

test('browser Back cannot carry a title rollback into another Canvas', async ({ page }) => {
  const canvasAName = 'Canvas A original'
  const canvasBName = 'Canvas B original'
  const canvasA = await createCanvas(page, canvasAName, 'history-a')
  const canvasB = await createCanvas(page, canvasBName, 'history-b')
  try {
    await openCanvas(page, canvasB)
    await openCanvas(page, canvasA)
    await page.getByTestId('canvas-title').click()
    await page.getByRole('textbox', { name: 'Canvas name' }).fill('Canvas A in progress')

    await page.goBack()
    await expect(page).toHaveURL(new RegExp(`#\\/canvas\\/${encodeURIComponent(canvasB)}$`))
    await expect(page.getByRole('textbox', { name: 'Canvas name' })).toHaveCount(0)
    await expect(page.getByTestId('canvas-title')).toHaveText(canvasBName)

    // A late Escape from the detached A input must not rename the newly active B Canvas.
    await page.keyboard.press('Escape')
    await expect(page.getByTestId('canvas-title')).toHaveText(canvasBName)
    await expect(page.getByTestId('autosave')).toContainText('saved')
    const response = await page.request.get(`/api/canvas/${encodeURIComponent(canvasB)}`)
    expect(response.ok(), await response.text()).toBeTruthy()
    expect((await response.json()).name).toBe(canvasBName)
  } finally {
    for (const canvasId of [canvasA, canvasB]) {
      expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBeTruthy()
    }
  }
})
