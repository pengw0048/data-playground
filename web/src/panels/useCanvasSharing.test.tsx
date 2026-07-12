import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  getShares: vi.fn(),
  addShare: vi.fn(),
  removeShare: vi.fn(),
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getShares: mocks.getShares,
      addShare: mocks.addShare,
      removeShare: mocks.removeShare,
    },
  }
})

import { useCanvasSharing } from './useCanvasSharing'

const sharesFor = (canvasId: string) => ({
  visibility: canvasId === 'a' ? 'private' : 'workspace_view',
  shares: [],
})

describe('useCanvasSharing — canvas generation isolation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getShares.mockImplementation(async (canvasId: string) => sharesFor(canvasId))
    mocks.addShare.mockResolvedValue({ ok: true })
    mocks.removeShare.mockResolvedValue({ ok: true })
  })

  it('ignores a late mutation success from canvas A after switching to B', async () => {
    let finishA!: (value: { ok: boolean }) => void
    mocks.addShare.mockImplementation((canvasId: string) => canvasId === 'a'
      ? new Promise((resolve) => { finishA = resolve })
      : Promise.resolve({ ok: true }))
    const { result, rerender } = renderHook(
      ({ canvasId }) => useCanvasSharing(canvasId, true),
      { initialProps: { canvasId: 'a' } },
    )
    await waitFor(() => expect(result.current.visibility).toBe('private'))

    let mutation!: Promise<void>
    act(() => { mutation = result.current.setCanvasVisibility('workspace') })
    await waitFor(() => expect(result.current.pending).toBe('visibility'))
    rerender({ canvasId: 'b' })
    await waitFor(() => expect(result.current.visibility).toBe('workspace_view'))

    await act(async () => { finishA({ ok: true }); await mutation })

    expect(result.current.visibility).toBe('workspace_view')
    expect(result.current.error).toBeNull()
    expect(result.current.pending).toBeNull()
  })

  it('ignores a late mutation failure from canvas A after switching to B', async () => {
    let failA!: (error: Error) => void
    mocks.addShare.mockImplementation((canvasId: string) => canvasId === 'a'
      ? new Promise((_resolve, reject) => { failA = reject })
      : Promise.resolve({ ok: true }))
    const { result, rerender } = renderHook(
      ({ canvasId }) => useCanvasSharing(canvasId, true),
      { initialProps: { canvasId: 'a' } },
    )
    await waitFor(() => expect(result.current.visibility).toBe('private'))

    let mutation!: Promise<void>
    act(() => { mutation = result.current.setCanvasVisibility('workspace') })
    await waitFor(() => expect(result.current.pending).toBe('visibility'))
    rerender({ canvasId: 'b' })
    await waitFor(() => expect(result.current.visibility).toBe('workspace_view'))

    await act(async () => { failA(Object.assign(new Error('late failure'), { status: 500 })); await mutation })

    expect(result.current.visibility).toBe('workspace_view')
    expect(result.current.error).toBeNull()
    expect(result.current.retryable).toBe(false)
  })

  it('does not let a Retry handler retained from A execute after switching to B', async () => {
    mocks.addShare.mockRejectedValueOnce(Object.assign(new Error('A failed'), { status: 500 }))
    const { result, rerender } = renderHook(
      ({ canvasId }) => useCanvasSharing(canvasId, true),
      { initialProps: { canvasId: 'a' } },
    )
    await waitFor(() => expect(result.current.visibility).toBe('private'))
    await act(async () => { await result.current.setCanvasVisibility('workspace') })
    await waitFor(() => expect(result.current.retryable).toBe(true))
    const retryFromA = result.current.retry

    rerender({ canvasId: 'b' })
    await waitFor(() => expect(result.current.visibility).toBe('workspace_view'))
    act(() => retryFromA())

    expect(mocks.addShare).toHaveBeenCalledTimes(1)
    expect(mocks.addShare).toHaveBeenCalledWith('a', { visibility: 'workspace' })
    expect(result.current.error).toBeNull()
  })
})
