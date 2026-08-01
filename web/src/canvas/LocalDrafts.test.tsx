import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { LocalCanvasDraft } from '../store/canvasDrafts'

const draft: LocalCanvasDraft = {
  draftId: 'draft-1',
  principalId: 'alice',
  canvasId: 'canvas-1',
  baseCanvasId: null,
  baseVersion: null,
  name: 'Offline analysis',
  doc: { id: 'canvas-1', version: 1, name: 'Offline analysis', requirements: [], nodes: [], edges: [] },
  createAttemptDoc: null,
  syncState: 'dirty',
  lastLocalEditAt: '2026-08-01T12:00:00.000Z',
}

const store = vi.hoisted(() => ({
  kernelUp: true,
  localDrafts: [] as LocalCanvasDraft[],
  draftStorageErrors: [] as string[],
  currentDraftId: null as string | null,
  retryLocalDraft: vi.fn(),
  forkLocalDraft: vi.fn(),
  discardLocalDraft: vi.fn(),
  exportLocalDraft: vi.fn(),
  openFile: vi.fn(),
  openLocalDraft: vi.fn(),
}))

vi.mock('../store/graph', () => ({
  useStore: (selector: (state: typeof store) => unknown) => selector(store),
}))

import { WorkspaceLocalDrafts } from './LocalDrafts'

describe('Local draft actions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    store.localDrafts = [draft]
  })

  it('requires an in-app confirmation before permanently deleting a draft', async () => {
    const user = userEvent.setup()
    render(<WorkspaceLocalDrafts />)

    await user.click(screen.getByRole('button', { name: 'Delete local draft Offline analysis' }))
    const dialog = screen.getByRole('dialog', { name: 'Delete local draft “Offline analysis”?' })
    expect(dialog).toHaveTextContent('changes saved only in this browser')
    expect(dialog).toHaveTextContent('does not delete a server Canvas')
    expect(store.discardLocalDraft).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(store.discardLocalDraft).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Delete local draft Offline analysis' }))
    await user.click(screen.getByRole('button', { name: 'Delete local draft' }))
    expect(store.discardLocalDraft).toHaveBeenCalledWith('draft-1')
  })

  it('does not offer deletion while a local draft is syncing', async () => {
    store.localDrafts = [{ ...draft, syncState: 'syncing' }]
    render(<WorkspaceLocalDrafts />)

    const remove = screen.getByRole('button', { name: 'Delete local draft Offline analysis' })
    expect(remove).toBeDisabled()
    expect(remove).toHaveAttribute('title', 'Wait for syncing to finish before deleting this draft')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
