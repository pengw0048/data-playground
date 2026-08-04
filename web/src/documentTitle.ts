import type { RelationshipsMode } from './router'

/** Product suffix shared by every committed browser title. */
export const PRODUCT_TITLE = 'Data Playground'

/** Display name when a Canvas title is missing or whitespace-only. */
export const UNTITLED_CANVAS_TITLE = 'Untitled'

/** Neutral title while identity is unresolved or no product context is ready. */
export const NEUTRAL_DOCUMENT_TITLE = PRODUCT_TITLE

/** Bound the Canvas subject only; the stored Canvas name is never mutated. */
export const MAX_CANVAS_TITLE_GRAPHEMES = 60

const TITLE_SEP = ' · '

const titleSegmenter = typeof Intl !== 'undefined' && 'Segmenter' in Intl
  ? new Intl.Segmenter(undefined, { granularity: 'grapheme' })
  : null

export type DocumentTitlePhase =
  | 'checking'
  | 'unavailable'
  | 'login'
  | 'bootstrapping'
  | 'ready'

/** Mirrors `DpView` without importing the store module. */
export type DocumentTitleView =
  | 'canvas'
  | 'workspace'
  | 'jobs'
  | 'inbox'
  | 'files'
  | 'transforms'
  | 'relationships'

export interface DocumentTitleState {
  phase: DocumentTitlePhase
  view: DocumentTitleView
  /** Committed open Canvas id (`doc.id`). */
  canvasId: string
  /** Committed open Canvas name (`doc.name`). */
  canvasName: string | null | undefined
  /**
   * Canvas id currently claimed by the URL hash, when the hash is a Canvas deep link.
   * When this differs from `canvasId`, the previous Canvas name must not appear in the tab.
   */
  routeCanvasId?: string | null
  relationshipsMode?: RelationshipsMode
}

/** Format `<label> · Data Playground` without mutating the label. */
export function productDocumentTitle(label: string): string {
  return `${label}${TITLE_SEP}${PRODUCT_TITLE}`
}

/** Trim and map empty/whitespace Canvas names to Untitled; bound grapheme length for the tab. */
export function displayCanvasTitleName(name: string | null | undefined): string {
  const trimmed = (name ?? '').trim()
  if (!trimmed) return UNTITLED_CANVAS_TITLE
  return boundGraphemes(trimmed, MAX_CANVAS_TITLE_GRAPHEMES)
}

function boundGraphemes(value: string, max: number): string {
  if (max < 1) return '…'
  const graphemes = titleSegmenter
    ? Array.from(titleSegmenter.segment(value), ({ segment }) => segment)
    : Array.from(value)
  if (graphemes.length <= max) return value
  return `${graphemes.slice(0, max - 1).join('')}…`
}

function shellTitle(view: DocumentTitleView, relationshipsMode?: RelationshipsMode): string {
  switch (view) {
    case 'workspace':
    case 'files':
      return 'Workspace'
    case 'jobs':
      return 'Jobs'
    case 'inbox':
      return 'Inbox'
    case 'transforms':
      return 'Transforms'
    case 'relationships':
      return relationshipsMode === 'lineage' ? 'Lineage' : 'Relationships'
    case 'canvas':
      return UNTITLED_CANVAS_TITLE
  }
}

/**
 * Pure projection from committed router/store (+ URL canvas claim) to `document.title`.
 * Does not write history or mutate Canvas metadata.
 */
export function projectDocumentTitle(state: DocumentTitleState): string {
  if (state.phase === 'checking' || state.phase === 'unavailable' || state.phase === 'bootstrapping') {
    return NEUTRAL_DOCUMENT_TITLE
  }
  if (state.phase === 'login') return productDocumentTitle('Sign in')

  if (state.view !== 'canvas') {
    return productDocumentTitle(shellTitle(state.view, state.relationshipsMode))
  }

  const routeId = state.routeCanvasId?.trim() || null
  // A deep link (or in-flight open) owns the tab before `doc` catches up. Keep the previous
  // private Canvas name out of the tab until the committed id matches the route claim.
  if (routeId && routeId !== state.canvasId) return NEUTRAL_DOCUMENT_TITLE

  return productDocumentTitle(displayCanvasTitleName(state.canvasName))
}
