import { describe, expect, it } from 'vitest'
import type { CanvasNode } from '../types/graph'
import {
  TOOLBAR_ACTION_SHELF_HEIGHT, TOOLBAR_NODE_HEIGHT, TOOLBAR_NODE_WIDTH, toolbarSafePosition,
} from './toolbarPlacement'

const bounds = { left: 0, top: 0, right: 1280, bottom: 650 }
const node = (id: string, type: string, position: { x: number; y: number }): CanvasNode => ({
  id, type, position, data: { title: type, status: 'draft', config: {} },
})

describe('toolbarSafePosition', () => {
  it('keeps repeated global-toolbar additions and their action shelves above the bottom toolbar', () => {
    const first = toolbarSafePosition([], { x: 524, y: 320 }, bounds)
    const second = toolbarSafePosition([node('source', 'source', first)], { x: 524, y: 320 }, bounds)
    const third = toolbarSafePosition([
      node('source', 'source', first),
      node('filter', 'filter', second),
    ], { x: 524, y: 320 }, bounds)

    expect([first, second, third]).toEqual([
      { x: 524, y: 320 },
      { x: 944, y: 320 },
      { x: 104, y: 320 },
    ])
    for (const position of [first, second, third]) {
      expect(position.y + TOOLBAR_NODE_HEIGHT + 5 + TOOLBAR_ACTION_SHELF_HEIGHT).toBeLessThanOrEqual(bounds.bottom - 12)
    }
  })

  it('uses the remaining visible strip when the Inspector narrows the Canvas', () => {
    const narrowed = { left: 150, top: 15.5, right: 1130, bottom: 665.5 }
    const position = toolbarSafePosition([
      node('source', 'source', { x: 524, y: 320 }),
      node('filter', 'filter', { x: 884, y: 335.5 }),
    ], { x: 674, y: 335.5 }, narrowed)

    expect(position).toEqual({ x: 162, y: 335.5 })
    expect(position.x + TOOLBAR_NODE_WIDTH).toBeLessThanOrEqual(narrowed.right - 12)
    expect(position.y + TOOLBAR_NODE_HEIGHT + 5 + TOOLBAR_ACTION_SHELF_HEIGHT).toBeLessThanOrEqual(narrowed.bottom - 12)
  })

  it('moves by the complete interaction footprint after pan leaves no free horizontal slot', () => {
    const panned = { left: 472, top: 1083.5, right: 1452, bottom: 1733.5 }
    const position = toolbarSafePosition([
      node('source', 'source', { x: 996, y: 1403.5 }),
      node('filter', 'filter', { x: 576, y: 1403.5 }),
    ], { x: 996, y: 1403.5 }, panned)

    expect(position).toEqual({ x: 996, y: 1160.5 })
    expect(position.y + TOOLBAR_NODE_HEIGHT + 5 + TOOLBAR_ACTION_SHELF_HEIGHT).toBeLessThanOrEqual(panned.bottom - 12)
  })
})
