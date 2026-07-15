import { describe, expect, it } from 'vitest'

import './section'
import { getSpec } from '../registry'

describe('section document shape', () => {
  it('writes only parentId containment and no inline subnodes field', () => {
    const config = getSpec('section')!.defaultData().config
    expect(config).not.toHaveProperty('subnodes')
    expect(config).toMatchObject({ params: {}, maxRuns: 200, outputs: ['out'] })
  })
})
