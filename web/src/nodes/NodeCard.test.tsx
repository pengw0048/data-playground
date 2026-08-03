import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { ReactFlowProvider } from '@xyflow/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'

const apiMocks = vi.hoisted(() => ({
  schema: vi.fn(),
  graphSizes: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_target, property) => property === 'schema'
      ? apiMocks.schema
      : property === 'graphSizes'
        ? apiMocks.graphSizes
        : async () => ({}),
  }),
}))

import { useStore } from '../store/graph'
import type { NodeData } from '../types/graph'
import { NodeCard } from './NodeCard'

describe('NodeCard result summary', () => {
  const runPreview = vi.fn()
  const closePanel = vi.fn()

  beforeEach(() => {
    runPreview.mockReset()
    closePanel.mockReset()
    apiMocks.schema.mockReset().mockResolvedValue({})
    apiMocks.graphSizes.mockReset().mockResolvedValue({})
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

    expect(screen.getByText('2 outputs · 250 ms')).toBeInTheDocument()
    expect(screen.queryByText(/\b250 rows\b/)).not.toBeInTheDocument()
  })

  it('keeps the result freshness icon visible on every resting node', () => {
    const latest = useStore.getState().doc.nodes[0].data
    const { rerender } = render(
      <ReactFlowProvider><NodeCard id="target" data={latest} /></ReactFlowProvider>,
    )

    expect(screen.getByTitle('latest')).toHaveTextContent('✓')

    const stale: NodeData = { ...latest, status: 'stale' }
    rerender(<ReactFlowProvider><NodeCard id="target" data={stale} /></ReactFlowProvider>)
    expect(screen.getByTitle('stale')).toBeVisible()
  })

  it('hides output-run history on Source nodes', () => {
    const data = useStore.getState().doc.nodes[0].data
    useStore.setState({ selectedIds: ['target'] })

    render(
      <TooltipProvider>
        <ReactFlowProvider><NodeCard id="target" data={data} /></ReactFlowProvider>
      </TooltipProvider>,
    )

    expect(screen.queryByRole('button', { name: 'Output versions' })).not.toBeInTheDocument()
  })

  it('opens the product node menu on right-click and applies actions to that node', async () => {
    const data = useStore.getState().doc.nodes[0].data
    render(
      <TooltipProvider>
        <ReactFlowProvider><NodeCard id="target" data={data} /></ReactFlowProvider>
      </TooltipProvider>,
    )

    fireEvent.contextMenu(screen.getByText('target'))
    const menu = await screen.findByRole('menu', { name: 'Node actions' })
    for (const name of ['Rename', 'Preview data', 'Run details', 'Lineage', 'Copy', 'Cut', 'Duplicate', 'Disable', 'Delete']) {
      expect(within(menu).getByRole('menuitem', { name: new RegExp(name) })).toBeVisible()
    }
    fireEvent.click(screen.getByRole('menuitem', { name: /Duplicate/ }))

    expect(useStore.getState().doc.nodes).toHaveLength(2)
  })

  it('highlights an editable card on hover and strengthens the cue over its rename target', () => {
    const data = useStore.getState().doc.nodes[0].data
    render(
      <TooltipProvider>
        <ReactFlowProvider><NodeCard id="target" data={data} /></ReactFlowProvider>
      </TooltipProvider>,
    )

    const title = screen.getByTitle('Click (when selected) or double-click to rename')
    const card = title.closest('div.overflow-hidden.rounded-lg')
    const wrapper = card?.closest('.dp-no-select')
    expect(card).not.toBeNull()
    expect(wrapper).not.toBeNull()
    expect(card).not.toHaveClass('ring-1', 'ring-2')
    fireEvent.mouseEnter(wrapper!)
    expect(card).toHaveClass('ring-1')
    fireEvent.mouseEnter(title)
    expect(card).toHaveClass('border-primary', 'ring-2')
    fireEvent.mouseLeave(title)
    expect(card).not.toHaveClass('ring-2')
    expect(card).toHaveClass('ring-1')
  })

  it('names retained successful snapshots as output versions on output nodes', () => {
    useStore.setState((state) => ({
      selectedIds: ['target'],
      doc: {
        ...state.doc,
        nodes: state.doc.nodes.map((node) => ({ ...node, type: 'filter' })),
      },
    }))
    const data = useStore.getState().doc.nodes[0].data

    render(
      <TooltipProvider>
        <ReactFlowProvider><NodeCard id="target" data={data} /></ReactFlowProvider>
      </TooltipProvider>,
    )

    expect(screen.getByRole('button', { name: 'Output versions' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'History' })).not.toBeInTheDocument()
  })

  it('removes an old Sample size hint immediately after its configuration changes', () => {
    const sample: NodeData = {
      title: 'sample', status: 'latest', config: { n: 1000, seed: 42 }, history: [],
    }
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: [{
          id: 'sample', type: 'sample', position: { x: 0, y: 0 }, data: sample,
        }],
      },
      sizes: { sample: { rows: 1000, confidence: 'bounded' } },
    }))

    render(<ReactFlowProvider><NodeCard id="sample" data={sample} /></ReactFlowProvider>)
    expect(screen.getByText('≤ 1,000 rows')).toBeInTheDocument()

    act(() => useStore.getState().updateConfig('sample', { n: 25 }))

    expect(screen.queryByText('≤ 1,000 rows')).not.toBeInTheDocument()
  })

  it('rejects an old size response that finishes after a Sample configuration change', async () => {
    let finishOldSizes!: (sizes: { sample: { rows: number; confidence: 'bounded' } }) => void
    apiMocks.graphSizes
      .mockImplementationOnce(() => new Promise((resolve) => { finishOldSizes = resolve }))
      .mockResolvedValueOnce({ sample: { rows: 25, confidence: 'bounded' } })
    const sample: NodeData = {
      title: 'sample', status: 'latest', config: { n: 1000, seed: 42 }, history: [],
    }
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: [{
          id: 'sample', type: 'sample', position: { x: 0, y: 0 }, data: sample,
        }],
      },
      sizes: { sample: { rows: 1000, confidence: 'bounded' } },
    }))
    render(<ReactFlowProvider><NodeCard id="sample" data={sample} /></ReactFlowProvider>)

    const oldRefresh = useStore.getState().refreshSchemas()
    await vi.waitFor(() => expect(apiMocks.graphSizes).toHaveBeenCalledTimes(1))
    act(() => useStore.getState().updateConfig('sample', { n: 25 }))
    expect(screen.queryByText('≤ 1,000 rows')).not.toBeInTheDocument()

    await act(async () => {
      finishOldSizes({ sample: { rows: 1000, confidence: 'bounded' } })
      await oldRefresh
    })
    expect(screen.queryByText('≤ 1,000 rows')).not.toBeInTheDocument()

    await act(async () => { await useStore.getState().refreshSchemas() })
    expect(screen.getByText('≤ 25 rows')).toBeInTheDocument()
  })

  it('puts selected Source preview in the shared action shelf without adding a run action', () => {
    useStore.setState({ selectedIds: ['target'] })
    const data: NodeData = {
      title: 'target', status: 'latest', config: { uri: 'input.csv' },
    }

    render(
      <TooltipProvider>
        <ReactFlowProvider><NodeCard id="target" data={data} /></ReactFlowProvider>
      </TooltipProvider>,
    )

    expect(screen.queryByRole('button', { name: 'Preview data' })).not.toBeInTheDocument()
    const preview = screen.getByRole('button', { name: 'View data' })
    expect(preview).toBeVisible()
    expect(preview).toBeEnabled()
    fireEvent.click(preview)
    expect(runPreview).toHaveBeenCalledWith('target')
    expect(screen.queryByRole('button', { name: 'Run up to here' })).not.toBeInTheDocument()

    act(() => useStore.setState({ openPanels: { target: 'data' } }))
    const hide = screen.getByRole('button', { name: 'Hide data' })
    fireEvent.click(hide)
    expect(closePanel).toHaveBeenCalledWith('target')
  })

  it('reveals Source preview from the shared shelf on hover', () => {
    const data: NodeData = {
      title: 'target', status: 'latest', config: { uri: 'input.csv' },
    }

    const { container } = render(
      <TooltipProvider>
        <ReactFlowProvider><NodeCard id="target" data={data} /></ReactFlowProvider>
      </TooltipProvider>,
    )

    expect(screen.queryByRole('button', { name: 'View data' })).not.toBeInTheDocument()
    fireEvent.mouseEnter(container.firstElementChild!)
    expect(screen.getByRole('button', { name: 'View data' })).toBeVisible()
  })

  it('keeps Source shelf preview available in a view-only Canvas', () => {
    useStore.setState({ canvasRole: 'viewer', selectedIds: ['target'] })
    const data: NodeData = {
      title: 'target', status: 'latest', config: { uri: 'input.csv' },
    }

    render(
      <TooltipProvider>
        <ReactFlowProvider><NodeCard id="target" data={data} /></ReactFlowProvider>
      </TooltipProvider>,
    )

    const preview = screen.getByRole('button', { name: 'View data' })
    expect(preview).toBeEnabled()
    fireEvent.click(preview)
    expect(runPreview).toHaveBeenCalledWith('target')
    expect(screen.queryByRole('button', { name: 'Run up to here' })).not.toBeInTheDocument()
  })

  it('keeps failed run details available after the transient error toast disappears', () => {
    useStore.setState((state) => ({
      selectedIds: ['target'],
      runs: { target: { phase: 'failed', error: 'Transform failed' } },
      doc: {
        ...state.doc,
        nodes: state.doc.nodes.map((node) => ({
          ...node,
          type: 'transform',
          data: { ...node.data, status: 'failed' },
        })),
      },
    }))
    const data = useStore.getState().doc.nodes[0].data

    render(
      <TooltipProvider>
        <ReactFlowProvider><NodeCard id="target" data={data} /></ReactFlowProvider>
      </TooltipProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Fix error' }))
    expect(useStore.getState().openPanels).toEqual({ target: 'run' })
  })
})
