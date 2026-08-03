import { expect, test } from '@playwright/test'

test('the sync conflict chip reopens the recovery choice, before and after a reload', async ({ page }) => {
  test.setTimeout(90_000)
  const canvasId = `sync-conflict-chip-${Date.now()}`
  const created = await page.request.post('/api/canvas', {
    data: { id: canvasId, name: 'Sync conflict chip', version: 1, nodes: [], edges: [] },
  })
  expect(created.ok()).toBe(true)
  let recoveredId: string | null = null

  try {
    await page.goto(`/#/canvas/${canvasId}`)
    await expect(page.getByTestId('autosave')).toHaveText(/saved$/, { timeout: 8_000 })

    const canvasPath = (url: URL) => url.pathname === `/api/canvas/${canvasId}`
    await page.route(canvasPath, async (route) => {
      if (route.request().method() !== 'PUT') return route.continue()
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'another session saved first' }),
      })
    })

    await page.getByTestId('canvas-title').click()
    await page.getByRole('textbox', { name: 'Canvas name' }).fill(`Sync conflict ${Date.now()}`)
    await page.keyboard.press('Enter')

    const chip = page.getByRole('button', { name: 'Sync conflict — choose how to continue' })
    const toast = page.getByTestId('toast').filter({ hasText: 'Your local draft is preserved' })
    await expect(chip).toBeVisible({ timeout: 8_000 })
    await expect(toast).toBeVisible()

    // A blocking state must outlive the ordinary error-toast lifetime.
    await page.waitForTimeout(9_000)
    await expect(toast).toBeVisible()

    await toast.getByRole('button', { name: 'Dismiss' }).click()
    await expect(toast).toHaveCount(0)

    await chip.click()
    await expect(toast.getByRole('button', { name: 'Open server copy' })).toBeVisible()
    await expect(toast.getByRole('button', { name: 'Keep local draft as new Canvas' })).toBeVisible()
    await toast.getByRole('button', { name: 'Dismiss' }).click()

    await page.reload()
    await expect(chip).toBeVisible({ timeout: 8_000 })
    await expect(toast).toHaveCount(0)
    await chip.focus()
    await page.keyboard.press('Enter')
    await expect(toast).toBeVisible()

    await toast.getByRole('button', { name: 'Keep local draft as new Canvas' }).click()
    await expect(page.getByTestId('canvas-title')).toContainText('(recovered)', { timeout: 8_000 })
    await expect(page.getByTestId('autosave')).toHaveText(/saved$/, { timeout: 8_000 })
    await expect(chip).toHaveCount(0)
    recoveredId = new URL(page.url()).hash.replace('#/canvas/', '')
    expect(recoveredId).not.toBe(canvasId)
  } finally {
    await page.request.delete(`/api/canvas/${canvasId}`)
    if (recoveredId) await page.request.delete(`/api/canvas/${encodeURIComponent(recoveredId)}`)
  }
})
