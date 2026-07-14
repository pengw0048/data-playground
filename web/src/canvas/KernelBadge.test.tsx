import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  kernelState: vi.fn(),
  restartKernel: vi.fn(),
  pushToast: vi.fn(),
  state: {
    doc: { id: 'canvas-1' },
    canvasRole: 'owner' as 'owner' | 'editor' | 'viewer' | null,
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

describe('KernelBadge', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state.canvasRole = 'owner'
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
    await waitFor(() => expect(badge).toHaveTextContent('kernel · warm'))

    fireEvent.click(badge)
    expect(await screen.findByText('Execution kernel')).toBeInTheDocument()
    expect(screen.getByText(/3 cached/)).toBeInTheDocument()
    expect(screen.getByText(/2m 5s/)).toBeInTheDocument()  // 125s uptime
  })

  it('degrades to offline (keeping the badge) when the kernel-state fetch fails', async () => {
    mocks.kernelState.mockRejectedValue(new Error('network down'))
    render(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    const badge = await screen.findByTestId('kernel-badge')
    await waitFor(() => expect(badge).toHaveTextContent('kernel · offline'))
  })

  it('shows cold (not offline) for a genuinely absent lease', async () => {
    mocks.kernelState.mockResolvedValue({ exists: false })
    render(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    const badge = await screen.findByTestId('kernel-badge')
    await waitFor(() => expect(badge).toHaveTextContent('kernel · cold'))
  })

  it('calls restartKernel and refreshes when Restart is clicked', async () => {
    mocks.kernelState.mockResolvedValue({ exists: true, state: 'ready', stale: false })
    render(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    fireEvent.click(await screen.findByTestId('kernel-badge'))
    const restart = await screen.findByRole('button', { name: /Restart kernel/ })
    mocks.kernelState.mockClear()

    fireEvent.click(restart)

    await waitFor(() => expect(mocks.restartKernel).toHaveBeenCalledWith('canvas-1'))
    await waitFor(() => expect(mocks.kernelState).toHaveBeenCalled())  // refresh after restart
    expect(mocks.pushToast).toHaveBeenCalledWith(expect.stringContaining('restarting'), 'success')
  })

  it('disables Restart on a view-only canvas', async () => {
    mocks.state.canvasRole = 'viewer'
    mocks.kernelState.mockResolvedValue({ exists: true, state: 'ready', stale: false })
    render(<KernelBadge kernelUp kernelInfo={kernelInfo} />)
    fireEvent.click(await screen.findByTestId('kernel-badge'))
    expect(await screen.findByRole('button', { name: /Restart kernel/ })).toBeDisabled()
  })
})
