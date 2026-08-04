import { describe, expect, it } from 'vitest'
import { newCanvasFileKey } from './fileKey'

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

describe('newCanvasFileKey', () => {
  it('mints full-strength UUID v4 values with no duplicates across independent batches', () => {
    const batchA = Array.from({ length: 2_000 }, () => newCanvasFileKey())
    const batchB = Array.from({ length: 2_000 }, () => newCanvasFileKey())
    for (const key of [...batchA, ...batchB]) expect(key).toMatch(UUID_V4)
    expect(new Set(batchA).size).toBe(batchA.length)
    expect(new Set(batchB).size).toBe(batchB.length)
    const seen = new Set(batchA)
    expect(batchB.some((key) => seen.has(key))).toBe(false)
  })
})
