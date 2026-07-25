import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  livez: vi.fn(),
}))

vi.mock('./api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api/client')>()
  return { ...actual, api: { ...actual.api, livez: mocks.livez } }
})

import { HubLiveness, HUB_LIVENESS_INTERVAL_MS, HUB_LIVENESS_TIMEOUT_MS } from './HubLiveness'
import { useStore } from './store/graph'

describe('HubLiveness', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mocks.livez.mockReset()
    useStore.setState({
      kernelUp: true,
      saved: true,
      doc: {
        id: 'liveness-canvas', name: 'Liveness canvas', version: 3, requirements: [],
        nodes: [], edges: [],
      },
      localDrafts: [{
        draftId: 'liveness-canvas', canvasId: 'liveness-canvas', principalId: 'alice',
        name: 'Liveness canvas', doc: {
          id: 'liveness-canvas', name: 'Liveness canvas', version: 3, requirements: [],
          nodes: [], edges: [],
        },
        baseCanvasId: 'liveness-canvas', baseVersion: 3, syncState: 'dirty',
        lastLocalEditAt: '2026-07-25T00:00:00Z',
      }],
    } as never)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('marks a missed hub offline within 4.5s and restores reachability without touching local state', async () => {
    mocks.livez
      .mockResolvedValueOnce({ ok: true })
      .mockImplementationOnce(({ signal }: { signal?: AbortSignal }) => new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(new DOMException('timed out', 'AbortError')))
      }))
      .mockResolvedValueOnce({ ok: true })

    const { unmount } = render(<HubLiveness />)
    await act(async () => { await Promise.resolve() })
    const originalDoc = useStore.getState().doc
    const originalDrafts = useStore.getState().localDrafts

    await act(async () => { await vi.advanceTimersByTimeAsync(HUB_LIVENESS_INTERVAL_MS) })
    await act(async () => { await vi.advanceTimersByTimeAsync(HUB_LIVENESS_TIMEOUT_MS - 1) })
    expect(useStore.getState().kernelUp).toBe(true)

    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(useStore.getState().kernelUp).toBe(false)
    expect(useStore.getState().saved).toBe(true)
    expect(useStore.getState().doc).toBe(originalDoc)
    expect(useStore.getState().localDrafts).toBe(originalDrafts)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(HUB_LIVENESS_INTERVAL_MS - HUB_LIVENESS_TIMEOUT_MS)
    })
    expect(useStore.getState().kernelUp).toBe(true)
    expect(mocks.livez).toHaveBeenCalledTimes(3)
    expect(useStore.getState().doc).toBe(originalDoc)
    expect(useStore.getState().localDrafts).toBe(originalDrafts)
    unmount()
  })
})
