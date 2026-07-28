import { expect, test } from '@playwright/test'
import { goToWorkspace, workspaceResource } from './support/workspace'

test.describe('Workspace Source next-step flow @ux-smoke', () => {
  test('creates one selected Source and connects Transform at 1280x720', async ({ page }) => {
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
    await expect(nextStep).toBeVisible()
    await expect(nextStep).toContainText('Add next step')
    const [toolbarBox, nextStepBox] = await Promise.all([toolbar.boundingBox(), nextStep.boundingBox()])
    expect(toolbarBox).not.toBeNull()
    expect(nextStepBox).not.toBeNull()
    expect(toolbarBox!.x).toBeGreaterThanOrEqual(0)
    expect(toolbarBox!.x + toolbarBox!.width).toBeLessThanOrEqual(1280)
    expect(nextStepBox!.x + nextStepBox!.width).toBeLessThanOrEqual(1280)

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
