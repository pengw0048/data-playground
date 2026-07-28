import { describe, expect, it } from 'vitest'
import { canvasFitOptions } from './viewportFit'

describe('canvasFitOptions', () => {
  it('fully fits small graphs but preserves a readable scale for dense graphs', () => {
    expect(canvasFitOptions(7)).toEqual({ padding: 0.3, maxZoom: 1 })
    expect(canvasFitOptions(8)).toEqual({ padding: 0.3, minZoom: 0.6, maxZoom: 1 })
  })
})
