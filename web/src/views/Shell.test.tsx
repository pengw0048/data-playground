import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  inboxUnreadCount: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mocks }))
vi.mock('../panels/SettingsModal', () => ({ SettingsModal: () => null }))
vi.mock('./ERDiagram', () => ({ ERDiagram: () => <div>relationships view</div> }))
vi.mock('./WorkspaceExplorer', () => ({ WorkspaceExplorer: () => <div>workspace view</div> }))
vi.mock('./JobsView', () => ({ JobsView: () => <div>jobs view</div> }))
vi.mock('./InboxView', () => ({
  InboxView: ({ onUnreadChange }: { onUnreadChange?: () => void }) => (
    <div>
      inbox view
      <button type="button" onClick={onUnreadChange}>refresh unread count</button>
    </div>
  ),
}))
vi.mock('./TransformsLibrary', () => ({ TransformsLibrary: () => <div>transforms view</div> }))

import { useStore } from '../store/graph'
import { Shell } from './Shell'

describe('Shell primary navigation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.inboxUnreadCount.mockResolvedValue({ count: 0 })
    useStore.setState({
      view: 'inbox',
      workspaceResourceId: 'dataset:events',
      workspaceScope: 'all',
      currentUser: { id: 'local', name: 'Local' },
      authEnabled: false,
    } as never)
  })

  it('opens Workspace home instead of restoring a stale resource dialog', async () => {
    render(<Shell />)

    expect(screen.queryByText('local mode')).not.toBeInTheDocument()
    expect(screen.queryByText('signed in')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('rail-collapse'))
    const productMark = screen.getByTestId('workspace-product-mark')
    expect(productMark).toContainHTML('<svg')
    expect(productMark).toHaveTextContent('')

    fireEvent.click(screen.getByTestId('rail-workspace'))

    expect(useStore.getState()).toMatchObject({
      view: 'workspace',
      workspaceResourceId: null,
      workspaceScope: 'all',
    })
    expect(screen.getByText('workspace view')).toBeVisible()
    await waitFor(() => expect(mocks.inboxUnreadCount).toHaveBeenCalled())
  })

  it('opens top-level Jobs and Transforms instead of retaining unavailable details', () => {
    useStore.setState({
      view: 'jobs',
      jobsQuery: 'status=failed&canvas=demo-canvas&run=missing-run&output=write%3Aout&report=missing-report&compare=missing-comparison',
      transformResourceId: 'missing-transform',
      transformVersion: 'v9',
    } as never)
    render(<Shell />)

    fireEvent.click(screen.getByTestId('rail-jobs'))
    expect(useStore.getState()).toMatchObject({ view: 'jobs', jobsQuery: 'status=failed&canvas=demo-canvas' })

    useStore.setState({ view: 'transforms' } as never)
    fireEvent.click(screen.getByTestId('rail-transforms'))
    expect(useStore.getState()).toMatchObject({
      view: 'transforms', transformResourceId: null, transformVersion: null,
      transformUpgradeCanvasId: null, transformUpgradeNodeId: null,
    })
  })

  it('labels an authenticated identity as signed in', () => {
    useStore.setState({ authEnabled: true } as never)

    render(<Shell />)

    expect(screen.getByText('signed in')).toBeVisible()
    expect(screen.queryByText('local mode')).not.toBeInTheDocument()
  })

  it('marks the current view in the navigation landmark', () => {
    render(<Shell />)
    const nav = screen.getByRole('navigation', { name: 'Primary navigation' })

    expect(screen.getByTestId('rail-inbox')).toHaveAttribute('aria-current', 'page')
    for (const other of ['workspace', 'jobs', 'transforms']) {
      expect(screen.getByTestId(`rail-${other}`)).not.toHaveAttribute('aria-current')
    }
    expect(nav).toContainElement(screen.getByTestId('rail-inbox'))
  })

  it('does not let an older unread-count response overwrite a mark-all refresh', async () => {
    let resolveOlder: (value: { count: number }) => void = () => {}
    let resolveLatest: (value: { count: number }) => void = () => {}
    mocks.inboxUnreadCount
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOlder = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveLatest = resolve }))

    render(<Shell />)
    await waitFor(() => expect(mocks.inboxUnreadCount).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'refresh unread count' }))
    await waitFor(() => expect(mocks.inboxUnreadCount).toHaveBeenCalledTimes(2))

    await act(async () => { resolveLatest({ count: 0 }) })
    await act(async () => { resolveOlder({ count: 7 }) })

    expect(screen.queryByTestId('inbox-unread-badge')).not.toBeInTheDocument()
  })
})
