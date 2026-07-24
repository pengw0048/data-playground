import { unlinkSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const fullProfile = process.env.DP_E2E_FIXTURE_PROFILE === 'full'

type Table = {
  id: string
  registrationId: string
  name: string
  uri: string
  version?: string | null
  columns: Array<{ name: string }>
}

async function catalogTable(request: APIRequestContext, query: string): Promise<Table> {
  const response = await request.get('/api/catalog/search', { params: { q: query, mode: 'lexical', limit: 10 } })
  expect(response.ok()).toBeTruthy()
  const tables = await response.json() as Table[]
  const table = tables.find((item) => item.name === query) ?? tables[0]
  if (!table) throw new Error(`No catalog table matched ${query}`)
  return table
}

async function seedSourceCanvas(page: Page, canvasId: string, table: Table) {
  const response = await page.request.post('/api/canvas', { data: {
    id: canvasId,
    name: 'Join with related E2E',
    version: 1,
    nodes: [{
      id: 'selected-source',
      type: 'source',
      position: { x: 120, y: 180 },
      data: {
        title: table.name,
        status: 'draft',
        history: [],
        config: { uri: table.uri, tableId: table.id, registrationId: table.registrationId },
      },
    }],
    edges: [],
  } })
  expect(response.ok()).toBeTruthy()
  await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
  await expect(page.locator('.react-flow__node')).toHaveCount(1)
  await page.locator('.react-flow__node').getByText('DATASET', { exact: true }).click()
  await expect(page.getByTestId('join-with-related-selected-source')).toBeVisible()
}

async function unregisterTable(request: APIRequestContext, table: { id: string, registrationId?: string, metadataRevision?: string }) {
  if (!table.registrationId || !table.metadataRevision) return
  await request.delete(`/api/catalog/tables/${encodeURIComponent(table.id)}`, { params: {
    expected_registration_id: table.registrationId,
    expected_revision: table.metadataRevision,
  } })
}

test.describe('Join with related data', () => {
  test('declared review cancellation leaves the Canvas untouched', async ({ page }) => {
    test.setTimeout(45_000)
    const left = await catalogTable(page.request, 'events')
    const right = await catalogTable(page.request, 'movies')
    const relation = {
      leftUri: left.uri,
      leftColumns: [left.columns[0]?.name ?? 'id'],
      rightUri: right.uri,
      rightColumns: [right.columns[0]?.name ?? 'id'],
      cardinality: '1:1',
      confidence: 'declared',
    }
    const declared = await page.request.post('/api/catalog/relationships', { data: relation })
    expect(declared.ok()).toBeTruthy()
    const canvasId = `join-related-${Date.now()}`
    try {
      await seedSourceCanvas(page, canvasId, left)
      await page.getByTestId('join-with-related-selected-source').click()
      await expect(page.getByText('Declared and proven references')).toBeVisible()
      await page.getByRole('button', { name: new RegExp(right.name, 'i') }).click()
      await expect(page.getByText('Selected dataset')).toBeVisible()
      await expect(page.getByText('Related dataset', { exact: true })).toBeVisible()
      await expect(page.getByText('Declared relationship')).toBeVisible()
      await page.getByRole('button', { name: 'Cancel', exact: true }).click()

      const cancelled = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      expect(cancelled.version).toBe(1)
      expect(cancelled.nodes).toHaveLength(1)
      expect(cancelled.edges).toHaveLength(0)
    } finally {
      await page.request.post('/api/catalog/relationships/delete', { data: relation })
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

  test('declared review creates one exact coherent graph edit', async ({ page }) => {
    test.setTimeout(45_000)
    const left = await catalogTable(page.request, 'events')
    const right = await catalogTable(page.request, 'movies')
    const relation = {
      leftUri: left.uri,
      leftColumns: [left.columns[0]?.name ?? 'id'],
      rightUri: right.uri,
      rightColumns: [right.columns[0]?.name ?? 'id'],
      cardinality: '1:1',
      confidence: 'declared',
    }
    const declared = await page.request.post('/api/catalog/relationships', { data: relation })
    expect(declared.ok()).toBeTruthy()
    const canvasId = `join-related-confirm-${Date.now()}`
    try {
      await seedSourceCanvas(page, canvasId, left)
      await page.getByTestId('join-with-related-selected-source').click()
      await expect(page.getByText('Declared and proven references')).toBeVisible()
      await page.getByRole('button', { name: new RegExp(right.name, 'i') }).click()
      await page.getByLabel('Join type').selectOption('left')
      await page.getByTestId('confirm-related-join').click()
      await expect(page.locator('.react-flow__node')).toHaveCount(3)

      const saved = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      expect(saved.version).toBe(2)
      expect(saved.nodes).toHaveLength(3)
      expect(saved.edges).toHaveLength(2)
      const source = saved.nodes.find((node: any) => node.id !== 'selected-source' && node.type === 'source')
      const join = saved.nodes.find((node: any) => node.type === 'join')
      expect(source.data.config).toMatchObject({ uri: right.uri, tableId: right.id, registrationId: right.registrationId })
      expect(join.data.config.how).toBe('left')
      expect(saved.edges.map((edge: any) => edge.target)).toEqual([join.id, join.id])
    } finally {
      await page.request.post('/api/catalog/relationships/delete', { data: relation })
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

  test('a real empty scoped search remains non-mutating', async ({ page }) => {
    const left = await catalogTable(page.request, 'events')
    const canvasId = `join-related-empty-${Date.now()}`
    try {
      await seedSourceCanvas(page, canvasId, left)
      await page.getByTestId('join-with-related-selected-source').click()
      const search = page.getByPlaceholder('Dataset, column, tag…')
      await search.fill(`definitely-no-related-${Date.now()}`)
      await expect(page.getByTestId('related-no-results')).toBeVisible()
      const unchanged = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      expect(unchanged.version).toBe(1)
      expect(unchanged.nodes).toHaveLength(1)
      expect(unchanged.edges).toHaveLength(0)
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

  test('a real local inferred candidate is reviewable and cancellation stays non-mutating', async ({ page }) => {
    const token = `join-inferred-${Date.now()}`
    const dataRoot = resolve(process.cwd(), '.e2e-workspace', 'data')
    const sourcePath = resolve(dataRoot, `${token}-source.csv`)
    const targetPath = resolve(dataRoot, `${token}-target.csv`)
    const canvasId = `${token}-canvas`
    const registered: Array<Table & { metadataRevision?: string }> = []
    writeFileSync(sourcePath, 'id,value\n1,source\n')
    writeFileSync(targetPath, 'id,label\n1,target\n')
    try {
      const sourceResponse = await page.request.post('/api/catalog/register', { data: {
        uri: sourcePath, name: `${token}-source`,
      } })
      expect(sourceResponse.ok()).toBeTruthy()
      const source = await sourceResponse.json() as Table & { metadataRevision?: string }
      registered.push(source)
      const targetResponse = await page.request.post('/api/catalog/register', { data: {
        uri: targetPath, name: `${token}-target`,
      } })
      expect(targetResponse.ok()).toBeTruthy()
      const target = await targetResponse.json() as Table & { metadataRevision?: string }
      registered.push(target)

      await seedSourceCanvas(page, canvasId, source)
      await page.getByTestId('join-with-related-selected-source').click()
      await page.getByPlaceholder('Dataset, column, tag…').fill(target.name)
      await expect(page.getByText('Inferred candidates')).toBeVisible()
      await page.getByRole('button', { name: new RegExp(target.name, 'i') }).click()
      await expect(page.getByText('Related dataset', { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Cancel', exact: true }).click()
      const unchanged = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      expect(unchanged.version).toBe(1)
      expect(unchanged.nodes).toHaveLength(1)
      expect(unchanged.edges).toHaveLength(0)
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
      await Promise.all(registered.map((table) => unregisterTable(page.request, table)))
      for (const path of [sourcePath, targetPath]) {
        try { unlinkSync(path) } catch { /* source files are disposable test fixtures */ }
      }
    }
  })

  test('a real bounded candidate page asks for refinement, then search and folder scopes converge', async ({ page }) => {
    test.skip(!fullProfile, 'large-catalog refinement acceptance runs with the scheduled full fixture profile')
    test.setTimeout(90_000)
    const token = `related-bounded-${Date.now()}`
    const dataRoot = resolve(process.cwd(), '.e2e-workspace', 'data')
    const sourcePath = resolve(dataRoot, `${token}-source.csv`)
    const focusedPath = resolve(dataRoot, `${token}-focused.csv`)
    const canvasId = `${token}-canvas`
    const registered: Array<Table & { metadataRevision?: string }> = []
    const paths = [sourcePath, focusedPath]
    writeFileSync(sourcePath, 'id\n1\n')
    writeFileSync(focusedPath, 'id\n1\n')
    try {
      const sourceResponse = await page.request.post('/api/catalog/register', { data: {
        uri: sourcePath, name: `${token}-source`, folder: `${token}/source`,
      } })
      expect(sourceResponse.ok()).toBeTruthy()
      const source = await sourceResponse.json() as Table & { metadataRevision?: string }
      registered.push(source)
      const focusedResponse = await page.request.post('/api/catalog/register', { data: {
        uri: focusedPath, name: `${token}-focused`, folder: `${token}/focused`,
      } })
      expect(focusedResponse.ok()).toBeTruthy()
      registered.push(await focusedResponse.json() as Table & { metadataRevision?: string })
      await seedSourceCanvas(page, canvasId, source)
      await page.getByTestId('join-with-related-selected-source').click()
      const truncation = page.getByText('Results are truncated to a bounded working set. Refine search or folder to inspect omitted datasets.')
      await expect(truncation).toBeVisible()

      const search = page.getByPlaceholder('Dataset, column, tag…')
      await search.fill(`${token}-focused`)
      await expect(page.getByRole('button', { name: new RegExp(`${token}-focused`, 'i') })).toBeVisible()
      await expect(truncation).toBeHidden()

      await search.fill('')
      const folder = page.getByPlaceholder('Optional folder subtree')
      await folder.fill(`${token}/focused`)
      await expect(page.getByRole('button', { name: new RegExp(`${token}-focused`, 'i') })).toBeVisible()
      await expect(truncation).toBeHidden()
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
      await Promise.all(registered.map((table) => unregisterTable(page.request, table)))
      for (const path of paths) {
        try { unlinkSync(path) } catch { /* cleanup is best effort after the server owns no registration */ }
      }
    }
  })

  test('a real stale Canvas keeps the reviewed candidate visible and offers reapply', async ({ page }) => {
    const left = await catalogTable(page.request, 'events')
    const right = await catalogTable(page.request, 'movies')
    const relation = {
      leftUri: left.uri, leftColumns: [left.columns[0]?.name ?? 'id'],
      rightUri: right.uri, rightColumns: [right.columns[0]?.name ?? 'id'], cardinality: '1:1',
    }
    expect((await page.request.post('/api/catalog/relationships', { data: relation })).ok()).toBeTruthy()
    const canvasId = `join-related-stale-${Date.now()}`
    try {
      await seedSourceCanvas(page, canvasId, left)
      await page.getByTestId('join-with-related-selected-source').click()
      await page.getByRole('button', { name: new RegExp(right.name, 'i') }).click()
      const current = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      const advanced = await page.request.put(`/api/canvas/${encodeURIComponent(canvasId)}?expectedVersion=1`, {
        data: { ...current, name: `${current.name} advanced` },
      })
      expect(advanced.ok()).toBeTruthy()
      await page.getByTestId('confirm-related-join').click()
      await expect(page.getByRole('button', { name: 'Reapply to latest Canvas' })).toBeVisible()
      await expect(page.getByText('Related dataset', { exact: true })).toBeVisible()
    } finally {
      await page.request.post('/api/catalog/relationships/delete', { data: relation })
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })
})
