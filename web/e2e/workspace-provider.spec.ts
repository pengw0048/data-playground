import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test } from '@playwright/test'

const enabled = process.env.DP_E2E_PROVIDER_ACCEPTANCE === '1'
const providerRoot = process.env.DP_E2E_PROVIDER_ROOT
const datasetName = 'Browser provider observations'

test.describe('provider Workspace Source acceptance', () => {
  test.skip(!enabled || !providerRoot, 'set DP_E2E_PROVIDER_ACCEPTANCE=1 and DP_E2E_PROVIDER_ROOT')

  test.beforeAll(() => {
    const root = resolve(providerRoot!)
    mkdirSync(root, { recursive: true })
    writeFileSync(resolve(root, 'observations.csv'), 'id,value\n1,alpha\n2,beta\n')
    writeFileSync(resolve(root, 'catalog.json'), JSON.stringify({ resources: [{
      id: 'browser-observations', kind: 'dataset', name: datasetName,
      uri: 'observations.csv', revisionId: 'browser-provider-revision-v1',
      columns: [{ name: 'id', type: 'int64' }, { name: 'value', type: 'string' }],
    }] }))
  })

  test('creates a local Canvas overlay, then uses, previews, runs, and inspects an exact provider Source in Chromium', async ({ page }) => {
    test.setTimeout(60_000)
    await page.goto('/#/workspace')
    const resource = page.getByRole('button', {
      name: new RegExp(`Open dataset ${datasetName} from Source-only mount browser-provider`),
    })
    await expect(resource).toBeVisible({ timeout: 20_000 })
    await resource.click()
    const detail = page.getByRole('dialog', { name: datasetName })
    await expect(detail).toContainText('Source-only mount browser-provider · dp-file-catalog')
    await detail.getByRole('button', { name: 'Use in canvas' }).click()

    const useDialog = page.getByRole('dialog', { name: `Use ${datasetName}` })
    await expect(useDialog).toContainText('data and credentials are not copied')
    await expect(useDialog).toContainText('locally owned overlay')
    const createRequest = page.waitForRequest((request) =>
      request.url().endsWith('/api/workspace/canvases') && request.method() === 'POST')
    await useDialog.getByRole('button', { name: 'Create and open' }).click()
    const createBody = JSON.parse((await createRequest).postData() ?? '{}') as {
      containerId?: string; requestId?: string; providerDatasetRefs?: string[]
    }
    expect(createBody.requestId).toEqual(expect.any(String))
    expect(createBody.containerId).not.toBe('browser-observations')
    expect(createBody.providerDatasetRefs).toHaveLength(1)
    await expect(page.getByTestId('toolbar')).toBeVisible()
    const source = page.locator('.react-flow__node').filter({ hasText: datasetName })
    await expect(source).toHaveCount(1)
    await expect(source.locator(
      '[title="Pinned provider revision browser-provider-revision-v1"]',
    )).toContainText('browser-prov…ision-v1')

    await source.getByText('DATASET', { exact: true }).click()
    const inspector = page.getByTestId('inspector')
    await inspector.getByRole('button', { name: 'View data' }).click()
    const dataPanel = page.getByTestId('panel-data')
    await expect(dataPanel.getByText('alpha', { exact: true })).toBeVisible({ timeout: 15_000 })
    await expect(dataPanel.getByText('beta', { exact: true })).toBeVisible()

    const canvasId = decodeURIComponent(
      new URL(page.url()).hash.split('/').pop()!.split('?')[0],
    )
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
  })
})
