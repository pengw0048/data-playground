import { writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

type Table = { id: string; registrationId: string; metadataRevision?: string; uri: string }

async function register(request: APIRequestContext, uri: string, name: string): Promise<Table> {
  const response = await request.post('/api/catalog/register', { data: { uri, name } })
  expect(response.ok()).toBeTruthy()
  return response.json() as Promise<Table>
}

async function saved(page: Page, canvasId: string): Promise<any> {
  const response = await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
  expect(response.ok()).toBeTruthy()
  return response.json()
}

async function unregister(request: APIRequestContext, table: Table) {
  if (!table.metadataRevision) return
  await request.delete(`/api/catalog/tables/${encodeURIComponent(table.id)}`, { params: {
    expected_registration_id: table.registrationId,
    expected_revision: table.metadataRevision,
  } })
}

test.describe('Join key builder', () => {
  test('reopens an unconfigured Join quietly while rerun all still refuses it', async ({ page }) => {
    const canvasId = `join-missing-condition-${Date.now()}`
    try {
      const created = await page.request.post('/api/canvas', { data: {
        id: canvasId, name: 'Join missing condition', version: 1,
        nodes: [
          { id: 'left', type: 'source', position: { x: 50, y: 100 }, data: { title: 'left', status: 'draft', history: [], config: { uri: 'events' } } },
          { id: 'right', type: 'source', position: { x: 50, y: 360 }, data: { title: 'right', status: 'draft', history: [], config: { uri: 'events' } } },
          { id: 'join', type: 'join', position: { x: 460, y: 220 }, data: { title: 'join', status: 'draft', history: [], config: { how: 'inner', on: '', condition: '' } } },
        ],
        edges: [
          { id: 'left-a', source: 'left', target: 'join', targetHandle: 'a', data: { wire: 'dataset' } },
          { id: 'right-b', source: 'right', target: 'join', targetHandle: 'b', data: { wire: 'dataset' } },
        ],
      } })
      expect(created.ok()).toBeTruthy()

      const graphEstimate = () => page.waitForResponse((response) => (
        new URL(response.url()).pathname === '/api/graph/estimate'
        && response.request().method() === 'POST'
        && response.request().postData()?.includes(canvasId) === true
      ))
      const firstEstimate = graphEstimate()
      await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
      const firstEstimateResponse = await firstEstimate
      expect(firstEstimateResponse.ok(), await firstEstimateResponse.text()).toBeTruthy()
      await expect(page.getByTestId('join-missing-condition')).toHaveText('Choose at least one left and right column.')
      const rawRefusal = page.getByTestId('toast').filter({
        hasText: 'needs at least one left and right column or an advanced condition',
      })
      await expect(rawRefusal).toHaveCount(0)

      const reloadEstimate = graphEstimate()
      await page.reload()
      const reloadEstimateResponse = await reloadEstimate
      expect(reloadEstimateResponse.ok(), await reloadEstimateResponse.text()).toBeTruthy()
      await expect(page.getByTestId('join-missing-condition')).toHaveText('Choose at least one left and right column.')
      await expect(rawRefusal).toHaveCount(0)

      const dispatched = page.waitForRequest((request) => {
        const path = new URL(request.url()).pathname
        return request.method() === 'POST' && (path === '/api/run' || path === '/api/run/estimate')
      }, { timeout: 750 }).then(() => true).catch(() => false)
      const feedback = page.getByTestId('toast').filter({ hasText: 'Choose at least one left and right column.' })
      await expect(feedback).toHaveCount(0)
      await page.getByRole('button', { name: 'Run all' }).click()
      await expect(feedback).toHaveCount(1)
      expect(await dispatched).toBe(false)
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

  test('uses the real a/b schemas for same, different, multi-key, advanced, and rewired joins', async ({ page }, testInfo) => {
    test.setTimeout(45_000)
    const token = `join-key-builder-${Date.now()}-${testInfo.workerIndex}-${testInfo.repeatEachIndex}`
    const dataRoot = resolve(process.cwd(), '.e2e-workspace', 'data')
    const leftPath = resolve(dataRoot, `${token}-left.csv`)
    const rightPath = resolve(dataRoot, `${token}-right.csv`)
    const replacementPath = resolve(dataRoot, `${token}-replacement.csv`)
    writeFileSync(leftPath, '_rowid,shared,left_region\nleft-1,shared-1,north\n')
    writeFileSync(rightPath, 'original_row_id,shared,right_region\nright-1,shared-1,north\n')
    writeFileSync(replacementPath, 'replacement_id\nreplacement-1\n')
    const [left, right, replacement] = await Promise.all([
      register(page.request, leftPath, `${token}-left`),
      register(page.request, rightPath, `${token}-right`),
      register(page.request, replacementPath, `${token}-replacement`),
    ])
    const canvasId = `${token}-canvas`
    try {
      const created = await page.request.post('/api/canvas', { data: {
        id: canvasId, name: 'Join key builder E2E', version: 1,
        nodes: [
          { id: 'left', type: 'source', position: { x: 50, y: 100 }, data: { title: 'left', status: 'draft', history: [], config: left } },
          { id: 'right', type: 'source', position: { x: 50, y: 360 }, data: { title: 'right', status: 'draft', history: [], config: right } },
          { id: 'replacement', type: 'source', position: { x: 50, y: 620 }, data: { title: 'replacement', status: 'draft', history: [], config: replacement } },
          { id: 'join', type: 'join', position: { x: 460, y: 220 }, data: { title: 'join', status: 'draft', history: [], config: { how: 'inner', on: 'shared', condition: '' } } },
        ],
        edges: [
          { id: 'left-a', source: 'left', target: 'join', targetHandle: 'a', data: { wire: 'dataset' } },
          { id: 'right-b', source: 'right', target: 'join', targetHandle: 'b', data: { wire: 'dataset' } },
        ],
      } })
      expect(created.ok()).toBeTruthy()
      await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
      await expect(page.getByLabel('Left key 1')).toHaveValue('shared')
      await expect(page.getByLabel('Right key 1')).toHaveValue('shared')
      await expect(page.getByLabel('Left key 1')).not.toHaveText(/original_row_id/)
      await expect(page.getByLabel('Right key 1')).not.toHaveText(/_rowid/)
      await page.locator('.react-flow__node[data-id="join"]').click({ position: { x: 35, y: 25 } })
      const inspector = page.getByTestId('inspector')
      await expect(inspector.getByText('Join configuration')).toBeVisible()
      await expect(inspector.getByText('a.shared = b.shared')).toBeVisible()
      await expect(inspector.getByText('shared key(s)')).toHaveCount(0)
      await expect(inspector.getByText(/ON expression/)).toHaveCount(0)
      await expect(inspector.getByRole('button', { name: 'Edit keys on Join card' })).toBeVisible()

      await page.getByRole('button', { name: 'Advanced condition' }).click()
      await expect(page.getByLabel('advanced ON condition')).toHaveValue('a.shared = b.shared')
      await page.getByLabel('advanced ON condition').fill('')
      await expect.poll(async () => (await saved(page, canvasId)).nodes.find((node: any) => node.id === 'join').data.config)
        .toMatchObject({ on: '', condition: '' })
      await page.getByLabel('advanced ON condition').fill('a.shared = b.shared OR a.left_region = b.right_region')
      await expect.poll(async () => (await saved(page, canvasId)).nodes.find((node: any) => node.id === 'join').data.config)
        .toMatchObject({ on: '', condition: 'a.shared = b.shared OR a.left_region = b.right_region' })
      await page.getByLabel('advanced ON condition').fill('a.shared = b.shared')
      await expect(page.getByLabel('advanced ON condition')).toHaveValue('a.shared = b.shared')
      await page.getByRole('button', { name: 'Use key builder' }).click()
      await expect(page.getByLabel('Left key 1')).toHaveValue('shared')
      await expect(page.getByLabel('Right key 1')).toHaveValue('shared')

      await page.getByLabel('Join type').selectOption('left')
      await page.getByLabel('Left key 1').selectOption('_rowid')
      await page.getByLabel('Right key 1').selectOption('original_row_id')
      await page.getByRole('button', { name: 'Add key pair' }).click()
      await page.getByLabel('Left key 2').selectOption('left_region')
      await page.getByLabel('Right key 2').selectOption('right_region')
      await expect.poll(async () => (await saved(page, canvasId)).nodes.find((node: any) => node.id === 'join').data.config)
        .toMatchObject({ how: 'left', on: '', condition: 'a._rowid = b.original_row_id AND a.left_region = b.right_region' })

      await expect(page.getByTestId('autosave')).toContainText('saved')
      const advanced = await saved(page, canvasId)
      advanced.nodes.find((node: any) => node.id === 'join').data.config = {
        how: 'left', on: '', condition: 'a._rowid = b.original_row_id OR a.left_region = b.right_region',
      }
      const advancedPut = await page.request.put(`/api/canvas/${encodeURIComponent(canvasId)}?expectedVersion=${advanced.version}`, { data: advanced })
      expect(advancedPut.ok()).toBeTruthy()
      await page.reload()
      await expect(page.getByLabel('advanced ON condition')).toHaveValue('a._rowid = b.original_row_id OR a.left_region = b.right_region')

      await expect(page.getByTestId('autosave')).toContainText('saved')
      const rewired = await saved(page, canvasId)
      rewired.nodes.find((node: any) => node.id === 'join').data.config = { how: 'left', on: '', condition: 'a.replacement_id = b.original_row_id' }
      rewired.edges.find((edge: any) => edge.id === 'left-a').source = 'replacement'
      const rewiredPut = await page.request.put(`/api/canvas/${encodeURIComponent(canvasId)}?expectedVersion=${rewired.version}`, { data: rewired })
      expect(rewiredPut.ok()).toBeTruthy()
      await page.reload()
      await expect(page.getByLabel('Left key 1')).toHaveValue('replacement_id')
      await expect(page.getByLabel('Left key 1')).not.toHaveText(/_rowid/)
      await expect(page.getByLabel('Right key 1')).toHaveValue('original_row_id')
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
      await Promise.all([left, right, replacement].map((table) => unregister(page.request, table)))
    }
  })
})
