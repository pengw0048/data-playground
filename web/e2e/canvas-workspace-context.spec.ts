import { expect, test } from '@playwright/test'

test.describe('Canvas Workspace placement context @ux-smoke', () => {
  test('creates an immediately chosen example beside a blank Canvas in the same nested folder', async ({ page }) => {
    const suffix = Date.now()
    const parent = `Example parent ${suffix}`
    const child = `Example child ${suffix}`
    const blankName = 'untitled'
    let blankId = ''
    let exampleId = ''
    let historyCanvasId = ''
    let historyStarted = false
    let releaseHistory!: () => void
    const historyGate = new Promise<void>((resolve) => { releaseHistory = resolve })

    try {
      await page.goto('/#/workspace')
      for (const folder of [parent, child]) {
        await page.getByRole('button', { name: 'New folder' }).click()
        await page.getByLabel('Folder name').fill(folder)
        await page.getByRole('button', { name: 'Create', exact: true }).click()
        await expect(page.getByRole('dialog', { name: 'New folder' })).toHaveCount(0)
      }
      await expect(page.getByRole('navigation', { name: 'Workspace path' })).toContainText(`${parent}/${child}`)

      // Keep the initial run-history check pending. The visible action must therefore promise a
      // separate example Canvas, and the create request must place it beside this blank Canvas.
      await page.route('**/api/canvas/*/runs', async (route) => {
        const match = /\/api\/canvas\/([^/]+)\/runs$/.exec(new URL(route.request().url()).pathname)
        if (route.request().method() !== 'GET' || !match) {
          await route.continue()
          return
        }
        historyCanvasId = decodeURIComponent(match[1])
        historyStarted = true
        await historyGate
        await route.fulfill({ json: [] }).catch(() => {
          // Navigating to the separately created example cancels the blank Canvas request.
        })
      })

      await page.getByRole('button', { name: 'Create canvas' }).click()
      const createCanvas = page.getByRole('dialog', { name: 'Create canvas' })
      await expect(createCanvas.getByLabel('Canvas name')).toHaveValue(blankName)
      await createCanvas.getByRole('button', { name: 'Create canvas' }).click()
      await expect(page).toHaveURL(/#\/canvas\//)
      blankId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!.split('?')[0])
      await expect.poll(() => historyStarted).toBe(true)
      expect(historyCanvasId).toBe(blankId)

      const example = page.getByRole('button', { name: 'Create example Canvas: Purchases per user' })
      await expect(example).toBeVisible()
      await example.click()
      await expect(page.locator('.react-flow__node').first()).toBeVisible()
      exampleId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!.split('?')[0])
      expect(exampleId).not.toBe(blankId)

      releaseHistory()
      await page.unroute('**/api/canvas/*/runs')
      await page.getByTestId('app-menu').click()
      await page.getByText('Back to Workspace', { exact: true }).click()
      const path = page.getByRole('navigation', { name: 'Workspace path' })
      await expect(path).toContainText(`${parent}/${child}`)
      await expect(page.getByRole('button', { name: `Open canvas ${blankName}` })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Open canvas Purchases per user' })).toBeVisible()

      await page.reload()
      await expect(path).toContainText(`${parent}/${child}`)
      await expect(page.getByRole('button', { name: `Open canvas ${blankName}` })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Open canvas Purchases per user' })).toBeVisible()
    } finally {
      releaseHistory?.()
      await page.unroute('**/api/canvas/*/runs').catch(() => {})
      if (exampleId) await page.request.delete(`/api/canvas/${encodeURIComponent(exampleId)}`)
      if (blankId) await page.request.delete(`/api/canvas/${encodeURIComponent(blankId)}`)
    }
  })

  test('retains a nested local location through reload, folder rename, and Canvas move at 1280px', async ({ page }) => {
    test.setTimeout(60_000)
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
      await page.getByRole('button', { name: 'Create', exact: true }).click()
      await expect(page.getByRole('dialog', { name: 'New folder' })).toHaveCount(0)
      await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: 'Workspace', exact: true }).click()
    }
    await page.getByRole('button', { name: `Open folder ${parent}` }).click()
    await page.getByRole('button', { name: 'New folder' }).click()
    await page.getByLabel('Folder name').fill(child)
    await page.getByRole('button', { name: 'Create', exact: true }).click()
    await expect(page.getByRole('dialog', { name: 'New folder' })).toHaveCount(0)
    await expect(page.getByText('This folder is empty. Create a Canvas here to get started.')).toBeVisible()
    await page.getByRole('button', { name: 'Create canvas' }).click()
    const createCanvas = page.getByRole('dialog', { name: 'Create canvas' })
    await expect(createCanvas).toBeVisible()
    await createCanvas.getByLabel('Canvas name').fill(canvas)
    await createCanvas.getByRole('button', { name: 'Create canvas' }).click()
    await expect(page).toHaveURL(/#\/canvas\//)
    const canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!.split('?')[0])

    const location = page.getByRole('navigation', { name: 'Canvas Workspace location' })
    await expect(location).toContainText(`Workspace/${parent}/${child}`)
    await expect(location).not.toContainText(canvas)
    for (const width of [1024, 1280]) {
      await page.setViewportSize({ width, height: 720 })
      const [locationBox, menuBox] = await Promise.all([
        location.boundingBox(), page.getByTestId('app-menu').boundingBox(),
      ])
      expect(locationBox).not.toBeNull()
      expect(menuBox).not.toBeNull()
      expect(locationBox!.y).toBeGreaterThanOrEqual(menuBox!.y + menuBox!.height)
      expect(locationBox!.x + locationBox!.width).toBeLessThanOrEqual(width)
    }
    const reloadedResolution = page.waitForResponse((response) =>
      decodeURIComponent(new URL(response.url()).pathname.split('/').pop() ?? '') === `canvas:${canvasId}`
        && response.request().method() === 'GET')
    await page.reload()
    expect((await reloadedResolution).ok()).toBeTruthy()
    await expect(location).toContainText(`Workspace/${parent}/${child}`)
    await expect(location).not.toContainText(canvas)

    // A global search cannot prove the Canvas is visible in this folder. Returning must clear it
    // atomically and restore the exact opaque parent location.
    await page.goto('/#/workspace?q=not-a-canvas-location')
    await expect(page.getByLabel('Search views, datasets, canvases, and containers'))
      .toHaveValue('not-a-canvas-location')
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    await expect(location).toContainText(child)
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace', { exact: true }).click()
    await expect(page).not.toHaveURL(/q=not-a-canvas-location/)
    await expect(page.getByRole('navigation', { name: 'Workspace path' })).toContainText(`${parent}/${child}`)

    await page.getByLabel(`Select ${canvas}`).check()
    await page.getByRole('button', { name: 'Duplicate', exact: true }).click()
    const duplicate = page.getByRole('dialog', { name: 'Duplicate canvas' })
    await expect(duplicate).toContainText(`Destination: ${child}`)
    await expect(duplicate.getByRole('navigation', { name: 'Choose copy destination' }))
      .toContainText(`Workspace${parent}${child}`)
    await duplicate.getByRole('button', { name: 'Cancel' }).click()

    await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: parent, exact: true }).click()
    await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: 'Workspace', exact: true }).click()
    await page.getByRole('button', { name: `More actions for ${parent}` }).click()
    await page.getByRole('menuitem', { name: 'Rename' }).click()
    const renameDialog = page.getByRole('dialog', { name: `Rename ${parent}` })
    await renameDialog.getByLabel('Folder name').fill(renamedParent)
    await renameDialog.getByRole('button', { name: 'Rename', exact: true }).click()
    await expect(renameDialog).toHaveCount(0)
    await expect(page.getByRole('navigation', { name: 'Workspace path' })).toContainText(renamedParent)
    await page.getByRole('button', { name: `Open folder ${child}` }).click()
    await page.getByRole('button', { name: `Open canvas ${canvas}` }).click()
    await expect(location).toContainText(`Workspace/${renamedParent}/${child}`)
    await expect(location).not.toContainText(canvas)

    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace', { exact: true }).click()
    await page.getByRole('button', { name: `More actions for ${canvas}` }).click()
    await page.getByRole('menuitem', { name: 'Move' }).click()
    await page.getByRole('button', { name: destination, exact: true }).click()
    await page.getByRole('button', { name: `Move to ${destination}` }).click()
    await page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: 'Workspace', exact: true }).click()
    await page.getByRole('button', { name: `Open folder ${destination}` }).click()
    await page.getByRole('button', { name: `Open canvas ${canvas}` }).click()
    await expect(location).toContainText(`Workspace/${destination}`)
    await expect(location).not.toContainText(canvas)
  })
})
