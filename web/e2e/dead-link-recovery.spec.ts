import { expect, test } from '@playwright/test'

test('replaces an unknown route with Workspace without borrowing a Canvas', async ({ page }) => {
  const canvasId = `dead-link-recovery-${Date.now()}`
  const created = await page.request.post('/api/canvas', { data: {
    id: canvasId,
    name: 'Dead link recovery source',
    version: 1,
    requirements: [],
    nodes: [],
    edges: [],
  } })
  expect(created.ok(), await created.text()).toBe(true)
  try {
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    await expect(page.getByTestId('toolbar')).toBeVisible()
    const canvasUrl = page.url()

    await page.evaluate(() => { location.hash = '#/not-a-route' })
    await expect(page).toHaveURL(/#\/workspace$/)
    await expect(page.getByRole('navigation', { name: 'Workspace path' })).toBeVisible()
    await expect(page.getByTestId('toolbar')).toHaveCount(0)
    await page.goBack()
    await expect(page).toHaveURL(canvasUrl)
    await expect(page.getByTestId('toolbar')).toBeVisible()

    await page.evaluate(() => { location.hash = '#//another-unknown-route' })
    await expect(page).toHaveURL(/#\/workspace$/)
    await page.reload()
    await expect(page.getByRole('navigation', { name: 'Workspace path' })).toBeVisible()
    await page.goBack()
    await expect(page).toHaveURL(canvasUrl)

    await page.evaluate(() => { location.hash = '#/workspace/%E0%A4%A' })
    await expect(page).toHaveURL(/#\/workspace$/)
    await page.goBack()
    await expect(page).toHaveURL(canvasUrl)

    await page.evaluate(() => { location.hash = '#/transforms/%E0%A4%A' })
    await expect(page).toHaveURL(/#\/transforms$/)
    await expect(page.getByRole('heading', { name: 'Transforms' })).toBeVisible()
    await page.reload()
    await expect(page).toHaveURL(/#\/transforms$/)
  } finally {
    expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBe(true)
  }
})

test('recovers from unavailable Transform and distribution-report deep links', async ({ page }) => {
  await page.goto('/#/workspace')
  const missingTransform = '/#/transforms/no-such-transform?q=robot&version=v9'
  await page.goto(missingTransform)
  await expect(page.getByRole('alert')).toContainText(/not found/i)
  await page.getByRole('button', { name: 'Back to Transforms' }).click()
  await expect(page).toHaveURL(/#\/transforms\?q=robot$/)
  await expect(page.getByText('Select a Transform to inspect its versions and use it.')).toBeVisible()
  await page.goBack()
  await expect(page).toHaveURL(/#\/workspace$/)

  await page.goto(missingTransform)
  await expect(page.getByRole('alert')).toContainText(/not found/i)
  await page.getByTestId('rail-transforms').click()
  await expect(page).toHaveURL(/#\/transforms\?q=robot$/)

  await page.goto('/#/workspace')
  const missingReport = '/#/jobs?status=failed&canvas=demo-canvas&report=no-such-report&compare=no-such-comparison'
  await page.goto(missingReport)
  await expect(page.getByRole('alert')).toContainText(/does not exist|not visible/i)
  await page.getByRole('link', { name: 'Back to Jobs' }).click()
  await expect(page).toHaveURL(/#\/jobs\?status=failed&canvas=demo-canvas$/)
  await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible()
  await page.goBack()
  await expect(page).toHaveURL(/#\/workspace$/)

  await page.goto(missingReport)
  await expect(page.getByRole('alert')).toContainText(/does not exist|not visible/i)
  await page.getByTestId('rail-jobs').click()
  await expect(page).toHaveURL(/#\/jobs\?status=failed&canvas=demo-canvas$/)
})
