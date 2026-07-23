import { expect, test } from '@playwright/test'

type CatalogTable = {
  id: string
  registrationId?: string | null
  name: string
  uri: string
  columns: Array<{ name: string; type: string; capabilities?: string[] }>
}

test('Workspace renders bounded field evidence and opens only its resolved target @ux-smoke', async ({ page }) => {
  const catalog = await page.request.get('/api/catalog/tables?limit=10')
  expect(catalog.ok()).toBeTruthy()
  const source = (await catalog.json() as { items: CatalogTable[] }).items[0]
  expect(source).toBeTruthy()
  const evidenceColumn = {
    name: 'foreign_id', type: 'int64', physicalType: 'INT64', nullable: false,
    hasDefault: null, fieldId: 'fixture.foreign_id', provenance: 'provider', capabilities: [],
    annotations: [{ key: 'fixture.note', value: 'bounded annotation', encoding: 'utf8', provenance: 'provider' }],
    rowReference: {
      target: { kind: 'exact', datasetId: 'evidence-target', revisionId: 'target-r7' },
      keyFields: ['id'], semanticType: 'fixture target', provenance: 'provider',
    },
  }

  // Selecting a dataset from the page intentionally reuses its bounded list row rather than
  // refetching it. Replace only that list response; the later logical target lookup remains a
  // separate request and proves the UI does not substitute a different head.
  await page.route(/\/api\/catalog\/tables\?.+$/, async (route) => {
    const response = await route.fetch()
    const body = await response.json() as { items?: CatalogTable[] }
    await route.fulfill({ response, json: {
      ...body,
      items: body.items?.map((table) => table.id === source.id ? { ...table, columns: [evidenceColumn] } : table),
    } })
  })
  await page.route('**/api/catalog/tables/evidence-target', async (route) => {
    await route.fulfill({ json: { id: 'current-target-registration', registrationId: 'current-target-registration', name: 'Current target display', uri: 'mem://current-target', columns: [] } })
  })

  await page.goto('/#/workspace?scope=datasets')
  await page.getByRole('button', { name: `Open dataset ${source.name}` }).click()
  await expect(page.getByRole('dialog', { name: source.name })).toBeVisible()
  await page.getByRole('button', { name: 'Inspect evidence for foreign_id' }).click()

  const evidence = page.getByTestId('field-evidence-foreign_id')
  await expect(evidence).toContainText('dataset:evidence-target · revision:target-r7')
  await expect(evidence).toContainText('bounded annotation')
  await expect(evidence).toContainText('Current target display')
  await page.getByRole('link', { name: 'Open current catalog entry' }).click()
  await expect(page).toHaveURL(/#\/workspace\/current-target-registration$/)
})
