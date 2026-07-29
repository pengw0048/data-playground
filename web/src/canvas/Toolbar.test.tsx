import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'

const viewport = vi.hoisted(() => ({ zoomIn: vi.fn(), zoomOut: vi.fn(), fitView: vi.fn(), zoom: 1 }))
const toolbarState = vi.hoisted(() => ({
  doc: { nodes: [{ id: 'source-1', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', status: 'draft', config: {} } }], edges: [] },
  selectedIds: [],
  specs: [] as Array<{ kind: string; title: string; category: string; inputs: []; outputs: []; canBypass: boolean; blurb: string }>,
  addNode: vi.fn(),
  addConnectedNode: vi.fn(),
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
vi.mock('./nextStep', () => ({
  uniqueNextStepConnection: (source: { id: string }, targetKind: string) => (
    source.id === 'source-1' && targetKind === 'transform'
      ? { sourceHandle: 'out', targetHandle: 'in', wire: 'dataset' }
      : null
  ),
}))

import { CanvasViewportControls, CanvasViewControls, Toolbar, toolbarDensityForWidth } from './Toolbar'

describe('toolbarDensityForWidth', () => {
  it('keeps all three densities reachable from the actual Canvas width', () => {
    expect(toolbarDensityForWidth(899, false)).toBe('icons')
    expect(toolbarDensityForWidth(980, false)).toBe('comfortable')
    expect(toolbarDensityForWidth(980, true)).toBe('compact')
    expect(toolbarDensityForWidth(1024, true)).toBe('comfortable')
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

  it('keeps the Inspector in the centred toolbar group', () => {
    const toggleInspector = vi.fn()
    render(
      <TooltipProvider delayDuration={0}>
        <CanvasViewControls labelsVisible inspectorCollapsed onInspectorToggle={toggleInspector} />
      </TooltipProvider>,
    )

    const controls = screen.getByRole('group', { name: 'View controls' })
    expect(controls).toContainElement(screen.getByRole('group', { name: 'Panel controls' }))
    expect(within(controls).queryByRole('group', { name: 'Viewport controls' })).not.toBeInTheDocument()
    expect(within(controls).getByText('View', { exact: true })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Show Inspector' })).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(screen.getByRole('button', { name: 'Show Inspector' }))
    expect(toggleInspector).toHaveBeenCalledOnce()
  })

  it('keeps viewport and Inspector controls available to a view-only canvas', () => {
    render(
      <TooltipProvider delayDuration={0}>
        <Toolbar inspectorCollapsed={false} onInspectorToggle={vi.fn()} />
      </TooltipProvider>,
    )

    expect(screen.getByTestId('view-only-badge')).toHaveTextContent('View-only canvas')
    expect(screen.queryByTestId('toolbar-add-controls')).not.toBeInTheDocument()
    expect(screen.getByTestId('canvas-viewport-controls')).toContainElement(screen.getByRole('button', { name: 'Zoom in' }))
    expect(screen.getByTestId('toolbar-view-controls')).not.toContainElement(screen.getByRole('button', { name: 'Zoom in' }))
    expect(screen.getByRole('button', { name: 'Fit view' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Hide Inspector' })).toBeEnabled()
  })

  it('shows a visible Add next step label only for an unambiguous selected source', () => {
    toolbarState.canvasRole = 'owner'
    toolbarState.selectedIds = ['source-1']
    toolbarState.specs = [{
      kind: 'transform', title: 'transform', category: 'compute', inputs: [], outputs: [], canBypass: true, blurb: '',
    }]
    const { rerender } = render(
      <TooltipProvider delayDuration={0}>
        <Toolbar inspectorCollapsed={false} onInspectorToggle={vi.fn()} />
      </TooltipProvider>,
    )

    const contextualAdd = screen.getByRole('button', { name: 'Add next step' })
    expect(contextualAdd).toHaveTextContent('Add next step')

    toolbarState.selectedIds = []
    rerender(
      <TooltipProvider delayDuration={0}>
        <Toolbar inspectorCollapsed={false} onInspectorToggle={vi.fn()} />
      </TooltipProvider>,
    )
    expect(screen.getByRole('button', { name: 'Add operation' })).not.toHaveTextContent('Add operation')
  })
})
