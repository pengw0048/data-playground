import { expect, type Page } from '@playwright/test'

export type AcceptanceScreenshotSurface = 'canvas' | 'jobs' | 'revision'

async function expectPrimaryContent(page: Page, surface: AcceptanceScreenshotSurface): Promise<void> {
  if (surface === 'canvas') {
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect(page.locator('.react-flow')).toBeVisible()
    await expect(page.locator('.react-flow__node').first()).toBeVisible()
    return
  }
  if (surface === 'jobs') {
    await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible()
    await expect(page.getByText('Loading Jobs…', { exact: true })).toHaveCount(0)
    return
  }

  const requested = await page.evaluate(() => {
    const query = location.hash.split('?', 2)[1] ?? ''
    const params = new URLSearchParams(query)
    return {
      datasetId: params.get('revisionDataset'),
      revisionId: params.get('revision'),
    }
  })
  expect(requested.datasetId, 'revision screenshot URL names a dataset').toBeTruthy()
  expect(requested.revisionId, 'revision screenshot URL names a revision').toBeTruthy()
  const viewer = page.getByTestId('dataset-viewer')
  await expect(viewer).toBeVisible()
  await expect(viewer.getByLabel('Dataset preview scope')).toContainText('from this selected version')
  await expect(viewer.getByTestId('detail-preview-scroll')).toBeVisible()
  await expect(viewer.getByTestId('dataset-version-context')).toHaveText(/^(Current|Previous|Selected) version$/)
  await expect(viewer).not.toContainText(`${requested.datasetId}@${requested.revisionId}`)
  await expect(viewer.getByText('Loading selected version preview…', { exact: true })).toHaveCount(0)
  await expect(viewer.getByText('Loading selected version schema…', { exact: true })).toHaveCount(0)
}

async function afterPaint(page: Page): Promise<void> {
  await page.evaluate(async () => {
    await document.fonts.ready
    await new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
    })
  })
}

async function closeTransientChrome(page: Page): Promise<void> {
  const openMenus = page.locator('[role="menu"][data-state="open"]')
  for (let attempt = 0; attempt < 4 && await openMenus.count(); attempt += 1) {
    await page.keyboard.press('Escape')
  }
  await expect(openMenus).toHaveCount(0)
  await expect(page.locator('[role="menu"]:visible')).toHaveCount(0)

  // These acceptance images document the default product state. Functional assertions may open
  // native details immediately before the capture; close them without scrolling the page away from
  // the primary surface being reviewed.
  await page.locator('details[open]').evaluateAll((elements) => {
    for (const element of elements) {
      if (element instanceof HTMLDetailsElement) element.open = false
    }
  })
  await expect(page.locator('details[open]')).toHaveCount(0)
}

async function resetPrimaryPosition(
  page: Page,
  surface: AcceptanceScreenshotSurface,
): Promise<void> {
  if (surface !== 'revision') return
  const content = page.getByTestId('dataset-detail-content')
  await content.evaluate((element) => {
    element.scrollTop = 0
    element.scrollLeft = 0
  })
  await expect.poll(() => content.evaluate((element) => ({
    left: element.scrollLeft,
    top: element.scrollTop,
  }))).toEqual({ left: 0, top: 0 })
}

export async function settleAcceptanceScreenshot(
  page: Page,
  surface: AcceptanceScreenshotSurface,
): Promise<void> {
  // Recheck after effects and layout have committed. History navigation can briefly render the old
  // exact-revision tree before clearing it for the destination request; one URL/content check alone
  // can therefore pass immediately before the loading state appears.
  await expectPrimaryContent(page, surface)
  await afterPaint(page)
  await expectPrimaryContent(page, surface)
  await closeTransientChrome(page)
  await resetPrimaryPosition(page, surface)
  await afterPaint(page)
  await expectPrimaryContent(page, surface)
  await expect(page.locator('details[open]')).toHaveCount(0)
  await expect(page.locator('[role="menu"]:visible')).toHaveCount(0)
}
