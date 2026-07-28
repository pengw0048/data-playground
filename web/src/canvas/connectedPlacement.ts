import type { CanvasNode } from '../types/graph'

// Cards are 232px wide. Leave room for their handles and a readable wire.
export const CONNECTED_NODE_OFFSET = 352

/**
 * Put a newly product-created target after every direct upstream card. This is deliberately a
 * placement hint, not a graph layout: callers apply it only to nodes still marked autoPlaced.
 */
export function connectedBasePosition(upstream: CanvasNode[]): { x: number; y: number } | null {
  const topLevel = upstream.filter((node) => !node.parentId)
  if (!topLevel.length) return null
  return {
    x: Math.max(...topLevel.map((node) => node.position.x)) + CONNECTED_NODE_OFFSET,
    y: topLevel.reduce((sum, node) => sum + node.position.y, 0) / topLevel.length,
  }
}
