import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { canvasOpenedAt, rememberCanvasOpenedAt } from './canvasRecents'

describe('Canvas recents', () => {
  const values = new Map<string, string>()
  beforeEach(() => {
    values.clear()
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) },
      removeItem: (key: string) => { values.delete(key) },
      clear: () => values.clear(),
    })
  })
  afterEach(() => vi.unstubAllGlobals())

  it('records the last open time per user and Canvas', () => {
    rememberCanvasOpenedAt('alice', 'canvas-1', new Date('2026-08-01T12:00:00Z'))
    expect(canvasOpenedAt('alice', 'canvas-1')).toBe('2026-08-01T12:00:00.000Z')
    expect(canvasOpenedAt('bob', 'canvas-1')).toBeNull()
  })

  it('ignores malformed device-local metadata', () => {
    localStorage.setItem('dp-opened-at-alice', '{not-json')
    expect(canvasOpenedAt('alice', 'canvas-1')).toBeNull()
  })
})
