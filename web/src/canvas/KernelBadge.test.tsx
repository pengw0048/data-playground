import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PreviewState } from '../store/graph'
import type { SampleResult } from '../types/api'

const mocks = vi.hoisted(() => ({
  kernelState: vi.fn(),
  restartKernel: vi.fn(),
  pushToast: vi.fn(),
  state: {
    doc: { id: 'canvas-1' },
    canvasRole: 'owner' as 'owner' | 'editor' | 'viewer' | null,
    runs: {} as Record<string, { phase: string }>,
    previews: {} as Record<string, PreviewState>,
  },
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, kernelState: mocks.kernelState, restartKernel: mocks.restartKernel } }
})

vi.mock('../store/graph', () => ({
  roleCanEdit: (r: string | null) => r === 'owner' || r === 'editor',
  useStore: (selector: (v: typeof mocks.state & { pushToast: typeof mocks.pushToast }) => unknown) =>
    selector({ ...mocks.state, pushToast: mocks.pushToast }),
}))

import { KernelBadge } from './KernelBadge'
import type { KernelInfo } from '../types/api'

const kernelInfo = { backend: 'kernel', runners: ['local-out-of-core'] } as unknown as KernelInfo
const previewResult = (patch: Partial<SampleResult> = {}): SampleResult => ({
  columns: [],
  rows: [],
  truncated: false,
  completeness: 'complete',
  notPreviewable: false,
  ...patch,
})
const previewState = (requestGeneration: number, patch: Partial<PreviewState> = {}): PreviewState => ({
  canvasId: 'canvas-1',
  nodeId: 'source',
  planIdentity: 'plan-1',
  requestGeneration,
  ...patch,
})

describe('KernelBadge', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state.canvasRole = 'owner'
    mocks.state.doc.id = 'canvas-1'
    mocks.state.previews = {}
    mocks.restartKernel.mockResolvedValue({ ok: true, restarted: true })
  })

  it('shows a warm badge with cache + uptime from a live kernel status', async () => {
    mocks.kernelState.mockResolvedValue({
      exists: true, state: 'ready', stale: false,
      relationCache: { entries: 3, bytes: 2048, maxEntries: 64, maxBytes: 268435456, tooBig: 0 },
      uptimeSeconds: 125, memoryLimit: '4GB', inflight: 0, activeRuns: 0,
    })
    render(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    const badge = await screen.findByTestId('kernel-badge')
    fireEvent.click(badge)  // opening the popover triggers the status fetch (no request while closed)
    expect(await screen.findByText('Canvas worker')).toBeInTheDocument()
    expect(await screen.findByText(/3 cached/)).toBeInTheDocument()
    expect(screen.getByText(/2m 5s/)).toBeInTheDocument()  // 125s uptime
    await waitFor(() => expect(badge).toHaveTextContent('worker · warm'))
  })

  it('degrades to offline (keeping the badge) when the kernel-state fetch fails', async () => {
    mocks.kernelState.mockRejectedValue(new Error('network down'))
    render(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    const badge = await screen.findByTestId('kernel-badge')
    fireEvent.click(badge)  // open → the fetch fails → the dot degrades to offline
    await waitFor(() => expect(badge).toHaveTextContent('worker · offline'))
  })

  it('shows cold (not offline) for a genuinely absent lease', async () => {
    mocks.kernelState.mockResolvedValue({ exists: false })
    render(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    const badge = await screen.findByTestId('kernel-badge')
    fireEvent.click(badge)  // open → fetch returns no lease → cold
    await waitFor(() => expect(badge).toHaveTextContent('worker · cold'))
  })

  it('calls restartKernel and refreshes when Restart is clicked', async () => {
    mocks.kernelState.mockResolvedValue({ exists: true, state: 'ready', stale: false })
    render(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    fireEvent.click(await screen.findByTestId('kernel-badge'))
    const restart = await screen.findByRole('button', { name: /Restart worker/ })
    mocks.kernelState.mockClear()

    fireEvent.click(restart)

    await waitFor(() => expect(mocks.restartKernel).toHaveBeenCalledWith('canvas-1'))
    await waitFor(() => expect(mocks.kernelState).toHaveBeenCalled())  // refresh after restart
    expect(mocks.pushToast).toHaveBeenCalledWith(expect.stringContaining('restarting'), 'success')
  })

  it('refreshes authoritative state only for newly settled successful preview generations', async () => {
    mocks.kernelState.mockResolvedValue({ exists: false })
    const { rerender } = render(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    const badge = await screen.findByTestId('kernel-badge')
    await waitFor(() => expect(badge).toHaveTextContent('worker · cold'))
    mocks.kernelState.mockClear()

    const succeeded = previewState(1, { result: previewResult() })
    mocks.kernelState.mockResolvedValue({ exists: true, state: 'ready', stale: false })
    mocks.state.previews = { source: succeeded }
    rerender(<KernelBadge kernelUp kernelInfo={kernelInfo} />)

    await waitFor(() => expect(mocks.kernelState).toHaveBeenCalledWith('canvas-1'))
    await waitFor(() => expect(badge).toHaveTextContent('worker · warm'))
    mocks.kernelState.mockClear()

    // A normal retry removes the prior result while loading, then records a top-level failure.
    mocks.state.previews = { source: previewState(2, { loading: true }) }
    rerender(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    expect(mocks.kernelState).not.toHaveBeenCalled()
    mocks.state.previews = { source: previewState(2, { error: 'preview failed' }) }
    rerender(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    expect(mocks.kernelState).not.toHaveBeenCalled()

    // refreshLatest retains old rows, but loading and its eventual top-level error are not successes.
    mocks.state.previews = {
      source: { ...succeeded, requestGeneration: 3, loading: true, error: undefined },
    }
    rerender(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    expect(mocks.kernelState).not.toHaveBeenCalled()
    mocks.state.previews = {
      source: { ...succeeded, requestGeneration: 3, loading: false, error: 'refresh failed' },
    }
    rerender(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    expect(mocks.kernelState).not.toHaveBeenCalled()

    mocks.state.previews = {
      source: previewState(4, { result: previewResult({ error: true }) }),
    }
    rerender(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    expect(mocks.kernelState).not.toHaveBeenCalled()
    mocks.state.previews = {
      source: previewState(5, { result: previewResult({ notPreviewable: true }) }),
    }
    rerender(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    expect(mocks.kernelState).not.toHaveBeenCalled()
    expect(badge).toHaveTextContent('worker · warm')

    mocks.kernelState.mockResolvedValue({ exists: false })
    mocks.state.previews = {
      source: previewState(6, { result: previewResult() }),
    }
    rerender(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    await waitFor(() => expect(mocks.kernelState).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(badge).toHaveTextContent('worker · cold'))
  })

  it('disables Restart on a view-only canvas', async () => {
    mocks.state.canvasRole = 'viewer'
    mocks.kernelState.mockResolvedValue({ exists: true, state: 'ready', stale: false })
    render(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    fireEvent.click(await screen.findByTestId('kernel-badge'))
    expect(await screen.findByRole('button', { name: /Restart worker/ })).toBeDisabled()
  })

  it('resets to the new canvas on switch (never shows the previous canvas state)', async () => {
    // #161 review #7: switching from a warm canvas to a cold one must not keep the warm badge.
    mocks.state.doc.id = 'canvas-A'
    mocks.kernelState.mockResolvedValue({ exists: true, state: 'ready', stale: false })
    const { rerender } = render(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    const badge = await screen.findByTestId('kernel-badge')
    await waitFor(() => expect(badge).toHaveTextContent('worker · warm'))

    mocks.kernelState.mockResolvedValue({ exists: false })  // canvas B has no live kernel
    mocks.kernelState.mockClear()
    mocks.state.doc.id = 'canvas-B'
    mocks.state.previews = {
      source: previewState(9, { canvasId: 'canvas-B', result: previewResult() }),
    }
    rerender(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    await waitFor(() => expect(badge).toHaveTextContent('worker · cold'))
    expect(mocks.kernelState).toHaveBeenCalledTimes(1)  // canvas refresh only; existing success is the new baseline
  })
})
