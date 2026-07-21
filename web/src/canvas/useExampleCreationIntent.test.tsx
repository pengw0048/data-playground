import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ listRuns: vi.fn() }))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, listRuns: mocks.listRuns } }
})

vi.mock('../store/graph', async () => {
  const { create } = await import('zustand')
  return {
    useStore: create(() => ({
      doc: { id: 'blank', version: 1, name: 'untitled', nodes: [], edges: [] },
      canvasRole: 'owner',
      currentDraftId: null,
      serverVersion: 1,
    })),
  }
})

import { useStore } from '../store/graph'
import { useExampleCreationIntent } from './useExampleCreationIntent'

describe('useExampleCreationIntent', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useStore.setState({
      doc: { id: 'blank', version: 1, name: 'untitled', nodes: [], edges: [] },
      canvasRole: 'owner', currentDraftId: null, serverVersion: 1,
    } as never)
  })

  it('describes a separate create while run history is still pending', () => {
    mocks.listRuns.mockReturnValue(new Promise(() => {}))

    const { result } = renderHook(() => useExampleCreationIntent())

    expect(result.current).toBe('create-separate')
  })

  it('keeps describing a separate create when run history fails', async () => {
    mocks.listRuns.mockRejectedValue(new TypeError('offline'))

    const { result } = renderHook(() => useExampleCreationIntent())
    await waitFor(() => expect(mocks.listRuns).toHaveBeenCalledOnce())
    await act(async () => { await Promise.resolve() })

    expect(result.current).toBe('create-separate')
  })

  it('describes an in-place replacement only after the exact pristine snapshot is confirmed', async () => {
    mocks.listRuns.mockResolvedValue([])

    const { result } = renderHook(() => useExampleCreationIntent())

    await waitFor(() => expect(result.current).toBe('replace-pristine'))
  })

  it('does not let late no-run evidence upgrade an edited Canvas', async () => {
    let finishRuns!: (runs: unknown[]) => void
    mocks.listRuns.mockReturnValue(new Promise((resolve) => { finishRuns = resolve }))
    const { result } = renderHook(() => useExampleCreationIntent())

    act(() => useStore.setState((state) => ({
      doc: { ...state.doc, requirements: ['duckdb>=1'] },
    })))
    finishRuns([])
    await act(async () => { await Promise.resolve() })

    expect(result.current).toBe('create-separate')
  })
})
