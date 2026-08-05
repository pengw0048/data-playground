import { test, expect } from '@playwright/test'
import { workspaceResource } from './support/workspace'

test.describe('Workspace personal favorites', () => {
  test('favorites survive reload and appear in the Favorites shelf', async ({ page }) => {
    const suffix = Date.now()
    const canvasId = `workspace-favorite-${suffix}`
    const canvasName = `Workspace favorite canvas ${suffix}`
    const created = await page.request.post('/api/canvas', {
      data: { id: canvasId, name: canvasName, version: 1, nodes: [], edges: [] },
    })
    expect(created.ok()).toBe(true)

    try {
      await page.goto('/#/workspace')
      await expect(page.getByRole('navigation', { name: 'Workspace path' })).toBeVisible()
      await expect(await workspaceResource(page, 'canvas', canvasName)).toBeVisible()
      await page.getByRole('button', { name: `Add ${canvasName} to Favorites` }).click()
      await expect(page.getByRole('button', { name: `Remove ${canvasName} from Favorites` })).toBeVisible()

      await page.reload()
      await expect(page.getByRole('navigation', { name: 'Workspace path' })).toBeVisible()
      await expect(await workspaceResource(page, 'canvas', canvasName)).toBeVisible()
      await expect(page.getByRole('button', { name: `Remove ${canvasName} from Favorites` })).toBeVisible()

      await page.getByTestId('workspace-favorites-filter').click()
      await expect(page.getByRole('button', { name: `Open canvas ${canvasName}` })).toBeVisible()
      await page.getByRole('button', { name: `Remove ${canvasName} from Favorites` }).click()
      await expect(page.getByRole('button', { name: `Open canvas ${canvasName}` })).toHaveCount(0)
    } finally {
      await page.request.delete(`/api/workspace/favorites/${encodeURIComponent(`canvas:${canvasId}`)}`)
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })
})
