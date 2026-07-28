import { describe, expect, it } from 'vitest'
import type { CanvasNode } from '../types/graph'
import {
  TOOLBAR_ACTION_SHELF_HEIGHT, TOOLBAR_NODE_HEIGHT, toolbarSafePosition,
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
})
