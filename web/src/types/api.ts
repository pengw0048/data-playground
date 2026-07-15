// Kernel API DTOs — camelCase on the wire, mirrors kernel/models.py.
import type { ColumnSchema } from './graph'

export interface ResourceSpec {
  cpu?: number | null
  mem?: string | null
  gpu?: number | null
  gpuType?: string | null
  labels?: Record<string, string>
}
export interface WorkerInfo { id: string; capacity: ResourceSpec; state: 'idle' | 'busy' | 'down' }
export interface BackendInfo { name: string; workers: WorkerInfo[] }

export interface CapabilityView { id: string; label: string; viewer: { kind: string } }
export interface KernelInfo {
  mode: 'local' | 'distributed'
  backend: string
  warm: boolean
  version: string
  adapters: string[]
  runners: string[]
  processors: string[]
  capabilities: string[]
  capabilityViews?: CapabilityView[]  // plugin capabilities that declare a viewer tab (rendered generically)
  backends: BackendInfo[]
}

export interface RelationCacheStats {
  entries: number
  bytes: number
  maxEntries: number
  maxBytes: number
  tooBig: number
}

// GET /canvas/{id}/kernel: the lease state, merged with the kernel's own /status when reachable.
export interface CanvasKernelStatus {
  exists: boolean
  state?: string
  stale?: boolean
  reachable?: boolean   // false = a live lease whose HTTP /status could not be reached (degraded, not warm)
  relationCache?: RelationCacheStats
  memoryLimit?: string | null
  memoryRssBytes?: number
  uptimeSeconds?: number
  inflight?: number
  activeRuns?: number
}

export interface KeyInfo { columns: string[]; confidence: 'declared' | 'verified' | 'inferred'; unique?: boolean | null }

export interface CatalogTable {
  id: string
  name: string
  uri: string
  rowCount?: number | null
  version?: string | null
  columns: ColumnSchema[]
  keys?: KeyInfo[]
  missing?: boolean
  updatedAt?: string | null
  meta?: string | null
  // organization primitives (browse hierarchy + faceting + curation)
  folder?: string
  tags?: string[]
  owner?: string | null
  description?: string | null
  usage?: number
}

// filter/sort/paginate params for the catalog browse query (mirrors CatalogQuery on the server)
export interface CatalogQueryParams {
  q?: string
  folder?: string
  tags?: string[]
  owner?: string
  uris?: string[]
  hasColumns?: string[]
  sort?: 'name' | 'rows' | 'updated' | 'usage' | 'folder'
  order?: 'asc' | 'desc'
  limit?: number
  offset?: number
}

export interface CatalogPage { items: CatalogTable[]; total: number; hasMore: boolean }
export interface FacetValue { value: string; count: number }
export interface Facets { folders: FacetValue[]; tags: FacetValue[]; owners: FacetValue[]; semanticAvailable?: boolean }
export interface FolderNode { name: string; path: string; tableCount: number }
export interface CatalogFolder { path: string }
export interface CatalogBrowse { prefix: string; folders: FolderNode[]; tables: CatalogTable[] }
export interface CatalogMetadata { folder?: string; tags?: string[]; owner?: string | null; description?: string | null; name?: string | null }
export interface RegisterRequest { uri: string; name?: string; folder?: string; tags?: string[]; owner?: string; description?: string }

export type Cardinality = '1:1' | '1:N' | 'N:1' | 'N:M' | 'unknown'

export interface JoinSuggestion {
  leftColumns: string[]
  rightColumns: string[]
  cardinality: Cardinality
  confidence: 'declared' | 'verified' | 'inferred'
  score: number
  reason: string
}

export interface JoinAnalysis {
  suggestions: JoinSuggestion[]
  warning?: string | null
  note?: string | null
}

export interface Relationship {
  leftUri: string
  leftColumns: string[]
  rightUri: string
  rightColumns: string[]
  cardinality: Cardinality
  confidence: 'declared' | 'verified' | 'inferred'
}

export interface LineageNode { id: string; name: string; uri: string; kind: string }
export interface LineageEdge { parent: string; child: string; column?: string | null; pipeline?: string | null }
export interface LineageResult { nodes: LineageNode[]; edges: LineageEdge[]; truncated?: boolean }

export interface SampleResult {
  columns: ColumnSchema[]
  rows: Record<string, unknown>[]
  rowCount?: number | null
  hasMore?: boolean
  truncated: boolean
  previewRef?: string | null
  notPreviewable: boolean
  error?: boolean
  reason?: string | null
  wire: string
}

export interface ColumnProfile {
  name: string
  type: string
  nonNull: number
  nulls: number
  distinct?: number | null
  min?: string | null
  max?: string | null
  mean?: number | null
}

export interface ProfileResult {
  columns: ColumnProfile[]
  rowCount: number
  sampled: boolean
  notPreviewable: boolean
  error?: boolean
  reason?: string | null
}

export interface ProcessorDescriptor {
  id: string
  version: string
  title: string
  mode: string
  category: string
  inputColumns: string[]
  outputSchema: ColumnSchema[]
  paramsSchema: Record<string, any>
  previewable: boolean
  blurb: string
}

export type Placement = 'local' | 'distributed'

export interface RunEstimate {
  rows: number | null   // real source-row count; null when size is unknown (no countable source)
  bytes?: number | null // estimated peak data volume — the confirm gate's cost signal
  placement: Placement
  needsConfirm: boolean
  breakdown?: string | null
}

export type RunState = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'

export interface PerNodeStatus {
  nodeId: string
  status: string
  rows?: number | null
  ms?: number | null
  label?: string | null
  error?: string | null   // set on the failed step — the error + a fix hint, attributed to its node
}

export interface RunStatus {
  runId: string
  status: RunState
  jobType?: 'run' | 'profile'
  targetNodeId?: string | null
  rowsProcessed: number
  totalRows?: number | null
  ms: number
  placement: Placement
  perNode: PerNodeStatus[]
  progress?: number | null   // 0..1 fraction of steps complete
  stalled?: boolean          // running but no step has completed for a while (a soft "stuck?" hint)
  error?: string | null
  outputUri?: string | null
  outputTable?: string | null
  profile?: ProfileResult | null
  planIdentity?: string | null
}

export interface PlanStep {
  nodeId: string
  kind: string
  mode?: string | null
  previewable: boolean
  label: string
}

export interface CompilePlan {
  targetNodeId?: string | null
  steps: PlanStep[]
  acyclic: boolean
  error?: string | null
}

// A plugin's UI-configurable field, declared in its dataplay.toml [[config]] (see GET /plugins).
export interface PluginConfigField {
  key: string
  type: string  // string | text | int | float | bool | select | password
  label: string
  default?: unknown
  env?: string
  secret?: boolean
  options?: string[]
  help?: string
  placeholder?: string
}
export interface PluginInfo {
  name: string
  source: string
  version?: string
  error?: string
  config?: PluginConfigField[]          // the declared schema (present only if the pack declares one)
  config_values?: Record<string, unknown>  // current non-secret values from settings
  config_set?: string[]                 // keys that have a stored value (incl. secrets — value never sent)
}

export interface ImportStage { name: string; processor: string; mode: string; previewable: boolean }
export interface DriverStep { kind: string; label: string; nodeType?: string | null }
export interface PipelineImport {
  config: string
  params: Record<string, unknown>
  inputColumns: string[]
  outputColumns: string[]
  dataFilter?: string | null
  stages: ImportStage[]
  driverSteps: DriverStep[]
  // a runnable canvas graph the importer decomposed the pipeline into — dropped onto a fresh canvas
  // (via applyAgentGraph) so it runs like any other graph. Same node/edge shape the agent returns.
  graph?: {
    nodes: { id: string; type: string; position: { x: number; y: number }; data: { title?: string; config?: Record<string, unknown> } }[]
    edges: { id: string; source: string; target: string; sourceHandle?: string | null; targetHandle?: string | null; data?: { wire: string } }[]
  }
}
