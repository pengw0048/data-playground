import type { CanvasDoc } from '../types/graph'

export const MAX_LOCAL_CANVAS_DRAFTS = 20

const INDEX_VERSION = 1
const INDEX_KEY = (principalId: string) => `dp-canvas-drafts-v1:${encodeURIComponent(principalId)}`
const RECORD_KEY = (principalId: string, draftId: string) => (
  `dp-canvas-draft-v1:${encodeURIComponent(principalId)}:${encodeURIComponent(draftId)}`
)

export type CanvasDraftSyncState = 'dirty' | 'syncing' | 'conflict' | 'error'

export interface LocalCanvasDraft {
  draftId: string
  principalId: string
  canvasId: string
  baseCanvasId: string | null
  baseVersion: number | null
  name: string
  doc: CanvasDoc
  /** Exact document sent by the first idempotent create attempt. */
  createAttemptDoc: CanvasDoc | null
  syncState: CanvasDraftSyncState
  lastLocalEditAt: string
  lastError?: string
}

interface DraftIndex {
  version: typeof INDEX_VERSION
  ids: string[]
}

export interface DraftReadResult {
  drafts: LocalCanvasDraft[]
  errors: string[]
}

export interface DraftWriteResult {
  ok: boolean
  error?: string
}

function storageMessage(action: string, error: unknown): string {
  const errorName = error && typeof error === 'object' && 'name' in error ? String(error.name) : ''
  const detail = errorName === 'QuotaExceededError'
    ? 'browser storage quota was exceeded'
    : errorName === 'SyntaxError'
      ? 'draft data is corrupt'
    : error instanceof Error && error.message
      ? error.message
      : 'browser storage is unavailable'
  return `Could not ${action} local Canvas draft: ${detail}. The draft is not saved in this browser.`
}

function readIndex(principalId: string): { index: DraftIndex; error?: string } {
  try {
    const raw = localStorage.getItem(INDEX_KEY(principalId))
    if (!raw) return { index: { version: INDEX_VERSION, ids: [] } }
    const parsed = JSON.parse(raw) as Partial<DraftIndex>
    if (parsed.version !== INDEX_VERSION || !Array.isArray(parsed.ids)
      || parsed.ids.some((id) => typeof id !== 'string' || !id)) {
      throw new Error('draft index is corrupt')
    }
    return { index: { version: INDEX_VERSION, ids: Array.from(new Set(parsed.ids)) } }
  } catch (error) {
    return {
      index: { version: INDEX_VERSION, ids: [] },
      error: storageMessage('read the', error),
    }
  }
}

function isCanvasDoc(value: unknown): value is CanvasDoc {
  if (!value || typeof value !== 'object') return false
  const doc = value as Partial<CanvasDoc>
  return typeof doc.id === 'string' && doc.id.length > 0
    && typeof doc.version === 'number' && Number.isInteger(doc.version) && doc.version >= 1
    && Array.isArray(doc.nodes) && Array.isArray(doc.edges)
}

function parseDraft(raw: string, principalId: string, draftId: string): LocalCanvasDraft {
  const value = JSON.parse(raw) as Partial<LocalCanvasDraft>
  if (value.draftId !== draftId || value.principalId !== principalId
    || value.canvasId !== draftId || !isCanvasDoc(value.doc)
    || value.doc.id !== value.canvasId || typeof value.name !== 'string'
    || (value.baseCanvasId !== null && value.baseCanvasId !== value.canvasId)
    || (value.baseVersion !== null && (!Number.isInteger(value.baseVersion) || Number(value.baseVersion) < 1))
    || (value.createAttemptDoc !== null && !isCanvasDoc(value.createAttemptDoc))
    || !['dirty', 'syncing', 'conflict', 'error'].includes(String(value.syncState))
    || typeof value.lastLocalEditAt !== 'string') {
    throw new Error('draft record is corrupt')
  }
  return value as LocalCanvasDraft
}

export function readCanvasDrafts(principalId: string): DraftReadResult {
  const { index, error } = readIndex(principalId)
  const errors = error ? [error] : []
  const drafts: LocalCanvasDraft[] = []
  if (error) return { drafts, errors }
  for (const draftId of index.ids) {
    try {
      const raw = localStorage.getItem(RECORD_KEY(principalId, draftId))
      if (!raw) throw new Error('draft record is missing')
      const draft = parseDraft(raw, principalId, draftId)
      // A tab/browser restart cannot still have an in-flight request. Make retry truth explicit.
      drafts.push(draft.syncState === 'syncing'
        ? { ...draft, syncState: 'dirty', lastError: 'The previous sync did not finish. Retry when the hub is reachable.' }
        : draft)
    } catch (recordError) {
      errors.push(storageMessage(`read ${draftId}`, recordError))
    }
  }
  drafts.sort((a, b) => b.lastLocalEditAt.localeCompare(a.lastLocalEditAt))
  return { drafts, errors }
}

export function writeCanvasDraft(draft: LocalCanvasDraft): DraftWriteResult {
  if (!draft.principalId || draft.draftId !== draft.canvasId || draft.doc.id !== draft.canvasId) {
    return { ok: false, error: 'Could not save local Canvas draft: invalid draft identity.' }
  }
  const { index, error } = readIndex(draft.principalId)
  if (error) return { ok: false, error }
  const exists = index.ids.includes(draft.draftId)
  if (!exists && index.ids.length >= MAX_LOCAL_CANVAS_DRAFTS) {
    return {
      ok: false,
      error: `Could not save local Canvas draft: the ${MAX_LOCAL_CANVAS_DRAFTS}-draft browser limit was reached. Export or delete a draft first.`,
    }
  }
  const recordKey = RECORD_KEY(draft.principalId, draft.draftId)
  let previous: string | null
  try {
    previous = localStorage.getItem(recordKey)
  } catch (readError) {
    return { ok: false, error: storageMessage('read the existing', readError) }
  }
  try {
    localStorage.setItem(recordKey, JSON.stringify(draft))
    if (!exists) {
      localStorage.setItem(INDEX_KEY(draft.principalId), JSON.stringify({
        version: INDEX_VERSION,
        ids: [...index.ids, draft.draftId],
      } satisfies DraftIndex))
    }
    return { ok: true }
  } catch (writeError) {
    // Avoid leaving a newly written but unreachable record when updating the index failed.
    try {
      if (previous === null) localStorage.removeItem(recordKey)
      else localStorage.setItem(recordKey, previous)
    } catch { /* the original visible error remains authoritative */ }
    return { ok: false, error: storageMessage('save the', writeError) }
  }
}

export function deleteCanvasDraft(principalId: string, draftId: string): DraftWriteResult {
  const { index, error } = readIndex(principalId)
  if (error) return { ok: false, error }
  const indexKey = INDEX_KEY(principalId)
  const recordKey = RECORD_KEY(principalId, draftId)
  let previousIndex: string | null = null
  let previousRecord: string | null = null
  try {
    previousIndex = localStorage.getItem(indexKey)
    previousRecord = localStorage.getItem(recordKey)
    localStorage.removeItem(recordKey)
    localStorage.setItem(indexKey, JSON.stringify({
      version: INDEX_VERSION,
      ids: index.ids.filter((id) => id !== draftId),
    } satisfies DraftIndex))
    return { ok: true }
  } catch (deleteError) {
    try {
      if (previousIndex === null) localStorage.removeItem(indexKey)
      else localStorage.setItem(indexKey, previousIndex)
      if (previousRecord === null) localStorage.removeItem(recordKey)
      else localStorage.setItem(recordKey, previousRecord)
    } catch { /* the original visible error remains authoritative */ }
    return { ok: false, error: storageMessage('delete the', deleteError) }
  }
}

export function canvasDocsEqual(left: CanvasDoc, right: CanvasDoc): boolean {
  return JSON.stringify(left) === JSON.stringify(right)
}
