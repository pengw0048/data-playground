import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  deleteCanvasDraft, MAX_LOCAL_CANVAS_DRAFTS, readCanvasDrafts, writeCanvasDraft,
  type LocalCanvasDraft,
} from './canvasDrafts'

const values = new Map<string, string>()
const storage: Storage = {
  get length() { return values.size },
  clear: () => values.clear(),
  getItem: (key) => values.get(key) ?? null,
  key: (index) => Array.from(values.keys())[index] ?? null,
  removeItem: (key) => { values.delete(key) },
  setItem: (key, value) => { values.set(key, String(value)) },
}

Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })

function draft(principalId: string, canvasId: string, edit = 0): LocalCanvasDraft {
  const doc = { id: canvasId, name: canvasId, version: 1, nodes: [], edges: [] }
  return {
    draftId: canvasId,
    principalId,
    canvasId,
    baseCanvasId: null,
    baseVersion: null,
    name: canvasId,
    doc,
    createAttemptDoc: doc,
    syncState: 'dirty',
    lastLocalEditAt: new Date(Date.UTC(2026, 6, 18, 0, 0, edit)).toISOString(),
  }
}

describe('principal-scoped Canvas draft storage', () => {
  beforeEach(() => values.clear())
  afterEach(() => vi.restoreAllMocks())

  it('recovers multiple drafts in edit order and never reads another principal index', () => {
    expect(writeCanvasDraft(draft('alice', 'a', 1)).ok).toBe(true)
    expect(writeCanvasDraft(draft('alice', 'b', 2)).ok).toBe(true)
    expect(writeCanvasDraft(draft('bob', 'secret', 3)).ok).toBe(true)

    expect(readCanvasDrafts('alice').drafts.map((item) => item.canvasId)).toEqual(['b', 'a'])
    expect(readCanvasDrafts('bob').drafts.map((item) => item.canvasId)).toEqual(['secret'])
  })

  it('updates one Canvas record without duplicating its stable identity', () => {
    expect(writeCanvasDraft(draft('alice', 'a', 1)).ok).toBe(true)
    const changed = draft('alice', 'a', 2)
    changed.name = 'renamed'
    changed.doc = { ...changed.doc, name: 'renamed' }
    expect(writeCanvasDraft(changed).ok).toBe(true)

    expect(readCanvasDrafts('alice').drafts).toMatchObject([{ draftId: 'a', name: 'renamed' }])
  })

  it('enforces a visible bound without evicting an existing draft', () => {
    for (let index = 0; index < MAX_LOCAL_CANVAS_DRAFTS; index += 1) {
      expect(writeCanvasDraft(draft('alice', `draft-${index}`, index)).ok).toBe(true)
    }
    const overflow = writeCanvasDraft(draft('alice', 'overflow', 30))

    expect(overflow).toMatchObject({ ok: false })
    expect(overflow.error).toContain(`${MAX_LOCAL_CANVAS_DRAFTS}-draft browser limit`)
    expect(readCanvasDrafts('alice').drafts).toHaveLength(MAX_LOCAL_CANVAS_DRAFTS)
  })

  it('surfaces an isolated corrupt record while retaining readable drafts', () => {
    writeCanvasDraft(draft('alice', 'good', 1))
    writeCanvasDraft(draft('alice', 'bad', 2))
    const badKey = Array.from(values.keys()).find((key) => key.endsWith(':bad'))!
    values.set(badKey, '{broken')

    const result = readCanvasDrafts('alice')
    expect(result.drafts.map((item) => item.draftId)).toEqual(['good'])
    expect(result.errors).toHaveLength(1)
    expect(result.errors[0]).toContain('corrupt')
  })

  it('reports quota failure and does not add an unreachable index entry', () => {
    const realSet = storage.setItem.bind(storage)
    const quota = new DOMException('full', 'QuotaExceededError')
    const spy = vi.spyOn(storage, 'setItem').mockImplementation((key, value) => {
      if (key.includes('drafts-v1')) throw quota
      realSet(key, value)
    })

    const result = writeCanvasDraft(draft('alice', 'quota'))
    expect(result.ok).toBe(false)
    expect(result.error).toContain('quota')
    expect(readCanvasDrafts('alice').drafts).toEqual([])
    spy.mockRestore()
  })

  it('deletes only the selected principal and Canvas record', () => {
    writeCanvasDraft(draft('alice', 'same'))
    writeCanvasDraft(draft('bob', 'same'))
    expect(deleteCanvasDraft('alice', 'same').ok).toBe(true)

    expect(readCanvasDrafts('alice').drafts).toEqual([])
    expect(readCanvasDrafts('bob').drafts).toHaveLength(1)
  })
})
