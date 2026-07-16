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
  kind: 'canvas' | 'dataset' | 'container',
  name: string,
): Promise<Locator> {
  const resource = page.getByRole('button', { name: `Open ${kind} ${name}`, exact: true })
  for (let pageIndex = 0; pageIndex < 20; pageIndex++) {
    const loadMore = page.getByTestId('workspace-load-more')
    await expect(resource.or(loadMore).first()).toBeVisible({ timeout: 15_000 })
    if (await resource.isVisible()) return resource
    await Promise.all([
      page.waitForResponse((response) => response.url().includes('/api/workspace/containers/')),
      loadMore.click(),
    ])
  }
  await expect(resource).toBeVisible({ timeout: 15_000 })
  return resource
}
