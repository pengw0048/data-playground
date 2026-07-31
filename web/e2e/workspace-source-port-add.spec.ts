import { expect, test, type Page } from '@playwright/test'
import { goToWorkspace, workspaceResource } from './support/workspace'

async function expectToolbarInsideCanvas(page: Page, viewportWidth: number) {
  const toolbar = page.getByTestId('toolbar')
  const canvas = page.locator('.react-flow')
  const globalAdd = page.getByRole('button', { name: 'Add operation', exact: true })
  const [toolbarBox, canvasBox, addBox] = await Promise.all([
    toolbar.boundingBox(), canvas.boundingBox(), globalAdd.boundingBox(),
  ])
  expect(toolbarBox).not.toBeNull()
  expect(canvasBox).not.toBeNull()
  expect(addBox).not.toBeNull()
  expect(toolbarBox!.x).toBeGreaterThanOrEqual(canvasBox!.x - 0.5)
  expect(toolbarBox!.x + toolbarBox!.width).toBeLessThanOrEqual(canvasBox!.x + canvasBox!.width + 0.5)
  expect(addBox!.x).toBeGreaterThanOrEqual(canvasBox!.x - 0.5)
  expect(addBox!.x + addBox!.width).toBeLessThanOrEqual(canvasBox!.x + canvasBox!.width + 0.5)
  expect(canvasBox!.x + canvasBox!.width).toBeLessThan(viewportWidth)
}

test.describe('Workspace Source port-add flow @ux-smoke', () => {
  test('keeps creation global and connects a local Transform from the selected Source port', async ({ page }) => {
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
    const viewportControls = page.getByTestId('canvas-viewport-controls')
    const globalAdd = page.getByRole('button', { name: 'Add operation', exact: true })
    const sourcePort = page.getByRole('button', { name: 'Add operation from dataset output' })
    await expect(page.getByRole('button', { name: 'Collapse Inspector' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Add next step' })).toHaveCount(0)
    await expect(globalAdd).toBeVisible()
    await expect(globalAdd).toHaveText('')
    await expect(sourcePort).toBeVisible()
    await expect(toolbar).toHaveAttribute('data-density', 'comfortable')
    await expect(viewportControls.getByRole('button', { name: 'Fit view', exact: true })).toBeVisible()
    await expect(page.getByTestId('toolbar-view-controls')).toHaveCount(0)
    await expect(page.getByTestId('inspector').getByRole('button', { name: 'Find join candidates' })).toBeVisible()
    await expect(page.getByTestId(/^join-with-related-canvas-/)).toHaveCount(0)
    await expectToolbarInsideCanvas(page, 1280)

    await page.setViewportSize({ width: 1440, height: 900 })
    await expect(toolbar).toHaveAttribute('data-density', 'comfortable')
    await expectToolbarInsideCanvas(page, 1440)

    await sourcePort.press('Enter')
    const finder = page.getByRole('dialog', { name: 'Connect to an operation' })
    await expect(finder).not.toHaveAttribute('aria-modal')
    await expect(page.locator('.dp-modal-overlay')).toHaveCount(0)
    await finder.getByRole('textbox', { name: 'Search operations' }).fill('transform')
    await finder.getByRole('option', { name: /^transform/i }).first().click()

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
