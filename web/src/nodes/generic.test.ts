import { describe, expect, it } from 'vitest'
import { getSpec, portMulti } from './registry'
import contractDescriptors from '../../../examples/plugins/dp_descriptor_contract/descriptor.json'
import type { BackendNodeSpec } from '../api/client'
import { getBackendSpec, nodeInvalidReason, parseNumericParam, registerGenericNodes } from './generic'

describe('generic node registration', () => {
  it('preserves the installed fixture descriptor at every frontend registration boundary', () => {
    const descriptors = contractDescriptors as unknown as BackendNodeSpec[]
    expect(registerGenericNodes(descriptors)).toBe(2)

    expect(getBackendSpec('descriptor_contract')).toEqual(descriptors[0])
    expect(getSpec('descriptor_contract')).toMatchObject({
      inputs: [{
        id: 'items', label: 'Ordered items', wire: 'dataset', accepts: ['dataset'], multi: true,
      }],
      outputs: [{ id: 'out', label: 'Rows', wire: 'dataset' }],
      previewable: true,
      requires: { cpu: 1, labels: {} },
    })
    expect(getSpec('descriptor_contract')!.defaultData().config).toEqual({ ratio: 0.5 })

    const node = (config: Record<string, unknown>) => ({
      type: 'descriptor_contract', data: { config },
    })
    const valid = { columns: ['source', 'ordinal'], count: 7, ratio: 1.25 }
    expect(nodeInvalidReason(node(valid), [{ name: 'source' }, { name: 'ordinal' }])).toBeNull()
    expect(nodeInvalidReason(node({ ...valid, columns: 'source,ordinal' }))).toContain('ordered list')
    expect(nodeInvalidReason(node(valid), undefined, { count: '12abc' })).toContain('safe integer')
    expect(nodeInvalidReason(node(valid), undefined, { ratio: 'Infinity' })).toContain('finite number')

    expect(getBackendSpec('descriptor_contract_unavailable')).toEqual(descriptors[1])
    expect(getSpec('descriptor_contract_unavailable')).toMatchObject({
      previewable: false,
      requires: { gpu: 1, labels: { engine: 'descriptor-contract' } },
    })
  })

  it('preserves a plugin multi-input descriptor for canvas connection validation', () => {
    registerGenericNodes([{
      kind: 'plugin-multi-input-contract', title: 'Plugin multi input', category: 'compute',
      inputs: [{ id: 'items', label: 'Items', wire: 'dataset', accepts: ['dataset'], multi: true }],
      outputs: [{ id: 'out', wire: 'dataset' }], params: [],
      canBypass: false, previewable: true, blurb: '',
    }])

    expect(portMulti('plugin-multi-input-contract', 'items')).toBe(true)
  })

  it('preserves plugin previewability and declared resource requirements', () => {
    registerGenericNodes([{
      kind: 'plugin-requires-preview-contract', title: 'Plugin full pass', category: 'compute',
      inputs: [{ id: 'in', wire: 'dataset' }], outputs: [{ id: 'out', wire: 'dataset' }], params: [],
      canBypass: false, previewable: false, requires: { gpu: 1, labels: { engine: 'plugin-gpu' } }, blurb: '',
    }])

    expect(getSpec('plugin-requires-preview-contract')).toMatchObject({
      previewable: false,
      requires: { gpu: 1, labels: { engine: 'plugin-gpu' } },
    })
  })

  it('keeps a columns parameter structured and rejects lossy shapes', () => {
    registerGenericNodes([{
      kind: 'plugin-structured-columns-contract', title: 'Plugin columns', category: 'compute',
      inputs: [{ id: 'in', wire: 'dataset' }], outputs: [{ id: 'out', wire: 'dataset' }],
      params: [{ name: 'columns', type: 'columns', required: true }], canBypass: false, previewable: true, blurb: '',
    }])
    const node = (columns: unknown) => ({ type: 'plugin-structured-columns-contract', data: { config: { columns } } })

    expect(nodeInvalidReason(node('id,event'))).toContain('ordered list')
    expect(nodeInvalidReason(node(['missing']), [{ name: 'id' }])).toContain('unavailable column')
    expect(nodeInvalidReason(node(['missing']), [])).toBeNull()
    expect(nodeInvalidReason(node(['event', 'id']), [{ name: 'id' }, { name: 'event' }])).toBeNull()
  })

  it.each([
    ['int', '  +42  ', { kind: 'valid', value: 42 }],
    ['int', '-0', { kind: 'valid', value: -0 }],
    ['int', '12abc', { kind: 'invalid' }],
    ['int', '1e3', { kind: 'invalid' }],
    ['float', '-1.25e+2', { kind: 'valid', value: -125 }],
    ['float', '.5', { kind: 'valid', value: 0.5 }],
    ['float', 'NaN', { kind: 'invalid' }],
    ['float', 'Infinity', { kind: 'invalid' }],
    ['float', '   ', { kind: 'empty' }],
  ] as const)('parses a complete %s value %j canonically', (type, text, expected) => {
    expect(parseNumericParam(text, type)).toEqual(expected)
  })

  it('validates numeric drafts and persisted JSON types from the declared schema', () => {
    registerGenericNodes([{
      kind: 'plugin-numeric-contract', title: 'Plugin numeric', category: 'compute',
      inputs: [], outputs: [{ id: 'out', wire: 'dataset' }],
      params: [
        { name: 'count', type: 'int', required: true },
        { name: 'ratio', type: 'float', default: 0.5 },
      ], canBypass: false, previewable: true, blurb: '',
    }])
    const node = (config: Record<string, unknown>) => ({ type: 'plugin-numeric-contract', data: { config } })

    expect(nodeInvalidReason(node({ count: 0 }))).toBeNull()
    expect(nodeInvalidReason(node({ count: 1 }), undefined, { count: '12abc' })).toContain('complete safe integer')
    expect(nodeInvalidReason(node({ count: 1 }), undefined, { count: '' })).toContain('required')
    expect(nodeInvalidReason(node({ count: '1' }))).toContain('complete safe integer')
    expect(nodeInvalidReason(node({ count: 1, ratio: Infinity }))).toContain('finite number')
  })
})
