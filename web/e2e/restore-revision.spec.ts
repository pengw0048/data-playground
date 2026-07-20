import { test, expect } from '@playwright/test'
import { workspaceResource } from './support/workspace'

const REVISION = (revisionId: string) => ({
  datasetId: 'stable-dataset', revisionId, committedAt: '2026-07-18T12:00:00Z', retentionOwner: 'core',
})
const DETAIL = (revisionId: string, rows: number) => ({
  ...REVISION(revisionId), parentRevisionId: null, producerOperation: 'overwrite',
  summary: { rowCount: rows, dataFileCount: 1, totalBytes: rows * 8, fragmentCount: 1 },
  preview: {
    columns: [{ fieldId: 'amount', name: 'amount', type: 'bigint', nullable: false, provenance: 'core', capabilities: [] }],
    rows: Array.from({ length: rows }, (_value, index) => ({ amount: index })), hasMore: false, rowLimit: 100,
  },
})

async function openHistory(page: import('@playwright/test').Page) {
  const catalog = await page.request.get('/api/catalog/tables?limit=1')
  expect(catalog.ok()).toBe(true)
  const dataset = (await catalog.json()).items[0] as { id: string; name: string }
  expect(dataset).toBeTruthy()
  await page.route('**/api/catalog/tables/*/revisions*', (route) =>
    route.fulfill({ json: { items: [REVISION('rev-head'), REVISION('rev-old')], nextCursor: null, hasMore: false } }))
  await page.route('**/api/catalog/revisions/**', (route) => {
    const parts = new URL(route.request().url()).pathname.split('/')
    const revisionId = decodeURIComponent(parts[parts.length - 1])
    route.fulfill({ json: DETAIL(revisionId, revisionId === 'rev-head' ? 3 : revisionId === 'rev-new' ? 2 : 2) })
  })
  await page.goto('/#/workspace')
  await (await workspaceResource(page, 'dataset', dataset.name)).click()
  await expect(page.getByTestId('dataset-revision-history')).toBeVisible()
  await page.getByRole('button', { name: 'Open revision rev-old' }).click()
  await expect(page.getByText('Exact revision rev-old')).toBeVisible()
}

test('restores an old revision as a new head and reopens the exact result', async ({ page }) => {
  await openHistory(page)
  // Registered last so it wins over the detail route for the restore POST url.
  await page.route('**/api/catalog/revisions/*/*/restore', (route) =>
    route.fulfill({ json: {
      taskId: 'restore-task', status: 'done', sourceDatasetId: 'stable-dataset',
      sourceRevisionId: 'rev-old', expectedHeadRevisionId: 'rev-head', childRevisionId: 'rev-new',
      diagnosticCode: null, receipt: null,
    } }))

  await page.getByTestId('restore-revision').click()
  const dialog = page.getByRole('dialog', { name: 'Restore revision as new head' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('rev-old')).toBeVisible()  // the source being restored
  await expect(dialog.getByText('rev-head')).toBeVisible()  // the current destination head
  await dialog.getByTestId('restore-revision-confirm').click()

  // Completion reopens the exact new revision through the ordinary Catalog surface.
  await expect(page.getByText('Exact revision rev-new')).toBeVisible()
  await expect(dialog).toBeHidden()
})

test('reports a moving-head conflict and publishes nothing', async ({ page }) => {
  await openHistory(page)
  await page.route('**/api/catalog/revisions/*/*/restore', (route) =>
    route.fulfill({ json: {
      taskId: 'restore-task', status: 'failed', sourceDatasetId: 'stable-dataset',
      sourceRevisionId: 'rev-old', expectedHeadRevisionId: 'rev-head', childRevisionId: null,
      diagnosticCode: 'stale_expected_head', receipt: null,
    } }))

  await page.getByTestId('restore-revision').click()
  const dialog = page.getByRole('dialog', { name: 'Restore revision as new head' })
  await dialog.getByTestId('restore-revision-confirm').click()
  await expect(dialog.getByRole('alert')).toContainText(/current head changed/i)
  await expect(page.getByText('Exact revision rev-new')).toBeHidden()
})
