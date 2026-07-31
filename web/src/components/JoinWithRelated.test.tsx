import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

const inspectorTrigger = 'Find join candidates'
const leftInputTrigger = 'Find join candidates · left'
const dialogName = 'Find join candidates'

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
    name: 'orders', folder: '', reason: 'Matching key columns',
    evidence: 'schema_match', evidenceStatus: 'inferred',
    leftColumns: ['id'], rightColumns: ['id'], cardinality: '1:N',
    cardinalityState: 'available',
    cardinalityReason: 'Measured across both current dataset versions.',
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

  it('keeps Source discovery in the Inspector and out of the Canvas card', async () => {
    const { rerender } = render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    expect(await screen.findByRole('dialog', { name: dialogName })).toBeVisible()

    rerender(<JoinWithRelated nodeId="source-1" surface="canvas" />)
    expect(screen.queryByTestId('join-with-related-canvas-source-1')).toBeNull()
  })

  it('separates evidence, keeps unknown explicit, and cancellation mutates nothing', async () => {
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    await screen.findByText('Related data')
    expect(screen.queryByText('Possible key matches')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show possible key matches (1)' }))
    expect(screen.getByText('Possible key matches')).toBeVisible()
    expect(screen.getByText(/Results are truncated/)).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    expect(screen.getByText('Not measured')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Add Join' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(mocks.confirm).not.toHaveBeenCalled()
    expect(mocks.loadDoc).not.toHaveBeenCalled()
  })

  it('keeps declared and reference-backed relationships strong while making name-only matches explicit and neutral', async () => {
    const declaredCandidate = {
      ...page.candidates[0],
      identity: { kind: 'local', registrationId: 'reg-accounts', revisionMode: 'current' },
      name: 'accounts',
      reason: 'Persisted by a catalog owner',
      evidence: 'declared_relationship',
    }
    mocks.related.mockResolvedValueOnce({
      ...page,
      candidates: [declaredCandidate, page.candidates[0]],
      possibleMatches: [{ ...page.possibleMatches[0], cardinality: '1:1' }],
      truncated: false,
      refinementRequired: false,
    })

    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    const dialog = await screen.findByRole('dialog', { name: dialogName })
    expect(within(dialog).getByText(/declared or reference-backed relationships and possible key matches/))
      .toBeVisible()

    const declaredCard = within(dialog).getByRole('button', { name: /accounts/ })
    expect(within(declaredCard).getByText(/Declared catalog relationship/)).toBeVisible()
    expect(within(declaredCard).queryByText(/No relationship is declared/)).toBeNull()
    const referenceCard = within(dialog).getByRole('button', { name: /users/ })
    expect(within(referenceCard).getByText(/Declared key\/reference/)).toBeVisible()
    expect(within(referenceCard).queryByText(/No relationship is declared/)).toBeNull()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Show possible key matches (1)' }))
    expect(within(dialog).getByText('Possible key matches')).toBeVisible()
    expect(within(dialog).getByText(/Matching key names only suggest where a join may be possible/))
      .toBeVisible()
    const possibleCard = within(dialog).getByRole('button', { name: /orders/ })
    expect(possibleCard).toHaveClass('border-dashed', 'bg-muted/20')
    expect(within(possibleCard).getByText('Suggested', { exact: true })).toBeVisible()
    expect(within(possibleCard).getByText('Matching column names', { exact: true })).toBeVisible()
    expect(within(possibleCard).queryByText(/No relationship is declared/)).toBeNull()
    const possibleCardinality = within(possibleCard).getByText('1:1', { exact: true })
    expect(possibleCardinality).toHaveClass('bg-muted', 'text-muted-foreground')
    expect(possibleCardinality).not.toHaveClass('bg-green-100')

    fireEvent.click(possibleCard)
    const review = screen.getByTestId('possible-key-match-review')
    expect(review).toHaveTextContent('Suggested from matching column names')
    expect(review).toHaveTextContent('no catalog relationship is declared')
    const reviewedCardinality = within(dialog).getByText('1:1', { exact: true })
    expect(reviewedCardinality).toHaveClass('bg-muted', 'text-muted-foreground')
    expect(reviewedCardinality).not.toHaveClass('bg-green-100')
    expect(within(dialog).queryByText(/Cardinality describes row matching/)).toBeNull()
  })

  it('removes stale possible matches while a scoped search is pending', async () => {
    let resolveFiltered!: (value: typeof page) => void
    mocks.related
      .mockResolvedValueOnce(page)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFiltered = resolve }))

    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: 'Show possible key matches (1)' }))
    expect(screen.getByRole('button', { name: /orders/ })).toBeVisible()

    fireEvent.change(screen.getByPlaceholderText('Dataset, column, tag…'), {
      target: { value: 'images' },
    })
    expect(screen.queryByRole('button', { name: /orders/ })).toBeNull()
    expect(screen.getByText('Finding bounded candidates…')).toBeVisible()
    await waitFor(() => expect(mocks.related).toHaveBeenCalledTimes(2))

    resolveFiltered({
      ...page,
      candidates: [],
      possibleMatches: [{ ...page.possibleMatches[0], name: 'images' }],
    })
    const disclosure = await screen.findByRole('button', { name: 'Show possible key matches (1)' })
    fireEvent.click(disclosure)
    fireEvent.click(screen.getByRole('button', { name: /images/ }))
    expect(screen.getByTestId('possible-key-match-review')).toHaveTextContent('Suggested')
  })

  it('does not disclose stale possible matches while a filtered search is still debouncing', async () => {
    let resolveInitial!: (value: typeof page) => void
    let resolveFiltered!: (value: typeof page) => void
    mocks.related
      .mockImplementationOnce(() => new Promise((resolve) => { resolveInitial = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFiltered = resolve }))

    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    await waitFor(() => expect(mocks.related).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByPlaceholderText('Dataset, column, tag…'), { target: { value: 'images' } })
    resolveInitial(page)
    await new Promise<void>((resolve) => window.setTimeout(resolve, 0))

    expect(screen.queryByRole('button', { name: 'Show possible key matches (1)' })).toBeNull()
    expect(screen.queryByRole('button', { name: /orders/ })).toBeNull()
    await waitFor(() => expect(mocks.related).toHaveBeenCalledTimes(2))
    resolveFiltered({
      ...page,
      candidates: [],
      possibleMatches: [{ ...page.possibleMatches[0], name: 'images' }],
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Show possible key matches (1)' }))
    fireEvent.click(screen.getByRole('button', { name: /images/ }))
    expect(screen.getByTestId('possible-key-match-review')).toHaveTextContent('Suggested')
  })

  it('shows measured cardinality evidence without stale unmeasured copy', async () => {
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: 'Show possible key matches (1)' }))

    const card = screen.getByRole('button', { name: /orders/ })
    expect(within(card).getByText('Matching column names')).toBeVisible()
    expect(within(card).getByText('1:N')).toHaveAttribute(
      'title', 'Measured across both current dataset versions.',
    )
    expect(screen.queryByText(/cardinality not measurable here/)).toBeNull()
    expect(screen.queryByText(/joined rows may multiply/)).toBeNull()
  })

  it('orients measured cardinality without embedding the opposite direction in its evidence', async () => {
    mocks.state.doc = {
      ...mocks.state.doc,
      nodes: [
        ...mocks.state.doc.nodes,
        { id: 'join-1', type: 'join', position: { x: 100, y: 0 },
          data: { title: 'Join', status: 'draft', config: {} } },
      ],
      edges: [{
        id: 'source-b', source: 'source-1', sourceHandle: 'out',
        target: 'join-1', targetHandle: 'b', data: { wire: 'dataset' },
      }],
    }

    render(<JoinWithRelated nodeId="join-1" surface="canvas" />)
    fireEvent.click(screen.getByRole('button', {
      name: leftInputTrigger,
    }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: 'Show possible key matches (1)' }))

    const candidate = screen.getByRole('button', { name: /orders/ })
    expect(within(candidate).getByText('N:1', { exact: true })).toBeVisible()
    expect(within(candidate).getByText('Matching column names')).toBeVisible()
    expect(within(candidate).getByText('N:1', { exact: true }))
      .toHaveAttribute('title', 'Measured across both current dataset versions.')
    expect(within(candidate).queryByText('1:N', { exact: true })).toBeNull()
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
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    fireEvent.click(screen.getByTestId('confirm-related-join'))

    await screen.findByText('Reapply to latest Canvas')
    expect(screen.getByText('Right input (b)').parentElement).toHaveTextContent('users')
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
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    expect(await screen.findByTestId('related-no-results'))
      .toHaveTextContent('No related data or possible key matches')
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
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await screen.findByRole('option', { name: /Retained version 1/ })
    fireEvent.change(screen.getByLabelText('Related dataset version'), { target: { value: 'rev-2' } })
    await waitFor(() => expect(mocks.reviewRevision).toHaveBeenCalledWith(
      page.source, page.candidates[0], 'rev-2', expect.any(Object),
    ))
    expect(screen.getByText(/Fan-out was not measured.*exact revision/)).toBeVisible()
  })

  it('keeps a failed exact choice selected and cannot confirm the current candidate by mistake', async () => {
    mocks.reviewRevision.mockRejectedValueOnce(new Error('revision rev-2 is unavailable'))
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
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
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
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
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await waitFor(() => expect(mocks.relatedRevisions).toHaveBeenCalled())
    expect(screen.queryByLabelText('Related dataset version')).toBeNull()
    expect(screen.queryByText('Version history is unavailable for this dataset.')).toBeNull()
    expect(screen.getByTestId('confirm-related-join')).toBeEnabled()
  })

  it('keeps opaque bindings and diagnostic codes out of the default review', async () => {
    mocks.relatedRevisions.mockRejectedValueOnce(new Error('related_dataset_revision_history_unavailable'))
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    await screen.findByText('Related data')
    fireEvent.click(screen.getByRole('button', { name: /users/ }))
    await waitFor(() => expect(mocks.relatedRevisions).toHaveBeenCalled())
    expect(screen.getByText('Not measured')).toBeVisible()
    expect(screen.queryByText('Version history is unavailable for this dataset.')).toBeNull()
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
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
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
    const opener = screen.getByRole('button', { name: inspectorTrigger })
    fireEvent.click(opener)
    await screen.findByText('Related data')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(opener).toHaveFocus())
  })

  it('uses the related-data modal isolation contract and ignores Delete', async () => {
    render(<JoinWithRelated nodeId="source-1" />)
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    await screen.findByText('Related data')
    const dialog = screen.getByRole('dialog', { name: dialogName })
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
    fireEvent.click(screen.getByRole('button', { name: inspectorTrigger }))
    await waitFor(() => expect(mocks.related).toHaveBeenCalledWith(
      expect.objectContaining({ revisionMode: 'exact', revisionId: 'source-v4' }), expect.any(Object),
    ))
  })

  it('never offers the Source action on the Canvas card', () => {
    const { rerender } = render(<JoinWithRelated nodeId="source-1" surface="canvas" />)
    expect(screen.queryByTestId('join-with-related-canvas-source-1')).toBeNull()
    mocks.state.doc.nodes[0].data.config = {
      uri: 'events.parquet', tableId: 'tbl-events',
      providerMountId: '', providerSourceBindingId: '',
    }
    rerender(<JoinWithRelated nodeId="source-1" surface="canvas" />)
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
      .toHaveAccessibleName(leftInputTrigger)
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
    fireEvent.click(screen.getByRole('button', { name: leftInputTrigger }))
    await screen.findByText('Related data')
    expect(screen.getByText('a.id = b.user_id')).toBeVisible()
    expect(screen.getByText('N:1')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /users/ }))

    expect(screen.getByText('Left input (a)').parentElement).toHaveTextContent('users')
    expect(screen.getByText('Right input (b)').parentElement).toHaveTextContent('events')
    expect(screen.getByText('a.id = b.user_id')).toBeVisible()
    expect(screen.getByText('N:1')).toBeVisible()

    fireEvent.change(screen.getByLabelText('Join type'), { target: { value: 'left' } })
    expect(screen.getByLabelText('Join type')).toHaveValue('left')
    fireEvent.change(screen.getByLabelText('Join type'), { target: { value: 'right' } })
    expect(screen.getByLabelText('Join type')).toHaveValue('right')
  })
})
