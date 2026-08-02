import { expect, type Locator, type Page } from '@playwright/test'

export async function goToWorkspace(page: Page) {
  await page.goto('/#/workspace')
  await expect(page.getByRole('navigation', { name: 'Workspace path' })).toBeVisible()
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
  await expect(page.getByRole('navigation', { name: 'Workspace path' })).toBeVisible()
}

export async function createCanvasFromWorkspace(page: Page, name = `E2E Canvas ${Date.now()}`) {
  await goToWorkspace(page)
  const previous = await page.evaluate(() => location.hash)
  const create = page.getByRole('button', { name: 'Create canvas', exact: true })
  await expect(create).toBeEnabled()
  await create.click()
  const dialog = page.getByRole('dialog', { name: 'Create canvas' })
  await dialog.getByLabel('Canvas name').fill(name)
  await dialog.getByRole('button', { name: 'Create canvas', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect.poll(() => page.evaluate(() => location.hash)).toMatch(/^#\/canvas\/.+/)
  await expect.poll(() => page.evaluate(() => location.hash)).not.toBe(previous)
  await expect(page.getByTestId('toolbar')).toBeVisible()
  return decodeURIComponent(new URL(page.url()).hash.split('?')[0].split('/').pop()!)
}

export async function workspaceResource(
  page: Page,
  kind: 'canvas' | 'dataset' | 'container' | 'catalog folder',
  name: string,
): Promise<Locator> {
  // Catalog-managed folders share the user-facing Folder kind in All; the opaque target retains
  // their authority without exposing another resource kind to assistive technology.
  const resourceKind = kind === 'catalog folder' ? 'folder' : kind
  const resource = page.getByRole('button', { name: `Open ${resourceKind} ${name}`, exact: true })
  const loadMore = page.getByTestId('workspace-load-more')
  const nextPage = page.getByTestId('workspace-next-page')
  const pageNumber = page.getByRole('navigation', { name: 'Workspace pages' }).getByText(/^Page \d+$/)
  for (let pageIndex = 0; pageIndex < 30; pageIndex++) {
    await expect(resource.or(loadMore).or(nextPage).first()).toBeVisible({ timeout: 15_000 })
    if (await resource.isVisible()) return resource
    // Support both legacy provider continuation and the file-browser page controls. Advance one
    // bounded page, tolerate a detaching control, then re-probe for the requested resource.
    const control = await loadMore.isVisible().catch(() => false)
      ? loadMore
      : await nextPage.isVisible().catch(() => false)
          && await nextPage.isEnabled().catch(() => false)
        ? nextPage
        : null
    if (!control) break
    const previousPage = control === nextPage ? await pageNumber.textContent() : null
    await Promise.all([
      page.waitForResponse(
        (response) => response.url().includes('/api/workspace/containers/'),
        { timeout: 8_000 },
      ).catch(() => null),
      control.click(),
    ])
    // The browse response can resolve before React commits its new page. Do not issue another
    // Next action against the old cursor while that render is still pending.
    if (previousPage) await expect(pageNumber).not.toHaveText(previousPage)
  }
  await expect(resource).toBeVisible({ timeout: 15_000 })
  return resource
}
