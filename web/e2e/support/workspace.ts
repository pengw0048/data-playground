import { expect, type Locator, type Page } from '@playwright/test'

export async function goToWorkspace(page: Page) {
  await page.goto('/#/workspace')
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible()
}

export async function backToWorkspace(page: Page) {
  await page.getByTestId('app-menu').click()
  await page.getByText('Back to Workspace').click()
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible()
}

export async function workspaceResource(
  page: Page,
  kind: 'canvas' | 'dataset' | 'container' | 'catalog folder',
  name: string,
): Promise<Locator> {
  const resource = page.getByRole('button', { name: `Open ${kind} ${name}`, exact: true })
  const loadMore = page.getByTestId('workspace-load-more')
  for (let pageIndex = 0; pageIndex < 30; pageIndex++) {
    await expect(resource.or(loadMore).first()).toBeVisible({ timeout: 15_000 })
    if (await resource.isVisible()) return resource
    // Advance a page, tolerating a detaching load-more, then re-probe.
    const settled = page
      .waitForResponse((response) => response.url().includes('/api/workspace/containers/'), { timeout: 8_000 })
      .catch(() => null)
    const advanced = await loadMore.click({ timeout: 5_000 }).then(() => true).catch(() => false)
    if (advanced) await settled
  }
  await expect(resource).toBeVisible({ timeout: 15_000 })
  return resource
}
