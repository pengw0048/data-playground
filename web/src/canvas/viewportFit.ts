const DENSE_GRAPH_NODE_COUNT = 8
const SELECTED_NODE_GUTTER = 12

type HorizontalExtent = { left: number; right: number }

/** Return the smallest screen-space translation that uncovers a node at the Inspector edge. */
export function rightViewportShiftToReveal(
  node: HorizontalExtent,
  viewport: HorizontalExtent,
  previousViewport: HorizontalExtent,
): number | null {
  const safeLeft = viewport.left + SELECTED_NODE_GUTTER
  const safeRight = viewport.right - SELECTED_NODE_GUTTER
  if (node.right - node.left > safeRight - safeLeft) return null
  if (node.right > previousViewport.right - SELECTED_NODE_GUTTER) return null
  if (node.right > safeRight) return safeRight - node.right
  return null
}

// Keep automatic fitting useful as a navigation action. A small graph still fits in full. Once a
// graph is dense enough to navigate rather than read all at once, Fit view stops before card text
// becomes illegible; the minimap and pan expose the rest. Manual zoom-out remains unchanged.
export function canvasFitOptions(nodeCount: number) {
  return nodeCount >= DENSE_GRAPH_NODE_COUNT
    ? { padding: 0.3, minZoom: 0.6, maxZoom: 1 } as const
    : { padding: 0.3, maxZoom: 1 } as const
}
