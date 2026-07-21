import type { CanvasNode } from '../types/graph'

export type NodeViewport = {
  getZoom: () => number
  setCenter: (x: number, y: number, options: { zoom: number; duration: number }) => void
}

/**
 * Resolve a node's canvas-space position, including every containing section.
 * Documents imported from older clients can contain more than one parent level,
 * even though the current drag interaction creates only one visual level.
 */
export function absoluteNodePosition(nodes: CanvasNode[], node: CanvasNode): { x: number; y: number } {
  const byId = new Map(nodes.map((candidate) => [candidate.id, candidate]))
  const seen = new Set<string>()
  let current = node
  let x = current.position.x
  let y = current.position.y
  while (current.parentId && !seen.has(current.parentId)) {
    seen.add(current.parentId)
    const parent = byId.get(current.parentId)
    if (!parent) break
    x += parent.position.x
    y += parent.position.y
    current = parent
  }
  return { x, y }
}

/** Locate one existing card without fitting the entire graph or persisting a viewport change. */
export function locateNode(nodes: CanvasNode[], id: string, viewport: NodeViewport): boolean {
  const node = nodes.find((candidate) => candidate.id === id)
  if (!node) return false
  const position = absoluteNodePosition(nodes, node)
  viewport.setCenter(position.x + 116, position.y + 72, {
    zoom: Math.max(0.8, Math.min(viewport.getZoom(), 1.3)),
    duration: 350,
  })
  return true
}
