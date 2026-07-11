import { render, screen } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ReactFlowProvider } from '@xyflow/react'

// importing the store triggers autosave side-effects → stub the api client (mirrors store.test.ts)
vi.mock('../../api/client', () => ({ api: new Proxy({}, { get: () => async () => ({}) }) }))

import './join'                                  // registers the hand-built Join card via register()
import { getComponent } from '../registry'
import { registerGenericNodes } from '../generic'
import { useStore } from '../../store/graph'

describe('Join card — join types come from the backend spec (UX-05)', () => {
  beforeEach(() => {
    // seed the backend spec the card derives its `how` options from (source of truth = all 4 types)
    registerGenericNodes([{
      kind: 'join', title: 'join', category: 'compute',
      inputs: [{ id: 'a', wire: 'dataset' }, { id: 'b', wire: 'dataset' }],
      outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false, blurb: '',
      params: [{ name: 'how', type: 'select', default: 'inner', options: ['inner', 'left', 'right', 'outer'] },
               { name: 'on', type: 'string' }, { name: 'condition', type: 'string' }],
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    }] as any)
    useStore.setState({
      doc: { id: 'c', version: 1, name: 't', requirements: [],
             nodes: [{ id: 'j', type: 'join', position: { x: 0, y: 0 },
                       data: { title: 'join', status: 'draft', config: { how: 'inner', on: '' } } }], edges: [] },
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } as any)
  })

  it('offers every backend join type (inner/left/right/outer), not a hardcoded subset', () => {
    const Join = getComponent('join')!
    const data = useStore.getState().doc.nodes[0].data
    render(<ReactFlowProvider><Join id="j" data={data} /></ReactFlowProvider>)
    for (const h of ['inner', 'left', 'right', 'outer']) {
      expect(screen.getByRole('option', { name: h })).toBeInTheDocument()
    }
  })
})
