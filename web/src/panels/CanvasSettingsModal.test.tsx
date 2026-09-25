import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  getShares: vi.fn(),
  addShare: vi.fn(),
  tablesPage: vi.fn(), tableByRegistration: vi.fn(), datasetRevisionCapabilities: vi.fn(),
  resolveDatasetRevision: vi.fn(), datasetRevisions: vi.fn(), datasetRevision: vi.fn(),
  state: {
    doc: {
      id: 'canvas-1', name: 'Revenue canvas', requirements: ['pandas'], parameters: [] as any[],
      resultRetention: { history: 'inherit' as 'inherit' | 'latest' | 'recent' },
    },
    canvasRole: 'owner' as 'owner' | 'editor' | 'viewer' | null,
    authEnabled: true,
    kernelInfo: { resultStorage: { id: 'workspace-managed', label: 'Local workspace', kind: 'local' } },
    renameFile: vi.fn(),
    setRequirements: vi.fn(),
    setResultRetention: vi.fn(),
    setParameters: vi.fn(),
  },
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, getShares: mocks.getShares, addShare: mocks.addShare,
    tablesPage: mocks.tablesPage, tableByRegistration: mocks.tableByRegistration,
    datasetRevisionCapabilities: mocks.datasetRevisionCapabilities, resolveDatasetRevision: mocks.resolveDatasetRevision,
    datasetRevisions: mocks.datasetRevisions, datasetRevision: mocks.datasetRevision } }
})

vi.mock('../store/graph', () => ({
  roleCanEdit: (role: string | null) => role === 'owner' || role === 'editor',
  useStore: (selector: (value: typeof mocks.state) => unknown) => selector(mocks.state),
}))

import { CanvasSettingsModal } from './CanvasSettingsModal'

describe('CanvasSettingsModal — sharing and read-only truth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state.canvasRole = 'owner'
    mocks.state.authEnabled = true
    mocks.getShares.mockResolvedValue({ visibility: 'private', shares: [] })
    mocks.addShare.mockResolvedValue({ ok: true })
    mocks.state.doc.parameters = []
    mocks.state.doc.resultRetention = { history: 'inherit' }
    const table = { id: 'catalog-orders', registrationId: 'registration-orders', name: 'Orders', uri: 'managed://orders', columns: [] }
    mocks.tablesPage.mockResolvedValue({ items: [table], hasMore: false })
    mocks.tableByRegistration.mockResolvedValue(table)
    mocks.datasetRevisionCapabilities.mockResolvedValue({ selectors: ['latest', 'exact'] })
    mocks.resolveDatasetRevision.mockResolvedValue({ datasetId: 'logical-orders', revisionId: 'rev-new', retentionOwner: 'core' })
    mocks.datasetRevisions.mockResolvedValue({ items: [
      { datasetId: 'logical-orders', revisionId: 'rev-old', committedAt: '2026-09-24T12:00:00Z', retentionOwner: 'core' },
    ], hasMore: false })
    mocks.datasetRevision.mockImplementation(async (datasetId, revisionId) => ({ datasetId, revisionId, name: 'Orders', summary: {} }))
  })

  it('renders workspace_view accurately and disables document fields for a viewer', async () => {
    mocks.state.canvasRole = 'viewer'
    mocks.getShares.mockResolvedValue({ visibility: 'workspace_view', shares: [] })
    render(<CanvasSettingsModal onClose={vi.fn()} />)

    expect(await screen.findByText('View-only access')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Revenue canvas')).toBeDisabled()
    expect(screen.getByDisplayValue('pandas')).toBeDisabled()
    const viewOnly = screen.getByRole('button', { name: /Workspace view-only/i })
    expect(viewOnly).toHaveAttribute('aria-pressed', 'true')
    expect(viewOnly).toBeDisabled()

    expect(mocks.state.renameFile).not.toHaveBeenCalled()
    expect(mocks.addShare).not.toHaveBeenCalled()
  })

  it('keeps the prior visibility on an offline failure and exposes Retry', async () => {
    mocks.addShare.mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce({ ok: true })
    render(<CanvasSettingsModal onClose={vi.fn()} />)
    const workspace = await screen.findByRole('button', { name: /^Workspace Everyone/i })
    const privateButton = screen.getByRole('button', { name: /^Private Only/i })

    fireEvent.click(workspace)
    expect(await screen.findByRole('alert')).toHaveTextContent('offline')
    expect(privateButton).toHaveAttribute('aria-pressed', 'true')
    expect(workspace).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(workspace).toHaveAttribute('aria-pressed', 'true'))
    expect(mocks.addShare).toHaveBeenNthCalledWith(2, 'canvas-1', { visibility: 'workspace' })
  })

  it('keeps invalid declaration edits local and validates dates and SecretRefs strictly', async () => {
    render(<CanvasSettingsModal onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add parameter' }))
    expect(mocks.state.setParameters).toHaveBeenCalledTimes(1)

    const name = screen.getByLabelText('Parameter name')
    fireEvent.change(name, { target: { value: '1bad' } })
    expect(screen.getByRole('alert')).toHaveTextContent('Names start with a letter')
    expect(mocks.state.setParameters).toHaveBeenCalledTimes(1)

    fireEvent.change(name, { target: { value: 'public_value' } })
    fireEvent.click(screen.getByLabelText('public_value type'))
    fireEvent.change(screen.getByLabelText('public_value type'), { target: { value: 'date' } })
    fireEvent.click(screen.getByLabelText('Required'))
    fireEvent.click(screen.getByLabelText('Default'))
    fireEvent.change(screen.getByLabelText('public_value default'), { target: { value: '2026-02-30' } })
    expect(screen.getByRole('alert')).toHaveTextContent('real YYYY-MM-DD')

    fireEvent.change(screen.getByLabelText('public_value type'), { target: { value: 'string' } })
    fireEvent.click(screen.getByLabelText('Required'))
    fireEvent.click(screen.getByLabelText('Default'))
    fireEvent.change(screen.getByLabelText('public_value default'), { target: { value: 'env:PRIVATE' } })
    expect(screen.getByRole('alert')).toHaveTextContent('must be plain text, not a secret')
    fireEvent.change(screen.getByLabelText('public_value default'), { target: { value: 'FILE:/private/token' } })
    expect(screen.getByRole('alert')).toHaveTextContent('must be plain text, not a secret')

    fireEvent.change(screen.getByLabelText('public_value default'), { target: { value: 's3://public-bucket/key' } })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('saves a dataset default by name and pins a version without storing registration or catalog IDs', async () => {
    mocks.state.doc.parameters = [{ name: 'input', type: 'dataset', required: false }]
    render(<CanvasSettingsModal onClose={vi.fn()} />)
    fireEvent.click(screen.getByLabelText('Default'))
    expect(screen.getByRole('alert')).toHaveTextContent('dataset default is incomplete')
    expect(mocks.state.setParameters).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: 'Choose dataset Orders' }))
    await waitFor(() => expect(mocks.state.setParameters).toHaveBeenCalledWith([
      { name: 'input', type: 'dataset', required: false, default: { kind: 'latest', datasetId: 'logical-orders' } },
    ]))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('input default selection')).toBeEnabled())
    fireEvent.change(screen.getByLabelText('input default selection'), { target: { value: 'exact' } })
    fireEvent.change(screen.getByLabelText('input default version'), { target: { value: 'rev-old' } })
    expect(mocks.state.setParameters).toHaveBeenLastCalledWith([
      { name: 'input', type: 'dataset', required: false, default: { kind: 'exact', datasetId: 'logical-orders', revisionId: 'rev-old' } },
    ])
  })

  it('keeps dataset defaults read-only for viewers', async () => {
    mocks.state.canvasRole = 'viewer'
    mocks.state.doc.parameters = [{ name: 'input', type: 'dataset', default: { kind: 'latest', datasetId: 'logical-orders' } }]
    render(<CanvasSettingsModal onClose={vi.fn()} />)
    expect(await screen.findByText('Orders')).toBeVisible()
    expect(screen.getByLabelText('input default dataset search')).toBeDisabled()
    expect(screen.getByLabelText('input default selection')).toBeDisabled()
    expect(mocks.state.setParameters).not.toHaveBeenCalled()
  })

  it('does not assign a late dataset choice to another parameter after reordering identical defaults', async () => {
    mocks.state.doc.parameters = ['alpha', 'beta'].map((name) => ({ name, type: 'dataset', default: { kind: 'latest', datasetId: 'logical-orders' } }))
    render(<CanvasSettingsModal onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getByLabelText('alpha default selection')).toBeEnabled())
    await waitFor(() => expect(screen.getByLabelText('beta default selection')).toBeEnabled())
    let resolve!: (value: unknown) => void
    mocks.resolveDatasetRevision.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const alpha = within(screen.getByLabelText('alpha default dataset binding'))
    fireEvent.click(await alpha.findByRole('button', { name: 'Choose dataset Orders' }))
    await waitFor(() => expect(mocks.resolveDatasetRevision).toHaveBeenCalledTimes(3))
    fireEvent.click(screen.getByRole('button', { name: 'Move alpha down' }))
    await act(async () => resolve({ datasetId: 'other-logical-dataset', revisionId: 'rev-other', retentionOwner: 'core' }))
    expect(mocks.state.setParameters).toHaveBeenCalledTimes(1)
    expect(mocks.state.setParameters).toHaveBeenCalledWith([
      { name: 'beta', type: 'dataset', default: { kind: 'latest', datasetId: 'logical-orders' } },
      { name: 'alpha', type: 'dataset', default: { kind: 'latest', datasetId: 'logical-orders' } },
    ])
  })

  it('saves a Canvas history override without offering a per-Canvas location', async () => {
    render(<CanvasSettingsModal onClose={vi.fn()} />)

    expect(screen.queryByText('Local workspace')).toBeNull()
    fireEvent.change(screen.getByLabelText('Result history'), { target: { value: 'recent' } })
    expect(mocks.state.setResultRetention).toHaveBeenCalledWith({
      history: 'recent', maxVersions: 10, maxAgeDays: 30,
    })
  })

  it('edits bounded recent-result limits without changing the storage location', () => {
    mocks.state.doc.resultRetention = {
      history: 'recent', maxVersions: 4, maxAgeDays: 14,
    } as any
    render(<CanvasSettingsModal onClose={vi.fn()} />)

    expect(screen.getByLabelText('Versions per step')).toHaveValue(4)
    expect(screen.getByLabelText('Days to keep')).toHaveValue(14)
    fireEvent.change(screen.getByLabelText('Versions per step'), { target: { value: '6' } })
    expect(mocks.state.setResultRetention).toHaveBeenCalledWith({
      history: 'recent', maxVersions: 6, maxAgeDays: 14,
    })
    expect(screen.getByText('Latest result is always kept.')).toBeVisible()
  })

  it('hides unenforceable visibility controls when authentication is off', () => {
    mocks.state.authEnabled = false
    render(<CanvasSettingsModal onClose={vi.fn()} />)

    expect(screen.getByDisplayValue('Revenue canvas')).toBeVisible()
    expect(screen.queryByText('Visibility')).toBeNull()
  })
})
