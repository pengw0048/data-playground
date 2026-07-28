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
  anchor, width, viewportWidth, viewportHeight, rightEdge, toolbarTop, canvasTop,
}: {
  anchor: Rect
  width: number
  viewportWidth: number
  viewportHeight: number
  rightEdge: number
  toolbarTop?: number
  canvasTop?: number
}): DataPanelPlacement {
  // Leave both measured Canvas chrome and the product toolbar clickable. Viewport edges keep first
  // paint bounded before either DOM measurement settles.
  const safeTop = Math.max(EDGE, (canvasTop ?? 0) + EDGE)
  const safeBottom = Math.min(viewportHeight - 16, (toolbarTop ?? viewportHeight) - EDGE)
  const availableRightWidth = rightEdge - anchor.right - EDGE * 2
  const rightPlacementFits = availableRightWidth >= MIN_DATA_PANEL_WIDTH
  const anchoredWidth = rightPlacementFits ? Math.min(width, availableRightWidth) : width
  const left = rightPlacementFits
    ? anchor.right + EDGE
    : Math.max(EDGE, Math.min(anchor.left, rightEdge - width - EDGE))
  const top = Math.max(safeTop, rightPlacementFits ? anchor.top : anchor.bottom + EDGE)
  const anchoredMaxHeight = Math.max(0, Math.min(MAX_PANEL_HEIGHT, safeBottom - top))

  if (anchoredMaxHeight - PANEL_TITLE_HEIGHT >= DATA_PANEL_USEFUL_CONTENT_HEIGHT) {
    return { presentation: 'anchored', left, top, width: anchoredWidth, maxHeight: anchoredMaxHeight }
  }

  // Keep a useful viewer inside the same canvas/Inspector boundary rather than opening a one-row
  // sliver below a node. Dock opposite the selected node so the remaining graph stays operable.
  const rightDockLeft = Math.max(EDGE, Math.min(rightEdge - width - EDGE, viewportWidth - width - EDGE))
  const dockLeft = (anchor.left + anchor.right) / 2 >= rightEdge / 2 ? EDGE : rightDockLeft
  return {
    presentation: 'docked',
    left: dockLeft,
    top: safeTop,
    width,
    maxHeight: Math.max(0, Math.min(MAX_PANEL_HEIGHT, safeBottom - safeTop)),
  }
}
