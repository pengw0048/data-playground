import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { WorkspaceFacetPage } from './types/api'

const mocks = vi.hoisted(() => ({ workspaceFacets: vi.fn() }))

vi.mock('./api/client', () => ({ api: { workspaceFacets: mocks.workspaceFacets } }))

import { clearWorkspaceFacetCache, useWorkspaceFacet, type WorkspaceFacetQuery } from './workspaceFacets'

const page = (count: number): WorkspaceFacetPage => ({
  field: 'kind', options: [{ value: 'canvas', label: 'Canvases', count }],
  nextCursor: null, hasMore: false, completeness: 'complete', reason: null,
})

const query = (overrides: Partial<WorkspaceFacetQuery> = {}): WorkspaceFacetQuery => ({
  field: 'kind', containerId: 'workspace-local-root', ...overrides,
})

describe('useWorkspaceFacet', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    clearWorkspaceFacetCache()
  })

  it('stays null while disabled and never fetches', async () => {
    const { result } = renderHook(() => useWorkspaceFacet({
      enabled: false, actorId: 'alice', revision: 0, query: query(),
    }))
    await act(() => new Promise((resolve) => setTimeout(resolve, 250)))
    expect(result.current).toBeNull()
    expect(mocks.workspaceFacets).not.toHaveBeenCalled()
  })

  it('fetches once per state and reuses the cache for an identical state', async () => {
    mocks.workspaceFacets.mockResolvedValue(page(3))
    const first = renderHook(() => useWorkspaceFacet({
      enabled: true, actorId: 'alice', revision: 0, query: query(),
    }))
    expect(first.result.current).toBeNull()
    await waitFor(() => expect(first.result.current?.options[0]?.count).toBe(3))
    first.unmount()

    const second = renderHook(() => useWorkspaceFacet({
      enabled: true, actorId: 'alice', revision: 0, query: query(),
    }))
    await waitFor(() => expect(second.result.current?.options[0]?.count).toBe(3))
    expect(mocks.workspaceFacets).toHaveBeenCalledTimes(1)
  })

  it('never lets a slower response replace a newer filter state or poison its cache', async () => {
    let finishSlow!: (value: WorkspaceFacetPage) => void
    mocks.workspaceFacets.mockImplementation((params: { name?: string }) => params.name === 'slow'
      ? new Promise<WorkspaceFacetPage>((resolve) => { finishSlow = resolve })
      : Promise.resolve(page(1)))
    const { result, rerender } = renderHook(
      ({ name }: { name: string }) => useWorkspaceFacet({
        enabled: true, actorId: 'alice', revision: 0, query: query({ name }),
      }),
      { initialProps: { name: 'slow' } },
    )
    await waitFor(() => expect(mocks.workspaceFacets).toHaveBeenCalledTimes(1))
    rerender({ name: 'fast' })
    await waitFor(() => expect(result.current?.options[0]?.count).toBe(1))

    await act(async () => { finishSlow(page(9)) })
    expect(result.current?.options[0]?.count).toBe(1)

    rerender({ name: 'slow' })
    await waitFor(() => expect(mocks.workspaceFacets).toHaveBeenCalledTimes(3))
  })

  it('isolates cached options by actor', async () => {
    mocks.workspaceFacets.mockResolvedValueOnce(page(3)).mockResolvedValueOnce(page(7))
    const { result, rerender } = renderHook(
      ({ actorId }: { actorId: string }) => useWorkspaceFacet({
        enabled: true, actorId, revision: 0, query: query(),
      }),
      { initialProps: { actorId: 'alice' } },
    )
    await waitFor(() => expect(result.current?.options[0]?.count).toBe(3))
    rerender({ actorId: 'bob' })
    await waitFor(() => expect(result.current?.options[0]?.count).toBe(7))
    rerender({ actorId: 'alice' })
    await waitFor(() => expect(result.current?.options[0]?.count).toBe(3))
    expect(mocks.workspaceFacets).toHaveBeenCalledTimes(2)
  })

  it('refetches when the page revision changes', async () => {
    mocks.workspaceFacets.mockResolvedValueOnce(page(2)).mockResolvedValueOnce(page(5))
    const { result, rerender } = renderHook(
      ({ revision }: { revision: number }) => useWorkspaceFacet({
        enabled: true, actorId: 'alice', revision, query: query(),
      }),
      { initialProps: { revision: 0 } },
    )
    await waitFor(() => expect(result.current?.options[0]?.count).toBe(2))
    rerender({ revision: 1 })
    await waitFor(() => expect(result.current?.options[0]?.count).toBe(5))
    expect(mocks.workspaceFacets).toHaveBeenCalledTimes(2)
  })

  it('leaves plain filtering available when the facet request fails', async () => {
    mocks.workspaceFacets.mockRejectedValue(new Error('offline'))
    const { result } = renderHook(() => useWorkspaceFacet({
      enabled: true, actorId: 'alice', revision: 0, query: query(),
    }))
    await waitFor(() => expect(mocks.workspaceFacets).toHaveBeenCalledTimes(1))
    await act(() => Promise.resolve())
    expect(result.current).toBeNull()
  })
})
