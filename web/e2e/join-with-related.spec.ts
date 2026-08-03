import { unlinkSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const fullProfile = process.env.DP_E2E_FIXTURE_PROFILE === 'full'
const dialogName = 'Find join candidates'
const leftInputTrigger = 'Find join candidates · left'

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

async function sourceRelatedAction(page: Page) {
  await page.locator('.react-flow__node[data-id="selected-source"]').click()
  const expand = page.getByRole('button', { name: 'Expand Inspector', exact: true })
  if (await expand.isVisible().catch(() => false)) await expand.click()
  const action = page.getByTestId('inspector').getByTestId('join-with-related-selected-source')
  await expect(action).toHaveAccessibleName('Find join candidates')
  return action
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
  await expect(page.getByTestId('join-with-related-canvas-selected-source')).toHaveCount(0)
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

async function seedPlacementReproductionCanvas(page: Page, canvasId: string, table: Table) {
  const config = { uri: table.uri, tableId: table.id, registrationId: table.registrationId }
  const response = await page.request.post('/api/canvas', { data: {
    id: canvasId,
    name: 'Related Join placement reproduction',
    version: 1,
    nodes: [
      { id: 'selected-source', type: 'source', position: { x: 32, y: 272 }, data: {
        title: table.name, status: 'draft', history: [], config,
      } },
      { id: 'existing-code', type: 'code', position: { x: 384, y: 272 }, data: {
        title: 'Existing code annotation', status: 'draft', history: [],
        // The code annotation's clipped 320x275 mounted envelope is deliberately wider than a
        // NodeCard.  This is the regression the server-side placement contract must reserve.
        config: { lang: 'python', code: Array.from({ length: 40 }, (_, index) => `value_${index} = ${index}`).join('\n') },
      } },
      { id: 'existing-write', type: 'write', position: { x: 700, y: 272 }, data: {
        title: 'Existing write', status: 'draft', history: [], config: {},
      } },
    ],
    edges: [],
  } })
  expect(response.ok()).toBeTruthy()
  await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
  await expect(page.locator('.react-flow__node')).toHaveCount(3)
  await expect(page.getByTestId('join-with-related-canvas-selected-source')).toHaveCount(0)
}

async function seedTallJoinPlacementCanvas(page: Page, canvasId: string, table: Table) {
  const config = { uri: table.uri, tableId: table.id, registrationId: table.registrationId }
  const tenKeys = Array.from({ length: 10 }, (_, index) => `key_${index}`).join(', ')
  const response = await page.request.post('/api/canvas', { data: {
    id: canvasId,
    name: 'Tall related Join placement reproduction',
    version: 1,
    nodes: [
      { id: 'selected-source', type: 'source', position: { x: 0, y: 0 }, data: {
        title: table.name, status: 'draft', history: [], config,
      } },
      // Ten persisted structured keys render ten key-builder rows.  This is deliberately a
      // legal disconnected Join: placement must reserve every existing top-level card.
      { id: 'existing-tall-join', type: 'join', position: { x: 450, y: -150 }, data: {
        title: 'Existing ten-key Join', status: 'draft', history: [],
        config: { how: 'inner', on: tenKeys },
      } },
    ],
    edges: [],
  } })
  expect(response.ok()).toBeTruthy()
  await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}`)
  await expect(page.locator('.react-flow__node')).toHaveCount(2)
  await expect(page.locator('.react-flow__node[data-id="existing-tall-join"]')
    .getByLabel('Left key 10')).toBeVisible()
}

function disjoint(first: { x: number, y: number, width: number, height: number }, second: { x: number, y: number, width: number, height: number }) {
  return first.x + first.width <= second.x || second.x + second.width <= first.x
    || first.y + first.height <= second.y || second.y + second.height <= first.y
}

async function unregisterTable(request: APIRequestContext, table: { id: string, registrationId?: string, metadataRevision?: string }) {
  if (!table.registrationId || !table.metadataRevision) return
  await request.delete(`/api/catalog/tables/${encodeURIComponent(table.id)}`, { params: {
    expected_registration_id: table.registrationId,
    expected_revision: table.metadataRevision,
  } })
}

test.describe('Related data and possible key matches', () => {
  test('places and reveals a confirmed related Source and Join in the safe desktop Canvas region', async ({ page }) => {
    test.setTimeout(60_000)
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
    expect((await page.request.post('/api/catalog/relationships', { data: relation })).ok()).toBeTruthy()
    try {
      for (const viewport of [{ width: 1280, height: 720 }, { width: 1440, height: 900 }]) {
        await page.setViewportSize(viewport)
        const canvasId = `join-related-placement-${viewport.width}-${Date.now()}`
        try {
          await seedPlacementReproductionCanvas(page, canvasId, left)
          const sourceIdentity = { kind: 'local', registrationId: left.registrationId, revisionMode: 'current' }
          const candidates = await page.request.post('/api/catalog/related-datasets', {
            data: { source: sourceIdentity, limit: 12 },
          })
          expect(candidates.ok()).toBeTruthy()
          const pageOfCandidates = await candidates.json() as { candidates: any[], possibleMatches: any[] }
          const candidate = [...pageOfCandidates.candidates, ...pageOfCandidates.possibleMatches]
            .find((item) => item.name === right.name)
          expect(candidate).toBeDefined()
          // The action endpoint is the one used by the review dialog.  Calling it directly here
          // keeps this visual regression focused on the persisted placement and Chromium geometry;
          // the dialog's confirm journey is covered separately below.
          const confirmed = await page.request.post(`/api/canvas/${encodeURIComponent(canvasId)}/join-with-related`, {
            data: {
              expectedCanvasVersion: 1,
              sourceNodeId: 'selected-source',
              sourceIdentity,
              candidate,
              how: 'inner',
            },
          })
          expect(confirmed.ok()).toBeTruthy()
          await page.reload()
          const inserted = page.locator('.react-flow__node[data-id^="source_related_"], .react-flow__node[data-id^="join_related_"]')
          await expect(inserted).toHaveCount(2)
          await expect(async () => {
            const surface = await page.locator('.react-flow').boundingBox()
            const boxes = await inserted.evaluateAll((elements) => elements.map((element) => {
              const box = element.getBoundingClientRect()
              return { x: box.x, y: box.y, width: box.width, height: box.height }
            }))
            const existing = await page.locator('.react-flow__node[data-id="selected-source"], .react-flow__node[data-id="existing-code"], .react-flow__node[data-id="existing-write"]').evaluateAll((elements) => elements.map((element) => {
              const box = element.getBoundingClientRect()
              return { x: box.x, y: box.y, width: box.width, height: box.height }
            }))
            expect(surface).not.toBeNull()
            expect(boxes).toHaveLength(2)
            for (const box of boxes) {
              expect(box.x).toBeGreaterThanOrEqual(surface!.x + 196)
              expect(box.y).toBeGreaterThanOrEqual(surface!.y + 96)
              expect(box.x + box.width).toBeLessThanOrEqual(surface!.x + surface!.width - 16)
              expect(box.y + box.height).toBeLessThanOrEqual(surface!.y + surface!.height - 208)
              for (const other of existing) expect(disjoint(box, other)).toBeTruthy()
            }
          }).toPass({ timeout: 5_000 })
        } finally {
          await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
        }
      }
    } finally {
      await page.request.post('/api/catalog/relationships/delete', { data: relation })
    }
  })

  test('keeps a confirmed related pair clear of a persisted ten-key Join at desktop widths', async ({ page }) => {
    test.setTimeout(60_000)
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
    expect((await page.request.post('/api/catalog/relationships', { data: relation })).ok()).toBeTruthy()
    try {
      for (const viewport of [{ width: 1280, height: 720 }, { width: 1440, height: 900 }]) {
        await page.setViewportSize(viewport)
        const canvasId = `join-related-tall-placement-${viewport.width}-${Date.now()}`
        try {
          await seedTallJoinPlacementCanvas(page, canvasId, left)
          const sourceIdentity = { kind: 'local', registrationId: left.registrationId, revisionMode: 'current' }
          const candidates = await page.request.post('/api/catalog/related-datasets', {
            data: { source: sourceIdentity, limit: 12 },
          })
          expect(candidates.ok()).toBeTruthy()
          const pageOfCandidates = await candidates.json() as { candidates: any[], possibleMatches: any[] }
          const candidate = [...pageOfCandidates.candidates, ...pageOfCandidates.possibleMatches]
            .find((item) => item.name === right.name)
          expect(candidate).toBeDefined()
          const confirmed = await page.request.post(`/api/canvas/${encodeURIComponent(canvasId)}/join-with-related`, {
            data: {
              expectedCanvasVersion: 1,
              sourceNodeId: 'selected-source',
              sourceIdentity,
              candidate,
              how: 'inner',
            },
          })
          expect(confirmed.ok()).toBeTruthy()
          await page.reload()
          const inserted = page.locator('.react-flow__node[data-id^="source_related_"], .react-flow__node[data-id^="join_related_"]')
          const existingTallJoin = page.locator('.react-flow__node[data-id="existing-tall-join"]')
          await expect(inserted).toHaveCount(2)
          await expect(async () => {
            const tallBox = await existingTallJoin.boundingBox()
            const insertedBoxes = await inserted.evaluateAll((elements) => elements.map((element) => {
              const box = element.getBoundingClientRect()
              return { x: box.x, y: box.y, width: box.width, height: box.height }
            }))
            expect(tallBox).not.toBeNull()
            expect(insertedBoxes).toHaveLength(2)
            for (const box of insertedBoxes) expect(disjoint(box, tallBox!)).toBeTruthy()
          }).toPass({ timeout: 5_000 })
        } finally {
          await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
        }
      }
    } finally {
      await page.request.post('/api/catalog/relationships/delete', { data: relation })
    }
  })

  test('an open related-data dialog blocks the Canvas Delete shortcut', async ({ page }) => {
    const source = await catalogTable(page.request, 'events')
    const canvasId = `join-related-modal-${Date.now()}`
    try {
      await seedSourceCanvas(page, canvasId, source)
      await (await sourceRelatedAction(page)).click()
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

  test('modal interactions preserve the selected Source behind the portal at 1280x720', async ({ page }) => {
    const source = await catalogTable(page.request, 'events')
    const canvasId = `join-related-selection-${Date.now()}`
    try {
      await seedSourceCanvas(page, canvasId, source, true)
      const selectedSource = page.locator('.react-flow__node[data-id="selected-source"]')
      const bystander = page.locator('.react-flow__node[data-id="bystander-source"]')
      await bystander.click()
      await expect(bystander).toHaveClass(/selected/)
      await expect(selectedSource).not.toHaveClass(/selected/)

      await (await sourceRelatedAction(page)).click()
      const dialog = page.getByRole('dialog', { name: dialogName })
      await expect(dialog).toBeVisible()
      await expect(selectedSource).toHaveClass(/selected/)
      await expect(bystander).not.toHaveClass(/selected/)

      await dialog.getByPlaceholder('Dataset, column, tag…').fill('images')
      await expect(dialog.getByPlaceholder('Dataset, column, tag…')).toHaveValue('images')
      await expect(selectedSource).toHaveClass(/selected/)
      await expect(bystander).not.toHaveClass(/selected/)

      await page.locator('.dp-modal-overlay').click({ position: { x: 2, y: 2 } })
      await expect(dialog).toBeHidden()
      await expect(selectedSource).toHaveClass(/selected/)
      await expect(bystander).not.toHaveClass(/selected/)
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
      await (await sourceRelatedAction(page)).click()
      const dialog = page.getByRole('dialog', { name: dialogName })
      await expect(dialog).toContainText('declared or reference-backed relationships and possible key matches')
      await expect(page.getByRole('heading', { name: 'Related data', exact: true })).toBeVisible()
      const declaredCard = page.getByRole('button', { name: new RegExp(right.name, 'i') })
      await expect(declaredCard).toContainText('Declared catalog relationship')
      await expect(declaredCard).not.toContainText('No relationship is declared')
      await declaredCard.click()
      await expect(page.getByText('Left input (a)').locator('..')).toContainText(left.name)
      await expect(page.getByText('Right input (b)').locator('..')).toContainText(right.name)
      await expect(page.getByText('Declared catalog relationship')).toBeVisible()
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
      await (await sourceRelatedAction(page)).click()
      await page.getByPlaceholder('Dataset, column, tag…').fill(target.name)
      const referenceCard = page.getByRole('button', { name: new RegExp(target.name, 'i') })
      await expect(referenceCard).toContainText('Declared key/reference')
      await expect(referenceCard).not.toContainText('No relationship is declared')
      await expect(page.getByRole('heading', { name: 'Related data', exact: true })).toBeVisible()
      await referenceCard.click()
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
      const mutations: Array<{ method: string, pathname: string }> = []
      page.on('request', (request) => {
        const url = new URL(request.url())
        if (url.pathname === `/api/canvas/${canvasId}/join-with-related`
          || url.pathname === `/api/canvas/${canvasId}`) {
          mutations.push({ method: request.method(), pathname: url.pathname })
        }
      })
      await (await sourceRelatedAction(page)).click()
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
      // A related Join POST already returns the authoritative Canvas.  Installing that server
      // document must not echo through autosave as a second PUT/version.  Wait past the debounce
      // so this is an exact-wheel, real-kernel regression rather than a mocked store assertion.
      await page.waitForTimeout(650)
      const settled = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      expect(settled.version).toBe(2)
      expect(mutations.filter((request) => request.method === 'POST'
        && request.pathname.endsWith('/join-with-related'))).toHaveLength(1)
      expect(mutations.filter((request) => request.method === 'PUT'
        && request.pathname === `/api/canvas/${canvasId}`)).toHaveLength(0)
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
      await expect(review.getByLabel('Join type')).toHaveValue('left')
      await review.getByLabel('Join type').selectOption('right')
      await expect(review.getByLabel('Join type')).toHaveValue('right')
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
      await (await sourceRelatedAction(page)).click()
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
      await (await sourceRelatedAction(page)).click()
      await page.getByPlaceholder('Dataset, column, tag…').fill(target.name)
      await expect(page.getByRole('heading', { name: 'Related data' })).toHaveCount(0)
      await page.getByRole('button', { name: /Show possible key matches/ }).click()
      await expect(page.getByRole('heading', { name: 'Possible key matches', exact: true })).toBeVisible()
      await expect(page.getByText(/No relationship is declared.*Matching key names only/)).toBeVisible()
      const targetCard = page.getByRole('button', { name: new RegExp(target.name, 'i') })
      await expect(targetCard.getByText('Suggested', { exact: true })).toBeVisible()
      await expect(targetCard.getByText('Matching column names', { exact: true })).toBeVisible()
      await expect(targetCard.getByText(/No relationship is declared/)).toHaveCount(0)
      const cardinality = targetCard.locator('span.self-center')
      await expect(cardinality).toHaveClass(/bg-muted/)
      await expect(cardinality).not.toHaveClass(/bg-green-100/)
      await targetCard.click()
      await expect(page.getByTestId('possible-key-match-review'))
        .toHaveText('Suggested from matching column names; no catalog relationship is declared.')
      await expect(page.getByText(/Cardinality describes row matching/)).toHaveCount(0)
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
      await (await sourceRelatedAction(page)).click()
      const truncation = page.getByText('Showing the first matches only. Refine the search or folder to see the rest.')
      await expect(truncation).toBeVisible()

      const search = page.getByPlaceholder('Dataset, column, tag…')
      await search.fill(`${token}-focused`)
      await page.getByRole('button', { name: /Show possible key matches/ }).click()
      await expect(page.getByRole('button', { name: new RegExp(`${token}-focused`, 'i') })).toBeVisible()
      await expect(truncation).toBeHidden()

      await search.fill('')
      const folder = page.getByPlaceholder('Optional folder subtree')
      await folder.fill(`${token}/focused`)
      await page.getByRole('button', { name: /Show possible key matches/ }).click()
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
      await (await sourceRelatedAction(page)).click()
      await page.getByRole('button', { name: new RegExp(right.name, 'i') }).click()
      const current = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      const advanced = await page.request.put(`/api/canvas/${encodeURIComponent(canvasId)}?expectedVersion=1`, {
        data: { ...current, name: `${current.name} advanced` },
      })
      expect(advanced.ok()).toBeTruthy()
      await page.getByTestId('confirm-related-join').click()
      await expect(page.getByRole('button', { name: 'Reapply to latest Canvas' })).toBeVisible()
      await expect(page.getByText('Right input (b)').locator('..')).toContainText(right.name)
    } finally {
      await page.request.post('/api/catalog/relationships/delete', { data: relation })
      await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)
    }
  })
})
