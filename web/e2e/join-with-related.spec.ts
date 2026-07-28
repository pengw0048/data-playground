import { unlinkSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const fullProfile = process.env.DP_E2E_FIXTURE_PROFILE === 'full'
const dialogName = 'Find related data or possible key matches'
const leftInputTrigger = 'Related / possible key matches · left'

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

async function seedSourceCanvas(
  page: Page,
  canvasId: string,
  table: Table,
  includeBystander = false,
) {
  const sourceConfig = {
    uri: table.uri, tableId: table.id, registrationId: table.registrationId,
  }
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
        config: sourceConfig,
      },
    }, ...(includeBystander ? [{
      id: 'bystander-source',
      type: 'source',
      position: { x: 520, y: 180 },
      data: {
        title: `${table.name} copy`,
        status: 'draft',
        history: [],
        config: sourceConfig,
      },
    }] : [])],
    edges: [],
  } })
  expect(response.ok()).toBeTruthy()
  await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
  await expect(page.locator('.react-flow__node')).toHaveCount(includeBystander ? 2 : 1)
  await expect(page.getByTestId('join-with-related-selected-source')).toHaveCount(0)
  await expect(page.getByTestId('join-with-related-canvas-selected-source')).toBeVisible()
}

async function seedOneSidedJoinCanvas(page: Page, canvasId: string, table: Table) {
  const response = await page.request.post('/api/canvas', { data: {
    id: canvasId,
    name: 'One-sided related Join E2E',
    version: 1,
    nodes: [{
      id: 'selected-source',
      type: 'source',
      position: { x: 80, y: 180 },
      data: {
        title: table.name,
        status: 'draft',
        history: [],
        config: { uri: table.uri, tableId: table.id, registrationId: table.registrationId },
      },
    }, {
      id: 'empty-join',
      type: 'join',
      position: { x: 420, y: 180 },
      data: { title: 'join', status: 'draft', history: [], config: { how: 'inner', on: '' } },
    }],
    edges: [{
      id: 'source-to-right',
      source: 'selected-source',
      target: 'empty-join',
      sourceHandle: 'out',
      targetHandle: 'b',
      data: { wire: 'dataset' },
    }],
  } })
  expect(response.ok()).toBeTruthy()
  await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
  await expect(page.locator('.react-flow__node')).toHaveCount(2)
  await expect(page.getByTestId('join-with-related-empty-join')).toHaveCount(0)
  await expect(page.getByTestId('join-with-related-canvas-empty-join'))
    .toHaveAccessibleName(leftInputTrigger)
}

async function unregisterTable(request: APIRequestContext, table: { id: string, registrationId?: string, metadataRevision?: string }) {
  if (!table.registrationId || !table.metadataRevision) return
  await request.delete(`/api/catalog/tables/${encodeURIComponent(table.id)}`, { params: {
    expected_registration_id: table.registrationId,
    expected_revision: table.metadataRevision,
  } })
}

test.describe('Related data and possible key matches', () => {
  test('an open related-data dialog blocks the Canvas Delete shortcut', async ({ page }) => {
    const source = await catalogTable(page.request, 'events')
    const canvasId = `join-related-modal-${Date.now()}`
    try {
      await seedSourceCanvas(page, canvasId, source)
      await page.locator('.react-flow__node[data-id="selected-source"]').click()
      await expect(page.getByTestId('join-with-related-selected-source')).toBeVisible()
      await page.getByTestId('join-with-related-canvas-selected-source').click()
      const dialog = page.getByRole('dialog', { name: dialogName })
      await expect(dialog).toBeVisible()
      await dialog.getByRole('button', { name: 'Cancel', exact: true }).focus()
      await page.keyboard.press('Delete')

      await expect(dialog).toBeVisible()
      await expect(page.locator('.react-flow__node[data-id="selected-source"]')).toBeVisible()
      const unchanged = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      expect(unchanged.version).toBe(1)
      expect(unchanged.nodes).toHaveLength(1)
      await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

  test('modal interactions preserve the Canvas selection behind the portal at 1280x720', async ({ page }) => {
    const source = await catalogTable(page.request, 'events')
    const canvasId = `join-related-selection-${Date.now()}`
    try {
      await seedSourceCanvas(page, canvasId, source, true)
      const selectedSource = page.locator('.react-flow__node[data-id="selected-source"]')
      const bystander = page.locator('.react-flow__node[data-id="bystander-source"]')
      await bystander.click()
      await expect(bystander).toHaveClass(/selected/)
      await expect(selectedSource).not.toHaveClass(/selected/)

      await page.getByTestId('join-with-related-canvas-selected-source').click()
      const dialog = page.getByRole('dialog', { name: dialogName })
      await expect(dialog).toBeVisible()
      await expect(bystander).toHaveClass(/selected/)
      await expect(selectedSource).not.toHaveClass(/selected/)

      await dialog.getByPlaceholder('Dataset, column, tag…').fill('images')
      await expect(dialog.getByPlaceholder('Dataset, column, tag…')).toHaveValue('images')
      await expect(bystander).toHaveClass(/selected/)
      await expect(selectedSource).not.toHaveClass(/selected/)

      await page.locator('.dp-modal-overlay').click({ position: { x: 2, y: 2 } })
      await expect(dialog).toBeHidden()
      await expect(bystander).toHaveClass(/selected/)
      await expect(selectedSource).not.toHaveClass(/selected/)
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

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
      await page.getByTestId('join-with-related-canvas-selected-source').click()
      const dialog = page.getByRole('dialog', { name: dialogName })
      await expect(dialog).toContainText('declared or reference-backed relationships and possible key matches')
      await expect(page.getByRole('heading', { name: 'Related data', exact: true })).toBeVisible()
      const declaredCard = page.getByRole('button', { name: new RegExp(right.name, 'i') })
      await expect(declaredCard).toContainText('Persisted catalog relationship')
      await expect(declaredCard).not.toContainText('No relationship is declared')
      await declaredCard.click()
      await expect(page.getByText('Selected dataset')).toBeVisible()
      await expect(page.getByText('Related dataset', { exact: true })).toBeVisible()
      await expect(page.getByText('Persisted catalog relationship')).toBeVisible()
      await expect(page.getByTestId('possible-key-match-review')).toHaveCount(0)
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

  test('typed-reference candidates retain the strong related-data treatment in the browser', async ({ page }) => {
    const source = await catalogTable(page.request, 'events')
    const target = await catalogTable(page.request, 'images')
    const canvasId = `join-reference-${Date.now()}`
    await page.route('**/api/catalog/related-datasets', async (route) => {
      const response = await route.fetch()
      const body = await response.json() as {
        candidates?: Array<Record<string, unknown>>
        possibleMatches?: Array<Record<string, unknown>>
      }
      const candidates = [...(body.candidates ?? []), ...(body.possibleMatches ?? [])]
      const seed = candidates.find((item) => item.name === target.name)
      if (!seed) {
        await route.fulfill({ response })
        return
      }
      await route.fulfill({ response, json: {
        ...body,
        candidates: [{
          ...seed,
          evidence: 'typed_reference',
          evidenceStatus: 'proven',
          confidence: 'verified',
          reason: `events.id has a typed reference to ${target.name}`,
        }],
        possibleMatches: [],
      } })
    })
    try {
      await seedSourceCanvas(page, canvasId, source)
      await page.getByTestId('join-with-related-canvas-selected-source').click()
      await page.getByPlaceholder('Dataset, column, tag…').fill(target.name)
      const referenceCard = page.getByRole('button', { name: new RegExp(target.name, 'i') })
      await expect(referenceCard).toContainText('Declared key/reference')
      await expect(referenceCard).not.toContainText('No relationship is declared')
      await expect(page.getByRole('heading', { name: 'Related data', exact: true })).toBeVisible()
      await referenceCard.click()
      await expect(page.getByText('Related dataset', { exact: true })).toBeVisible()
      await expect(page.getByText('Declared key/reference')).toBeVisible()
      await expect(page.getByTestId('possible-key-match-review')).toHaveCount(0)
      await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    } finally {
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
      await page.getByTestId('join-with-related-canvas-selected-source').click()
      await expect(page.getByRole('heading', { name: 'Related data', exact: true })).toBeVisible()
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

  test('fills a one-sided Join from the Canvas and exposes different-name keys in the builder', async ({ page }) => {
    test.setTimeout(45_000)
    const token = `join-related-keys-${Date.now()}`
    const dataRoot = resolve(process.cwd(), '.e2e-workspace', 'data')
    const sourcePath = resolve(dataRoot, `${token}-source.csv`)
    const targetPath = resolve(dataRoot, `${token}-target.csv`)
    const canvasId = `${token}-canvas`
    const registered: Array<Table & { metadataRevision?: string }> = []
    const relation = {
      leftUri: '',
      leftColumns: ['user_id'],
      rightUri: '',
      rightColumns: ['id'],
      cardinality: '1:N',
      confidence: 'declared',
    }
    writeFileSync(sourcePath, 'user_id,value\n1,source\n2,source-2\n')
    writeFileSync(targetPath, 'id,label\n1,target\n2,target-2\n')
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
      relation.leftUri = source.uri
      relation.rightUri = target.uri
      expect((await page.request.post('/api/catalog/relationships', { data: relation })).ok()).toBeTruthy()

      await seedOneSidedJoinCanvas(page, canvasId, source)
      await page.getByTestId('join-with-related-canvas-empty-join').click()
      await expect(page.getByTestId('join-with-related-empty-join')).toHaveCount(0)
      await page.getByPlaceholder('Dataset, column, tag…').fill(target.name)
      await page.getByRole('button', { name: new RegExp(target.name, 'i') }).click()
      const review = page.getByRole('dialog', { name: dialogName })
      await expect(review.getByText('a.id = b.user_id')).toBeVisible()
      await expect(review.getByText('N:1', { exact: true })).toBeVisible()
      await review.getByLabel('Join type').selectOption('left')
      await expect(review.getByTestId('related-join-behavior'))
        .toHaveText(`Keeps every row from left input (a): ${target.name}.`)
      await review.getByLabel('Join type').selectOption('right')
      await expect(review.getByTestId('related-join-behavior'))
        .toHaveText(`Keeps every row from right input (b): ${source.name}.`)
      await review.getByLabel('Join type').selectOption('inner')
      await page.getByTestId('confirm-related-join').click()

      await expect(page.locator('.react-flow__node')).toHaveCount(3)
      await expect(page.getByLabel('Left key 1')).toHaveValue('id')
      await expect(page.getByLabel('Right key 1')).toHaveValue('user_id')
      const saved = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      const join = saved.nodes.find((node: any) => node.id === 'empty-join')
      expect(saved.version).toBe(2)
      expect(saved.edges).toHaveLength(2)
      expect(join.data.config).toMatchObject({ on: '', condition: 'a."id" = b."user_id"' })
    } finally {
      if (relation.leftUri && relation.rightUri) {
        await page.request.post('/api/catalog/relationships/delete', { data: relation })
      }
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
      await Promise.all(registered.map((table) => unregisterTable(page.request, table)))
      for (const path of [sourcePath, targetPath]) {
        try { unlinkSync(path) } catch { /* source files are disposable test fixtures */ }
      }
    }
  })

  test('a real empty scoped search remains non-mutating', async ({ page }) => {
    const left = await catalogTable(page.request, 'events')
    const canvasId = `join-related-empty-${Date.now()}`
    try {
      await seedSourceCanvas(page, canvasId, left)
      await page.getByTestId('join-with-related-canvas-selected-source').click()
      const search = page.getByPlaceholder('Dataset, column, tag…')
      await search.fill(`definitely-no-related-${Date.now()}`)
      await expect(page.getByTestId('related-no-results'))
        .toHaveText('No related data or possible key matches in this search/folder scope.')
      const unchanged = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      expect(unchanged.version).toBe(1)
      expect(unchanged.nodes).toHaveLength(1)
      expect(unchanged.edges).toHaveLength(0)
    } finally {
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })

  test('seeded name-only IDs are possible matches, not related data', async ({ page }) => {
    const source = await catalogTable(page.request, 'events')
    const target = await catalogTable(page.request, 'images')
    const canvasId = `join-inferred-${Date.now()}`
    let testError: unknown
    try {
      await seedSourceCanvas(page, canvasId, source)
      await page.getByTestId('join-with-related-canvas-selected-source').click()
      await page.getByPlaceholder('Dataset, column, tag…').fill(target.name)
      await expect(page.getByRole('heading', { name: 'Related data' })).toHaveCount(0)
      await page.getByRole('button', { name: /Show possible key matches/ }).click()
      await expect(page.getByRole('heading', { name: 'Possible key matches', exact: true })).toBeVisible()
      await expect(page.getByText(/No relationship is declared.*Matching key names only/)).toBeVisible()
      const targetCard = page.getByRole('button', { name: new RegExp(target.name, 'i') })
      await expect(targetCard.getByText('Possible key match', { exact: true })).toBeVisible()
      await expect(targetCard.getByText('No relationship is declared.')).toBeVisible()
      await expect(targetCard.getByText(/Matching key names only/)).toBeVisible()
      const cardinality = targetCard.locator('span.self-center')
      await expect(cardinality).toHaveClass(/bg-muted/)
      await expect(cardinality).not.toHaveClass(/bg-green-100/)
      await targetCard.click()
      await expect(page.getByText('Possible key match', { exact: true })).toBeVisible()
      await expect(page.getByTestId('possible-key-match-review')).toContainText('No relationship is declared')
      await expect(page.getByTestId('possible-key-match-review'))
        .toContainText('Cardinality describes row matching, not relationship confidence')
      await page.getByRole('button', { name: 'Cancel', exact: true }).click()
      const unchanged = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      expect(unchanged.version).toBe(1)
      expect(unchanged.nodes).toHaveLength(1)
      expect(unchanged.edges).toHaveLength(0)
    } catch (error) {
      testError = error
    } finally {
      try {
        const deleted = await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
        expect(deleted.ok(), await deleted.text()).toBeTruthy()
      } catch (cleanupError) {
        if (!testError) throw cleanupError
        console.error('Possible-match test cleanup failed after the primary test error:', cleanupError)
      }
    }
    if (testError) throw testError
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
      await page.getByTestId('join-with-related-canvas-selected-source').click()
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
      await page.getByTestId('join-with-related-canvas-selected-source').click()
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
