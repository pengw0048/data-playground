import { describe, expect, it } from 'vitest'
import { register } from '../nodes/registry'
import type { CanvasNode } from '../types/graph'
import { uniqueNextStepConnection } from './nextStep'

const Empty = () => null
const node = (id: string, type: string): CanvasNode => ({
  id, type, position: { x: 0, y: 0 },
  data: { title: id, status: 'draft', history: [], config: {} },
})

register({
  kind: 'next-source-test', title: 'source', category: 'io', inputs: [],
  outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false, blurb: '',
  defaultData: () => ({ title: 'source', status: 'draft', history: [], config: {} }),
}, Empty)
register({
  kind: 'next-sample-test', title: 'sample', category: 'shape',
  inputs: [{ id: 'in', wire: 'dataset' }], outputs: [{ id: 'out', wire: 'sample' }], canBypass: false, blurb: '',
  defaultData: () => ({ title: 'sample', status: 'draft', history: [], config: {} }),
}, Empty)
register({
  kind: 'next-filter-test', title: 'filter', category: 'shape',
  inputs: [{ id: 'in', wire: 'dataset' }], outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false, blurb: '',
  defaultData: () => ({ title: 'filter', status: 'draft', history: [], config: {} }),
}, Empty)
register({
  kind: 'next-transform-test', title: 'transform', category: 'compute',
  inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }], outputs: [], canBypass: false, blurb: '',
  defaultData: () => ({ title: 'transform', status: 'draft', history: [], config: {} }),
}, Empty)
register({
  kind: 'next-ambiguous-output-test', title: 'ambiguous output', category: 'compute', inputs: [],
  outputs: [{ id: 'left', wire: 'dataset' }, { id: 'right', wire: 'dataset' }], canBypass: false, blurb: '',
  defaultData: () => ({ title: 'ambiguous output', status: 'draft', history: [], config: {} }),
}, Empty)
register({
  kind: 'next-ambiguous-input-test', title: 'ambiguous input', category: 'compute',
  inputs: [{ id: 'a', wire: 'dataset' }, { id: 'b', wire: 'dataset' }], outputs: [], canBypass: false, blurb: '',
  defaultData: () => ({ title: 'ambiguous input', status: 'draft', history: [], config: {} }),
}, Empty)

describe('uniqueNextStepConnection', () => {
  it('finds the one Source -> Sample/Filter pair and Sample/Filter -> Transform pair', () => {
    expect(uniqueNextStepConnection(node('source', 'next-source-test'), 'next-sample-test')).toEqual({
      sourceHandle: 'out', targetHandle: 'in', wire: 'dataset',
    })
    expect(uniqueNextStepConnection(node('sample', 'next-sample-test'), 'next-transform-test')).toEqual({
      sourceHandle: 'out', targetHandle: 'in', wire: 'sample',
    })
    expect(uniqueNextStepConnection(node('filter', 'next-filter-test'), 'next-transform-test')).toEqual({
      sourceHandle: 'out', targetHandle: 'in', wire: 'dataset',
    })
  })

  it('refuses to guess a multiple-output or multiple-input connection', () => {
    expect(uniqueNextStepConnection(node('split', 'next-ambiguous-output-test'), 'next-sample-test')).toBeNull()
    expect(uniqueNextStepConnection(node('source', 'next-source-test'), 'next-ambiguous-input-test')).toBeNull()
  })
})
