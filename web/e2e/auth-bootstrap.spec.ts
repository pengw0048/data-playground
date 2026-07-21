import { expect, test } from '@playwright/test'

test('auth bootstrap stays fenced while unavailable and recovers on Retry without reloading', async ({ page }) => {
  let authRequests = 0
  let bootstrapRequests = 0
  let recovered = false
  let pageLoads = 0
  const bootstrapPaths = new Set(['/api/kernel', '/api/processors', '/api/nodes', '/api/me', '/api/users', '/api/canvas'])

  page.on('load', () => { pageLoads += 1 })
  page.on('request', (request) => {
    if (bootstrapPaths.has(new URL(request.url()).pathname)) bootstrapRequests += 1
  })
  await page.route('**/api/auth/status', (route) => {
    authRequests += 1
    if (!recovered) {
      return route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'auth bootstrap unavailable' }),
      })
    }
    return route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ authEnabled: false, userId: null }),
    })
  })

  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Connection unavailable' })).toBeVisible()
  await expect(page.getByText('Checked 3 times.')).toBeVisible()
  await expect(page.getByTestId('toolbar')).toHaveCount(0)
  expect(authRequests).toBe(3)
  expect(bootstrapRequests).toBe(0)
  expect(pageLoads).toBe(1)

  recovered = true
  await page.getByRole('button', { name: 'Retry connection' }).click()

  // Recovery restores the product's normal entry state: an existing Canvas opens, while a truly
  // fresh workspace offers its explicit Canvas choice.
  await expect.poll(async () => (
    await page.getByTestId('toolbar').count() + await page.getByTestId('first-run-canvas-choice').count()
  )).toBeGreaterThan(0)
  expect(authRequests).toBe(4)
  expect(bootstrapRequests).toBeGreaterThan(0)
  expect(pageLoads).toBe(1)
})
