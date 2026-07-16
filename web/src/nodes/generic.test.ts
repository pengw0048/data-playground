import { describe, expect, it } from 'vitest'
import { portMulti } from './registry'
import { registerGenericNodes } from './generic'

describe('generic node registration', () => {
  it('preserves a plugin multi-input descriptor for canvas connection validation', () => {
    registerGenericNodes([{
      kind: 'plugin-multi-input-contract', title: 'Plugin multi input', category: 'compute',
      inputs: [{ id: 'items', label: 'Items', wire: 'dataset', accepts: ['dataset'], multi: true }],
      outputs: [{ id: 'out', wire: 'dataset' }], params: [],
      canBypass: false, previewable: true, blurb: '',
    }])

    expect(portMulti('plugin-multi-input-contract', 'items')).toBe(true)
  })
})
