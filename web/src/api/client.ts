// Kernel HTTP client. The canvas builds fine with no kernel; data/preview/run need it.
import type {
  CanvasKernelStatus,
  CatalogBrowse, CatalogEdit, CatalogFolder, CatalogMetadata, CatalogPage, CatalogQueryParams, CatalogTable, CompilePlan, DatasetRevisionCapabilities, DatasetRevisionDetail, DatasetRevisionPage, DatasetRevisionResolution, DatasetViewCreateRequest, DatasetViewDefinition, DatasetViewPreview, Facets,
  InputDrift, JoinAnalysis, JoinSuggestion, KernelInfo, LineageResult, PipelineImport,
  CanvasTransformReference, PerNodeStatus, PluginInfo, ProcessorDescriptor, ProfileEstimate, ProfileIdentity, ProfileResult, RegisterRequest, Relationship, ResourceSpec, RunEstimate, RunInputManifestItem, RunOutput, RunStatus, SampleResult, TransformLibraryDetail, TransformLibraryPage, WriteAdmission, WriteIntent, WriteReceipt,
  CatalogUnregisterResult, WorkspaceAddDatasetResult, WorkspaceBrowsePage, WorkspaceCreateCanvasResult,
  WorkspaceMoveCanvasResult, WorkspaceProviderRelinkResult, WorkspaceResourceResolution, WorkspaceSearchPage,
} from '../types/api'
import type { CanvasDoc, ColumnSchema } from '../types/graph'

const BASE = '/api'

// The current user id, carried on every request as X-DP-User (internal-tool-grade identity).
let _userId: string | null = null
export function setApiUser(id: string | null) { _userId = id }

export class KernelError extends Error {
  status: number
  code?: string
  retryable?: boolean
  constructor(status: number, message: string, code?: string, retryable?: boolean) {
    super(message)
    this.status = status
    this.code = code
    this.retryable = retryable
  }
}

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  // Only JSON string bodies get the JSON content-type; a raw File/Blob upload keeps the browser's own
  // content-type (forcing application/json would corrupt it). Non-string body ⇒ don't set it.
  const rawBody = opts?.body != null && typeof opts.body !== 'string'
  const headers: Record<string, string> = { ...(rawBody ? {} : { 'Content-Type': 'application/json' }), ...(opts?.headers as Record<string, string>) }
  if (_userId) headers['X-DP-User'] = _userId
  const res = await fetch(`${BASE}${path}`, { ...opts, headers })
  if (!res.ok) {
    let detail = res.statusText
    let code: string | undefined
    let retryable: boolean | undefined
    try {
      const body = await res.json()
      detail = body.detail ?? detail
      code = typeof body.code === 'string' ? body.code : undefined
      retryable = typeof body.retryable === 'boolean' ? body.retryable : undefined
    } catch {
      /* noop */
    }
    throw new KernelError(
      res.status, typeof detail === 'string' ? detail : JSON.stringify(detail), code, retryable,
    )
  }
  return res.json() as Promise<T>
}

async function reqVoid(path: string, opts?: RequestInit): Promise<void> {
  const rawBody = opts?.body != null && typeof opts.body !== 'string'
  const headers: Record<string, string> = { ...(rawBody ? {} : { 'Content-Type': 'application/json' }), ...(opts?.headers as Record<string, string>) }
  if (_userId) headers['X-DP-User'] = _userId
  const res = await fetch(`${BASE}${path}`, { ...opts, headers })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch {
      /* noop */
    }
    throw new KernelError(res.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
}

// Strip transient UI-only fields the kernel does not need before sending a graph.
// `note` nodes are canvas annotations with no ports/build step — the engine never sees them.
function toGraph(doc: CanvasDoc) {
  const dataNodes = doc.nodes.filter((n) => n.type !== 'note' && n.type !== 'code')
  const dataIds = new Set(dataNodes.map((n) => n.id))
  return {
    id: doc.id,
    version: doc.version,
    requirements: doc.requirements ?? [],  // the canvas's declared pip deps → the kernel installs them
    nodes: dataNodes.map((n) => ({
      id: n.id,
      type: n.type,
      position: n.position,
      parentId: n.parentId ?? null, // section containment — the backend runs parentId children
      // `status` lets the server's size estimator trust a prior run's per-node row count only while the
      // node is still 'latest' (an edited node's old count would mislead) — see routers/runs._actuals_for.
      data: { title: n.data.title, config: n.data.config, bypassed: n.data.bypassed, disabled: n.data.disabled, status: n.data.status },
    })),
    edges: doc.edges.filter((e) => dataIds.has(e.source) && dataIds.has(e.target)).map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      sourceHandle: e.sourceHandle,
      targetHandle: e.targetHandle,
      data: e.data ?? { wire: 'dataset' },
    })),
  }
}

// Build the querystring for a catalog browse/facet request (lists → comma-joined; empties dropped).
function catalogQuery(p: CatalogQueryParams): string {
  const qs = new URLSearchParams()
  if (p.q) qs.set('q', p.q)
  if (p.folder) qs.set('folder', p.folder)
  if (p.tags?.length) qs.set('tags', p.tags.join(','))
  if (p.owner) qs.set('owner', p.owner)
  if (p.uris?.length) for (const u of p.uris) qs.append('uris', u)  // repeated param (uris may contain commas)
  if (p.hasColumns?.length) qs.set('hasColumns', p.hasColumns.join(','))
  if (p.sort) qs.set('sort', p.sort)
  if (p.order) qs.set('order', p.order)
  if (p.limit != null) qs.set('limit', String(p.limit))
  if (p.offset != null) qs.set('offset', String(p.offset))
  const s = qs.toString()
  return s ? `?${s}` : ''
}

function catalogSearchQuery(p: CatalogQueryParams, mode: 'lexical' | 'semantic' | 'hybrid'): string {
  const query = catalogQuery(p)
  return `${query || '?'}${query ? '&' : ''}mode=${encodeURIComponent(mode)}`
}

function fullResultExportPath(runId: string, nodeId: string, portId: string, filename?: string): string {
  const params = new URLSearchParams({ nodeId, portId })
  if (filename) params.set('filename', filename)
  // A hidden iframe cannot carry the open-mode X-DP-User header. The kernel accepts this identity
  // hint only when authentication is disabled; authenticated deployments ignore it and use session.
  if (_userId) params.set('userId', _userId)
  return `/run/${encodeURIComponent(runId)}/export?${params.toString()}`
}

export interface BackendPort { id: string; label?: string; wire: string; accepts?: string[]; multi?: boolean }
export interface BackendParam { name: string; type: string; default?: unknown; options?: string[]; label?: string; lang?: string; required?: boolean; showWhen?: { param: string; in: string[] } }
export interface BackendNodeSpec {
  kind: string; title: string; category: string; tag?: string
  inputs: BackendPort[]; outputs: BackendPort[]; params: BackendParam[]
  canBypass: boolean; previewable: boolean; requires?: ResourceSpec | null; blurb: string; source?: string
}

export interface AgentBackendNode { id: string; type: string; position: { x: number; y: number }; data: { title?: string; config?: Record<string, unknown> } }
export interface AgentBackendEdge { id: string; source: string; target: string; sourceHandle?: string | null; targetHandle?: string | null; data?: { wire: string } }
export type SettingScope = 'global' | 'user'
export interface SettingsRevision { global: number; user: number }
export interface SettingsSnapshot {
  global: Record<string, unknown>
  user: Record<string, unknown>
  revision: SettingsRevision
}
export interface SettingChange { scope: SettingScope; key: string; value: unknown }
export interface AgentResult {
  available: boolean
  errorCode?: string
  reason?: string
  model?: string
  provider?: string
  summary?: string
  transcript?: { tool: string; input: Record<string, unknown>; result: Record<string, unknown> }[]
  graph?: { nodes: AgentBackendNode[]; edges: AgentBackendEdge[] }
  policy?: AgentDataDisclosure
  disclosure?: AgentDataDisclosure
}

export interface AgentDataDisclosure {
  provider?: string
  model?: string
  level?: string
  endpointIsLocal?: boolean
  hosted?: boolean
  rowValuesMayLeave?: boolean
}

export interface AgentStatus {
  available: boolean
  errorCode?: string
  reason: string
  model?: string
  provider?: string
  policy?: AgentDataDisclosure
  disclosure?: AgentDataDisclosure
}

export const api = {
  kernel: () => req<KernelInfo>('/kernel'),
  nodes: () => req<BackendNodeSpec[]>('/nodes'),
  registerFile: (uri: string, name?: string) =>
    req<CatalogTable>('/catalog/register', { method: 'POST', body: JSON.stringify({ uri, name }) }),
  // register with the full curation payload (the Register modal): name/folder/tags/owner/description
  registerDataset: (r: RegisterRequest) =>
    req<CatalogTable>('/catalog/register', { method: 'POST', body: JSON.stringify(r) }),
  // upload a dataset file's bytes (raw body; name in a header) → lands in shared storage + registers
  uploadFile: (file: File) =>
    req<CatalogTable>('/catalog/upload', { method: 'POST', body: file, headers: { 'X-Upload-Filename': encodeURIComponent(file.name) } }),

  // One filtered/sorted page with its bounded window and total in the response body.
  tablesPage: (params: CatalogQueryParams = {}) =>
    req<CatalogPage>(`/catalog/tables${catalogQuery(params)}`),
  facets: (params: CatalogQueryParams = {}) =>
    req<Facets>(`/catalog/facets${catalogQuery(params)}`),
  catalogTree: (prefix = '', options?: { signal?: AbortSignal }) =>
    req<CatalogBrowse>(`/catalog/tree${prefix ? `?prefix=${encodeURIComponent(prefix)}` : ''}`, {
      signal: options?.signal,
    }),
  workspaceBrowse: (containerId: string, params?: { cursor?: string; limit?: number }) => {
    const query = new URLSearchParams()
    if (params?.cursor) query.set('cursor', params.cursor)
    if (params?.limit) query.set('limit', String(params.limit))
    return req<WorkspaceBrowsePage>(`/workspace/containers/${encodeURIComponent(containerId)}${query.size ? `?${query}` : ''}`)
  },
  workspaceResource: (resourceId: string) =>
    req<WorkspaceResourceResolution>(`/workspace/resources/${encodeURIComponent(resourceId)}`),
  workspaceRelink: (resourceId: string, body: { mountId: string; resourceId: string }) =>
    req<WorkspaceProviderRelinkResult>(
      `/workspace/resources/${encodeURIComponent(resourceId)}/relink`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  workspaceSearch: (query: string, params?: { cursor?: string; limit?: number }) => {
    const search = new URLSearchParams({ q: query })
    if (params?.cursor) search.set('cursor', params.cursor)
    if (params?.limit) search.set('limit', String(params.limit))
    return req<WorkspaceSearchPage>(`/workspace/search?${search}`)
  },
  workspaceCreateCanvas: (body: { containerId: string; expectedContainerVersion: number; name: string; datasetIds?: string[]; providerDatasetRefs?: string[]; transformId?: string; transformVersion?: string }) =>
    req<WorkspaceCreateCanvasResult>('/workspace/canvases', {
      method: 'POST', body: JSON.stringify(body),
    }),
  workspaceAddDatasets: (canvasId: string, body: { datasetIds?: string[]; providerDatasetRefs?: string[]; expectedCanvasVersion: number }) =>
    req<WorkspaceAddDatasetResult>(`/workspace/canvases/${encodeURIComponent(canvasId)}/datasets`, {
      method: 'POST', body: JSON.stringify(body),
    }),
  workspaceAddTransform: (canvasId: string, body: { transformId: string; transformVersion: string; expectedCanvasVersion: number; replaceNodeId?: string }) =>
    req<{ ok: boolean; id: string; version: number; nodeId: string; doc: CanvasDoc }>(
      `/workspace/canvases/${encodeURIComponent(canvasId)}/transforms`, {
        method: 'POST', body: JSON.stringify(body),
      }),
  workspaceMoveCanvas: (placementId: string, body: { containerId: string; expectedContainerVersion: number; expectedVersion: number }) =>
    req<WorkspaceMoveCanvasResult>(`/workspace/placements/${encodeURIComponent(placementId)}/canvas`, {
      method: 'PUT', body: JSON.stringify(body),
    }),
  // folder entities (incl. empty ones) — used for the folder-name autocomplete + tree editing
  catalogFolders: () => req<CatalogFolder[]>('/catalog/folders'),
  createFolder: (path: string) =>
    req<CatalogFolder>('/catalog/folders', { method: 'POST', body: JSON.stringify({ path }) }),
  renameFolder: (oldPath: string, newPath: string) =>
    req<{ ok: boolean }>('/catalog/folders/rename', { method: 'PUT', body: JSON.stringify({ oldPath, newPath }) }),
  deleteFolder: (path: string) =>
    req<{ ok: boolean }>('/catalog/folders/delete', { method: 'POST', body: JSON.stringify({ path }) }),
  searchCatalog: (params: CatalogQueryParams, mode: 'lexical' | 'semantic' | 'hybrid' = 'hybrid') =>
    req<CatalogTable[]>(`/catalog/search${catalogSearchQuery(params, mode)}`),
  table: (id: string) => req<CatalogTable>(`/catalog/tables/${encodeURIComponent(id)}`),
  tableByRegistration: (id: string) =>
    req<CatalogTable>(`/catalog/tables/${encodeURIComponent(id)}?registration=true`),
  datasetRevisions: (tableId: string, options?: { limit?: number; cursor?: string }) => {
    const query = new URLSearchParams()
    if (options?.limit != null) query.set('limit', String(options.limit))
    if (options?.cursor) query.set('cursor', options.cursor)
    const suffix = query.size ? `?${query.toString()}` : ''
    return req<DatasetRevisionPage>(`/catalog/tables/${encodeURIComponent(tableId)}/revisions${suffix}`)
  },
  datasetRevisionCapabilities: (tableId: string) =>
    req<DatasetRevisionCapabilities>(`/catalog/tables/${encodeURIComponent(tableId)}/revisions/capabilities`),
  resolveDatasetRevision: (tableId: string, asOf?: string) => {
    const query = asOf ? `?asOf=${encodeURIComponent(asOf)}` : ''
    return req<DatasetRevisionResolution>(`/catalog/tables/${encodeURIComponent(tableId)}/revisions/resolve${query}`)
  },
  datasetRevision: (datasetId: string, revisionId: string) =>
    req<DatasetRevisionDetail>(`/catalog/revisions/${encodeURIComponent(datasetId)}/${encodeURIComponent(revisionId)}`),
  createDatasetView: (body: DatasetViewCreateRequest) =>
    req<DatasetViewDefinition>('/dataset-views', {
      method: 'POST', body: JSON.stringify(body),
    }),
  datasetView: (viewId: string) =>
    req<DatasetViewDefinition>(`/dataset-views/${encodeURIComponent(viewId)}`),
  previewDatasetView: (viewId: string) =>
    req<DatasetViewPreview>(`/dataset-views/${encodeURIComponent(viewId)}/preview`, {
      method: 'POST',
    }),
  deleteDatasetView: (viewId: string) =>
    req<{ ok: boolean; deleted: boolean }>(`/dataset-views/${encodeURIComponent(viewId)}`, {
      method: 'DELETE',
    }),
  setTableMetadata: (id: string, meta: CatalogMetadata) =>
    req<CatalogTable>(`/catalog/tables/${encodeURIComponent(id)}/metadata`, { method: 'PUT', body: JSON.stringify(meta) }),
  saveTableEdit: (id: string, edit: CatalogEdit) =>
    req<CatalogTable>(`/catalog/tables/${encodeURIComponent(id)}/edit`, { method: 'PUT', body: JSON.stringify(edit) }),
  unregisterTable: (id: string, expectedRegistrationId: string, expectedRevision: string) => req<{ ok: boolean }>(
    `/catalog/tables/${encodeURIComponent(id)}?${new URLSearchParams({ expected_registration_id: expectedRegistrationId, expected_revision: expectedRevision })}`,
    { method: 'DELETE' },
  ),
  unregisterTables: (targets: { id: string; expectedRegistrationId: string; expectedRevision: string }[]) =>
    req<CatalogUnregisterResult>('/catalog/tables/delete', { method: 'POST', body: JSON.stringify({ targets }) }),
  lineage: (uri: string, depth = 6, maxNodes = 500) =>
    req<LineageResult>(`/catalog/lineage?uri=${encodeURIComponent(uri)}&depth=${depth}&maxNodes=${maxNodes}`),

  sample: (uri: string, k = 50, columns?: string[], offset = 0) =>
    req<SampleResult>('/data/sample', { method: 'POST', body: JSON.stringify({ uri, k, columns, offset }) }),
  runOutputSample: (runId: string, nodeId: string, portId: string, k = 50, offset = 0) =>
    req<SampleResult>(`/run/${encodeURIComponent(runId)}/sample`, {
      method: 'POST', body: JSON.stringify({ nodeId, portId, k, offset }),
    }),
  fullResultExportUrl: (runId: string, nodeId: string, portId: string, filename?: string) =>
    `${BASE}${fullResultExportPath(runId, nodeId, portId, filename)}`,
  preflightFullResultExport: async (runId: string, nodeId: string, portId: string, filename?: string) => {
    // Capture one path so the HEAD and iframe GET retain the same open-mode identity even if the UI
    // user changes while preflight is in flight.
    const path = fullResultExportPath(runId, nodeId, portId, filename)
    await reqVoid(path, { method: 'HEAD' })
    return `${BASE}${path}`
  },

  processors: () => req<ProcessorDescriptor[]>('/processors'),
  transformLibrary: (params: { q?: string; source?: 'all' | 'promoted' | 'plugin'; mode?: string; category?: string; limit?: number; cursor?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.q) query.set('q', params.q)
    if (params.source && params.source !== 'all') query.set('source', params.source)
    if (params.mode) query.set('mode', params.mode)
    if (params.category) query.set('category', params.category)
    if (params.limit != null) query.set('limit', String(params.limit))
    if (params.cursor) query.set('cursor', params.cursor)
    const suffix = query.size ? `?${query}` : ''
    return req<TransformLibraryPage>(`/transform-library${suffix}`)
  },
  transformLibraryDetail: (id: string, version?: string) => {
    const query = version ? `?${new URLSearchParams({ version })}` : ''
    return req<TransformLibraryDetail>(`/transform-library/${encodeURIComponent(id)}${query}`)
  },
  deleteTransformVersion: (id: string, version: string) =>
    req<{ ok: boolean; deleted: boolean }>(
      `/processors/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}`,
      { method: 'DELETE' },
    ),
  canvasTransformReferences: (canvasId: string) =>
    req<CanvasTransformReference[]>(
      `/canvas/${encodeURIComponent(canvasId)}/transform-references`,
    ),
  promote: (body: {
    id: string; title: string; mode: string; code: string
    inputColumns: string[]; inputSchema?: ColumnSchema[]; outputSchema: ColumnSchema[]
    requirements: string[]; category?: string; blurb?: string
  }) => req<ProcessorDescriptor>('/processors/promote', { method: 'POST', body: JSON.stringify(body) }),

  importPipeline: (config: string, params?: Record<string, unknown>, options?: { signal?: AbortSignal }) =>
    req<PipelineImport>('/pipelines/import', {
      method: 'POST', body: JSON.stringify({ config, params }), signal: options?.signal,
    }),

  compile: (doc: CanvasDoc, targetNodeId?: string) =>
    req<CompilePlan>('/graph/compile', { method: 'POST', body: JSON.stringify({ graph: toGraph(doc), targetNodeId }) }),

  preview: (doc: CanvasDoc, nodeId: string, k = 50, offset = 0, portId?: string,
    inputManifest?: RunInputManifestItem[]) =>
    req<SampleResult>('/run/preview', {
      method: 'POST', body: JSON.stringify({ graph: toGraph(doc), nodeId, portId, k, offset, inputManifest }),
    }),
  profile: (doc: CanvasDoc, nodeId: string, portId?: string,
    inputManifest?: RunInputManifestItem[]) =>
    req<ProfileResult>('/run/profile', {
      method: 'POST', body: JSON.stringify({ graph: toGraph(doc), nodeId, portId, inputManifest }),
    }),

  // per-node, per-output-port columns (metadata only); null = untyped port
  schema: (doc: CanvasDoc, targetNodeId?: string, inputManifest?: RunInputManifestItem[]) =>
    req<Record<string, Record<string, ColumnSchema[] | null>>>('/graph/schema', {
      method: 'POST', body: JSON.stringify({ graph: toGraph(doc), targetNodeId, inputManifest }),
    }),
  // per-node output-size estimate (rows + confidence) → the card "~N rows" hint; unknown → rows null
  graphSizes: (doc: CanvasDoc) =>
    req<Record<string, { rows: number | null; confidence: string }>>('/graph/estimate', { method: 'POST', body: JSON.stringify({ graph: toGraph(doc) }) }),
  // the execution plan for a target: regions + backend + boundary tier + estimated size (the run-plan preview)
  plan: (doc: CanvasDoc, targetNodeId: string) =>
    req<{ regions: { id: string; outputNode: string; backend: string; worker: string | null; nodeIds: string[]; tier: string | null; rows: number | null; confidence: string; requires?: string; unsatisfied?: boolean; available?: string; preflight?: string[] }[]; error?: string }>(
      '/graph/plan', { method: 'POST', body: JSON.stringify({ graph: toGraph(doc), targetNodeId }) }),

  // catalog-driven join hints for a join node: ranked keys (measured cardinality) + a fan-out warning
  joinAnalysis: (doc: CanvasDoc, nodeId: string) =>
    req<JoinAnalysis>('/graph/join-analysis', { method: 'POST', body: JSON.stringify({ graph: toGraph(doc), targetNodeId: nodeId }) }),
  // ranked ways to join two catalog datasets directly (used outside the canvas)
  joinSuggestions: (leftUri: string, rightUri: string) =>
    req<JoinSuggestion[]>('/catalog/join-suggestions', { method: 'POST', body: JSON.stringify({ leftUri, rightUri }) }),

  // owner-declared keys + relationships (the ER view)
  declareKey: (tableId: string, columns: string[]) =>
    req<CatalogTable>(`/catalog/tables/${encodeURIComponent(tableId)}/key`, { method: 'PUT', body: JSON.stringify({ columns }) }),
  relationships: (uri?: string) =>
    req<Relationship[]>(`/catalog/relationships${uri ? `?uri=${encodeURIComponent(uri)}` : ''}`),
  addRelationship: (rel: Relationship) =>
    req<Relationship[]>('/catalog/relationships', { method: 'POST', body: JSON.stringify(rel) }),
  deleteRelationship: (rel: Relationship) =>
    req<Relationship[]>('/catalog/relationships/delete', { method: 'POST', body: JSON.stringify(rel) }),

  estimate: (doc: CanvasDoc, targetNodeId?: string, inputManifest?: RunInputManifestItem[]) =>
    req<RunEstimate>('/run/estimate', {
      method: 'POST', body: JSON.stringify({ graph: toGraph(doc), targetNodeId, inputManifest }),
    }),

  writeAdmission: (doc: CanvasDoc, nodeId: string, submissionId: string,
    inputManifest?: RunInputManifestItem[]) =>
    req<WriteAdmission>('/run/write-admission', {
      method: 'POST', body: JSON.stringify({
        graph: toGraph(doc), nodeId, submissionId, inputManifest,
      }),
    }),

  inputDrift: (doc: CanvasDoc, targetNodeId: string, inputManifest: RunInputManifestItem[]) =>
    req<InputDrift>('/run/input-drift', {
      method: 'POST', body: JSON.stringify({ graph: toGraph(doc), targetNodeId, inputManifest }),
    }),

  profileEstimate: (doc: CanvasDoc, nodeId: string, portId?: string,
    inputManifest?: RunInputManifestItem[]) =>
    req<ProfileEstimate>('/run/profile-estimate', {
      method: 'POST', body: JSON.stringify({ graph: toGraph(doc), nodeId, portId, inputManifest }),
    }),

  profileIdentity: (doc: CanvasDoc, nodeId: string, portId?: string,
    inputManifest?: RunInputManifestItem[]) =>
    req<ProfileIdentity>('/run/profile-identity', {
      method: 'POST', body: JSON.stringify({ graph: toGraph(doc), nodeId, portId, inputManifest }),
    }),

  run: async (doc: CanvasDoc, targetNodeId: string | undefined, confirmed: boolean, submissionId: string,
    inputManifest?: RunInputManifestItem[], writeIntent?: WriteIntent) => {
    // Keep the same client-owned id across a lost HTTP response: the hub adopts the one immutable
    // admission instead of starting another full pass against a moved source head.
    const admittedProducerVersion = writeIntent?.provenance.publication.producerVersion
    const submittedDoc = admittedProducerVersion == null
      ? doc
      : { ...doc, version: admittedProducerVersion }
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await req<RunStatus>('/run', {
          method: 'POST',
          body: JSON.stringify({ graph: toGraph(submittedDoc), targetNodeId, confirmed, submissionId, inputManifest, writeIntent }),
        })
      } catch (error) {
        if (error instanceof KernelError || attempt >= 2) throw error
        await new Promise((resolve) => setTimeout(resolve, 150 * (attempt + 1)))
      }
    }
  },

  fullProfile: (doc: CanvasDoc, nodeId: string, portId: string | undefined, planDigest: string,
    submissionId: string, confirmed = false, inputManifest?: RunInputManifestItem[]) =>
    req<RunStatus>('/run/profile-job', {
      method: 'POST', body: JSON.stringify({
        graph: toGraph(doc), nodeId, portId, planDigest, submissionId, confirmed, inputManifest,
      }),
    }),

  runStatus: (runId: string) => req<RunStatus>(`/run/${runId}`),
  activeRuns: (canvasId: string) => req<RunStatus[]>(`/canvas/${encodeURIComponent(canvasId)}/active-runs`),
  profileJobs: (canvasId: string) => req<RunStatus[]>(`/canvas/${encodeURIComponent(canvasId)}/profile-jobs`),
  kernelState: (canvasId: string) => req<CanvasKernelStatus>(`/canvas/${encodeURIComponent(canvasId)}/kernel`),
  restartKernel: (canvasId: string) => req<{ ok: boolean; restarted: boolean }>(`/canvas/${encodeURIComponent(canvasId)}/kernel/restart`, { method: 'POST' }),
  cancelRun: (runId: string) => req<RunStatus>(`/run/${runId}/cancel`, { method: 'POST' }),
  retryRun: (runId: string, actionId: string) => req<RunStatus>(`/run/${runId}/retry`, {
    method: 'POST', body: JSON.stringify({ actionId }),
  }),

  agentStatus: () => req<AgentStatus>('/agent'),
  agentAct: (doc: CanvasDoc, outcome: string) =>
    req<AgentResult>('/agent', { method: 'POST', body: JSON.stringify({ outcome, graph: toGraph(doc) }) }),

  // users (internal-tool identity) + settings
  me: () => req<DpUser>('/me'),
  users: () => req<DpUser[]>('/users'),
  createUser: (name: string, password?: string) =>
    req<DpUser>('/users', { method: 'POST', body: JSON.stringify({ name, password }) }),
  getSettings: () => req<SettingsSnapshot>('/settings'),
  putSettingsBatch: (expectedRevision: SettingsRevision, changes: SettingChange[]) =>
    req<{ ok: boolean; revision: SettingsRevision }>('/settings/batch', {
      method: 'PUT', body: JSON.stringify({ expectedRevision, changes }),
    }),

  // loaded plugin packs (name/version/error + any declared [[config]] schema & current values)
  plugins: () => req<PluginInfo[]>('/plugins'),

  // credentials (first-class Cred entity — refs only; admin-only)
  listCreds: () => req<Cred[]>('/creds'),
  createCred: (body: { name: string; kind: CredKind; fields: Record<string, string> }) =>
    req<Cred>('/creds', { method: 'POST', body: JSON.stringify(body) }),
  updateCred: (id: string, body: { name: string; kind: CredKind; fields: Record<string, string> }) =>
    req<Cred>(`/creds/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  deleteCred: (id: string) => req<{ ok: boolean }>(`/creds/${id}`, { method: 'DELETE' }),

  // destinations (save/open "places" — local + pluggable object stores)
  destinations: () => req<{ destinations: DestinationPreset[]; backends: string[] }>('/destinations'),
  browseDestination: (destinationId: string, path = '') =>
    req<BrowseResult>('/destinations/browse', { method: 'POST', body: JSON.stringify({ destinationId, path }) }),
  mkdirDestination: (destinationId: string, path: string, name: string) =>
    req<{ ok?: boolean; error?: string }>('/destinations/mkdir', { method: 'POST', body: JSON.stringify({ destinationId, path, name }) }),

  // per-user canvases (multi-file)
  listCanvases: () => req<CanvasFile[]>('/canvas'),
  getCanvas: (id: string) => req<CanvasDoc>(`/canvas/${id}`),
  createCanvas: (doc: CanvasDoc) =>
    req<{ ok: boolean; id: string; created: boolean }>('/canvas', {
      method: 'POST', body: JSON.stringify(doc),
    }),
  saveCanvas: (doc: CanvasDoc, keepalive = false, expectedVersion?: number) => {  // keepalive: let the PUT survive a tab-close flush
    const query = expectedVersion == null ? '' : `?expectedVersion=${encodeURIComponent(expectedVersion)}`
    return req<{ ok: boolean; id: string; version: number }>(`/canvas/${doc.id}${query}`, {
      method: 'PUT', body: JSON.stringify(doc), keepalive,
    })
  },
  deleteCanvas: (id: string) => req<{ ok: boolean }>(`/canvas/${id}`, { method: 'DELETE' }),
  listRuns: (canvasId: string) => req<RunRecordDto[]>(`/canvas/${canvasId}/runs`),
  executionManifest: (canvasId: string, subjectId: string) =>
    req<ExecutionManifestDetail>(
      `/canvas/${encodeURIComponent(canvasId)}/runs/${encodeURIComponent(subjectId)}/manifest`,
    ),
  workspaceJobs: (params: WorkspaceJobsQuery = {}) => {
    const query = new URLSearchParams()
    if (params.limit != null) query.set('limit', String(params.limit))
    if (params.cursor) query.set('cursor', params.cursor)
    if (params.status) query.set('status', params.status)
    if (params.canvasId) query.set('canvas_id', params.canvasId)
    if (params.nodeId) query.set('node_id', params.nodeId)
    if (params.runId) query.set('run_id', params.runId)
    if (params.backend) query.set('backend', params.backend)
    if (params.after) query.set('after', params.after)
    if (params.before) query.set('before', params.before)
    if (params.q) query.set('q', params.q)
    return req<WorkspaceJobsPage>(`/jobs?${query}`)
  },
  inboxList: (params: InboxListQuery = {}) => {
    const query = new URLSearchParams()
    if (params.limit != null) query.set('limit', String(params.limit))
    if (params.cursor) query.set('cursor', params.cursor)
    if (params.filter) query.set('filter', params.filter)
    return req<InboxPage>(`/inbox?${query}`)
  },
  inboxUnreadCount: () => req<InboxUnreadCount>('/inbox/unread-count'),
  inboxMarkRead: (itemId: string) =>
    req<InboxItemDto>(`/inbox/${encodeURIComponent(itemId)}/read`, { method: 'POST' }),
  // named/versioned schema contracts (workspace artifacts a node can reference by name)
  listSchemas: () => req<SchemaContractDto[]>('/schemas'),
  saveSchema: (name: string, columns: ColumnSchema[]) =>
    req<SchemaContractDto>('/schemas', { method: 'POST', body: JSON.stringify({ name, columns }) }),
  diffSchema: (name: string, a: number, b: number) =>
    req<SchemaCompatibilityDto>(`/schemas/diff?name=${encodeURIComponent(name)}&a=${a}&b=${b}`),
  listVersions: (canvasId: string) => req<CanvasVersionDto[]>(`/canvas/${canvasId}/versions`),
  restoreCanvas: (canvasId: string, versionId: string) =>
    req<{ ok: boolean; id: string; doc: CanvasDoc }>(`/canvas/${canvasId}/restore`, { method: 'POST', body: JSON.stringify({ version_id: versionId }) }),
  authStatus: () => req<{ authEnabled: boolean; userId: string | null }>('/auth/status'),
  login: (userId: string, password: string) => req<{ ok: boolean; userId: string }>('/auth/login', { method: 'POST', body: JSON.stringify({ userId, password }) }),
  logout: () => req<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
  changePassword: (oldPassword: string, newPassword: string) =>
    req<{ ok: boolean }>('/auth/password', { method: 'POST', body: JSON.stringify({ oldPassword, newPassword }) }),
  getShares: (canvasId: string) => req<{ visibility: CanvasVisibility; shares: ShareInfo[] }>(`/canvas/${canvasId}/shares`),
  addShare: (canvasId: string, body: { userId?: string; role?: ShareRole; visibility?: CanvasVisibility }) =>
    req<{ ok: boolean }>(`/canvas/${canvasId}/share`, { method: 'POST', body: JSON.stringify(body) }),
  removeShare: (canvasId: string, userId: string) =>
    req<{ ok: boolean }>(`/canvas/${canvasId}/share/${userId}`, { method: 'DELETE' }),
}

export type CredKind = 'object_store' | 'agent'
export interface Cred { id: string; name: string; kind: CredKind; fields: Record<string, string>; createdAt?: string | null }
export interface DestinationPreset { id: string; name: string; backend: string; root: string; credId?: string | null }
export interface BrowseEntry { name: string; kind: 'dir' | 'file'; uri: string }
export interface BrowseResult { path: string; entries: BrowseEntry[]; error?: string | null; writable?: boolean }
export type PerNodeStat = PerNodeStatus
export type ExecutionManifestAvailability = 'available' | 'pruned' | 'not_recorded' | 'unavailable' | 'corrupt'
export interface ExecutionManifestDocument {
  schemaVersion: number
  graph: { nodes: unknown[]; edges: unknown[]; requirements: string[] }
  target: { nodeId?: string | null; portId?: string | null }
  admittedInputs: Array<{ nodeId: string; datasetId: string; revisionId: string; provider: string }>
  writeIntent?: unknown | null
  descriptors: unknown
  parameters?: unknown
}
export interface ExecutionManifestDetail {
  sha256?: string | null
  schemaVersion?: number | null
  availability: ExecutionManifestAvailability
  document?: ExecutionManifestDocument | null
}
export interface RunRecordDto { id: string; runId?: string | null; requestId?: string | null; jobType: 'run' | 'profile'; status: string; targetNodeId?: string | null; targetPortId?: string | null; rows?: number | null; ms?: number | null; error?: string | null; inputManifest?: RunInputManifestItem[] | null; executionManifestSha256?: string | null; executionManifestSchemaVersion?: number | null; executionManifestAvailability?: ExecutionManifestAvailability; executionManifestReconstructable?: boolean; outputs: RunOutput[]; profile?: ProfileResult | null; perNode?: PerNodeStat[] | null; createdAt?: string | null }
export interface DurableTaskAttemptDto { id: string; attemptNumber: number; status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'fenced'; progress?: number | null; error?: string | null; startedAt?: string | null; completedAt?: string | null; updatedAt: string }
export interface ExternalWaitJobDto { providerKind: string; phase: 'unsubmitted' | 'submitting' | 'accepted' | 'running' | 'provider_succeeded' | 'downloading' | 'downloaded' | 'publishing' | 'published' | 'provider_failed' | 'provider_cancelled' | 'finalization_failed' | 'cancelled_before_submit' | 'cancelled_after_success'; attemptNumber: number; cancelRequested: boolean; canRetry: boolean; diagnosticCode?: string | null }
export interface CheckpointJobDto {
  phase: 'pending' | 'materializing' | 'committed' | 'publishing' | 'terminal'
  checkpointNodeId: string
  outputPortId: string
  committedAt?: string | null
  rows?: number | null
  bytes?: number | null
  contentDigest?: string | null
  resumeEligible: boolean
  retryLabel?: string | null
  clientKey: string
  diagnosticCode?: string | null
}
export interface BoundedFanoutJobDto {
  stage: 'checkpointing' | 'planning' | 'running_partitions' | 'gathering' | 'publishing' | 'terminal'
  partitionCount?: number | null
  completedPartitions: number
  failedPartitions: number
  checkpoint: 'pending' | 'committed' | 'reused'
  gather: 'pending' | 'running' | 'committed'
  diagnosticCode?: string | null
}
export interface WorkspaceJobDto extends RunRecordDto { canvasId: string; canvasName: string; nodeLabel?: string | null; backend: string; placement: 'local' | 'distributed'; attempt: string; progress?: number | null; updatedAt?: string | null; taskId?: string | null; taskAttempts?: DurableTaskAttemptDto[]; cancelRequested?: boolean; canRetry?: boolean; canCancel?: boolean; writeIntent?: WriteIntent | null; outputReceipt?: WriteReceipt | null; externalWait?: ExternalWaitJobDto | null; checkpoint?: CheckpointJobDto | null; boundedFanout?: BoundedFanoutJobDto | null }
export interface WorkspaceJobsPage { items: WorkspaceJobDto[]; nextCursor?: string | null; hasMore: boolean }
export interface WorkspaceJobsQuery { limit?: number; cursor?: string; status?: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'; canvasId?: string; nodeId?: string; runId?: string; backend?: string; after?: string; before?: string; q?: string }
export interface InboxItemDto {
  id: string
  taskId: string
  canvasId: string
  canvasName?: string | null
  taskKind: InboxTaskKind
  outcome: 'completed' | 'failed' | 'cancelled'
  diagnosticCode?: string | null
  terminalAt: string
  readAt?: string | null
  jobAvailable: boolean
}
export type InboxTaskKind =
  | 'managed_local_write'
  | 'external_wait'
  | 'linear_checkpoint_write'
  | 'bounded_fanout_write'
export interface InboxPage { items: InboxItemDto[]; nextCursor?: string | null; hasMore: boolean }
export interface InboxUnreadCount { count: number }
export interface InboxListQuery { limit?: number; cursor?: string; filter?: 'unread' | 'all' }
export interface SchemaContractDto { name: string; version: number; columns: ColumnSchema[]; versions?: number[] }
export interface SchemaFieldCompatibilityDto { kind: 'unchanged' | 'renamed' | 'added' | 'removed' | 'changed'; status: 'compatible' | 'breaking' | 'unknown'; reason: string; fieldId?: string | null; oldName?: string | null; newName?: string | null }
export interface SchemaCompatibilityDto { status: 'compatible' | 'breaking' | 'unknown'; fields: SchemaFieldCompatibilityDto[] }
export interface CanvasVersionDto { id: string; version: number; label?: string | null; authorId?: string | null; createdAt?: string | null }
export type CanvasRole = 'owner' | 'editor' | 'viewer'
export type ShareRole = Exclude<CanvasRole, 'owner'>
export type CanvasVisibility = 'private' | 'workspace' | 'workspace_view'
export interface ShareInfo { userId: string; name: string; role: ShareRole }
export interface DpUser { id: string; name: string; email?: string | null; capabilities?: string[] }
export interface CanvasFile { id: string; name: string; version: number; updatedAt?: string; role?: CanvasRole; shared?: boolean; visibility?: CanvasVisibility }

export { toGraph }
