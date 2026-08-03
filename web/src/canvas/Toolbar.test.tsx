import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'

const viewport = vi.hoisted(() => ({
  zoomIn: vi.fn(), zoomOut: vi.fn(), fitView: vi.fn(), zoom: 1,
  screenToFlowPosition: vi.fn(({ x, y }: { x: number; y: number }) => ({ x, y })),
}))
const toolbarState = vi.hoisted(() => ({
  doc: { id: 'canvas-1', nodes: [{ id: 'source-1', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', status: 'draft', config: {} } }], edges: [] },
  selectedId: null as string | null,
  selectedIds: [],
  specs: [] as Array<{ kind: string; title: string; category: string; inputs: Array<{ id: string; wire: string }>; outputs: Array<{ id: string; wire: string }>; canBypass: boolean; blurb: string }>,
  addNode: vi.fn(),
  addConnectedNode: vi.fn(),
  requestNodeReveal: vi.fn(),
  select: vi.fn(),
  setAgentOpen: vi.fn(),
  agentOpen: false,
  canvasRole: 'viewer',
}))

vi.mock('@xyflow/react', () => ({
  useReactFlow: () => ({
    zoomIn: viewport.zoomIn, zoomOut: viewport.zoomOut, fitView: viewport.fitView,
    screenToFlowPosition: viewport.screenToFlowPosition,
  }),
  useViewport: () => ({ zoom: viewport.zoom }),
}))

vi.mock('../store/graph', () => ({
  useStore: Object.assign((selector: (state: typeof toolbarState) => unknown) => selector(toolbarState), {
    getState: () => toolbarState,
  }),
  freePosition: vi.fn(),
  roleCanEdit: (role: string) => role === 'owner' || role === 'editor',
}))

vi.mock('../nodes', () => ({ allSpecs: () => toolbarState.specs }))
vi.mock('../nodes/registry', () => ({
  nodeOutputs: (node: { type: string }) => toolbarState.specs.find((spec) => spec.kind === node.type)?.outputs
    ?? (node.type === 'source' ? [{ id: 'out', wire: 'dataset' }] : []),
  firstCompatibleInput: (kind: string, wire: string) => toolbarState.specs
    .find((spec) => spec.kind === kind)?.inputs.find((input) => input.wire === wire),
}))
vi.mock('../theme/tokens', () => ({
  categoryOrder: ['io', 'shape', 'compute', 'query', 'inspect', 'control'],
  color: {}, kindAccent: {},
}))
import { CanvasViewportControls, Toolbar, toolbarDensityForWidth } from './Toolbar'

describe('toolbarDensityForWidth', () => {
  it('switches labels from the actual Canvas width', () => {
    expect(toolbarDensityForWidth(899)).toBe('icons')
    expect(toolbarDensityForWidth(900)).toBe('comfortable')
    expect(toolbarDensityForWidth(1024)).toBe('comfortable')
  })
})

describe('Canvas controls', () => {
  beforeEach(() => {
    viewport.zoom = 1
    viewport.zoomIn.mockReset()
    viewport.zoomOut.mockReset()
    viewport.fitView.mockReset()
    toolbarState.canvasRole = 'viewer'
    toolbarState.selectedId = null
    toolbarState.selectedIds = []
    toolbarState.specs = []
    toolbarState.addNode.mockReset()
    toolbarState.addConnectedNode.mockReset()
    toolbarState.requestNodeReveal.mockReset()
  })

  it('keeps viewport operations accessible while rendering icons only', () => {
    render(
      <TooltipProvider delayDuration={0}>
        <CanvasViewportControls />
      </TooltipProvider>,
    )

    const controls = screen.getByRole('group', { name: 'Viewport controls' })
    for (const name of ['Zoom in', 'Zoom out', 'Fit view']) {
      expect(controls).toContainElement(screen.getByRole('button', { name }))
      expect(within(controls).queryByText(name, { exact: true })).not.toBeInTheDocument()
    }

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }))
    fireEvent.click(screen.getByRole('button', { name: 'Fit view' }))

    expect(viewport.zoomIn).toHaveBeenCalledOnce()
    expect(viewport.zoomOut).toHaveBeenCalledOnce()
    expect(viewport.fitView).toHaveBeenCalledWith({ padding: 0.3, maxZoom: 1 })
  })

  it('preserves both zoom boundaries', () => {
    viewport.zoom = 2.5
    const { rerender } = render(
      <TooltipProvider delayDuration={0}>
        <CanvasViewportControls />
      </TooltipProvider>,
    )

    expect(screen.getByRole('button', { name: 'Zoom in' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Zoom out' })).toBeEnabled()

    viewport.zoom = 0.2
    rerender(
      <TooltipProvider delayDuration={0}>
        <CanvasViewportControls />
      </TooltipProvider>,
    )
    expect(screen.getByRole('button', { name: 'Zoom in' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Zoom out' })).toBeDisabled()
  })

  it('keeps viewport controls available without rendering an empty view-only toolbar', () => {
    render(
      <TooltipProvider delayDuration={0}>
        <Toolbar />
      </TooltipProvider>,
    )

    expect(screen.getByTestId('view-only-badge')).toHaveTextContent('View-only canvas')
    expect(screen.queryByTestId('toolbar-add-controls')).not.toBeInTheDocument()
    expect(screen.queryByTestId('toolbar')).not.toBeInTheDocument()
    expect(screen.getByTestId('canvas-viewport-controls')).toContainElement(screen.getByRole('button', { name: 'Zoom in' }))
    expect(screen.getByRole('button', { name: 'Fit view' })).toBeEnabled()
  })

  it('keeps category tools and node search without a redundant global add button', () => {
    toolbarState.canvasRole = 'owner'
    toolbarState.selectedIds = ['source-1']
    toolbarState.specs = [{
      kind: 'transform', title: 'transform', category: 'compute', inputs: [], outputs: [], canBypass: true, blurb: '',
    }]
    const { rerender } = render(
      <TooltipProvider delayDuration={0}>
        <Toolbar />
      </TooltipProvider>,
    )

    expect(screen.queryByRole('button', { name: 'Add next step' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add operation' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Locate existing node' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Inspector/ })).not.toBeInTheDocument()

    toolbarState.selectedIds = []
    rerender(
      <TooltipProvider delayDuration={0}>
        <Toolbar />
      </TooltipProvider>,
    )
    expect(screen.queryByRole('button', { name: 'Add operation' })).not.toBeInTheDocument()
  })

  it('connects a toolbar operation to the sole selected compatible node', () => {
    toolbarState.canvasRole = 'owner'
    toolbarState.selectedId = 'source-1'
    toolbarState.selectedIds = ['source-1']
    toolbarState.specs = [{
      kind: 'transform', title: 'Transform', category: 'compute',
      inputs: [{ id: 'in', wire: 'dataset' }], outputs: [{ id: 'out', wire: 'dataset' }],
      canBypass: true, blurb: 'Transform rows',
    }]
    toolbarState.addConnectedNode.mockReturnValue({ id: 'transform-1' })

    render(
      <TooltipProvider delayDuration={0}>
        <Toolbar />
      </TooltipProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Compute' }))
    fireEvent.click(screen.getByRole('button', { name: 'Transform' }))

    expect(toolbarState.addConnectedNode).toHaveBeenCalledWith(
      'transform',
      expect.any(Object),
      { source: 'source-1', sourceHandle: 'out', targetHandle: 'in', wire: 'dataset' },
    )
    expect(toolbarState.addNode).not.toHaveBeenCalled()
    expect(toolbarState.requestNodeReveal).toHaveBeenCalledWith('canvas-1', 'transform-1')
  })
})
