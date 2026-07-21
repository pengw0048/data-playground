import { expect, test } from '@playwright/test'

test.describe('Canvas Workspace placement context @ux-smoke', () => {
  test('retains a nested local location through reload, folder rename, and Canvas move at 1280px', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 })
    const suffix = Date.now()
    const destination = `Canvas destination ${suffix}`
    const parent = `Canvas parent ${suffix}`
    const renamedParent = `Canvas renamed parent ${suffix}`
    const child = `Canvas child ${suffix}`
    const canvas = `Canvas context ${suffix}`

    await page.goto('/#/workspace')
    for (const name of [destination, parent]) {
      await page.getByRole('button', { name: 'New folder' }).click()
      await page.getByLabel('Folder name').fill(name)
      await page.getByRole('button', { name: 'Create' }).click()
      await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: 'Workspace', exact: true }).click()
    }
    await page.getByRole('button', { name: `Open folder ${parent}` }).click()
    await page.getByRole('button', { name: 'New folder' }).click()
    await page.getByLabel('Folder name').fill(child)
    await page.getByRole('button', { name: 'Create' }).click()
    await page.getByRole('button', { name: 'New canvas here' }).click()
    await page.getByLabel('Canvas name').fill(canvas)
    await page.getByRole('button', { name: 'Create canvas' }).click()
    const canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!.split('?')[0])

    const location = page.getByRole('navigation', { name: 'Canvas Workspace location' })
    await expect(location).toContainText(`Workspace/${parent}/${child}/${canvas}`)
    const reloadedResolution = page.waitForResponse((response) =>
      response.url().includes(`/api/workspace/resources/${encodeURIComponent(`canvas:${canvasId}`)}`)
        && response.request().method() === 'GET')
    await page.reload()
    expect((await reloadedResolution).ok()).toBeTruthy()
    await expect(location).toContainText(`Workspace/${parent}/${child}/${canvas}`)

    // A Datasets filter cannot prove the Canvas is visible in this folder. Returning must reset
    // it atomically to All Workspace at the exact opaque parent location.
    await page.goto('/#/workspace?scope=datasets&dq=not-a-canvas-location')
    await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible()
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    await expect(location).toContainText(child)
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace', { exact: true }).click()
    await expect(page).not.toHaveURL(/scope=datasets|dq=not-a-canvas-location/)
    await expect(page.getByRole('navigation', { name: 'Workspace path' })).toContainText(`${parent}/${child}`)

    await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: parent, exact: true }).click()
    await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: 'Workspace', exact: true }).click()
    await page.getByRole('button', { name: `More actions for ${parent}` }).click()
    await page.getByRole('menuitem', { name: 'Rename' }).click()
    await page.getByLabel('Folder name').fill(renamedParent)
    await page.getByRole('button', { name: 'Rename' }).click()
    await page.getByRole('button', { name: `Open folder ${renamedParent}` }).click()
    await page.getByRole('button', { name: `Open folder ${child}` }).click()
    await page.getByRole('button', { name: `Open canvas ${canvas}` }).click()
    await expect(location).toContainText(`Workspace/${renamedParent}/${child}/${canvas}`)

    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace', { exact: true }).click()
    await page.getByRole('button', { name: `More actions for ${canvas}` }).click()
    await page.getByRole('menuitem', { name: 'Move' }).click()
    await page.getByRole('button', { name: destination, exact: true }).click()
    await page.getByRole('button', { name: `Move to ${destination}` }).click()
    await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: 'Workspace', exact: true }).click()
    await page.getByRole('button', { name: `Open folder ${destination}` }).click()
    await page.getByRole('button', { name: `Open canvas ${canvas}` }).click()
    await expect(location).toContainText(`Workspace/${destination}/${canvas}`)
  })
})
