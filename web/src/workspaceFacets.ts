/**
 * Adaptive facet options for the categorical Workspace filters.
 *
 * Requests debounce and cancel, and a response commits only while it still matches the latest
 * requested state, so a slower response can never replace a newer filter state. Results cache
 * in memory keyed by actor, page revision, scope, and the normalized filter state.
 */

import { useEffect, useState } from 'react'
import { api } from './api/client'
import type { WorkspaceFacetPage } from './types/api'

export interface WorkspaceFacetQuery {
  field: 'kind' | 'source' | 'owner'
  containerId?: string
  q?: string
  kinds?: Array<'container' | 'canvas' | 'dataset' | 'dataset_view'>
  name?: string
  updatedAfter?: string
  updatedBefore?: string
  rowsMin?: number
  rowsMax?: number
  owner?: string
  sourceId?: string
}

export const FACET_DEBOUNCE_MS = 150
const FACET_CACHE_MAX_ENTRIES = 64

const facetCache = new Map<string, WorkspaceFacetPage>()

export function workspaceFacetKey(actorId: string, revision: number, query: WorkspaceFacetQuery): string {
  return JSON.stringify([
    actorId, revision, query.field, query.containerId ?? null, query.q ?? null,
    [...(query.kinds ?? [])].sort(), query.name ?? null, query.sourceId ?? null,
    query.updatedAfter ?? null, query.updatedBefore ?? null,
    query.rowsMin ?? null, query.rowsMax ?? null, query.owner ?? null,
  ])
}

export function clearWorkspaceFacetCache(): void {
  facetCache.clear()
}

/** Latest facet page for the given state, or null while unavailable, disabled, or in flight. */
export function useWorkspaceFacet(params: {
  enabled: boolean
  actorId: string
  revision: number
  query: WorkspaceFacetQuery
}): WorkspaceFacetPage | null {
  const { enabled, actorId, revision, query } = params
  const [page, setPage] = useState<WorkspaceFacetPage | null>(null)
  const key = enabled ? workspaceFacetKey(actorId, revision, query) : ''

  // The key serializes every request input, so it is the only effect dependency.
  useEffect(() => {
    if (!key) { setPage(null); return }
    const cached = facetCache.get(key)
    if (cached) { setPage(cached); return }
    setPage(null)
    const controller = new AbortController()
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const result = await api.workspaceFacets({ ...query, signal: controller.signal })
          if (controller.signal.aborted) return
          facetCache.set(key, result)
          while (facetCache.size > FACET_CACHE_MAX_ENTRIES) {
            facetCache.delete(facetCache.keys().next().value!)
          }
          setPage(result)
        } catch {
          // No adaptive counts on failure; plain typed filtering stays available.
        }
      })()
    }, FACET_DEBOUNCE_MS)
    return () => { clearTimeout(timer); controller.abort() }
  }, [key])
  return page
}
