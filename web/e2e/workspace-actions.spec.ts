import { test, expect } from '@playwright/test'

test.describe('Workspace capability actions @ux-smoke', () => {
  test('creates, renames, reloads, and reopens nested local folders at 1280x720', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 })
    const suffix = Date.now()
    const parent = `Workspace action parent ${suffix}`
    const child = `Workspace action child ${suffix}`
    const renamed = `Workspace action renamed ${suffix}`
    const canvas = `Workspace action Canvas ${suffix}`
    let folderDeleteWrites = 0
    page.on('request', (request) => {
      if (request.method() === 'DELETE' && request.url().includes('/api/workspace/folders/')) folderDeleteWrites += 1
    })

    await page.goto('/#/workspace')
    await page.getByRole('button', { name: 'New folder' }).click()
    await page.getByLabel('Folder name').fill(parent)
    await page.getByRole('button', { name: 'Create' }).click()
    await expect(page.getByRole('navigation', { name: 'Workspace path' })).toContainText(parent)

    await page.getByRole('button', { name: 'New folder' }).click()
    await page.getByLabel('Folder name').fill(child)
    await page.getByRole('button', { name: 'Create' }).click()
    await expect(page.getByRole('navigation', { name: 'Workspace path' })).toContainText(child)

    await page.getByRole('button', { name: 'New canvas here' }).click()
    await page.getByLabel('Canvas name').fill(canvas)
    await page.getByRole('button', { name: 'Create canvas' }).click()
    await expect(page).toHaveURL(/#\/canvas\//)
    const canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace').click()

    await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: parent }).click()
    await page.getByRole('button', { name: `More actions for ${child}` }).click()
    await page.getByRole('menuitem', { name: 'Delete' }).click()
    await expect(page.getByRole('dialog', { name: `Delete ${child}` })).toContainText('This folder must be empty before it can be deleted.')
    expect(folderDeleteWrites).toBe(0)
    await page.getByRole('button', { name: 'Open folder', exact: true }).click()
    await expect(page.getByRole('button', { name: `Open canvas ${canvas}` })).toBeVisible()
    await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: parent }).click()
    await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: 'Workspace', exact: true }).click()
    await page.getByRole('button', { name: `More actions for ${parent}` }).click()
    await page.getByRole('menuitem', { name: 'Rename' }).click()
    await page.getByLabel('Folder name').fill(renamed)
    await page.getByRole('button', { name: 'Rename' }).click()
    await page.reload()
    await expect(page.getByRole('button', { name: `Open folder ${renamed}` })).toBeVisible()
    await page.getByRole('button', { name: `Open folder ${renamed}` }).click()
    await page.getByRole('button', { name: `Open folder ${child}` }).click()
    await page.getByRole('button', { name: `Open canvas ${canvas}` }).click()
    await expect(page).toHaveURL(new RegExp(`/#/canvas/${canvasId}$`))
  })

  test('moves a local Canvas from its overflow menu and undoes the placement move', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 })
    const suffix = Date.now()
    const destination = `Workspace move destination ${suffix}`
    const canvas = `Workspace movable Canvas ${suffix}`

    await page.goto('/#/workspace')
    await page.getByRole('button', { name: 'New folder' }).click()
    await page.getByLabel('Folder name').fill(destination)
    await page.getByRole('button', { name: 'Create' }).click()
    await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: 'Workspace', exact: true }).click()
    await page.getByRole('button', { name: 'New canvas here' }).click()
    await page.getByLabel('Canvas name').fill(canvas)
    await page.getByRole('button', { name: 'Create canvas' }).click()
    await expect(page).toHaveURL(/#\/canvas\//)
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace').click()

    await page.getByRole('button', { name: `More actions for ${canvas}` }).click()
    await page.getByRole('menuitem', { name: 'Move' }).click()
    await page.getByRole('button', { name: destination, exact: true }).click()
    await expect(page.getByText(/Destination:/)).toContainText(`Workspace / ${destination}`)
    await page.getByRole('button', { name: `Move to ${destination}` }).click()
    const status = page.getByRole('status')
    await expect(status).toContainText(`Moved “${canvas}” to Workspace / ${destination}.`)
    await page.getByRole('button', { name: 'Undo move' }).click()
    await expect(status).toHaveCount(0)
    await expect(page.getByRole('button', { name: `Open canvas ${canvas}` })).toBeVisible()
  })
})
