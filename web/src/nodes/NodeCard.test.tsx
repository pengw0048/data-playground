import { render, screen } from '@testing-library/react'
import { ReactFlowProvider } from '@xyflow/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({ api: new Proxy({}, { get: () => async () => ({}) }) }))

import { useStore } from '../store/graph'
import type { NodeData } from '../types/graph'
import { NodeCard } from './NodeCard'

describe('NodeCard result summary', () => {
  beforeEach(() => {
    useStore.setState({
      canvasRole: 'owner', selectedIds: [], openPanels: {}, runs: {}, sizes: {},
      doc: {
        id: 'c', name: 'test', version: 1, requirements: [], edges: [], nodes: [{
          id: 'target', type: 'source', position: { x: 0, y: 0 },
          data: { title: 'target', status: 'latest', config: {}, history: [] },
        }],
      },
    } as any)
  })

  it('shows output cardinality for a named multi-output result', () => {
    const data: NodeData = {
      title: 'target', status: 'latest', config: {},
      lastRun: { outputCount: 2, ms: 250, placement: 'local' },
    }

    render(<ReactFlowProvider><NodeCard id="target" data={data} /></ReactFlowProvider>)

    expect(screen.getByText('2 outputs · 250ms')).toBeInTheDocument()
    expect(screen.queryByText(/\b250 rows\b/)).not.toBeInTheDocument()
  })
})
