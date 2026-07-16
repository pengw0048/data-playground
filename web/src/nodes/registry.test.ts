import { describe, expect, it } from 'vitest'
import { nodeOutputs, portAccepts, portMulti, portWire, register } from './registry'
import type { CanvasNode } from '../types/graph'

const DummyNode = () => null

function node(type: string, outputs: unknown): CanvasNode {
  return {
    id: `${type}-node`, type, position: { x: 0, y: 0 },
    data: { title: type, status: 'draft', history: [], config: { outputs } },
  }
}

describe('nodeOutputs', () => {
  it('uses config.outputs only for Section nodes', () => {
    register({
      kind: 'configured-plugin-test', title: 'plugin', category: 'compute', inputs: [],
      outputs: [{ id: 'declared', label: 'Declared', wire: 'metric' }], canBypass: false,
      blurb: '', defaultData: () => ({ title: 'plugin', status: 'draft', history: [], config: {} }),
    }, DummyNode)

    expect(nodeOutputs(node('configured-plugin-test', ['injected']))).toEqual([
      { id: 'declared', label: 'Declared', wire: 'metric' },
    ])
    expect(nodeOutputs(node('section', ['pass', 'out']))).toEqual([
      { id: 'pass', label: 'pass', wire: 'dataset' },
      { id: 'out', label: 'out', wire: 'dataset' },
    ])
  })

  it.each([
    [[' out']],
    [['']],
    [[123]],
    [['out', 'out']],
    [[...Array.from({ length: 65 }, (_, index) => `out-${index}`)]],
    [['x'.repeat(129)]],
  ])('does not synthesize ports from an invalid Section declaration: %j', (outputs) => {
    expect(nodeOutputs(node('section', outputs))).toEqual([])
  })
})

describe('port lookup', () => {
  it('requires an exact source handle for multi-output nodes', () => {
    register({
      kind: 'multi-output-port-test', title: 'multi output', category: 'compute', inputs: [],
      outputs: [
        { id: 'left', label: 'Left', wire: 'dataset' },
        { id: 'score', label: 'Score', wire: 'metric' },
      ],
      canBypass: false, blurb: '',
      defaultData: () => ({ title: 'multi output', status: 'draft', history: [], config: {} }),
    }, DummyNode)
    const source = node('multi-output-port-test', undefined)

    expect(portWire([source], source.id, 'score', 'source')).toBe('metric')
    expect(portWire([source], source.id, 'missing', 'source')).toBeNull()
    expect(portWire([source], source.id, undefined, 'source')).toBeNull()
  })

  it('uses the sole source output when its handle is omitted', () => {
    register({
      kind: 'single-output-port-test', title: 'single output', category: 'compute', inputs: [],
      outputs: [{ id: 'only', label: 'Only', wire: 'dataset' }], canBypass: false, blurb: '',
      defaultData: () => ({ title: 'single output', status: 'draft', history: [], config: {} }),
    }, DummyNode)
    const source = node('single-output-port-test', undefined)

    expect(portWire([source], source.id, undefined, 'source')).toBe('dataset')
    expect(portWire([source], source.id, 'missing', 'source')).toBeNull()
  })

  it('does not reinterpret an unknown target handle as the primary input', () => {
    register({
      kind: 'target-port-test', title: 'target', category: 'compute',
      inputs: [
        { id: 'primary', label: 'Primary', wire: 'dataset' },
        { id: 'many', label: 'Many', wire: 'metric', accepts: ['metric', 'sample'], multi: true },
      ],
      outputs: [], canBypass: false, blurb: '',
      defaultData: () => ({ title: 'target', status: 'draft', history: [], config: {} }),
    }, DummyNode)

    expect(portAccepts('target-port-test', undefined)).toEqual(['dataset'])
    expect(portAccepts('target-port-test', 'many')).toEqual(['metric', 'sample'])
    expect(portMulti('target-port-test', 'many')).toBe(true)
    expect(portAccepts('target-port-test', 'missing')).toEqual([])
    expect(portMulti('target-port-test', 'missing')).toBe(false)
  })
})
