import { act, fireEvent, render, screen } from '@testing-library/react'
import { ReactFlowProvider } from '@xyflow/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({ api: new Proxy({}, { get: () => async () => ({}) }) }))

import { useStore } from '../store/graph'
import type { NodeData } from '../types/graph'
import { NodeCard } from './NodeCard'

describe('NodeCard result summary', () => {
  const runPreview = vi.fn()
  const closePanel = vi.fn()

  beforeEach(() => {
    runPreview.mockReset()
    closePanel.mockReset()
    useStore.setState({
      canvasRole: 'owner', kernelUp: true, selectedIds: [], openPanels: {}, runs: {}, sizes: {},
      runPreview, closePanel,
      doc: {
        id: 'c', name: 'test', version: 1, requirements: [], edges: [], nodes: [{
          id: 'target', type: 'source', position: { x: 0, y: 0 },
          data: { title: 'target', status: 'latest', config: { uri: 'input.csv' }, history: [] },
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

  it('keeps Source preview in the header for an unselected viewer', () => {
    useStore.setState({ canvasRole: 'viewer' })
    const data: NodeData = {
      title: 'target', status: 'latest', config: { uri: 'input.csv' },
    }

    render(<ReactFlowProvider><NodeCard id="target" data={data} /></ReactFlowProvider>)

    const preview = screen.getByRole('button', { name: 'Preview data' })
    expect(preview).toBeVisible()
    expect(preview).toBeEnabled()
    expect(preview).toHaveAttribute('title', 'Preview data')
    fireEvent.click(preview)
    expect(runPreview).toHaveBeenCalledWith('target')

    act(() => useStore.setState({ openPanels: { target: 'data' } }))
    const hide = screen.getByRole('button', { name: 'Hide preview' })
    expect(hide).toHaveAttribute('title', 'Hide preview')
    fireEvent.click(hide)
    expect(closePanel).toHaveBeenCalledWith('target')
  })
})
