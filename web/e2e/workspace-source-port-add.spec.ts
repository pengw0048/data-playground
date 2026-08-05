import { expect, test, type Page } from '@playwright/test'
import { canvasIdFromLocation } from './support/canvasRoute'
import { goToWorkspace, workspaceResource } from './support/workspace'

async function expectToolbarInsideCanvas(page: Page, viewportWidth: number) {
  const toolbar = page.getByTestId('toolbar')
  const canvas = page.locator('.react-flow')
  const locator = page.getByRole('button', { name: 'Locate existing node', exact: true })
  const [toolbarBox, canvasBox, locatorBox] = await Promise.all([
    toolbar.boundingBox(), canvas.boundingBox(), locator.boundingBox(),
  ])
  expect(toolbarBox).not.toBeNull()
  expect(canvasBox).not.toBeNull()
  expect(locatorBox).not.toBeNull()
  expect(toolbarBox!.x).toBeGreaterThanOrEqual(canvasBox!.x - 0.5)
  expect(toolbarBox!.x + toolbarBox!.width).toBeLessThanOrEqual(canvasBox!.x + canvasBox!.width + 0.5)
  expect(locatorBox!.x).toBeGreaterThanOrEqual(canvasBox!.x - 0.5)
  expect(locatorBox!.x + locatorBox!.width).toBeLessThanOrEqual(canvasBox!.x + canvasBox!.width + 0.5)
  expect(canvasBox!.x + canvasBox!.width).toBeLessThan(viewportWidth)
}

async function expectSelectedNodeInsideCanvas(page: Page, type: string) {
  const node = page.locator(`.react-flow__node-${type}.selected`)
  const output = node.locator('.react-flow__handle-right[role="button"]')
  await expect(node).toHaveCount(1)
  await expect(output).toBeVisible()
  await expect(output).toHaveText('')
  await expect(page.locator('[data-node-reveal-pending]'))
    .toHaveAttribute('data-node-reveal-pending', 'false')
  await expect.poll(async () => {
    const [nodeBox, outputBox, canvasBox] = await Promise.all([
      node.boundingBox(), output.boundingBox(), page.locator('.react-flow').boundingBox(),
    ])
    if (!nodeBox || !outputBox || !canvasBox) return false
    const left = canvasBox.x - 0.5
    const right = canvasBox.x + canvasBox.width + 0.5
    return nodeBox.x >= left && nodeBox.x + nodeBox.width <= right
      && outputBox.x >= left && outputBox.x + outputBox.width <= right
  }).toBe(true)
  return output
}

test.describe('Workspace Source port-add flow @ux-smoke', () => {
  test('keeps consecutive connected steps visible without a manual fit', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 })
    await goToWorkspace(page)

    // This is the normal researcher route: a Workspace dataset detail, not an API-created graph.
    await (await workspaceResource(page, 'dataset', 'events')).click()
    await page.getByTestId('detail-use').click()
    const useDialog = page.getByRole('dialog', { name: 'Use events' })
    await useDialog.getByRole('button', { name: 'Create and open' }).click()
    await expect(page).toHaveURL(/#\/canvas\//)
    const canvasId = canvasIdFromLocation(page.url())

    const toolbar = page.getByTestId('toolbar')
    const viewportControls = page.getByTestId('canvas-viewport-controls')
    const sourcePort = page.getByRole('button', { name: 'Add operation from dataset output' })
    await expect(page.getByRole('button', { name: 'Collapse Inspector' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Add next step' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Add operation', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Sources & sinks', exact: true })).toBeVisible()
    await expect(sourcePort).toBeVisible()
    await expect(sourcePort).toHaveText('')
    await sourcePort.hover()
    await expect(sourcePort).toHaveText('+')
    await toolbar.hover()
    await expect(sourcePort).toHaveText('')
    await expect(toolbar).toHaveAttribute('data-density', 'comfortable')
    await expect(viewportControls.getByRole('button', { name: 'Fit view', exact: true })).toBeVisible()
    await expect(page.getByTestId('toolbar-view-controls')).toHaveCount(0)
    await expect(page.getByTestId('inspector').getByRole('button', { name: 'Find join candidates' })).toBeVisible()
    await expect(page.getByTestId(/^join-with-related-canvas-/)).toHaveCount(0)
    await expectToolbarInsideCanvas(page, 1280)

    await page.setViewportSize({ width: 1440, height: 900 })
    await expect(toolbar).toHaveAttribute('data-density', 'comfortable')
    await expectToolbarInsideCanvas(page, 1440)

    await page.setViewportSize({ width: 1280, height: 720 })
    await expectToolbarInsideCanvas(page, 1280)
    await sourcePort.focus()
    await expect(sourcePort).toBeFocused()
    await page.keyboard.press('Enter')
    const finder = page.getByRole('dialog', { name: 'Connect to an operation' })
    await expect(finder).not.toHaveAttribute('aria-modal')
    await expect(page.locator('.dp-modal-overlay')).toHaveCount(0)
    await finder.getByRole('textbox', { name: 'Search operations' }).fill('sample')
    await finder.getByRole('option', { name: /^sample/i }).first().click()

    const samplePort = await expectSelectedNodeInsideCanvas(page, 'sample')
    await samplePort.press('Enter')
    await finder.getByRole('textbox', { name: 'Search operations' }).fill('transform')
    await finder.getByRole('option', { name: /^transform/i }).first().click()
    await expectSelectedNodeInsideCanvas(page, 'transform')

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
      nodeTypes: ['sample', 'source', 'transform'],
      edges: [
        { source: 'source', target: 'sample', wire: 'dataset' },
        { source: 'sample', target: 'transform', wire: 'sample' },
      ],
    })
  })
})
