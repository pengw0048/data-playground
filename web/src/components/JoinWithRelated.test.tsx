import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  state: {} as any,
  related: vi.fn(),
  relatedRevisions: vi.fn(),
  reviewRevision: vi.fn(),
  confirm: vi.fn(),
  getCanvas: vi.fn(),
  loadDoc: vi.fn(),
  select: vi.fn(),
  toast: vi.fn(),
}))

vi.mock('../store/graph', () => {
  const useStore = (selector: (state: any) => unknown) => selector(mocks.state)
  useStore.getState = () => mocks.state
  return { roleCanEdit: () => true, useStore }
})
vi.mock('../api/client', () => ({
  api: {
    relatedDatasets: mocks.related,
    relatedDatasetRevisions: mocks.relatedRevisions,
    reviewRelatedDatasetRevision: mocks.reviewRevision,
    joinWithRelated: mocks.confirm,
    getCanvas: mocks.getCanvas,
  },
}))

import { JoinWithRelated } from './JoinWithRelated'

const page = {
  source: { kind: 'local', registrationId: 'reg-events', revisionMode: 'current' },
  sourceName: 'events',
  candidates: [{
    identity: { kind: 'local', registrationId: 'reg-users', revisionMode: 'current' },
    name: 'users', folder: 'curated', reason: 'events.user_id references users',
    evidence: 'typed_reference', evidenceStatus: 'proven',
    leftColumns: ['user_id'], rightColumns: ['id'], cardinality: 'unknown',
    cardinalityState: 'unmeasured',
    confidence: 'verified',
  }],
  possibleMatches: [{
    identity: { kind: 'local', registrationId: 'reg-orders', revisionMode: 'current' },
    name: 'orders', folder: '', reason: 'matching key column(s) — cardinality not measurable here',
    evidence: 'schema_match', evidenceStatus: 'inferred',
    leftColumns: ['id'], rightColumns: ['id'], cardinality: '1:N',
    cardinalityState: 'available',
    confidence: 'inferred', warning: 'This join is 1:N: right fans out, so rows may multiply.',
  }],
  excluded: [],
  limit: 12,
  inspected: 20,
  truncated: true,
  refinementRequired: true,
}

describe('JoinWithRelated', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.state = {
      canvasRole: 'owner',
      serverVersion: 3,
      currentDraftId: null,
      doc: {
        id: 'canvas-1', version: 3, nodes: [{
          id: 'source-1', type: 'source', position: { x: 0, y: 0 },
          data: { title: 'events', status: 'draft', config: { uri: 'events.parquet', tableId: 'tbl-events', registrationId: 'reg-events' } },
        }], edges: [],
      },
      loadDoc: mocks.loadDoc,
      select: mocks.select,
      pushToast: mocks.toast,
    }
    mocks.related.mockResolvedValue(page)
    mocks.relatedRevisions.mockResolvedValue({ items: [{
      datasetId: 'reg-users', revisionId: 'rev-2', committedAt: '2026-07-24T12:00:00Z', retentionOwner: 'provider',
    }], nextCursor: null, hasMore: false })
    mocks.getCanvas.mockResolvedValue({ ...mocks.state.doc, version: 4 })
  })

  it('separates evidence, keeps unknown explicit, and cancellation mutates nothing', async () => {
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await screen.findByText('Related data')
    expect(screen.queryByText('Possible matches')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show possible matches (1)' }))
    expect(screen.getByText('Possible matches')).toBeVisible()
    expect(screen.getByText(/Results are truncated/)).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    expect(screen.getByText(/Cardinality is unknown because it was not measured/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(mocks.confirm).not.toHaveBeenCalled()
    expect(mocks.loadDoc).not.toHaveBeenCalled()
  })

  it('keeps the review after a conflict and installs only a confirmed server document', async () => {
    mocks.confirm.mockRejectedValueOnce(new Error("canvas 'canvas-1' changed from expected version 3"))
    mocks.confirm.mockResolvedValueOnce({
      ok: true,
      canvas: { ...mocks.state.doc, version: 5, nodes: [...mocks.state.doc.nodes, { id: 'join-1' }] },
      sourceNodeId: 'source-2',
      joinNodeId: 'join-1',
      version: 5,
    })
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    fireEvent.click(screen.getByTestId('confirm-related-join'))

    await screen.findByText('Reapply to latest Canvas')
    expect(screen.getByText('Related dataset')).toBeVisible()
    expect(mocks.loadDoc).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Reapply to latest Canvas' }))

    await waitFor(() => expect(mocks.confirm).toHaveBeenCalledTimes(2))
    expect(mocks.confirm.mock.calls[1][1].expectedCanvasVersion).toBe(4)
    expect(mocks.loadDoc).toHaveBeenCalledWith(expect.objectContaining({ version: 5 }))
    expect(mocks.select).toHaveBeenCalledWith('join-1')
  })

  it('shows a healthy no-result state separately from provider failure', async () => {
    mocks.related.mockResolvedValueOnce({ ...page, candidates: [], possibleMatches: [], truncated: false, refinementRequired: false })
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    expect(await screen.findByTestId('related-no-results')).toHaveTextContent('No known relationships')
    expect(mocks.confirm).not.toHaveBeenCalled()
  })

  it('re-reviews a retained revision before it can be confirmed', async () => {
    mocks.reviewRevision.mockResolvedValue({
      ...page.candidates[0],
      identity: { kind: 'local', registrationId: 'reg-users', revisionMode: 'exact', revisionId: 'rev-2' },
      exactRef: { kind: 'exact', datasetId: 'reg-users', revisionId: 'rev-2' },
      cardinality: 'unknown', confidence: 'inferred',
      cardinalityState: 'unmeasured',
      cardinalityReason: 'Fan-out was not measured because this join uses an exact revision; measuring it would require scanning the pinned dataset.',
    })
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByRole('option', { name: /Retained version 1/ })
    fireEvent.change(screen.getByLabelText('Related dataset version'), { target: { value: 'rev-2' } })
    await waitFor(() => expect(mocks.reviewRevision).toHaveBeenCalledWith(
      page.source, page.candidates[0], 'rev-2', expect.any(Object),
    ))
    expect(screen.getByText(/Cardinality is unknown because it was not measured/)).toBeVisible()
  })

  it('keeps a failed exact choice selected and cannot confirm the current candidate by mistake', async () => {
    mocks.reviewRevision.mockRejectedValueOnce(new Error('revision rev-2 is unavailable'))
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByRole('option', { name: /Retained version 1/ })
    fireEvent.change(screen.getByLabelText('Related dataset version'), { target: { value: 'rev-2' } })

    await screen.findByText(/Version history could not be loaded/)
    expect(screen.getByLabelText('Related dataset version')).toHaveValue('rev-2')
    expect(screen.getByTestId('confirm-related-join')).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry selected version' }))
    await waitFor(() => expect(mocks.reviewRevision).toHaveBeenCalledTimes(2))
    expect(mocks.confirm).not.toHaveBeenCalled()
  })

  it('refreshes both the reviewed candidate and its revision base after a stale exact review', async () => {
    const refreshed = {
      ...page,
      candidates: [{ ...page.candidates[0], reason: 'refreshed review base' }], possibleMatches: page.possibleMatches,
    }
    mocks.reviewRevision
      .mockResolvedValueOnce({
        ...page.candidates[0],
        identity: { kind: 'local', registrationId: 'reg-users', revisionMode: 'exact', revisionId: 'rev-2' },
        exactRef: { kind: 'exact', datasetId: 'reg-users', revisionId: 'rev-2' },
      })
    mocks.confirm.mockRejectedValueOnce(new Error('dataset revision changed'))
    mocks.related.mockResolvedValueOnce(page).mockResolvedValueOnce(refreshed)
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByRole('option', { name: /Retained version 1/ })
    fireEvent.change(screen.getByLabelText('Related dataset version'), { target: { value: 'rev-2' } })
    await waitFor(() => expect(mocks.reviewRevision).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByTestId('confirm-related-join'))
    await screen.findByRole('button', { name: 'Refresh review' })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh review' }))
    await screen.findByText(/refreshed review base/)
    fireEvent.change(screen.getByLabelText('Related dataset version'), { target: { value: 'rev-2' } })
    await waitFor(() => expect(mocks.reviewRevision).toHaveBeenLastCalledWith(
      page.source, expect.objectContaining({ reason: 'refreshed review base' }), 'rev-2', expect.any(Object),
    ))
  })

  it('keeps the current candidate confirmable when revision history is unavailable', async () => {
    mocks.relatedRevisions.mockRejectedValueOnce(new Error('related_dataset_revision_history_unavailable'))
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByText('Version history is unavailable for this dataset.')
    expect(screen.getByTestId('confirm-related-join')).toBeEnabled()
  })

  it('keeps opaque bindings and diagnostic codes out of the default review', async () => {
    mocks.relatedRevisions.mockRejectedValueOnce(new Error('related_dataset_revision_history_unavailable'))
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByText('Version history is unavailable for this dataset.')
    expect(screen.getByText(/Cardinality is unknown because it was not measured/)).toBeVisible()
    const details = screen.getByText('Details').parentElement!
    expect(details).not.toHaveAttribute('open')
    fireEvent.click(screen.getByText('Details'))
    expect(details).toHaveAttribute('open')
    expect(details).toHaveTextContent('reg-users@current')
    expect(details).toHaveTextContent('related_dataset_revision_history_unavailable')
  })

  it('loads retained revision pages without losing the selected exact revision', async () => {
    mocks.relatedRevisions
      .mockResolvedValueOnce({ items: [{
        datasetId: 'reg-users', revisionId: 'rev-2', retentionOwner: 'provider',
      }], nextCursor: 'next-page', hasMore: true })
      .mockResolvedValueOnce({ items: [{
        datasetId: 'reg-users', revisionId: 'rev-1', retentionOwner: 'provider',
      }], nextCursor: null, hasMore: false })
    mocks.reviewRevision.mockResolvedValue({
      ...page.candidates[0],
      identity: { kind: 'local', registrationId: 'reg-users', revisionMode: 'exact', revisionId: 'rev-2' },
      exactRef: { kind: 'exact', datasetId: 'reg-users', revisionId: 'rev-2' },
      cardinality: 'unknown', confidence: 'inferred',
    })
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByRole('option', { name: /Retained version 1/ })
    fireEvent.change(screen.getByLabelText('Related dataset version'), { target: { value: 'rev-2' } })
    await waitFor(() => expect(mocks.reviewRevision).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'Load more versions' }))
    await waitFor(() => expect(mocks.relatedRevisions).toHaveBeenLastCalledWith(
      page.candidates[0].identity, { limit: 20, cursor: 'next-page' },
    ))
    expect(screen.getByRole('option', { name: /Retained version 1/ })).toBeVisible()
  })

  it('closes with Escape and restores focus to the opener', async () => {
    render(<JoinWithRelated nodeId="source-1" />)
    const opener = screen.getByRole('button', { name: 'Join with…' })
    fireEvent.click(opener)
    await screen.findByText('Related data')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(opener).toHaveFocus())
  })

  it('uses the Canvas modal shortcut-isolation contract and ignores Delete', async () => {
    render(<JoinWithRelated nodeId="source-1" surface="canvas" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with related data' }))
    await screen.findByText('Related data')
    const dialog = screen.getByRole('dialog', { name: 'Join with related data' })
    expect(dialog.parentElement).toHaveClass('dp-modal-overlay')
    const cancel = screen.getByRole('button', { name: 'Cancel', exact: true })
    cancel.focus()
    fireEvent.keyDown(cancel, { key: 'Delete' })
    expect(dialog).toBeVisible()
    expect(mocks.confirm).not.toHaveBeenCalled()
    expect(mocks.loadDoc).not.toHaveBeenCalled()
  })

  it('preserves an existing local exact Source identity for review', async () => {
    mocks.state.doc.nodes[0].data.config.datasetRef = {
      kind: 'exact', datasetId: 'reg-events', revisionId: 'source-v4',
    }
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await waitFor(() => expect(mocks.related).toHaveBeenCalledWith(
      expect.objectContaining({ revisionMode: 'exact', revisionId: 'source-v4' }), expect.any(Object),
    ))
  })

  it('offers the Canvas action only for a canonical Source binding', () => {
    const { unmount } = render(<JoinWithRelated nodeId="source-1" surface="canvas" />)
    expect(screen.getByTestId('join-with-related-canvas-source-1')).toHaveAccessibleName('Join with related data')
    unmount()

    mocks.state.doc.nodes[0].data.config = {
      uri: 'events.parquet', tableId: 'tbl-events',
      providerMountId: '', providerSourceBindingId: '',
    }
    render(<JoinWithRelated nodeId="source-1" surface="canvas" />)
    expect(screen.queryByTestId('join-with-related-canvas-source-1')).toBeNull()
  })

  it('hides both Canvas and Inspector actions for a parameter-bound Source', () => {
    mocks.state.doc.nodes[0].data.config.datasetRef = { parameterRef: 'runtime_dataset' }
    const { unmount } = render(<JoinWithRelated nodeId="source-1" surface="canvas" />)
    expect(screen.queryByTestId('join-with-related-canvas-source-1')).toBeNull()
    unmount()

    render(<JoinWithRelated nodeId="source-1" />)
    expect(screen.queryByTestId('join-with-related-source-1')).toBeNull()
    expect(mocks.related).not.toHaveBeenCalled()
  })

  it('labels the empty side of a one-input Join without guessing a missing port', () => {
    mocks.state.doc.nodes.push({
      id: 'join-1', type: 'join', position: { x: 200, y: 0 },
      data: { title: 'join', status: 'draft', config: {} },
    })
    mocks.state.doc.edges = [{
      id: 'source-to-right', source: 'source-1', target: 'join-1',
      sourceHandle: 'out', targetHandle: 'b',
    }]
    const { unmount } = render(<JoinWithRelated nodeId="join-1" surface="canvas" />)
    expect(screen.getByTestId('join-with-related-canvas-join-1'))
      .toHaveAccessibleName('Join with related data on left input')
    unmount()

    mocks.state.doc.edges[0].targetHandle = null
    render(<JoinWithRelated nodeId="join-1" surface="canvas" />)
    expect(screen.queryByTestId('join-with-related-canvas-join-1')).toBeNull()
  })

  it('orients datasets, keys, cardinality, and join behavior to the actual a/b ports', async () => {
    const swappedPage = {
      ...page,
      candidates: [{
        ...page.candidates[0],
        cardinality: '1:N',
        cardinalityState: 'available',
        warning: 'This join may multiply rows; inspect the resulting Join analysis before running.',
      }],
      truncated: false,
      refinementRequired: false,
    }
    mocks.related.mockResolvedValueOnce(swappedPage)
    mocks.state.doc.nodes.push({
      id: 'join-1', type: 'join', position: { x: 200, y: 0 },
      data: { title: 'join', status: 'draft', config: {} },
    })
    mocks.state.doc.edges = [{
      id: 'source-to-right', source: 'source-1', target: 'join-1',
      sourceHandle: 'out', targetHandle: 'b',
    }]

    render(<JoinWithRelated nodeId="join-1" surface="canvas" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with related data on left input' }))
    await screen.findByText('Related data')
    expect(screen.getByText('a.id = b.user_id')).toBeVisible()
    expect(screen.getByText('N:1')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /users/ }))

    expect(screen.getByText('Left input (a)').parentElement).toHaveTextContent('users')
    expect(screen.getByText('Left input (a)').parentElement).toHaveTextContent('Related dataset')
    expect(screen.getByText('Right input (b)').parentElement).toHaveTextContent('events')
    expect(screen.getByText('Right input (b)').parentElement).toHaveTextContent('Selected dataset')
    expect(screen.getByText('a.id = b.user_id')).toBeVisible()
    expect(screen.getByText('N:1')).toBeVisible()
    expect(screen.getByText(/right input \(b\) row can match multiple left input \(a\) rows/)).toBeVisible()

    fireEvent.change(screen.getByLabelText('Join type'), { target: { value: 'left' } })
    expect(screen.getByTestId('related-join-behavior'))
      .toHaveTextContent('Keeps every row from left input (a): users.')
    fireEvent.change(screen.getByLabelText('Join type'), { target: { value: 'right' } })
    expect(screen.getByTestId('related-join-behavior'))
      .toHaveTextContent('Keeps every row from right input (b): events.')
  })
})
