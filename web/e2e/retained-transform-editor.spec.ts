import { expect, test } from '@playwright/test'

type CatalogTable = { id: string; name: string; uri: string; registrationId: string }

async function seededTable(page: import('@playwright/test').Page, name: string): Promise<CatalogTable> {
  const response = await page.request.get('/api/catalog/search', {
    params: { q: name, mode: 'lexical', limit: 10 },
  })
  expect(response.ok()).toBeTruthy()
  const tables = await response.json() as CatalogTable[]
  const table = tables.find((item) => item.name === name)
  if (!table) throw new Error(`Missing seeded ${name} catalog table`)
  return table
}

test('fullscreen Transform reuses a retained full-run result after bounded remote preview refusal', async ({ page }) => {
  const canvasId = `retained-transform-${Date.now()}`
  const sourceUri = 'e2e-full-run-only://events'
  const registered = await page.request.post('/api/catalog/register', {
    data: { uri: sourceUri, name: 'E2E full-run-only events' },
  })
  const registrationText = await registered.text()
  expect(registered.ok(), registrationText).toBe(true)
  const registeredTable = JSON.parse(registrationText)
  let canvasCreated = false
  try {
    const graph = {
      id: canvasId,
      name: 'Retained Transform editor',
      version: 1,
      requirements: [],
      nodes: [
        {
          id: 'source', type: 'source', position: { x: 80, y: 180 },
          data: { title: 'Remote events', status: 'latest', config: {
            uri: sourceUri,
            datasetRef: {
              kind: 'exact', datasetId: registeredTable.registrationId, revisionId: 'fixture-v1',
            },
          } },
        },
        {
          id: 'sample', type: 'sample', position: { x: 390, y: 180 },
          data: { title: 'Remote sample', status: 'draft', config: { n: 8, seed: 42 } },
        },
        {
          id: 'transform', type: 'transform', position: { x: 700, y: 180 },
          data: { title: 'Test retained rows', status: 'draft', config: {
            source: 'adhoc', mode: 'map',
            code: "def fn(row):\n    return {**row, 'tested_in_editor': True}",
            onError: 'raise',
          } },
        },
      ],
      edges: [
        {
          id: 'source-sample', source: 'source', target: 'sample',
          sourceHandle: 'out', targetHandle: 'in', data: { wire: 'dataset' },
        },
        {
          id: 'sample-transform', source: 'sample', target: 'transform',
          sourceHandle: 'out', targetHandle: 'in', data: { wire: 'sample' },
        },
      ],
    }
    const created = await page.request.post('/api/canvas', { data: graph })
    expect(created.ok()).toBe(true)
    canvasCreated = true

    const refused = await page.request.post('/api/run/preview', {
      data: { graph, nodeId: 'sample', k: 50, offset: 0 },
    })
    expect(refused.ok()).toBe(true)
    const refusedBody = await refused.json()
    expect(refusedBody.notPreviewable).toBe(true)

    const started = await page.request.post('/api/run', { data: {
      graph, targetNodeId: 'sample', confirmed: true, submissionId: crypto.randomUUID(),
    } })
    expect(started.ok()).toBe(true)
    const { runId } = await started.json()
    await expect.poll(async () => {
      const response = await page.request.get(`/api/run/${encodeURIComponent(runId)}`)
      return (await response.json()).status
    }, { timeout: 15_000 }).toBe('done')

    const openEditor = async () => {
      await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=transform`)
      await expect(page.locator('.react-flow__node')).toHaveCount(3)
      await page.getByRole('button', { name: 'Edit code' }).last().click()
      await expect(page.getByRole('button', { name: 'Test code' })).toBeVisible()
      await expect(page.getByText('Using Remote sample · 8 rows')).toBeVisible({
        timeout: 15_000,
      })
      await expect(page.getByText('true', { exact: true }).first()).toBeVisible()
      await expect(page.getByText(/Run a full pass/)).toHaveCount(0)
    }

    await openEditor()
    await page.reload()
    await openEditor()
  } finally {
    if (canvasCreated) {
      expect((await page.request.delete(
        `/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBe(true)
    }
    const query = new URLSearchParams({
      expected_registration_id: registeredTable.registrationId,
      expected_revision: registeredTable.metadataRevision,
    })
    expect((await page.request.delete(
      `/api/catalog/tables/${encodeURIComponent(registeredTable.id)}?${query}`)).ok()).toBe(true)
  }
})

test('Workspace-style local Sources retain a Join result when the Transform editor is reopened', async ({ page }) => {
  test.setTimeout(45_000)
  const [events, images] = await Promise.all([
    seededTable(page, 'events'), seededTable(page, 'images'),
  ])
  const canvasId = `retained-local-join-${Date.now()}`
  let canvasCreated = false
  try {
    // This is exactly the Source config emitted by Workspace Use: logical registration, no
    // client-carried exact DatasetRef. The run's execution manifest owns the admitted revisions.
    const graph = {
      id: canvasId, name: 'Retained local Join editor', version: 1, requirements: [],
      nodes: [
        { id: 'events', type: 'source', position: { x: 80, y: 120 }, data: {
          title: events.name, status: 'draft', config: {
            uri: events.uri, tableId: events.id, registrationId: events.registrationId,
          },
        } },
        { id: 'images', type: 'source', position: { x: 80, y: 360 }, data: {
          title: images.name, status: 'draft', config: {
            uri: images.uri, tableId: images.id, registrationId: images.registrationId,
          },
        } },
        { id: 'join', type: 'join', position: { x: 400, y: 220 }, data: {
          title: 'events with images', status: 'draft', config: { how: 'inner', on: 'id' },
        } },
        { id: 'transform', type: 'transform', position: { x: 700, y: 220 }, data: {
          title: 'Test retained Join', status: 'draft', config: {
            source: 'adhoc', mode: 'map',
            code: "def fn(row):\n    return {**row, 'tested_in_editor': True}", onError: 'raise',
          },
        } },
      ],
      edges: [
        { id: 'events-join', source: 'events', target: 'join', sourceHandle: 'out', targetHandle: 'a', data: { wire: 'dataset' } },
        { id: 'images-join', source: 'images', target: 'join', sourceHandle: 'out', targetHandle: 'b', data: { wire: 'dataset' } },
        { id: 'join-transform', source: 'join', target: 'transform', sourceHandle: 'out', targetHandle: 'in', data: { wire: 'dataset' } },
      ],
    }
    const created = await page.request.post('/api/canvas', { data: graph })
    expect(created.ok()).toBeTruthy()
    canvasCreated = true
    const started = await page.request.post('/api/run', { data: {
      graph, targetNodeId: 'join', confirmed: true, submissionId: crypto.randomUUID(),
    } })
    expect(started.ok()).toBeTruthy()
    const { runId } = await started.json()
    await expect.poll(async () => (await (await page.request.get(`/api/run/${runId}`)).json()).status,
      { timeout: 20_000 }).toBe('done')

    const openEditor = async () => {
      await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=transform`)
      await expect(page.locator('.react-flow__node')).toHaveCount(4)
      await page.getByRole('button', { name: 'Edit code' }).last().click()
      await expect(page.getByRole('button', { name: 'Test code' })).toBeVisible()
      await expect(page.getByText('Using events with images · 500 rows')).toBeVisible()
      await expect(page.getByText('true', { exact: true }).first()).toBeVisible()
    }
    await openEditor()
    await page.getByRole('button', { name: 'Close' }).click()
    await openEditor()
  } finally {
    if (canvasCreated) expect((await page.request.delete(`/api/canvas/${canvasId}`)).ok()).toBeTruthy()
  }
})
