import { test, expect } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { readFileSync, rmSync } from 'node:fs'
import { goldenCanvas, installCanvas } from './support/ux-fixtures'
import { goToWorkspace, workspaceResource } from './support/workspace'

async function expectCompactFullResult(
  surface: import('@playwright/test').Locator,
  minimumVisibleTableHeight: number,
) {
  await expect(surface.getByTestId('full-result-status')).toHaveCount(1)
  await expect(surface.getByTestId('full-result-status')).toHaveText(/Complete · [\d,]+ rows/)
  await expect(surface.getByText('Full result artifact')).toHaveCount(0)
  await expect(surface.getByText(/Complete artifact/)).toHaveCount(0)
  await expect(surface.getByRole('button', { name: 'Export result' })).toHaveText('Export')
  const geometry = await surface.evaluate((element) => {
    const status = element.querySelector<HTMLElement>('[data-testid="full-result-status"]')
    const table = element.querySelector<HTMLTableElement>('table')
    const toolbar = element.querySelector<HTMLElement>('[data-testid="full-result-toolbar"]')
    if (!status || !table || !toolbar) return null
    const surfaceBox = element.getBoundingClientRect()
    const toolbarBox = toolbar.getBoundingClientRect()
    const tableBox = table.getBoundingClientRect()
    return {
      gapBelowToolbar: Math.round(tableBox.top - toolbarBox.bottom),
      visibleTableHeight: Math.round(
        Math.max(0, Math.min(tableBox.bottom, surfaceBox.bottom, window.innerHeight) - tableBox.top),
      ),
      surfaceClientWidth: element.clientWidth,
      surfaceScrollWidth: element.scrollWidth,
      toolbarClientWidth: toolbar.clientWidth,
      toolbarScrollWidth: toolbar.scrollWidth,
    }
  })
  expect(geometry).not.toBeNull()
  expect(geometry!.gapBelowToolbar).toBeLessThanOrEqual(1)
  expect(geometry!.visibleTableHeight).toBeGreaterThanOrEqual(minimumVisibleTableHeight)
  expect(geometry!.surfaceScrollWidth).toBeLessThanOrEqual(geometry!.surfaceClientWidth)
  expect(geometry!.toolbarScrollWidth).toBeLessThanOrEqual(geometry!.toolbarClientWidth)
}

async function expectNoTechnicalResultEvidence(
  surface: import('@playwright/test').Locator,
  runId: string,
  output: string,
) {
  await expect(surface.getByRole('button', { name: 'Diagnostics' })).toHaveCount(0)
  await expect(surface.getByTestId('full-result-technical-details')).toHaveCount(0)
  await expect(surface).not.toContainText(runId)
  await expect(surface).not.toContainText(output)
}

test.describe('researcher golden workflow @ux-smoke', () => {
  test('targets the chosen canvas and labels/downloads only the visible preview page', async ({ page }) => {
    const primary = goldenCanvas(`ux-golden-primary-${randomUUID()}`, 'UX primary canvas', 'UX primary source')
    const secondary = goldenCanvas(`ux-golden-secondary-${randomUUID()}`, 'UX secondary canvas', 'UX secondary source')
    await installCanvas(page.request, primary)
    await installCanvas(page.request, secondary)

    await page.goto(`/#/canvas/${primary.id}`)
    const primaryNode = page.locator('.react-flow__node', { hasText: 'UX primary source' })
    await expect(primaryNode).toBeVisible()
    await expect(page.locator('.react-flow__node', { hasText: 'UX secondary source' })).toHaveCount(0)

    await primaryNode.click()
    await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
    const panel = page.getByTestId('panel-data')
    await expect(panel).toBeVisible()
    await expect(panel.getByText('rows 1–50', { exact: true })).toBeVisible()
    await expect(panel.getByText('Preview prefix', { exact: true })).toHaveCount(0)
    await expect(panel.getByText(/Full dataset not scanned/)).toHaveCount(0)
    await expect(panel.getByText(/Preview uses up to .* rows from each input/)).toHaveCount(0)
    const exportPage = panel.getByRole('button', { name: 'Export this preview page' })
    await expect(exportPage).toBeVisible()
    const downloaded = page.waitForEvent('download')
    await exportPage.click()
    await page.getByRole('menuitem', { name: 'Download preview page as CSV' }).click()
    const download = await downloaded
    expect(download.suggestedFilename()).toBe('UX_primary_source-preview-page-1-50.csv')
    const file = await download.path()
    expect(file).not.toBeNull()
    const rows = readFileSync(file!, 'utf8').trim().split('\n')
    expect(rows[0]).toBe('id,user_id,event,amount')
    expect(rows).toHaveLength(51) // header + the bounded 50-row preview, never a silent full export

    await page.goto(`/#/canvas/${secondary.id}`)
    await expect(page.locator('.react-flow__node', { hasText: 'UX secondary source' })).toBeVisible()
    await expect(page.locator('.react-flow__node', { hasText: 'UX primary source' })).toHaveCount(0)

    page.once('dialog', async (dialog) => {
      expect(dialog.message()).toContain('UX secondary canvas')
      expect(dialog.message()).toContain("can't be undone")
      await dialog.dismiss()
    })
    await page.getByTestId('app-menu').click()
    await page.getByText('Delete this Canvas').click()
    await expect(page.getByTestId('toolbar')).toBeVisible()

    await goToWorkspace(page)
    await expect(await workspaceResource(page, 'canvas', 'UX secondary canvas')).toBeVisible()
  })

  test('a changed graph invalidates the old result instead of treating it as current', async ({ page }) => {
    const doc = goldenCanvas(`ux-golden-stale-${randomUUID()}`, 'UX stale canvas', 'UX stale source')
    await installCanvas(page.request, doc)
    const graph = {
      id: doc.id,
      version: doc.version,
      requirements: doc.requirements ?? [],
      nodes: doc.nodes.map((node) => ({
        id: node.id,
        type: node.type,
        position: node.position,
        parentId: node.parentId ?? null,
        data: {
          title: node.data.title,
          config: node.data.config,
          status: node.data.status,
          bypassed: node.data.bypassed,
          disabled: node.data.disabled,
        },
      })),
      edges: doc.edges,
    }
    const started = await page.request.post('/api/run', {
      data: { graph, targetNodeId: 'filter', confirmed: true },
    })
    const startFailure = started.ok() ? '' : await started.text()
    expect(started.ok(), startFailure).toBe(true)
    const runId = (await started.json()).runId as string
    await expect.poll(async () => {
      const response = await page.request.get(`/api/run/${encodeURIComponent(runId)}`)
      return (await response.json()).status
    }, { timeout: 30_000 }).toBe('done')

    await page.goto(`/#/canvas/${doc.id}`)
    const filter = page.locator('.react-flow__node', { hasText: 'UX golden filter' })
    await expect(filter).toContainText(/[\d,]+ rows · [\d,]+ ms/)
    await filter.click()
    await filter.getByPlaceholder('is_valid = true AND score > 0.5').fill("event = 'signup' OR amount > 0")
    await expect(filter).not.toContainText(/[\d,]+ rows · [\d,]+ ms/)
  })

  test('reopens and downloads the native full result without navigating away', async ({ page }) => {
    const doc = goldenCanvas(`ux-golden-export-${randomUUID()}`, 'UX export canvas', 'UX export source')
    await installCanvas(page.request, doc)
    const graph = {
      id: doc.id,
      version: doc.version,
      requirements: doc.requirements ?? [],
      nodes: doc.nodes.map((node) => ({
        id: node.id,
        type: node.type,
        position: node.position,
        parentId: node.parentId ?? null,
        data: {
          title: node.data.title,
          config: node.data.config,
          status: node.data.status,
          bypassed: node.data.bypassed,
          disabled: node.data.disabled,
        },
      })),
      edges: doc.edges,
    }
    const started = await page.request.post('/api/run', {
      data: { graph, targetNodeId: 'source', confirmed: true },
    })
    const startFailure = started.ok() ? '' : await started.text()
    expect(started.ok(), startFailure).toBe(true)
    const runId = (await started.json()).runId as string
    await expect.poll(async () => {
      const response = await page.request.get(`/api/run/${encodeURIComponent(runId)}`)
      return (await response.json()).status
    }, { timeout: 30_000 }).toBe('done')

    await page.goto(`/#/canvas/${doc.id}`)
    await page.getByTestId('app-menu').click()
    await page.getByText('Run history').click()
    await page.getByRole('button', { name: 'Open full result' }).click()
    await expect(page.getByTestId('full-result-status')).toHaveText(/Complete · [\d,]+ rows/)

    const downloaded = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Export result' }).click()
    await page.getByRole('menuitem', { name: 'Export all rows' }).click()
    const download = await downloaded
    expect(download.suggestedFilename()).toMatch(/-full-result\.parquet$/)
    const file = await download.path()
    expect(file).not.toBeNull()
    const bytes = readFileSync(file!)
    expect(bytes.subarray(0, 4).toString()).toBe('PAR1')
    expect(bytes.subarray(-4).toString()).toBe('PAR1')
    await expect(page).toHaveURL(new RegExp(`/#/canvas/${doc.id}$`))
  })

  test('recovers the exact retained result in a fresh browser and stops after a stale edit', async ({ page, browser, baseURL }) => {
    const doc = goldenCanvas(`ux-golden-retained-${randomUUID()}`, 'UX retained canvas', 'UX retained source')
    await installCanvas(page.request, doc)
    const graph = {
      id: doc.id,
      version: doc.version,
      requirements: doc.requirements ?? [],
      nodes: doc.nodes.map((node) => ({
        id: node.id,
        type: node.type,
        position: node.position,
        parentId: node.parentId ?? null,
        data: {
          title: node.data.title,
          config: node.data.config,
          status: node.data.status,
          bypassed: node.data.bypassed,
          disabled: node.data.disabled,
        },
      })),
      edges: doc.edges,
    }
    const started = await page.request.post('/api/run', {
      data: { graph, targetNodeId: 'filter', confirmed: true },
    })
    const startFailure = started.ok() ? '' : await started.text()
    expect(started.ok(), startFailure).toBe(true)
    const runId = (await started.json()).runId as string
    await expect.poll(async () => {
      const response = await page.request.get(`/api/run/${encodeURIComponent(runId)}`)
      return (await response.json()).status
    }, { timeout: 30_000 }).toBe('done')
    const historyBefore = await page.request.get(`/api/canvas/${doc.id}/runs`)
    expect(historyBefore.ok()).toBe(true)
    const runsBefore = await historyBefore.json() as Array<{ runId?: string }>
    const retained = await page.request.post('/api/run/retained-result', {
      data: { graph, nodeId: 'filter', portId: 'out' },
    })
    const retainedFailure = retained.ok() ? '' : await retained.text()
    expect(retained.ok(), retainedFailure).toBe(true)

    const freshContext = await browser.newContext({ baseURL })
    const freshPage = await freshContext.newPage()
    try {
      await freshPage.goto(`/#/canvas/${doc.id}`)
      const filter = freshPage.locator('.react-flow__node', { hasText: 'UX golden filter' })
      await expect(filter).toContainText(/\d[\d,]* rows · \d+(?:\.\d)? (?:ms|s)/)
      await filter.click()
      await freshPage.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()

      const panel = freshPage.getByTestId('panel-data')
      await expect(panel.getByTestId('full-result-status')).toHaveText(/Complete · [\d,]+ rows/)
      await expect(panel.getByRole('button', { name: 'Full result', exact: true }))
        .toHaveAttribute('aria-pressed', 'true')
      const historyAfterOpen = await freshPage.request.get(`/api/canvas/${doc.id}/runs`)
      expect((await historyAfterOpen.json()) as Array<{ runId?: string }>).toEqual(runsBefore)

      await filter.getByPlaceholder('is_valid = true AND score > 0.5').fill("event = 'signup'")
      await expect(filter).not.toContainText(/\d[\d,]* rows · \d+(?:\.\d)? (?:ms|s)/)
      await expect(panel.getByTestId('full-result-status')).toHaveCount(0)
      await expect(panel.getByRole('button', { name: 'Full result', exact: true })).toHaveCount(0)
    } finally {
      await freshContext.close()
      await page.request.delete(`/api/canvas/${doc.id}`)
    }
  })

  test('keeps one complete-result header and table space from Canvas through Jobs', async ({ page }, testInfo) => {
    const doc = goldenCanvas(`ux-full-result-header-${randomUUID()}`, 'UX full result header', 'UX full result source')
    await installCanvas(page.request, doc)
    const graph = {
      id: doc.id,
      version: doc.version,
      requirements: doc.requirements ?? [],
      nodes: doc.nodes.map((node) => ({
        id: node.id,
        type: node.type,
        position: node.position,
        parentId: node.parentId ?? null,
        data: {
          title: node.data.title,
          config: node.data.config,
          status: node.data.status,
          bypassed: node.data.bypassed,
          disabled: node.data.disabled,
        },
      })),
      edges: doc.edges,
    }
    const started = await page.request.post('/api/run', {
      data: { graph, targetNodeId: 'filter', confirmed: true },
    })
    expect(started.ok(), started.ok() ? '' : await started.text()).toBe(true)
    const runId = (await started.json()).runId as string
    await expect.poll(async () => {
      const response = await page.request.get(`/api/run/${encodeURIComponent(runId)}`)
      return (await response.json()).status
    }, { timeout: 30_000 }).toBe('done')

    await page.goto(`/#/canvas/${doc.id}`)
    const filter = page.locator('.react-flow__node', { hasText: 'UX golden filter' })
    await filter.click()
    await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
    const canvasResult = page.getByTestId('panel-data')
    for (const viewport of [{ width: 1280, height: 720 }, { width: 1440, height: 900 }]) {
      await page.setViewportSize(viewport)
      await expect(canvasResult.getByRole('button', { name: 'Full result', exact: true }))
        .toHaveAttribute('aria-pressed', 'true')
      await expect(canvasResult.getByRole('button', { name: 'Preview sample' })).toHaveCount(1)
      await expect(canvasResult.getByRole('button', { name: 'Full result', exact: true })).toHaveCount(1)
      await expectCompactFullResult(canvasResult, 430)
      await expectNoTechnicalResultEvidence(canvasResult, runId, 'filter:out')
    }
    await canvasResult.getByRole('button', { name: 'Export result' }).click()
    await expect(page.getByRole('menuitem', { name: 'Export all rows' })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: 'Download current page as CSV' })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: 'Download current page as JSON' })).toBeVisible()
    await page.keyboard.press('Escape')

    await page.goto(`/#/jobs?run=${encodeURIComponent(runId)}`)
    const job = page.getByRole('button', { name: new RegExp(`Open run ${runId}`) })
    await expect(job).toHaveAttribute('aria-expanded', 'true')
    await page.getByRole('button', { name: 'Open result' }).click()
    const jobsResult = page.getByRole('complementary', { name: 'Saved result' })
    for (const viewport of [{ width: 1280, height: 720 }, { width: 1440, height: 900 }]) {
      await page.setViewportSize(viewport)
      await expectCompactFullResult(jobsResult, viewport.height === 720 ? 190 : 250)
      await expectNoTechnicalResultEvidence(jobsResult, runId, 'filter:out')
    }

    const historyResponse = await page.request.get(`/api/canvas/${encodeURIComponent(doc.id)}/runs`)
    expect(historyResponse.ok(), await historyResponse.text()).toBe(true)
    const history = await historyResponse.json() as Array<{
      runId?: string
      outputs?: Array<{ nodeId?: string; portId?: string; uri?: string }>
    }>
    const exactOutput = history.find((entry) => entry.runId === runId)?.outputs?.find((output) => (
      output.nodeId === 'filter' && output.portId === 'out'
    ))
    expect(exactOutput?.uri).toBeTruthy()
    // Retire only the exact run artifact reported by Canvas history. The re-open flow must not
    // substitute any other result when this retained output is unavailable.
    rmSync(exactOutput!.uri!)
    await page.goto(`/#/canvas/${doc.id}`)
    const expiredFilter = page.locator('.react-flow__node', { hasText: 'UX golden filter' })
    await expiredFilter.click()
    await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
    const expiredResult = page.getByTestId('panel-data')
    await expect(expiredFilter).toContainText(/\d[\d,]* rows · \d+(?:\.\d)? (?:ms|s)/)
    await expect(expiredResult.getByText('Current result unavailable')).toBeVisible()
    await expect(expiredResult.getByText(/calculation is still up to date/i)).toBeVisible()
    await expect(expiredResult.getByRole('button', { name: 'Rerun and save result' })).toBeVisible()
    await expect(expiredResult.getByText('Run this step to see results')).toHaveCount(0)
    await expect(expiredResult.getByText('Full result expired or removed')).toHaveCount(0)
    for (const viewport of [{ width: 1280, height: 720 }, { width: 1440, height: 900 }]) {
      await page.setViewportSize(viewport)
      await expect(expiredResult).toBeVisible()
      await expiredResult.screenshot({ path: testInfo.outputPath(
        `issue-1109-current-result-unavailable-${viewport.width}x${viewport.height}.png`,
      ) })
    }
    await expectNoTechnicalResultEvidence(expiredResult, runId, 'filter:out')

    const sampleRoute = `**/api/run/${encodeURIComponent(runId)}/sample`
    await page.route(sampleRoute, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          columns: [],
          rows: [],
          rowCount: null,
          hasMore: false,
          truncated: false,
          completeness: 'unknown',
          error: true,
          reason: 'adapter failed while opening the retained artifact',
        }),
      })
    })
    await page.goto(`/#/canvas/${doc.id}`)
    const errorFilter = page.locator('.react-flow__node', { hasText: 'UX golden filter' })
    await errorFilter.click()
    await page.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()
    const errorResult = page.getByTestId('panel-data')
    await expect(errorResult.getByText('Couldn’t read full result')).toBeVisible()
    await expect(errorResult.getByText('adapter failed while opening the retained artifact')).toBeVisible()
    await expectNoTechnicalResultEvidence(errorResult, runId, 'filter:out')
    await page.unroute(sampleRoute)
  })

  test('states a capped chart window once while keeping whole-result export', async ({ page }) => {
    const doc = goldenCanvas(`ux-capped-chart-${randomUUID()}`, 'UX capped chart canvas', 'UX capped chart source')
    doc.nodes.push({
      id: 'chart', type: 'chart', position: { x: 700, y: 180 },
      data: {
        title: 'UX capped chart', status: 'latest',
        config: { chartType: 'bar', x: 'event', agg: 'count' },
      },
    })
    doc.edges.push({
      id: 'source-chart', source: 'source', target: 'chart', data: { wire: 'dataset' },
    })
    await installCanvas(page.request, doc)
    const graph = {
      id: doc.id,
      version: doc.version,
      requirements: doc.requirements ?? [],
      nodes: doc.nodes.map((node) => ({
        id: node.id,
        type: node.type,
        position: node.position,
        parentId: node.parentId ?? null,
        data: {
          title: node.data.title,
          config: node.data.config,
          status: node.data.status,
          bypassed: node.data.bypassed,
          disabled: node.data.disabled,
        },
      })),
      edges: doc.edges,
    }
    const started = await page.request.post('/api/run', {
      data: { graph, targetNodeId: 'chart', confirmed: true },
    })
    expect(started.ok(), started.ok() ? '' : await started.text()).toBe(true)
    const runId = (await started.json()).runId as string
    await expect.poll(async () => {
      const response = await page.request.get(`/api/run/${encodeURIComponent(runId)}`)
      return (await response.json()).status
    }, { timeout: 30_000 }).toBe('done')

    const sampleRoute = `**/api/run/${encodeURIComponent(runId)}/sample`
    await page.route(sampleRoute, async (route) => {
      expect(route.request().postDataJSON()).toMatchObject({
        nodeId: 'chart', portId: 'out', k: 2_000, offset: 0,
      })
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          columns: [
            { name: 'x', type: 'VARCHAR', capabilities: [] },
            { name: 'y', type: 'DOUBLE', capabilities: [] },
          ],
          rows: Array.from({ length: 2_000 }, (_, index) => ({
            x: `group-${index}`, y: index,
          })),
          rowCount: 2_001,
          hasMore: false,
          truncated: true,
          completeness: 'capped',
          rowLimit: 2_000,
          limitScope: 'result-window',
          limitReason: 'interactive-row-budget',
        }),
      })
    })

    await page.goto(`/#/canvas/${doc.id}`)
    await page.getByTestId('rf__node-chart').click()
    await page.getByTestId('inspector').getByRole('button', { name: 'View chart result' }).click()
    const result = page.getByTestId('panel-data')
    await expect(result.getByTestId('full-result-status')).toHaveText('Complete · 2,001 rows')
    await expect(result.getByText('Showing 2,000 groups · display capped')).toBeVisible()
    await expect(result.getByText('count(*) vs event')).toHaveCount(0)
    await expect(result.getByText(/Interactive view reached its 2,000 group display limit/)).toBeVisible()
    await expect(result.getByText(/Export all rows to use data beyond this window/)).toBeVisible()
    await expect(result.getByText('Interactive chart')).toHaveCount(0)
    const text = await result.innerText()
    expect(text.match(/2,001/g)).toHaveLength(1)
    expect(text.match(/\bComplete\b/g)).toHaveLength(1)
    await result.getByRole('button', { name: 'Export result' }).click()
    await expect(page.getByRole('menuitem', { name: 'Export all rows' })).toBeVisible()
    await page.unroute(sampleRoute)
  })

  test('never falls back to an older default-binding result in a fresh browser', async ({ page, browser, baseURL }) => {
    const doc = goldenCanvas(
      `ux-golden-retained-parameters-${randomUUID()}`,
      'UX retained parameter canvas',
      'UX retained parameter source',
    )
    doc.parameters = [{
      name: 'predicate', type: 'string', default: "event = 'purchase'",
    }]
    doc.nodes[1].data.config.predicate = { parameterRef: 'predicate' }
    await installCanvas(page.request, doc)
    const graph = {
      id: doc.id,
      version: doc.version,
      requirements: doc.requirements ?? [],
      parameters: doc.parameters,
      nodes: doc.nodes.map((node) => ({
        id: node.id,
        type: node.type,
        position: node.position,
        parentId: node.parentId ?? null,
        data: {
          title: node.data.title,
          config: node.data.config,
          status: node.data.status,
          bypassed: node.data.bypassed,
          disabled: node.data.disabled,
        },
      })),
      edges: doc.edges,
    }
    const startRun = async (parameterBindings?: Array<{ name: string; value: unknown }>) => {
      const response = await page.request.post('/api/run', {
        data: {
          graph, targetNodeId: 'filter', confirmed: true,
          ...(parameterBindings ? { parameterBindings } : {}),
        },
      })
      const failure = response.ok() ? '' : await response.text()
      expect(response.ok(), failure).toBe(true)
      const runId = (await response.json()).runId as string
      await expect.poll(async () => {
        const status = await page.request.get(`/api/run/${encodeURIComponent(runId)}`)
        return (await status.json()).status
      }, { timeout: 30_000 }).toBe('done')
      return runId
    }
    const defaultRunId = await startRun()
    const boundRunId = await startRun([{
      name: 'predicate', value: "event = 'signup'",
    }])
    expect(boundRunId).not.toBe(defaultRunId)
    const exact = await page.request.post('/api/run/retained-result', {
      data: {
        graph, nodeId: 'filter', portId: 'out',
        parameterBindings: [{ name: 'predicate', value: "event = 'signup'" }],
      },
    })
    expect(exact.ok(), exact.ok() ? '' : await exact.text()).toBe(true)
    expect((await exact.json()).runId).toBe(boundRunId)
    const recovered = await page.request.post('/api/run/retained-result', {
      data: { graph, nodeId: 'filter', portId: 'out' },
    })
    expect(recovered.ok(), recovered.ok() ? '' : await recovered.text()).toBe(true)
    const recoveredIdentity = await recovered.json() as {
      runId: string
      parameterBindings: Array<{ name: string; value: unknown }>
    }
    expect(recoveredIdentity.runId).toBe(boundRunId)
    expect(recoveredIdentity.parameterBindings).toEqual([{
      name: 'predicate', value: "event = 'signup'",
    }])

    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${doc.id}/runs`)
      const runs = await response.json() as Array<{ runId?: string }>
      return runs.map((run) => run.runId)
    }, { timeout: 30_000 }).toEqual(expect.arrayContaining([defaultRunId, boundRunId]))
    const historyBefore = await page.request.get(`/api/canvas/${doc.id}/runs`)
    const runsBefore = await historyBefore.json() as Array<{ runId?: string }>
    const freshContext = await browser.newContext({ baseURL })
    const freshPage = await freshContext.newPage()
    try {
      await freshPage.goto(`/#/canvas/${doc.id}`)
      const filter = freshPage.locator('.react-flow__node', { hasText: 'UX golden filter' })
      await expect(filter).toContainText(/\d[\d,]* rows · \d+(?:\.\d)? (?:ms|s)/)
      await filter.click()
      await freshPage.getByTestId('inspector').getByRole('button', { name: 'View data' }).click()

      const panel = freshPage.getByTestId('panel-data')
      await expect(panel.getByTestId('full-result-status')).toBeVisible()
      await expect(panel.getByText('event', { exact: true }).first()).toBeVisible()
      const historyAfter = await freshPage.request.get(`/api/canvas/${doc.id}/runs`)
      expect(await historyAfter.json()).toEqual(runsBefore)
    } finally {
      await freshContext.close()
      await page.request.delete(`/api/canvas/${doc.id}`)
    }
  })
})
