import { expect, test } from '@playwright/test'
import { access, unlink, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { canvasIdFromLocation } from './support/canvasRoute'
import { workspaceResource } from './support/workspace'

type RegisteredDataset = {
  id: string
  registrationId: string
  metadataRevision: string
  name: string
}

test('adds, previews, uses, and removes a local dataset in the unified Workspace @ux-smoke', async ({ page }) => {
  const suffix = Date.now()
  const registeredName = `workspace_dataset_${suffix}`
  const registeredFolder = `research/workspace-${suffix}`
  const registeredPath = resolve('.e2e-workspace/data', `${registeredName}.csv`)
  const canvasName = `Workspace dataset exploration ${suffix}`
  let registered: RegisteredDataset | null = null
  let canvasId = ''

  await writeFile(registeredPath, 'id,value\n1,registered\n2,ready\n', 'utf8')
  try {
    await page.goto('/#/workspace')
    await expect(page.getByRole('tab')).toHaveCount(0)

    // Catalog folders are selected rather than typed. Create this test folder before opening the
    // bounded picker so the registration journey uses the same choice users see.
    const folderResponse = await page.request.post('/api/catalog/folders', { data: { path: registeredFolder } })
    expect(folderResponse.ok()).toBeTruthy()
    await page.getByTestId('workspace-add-data').click()
    await page.getByRole('button', { name: 'Register path or URI' }).click()
    await page.getByTestId('register-uri').fill(registeredPath)
    await page.getByLabel('Name (optional)').fill(registeredName)
    await page.getByLabel('Folder (optional)').selectOption(registeredFolder)
    const registeredResponse = page.waitForResponse((response) =>
      response.url().endsWith('/api/catalog/register') && response.request().method() === 'POST')
    await page.getByTestId('register-submit').click()
    registered = await (await registeredResponse).json() as RegisteredDataset
    expect(registered.registrationId).toBeTruthy()
    await expect(page.getByRole('dialog', { name: 'Register a dataset' })).toHaveCount(0)

    const catalogRoot = await workspaceResource(page, 'catalog folder', 'research')
    await catalogRoot.click()
    await expect(page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: 'research' })).toBeVisible()
    const projectedFolder = await workspaceResource(page, 'catalog folder', `workspace-${suffix}`)
    await projectedFolder.click()
    await expect(page.getByRole('navigation', { name: 'Workspace path' }).getByRole('button', { name: `workspace-${suffix}` })).toBeVisible()
    const dataset = await workspaceResource(page, 'dataset', registeredName)
    await expect(dataset).toBeVisible()
    await page.reload()
    await (await workspaceResource(page, 'dataset', registeredName)).click()

    const viewer = page.getByTestId('dataset-viewer')
    await expect(viewer).toBeVisible()
    await expect(viewer.getByRole('heading', { name: 'Data preview' })).toBeVisible()
    await expect(viewer.getByRole('status').filter({ hasText: 'Showing 2 preview rows.' })).toBeVisible()
    await expect(viewer.getByRole('cell', { name: 'registered' })).toBeVisible()
    await viewer.getByRole('button', { name: 'Use in Canvas' }).click()

    const useDialog = page.getByRole('dialog', { name: `Use ${registeredName}` })
    await expect(useDialog).toContainText('Explore in a new Canvas')
    await useDialog.getByLabel('New canvas name').fill(canvasName)
    await useDialog.getByRole('button', { name: 'Create and open' }).click()
    await expect(page).toHaveURL(/#\/canvas\//)
    canvasId = canvasIdFromLocation(page.url())
    await expect(page.locator('.react-flow__node', { hasText: registeredName })).toBeVisible()

    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace', { exact: true }).click()
    await expect(page).toHaveURL(/#\/workspace\/container%3A/)
    await page.getByRole('button', { name: `More actions for ${registeredName}` }).click()
    await page.getByRole('menuitem', { name: 'Remove dataset…' }).click()

    const removeDialog = page.getByRole('dialog', { name: `Remove ${registeredName}` })
    await expect(removeDialog).toContainText('Remove from Workspace')
    await removeDialog.getByText('Delete the source file too').click()
    await expect(removeDialog).toContainText(registeredPath)
    const removedResponse = page.waitForResponse((response) =>
      response.url().includes('/api/catalog/tables/') && response.request().method() === 'DELETE')
    await removeDialog.getByRole('button', { name: 'Delete file and remove' }).click()
    expect((await removedResponse).ok()).toBeTruthy()
    registered = null
    await expect(removeDialog).toHaveCount(0)
    await expect.poll(async () => access(registeredPath).then(() => true).catch(() => false)).toBe(false)
    await expect(page.getByRole('button', { name: `Open dataset ${registeredName}` })).toHaveCount(0)
  } finally {
    if (registered) {
      await page.request.delete(`/api/catalog/tables/${encodeURIComponent(registered.id)}`, { params: {
        expected_registration_id: registered.registrationId,
        expected_revision: registered.metadataRevision,
      }, timeout: 3_000 }).catch(() => {})
    }
    if (canvasId) await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`, { timeout: 3_000 }).catch(() => {})
    await page.request.post('/api/catalog/folders/delete', {
      data: { path: registeredFolder },
      timeout: 3_000,
    }).catch(() => {})
    await unlink(registeredPath).catch(() => {})
  }
})
