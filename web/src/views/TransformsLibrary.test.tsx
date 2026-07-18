import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  transformLibrary: vi.fn(), transformLibraryDetail: vi.fn(), workspaceBrowse: vi.fn(),
  workspaceCreateCanvas: vi.fn(), workspaceAddTransform: vi.fn(), deleteTransformVersion: vi.fn(),
  getCanvas: vi.fn(),
}))
const store = vi.hoisted(() => ({
  transformLibraryQuery: '', transformResourceId: 'tr_exact', transformVersion: 'v1',
  transformUpgradeCanvasId: null as string | null, transformUpgradeNodeId: null as string | null,
  setTransformLibraryQuery: vi.fn(), setTransformResource: vi.fn(),
  files: [
    { id: 'viewer', name: 'Read only', version: 4, role: 'viewer' as const },
    { id: 'target', name: 'Exact target', version: 9, role: 'editor' as const },
  ],
  refreshFiles: vi.fn(), openFile: vi.fn(), select: vi.fn(), pushToast: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: mocks,
  KernelError: class KernelError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))
vi.mock('../store/graph', () => {
  const roleCanEdit = (role: string | null | undefined) => role === 'owner' || role === 'editor'
  const useStore = Object.assign(
    (select: (state: typeof store) => unknown) => select(store),
    { getState: () => store },
  )
  return { useStore, roleCanEdit }
})

import { KernelError } from '../api/client'
import { TransformsLibrary } from './TransformsLibrary'

const schema = [{ name: 'value', fieldId: 'value-id', type: 'int', nullable: false, hasDefault: false }]
const entry = (version = 'v1', availability: 'active' | 'deleted' = 'active') => ({
  id: 'tr_exact', version, title: 'Robot scorer', mode: 'map', category: 'robotics',
  inputColumns: ['value'], inputSchema: schema, outputSchema: schema, requirements: ['pyarrow'],
  paramsSchema: {}, previewable: true, blurb: 'Scores robot observations', provenance: 'promoted' as const,
  availability, versionCount: 2, retention: { canvas: 0, canvasVersion: 0, executionManifest: 0 },
})
const targetDoc = (version = 'v1', canvasVersion = 9) => ({
  id: 'target', name: 'Exact target', version: canvasVersion, edges: [], nodes: [{
    id: 'node-1', type: 'transform', position: { x: 0, y: 0 },
    data: { title: 'Robot scorer', status: 'latest', config: { source: 'library', processor: 'tr_exact', version, mode: 'map' } },
  }],
})

describe('TransformsLibrary', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    store.transformLibraryQuery = ''
    store.transformResourceId = 'tr_exact'
    store.transformVersion = 'v1'
    store.transformUpgradeCanvasId = null
    store.transformUpgradeNodeId = null
    store.refreshFiles.mockResolvedValue(true)
    store.openFile.mockResolvedValue(true)
    mocks.transformLibrary.mockResolvedValue({ items: [entry()], hasMore: false, nextCursor: null })
    mocks.transformLibraryDetail.mockResolvedValue({
      id: 'tr_exact', provenance: 'promoted', requestedVersion: 'v1', versions: [entry('v2'), entry('v1')],
    })
    mocks.workspaceBrowse.mockResolvedValue({ container: { id: 'container:workspace-local-root', name: 'Workspace', kind: 'container', version: 1 }, items: [], hasMore: false })
    mocks.workspaceCreateCanvas.mockResolvedValue({ ok: true, id: 'created', created: true, nodeId: 'new-transform', resource: {} })
    mocks.workspaceAddTransform.mockResolvedValue({ ok: true, id: 'target', version: 10, nodeId: 'new-transform', doc: {} })
    mocks.deleteTransformVersion.mockResolvedValue({ ok: true, deleted: true })
  })
  afterEach(() => cleanup())

  it('uses only an explicitly selected editable Canvas and focuses after server confirmation', async () => {
    render(<TransformsLibrary />)
    fireEvent.click(await screen.findByRole('button', { name: 'Use exact v1' }))
    fireEvent.click(screen.getByRole('button', { name: /Add to Canvas/ }))
    expect(screen.getByLabelText('Target Canvas')).toHaveValue('target')
    expect(screen.queryByRole('option', { name: /Read only/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    await waitFor(() => expect(mocks.workspaceAddTransform).toHaveBeenCalledWith('target', {
      transformId: 'tr_exact', transformVersion: 'v1', expectedCanvasVersion: 9,
    }))
    expect(store.openFile).toHaveBeenCalledWith('target', { serverCopy: true })
    expect(store.select).toHaveBeenCalledWith('new-transform')
  })

  it('keeps a stale target failure in the chooser and never opens a Canvas', async () => {
    mocks.workspaceAddTransform.mockRejectedValue(new KernelError(409, 'canvas changed'))
    render(<TransformsLibrary />)
    fireEvent.click(await screen.findByRole('button', { name: 'Use exact v1' }))
    fireEvent.click(screen.getByRole('button', { name: /Add to Canvas/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('no other Canvas was changed')
    expect(store.openFile).not.toHaveBeenCalled()
  })

  it('reloads the exact Workspace root after a stale new-Canvas destination', async () => {
    mocks.workspaceCreateCanvas
      .mockRejectedValueOnce(new KernelError(409, 'workspace changed'))
      .mockResolvedValueOnce({ ok: true, id: 'created', created: true, nodeId: 'new-transform', resource: {} })
    mocks.workspaceBrowse
      .mockResolvedValueOnce({ container: { id: 'container:workspace-local-root', name: 'Workspace', kind: 'container', version: 1 }, items: [], hasMore: false })
      .mockResolvedValueOnce({ container: { id: 'container:workspace-local-root', name: 'Workspace', kind: 'container', version: 2 }, items: [], hasMore: false })
    render(<TransformsLibrary />)
    fireEvent.click(await screen.findByRole('button', { name: 'Use exact v1' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create and open' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('no other Canvas was changed')
    await waitFor(() => expect(mocks.workspaceBrowse).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    await waitFor(() => expect(mocks.workspaceCreateCanvas).toHaveBeenLastCalledWith(expect.objectContaining({
      expectedContainerVersion: 2,
    })))
  })

  it('keeps the chooser and its exact target locked while a mutation is in flight', async () => {
    let resolveMutation: ((value: { ok: boolean; id: string; version: number; nodeId: string; doc: object }) => void) | undefined
    mocks.workspaceAddTransform.mockImplementation(() => new Promise((resolve) => { resolveMutation = resolve }))
    render(<TransformsLibrary />)
    fireEvent.click(await screen.findByRole('button', { name: 'Use exact v1' }))
    fireEvent.click(screen.getByRole('button', { name: /Add to Canvas/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))

    const dialog = screen.getByRole('dialog')
    expect(screen.getByRole('button', { name: 'Close' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Create new Canvas/ })).toBeDisabled()
    expect(screen.getByLabelText('Target Canvas')).toBeDisabled()
    fireEvent.click(dialog.parentElement!)
    expect(screen.getByRole('dialog')).toBeVisible()

    resolveMutation?.({ ok: true, id: 'target', version: 10, nodeId: 'new-transform', doc: {} })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(store.openFile).toHaveBeenCalledWith('target', { serverCopy: true })
  })

  it('closes after a committed mutation even when the target cannot be opened locally', async () => {
    store.openFile.mockResolvedValue(false)
    render(<TransformsLibrary />)
    fireEvent.click(await screen.findByRole('button', { name: 'Use exact v1' }))
    fireEvent.click(screen.getByRole('button', { name: /Add to Canvas/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add and open' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(store.pushToast).toHaveBeenCalledWith(expect.stringContaining('could not be opened'), 'info')
  })

  it('does not replay a committed new-Canvas mutation when its target cannot be opened locally', async () => {
    store.openFile.mockResolvedValue(false)
    render(<TransformsLibrary />)
    fireEvent.click(await screen.findByRole('button', { name: 'Use exact v1' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create and open' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(mocks.workspaceCreateCanvas).toHaveBeenCalledTimes(1)
    expect(store.pushToast).toHaveBeenCalledWith(expect.stringContaining('could not be opened'), 'info')
  })

  it('does not offer an upgrade from hidden Canvas state without explicit context', async () => {
    store.transformVersion = 'v2'
    render(<TransformsLibrary />)
    expect(await screen.findByRole('button', { name: 'Use exact v2' })).toBeVisible()
    expect(screen.queryByText('Explicit upgrade target')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Confirm exact upgrade/ })).not.toBeInTheDocument()
  })

  it('reloads a deep-linked explicit target and admits only a compatible exact upgrade', async () => {
    store.transformVersion = 'v2'
    store.transformUpgradeCanvasId = 'target'
    store.transformUpgradeNodeId = 'node-1'
    mocks.transformLibraryDetail.mockResolvedValue({
      id: 'tr_exact', provenance: 'promoted', requestedVersion: 'v2',
      versions: [{
        ...entry('v2'), outputSchema: [
          ...schema,
          { name: 'confidence', fieldId: 'confidence-id', type: 'float64', nullable: true, hasDefault: false },
        ],
      }, entry('v1')],
    })
    mocks.getCanvas
      .mockResolvedValueOnce(targetDoc())
      .mockResolvedValueOnce(targetDoc('v2', 10))
    render(<TransformsLibrary />)
    expect(await screen.findByText(/Exact target/)).toBeVisible()
    expect(screen.getByText('+ confidence')).toBeVisible()
    expect(screen.getByText('nullable field was added')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm exact upgrade to v2' }))
    await waitFor(() => expect(mocks.workspaceAddTransform).toHaveBeenCalledWith('target', {
      transformId: 'tr_exact', transformVersion: 'v2', expectedCanvasVersion: 9, replaceNodeId: 'node-1',
    }))
  })

  it('keeps a committed upgrade non-retryable when its Canvas cannot be opened', async () => {
    store.transformVersion = 'v2'
    store.transformUpgradeCanvasId = 'target'
    store.transformUpgradeNodeId = 'node-1'
    mocks.getCanvas
      .mockResolvedValueOnce(targetDoc())
      .mockResolvedValueOnce(targetDoc('v2', 10))
    store.openFile.mockResolvedValue(false)
    render(<TransformsLibrary />)
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm exact upgrade to v2' }))
    await waitFor(() => expect(store.pushToast).toHaveBeenCalledWith(
      expect.stringContaining('could not be opened'), 'info'))
    await waitFor(() => expect(screen.queryByRole('button', {
      name: 'Confirm exact upgrade to v2',
    })).not.toBeInTheDocument())
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(mocks.workspaceAddTransform).toHaveBeenCalledTimes(1)
  })

  it('refreshes both detail and library immediately after a successful tombstone', async () => {
    mocks.transformLibraryDetail
      .mockResolvedValueOnce({ id: 'tr_exact', provenance: 'promoted', requestedVersion: 'v1', versions: [entry('v1')] })
      .mockResolvedValueOnce({ id: 'tr_exact', provenance: 'promoted', requestedVersion: 'v1', versions: [entry('v1', 'deleted')] })
    mocks.transformLibrary
      .mockResolvedValueOnce({ items: [entry('v1')], hasMore: false, nextCursor: null })
      .mockResolvedValueOnce({ items: [entry('v1', 'deleted')], hasMore: false, nextCursor: null })
    render(<TransformsLibrary />)
    fireEvent.click(await screen.findByText('Delete unreferenced version…'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete exact version' }))
    await waitFor(() => expect(mocks.deleteTransformVersion).toHaveBeenCalledWith('tr_exact', 'v1'))
    await waitFor(() => expect(mocks.transformLibraryDetail.mock.calls.length).toBeGreaterThan(1))
    expect(mocks.transformLibrary.mock.calls.length).toBeGreaterThan(1)
    expect(await screen.findByText('v1 · deleted')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Use exact v1' })).toBeDisabled()
  })

  it('continues a large library only from the server cursor', async () => {
    mocks.transformLibrary
      .mockResolvedValueOnce({ items: [entry()], hasMore: true, nextCursor: 'cursor-2' })
      .mockResolvedValueOnce({ items: [{ ...entry(), id: 'tr_second', title: 'Second' }], hasMore: false, nextCursor: null })
    render(<TransformsLibrary />)
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(mocks.transformLibrary).toHaveBeenLastCalledWith({
      q: '', source: 'all', mode: '', category: '', limit: 25, cursor: 'cursor-2',
    }))
    expect(await screen.findByText('Second')).toBeVisible()
  })

  it('discards an old load-more response after the library filters change', async () => {
    let resolveOldPage: ((value: { items: ReturnType<typeof entry>[]; hasMore: boolean; nextCursor: null }) => void) | undefined
    mocks.transformLibrary
      .mockResolvedValueOnce({ items: [entry()], hasMore: true, nextCursor: 'old-cursor' })
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOldPage = resolve }))
      .mockResolvedValueOnce({
        items: [{ ...entry(), id: 'tr_filtered', title: 'Filtered result' }],
        hasMore: false, nextCursor: null,
      })
    const { rerender } = render(<TransformsLibrary />)
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }))

    store.transformLibraryQuery = 'q=filtered'
    rerender(<TransformsLibrary />)
    expect(await screen.findByText('Filtered result')).toBeVisible()
    resolveOldPage?.({
      items: [{ ...entry(), id: 'tr_old_page', title: 'Old page result' }],
      hasMore: false, nextCursor: null,
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.queryByText('Old page result')).not.toBeInTheDocument()
  })
})
