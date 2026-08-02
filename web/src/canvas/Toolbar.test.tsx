import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'

const viewport = vi.hoisted(() => ({ zoomIn: vi.fn(), zoomOut: vi.fn(), fitView: vi.fn(), zoom: 1 }))
const toolbarState = vi.hoisted(() => ({
  doc: { nodes: [{ id: 'source-1', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', status: 'draft', config: {} } }], edges: [] },
  selectedIds: [],
  specs: [] as Array<{ kind: string; title: string; category: string; inputs: []; outputs: []; canBypass: boolean; blurb: string }>,
  addNode: vi.fn(),
  select: vi.fn(),
  setAgentOpen: vi.fn(),
  agentOpen: false,
  canvasRole: 'viewer',
}))

vi.mock('@xyflow/react', () => ({
  useReactFlow: () => ({ zoomIn: viewport.zoomIn, zoomOut: viewport.zoomOut, fitView: viewport.fitView }),
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
vi.mock('../theme/tokens', () => ({ categoryOrder: [], color: {}, kindAccent: {} }))
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
    toolbarState.selectedIds = []
    toolbarState.specs = []
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

  it('keeps one searchable operation picker in the bottom toolbar when selection changes', () => {
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
    expect(screen.getAllByRole('button', { name: 'Add operation' })).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Locate existing node' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Inspector/ })).not.toBeInTheDocument()

    toolbarState.selectedIds = []
    rerender(
      <TooltipProvider delayDuration={0}>
        <Toolbar />
      </TooltipProvider>,
    )
    expect(screen.getAllByRole('button', { name: 'Add operation' })).toHaveLength(1)
  })
})
