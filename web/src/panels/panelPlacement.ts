export const DATA_PANEL_USEFUL_CONTENT_HEIGHT = 360

const EDGE = 12
const MAX_PANEL_HEIGHT = 620
const PANEL_TITLE_HEIGHT = 41
const MIN_DATA_PANEL_WIDTH = 400

export interface Rect {
  left: number
  right: number
  top: number
  bottom: number
}

export interface DataPanelPlacement {
  presentation: 'anchored' | 'docked'
  left: number
  top: number
  width: number
  maxHeight: number
}

export function dataPanelPlacement({
  anchor, width, viewportWidth, viewportHeight, rightEdge, toolbarTop,
}: {
  anchor: Rect
  width: number
  viewportWidth: number
  viewportHeight: number
  rightEdge: number
  toolbarTop?: number
}): DataPanelPlacement {
  // The docked panel must leave the product toolbar clickable. Its coordinate is a real DOM
  // measurement when available; the viewport edge keeps first paint bounded before that settles.
  const safeBottom = Math.min(viewportHeight - 16, (toolbarTop ?? viewportHeight) - EDGE)
  const availableRightWidth = rightEdge - anchor.right - EDGE * 2
  const rightPlacementFits = availableRightWidth >= MIN_DATA_PANEL_WIDTH
  const anchoredWidth = rightPlacementFits ? Math.min(width, availableRightWidth) : width
  const left = rightPlacementFits
    ? anchor.right + EDGE
    : Math.max(EDGE, Math.min(anchor.left, rightEdge - width - EDGE))
  const top = Math.max(EDGE, rightPlacementFits ? anchor.top : anchor.bottom + EDGE)
  const anchoredMaxHeight = Math.max(0, Math.min(MAX_PANEL_HEIGHT, safeBottom - top))

  if (anchoredMaxHeight - PANEL_TITLE_HEIGHT >= DATA_PANEL_USEFUL_CONTENT_HEIGHT) {
    return { presentation: 'anchored', left, top, width: anchoredWidth, maxHeight: anchoredMaxHeight }
  }

  // Keep a useful viewer inside the same canvas/Inspector boundary rather than opening a one-row
  // sliver below a node. At the supported viewport this gives DataPanel up to 579px of content.
  return {
    presentation: 'docked',
    left: Math.max(EDGE, Math.min(rightEdge - width - EDGE, viewportWidth - width - EDGE)),
    top: EDGE,
    width,
    maxHeight: Math.max(0, Math.min(MAX_PANEL_HEIGHT, safeBottom - EDGE)),
  }
}
