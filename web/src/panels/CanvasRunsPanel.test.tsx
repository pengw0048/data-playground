import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CanvasDoc } from '../types/graph'
import type { RunRecordDto } from '../api/client'
import type { CanvasResultRecovery, RunOutput } from '../types/api'

const calls = vi.hoisted(() => ({ listRuns: vi.fn(), currentResults: vi.fn(), executionManifest: vi.fn() }))
vi.mock('../api/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api/client')>(), api: calls,
}))
vi.mock('../store/graph', async () => {
  const { create } = await import('zustand')
  type State = ReturnType<typeof import('../store/graph').useStore.getState>
  return {
    useStore: create<State>(() => ({} as State)),
    roleCanEdit: (role: string) => ['owner', 'editor'].includes(role),
  }
})
vi.mock('./RunHistoryModal', () => ({
  fmtMs: (ms: number) => `${ms} ms`,
  RunInputManifest: () => null,
  HistoryOutputs: ({ historyId, runId, outputs }: { historyId: string; runId?: string; outputs: RunOutput[] }) =>
    <div data-testid="exact-output">{JSON.stringify({ historyId, runId, outputs })}</div>,
}))
import { useStore } from '../store/graph'
import { CanvasRunsPanel } from './CanvasRunsPanel'

const output: RunOutput = { nodeId: 'filter', portId: 'out', wire: 'dataset', publicationKind: 'result', outcome: 'committed', uri: 'artifact://original', rows: 12 }
const success: RunRecordDto = { id: 'history-success', runId: 'run-success', jobType: 'run', status: 'done', targetNodeId: 'filter', outputs: [output], createdAt: '2026-09-25T12:00:00Z', rows: 12 }
const failure: RunRecordDto = { id: 'history-failure', runId: 'run-failure', jobType: 'run', status: 'failed', targetNodeId: 'filter', outputs: [], error: 'Unknown column missing', perNode: [{ nodeId: 'filter', label: 'Purchases', status: 'failed', error: 'Unknown column missing' }], createdAt: '2026-09-25T12:01:00Z' }
const emptyProof = (): CanvasResultRecovery => ({ latestNodeIds: [], staleNodeIds: [], failedNodeIds: [], unknownNodeIds: [], results: [] })
const doc = (): CanvasDoc => ({ id: 'canvas-1', name: 'Purchases', version: 1, nodes: [{ id: 'filter', type: 'filter', position: { x: 0, y: 0 }, data: { title: 'Purchases', config: { predicate: 'amount > 10' } } }], edges: [] })

beforeEach(() => {
  vi.clearAllMocks()
  useStore.setState({ doc: doc(), currentUser: { id: 'alice', name: 'Alice' } as ReturnType<typeof useStore.getState>['currentUser'], runs: {}, graphRun: null, detachedRuns: {}, executionRecovery: null, kernelUp: true, canvasRole: 'owner', select: vi.fn(), requestNodeReveal: vi.fn(), openPanel: vi.fn(), cancelRun: vi.fn().mockResolvedValue(undefined) })
  calls.listRuns.mockResolvedValue([failure, success])
  calls.currentResults.mockResolvedValue(emptyProof())
  calls.executionManifest.mockResolvedValue({ availability: 'available', document: {
    parameters: [{ name: 'threshold', value: 10 }],
    graph: { nodes: [{ id: 'filter', type: 'filter', data: { config: { predicate: 'amount > 1' } } }] },
  } })
})

const show = () => render(<CanvasRunsPanel onClose={vi.fn()} onHistory={vi.fn()} />)

describe('Canvas runs and results', () => {
  it('keeps the last successful exact run reachable after a failure and selects the failed node', async () => {
    const user = userEvent.setup()
    show()
    expect(await screen.findByText('Run failed')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Go to failed node' }))
    expect(useStore.getState().requestNodeReveal).toHaveBeenCalledWith('canvas-1', 'filter')
    await user.click(screen.getByRole('button', { name: 'View last successful result' }))
    expect(screen.getByText('Run completed')).toBeInTheDocument()
    const saved = JSON.parse(screen.getByTestId('exact-output').textContent!)
    expect(saved).toEqual({ historyId: 'history-success', runId: 'run-success', outputs: [output] })
    expect(await screen.findByText(/Saved result — not verified/)).toBeInTheDocument()
  })

  it('uses the server result identity and saved parameters instead of current parameter drafts', async () => {
    calls.listRuns.mockResolvedValue([success])
    calls.currentResults.mockResolvedValue({ ...emptyProof(), latestNodeIds: ['filter'], results: [{ runId: 'run-success', executionManifestSha256: 'recorded-identity', parameterBindings: [], output }] })
    useStore.setState({ runs: { filter: { phase: 'idle', parameterBindings: [{ name: 'threshold', value: 999 }] } } })
    const user = userEvent.setup()
    show()
    expect(await screen.findByText(/match the current Canvas plan using this run’s recorded/)).toBeInTheDocument()
    await user.click(screen.getByText('Settings used for this run'))
    expect(await screen.findByText('10', { selector: 'code' })).toBeInTheDocument()
    expect(screen.queryByText('999')).not.toBeInTheDocument()
    expect(calls.executionManifest).toHaveBeenCalledWith('canvas-1', 'history-success')
    await user.click(screen.getByText('filter', { selector: 'summary' }))
    expect(screen.getByText('amount > 1', { selector: 'dd' })).toBeInTheDocument()
    expect(screen.queryByText('amount > 10', { selector: 'dd' })).not.toBeInTheDocument()
  })

  it('drops current-plan claims immediately after an edit and ignores the old in-flight proof', async () => {
    calls.listRuns.mockResolvedValue([success])
    let resolveOld: (value: CanvasResultRecovery) => void = () => {}
    calls.currentResults.mockImplementationOnce(() => new Promise<CanvasResultRecovery>((resolve) => { resolveOld = resolve }))
    show()
    await waitFor(() => expect(calls.currentResults).toHaveBeenCalledTimes(1))
    act(() => useStore.setState({ doc: { ...doc(), nodes: [{ ...doc().nodes[0], data: { ...doc().nodes[0].data, config: { predicate: 'amount > 100' } } }] } }))
    await act(async () => resolveOld({ ...emptyProof(), latestNodeIds: ['filter'], results: [{ runId: 'run-success', executionManifestSha256: 'old', parameterBindings: [], output }] }))
    expect(screen.queryByText(/match the current Canvas plan using/)).not.toBeInTheDocument()
    expect(await screen.findByText(/Saved result — not verified/)).toBeInTheDocument()
  })

  it('does not reveal another Canvas history from a late response', async () => {
    let resolveOld: (runs: RunRecordDto[]) => void = () => {}
    calls.listRuns.mockImplementationOnce(() => new Promise<RunRecordDto[]>((resolve) => { resolveOld = resolve }))
    show()
    await waitFor(() => expect(calls.listRuns).toHaveBeenCalledWith('canvas-1'))
    calls.listRuns.mockResolvedValue([])
    act(() => useStore.setState({ doc: { ...doc(), id: 'canvas-2' } }))
    expect(await screen.findByText(/No saved runs yet/)).toBeInTheDocument()
    await act(async () => resolveOld([success]))
    expect(screen.queryByTestId('exact-output')).not.toBeInTheDocument()
  })

  it('keeps saved results accessible when current verification fails', async () => {
    calls.listRuns.mockResolvedValue([success])
    calls.currentResults.mockRejectedValue(new Error('Provider offline'))
    show()
    expect(await screen.findByText(/Current result status could not be verified/)).toBeInTheDocument()
    expect(screen.getByTestId('exact-output')).toHaveTextContent('run-success')
    expect(screen.queryByText(/match the current Canvas plan using/)).not.toBeInTheDocument()
  })

  it.each(['parameters', 'confirm', 'drift'] as const)('continues a pending %s action in the run panel', async (phase) => {
    useStore.setState({ runs: { filter: { phase } } })
    const user = userEvent.setup()
    show()
    await user.click(screen.getByRole('button', { name: 'Continue at node' }))
    expect(useStore.getState().openPanel).toHaveBeenCalledWith('filter', 'run')
  })

  it('makes the batch cancellation scope explicit for off-canvas runs', async () => {
    const status = { runId: 'detached', status: 'running' as const, jobType: 'run' as const, targetNodeId: 'removed', rowsProcessed: 0, ms: 100, placement: 'local' as const, outputs: [], perNode: [] }
    const cancelDetachedRuns = vi.fn().mockResolvedValue(undefined)
    useStore.setState({ detachedRuns: { detached: { canvasId: 'canvas-1', principalId: 'alice', status } }, cancelDetachedRuns })
    const user = userEvent.setup()
    show()
    expect(screen.queryByRole('button', { name: 'Stop run', exact: true })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Stop all off-canvas runs' }))
    expect(cancelDetachedRuns).toHaveBeenCalledOnce()
  })

  it('shows live steps and uses the existing cancel owner while preserving view-only controls', async () => {
    const state = { phase: 'running' as const, status: { runId: 'live', status: 'running' as const, jobType: 'run' as const, targetNodeId: 'filter', rowsProcessed: 20, ms: 100, placement: 'local' as const, outputs: [], perNode: [{ nodeId: 'filter', label: 'Purchases', status: 'running' }] } }
    useStore.setState({ runs: { filter: state } })
    const user = userEvent.setup()
    show()
    expect(screen.getByText(/20 rows processed/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Stop run' }))
    expect(useStore.getState().cancelRun).toHaveBeenCalledWith('filter')
    expect(screen.getByText(/Purchases · running/, { selector: 'strong' })).toBeInTheDocument()
    act(() => useStore.setState({ canvasRole: 'viewer' }))
    expect(screen.getByRole('button', { name: 'Stop run' })).toBeDisabled()
  })
})
