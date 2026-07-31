import { describe, expect, it, vi } from 'vitest'
import { absoluteNodePosition, locateNode } from './locateNode'
import type { CanvasNode } from '../types/graph'

function node(id: string, x: number, y: number, parentId?: string): CanvasNode {
  return {
    id, type: 'source', position: { x, y }, parentId,
    data: { title: id, status: 'idle', config: {} },
  } as CanvasNode
}

describe('locateNode', () => {
  it('uses the absolute position through nested sections and bounded existing zoom', async () => {
    const nodes = [node('outer', 100, 200), node('inner', 30, 40, 'outer'), node('target', 10, 20, 'inner')]
    const viewport = { getZoom: () => 2, setCenter: vi.fn().mockResolvedValue(true) }

    expect(absoluteNodePosition(nodes, nodes[2])).toEqual({ x: 140, y: 260 })
    await expect(locateNode(nodes, 'target', viewport)).resolves.toBe(true)
    expect(viewport.setCenter).toHaveBeenCalledWith(256, 332, { zoom: 1.3, duration: 350 })
  })

  it('does not move the viewport for a missing node', async () => {
    const viewport = { getZoom: () => 1, setCenter: vi.fn().mockResolvedValue(true) }
    await expect(locateNode([node('present', 0, 0)], 'missing', viewport)).resolves.toBe(false)
    expect(viewport.setCenter).not.toHaveBeenCalled()
  })
})
