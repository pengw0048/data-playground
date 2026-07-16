import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({ api: new Proxy({}, { get: () => async () => ({}) }) }))
vi.mock('../ui/CodeEditor', () => ({ CodeEditor: () => <div data-testid="code-editor" /> }))

import { useStore } from '../store/graph'
import { SectionPanel } from './SectionPanel'

describe('SectionPanel driver help', () => {
  beforeEach(() => {
    useStore.setState({
      canvasRole: 'owner',
      doc: {
        id: 'c', name: 'test', version: 1, requirements: [], edges: [], nodes: [{
          id: 'section', type: 'section', position: { x: 0, y: 0 },
          data: { title: 'section', status: 'draft', config: { script: '', outputs: ['out'] }, history: [] },
        }],
      },
    } as any)
  })

  it('documents explicit child output selection for multi-output nodes', () => {
    const { container } = render(<SectionPanel nodeId="section" />)
    const examples = Array.from(container.querySelectorAll('code')).map((node) => node.textContent)

    expect(examples).toContain("run(alias, data=inputs['in'], output_port='port', **cfg)")
    expect(container).toHaveTextContent(/choose output_port when that child has multiple outputs/i)
  })
})
