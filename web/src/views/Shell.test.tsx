import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  inboxUnreadCount: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mocks }))
vi.mock('../panels/SettingsModal', () => ({ SettingsModal: () => null }))
vi.mock('./ERDiagram', () => ({ ERDiagram: () => <div>relationships view</div> }))
vi.mock('./WorkspaceExplorer', () => ({ WorkspaceExplorer: () => <div>workspace view</div> }))
vi.mock('./JobsView', () => ({ JobsView: () => <div>jobs view</div> }))
vi.mock('./InboxView', () => ({ InboxView: () => <div>inbox view</div> }))
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

    fireEvent.click(screen.getByTestId('rail-workspace'))

    expect(useStore.getState()).toMatchObject({
      view: 'workspace',
      workspaceResourceId: null,
      workspaceScope: 'all',
    })
    expect(screen.getByText('workspace view')).toBeVisible()
    await waitFor(() => expect(mocks.inboxUnreadCount).toHaveBeenCalled())
  })
})
