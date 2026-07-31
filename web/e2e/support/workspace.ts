import { expect, type Locator, type Page } from '@playwright/test'

export async function goToWorkspace(page: Page) {
  await page.goto('/#/workspace')
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible()
}

export async function backToWorkspace(page: Page) {
  const menu = page.getByRole('menu', { name: 'Data Playground menu' })
  // A preceding menu selection may still be finishing its close animation while a newly created
  // Canvas settles. Reopening before that portal is hidden makes Radix replace the clicked item.
  await expect(menu).toBeHidden()
  await expect(page.getByTestId('autosave')).toContainText(/saved/)
  await page.getByTestId('app-menu').click()
  await expect(menu).toBeVisible()
  await menu.getByRole('menuitem', { name: 'Back to Workspace', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible()
}

export async function workspaceResource(
  page: Page,
  kind: 'canvas' | 'dataset' | 'container' | 'catalog folder',
  name: string,
): Promise<Locator> {
  // Catalog-managed folders are one user-facing Folder model in All Workspace. Their authority is
  // visible in supporting copy, not encoded in a separate accessible resource kind.
  const resourceKind = kind === 'catalog folder' ? 'folder' : kind
  const resource = page.getByRole('button', { name: `Open ${resourceKind} ${name}`, exact: true })
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
