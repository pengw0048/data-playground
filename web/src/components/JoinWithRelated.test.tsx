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
    confidence: 'verified',
  }, {
    identity: { kind: 'local', registrationId: 'reg-orders', revisionMode: 'current' },
    name: 'orders', folder: '', reason: 'matching key column(s) — cardinality not measurable here',
    evidence: 'schema_match', evidenceStatus: 'inferred',
    leftColumns: ['id'], rightColumns: ['id'], cardinality: '1:N',
    confidence: 'verified', warning: 'This join is 1:N: right fans out, so rows may multiply.',
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
    await screen.findByText('Declared and proven references')
    expect(screen.getByText('Inferred candidates')).toBeVisible()
    expect(screen.getByText(/Results are truncated/)).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    expect(screen.getByText(/not verified; selectable with caution/)).toBeVisible()
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
    await screen.findByText('Declared and proven references')
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
    mocks.related.mockResolvedValueOnce({ ...page, candidates: [], truncated: false, refinementRequired: false })
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    expect(await screen.findByTestId('related-no-results')).toHaveTextContent('No related datasets')
    expect(mocks.confirm).not.toHaveBeenCalled()
  })

  it('re-reviews a retained revision before it can be confirmed', async () => {
    mocks.reviewRevision.mockResolvedValue({
      ...page.candidates[0],
      identity: { kind: 'local', registrationId: 'reg-users', revisionMode: 'exact', revisionId: 'rev-2' },
      exactRef: { kind: 'exact', datasetId: 'reg-users', revisionId: 'rev-2' },
      cardinality: 'unknown', confidence: 'inferred',
    })
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await screen.findByText('Declared and proven references')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByRole('option', { name: /rev-2/ })
    fireEvent.change(screen.getByLabelText('Related dataset version'), { target: { value: 'rev-2' } })
    await waitFor(() => expect(mocks.reviewRevision).toHaveBeenCalledWith(
      page.source, page.candidates[0], 'rev-2', expect.any(Object),
    ))
    expect(screen.getByText('reg-users@rev-2')).toBeVisible()
  })

  it('keeps a failed exact choice selected and cannot confirm the current candidate by mistake', async () => {
    mocks.reviewRevision.mockRejectedValueOnce(new Error('revision rev-2 is unavailable'))
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await screen.findByText('Declared and proven references')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByRole('option', { name: /rev-2/ })
    fireEvent.change(screen.getByLabelText('Related dataset version'), { target: { value: 'rev-2' } })

    await screen.findByText(/revision rev-2 is unavailable/)
    expect(screen.getByLabelText('Related dataset version')).toHaveValue('rev-2')
    expect(screen.getByTestId('confirm-related-join')).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry selected version' }))
    await waitFor(() => expect(mocks.reviewRevision).toHaveBeenCalledTimes(2))
    expect(mocks.confirm).not.toHaveBeenCalled()
  })

  it('refreshes both the reviewed candidate and its revision base after a stale exact review', async () => {
    const refreshed = {
      ...page,
      candidates: [{ ...page.candidates[0], reason: 'refreshed review base' }, page.candidates[1]],
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
    await screen.findByText('Declared and proven references')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByRole('option', { name: /rev-2/ })
    fireEvent.change(screen.getByLabelText('Related dataset version'), { target: { value: 'rev-2' } })
    await screen.findByText('reg-users@rev-2')
    fireEvent.click(screen.getByTestId('confirm-related-join'))
    await screen.findByRole('button', { name: 'Refresh review' })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh review' }))
    await screen.findByText(/refreshed review base/)
    expect(screen.getByText('reg-users@current')).toBeVisible()
    fireEvent.change(screen.getByLabelText('Related dataset version'), { target: { value: 'rev-2' } })
    await waitFor(() => expect(mocks.reviewRevision).toHaveBeenLastCalledWith(
      page.source, expect.objectContaining({ reason: 'refreshed review base' }), 'rev-2', expect.any(Object),
    ))
  })

  it('keeps the current candidate confirmable when revision history is unavailable', async () => {
    mocks.relatedRevisions.mockRejectedValueOnce(new Error('provider history unavailable'))
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Join with…' }))
    await screen.findByText('Declared and proven references')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByText(/provider history unavailable/)
    expect(screen.getByTestId('confirm-related-join')).toBeEnabled()
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
    await screen.findByText('Declared and proven references')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByRole('option', { name: /rev-2/ })
    fireEvent.change(screen.getByLabelText('Related dataset version'), { target: { value: 'rev-2' } })
    await waitFor(() => expect(screen.getByText('reg-users@rev-2')).toBeVisible())
    fireEvent.click(screen.getByRole('button', { name: 'Load more versions' }))
    await waitFor(() => expect(mocks.relatedRevisions).toHaveBeenLastCalledWith(
      page.candidates[0].identity, { limit: 20, cursor: 'next-page' },
    ))
    expect(screen.getByText('reg-users@rev-2')).toBeVisible()
    expect(screen.getByRole('option', { name: /rev-1/ })).toBeVisible()
  })

  it('closes with Escape and restores focus to the opener', async () => {
    render(<JoinWithRelated nodeId="source-1" />)
    const opener = screen.getByRole('button', { name: 'Join with…' })
    fireEvent.click(opener)
    await screen.findByText('Declared and proven references')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(opener).toHaveFocus())
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
})
