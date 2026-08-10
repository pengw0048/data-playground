/**
 * Bounded, versioned Workspace browse projection for the shareable URL.
 *
 * Container identity and committed search (`q`) stay on the route path/query separately.
 * This module owns sort, kind filter, list/grid mode, and the typed column-filter map.
 * Opaque pagination cursors, search drafts, multi-selection, and loading are never encoded.
 */

export const WORKSPACE_BROWSE_QUERY_VERSION = 1

/** URL keys owned by the browse projection (scope=all only). */
export const WORKSPACE_BROWSE_QUERY_KEYS = [
  'wq', 'sort', 'order', 'kind', 'view', 'cf',
] as const

export type WorkspaceBrowseSortField = 'name' | 'updated' | 'opened'
export type WorkspaceBrowseSortOrder = 'asc' | 'desc'
export type WorkspaceBrowseKind = 'container' | 'canvas' | 'dataset' | 'dataset_view'
export type WorkspaceBrowseViewMode = 'list' | 'grid'

/** UI sort mode including the source-default sentinel (omitted from the URL). */
export type WorkspaceBrowseSortMode =
  | 'source'
  | 'name-asc'
  | 'name-desc'
  | 'updated-desc'
  | 'updated-asc'
  | 'opened-desc'

/** Committed typed column filters. Dates are day-granular `YYYY-MM-DD` values. */
export interface WorkspaceColumnFilters {
  name?: string
  source?: string
  updatedAfter?: string
  updatedBefore?: string
  rowsMin?: number
  rowsMax?: number
  owner?: string
}

export interface WorkspaceBrowseQuery {
  version: typeof WORKSPACE_BROWSE_QUERY_VERSION
  sort?: WorkspaceBrowseSortField
  order?: WorkspaceBrowseSortOrder
  kind?: WorkspaceBrowseKind
  view?: WorkspaceBrowseViewMode
  columnFilters?: WorkspaceColumnFilters
}

const SORT_FIELDS = new Set<WorkspaceBrowseSortField>(['name', 'updated', 'opened'])
const SORT_ORDERS = new Set<WorkspaceBrowseSortOrder>(['asc', 'desc'])
const KINDS = new Set<WorkspaceBrowseKind>(['container', 'canvas', 'dataset', 'dataset_view'])
const VIEWS = new Set<WorkspaceBrowseViewMode>(['list', 'grid'])

const MAX_COLUMN_FILTER_VALUE_CHARS = 128
const MAX_BROWSE_QUERY_CHARS = 1024

const COLUMN_FILTER_WIRE_KEYS: Record<string, keyof WorkspaceColumnFilters> = {
  name: 'name',
  source: 'source',
  updated_after: 'updatedAfter',
  updated_before: 'updatedBefore',
  rows_min: 'rowsMin',
  rows_max: 'rowsMax',
  owner: 'owner',
}
const COLUMN_FILTER_KEY_ORDER = [
  'name', 'source', 'updated_after', 'updated_before', 'rows_min', 'rows_max', 'owner',
] as const

const DATE_VALUE = /^\d{4}-\d{2}-\d{2}$/
const SOURCE_VALUE = /^(local|mount:[^\s,]{1,128})$/
const ROWS_VALUE = /^\d{1,15}$/

const DEFAULT_QUERY: WorkspaceBrowseQuery = { version: WORKSPACE_BROWSE_QUERY_VERSION }

function isSortField(value: string | null): value is WorkspaceBrowseSortField {
  return !!value && SORT_FIELDS.has(value as WorkspaceBrowseSortField)
}

function isSortOrder(value: string | null): value is WorkspaceBrowseSortOrder {
  return !!value && SORT_ORDERS.has(value as WorkspaceBrowseSortOrder)
}

function isKind(value: string | null): value is WorkspaceBrowseKind {
  return !!value && KINDS.has(value as WorkspaceBrowseKind)
}

function isView(value: string | null): value is WorkspaceBrowseViewMode {
  return !!value && VIEWS.has(value as WorkspaceBrowseViewMode)
}

function isDateValue(value: string): boolean {
  return DATE_VALUE.test(value) && !Number.isNaN(new Date(`${value}T00:00:00`).getTime())
}

function normalizeRowsBound(value: number | undefined): number | undefined {
  if (value == null || !Number.isSafeInteger(value) || value < 0) return undefined
  return value
}

/** Drop invalid values, clamp lengths, and keep the updated/rows ranges ordered. */
export function normalizeColumnFilters(
  filters: WorkspaceColumnFilters | undefined,
): WorkspaceColumnFilters | undefined {
  if (!filters) return undefined
  const normalized: WorkspaceColumnFilters = {}
  const name = filters.name?.trim().replace(/\s+/g, ' ').slice(0, MAX_COLUMN_FILTER_VALUE_CHARS)
  if (name) normalized.name = name
  const source = filters.source?.trim()
  if (source && SOURCE_VALUE.test(source)) normalized.source = source
  let after = filters.updatedAfter?.trim()
  let before = filters.updatedBefore?.trim()
  if (after && !isDateValue(after)) after = undefined
  if (before && !isDateValue(before)) before = undefined
  if (after && before && after > before) [after, before] = [before, after]
  if (after) normalized.updatedAfter = after
  if (before) normalized.updatedBefore = before
  let rowsMin = normalizeRowsBound(filters.rowsMin)
  let rowsMax = normalizeRowsBound(filters.rowsMax)
  if (rowsMin != null && rowsMax != null && rowsMin > rowsMax) [rowsMin, rowsMax] = [rowsMax, rowsMin]
  if (rowsMin != null) normalized.rowsMin = rowsMin
  if (rowsMax != null) normalized.rowsMax = rowsMax
  const owner = filters.owner?.trim().slice(0, MAX_COLUMN_FILTER_VALUE_CHARS)
  if (owner) normalized.owner = owner
  return Object.keys(normalized).length ? normalized : undefined
}

/** Decode the bounded `cf=key:value,...` map; unknown keys and malformed values are dropped. */
export function parseColumnFilterMap(
  raw: string | null | undefined,
): WorkspaceColumnFilters | undefined {
  if (!raw) return undefined
  const filters: WorkspaceColumnFilters = {}
  for (const part of raw.split(',')) {
    const trimmed = part.trim()
    const sep = trimmed.indexOf(':')
    if (sep <= 0) continue
    const key = COLUMN_FILTER_WIRE_KEYS[trimmed.slice(0, sep).trim()]
    if (!key) continue
    const encoded = trimmed.slice(sep + 1).trim()
    if (!encoded || encoded.length > MAX_COLUMN_FILTER_VALUE_CHARS * 3) continue
    let value: string
    try {
      value = decodeURIComponent(encoded)
    } catch {
      continue
    }
    if (key === 'rowsMin' || key === 'rowsMax') {
      if (ROWS_VALUE.test(value)) filters[key] = Number(value)
    } else {
      filters[key] = value
    }
  }
  return normalizeColumnFilters(filters)
}

function serializeColumnFilterMap(filters: WorkspaceColumnFilters | undefined): string | undefined {
  const normalized = normalizeColumnFilters(filters)
  if (!normalized) return undefined
  const parts: string[] = []
  for (const wireKey of COLUMN_FILTER_KEY_ORDER) {
    const value = normalized[COLUMN_FILTER_WIRE_KEYS[wireKey]!]
    if (value != null && value !== '') parts.push(`${wireKey}:${encodeURIComponent(String(value))}`)
  }
  return parts.length ? parts.join(',') : undefined
}

/**
 * Normalize a browse projection: pair sort/order, drop defaults, clamp maps.
 * Unknown or half-specified sort pairs fall back to source default (omitted).
 */
export function normalizeWorkspaceBrowseQuery(
  query: Partial<WorkspaceBrowseQuery> | undefined,
): WorkspaceBrowseQuery {
  if (!query || query.version !== WORKSPACE_BROWSE_QUERY_VERSION) return { ...DEFAULT_QUERY }

  const sort = query.sort && SORT_FIELDS.has(query.sort) ? query.sort : undefined
  const order = query.order && SORT_ORDERS.has(query.order) ? query.order : undefined
  const paired = sort && order ? { sort, order } : {}
  const kind = query.kind && KINDS.has(query.kind) ? query.kind : undefined
  const view = query.view && VIEWS.has(query.view) && query.view !== 'list' ? query.view : undefined
  const columnFilters = normalizeColumnFilters(query.columnFilters)

  return {
    version: WORKSPACE_BROWSE_QUERY_VERSION,
    ...paired,
    ...(kind ? { kind } : {}),
    ...(view ? { view } : {}),
    ...(columnFilters ? { columnFilters } : {}),
  }
}

/** Parse browse keys from a URLSearchParams / query string. Unknown version → defaults. */
export function parseWorkspaceBrowseQuery(
  raw: string | URLSearchParams | null | undefined,
): WorkspaceBrowseQuery {
  if (raw == null || raw === '') return { ...DEFAULT_QUERY }
  const params = typeof raw === 'string' ? new URLSearchParams(raw) : raw
  if ([...params.keys()].join('').length > MAX_BROWSE_QUERY_CHARS) return { ...DEFAULT_QUERY }

  const versionRaw = params.get('wq')
  // Absent version with only known browse keys is treated as v1 so short bookmarks stay valid.
  if (versionRaw != null && versionRaw !== String(WORKSPACE_BROWSE_QUERY_VERSION)) {
    return { ...DEFAULT_QUERY }
  }

  const sort = params.get('sort')
  const order = params.get('order')
  return normalizeWorkspaceBrowseQuery({
    version: WORKSPACE_BROWSE_QUERY_VERSION,
    ...(isSortField(sort) ? { sort } : {}),
    ...(isSortOrder(order) ? { order } : {}),
    ...(isKind(params.get('kind')) ? { kind: params.get('kind') as WorkspaceBrowseKind } : {}),
    ...(isView(params.get('view')) ? { view: params.get('view') as WorkspaceBrowseViewMode } : {}),
    columnFilters: parseColumnFilterMap(params.get('cf')),
  })
}

/** Serialize to a query string with defaults omitted. Empty string means no browse params. */
export function serializeWorkspaceBrowseQuery(query: WorkspaceBrowseQuery | undefined): string {
  const normalized = normalizeWorkspaceBrowseQuery(query)
  const params = new URLSearchParams()
  const hasBrowse = !!(normalized.sort || normalized.kind || normalized.view || normalized.columnFilters)
  if (!hasBrowse) return ''
  params.set('wq', String(WORKSPACE_BROWSE_QUERY_VERSION))
  if (normalized.sort && normalized.order) {
    params.set('sort', normalized.sort)
    params.set('order', normalized.order)
  }
  if (normalized.kind) params.set('kind', normalized.kind)
  if (normalized.view) params.set('view', normalized.view)
  const cf = serializeColumnFilterMap(normalized.columnFilters)
  if (cf) params.set('cf', cf)
  const encoded = params.toString()
  return encoded.length > MAX_BROWSE_QUERY_CHARS ? '' : encoded
}

/** Extract only browse keys from a full workspace query string. */
export function extractWorkspaceBrowseQuery(raw: string | URLSearchParams | null | undefined): string {
  if (raw == null || raw === '') return ''
  const source = typeof raw === 'string' ? new URLSearchParams(raw) : raw
  const browse = new URLSearchParams()
  for (const key of WORKSPACE_BROWSE_QUERY_KEYS) {
    const value = source.get(key)
    if (value != null && value !== '') browse.set(key, value)
  }
  return serializeWorkspaceBrowseQuery(parseWorkspaceBrowseQuery(browse))
}

export function browseQueryFromSortMode(sortMode: WorkspaceBrowseSortMode): Pick<WorkspaceBrowseQuery, 'sort' | 'order'> {
  if (sortMode === 'source') return {}
  const [sort, order] = sortMode.split('-') as [WorkspaceBrowseSortField, WorkspaceBrowseSortOrder]
  return { sort, order }
}

export function sortModeFromBrowseQuery(query: WorkspaceBrowseQuery): WorkspaceBrowseSortMode {
  if (!query.sort || !query.order) return 'source'
  return `${query.sort}-${query.order}` as WorkspaceBrowseSortMode
}

export function browseStateFromQuery(serialized: string): {
  sortMode: WorkspaceBrowseSortMode
  kindFilter: WorkspaceBrowseKind | 'all'
  viewMode: WorkspaceBrowseViewMode
  columnFilters: WorkspaceColumnFilters
} {
  const query = parseWorkspaceBrowseQuery(serialized)
  return {
    sortMode: sortModeFromBrowseQuery(query),
    kindFilter: query.kind ?? 'all',
    viewMode: query.view ?? 'list',
    columnFilters: query.columnFilters ?? {},
  }
}

export function serializeBrowseState(state: {
  sortMode: WorkspaceBrowseSortMode
  kindFilter: WorkspaceBrowseKind | 'all'
  viewMode: WorkspaceBrowseViewMode
  columnFilters?: WorkspaceColumnFilters
}): string {
  return serializeWorkspaceBrowseQuery({
    version: WORKSPACE_BROWSE_QUERY_VERSION,
    ...browseQueryFromSortMode(state.sortMode),
    ...(state.kindFilter !== 'all' ? { kind: state.kindFilter } : {}),
    ...(state.viewMode !== 'list' ? { view: state.viewMode } : {}),
    ...(state.columnFilters ? { columnFilters: state.columnFilters } : {}),
  })
}
