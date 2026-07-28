import { expect, test, type Page } from '@playwright/test'
import { goToWorkspace, workspaceResource } from './support/workspace'

async function expectToolbarInsideCanvas(page: Page, viewportWidth: number) {
  const toolbar = page.getByTestId('toolbar')
  const canvas = page.locator('.react-flow')
  const nextStep = page.getByRole('button', { name: 'Add next step', exact: true })
  const [toolbarBox, canvasBox, nextStepBox] = await Promise.all([
    toolbar.boundingBox(), canvas.boundingBox(), nextStep.boundingBox(),
  ])
  expect(toolbarBox).not.toBeNull()
  expect(canvasBox).not.toBeNull()
  expect(nextStepBox).not.toBeNull()
  expect(toolbarBox!.x).toBeGreaterThanOrEqual(canvasBox!.x - 0.5)
  expect(toolbarBox!.x + toolbarBox!.width).toBeLessThanOrEqual(canvasBox!.x + canvasBox!.width + 0.5)
  expect(nextStepBox!.x).toBeGreaterThanOrEqual(canvasBox!.x - 0.5)
  expect(nextStepBox!.x + nextStepBox!.width).toBeLessThanOrEqual(canvasBox!.x + canvasBox!.width + 0.5)
  expect(canvasBox!.x + canvasBox!.width).toBeLessThan(viewportWidth)
}

test.describe('Workspace Source next-step flow @ux-smoke', () => {
  test('keeps the selected Source next-step flow responsive and connects Transform', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 })
    await goToWorkspace(page)

    // This is the normal researcher route: a Workspace dataset detail, not an API-created graph.
    await (await workspaceResource(page, 'dataset', 'events')).click()
    await page.getByTestId('detail-use').click()
    const useDialog = page.getByRole('dialog', { name: 'Use events' })
    await useDialog.getByRole('button', { name: 'Create and open' }).click()
    await expect(page).toHaveURL(/#\/canvas\//)
    const canvasId = decodeURIComponent(new URL(page.url()).hash.split('/').pop()!.split('?')[0])

    const toolbar = page.getByTestId('toolbar')
    const nextStep = page.getByRole('button', { name: 'Add next step', exact: true })
    await expect(page.getByRole('button', { name: 'Collapse Inspector' })).toBeVisible()
    await expect(nextStep).toBeVisible()
    await expect(nextStep).toContainText('Add next step')
    await expect(toolbar).toHaveAttribute('data-density', 'compact')
    await expect(toolbar.getByText('View', { exact: true })).toBeVisible()
    await expect(toolbar.getByRole('button', { name: 'Fit view' })).toContainText('Fit view')
    await expectToolbarInsideCanvas(page, 1280)

    await page.setViewportSize({ width: 1440, height: 900 })
    await expect(toolbar).toHaveAttribute('data-density', 'comfortable')
    await expect(toolbar.getByText('View', { exact: true })).toBeVisible()
    await expect(toolbar.getByRole('button', { name: 'Fit view' })).toContainText('Fit view')
    await expect(nextStep).toContainText('Add next step')
    await expectToolbarInsideCanvas(page, 1440)

    await nextStep.click()
    const finder = page.getByRole('dialog', { name: 'Add an operation' })
    await finder.getByRole('textbox', { name: 'Search operations' }).fill('transform')
    const transform = finder.getByRole('option', { name: /^transform/i }).first()
    await expect(transform).toContainText('Add next step after events')
    await transform.click()

    await expect.poll(async () => {
      const response = await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)
      expect(response.ok()).toBeTruthy()
      const graph = await response.json() as {
        nodes: Array<{ id: string; type: string }>
        edges: Array<{ source: string; target: string; data?: { wire?: string } }>
      }
      const typeById = new Map(graph.nodes.map((node) => [node.id, node.type]))
      return {
        nodeTypes: graph.nodes.map((node) => node.type).sort(),
        edges: graph.edges.map((edge) => ({
          source: typeById.get(edge.source), target: typeById.get(edge.target), wire: edge.data?.wire,
        })),
      }
    }).toEqual({
      nodeTypes: ['source', 'transform'],
      edges: [{ source: 'source', target: 'transform', wire: 'dataset' }],
    })
  })
})
