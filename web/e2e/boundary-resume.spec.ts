import { test, expect } from '@playwright/test'
import { randomUUID } from 'node:crypto'

type RunStatus = {
  runId: string
  status: string
  reusedBoundary?: {
    boundaryNodeId: string
    boundaryRunId: string
  } | null
  perNode?: Array<{ nodeId: string; reused?: boolean; ms?: number | null }>
}

async function waitRun(
  request: import('@playwright/test').APIRequestContext, runId: string,
): Promise<RunStatus> {
  let latest: RunStatus | null = null
  await expect.poll(async () => {
    const response = await request.get(`/api/run/${encodeURIComponent(runId)}`)
    expect(response.ok()).toBe(true)
    latest = await response.json() as RunStatus
    return latest.status
  }, { timeout: 30_000 }).toMatch(/^(done|failed|cancelled)$/)
  expect(latest).not.toBeNull()
  return latest!
}

test('resumes a downstream local run from the nearest retained boundary @ux-smoke', async ({
  page, browser, baseURL,
}) => {
  const canvasId = `ux-boundary-resume-${randomUUID()}`
  const graph = {
    id: canvasId,
    name: 'UX boundary resume',
    version: 1,
    executionBackend: 'local-out-of-core',
    requirements: [] as string[],
    nodes: [
      {
        id: 'source', type: 'source', position: { x: 40, y: 80 },
        data: { title: 'UX boundary source', config: { uri: 'events' } },
      },
      {
        id: 'sample', type: 'sample', position: { x: 280, y: 80 },
        data: { title: 'UX boundary sample', config: { n: 20, seed: 7 } },
      },
      {
        id: 'filter', type: 'filter', position: { x: 520, y: 80 },
        data: {
          title: 'UX boundary filter',
          config: { predicate: "event = 'purchase' OR amount > 0" },
        },
      },
    ],
    edges: [
      {
        id: 'source-sample', source: 'source', target: 'sample',
        sourceHandle: 'out', targetHandle: 'in', data: { wire: 'dataset' },
      },
      {
        id: 'sample-filter', source: 'sample', target: 'filter',
        sourceHandle: 'out', targetHandle: 'in', data: { wire: 'dataset' },
      },
    ],
  }

  const created = await page.request.post('/api/canvas', { data: graph })
  expect(created.ok(), await created.text()).toBe(true)
  try {
    const intermediate = await page.request.post('/api/run', {
      data: {
        graph, targetNodeId: 'sample', confirmed: true, submissionId: randomUUID(),
      },
    })
    expect(intermediate.ok(), await intermediate.text()).toBe(true)
    const intermediateStatus = await waitRun(page.request, (await intermediate.json()).runId)
    expect(intermediateStatus.status).toBe('done')

    const fresh = await browser.newContext({ baseURL })
    const freshPage = await fresh.newPage()
    try {
      await freshPage.goto(`/#/canvas/${canvasId}`)
      await expect(freshPage.locator('.react-flow__node', { hasText: 'UX boundary sample' })).toBeVisible()

      const downstream = await freshPage.request.post('/api/run', {
        data: {
          graph, targetNodeId: 'filter', confirmed: true, submissionId: randomUUID(),
        },
      })
      expect(downstream.ok(), await downstream.text()).toBe(true)
      const resumed = await waitRun(freshPage.request, (await downstream.json()).runId)
      expect(resumed.status).toBe('done')
      expect(resumed.reusedBoundary?.boundaryNodeId).toBe('sample')
      expect(resumed.reusedBoundary?.boundaryRunId).toBe(intermediateStatus.runId)
      const byId = Object.fromEntries(
        (resumed.perNode ?? []).map((item) => [item.nodeId, item]),
      )
      expect(byId.sample?.reused).toBe(true)
      expect(byId.sample?.ms ?? null).toBeNull()
      expect(byId.filter?.reused).toBe(false)
    } finally {
      await fresh.close()
    }
  } finally {
    await page.request.delete(`/api/canvas/${canvasId}`)
  }
})
