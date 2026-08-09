import { describe, expect, it } from 'vitest'
import { canvasFitOptions, rightViewportShiftToReveal } from './viewportFit'

describe('canvasFitOptions', () => {
  it('fully fits small graphs but preserves a readable scale for dense graphs', () => {
    expect(canvasFitOptions(7)).toEqual({ padding: 0.3, maxZoom: 1 })
    expect(canvasFitOptions(8)).toEqual({ padding: 0.3, minZoom: 0.6, maxZoom: 1 })
  })
})

describe('rightViewportShiftToReveal', () => {
  const viewport = { left: 0, right: 980 }
  const previousViewport = { left: 0, right: 1_280 }

  it('returns only the translation needed at the Inspector edge', () => {
    expect(rightViewportShiftToReveal({ left: 965, right: 1134 }, viewport, previousViewport)).toBe(-166)
  })

  it('leaves visible, previously clipped, left-clipped, and impossible oversized nodes alone', () => {
    expect(rightViewportShiftToReveal({ left: 12, right: 968 }, viewport, previousViewport)).toBeNull()
    expect(rightViewportShiftToReveal({ left: 965, right: 1134 }, viewport, { left: 0, right: 1_100 })).toBeNull()
    expect(rightViewportShiftToReveal({ left: -20, right: 149 }, viewport, previousViewport)).toBeNull()
    expect(rightViewportShiftToReveal({ left: -20, right: 1_020 }, viewport, previousViewport)).toBeNull()
  })
})
