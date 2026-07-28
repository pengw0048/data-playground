const DENSE_GRAPH_NODE_COUNT = 8

// Keep automatic fitting useful as a navigation action. A small graph still fits in full. Once a
// graph is dense enough to navigate rather than read all at once, Fit view stops before card text
// becomes illegible; the minimap and pan expose the rest. Manual zoom-out remains unchanged.
export function canvasFitOptions(nodeCount: number) {
  return nodeCount >= DENSE_GRAPH_NODE_COUNT
    ? { padding: 0.3, minZoom: 0.6, maxZoom: 1 } as const
    : { padding: 0.3, maxZoom: 1 } as const
}
