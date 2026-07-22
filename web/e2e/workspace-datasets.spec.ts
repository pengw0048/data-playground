import { expect, test } from '@playwright/test'
import { unlink, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { workspaceResource } from './support/workspace'

type RegisteredDataset = {
  id: string
  registrationId: string
  metadataRevision: string
  name: string
}

test('discovers, previews, batch-uses, runs, and safely unregisters local datasets @ux-smoke', async ({ page }) => {
  test.slow()  // registers + previews + runs two canvases + batch-unregisters; the full-profile catalog pushes it past the 30s default
  const suffix = Date.now()
  const registeredName = `issue497_registered_${suffix}`
  const registeredFolder = `research/issue-497-${suffix}`
  const uploadedFilename = `issue497_uploaded_${suffix}.csv`
  const registeredPath = resolve('.e2e-workspace/data', `${registeredName}.csv`)
  const canvasName = `Issue 497 dataset exploration ${suffix}`
  let registered: RegisteredDataset | null = null
  let uploaded: RegisteredDataset | null = null
  let canvasId = ''

  await writeFile(registeredPath, 'id,value\n1,registered\n2,ready\n', 'utf8')
  try {
    await page.goto('/#/workspace')
    await page.getByRole('tab', { name: 'Datasets' }).click()
    await expect(page.getByRole('tab', { name: 'Datasets' })).toHaveAttribute('aria-selected', 'true')
    await expect(page).toHaveURL(/#\/workspace\?scope=datasets/)

    await page.getByTestId('register-dataset').click()
    await page.getByTestId('register-uri').fill(registeredPath)
    await page.getByLabel('Name (optional)').fill(registeredName)
    await page.getByLabel('Folder (optional)').fill(registeredFolder)
    await page.getByLabel('Tags (optional)').fill('robotics, acceptance')
    await page.getByLabel('Owner (optional)').fill('research-data')
    const registeredResponse = page.waitForResponse((response) => response.url().endsWith('/api/catalog/register') && response.request().method() === 'POST')
    await page.getByTestId('register-submit').click()
    registered = await (await registeredResponse).json() as RegisteredDataset
    expect(registered.registrationId).toBeTruthy()
    expect(registered.metadataRevision).toBeTruthy()

    await page.getByRole('tab', { name: 'All Workspace' }).click()
    const catalogRoot = await workspaceResource(page, 'catalog folder', 'research')
    await expect(catalogRoot.locator('..')).toContainText('Folder · Catalog organization')
    await catalogRoot.click()
    const projectedFolder = await workspaceResource(page, 'catalog folder', `issue-497-${suffix}`)
    await projectedFolder.click()
    await expect(await workspaceResource(page, 'dataset', registeredName)).toBeVisible()
    await expect(page).toHaveURL(/#\/workspace\/container%3A/)
    await page.reload()
    await expect(await workspaceResource(page, 'dataset', registeredName)).toBeVisible()
    await page.getByRole('tab', { name: 'Datasets' }).click()
    await page.getByRole('button', { name: 'All datasets' }).click()

    const uploadedResponse = page.waitForResponse((response) => response.url().endsWith('/api/catalog/upload') && response.request().method() === 'POST')
    await page.locator('input[type="file"]').setInputFiles({
      name: uploadedFilename,
      mimeType: 'text/csv',
      buffer: Buffer.from('id,value\n10,uploaded\n20,ready\n'),
    })
    uploaded = await (await uploadedResponse).json() as RegisteredDataset
    expect(uploaded.registrationId).toBeTruthy()
    expect(uploaded.metadataRevision).toBeTruthy()

    const search = page.getByTestId('catalog-search')
    await search.fill(registeredName)
    await expect(page).toHaveURL(new RegExp(`scope=datasets.*dq=${registeredName}`))
    await page.getByRole('button', { name: `Open dataset ${registeredName}` }).click()
    await expect(page.getByRole('dialog', { name: registeredName })).toBeVisible()
    await expect(page).toHaveURL(new RegExp(`#\\/workspace\\/dataset%3A${encodeURIComponent(registered.registrationId)}`))
    await page.getByTestId('detail-preview').click()
    await expect(page.getByRole('status').filter({ hasText: /Complete dataset|Dataset preview/ })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'registered' })).toBeVisible()

    await page.goBack()
    await expect(page.getByRole('dialog', { name: registeredName })).toHaveCount(0)
    await expect(search).toHaveValue(registeredName)
    await page.goForward()
    await expect(page.getByRole('dialog', { name: registeredName })).toBeVisible()
    await page.getByRole('button', { name: 'Close' }).click()
    await search.fill('issue497_')
    await expect(page.getByRole('button', { name: `Open dataset ${registeredName}` })).toBeVisible()
    await expect(page.getByRole('button', { name: `Open dataset ${uploaded.name}` })).toBeVisible()

    // A Catalog folder is displayed in both lenses, but the destination is an opaque projected
    // container resolved from the dataset registration—not a same-named local path.
    await page.getByRole('button', { name: `Open dataset ${registeredName}` }).click()
    await page.getByRole('button', { name: /Open in Workspace/ }).click()
    await expect(page).toHaveURL(/#\/workspace\/container%3A/)
    await expect(await workspaceResource(page, 'dataset', registeredName)).toBeVisible()
    await expect(page.getByText('Folder organization comes from this catalog. Canvases stored here are local to Data Playground.')).toBeVisible()
    await page.getByRole('tab', { name: 'Datasets' }).click()
    const folderQuery = `folder=${encodeURIComponent(registeredFolder)}`
    await expect(page).toHaveURL(new RegExp(`scope=datasets.*${folderQuery}`))
    await page.reload()
    await expect(page).toHaveURL(new RegExp(`scope=datasets.*${folderQuery}`))
    await expect(page.getByRole('button', { name: `Open dataset ${registeredName}` })).toBeVisible()
    await page.goBack()
    await expect(page).toHaveURL(/#\/workspace\/container%3A/)
    await expect(await workspaceResource(page, 'dataset', registeredName)).toBeVisible()
    await page.goForward()
    await expect(page).toHaveURL(new RegExp(`scope=datasets.*${folderQuery}`))

    await page.getByRole('button', { name: 'All datasets' }).click()
    await page.getByRole('checkbox', { name: `Select ${registeredName}` }).check()
    await page.getByRole('checkbox', { name: `Select ${uploaded.name}` }).check()
    const selection = page.getByTestId('catalog-selection-bar')
    await expect(selection).toContainText('2 selected')
    await selection.getByRole('button', { name: 'Use', exact: true }).click()
    await expect(page.getByRole('dialog', { name: 'Use 2 datasets' })).toContainText('applied atomically under one Canvas version precondition')
    await page.getByLabel('New canvas name').fill(canvasName)
    await page.getByRole('button', { name: 'Create and open' }).click()
    await expect(page).toHaveURL(/#\/canvas\//)
    canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!)
    await expect(page.locator('.react-flow__node', { hasText: registeredName })).toBeVisible()
    await expect(page.locator('.react-flow__node', { hasText: uploaded.name })).toBeVisible()

    const runIds: string[] = []
    page.on('response', async (response) => {
      if (new URL(response.url()).pathname !== '/api/run'
          || response.request().method() !== 'POST' || !response.ok()) return
      const body = await response.json() as { runId?: string }
      if (body.runId) runIds.push(body.runId)
    })
    await page.getByRole('button', { name: /rerun all/i }).click()
    await expect.poll(() => runIds.length).toBe(2)
    await expect.poll(async () => {
      return Promise.all(runIds.map(async (runId) => {
        const response = await page.request.get(`/api/run/${encodeURIComponent(runId)}`)
        if (!response.ok()) return 'unavailable'
        return (await response.json() as { status: string }).status
      }))
    }).toEqual(['done', 'done'])

    await expect(page.getByRole('navigation', { name: 'Canvas Workspace location' })).toBeVisible()
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace').click()
    await expect(page.getByRole('tab', { name: 'All Workspace' })).toHaveAttribute('aria-selected', 'true')
    await expect(page).toHaveURL(/#\/workspace\/container%3Aworkspace-local-root$/)
    await page.getByRole('tab', { name: 'Datasets' }).click()
    await expect(search).toHaveValue('')
    await search.fill('issue497_')
    await page.getByRole('checkbox', { name: `Select ${registeredName}` }).check()
    await page.getByRole('checkbox', { name: `Select ${uploaded.name}` }).check()
    page.once('dialog', (dialog) => dialog.accept())
    await page.getByTestId('catalog-delete-selected').click()
    const result = page.getByTestId('catalog-unregister-result')
    await expect(result).toContainText('Best-effort unregister result')
    await expect(result).toContainText(`${registeredName}: unregistered`)
    await expect(result.locator('span').filter({ hasText: ': unregistered' })).toHaveCount(2)
  } finally {
    if (registered) {
      await page.request.delete(`/api/catalog/tables/${encodeURIComponent(registered.id)}`, { params: {
        expected_registration_id: registered.registrationId,
        expected_revision: registered.metadataRevision,
      } })
    }
    if (uploaded) {
      await page.request.delete(`/api/catalog/tables/${encodeURIComponent(uploaded.id)}`, { params: {
        expected_registration_id: uploaded.registrationId,
        expected_revision: uploaded.metadataRevision,
      } })
    }
    if (canvasId) await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    await unlink(registeredPath).catch(() => {})
  }
})
