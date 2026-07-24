// Kernel API DTOs — camelCase on the wire, mirrors kernel/models.py.
import type { ColumnSchema } from './graph'
import type { WireType } from '../theme/tokens'

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

export interface NativeCanvasDiagnostic {
  code: string
  severity: 'error' | 'warning'
  message: string
  path?: string
}

export interface NativeCanvasValidation {
  name: string
  nodeCount: number
  edgeCount: number
  requirements: string[]
  parameters: unknown[]
  diagnostics: NativeCanvasDiagnostic[]
  canImport: boolean
  requiresConfirmation: boolean
  // Issued by validation and bound to the exact envelope. Import must return it so a
  // confirmation cannot be replayed against a file that was never checked.
  validationDigest: string
}

export interface CanvasCopyValidation extends NativeCanvasValidation {
  copyIntentDigest: string
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
  registrationId?: string | null
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
  metadataRevision?: string | null
}

export interface DatasetRevision {
  datasetId: string
  revisionId: string
  committedAt?: string | null
  retentionOwner: 'provider' | 'core'
}

export interface DatasetRevisionPage {
  items: DatasetRevision[]
  nextCursor?: string | null
  hasMore: boolean
}

export interface DatasetRevisionResolution extends DatasetRevision {
  selector: 'latest' | 'as_of' | 'exact'
}

export interface DatasetRevisionCapabilities {
  selectors: Array<'exact' | 'latest' | 'as_of'>
  asOfOrdering?: 'latest_committed_at_at_or_before' | null
  timezone?: 'UTC' | null
  datasetViewSave: boolean
}

export interface DatasetRevisionSummary {
  rowCount?: number | null
  dataFileCount?: number | null
  totalBytes?: number | null
  fragmentCount?: number | null
}

export interface DatasetRevisionPreview {
  columns: ColumnSchema[]
  rows: Record<string, unknown>[]
  hasMore: boolean
  rowLimit: 100
}

export interface DatasetRevisionDetail extends DatasetRevision {
  parentRevisionId?: string | null
  producerOperation?: string | null
  summary: DatasetRevisionSummary
  preview: DatasetRevisionPreview
}

export type DatasetViewSampling =
  | { kind: 'all' }
  | { kind: 'reservoir'; size: number; seed: number }

export interface TemporalWindowV1 {
  timeField: string
  timeDomain: string
  startTick: string
  endTick: string
}

export interface DatasetViewCreateRequest {
  submissionId: string
  name: string
  datasetRef: { kind: 'exact'; datasetId: string; revisionId: string; lastKnown?: { committedAt?: string | null } | null }
  selectedColumns: string[]
  predicate?: string | null
  temporalWindow?: TemporalWindowV1 | null
  sampling: DatasetViewSampling
}

export interface DatasetViewDefinition {
  schemaVersion: 1
  id: string
  creatorId: string
  name: string
  datasetRef: { kind: 'exact'; datasetId: string; revisionId: string; lastKnown?: { committedAt?: string | null } | null }
  placement: { containerId: string; placementId: string; sourceRegistrationId: string }
  selectedColumns: string[]
  predicate?: string | null
  temporalWindow?: TemporalWindowV1 | null
  sampling: DatasetViewSampling
  sampleProvenance?: SampleProvenance | null
  retentionOwner: 'provider' | 'core'
  createdAt: string
  semanticSha256: string
  definitionSha256: string
}

export interface DatasetViewPreview {
  columns: ColumnSchema[]
  rows: Record<string, unknown>[]
  rowCount?: number | null
  hasMore: boolean
  rowLimit: 100
  sampleProvenance?: SampleProvenance | null
}

export interface DistributionReportEstimate {
  schemaVersion: 1
  datasetViewId: string
  viewDefinitionSha256: string
  estimatedScanRows?: number | null
  estimatedScanBytes?: number | null
  selectedColumnCount: number
  needsConfirmation: boolean
  reason?: 'unknown_size' | 'large_scan' | null
  limits: { reportedColumns: number; topCategories: number; histogramBuckets: number; deadlineSeconds: number }
}

export interface DistributionReportTask {
  id: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  progress?: number | null
  error?: string | null
  cancelRequested: boolean
  maxAttempts: number
  attempts: Array<{ id: string; attemptNumber: number; status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'fenced'; progress?: number | null; error?: string | null; startedAt?: string | null; completedAt?: string | null }>
}

export type DistributionReportSection =
  | { kind: 'coverage_schema'; sectionId: string; selectedColumnCount: number; reportedColumnCount: number; columns: ColumnSchema[] }
  | { kind: 'missingness'; sectionId: string; columnName: string; missingCount: number }
  | { kind: 'numeric'; sectionId: string; columnName: string; count: number; nonFiniteCount: number; min?: number | null; max?: number | null; mean?: number | null; stddev?: number | null; quantiles: Array<{ probability: number; value: number }>; histogram: Array<{ bucketId: string; lower: number; upper: number; count: number; upperInclusive: boolean }> }
  | { kind: 'categorical'; sectionId: string; columnName: string; top: Array<{ bucketId: string; label: string | boolean; count: number }>; otherCount: number; distinctCount: number; distinctCountApproximate: boolean }
  | { kind: 'temporal'; sectionId: string; columnName: string; min?: string | null; max?: string | null; buckets: Array<{ bucketId: string; start: string; end: string; count: number; endInclusive: boolean }> }
  | { kind: 'unsupported'; sectionId: string; columnName?: string | null; reason: string; omittedCount?: number | null; partial: boolean }

export interface DistributionReportDocument {
  schemaVersion: 1
  reportId: string
  taskId: string
  datasetViewId: string
  datasetId: string
  revisionId: string
  viewDefinitionSha256: string
  computationVersion: string
  measuredRows: number
  complete: boolean
  sampleProvenance?: SampleProvenance | null
  limitations: string[]
  sections: DistributionReportSection[]
}

export interface DistributionReportEnvelope {
  schemaVersion: 1
  reportId: string
  task: DistributionReportTask
  intent: { submissionId: string; datasetViewId: string; viewDefinitionSha256: string; computationVersion: string; maxAttempts: number }
  viewSnapshot: DatasetViewDefinition
  revisionRetentionOwner: 'core'
  report?: DistributionReportDocument | null
  createdAt: string
  updatedAt: string
  completedAt?: string | null
}

export interface DistributionReportIdentity {
  reportId: string
  datasetViewId: string
  datasetId: string
  revisionId: string
  viewDefinitionSha256: string
  computationVersion: string
  measuredRows: number
  complete: boolean
  samplingIdentity: string
  sampleProvenance?: SampleProvenance | null
}
export type DistributionMetricDelta =
  | { kind: 'numeric'; countDelta: number; nonFiniteCountDelta: number; minDelta?: number | null; maxDelta?: number | null; meanDelta?: number | null; stddevDelta?: number | null; quantiles: Array<{ probability: number; valueDelta?: number | null }>; histogram?: Array<{ leftBucketId: string; rightBucketId: string; lower: number; upper: number; upperInclusive: boolean; countDelta: number }> | null; histogramReason: 'equal_edges' | 'unequal_edges' }
  | { kind: 'categorical'; categories: Array<{ label: string | boolean; leftCount?: number | null; rightCount?: number | null; countDelta?: number | null; reason: 'present_in_both_top_k' | 'outside_left_top_k' | 'outside_right_top_k' }>; otherCountDelta?: number | null; otherCountReason: 'same_top_k' | 'different_top_k'; distinctCountDelta?: number | null; distinctCountReason: 'exact' | 'approximate' }
  | { kind: 'temporal'; buckets?: Array<{ leftBucketId: string; rightBucketId: string; start: string; end: string; endInclusive: boolean; countDelta: number }> | null; bucketReason: 'equal_edges' | 'unequal_edges' }
export interface DistributionReportComparison {
  schemaVersion: 1
  coverage: { left: DistributionReportIdentity; right: DistributionReportIdentity; comparable: boolean; reason: 'compatible_full_coverage' | 'same_deterministic_sample' | 'full_sample_coverage_mismatch' | 'different_deterministic_samples' }
  columns: Array<{ matchReason: 'stable_field_identity' | 'name_and_logical_type'; fieldId?: string | null; leftColumn: ColumnSchema; rightColumn: ColumnSchema; leftSections: DistributionReportSection[]; rightSections: DistributionReportSection[]; comparable: boolean; reason: 'compatible' | 'coverage_mismatch' | 'computation_version_mismatch' | 'logical_type_mismatch' | 'section_kind_mismatch' | 'unsupported_section'; missingCountDelta?: number | null; metricDelta?: DistributionMetricDelta | null }>
  unmatchedLeftColumns: ColumnSchema[]
  unmatchedRightColumns: ColumnSchema[]
}
export interface DistributionReportBucketExamples {
  schemaVersion: 1
  reportId: string
  datasetViewId: string
  datasetId: string
  revisionId: string
  viewDefinitionSha256: string
  computationVersion: string
  samplingIdentity: string
  sampleProvenance?: SampleProvenance | null
  sectionId: string
  bucketId: string
  bucketKind: 'numeric' | 'categorical' | 'temporal'
  columnName: string
  bucketCount: number
  exampleSemantics: 'bounded_examples_from_measured_bucket'
  rowLimit: 100
  returnedRows: number
  truncated: boolean
  rows: Array<Record<string, unknown>>
}

export type SchemaCompatibilityStatus = 'compatible' | 'breaking' | 'unknown'
export interface SchemaFieldCompatibility {
  kind: 'unchanged' | 'renamed' | 'added' | 'removed' | 'changed'
  status: SchemaCompatibilityStatus
  reason: string
  fieldId?: string | null
  oldName?: string | null
  newName?: string | null
}
export interface SchemaCompatibility {
  status: SchemaCompatibilityStatus
  fields: SchemaFieldCompatibility[]
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

export interface CatalogPage {
  items: CatalogTable[]
  total: number
  offset: number
  limit: number
  hasMore: boolean
}
export interface FacetValue { value: string; count: number }
export interface Facets { folders: FacetValue[]; tags: FacetValue[]; owners: FacetValue[]; semanticAvailable?: boolean }
export interface FolderNode { name: string; path: string; tableCount: number }
export interface CatalogFolder { path: string }
export interface CatalogBrowse { prefix: string; folders: FolderNode[]; tables: CatalogTable[] }
export type WorkspaceResourceKind = 'container' | 'canvas' | 'dataset' | 'dataset_view'
export interface WorkspaceLocalPlacementCapability {
  writable: boolean
  canCreateCanvas: boolean
  canMoveCanvas: boolean
  containerId?: string | null
  containerVersion?: number | null
  recoveryState: 'ready' | 'unavailable'
}
export interface WorkspaceResource {
  id: string
  kind: WorkspaceResourceKind
  name: string
  parentId?: string | null
  placementId?: string | null
  version?: number | null
  /** Stable built-in Catalog folder binding when this local container is a folder projection. */
  catalogFolderId?: string | null
  catalogFolderState?: 'current' | 'detached' | null
  catalogFolderPath?: string | null
  detached: boolean
  source: 'local' | 'provider'
  mountId?: string | null
  provider?: string | null
  resourceId?: string | null
  bindingId?: string | null
  referenceState?: 'current' | 'offline' | 'permission_lost' | 'detached' | 'provider_error'
  lastKnown?: boolean
  lastResolvedAt?: string | null
  /** A local Canvas destination paired with this source-only provider resource. */
  localPlacement?: WorkspaceLocalPlacementCapability | null
  /** Provider resources are never mutated by Workspace Canvas actions. */
  providerMutation?: boolean
  /** Local Folder authority is explicit; provider hierarchy never gains it. */
  canCreateFolder?: boolean
  canRenameFolder?: boolean
  canDeleteFolder?: boolean
  folderMutationUnavailableReason?: string | null
}
export interface WorkspaceSourceStatus {
  id: string
  kind: 'local' | 'provider' | 'configuration'
  completeness: 'complete' | 'page' | 'pending' | 'partial' | 'unavailable' | 'unsupported'
  mountId?: string | null
  provider?: string | null
  error?: string | null
  referenceState?: 'current' | 'offline' | 'permission_lost' | 'detached' | 'provider_error' | null
}
export interface WorkspaceBrowsePage {
  container: WorkspaceResource | null
  items: WorkspaceResource[]
  nextCursor?: string | null
  hasMore: boolean
  completeness: 'complete' | 'page' | 'partial'
  sources: WorkspaceSourceStatus[]
}
export interface WorkspaceResourceResolution {
  resource: WorkspaceResource | null
  ancestors: WorkspaceResource[]
  source: WorkspaceSourceStatus
}
export interface WorkspaceProviderRelinkResult {
  ok: boolean
  resource: WorkspaceResource
  previousResource: WorkspaceResource
}
export interface WorkspaceSearchSourceStatus extends WorkspaceSourceStatus {
  freshness: 'current' | 'stale' | 'unknown'
  searchMode: 'native' | 'fallback' | 'unsupported'
}
export interface WorkspaceSearchGroup {
  source: WorkspaceSearchSourceStatus
  items: WorkspaceResource[]
}
export interface WorkspaceSearchPage {
  query: string
  groups: WorkspaceSearchGroup[]
  nextCursor?: string | null
  hasMore: boolean
  completeness: 'complete' | 'page' | 'partial'
}
export interface WorkspaceCreateCanvasResult {
  ok: boolean
  id: string
  created: boolean
  nodeId?: string | null
  resource: WorkspaceResource
}
export interface WorkspaceAddDatasetResult {
  ok: boolean
  id: string
  version: number
}
export interface CatalogUnregisterResult {
  mode: 'best_effort'
  limit: number
  results: { id: string; status: 'unregistered' | 'missing' | 'conflict' | 'failed'; detail?: string | null }[]
}
export interface WorkspaceMoveCanvasResult {
  ok: boolean
  resource: WorkspaceResource
  previousContainer: WorkspaceResource
  container: WorkspaceResource
}
export interface WorkspaceFolderActionResult {
  ok: boolean
  resource: WorkspaceResource
}
export interface CatalogMetadata { folder?: string; tags?: string[]; owner?: string | null; description?: string | null; name?: string | null }
export interface CatalogEdit { expectedRevision: string; folder: string; tags: string[]; owner: string | null; description: string | null; name?: string | null; declaredKey: string[] }
export interface RegisterRequest { uri: string; name?: string; folder?: string; tags?: string[]; owner?: string; description?: string }

export type Cardinality = '1:1' | '1:N' | 'N:1' | 'N:M' | 'unknown'

export interface JoinSuggestion {
  leftColumns: string[]
  rightColumns: string[]
  cardinality: Cardinality
  confidence: 'declared' | 'verified' | 'inferred'
  score: number
  reason: string
  rowReference?: RowReferenceDiagnosis[]
}

export interface RowReferenceInputIdentity {
  kind: 'exact' | 'canonical'
  datasetId: string
  revisionId?: string | null
}

export interface RowReferenceDiagnosis {
  leftInput?: RowReferenceInputIdentity | null
  rightInput?: RowReferenceInputIdentity | null
  leftField: string
  rightField: string
  status: 'compatible' | 'conflict' | 'unknown'
  reason: string
}

export interface JoinAnalysis {
  suggestions: JoinSuggestion[]
  warning?: string | null
  note?: string | null
  configuredRowReference?: RowReferenceDiagnosis[]
  blockingCode?: 'row_reference_target_mismatch' | null
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
export interface LineageEdge { parent: string; child: string; factCount: number }
export interface LineageResult { rootUri: string; nodes: LineageNode[]; edges: LineageEdge[]; truncated?: boolean }

export interface LineageFieldMapping {
  sourceField: string
  destinationField: string
}

export interface LineageFact {
  id: string
  factKey: string
  publicationKey: string
  sourceKey: string
  sourceUri: string
  sourceVersion: string | null
  destinationKey: string
  destinationUri: string
  destinationVersion: string | null
  runId: string | null
  executionManifestSha256?: string | null
  attemptId: string | null
  producer: string | null
  producerVersion: number | null
  stepId: string | null
  provenance: 'run' | 'manual' | 'imported'
  fieldMappings: LineageFieldMapping[]
  createdAt: string
}

export interface LineageFactsPage {
  items: LineageFact[]
  nextAfterId: string | null
  hasMore: boolean
}

export interface SampleResult {
  columns: ColumnSchema[]
  rows: Record<string, unknown>[]
  rowCount?: number | null
  hasMore?: boolean | null
  truncated: boolean
  completeness: 'complete' | 'page' | 'sample' | 'capped' | 'unknown'
  rowLimit?: number | null
  limitReason?: 'preview-scan' | 'interactive-row-budget' | null
  limitScope?: 'each-source' | 'result-window' | null
  sampleProvenance?: SampleProvenance | null
  previewRef?: string | null
  inputManifest?: RunInputManifestItem[] | null
  notPreviewable: boolean
  error?: boolean
  reason?: string | null
  wire: string
}

export interface SampleProvenance {
  strategy: 'prefix' | 'reservoir'
  seed?: number | null
  requestedRows: number
  scannedRows?: number | null
  returnedRows: number
  totalRows?: number | null
  datasetIdentity?: string | null
  datasetRevision?: string | null
  identity: string
  limitations: string[]
}

export interface ColumnProfile {
  name: string
  type: string
  nonNull: number
  nulls: number
  distinct?: number | null
  distinctIsApproximate: boolean
  min?: string | null
  max?: string | null
  mean?: number | null
}

export interface ProfileResult {
  targetPortId?: string | null
  columns: ColumnProfile[]
  rowCount: number
  sampled: boolean
  completeness: 'complete' | 'sample' | 'unknown'
  sampleProvenance?: SampleProvenance | null
  inputManifest?: RunInputManifestItem[] | null
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
  inputSchema: ColumnSchema[]
  outputSchema: ColumnSchema[]
  requirements: string[]
  paramsSchema: Record<string, any>
  previewable: boolean
  blurb: string
  provenance: 'plugin' | 'promoted'
  creatorId?: string | null
  createdAt?: string | null
  semanticDigest?: string | null
}

export type TransformAvailability = 'active' | 'deleted' | 'missing'

export interface TransformRetention {
  canvas: number
  canvasVersion: number
  executionManifest: number
}

export interface TransformLibraryEntry extends ProcessorDescriptor {
  availability: TransformAvailability
  deletedAt?: string | null
  versionCount: number
  retention: TransformRetention
}

export interface TransformLibraryPage {
  items: TransformLibraryEntry[]
  nextCursor?: string | null
  hasMore: boolean
}

export interface TransformLibraryDetail {
  id: string
  provenance: 'plugin' | 'promoted'
  requestedVersion?: string | null
  versions: TransformLibraryEntry[]
}

export interface CanvasTransformReference {
  id: string
  version: string
  nodeIds: string[]
  availability: TransformAvailability
  descriptor?: ProcessorDescriptor | null
}

export type Placement = 'local' | 'distributed'

export interface RunEstimate {
  rows: number | null   // real source-row count; null when size is unknown (no countable source)
  bytes?: number | null // estimated peak data volume — the confirm gate's cost signal
  placement: Placement
  needsConfirm: boolean
  breakdown?: string | null
}

export interface ProfileEstimate extends RunEstimate {
  targetPortId: string
  planDigest: string
  inputManifest?: RunInputManifestItem[] | null
}

export interface ProfileIdentity {
  targetPortId: string
  planDigest: string
  inputManifest?: RunInputManifestItem[] | null
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

export type RunOutputOutcome = 'pending' | 'committed' | 'failed' | 'skipped' | 'cancelled'

export interface RunOutput {
  nodeId: string
  portId: string
  portLabel?: string | null
  wire: WireType
  publicationKind: 'result' | 'catalog'
  outcome: RunOutputOutcome
  uri?: string | null
  table?: string | null
  version?: string | null
  rows?: number | null
  error?: string | null
  sampleProvenance?: SampleProvenance | null
  writeReceipt?: WriteReceipt | null
}

export interface WriteIntent {
  destination: { logicalUri: string; name: string; datasetId?: string | null; provider: 'managed-local-file' | 'managed-local-lance' }
  mode: 'create' | 'replace' | 'append'
  expectedSchema: { name: string; type: string; capabilities?: string[] }[]
  expectedHead?: { kind: 'exact'; datasetId: string; revisionId: string } | null
  idempotencyKey: string
  partitions: { field: string }[]
  provenance: {
    publication: { idempotencyKey: string; runId?: string | null; attemptId?: string | null; producer?: string | null; producerVersion?: number | null; stepId?: string | null; provenance: string }
    parents: string[]
  }
}

export interface WriteReceipt {
  datasetId: string
  revisionId: string
  parentHead?: { kind: 'exact'; datasetId: string; revisionId: string } | null
  head: { datasetId: string; revisionId: string; committedAt?: string | null; retentionOwner: string }
  rows: number
  bytes: number
  schema: { name: string; type: string; capabilities?: string[] }[]
  partitions: { field: string }[]
  publication: { provider: string; logicalUri: string; artifactUri: string; publishSequence: number; idempotencyKey: string; catalogVersion?: string | null; backendVersion?: string | null }
  executionManifestSha256?: string | null
  durable: true
}

export interface WriteAdmission {
  nodeId: string
  managed: boolean
  destination: string
  mode: 'create' | 'replace' | 'overwrite' | 'append'
  provider: string
  expectedSchema: { name: string; type: string; capabilities?: string[] }[]
  partitions: { field: string }[]
  expectedHead?: { kind: 'exact'; datasetId: string; revisionId: string } | null
  intent?: WriteIntent | null
  recoveredReceipt?: WriteReceipt | null
  blocker?: string | null
}

// Certified local add/replace-column work.  The browser deliberately carries only the small,
// three-node graph and lets the API remain the authority for eligibility, coverage, schema, and
// moving-head admission.
export interface MergeColumnRule { source: string; target: string; mode: 'add' | 'replace' }
export interface MergeColumnsRequest {
  graph: { id: string; version: number; requirements: string[]; parameters: unknown[]; nodes: unknown[]; edges: unknown[] }
  submissionId: string
  identityColumns: string[]
  rules: MergeColumnRule[]
}
export interface MergeColumnsCoverageCounts { rows: number; uniqueIdentities: number; nullRows: number; duplicateGroups: number; duplicateRows: number }
export interface MergeColumnsPreflight {
  base: { kind: 'exact'; datasetId: string; revisionId: string }
  declaredKey: string[]
  identityColumns: string[]
  coverage: { base: MergeColumnsCoverageCounts; candidate: MergeColumnsCoverageCounts; matchedIdentities: number; missingIdentities: number; extraIdentities: number; status: string }
  rules: MergeColumnRule[]
  expectedHead: { kind: 'exact'; datasetId: string; revisionId: string }
  outputSchema: ColumnSchema[]
  provenance: { producer: string; source: string; selectKind: string; selectVersion: number }
  eligible: boolean
}
export interface MergeColumnsTaskProjection {
  producerKind: 'sparse-output' | 'managed-sidecar'
  phase: 'validating' | 'merging' | 'candidate_committed' | 'publishing' | 'done' | 'failed' | 'cancelled'
  baseDatasetId: string
  baseRevisionId: string
  candidate: 'pending' | 'committed'
  reused: boolean
  candidateRows?: number | null
  candidateBytes?: number | null
  candidateDigest?: string | null
  canRetry: boolean
  canCancel: boolean
  diagnosticCode?: string | null
}
export interface MergeColumnsTask {
  taskId: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  canRetry: boolean
  canCancel: boolean
  mergeColumns?: MergeColumnsTaskProjection | null
}

// A headless, exact managed-local sidecar merge.  Unlike the Source → Select merge above, the
// sidecar was already published by an upstream run; this request never carries a graph, URI, or
// plugin-provided authority.
export interface ManagedSidecarMergeRequest {
  submissionId: string
  base: { kind: 'exact'; datasetId: string; revisionId: string }
  sidecar: { kind: 'exact'; datasetId: string; revisionId: string }
  expectedHead: { kind: 'exact'; datasetId: string; revisionId: string }
  identityColumns: string[]
  rules: MergeColumnRule[]
}
export interface ManagedSidecarMergePreflight {
  base: { kind: 'exact'; datasetId: string; revisionId: string }
  sidecar: { kind: 'exact'; datasetId: string; revisionId: string }
  expectedHead: { kind: 'exact'; datasetId: string; revisionId: string }
  identityColumns: string[]
  coverage: MergeColumnsPreflight['coverage']
  rules: MergeColumnRule[]
  baseSchema: ColumnSchema[]
  sidecarSchema: ColumnSchema[]
  outputSchema: ColumnSchema[]
  eligible: boolean
}
export interface ManagedSidecarMergeTask extends ManagedSidecarMergePreflight {
  taskId: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  childRevisionId?: string | null
  receipt?: WriteReceipt | null
  diagnosticCode?: string | null
  canRetry: boolean
  canCancel: boolean
  mergeColumns?: MergeColumnsTaskProjection | null
}

// One durable restore of a retained revision as a new head. The API owns moving-head admission;
// the browser previews the source and current head, then submits and polls this owner-scoped task.
export interface RestoreRevisionTask {
  taskId: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  sourceDatasetId: string
  sourceRevisionId: string
  expectedHeadRevisionId: string
  childRevisionId?: string | null
  diagnosticCode?: string | null
  receipt?: WriteReceipt | null
}

// One certified keyed upsert of a payload revision into a managed-local head. The API owns
// eligibility, key validation, schema effects, and moving-head admission; the browser renders only
// what preflight/task provide.
export interface UpsertEvidence {
  matched: number
  inserted: number
  unchanged: number
  rejected: number
  duplicate: number
  conflict: number
}
export interface UpsertRequest {
  submissionId: string
  datasetId: string
  expectedHeadRevisionId: string
  payloadDatasetId: string
  payloadRevisionId: string
  keys: string[]
}
export interface UpsertPreflight {
  base: { kind: 'exact'; datasetId: string; revisionId: string }
  head: { kind: 'exact'; datasetId: string; revisionId: string }
  expectedHead: { kind: 'exact'; datasetId: string; revisionId: string }
  keys: string[]
  outputSchema: ColumnSchema[]
  evidence: UpsertEvidence
  eligible: boolean
}
export interface UpsertTask {
  taskId: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  datasetId: string
  expectedHeadRevisionId: string
  payloadDatasetId: string
  payloadRevisionId: string
  childRevisionId?: string | null
  diagnosticCode?: string | null
  canCancel: boolean
  canRetry: boolean
  receipt?: WriteReceipt | null
  evidence?: UpsertEvidence | null
}

export interface RunInputManifestItem {
  // Run history persists this deliberately minimal dict verbatim, so its inner keys remain snake_case.
  node_id: string
  dataset_id: string
  revision_id: string
  provider: string
  resolved_at: string
}

export interface InputDriftSource {
  nodeId: string
  datasetId: string
  previewRevisionId: string
  latestRevisionId?: string | null
  oldRevisionReadable: boolean
  compatibility?: SchemaCompatibility | null
}

export interface InputDrift {
  drifted: boolean
  sources: InputDriftSource[]
}

export interface RunStatus {
  runId: string
  status: RunState
  jobType: 'run' | 'profile'
  targetNodeId?: string | null
  targetPortId?: string | null
  rowsProcessed: number
  totalRows?: number | null
  ms: number
  placement: Placement
  perNode: PerNodeStatus[]
  progress?: number | null   // 0..1 fraction of steps complete
  stalled?: boolean          // running but no step has completed for a while (a soft "stuck?" hint)
  error?: string | null
  outputs: RunOutput[]
  profile?: ProfileResult | null
  planDigest?: string | null
  profileAttemptOrder?: number | null
  executionManifestSha256?: string | null
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
  package?: string
  source: string
  version?: string
  state?: 'active' | 'inactive' | 'degraded' | 'conflict' | 'failed'
  required?: boolean
  failure_impact?: 'startup-blocking' | 'optional-degradation'
  effective_capabilities?: string[]
  process_placement?: string[]
  failure_summary?: string
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
