import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test } from '@playwright/test'

const enabled = process.env.DP_E2E_PROVIDER_ACCEPTANCE === '1'
const providerRoot = process.env.DP_E2E_PROVIDER_ROOT
const containerNameA = 'Browser provider collection A'
const containerNameB = 'Browser provider collection B'
const datasetNameA = 'Browser provider observations'
const datasetNameB = 'Browser provider observations'

test.describe('provider Workspace Source acceptance', () => {
  test.skip(!enabled || !providerRoot, 'set DP_E2E_PROVIDER_ACCEPTANCE=1 and DP_E2E_PROVIDER_ROOT')

  test.beforeAll(() => {
    const root = resolve(providerRoot!)
    mkdirSync(root, { recursive: true })
    writeFileSync(resolve(root, 'observations.csv'), 'id,value\n1,alpha\n2,beta\n')
    writeFileSync(resolve(root, 'catalog.json'), JSON.stringify({ resources: [
      { placementId: 'browser-collection-a', kind: 'container', name: containerNameA },
      { placementId: 'browser-collection-b', kind: 'container', name: containerNameB },
      {
        placementId: 'browser-observations-a', datasetId: 'browser-canonical-observations',
        kind: 'dataset', name: datasetNameA, parentPlacementId: 'browser-collection-a',
        uri: 'observations.csv', revisionId: 'browser-provider-revision-v1',
        columns: [{ name: 'id', type: 'int64' }, { name: 'value', type: 'string' }],
      },
      {
        placementId: 'browser-observations-b', datasetId: 'browser-canonical-observations',
        kind: 'dataset', name: datasetNameB, parentPlacementId: 'browser-collection-b',
        uri: 'observations.csv',
        revisionId: 'browser-provider-revision-v1',
        columns: [{ name: 'id', type: 'int64' }, { name: 'value', type: 'string' }],
      },
    ] }))
  })

  test('deduplicates canonical provider placements, then previews, runs, and inspects the exact Source in Chromium', async ({ page }) => {
    test.setTimeout(60_000)
    const providerCatalogBefore = readFileSync(resolve(providerRoot!, 'catalog.json'))
    const providerDatasetBefore = readFileSync(resolve(providerRoot!, 'observations.csv'))
    await page.goto('/#/workspace')
    const container = page.getByRole('button', {
      name: new RegExp(`Open folder ${containerNameA} from Source-only mount browser-provider`),
    })
    await expect(container).toBeVisible({ timeout: 20_000 })
    const externalBrowse = page.waitForResponse((response) =>
      response.url().includes('/api/workspace/containers/')
      && response.request().method() === 'GET')
    await container.click()
    const externalPage = await (await externalBrowse).json() as {
      container: { id: string; resourceId?: string; providerMutation?: boolean; localPlacement?: { containerId: string; containerVersion: number } }
      items: Array<{ id: string; providerPlacementId?: string; providerDatasetId?: string }>
    }
    expect(externalPage.container.resourceId).toBe('browser-collection-a')
    expect(externalPage.container.providerMutation).toBe(false)
    const localPlacement = externalPage.container.localPlacement
    expect(localPlacement).toBeTruthy()
    const resource = page.getByRole('button', {
      name: new RegExp(`Open dataset ${datasetNameA} from Source-only mount browser-provider`),
    })
    await expect(resource).toBeVisible({ timeout: 20_000 })
    await resource.click()
    const detail = page.getByRole('dialog', { name: datasetNameA })
    await expect(detail).toContainText('Source-only mount browser-provider · dp-file-catalog')
    await expect(detail).toContainText('Workspace placement')
    await expect(detail).toContainText('Canonical datasetMountbrowser-providerDataset IDbrowser-canonical-observations')
    const placementAId = externalPage.items.find((item) => item.providerPlacementId === 'browser-observations-a')?.id
    expect(placementAId).toBeTruthy()
    const placementA = await page.request.get(`/api/workspace/resources/${encodeURIComponent(placementAId!)}`)
    expect(placementA.ok()).toBeTruthy()
    const placementAResolution = await placementA.json() as { canonicalSourceBinding?: { mountId: string; sourceBindingId: string } }
    const canonicalAResponse = await page.request.get(
      `/api/workspace/resources/${encodeURIComponent(placementAId!)}/canonical-dataset`,
    )
    expect(canonicalAResponse.ok()).toBeTruthy()
    const canonicalA = await canonicalAResponse.json() as {
      mountId: string
      sourceBindingId: string
      providerDatasetId: string
      datasetIdentity: string
      readMode: 'exact' | 'current'
      revisionId?: string | null
      columns: Array<{ name: string; type: string }>
    }
    expect(canonicalA).toEqual(expect.objectContaining({
      mountId: 'browser-provider',
      sourceBindingId: placementAResolution.canonicalSourceBinding?.sourceBindingId,
      providerDatasetId: 'browser-canonical-observations',
      datasetIdentity: expect.stringMatching(/^workspace-provider:/),
      readMode: 'exact',
      revisionId: 'browser-provider-revision-v1',
      columns: expect.arrayContaining([
        expect.objectContaining({ name: 'id', type: 'int64' }),
        expect.objectContaining({ name: 'value', type: 'string' }),
      ]),
    }))
    expect(JSON.stringify(canonicalA)).not.toContain('observations.csv')
    expect(JSON.stringify(canonicalA)).not.toContain(resolve(providerRoot!))
    const canonicalDetail = detail.getByTestId('canonical-provider-dataset-context')
    await expect(canonicalDetail).toContainText('Exact revision · browser-provider-revision-v1')
    await expect(canonicalDetail).toContainText('id · int64')
    await expect(canonicalDetail).toContainText('value · string')
    await detail.getByRole('button', { name: 'Use in canvas' }).click()

    const useDialog = page.getByRole('dialog', { name: `Use ${datasetNameA}` })
    await expect(useDialog).toContainText('data and credentials are not copied')
    await expect(useDialog).toContainText('locally owned overlay')
    const writes: string[] = []
    page.on('request', (request) => {
      if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method()) && request.url().includes('/api/')) {
        writes.push(new URL(request.url()).pathname)
      }
    })
    const createRequest = page.waitForRequest((request) =>
      request.url().endsWith('/api/workspace/canvases') && request.method() === 'POST')
    await useDialog.getByRole('button', { name: 'Create and open' }).click()
    const createBody = JSON.parse((await createRequest).postData() ?? '{}') as {
      containerId?: string; expectedContainerVersion?: number; requestId?: string; providerDatasetRefs?: string[]
    }
    expect(createBody).toEqual(expect.objectContaining({
      containerId: localPlacement!.containerId,
      expectedContainerVersion: localPlacement!.containerVersion,
      requestId: expect.any(String), providerDatasetRefs: expect.any(Array),
    }))
    expect(createBody.containerId).not.toBe(externalPage.container.id)
    expect(createBody.containerId).not.toBe('browser-collection-a')
    expect(createBody.providerDatasetRefs).toHaveLength(1)
    expect(writes).toEqual(['/api/workspace/canvases'])
    await expect(page.getByTestId('toolbar')).toBeVisible()
    const canvasLocation = page.getByRole('navigation', { name: 'Canvas Workspace location' })
    await expect(canvasLocation).toContainText(`Workspace/${containerNameA}`)
    const source = page.locator('.react-flow__node').filter({ hasText: datasetNameA })
    await expect(source).toHaveCount(1)
    await expect(source.locator(
      '[title="Pinned provider revision browser-provider-revision-v1"]',
    )).toContainText('browser-prov…ision-v1')
    const canvasId = decodeURIComponent(
      new URL(page.url()).hash.split('/').pop()!.split('?')[0],
    )

    await page.goto('/#/workspace')
    const search = page.getByRole('textbox', { name: 'Search views, datasets, canvases, and containers' })
    await search.fill(datasetNameA)
    await search.press('Enter')
    const searchResults = page.getByTestId('workspace-search-results')
    await expect(searchResults.getByText(`Placement path · ${containerNameA} / ${datasetNameA}`, { exact: true })).toBeVisible()
    await expect(searchResults.getByText(`Placement path · ${containerNameB} / ${datasetNameB}`, { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Clear Workspace search' }).click()
    const duplicateContainer = page.getByRole('button', {
      name: new RegExp(`Open folder ${containerNameB} from Source-only mount browser-provider`),
    })
    await expect(duplicateContainer).toBeVisible({ timeout: 20_000 })
    const duplicateBrowse = page.waitForResponse((response) =>
      response.url().includes('/api/workspace/containers/')
      && response.request().method() === 'GET')
    await duplicateContainer.click()
    const duplicatePage = await (await duplicateBrowse).json() as {
      items: Array<{ id: string; providerPlacementId?: string }>
    }
    const placementBId = duplicatePage.items.find(
      (item) => item.providerPlacementId === 'browser-observations-b',
    )?.id
    expect(placementBId).toBeTruthy()
    const duplicateResource = page.getByRole('button', {
      name: new RegExp(`Open dataset ${datasetNameB} from Source-only mount browser-provider`),
    })
    await expect(duplicateResource).toBeVisible({ timeout: 20_000 })
    await duplicateResource.click()
    const duplicateDetail = page.getByRole('dialog', { name: datasetNameB })
    await expect(duplicateDetail).toContainText(`Also observed at${containerNameA} / ${datasetNameA}`)
    const placementBRequest = page.waitForResponse((response) =>
      decodeURIComponent(new URL(response.url()).pathname.split('/').pop() ?? '') === placementBId
      && response.request().method() === 'GET')
    await page.reload()
    const placementBResponse = await placementBRequest
    const placementBResolution = await placementBResponse.json() as { canonicalSourceBinding?: { mountId: string; sourceBindingId: string } }
    expect(placementBResolution.canonicalSourceBinding).toEqual(placementAResolution.canonicalSourceBinding)
    await expect(page.getByRole('dialog', { name: datasetNameB })).toBeVisible()
    const canonicalBResponse = await page.request.get(
      `/api/workspace/resources/${encodeURIComponent(placementBId!)}/canonical-dataset`,
    )
    expect(canonicalBResponse.ok()).toBeTruthy()
    expect(await canonicalBResponse.json()).toEqual(canonicalA)
    await expect(duplicateDetail.getByTestId('canonical-provider-dataset-context')).toContainText(
      'Exact revision · browser-provider-revision-v1',
    )
    await duplicateDetail.getByRole('button', { name: 'Use in canvas' }).click()
    const duplicateUseDialog = page.getByRole('dialog', { name: `Use ${datasetNameB}` })
    const chooseCanvas = duplicateUseDialog.getByRole('button', { name: /^Choose a Canvas/ })
    await expect(chooseCanvas).toBeEnabled()
    await chooseCanvas.click()
    const targetCanvas = duplicateUseDialog.getByRole('combobox', { name: 'Target canvas' })
    await expect(targetCanvas).toBeEnabled()
    await targetCanvas.selectOption(canvasId)

    const addResponsePromise = page.waitForResponse((response) =>
      new URL(response.url()).pathname === `/api/workspace/canvases/${canvasId}/datasets`
      && response.request().method() === 'POST')
    await duplicateUseDialog.getByRole('button', { name: 'Add and open' }).click()
    const addResponse = await addResponsePromise
    expect(addResponse.ok()).toBeTruthy()
    const addRequestBody = JSON.parse(addResponse.request().postData() ?? '{}') as {
      requestId?: string; providerDatasetRefs?: string[]
    }
    expect(addRequestBody.requestId).toEqual(expect.any(String))
    expect(addRequestBody.providerDatasetRefs).toHaveLength(1)
    expect(await addResponse.json()).toEqual(expect.objectContaining({
      ok: true,
      id: canvasId,
      version: expect.any(Number),
      changed: false,
      alreadyPresent: true,
      addedCount: 0,
    }))
    await expect(page.getByText(
      'This provider dataset is already present in the selected Canvas.',
      { exact: true },
    )).toBeVisible()
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect(canvasLocation).toContainText(`Workspace/${containerNameA}`)
    await expect(source).toHaveCount(1)
    await expect(page.locator('.react-flow__node').filter({ hasText: datasetNameB })).toHaveCount(0)

    await source.getByText('DATASET', { exact: true }).click()
    const inspector = page.getByTestId('inspector')
    await inspector.getByRole('button', { name: 'View data' }).click()
    const dataPanel = page.getByTestId('panel-data')
    await expect(dataPanel.getByText('alpha', { exact: true })).toBeVisible({ timeout: 15_000 })
    await expect(dataPanel.getByText('beta', { exact: true })).toBeVisible()

    const startedResponse = page.waitForResponse((response) =>
      response.url().endsWith('/api/run') && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Rerun all' }).click()
    const started = await startedResponse
    expect(started.ok()).toBeTruthy()
    const runId = (await started.json()).runId as string
    await expect.poll(async () => {
      const response = await page.request.get(`/api/run/${runId}`)
      if (!response.ok()) return 'unavailable'
      return (await response.json()).status
    }, { timeout: 20_000 }).toBe('done')
    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${canvasId}/runs`)
      if (!response.ok()) return false
      return (await response.json()).some((item: { runId?: string }) => item.runId === runId)
    }).toBe(true)

    await page.getByTestId('app-menu').click()
    await page.getByText('Run history', { exact: true }).click()
    const history = page.getByRole('dialog').filter({
      has: page.getByRole('heading', { name: 'Run history' }),
    })
    await expect(history.getByText('2 rows', { exact: true }).first()).toBeVisible()
    await history.getByRole('button', { name: /Admitted inputs/ }).click()
    await expect(history.getByText(/browser-provider-revision-v1/).first()).toBeVisible()
    await history.getByRole('button', { name: /Execution manifest/ }).click()
    await expect(history.getByText(/browser-provider-revision-v1/).first()).toBeVisible()
    await history.getByRole('button', { name: 'Close' }).click()

    const deepLink = await page.request.get(`/api/workspace/resources/canvas:${canvasId}`)
    expect(deepLink.ok()).toBeTruthy()
    const deepLinkBody = await deepLink.json() as {
      resource: { parentId?: string }; ancestors: Array<{ id?: string }>
    }
    expect(deepLinkBody.resource.parentId).toBe(externalPage.container.id)
    expect(deepLinkBody.ancestors.at(-1)?.id).toBe(externalPage.container.id)

    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    await expect(page.getByTestId('toolbar')).toBeVisible()
    await expect(page.locator('.react-flow__node').filter({ hasText: datasetNameA })).toHaveCount(1)

    // The Canvas location came from the stable overlay parent. Returning uses that opaque id and
    // never writes to the provider.
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace', { exact: true }).click()
    await expect(page).toHaveURL(new RegExp(`/\\#/workspace/${encodeURIComponent(externalPage.container.id)}`))
    expect(writes).toEqual(expect.arrayContaining([
      '/api/workspace/canvases',
      `/api/workspace/canvases/${canvasId}/datasets`,
      '/api/run',
    ]))
    expect(writes.every((path) => [
      /^\/api\/workspace\/canvases$/,
      /^\/api\/workspace\/canvases\/[^/]+\/datasets$/,
      /^\/api\/canvas\/[^/]+$/,
      /^\/api\/graph\/(plan|schema|estimate)$/,
      /^\/api\/run(\/preview|\/estimate|\/input-drift)?$/,
    ].some((allowed) => allowed.test(path)))).toBe(true)
    const writesBeforeUnavailableReturn = [...writes]

    let unavailableIntercepted = false
    await page.route((url) => (
      decodeURIComponent(url.pathname.split('/').pop() ?? '') === `canvas:${canvasId}`
    ), (route) => {
      unavailableIntercepted = true
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          resource: { id: `canvas:${canvasId}`, kind: 'canvas', name: 'provider context Canvas', parentId: externalPage.container.id, detached: false, source: 'local' },
          ancestors: [],
          source: { id: 'mount:browser-provider', kind: 'provider', completeness: 'unavailable', referenceState: 'detached', error: 'resource detached' },
        }),
      })
    })
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
    await expect.poll(() => unavailableIntercepted).toBe(true)
    await expect(page.getByText('Its Workspace location is unavailable.', { exact: true })).toBeVisible()
    await page.getByTestId('app-menu').click()
    await page.getByText('Back to Workspace', { exact: true }).click()
    await expect(page).toHaveURL(new RegExp(`/\\#/workspace/${encodeURIComponent(externalPage.container.id)}`))
    expect(writes).toEqual(writesBeforeUnavailableReturn)

    let placementState: 'detached' | 'canonical-offline' = 'detached'
    await page.route((url) => decodeURIComponent(url.pathname.split('/').pop() ?? '') === placementAId, (route) => {
      const detached = placementState === 'detached'
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          resource: {
            id: placementAId, kind: 'dataset', name: datasetNameA, detached,
            source: 'provider', mountId: 'browser-provider', provider: 'dp-file-catalog',
            resourceId: 'browser-observations-a', providerPlacementId: 'browser-observations-a',
            parentProviderPlacementId: 'browser-collection-a', providerDatasetId: 'browser-canonical-observations',
            referenceState: detached ? 'detached' : 'current',
            canonicalReferenceState: detached ? 'current' : 'offline', lastKnown: true,
            providerMutation: false,
          },
          ancestors: [externalPage.container],
          source: { id: 'mount:browser-provider', kind: 'provider', completeness: 'complete' },
        }),
      })
    })
    await page.goto(`/#/workspace/${encodeURIComponent(placementAId!)}`)
    await expect(page.getByRole('dialog', { name: datasetNameA })).toContainText(
      'Placement state · detached · canonical dataset is current',
    )
    placementState = 'canonical-offline'
    await page.goto('/#/workspace')
    await page.goto(`/#/workspace/${encodeURIComponent(placementAId!)}`)
    const canonicalUnavailable = page.getByRole('dialog', { name: datasetNameA })
    await expect(canonicalUnavailable).toContainText('Canonical dataset state · offline')
    await expect(canonicalUnavailable).toContainText('Placement state · current')
    await expect(canonicalUnavailable.getByRole('button', { name: 'Use in canvas' })).toBeDisabled()
    expect(writes).toEqual(writesBeforeUnavailableReturn)
    expect(readFileSync(resolve(providerRoot!, 'catalog.json'))).toEqual(providerCatalogBefore)
    expect(readFileSync(resolve(providerRoot!, 'observations.csv'))).toEqual(providerDatasetBefore)
  })
})
