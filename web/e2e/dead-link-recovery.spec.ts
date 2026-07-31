import { expect, test } from '@playwright/test'

test('recovers from unavailable Transform and distribution-report deep links', async ({ page }) => {
  await page.goto('/#/workspace')
  const missingTransform = '/#/transforms/no-such-transform?q=robot&version=v9'
  await page.goto(missingTransform)
  await expect(page.getByRole('alert')).toContainText(/not found/i)
  await page.getByRole('button', { name: 'Back to Transforms' }).click()
  await expect(page).toHaveURL(/#\/transforms\?q=robot$/)
  await expect(page.getByText('Select a Transform to inspect its exact versions and use it.')).toBeVisible()
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
