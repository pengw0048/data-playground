import { test, expect } from '@playwright/test'

test.describe('local Workspace golden journey @ux-smoke', () => {
  test('shows normal canvas and catalog lifecycles through stable Workspace navigation', async ({ page }) => {
    const catalog = await page.request.get('/api/catalog/tables?limit=1')
    expect(catalog.ok()).toBe(true)
    const dataset = (await catalog.json()).items[0] as { id: string; name: string }
    expect(dataset).toBeTruthy()

    const canvasId = `workspace-golden-${Date.now()}`
    const canvasName = 'Workspace golden canvas'
    const created = await page.request.post('/api/canvas', {
      data: { id: canvasId, name: canvasName, version: 1, nodes: [], edges: [] },
    })
    expect(created.ok()).toBe(true)

    await page.goto('/#/workspace')
    await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible()
    await expect(page.getByRole('button', { name: `Open canvas ${canvasName}` })).toBeVisible()
    await expect(page.getByRole('button', { name: `Open dataset ${dataset.name}` })).toBeVisible()

    await page.getByRole('button', { name: `Open dataset ${dataset.name}` }).click()
    await expect(page.getByRole('dialog', { name: dataset.name })).toBeVisible()
    await page.getByRole('button', { name: 'Close' }).click()

    await page.getByRole('button', { name: `Open canvas ${canvasName}` }).click()
    await expect(page).toHaveURL(new RegExp(`/#/canvas/${canvasId}$`))
    await page.goBack()
    await expect(page).toHaveURL(/#\/workspace\/container%3Aworkspace-local-root$/)
    await expect(page.getByRole('button', { name: `Open canvas ${canvasName}` })).toBeVisible()
  })
})
