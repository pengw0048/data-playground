import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { goldenCanvas, installCanvas } from './support/ux-fixtures'

test.describe('researcher golden workflow @ux-smoke', () => {
  test('targets the chosen canvas and labels/downloads only a preview sample', async ({ page }) => {
    const primary = goldenCanvas('ux-golden-primary', 'UX primary canvas', 'UX primary source')
    const secondary = goldenCanvas('ux-golden-secondary', 'UX secondary canvas', 'UX secondary source')
    await installCanvas(page.request, primary)
    await installCanvas(page.request, secondary)

    await page.goto(`/#/canvas/${primary.id}`)
    const primaryNode = page.locator('.react-flow__node', { hasText: 'UX primary source' })
    await expect(primaryNode).toBeVisible()
    await expect(page.locator('.react-flow__node', { hasText: 'UX secondary source' })).toHaveCount(0)

    await primaryNode.click()
    await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
    const panel = page.getByTestId('panel-data')
    await expect(panel).toBeVisible()
    await expect(panel.getByText('sample', { exact: true })).toBeVisible()
    const csv = panel.getByTitle(/Download these rows as CSV.*previewed sample only/)
    await expect(csv).toBeVisible()
    const downloaded = page.waitForEvent('download')
    await csv.click()
    const download = await downloaded
    expect(download.suggestedFilename()).toBe('UX_primary_source.csv')
    const file = await download.path()
    expect(file).not.toBeNull()
    const rows = readFileSync(file!, 'utf8').trim().split('\n')
    expect(rows[0]).toBe('id,user_id,event,amount')
    expect(rows).toHaveLength(51) // header + the bounded 50-row preview, never a silent full export

    await page.goto(`/#/canvas/${secondary.id}`)
    await expect(page.locator('.react-flow__node', { hasText: 'UX secondary source' })).toBeVisible()
    await expect(page.locator('.react-flow__node', { hasText: 'UX primary source' })).toHaveCount(0)

    await page.goto('/#/files')
    await expect(page.getByRole('heading', { name: 'Recents' })).toBeVisible()
    page.once('dialog', async (dialog) => {
      expect(dialog.message()).toContain('UX secondary canvas')
      expect(dialog.message()).toContain("can't be undone")
      await dialog.dismiss()
    })
    await page.getByRole('button', { name: 'Delete UX secondary canvas' }).click()
    await expect(page.getByRole('button', { name: 'Open UX secondary canvas' })).toBeVisible()
  })

  test('a changed graph invalidates the old result instead of treating it as current', async ({ page }) => {
    const doc = goldenCanvas('ux-golden-stale', 'UX stale canvas', 'UX stale source')
    await installCanvas(page.request, doc)

    await page.goto(`/#/canvas/${doc.id}`)
    const filter = page.locator('.react-flow__node', { hasText: 'UX golden filter' })
    await expect(filter.getByTitle('latest')).toBeVisible()
    await filter.click()
    await filter.getByPlaceholder('is_valid = true AND score > 0.5').fill("event = 'signup' OR amount > 0")
    await expect(filter.getByTitle('stale')).toBeVisible()
  })
})
