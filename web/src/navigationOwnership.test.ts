import { describe, expect, it } from 'vitest'
import { startNavigation } from './navigationOwnership'
import { useStore } from './store/graph'

describe('navigation ownership', () => {
  it('does not let a stale route-owned shell projection overwrite an explicit destination', () => {
    const routeToken = startNavigation()
    useStore.getState().setInboxQuery('')

    useStore.getState().applyRoute({
      view: 'workspace', workspaceResourceId: 'dataset:exact', workspaceScope: 'datasets',
      workspaceDatasetQuery: 'dq=robot',
    }, routeToken)

    expect(useStore.getState().view).toBe('inbox')
    expect(useStore.getState().workspaceResourceId).not.toBe('dataset:exact')
  })

  it('applies an owned exact Workspace route atomically', () => {
    const routeToken = startNavigation()
    useStore.getState().applyRoute({
      view: 'workspace', workspaceResourceId: 'dataset:exact', workspaceScope: 'datasets',
      workspaceDatasetQuery: 'dq=robot&sort=updated',
    }, routeToken)

    expect(useStore.getState()).toMatchObject({
      view: 'workspace', workspaceResourceId: 'dataset:exact', workspaceScope: 'datasets',
      workspaceDatasetQuery: 'dq=robot&sort=updated',
    })
  })

  it('clears incompatible Workspace filters with the exact Canvas parent atomically', () => {
    useStore.setState({
      view: 'canvas', workspaceScope: 'datasets', workspaceResourceId: null,
      workspaceSearchQuery: 'old search', workspaceDatasetQuery: 'dq=robot&folder=research',
    })

    useStore.getState().switchWorkspaceScope('all', {
      resourceId: 'container:exact-parent', searchQuery: '', datasetQuery: '',
    })

    expect(useStore.getState()).toMatchObject({
      view: 'workspace', workspaceScope: 'all', workspaceResourceId: 'container:exact-parent',
      workspaceSearchQuery: '', workspaceDatasetQuery: '',
    })
  })
})
