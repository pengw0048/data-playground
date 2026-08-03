import { describe, expect, it } from 'vitest'
import { nodeTypeIconName } from './NodeTypeIcon'

describe('nodeTypeIconName', () => {
  it('uses recognizable operation icons instead of generated letter tiles', () => {
    expect(nodeTypeIconName({ kind: 'filter', category: 'shape' })).toBe('filter')
    expect(nodeTypeIconName({ kind: 'join', category: 'shape' })).toBe('join')
    expect(nodeTypeIconName({ kind: 'assert', category: 'inspect' })).toBe('check')
    expect(nodeTypeIconName({ kind: 'chart', category: 'inspect' })).toBe('chart')
  })

  it('falls back to the plugin category icon', () => {
    expect(nodeTypeIconName({ kind: 'plugin-operation', category: 'compute' })).toBe('fx')
  })
})
