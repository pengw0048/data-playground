import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  workspaceResource: vi.fn(),
  state: {
    doc: { id: 'canvas-1' },
    currentDraftId: null as string | null,
  },
}))

vi.mock('../api/client', () => ({ api: { workspaceResource: mocks.workspaceResource } }))
vi.mock('../store/graph', () => ({
  useStore: (selector: (state: typeof mocks.state) => unknown) => selector(mocks.state),
}))

import { CanvasWorkspaceLocation } from './CanvasWorkspaceLocation'

const ROOT = { id: 'container:workspace-local-root', kind: 'container' as const, name: 'Workspace', parentId: null, detached: false, source: 'local' as const }
const RESEARCH = { id: 'container:research', kind: 'container' as const, name: 'Research', parentId: ROOT.id, detached: false, source: 'local' as const }
const ROBOTICS = { id: 'container:robotics', kind: 'container' as const, name: 'Robotics', parentId: RESEARCH.id, detached: false, source: 'local' as const }
const CANVAS = { id: 'canvas:canvas-1', kind: 'canvas' as const, name: 'Purchases per user', parentId: ROBOTICS.id, placementId: 'placement-1', detached: false, source: 'local' as const }
const COMPLETE = { id: 'local', kind: 'local' as const, completeness: 'complete' as const }

describe('CanvasWorkspaceLocation', () => {
  const onReturnDestination = vi.fn()
  const onNavigate = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state.doc.id = 'canvas-1'
    mocks.state.currentDraftId = null
  })
  afterEach(cleanup)

  it('renders only API-resolved local ancestors and routes each crumb by opaque id', async () => {
    mocks.workspaceResource.mockResolvedValue({ resource: CANVAS, ancestors: [ROOT, RESEARCH, ROBOTICS], source: COMPLETE })
    render(<CanvasWorkspaceLocation onReturnDestination={onReturnDestination} onNavigate={onNavigate} />)

    const location = await screen.findByRole('navigation', { name: 'Canvas Workspace location' })
    expect(location).toHaveTextContent('Workspace/Research/Robotics/Purchases per user')
    expect(onReturnDestination).toHaveBeenLastCalledWith(ROBOTICS.id)
    fireEvent.click(screen.getByRole('button', { name: 'Research' }))
    expect(onNavigate).toHaveBeenCalledWith(RESEARCH.id)
    fireEvent.click(screen.getByRole('button', { name: 'Workspace' }))
    expect(onNavigate).toHaveBeenLastCalledWith(null)
  })

  it('keeps a provider Canvas usable while exposing only the truthful unavailable recovery state', async () => {
    const providerCanvas = {
      ...CANVAS, parentId: 'container:external.mount-folder', source: 'local' as const,
    }
    mocks.workspaceResource.mockResolvedValue({
      resource: providerCanvas,
      ancestors: [ROOT, { id: 'container:external.mount-folder', kind: 'container' as const, name: 'Remote', parentId: ROOT.id, detached: false, source: 'provider' as const, bindingId: 'hidden-binding', resourceId: 'hidden-provider-id' }],
      source: { id: 'mount:warehouse', kind: 'provider' as const, completeness: 'unavailable' as const, referenceState: 'detached' as const, mountId: 'warehouse', error: 'resource detached' },
    })
    render(<CanvasWorkspaceLocation onReturnDestination={onReturnDestination} onNavigate={onNavigate} />)

    expect(await screen.findByRole('status')).toHaveTextContent('Its Workspace location is unavailable.')
    expect(screen.queryByRole('navigation', { name: 'Canvas Workspace location' })).not.toBeInTheDocument()
    expect(screen.queryByText('hidden-binding')).not.toBeInTheDocument()
    expect(screen.queryByText('hidden-provider-id')).not.toBeInTheDocument()
    expect(onReturnDestination).toHaveBeenLastCalledWith('container:external.mount-folder')
  })

  it('does not fabricate a location for a local draft or an unplaced Canvas', async () => {
    mocks.state.currentDraftId = 'draft-1'
    const { rerender } = render(<CanvasWorkspaceLocation onReturnDestination={onReturnDestination} onNavigate={onNavigate} />)
    expect(mocks.workspaceResource).not.toHaveBeenCalled()
    expect(onReturnDestination).toHaveBeenLastCalledWith(undefined)

    mocks.state.currentDraftId = null
    mocks.workspaceResource.mockRejectedValue(new Error('Workspace resource not found'))
    rerender(<CanvasWorkspaceLocation onReturnDestination={onReturnDestination} onNavigate={onNavigate} />)
    await waitFor(() => expect(mocks.workspaceResource).toHaveBeenCalledWith('canvas:canvas-1'))
    expect(screen.queryByRole('navigation', { name: 'Canvas Workspace location' })).not.toBeInTheDocument()
    expect(onReturnDestination).toHaveBeenLastCalledWith(undefined)
  })

  it('ignores an older Canvas resolution that settles after a newer Canvas opens', async () => {
    let resolveOld!: (value: unknown) => void
    const old = new Promise((resolve) => { resolveOld = resolve })
    mocks.workspaceResource.mockReturnValueOnce(old)
    const { rerender } = render(<CanvasWorkspaceLocation onReturnDestination={onReturnDestination} onNavigate={onNavigate} />)
    mocks.state.doc.id = 'canvas-2'
    const fresh = { ...CANVAS, id: 'canvas:canvas-2', name: 'Fresh Canvas' }
    mocks.workspaceResource.mockResolvedValueOnce({ resource: fresh, ancestors: [ROOT, RESEARCH, ROBOTICS], source: COMPLETE })
    rerender(<CanvasWorkspaceLocation onReturnDestination={onReturnDestination} onNavigate={onNavigate} />)

    await screen.findByText('Fresh Canvas')
    resolveOld({ resource: CANVAS, ancestors: [ROOT], source: COMPLETE })
    await waitFor(() => expect(screen.getByText('Fresh Canvas')).toBeInTheDocument())
    expect(screen.queryByText('Purchases per user')).not.toBeInTheDocument()
  })
})
