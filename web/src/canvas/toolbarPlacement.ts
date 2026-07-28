import type { CanvasNode } from '../types/graph'

// NodeCard is 232px wide. Reserve the tallest first-party card plus its below-card action shelf,
// rather than relying on stacking order when the global toolbar occupies the bottom of the Canvas.
export const TOOLBAR_NODE_WIDTH = 232
export const TOOLBAR_NODE_HEIGHT = 200
export const TOOLBAR_ACTION_SHELF_HEIGHT = 38
const SAFE_GUTTER = 12
const FREE_WIDTH = 280
const FREE_HEIGHT = 180

export type ToolbarSafeBounds = { left: number; top: number; right: number; bottom: number }

function clashes(nodes: readonly CanvasNode[], x: number, y: number) {
  return nodes.some((node) => (
    Math.abs(node.position.x - x) < FREE_WIDTH && Math.abs(node.position.y - y) < FREE_HEIGHT
  ))
}

function candidates(base: { x: number; y: number }) {
  const positions = [base]
  const directions = [[1, 0], [0, 1], [1, 1], [-1, 0], [-1, 1], [0, -1], [1, -1], [-1, -1]]
  for (let radius = 1; radius < 50; radius++) {
    for (const [dx, dy] of directions) {
      positions.push({ x: base.x + dx * FREE_WIDTH * radius * 0.75, y: base.y + dy * FREE_HEIGHT * radius * 0.9 })
    }
  }
  return positions
}

function isInsideSafeBounds(position: { x: number; y: number }, bounds: ToolbarSafeBounds) {
  return position.x >= bounds.left + SAFE_GUTTER
    && position.y >= bounds.top + SAFE_GUTTER
    && position.x + TOOLBAR_NODE_WIDTH <= bounds.right - SAFE_GUTTER
    && position.y + TOOLBAR_NODE_HEIGHT + 5 + TOOLBAR_ACTION_SHELF_HEIGHT <= bounds.bottom - SAFE_GUTTER
}

/**
 * Finds the usual nearby free position, but only within the Canvas area that remains clickable
 * above the fixed bottom toolbar. This applies exclusively to nodes created by the global toolbar.
 */
export function toolbarSafePosition(
  nodes: readonly CanvasNode[], base: { x: number; y: number }, bounds: ToolbarSafeBounds,
): { x: number; y: number } {
  const positions = candidates(base)
  const safe = positions.find((position) => !clashes(nodes, position.x, position.y) && isInsideSafeBounds(position, bounds))
  if (safe) return safe

  // Keep the pre-existing free-position behavior when the visible region is genuinely full.
  return positions.find((position) => !clashes(nodes, position.x, position.y)) ?? base
}
