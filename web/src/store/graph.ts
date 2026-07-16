import { create } from 'zustand'
import type { WireType } from '../theme/tokens'
import type {
  CanvasDoc, CanvasEdge, CanvasNode, NodeConfig, NodeData, NodeStatus, NodeVersion,
} from '../types/graph'
import type {
  CatalogTable, KernelInfo, ProcessorDescriptor, ProfileResult, RunEstimate, RunStatus, SampleResult,
} from '../types/api'
import { getSpec, nodeOutputs } from '../nodes/registry'
import { registerGenericNodes, nodeInvalidReason } from '../nodes/generic'
import type { SchemaMap } from '../nodes/schema'
import { parseHash } from '../router'
import { exampleDoc } from '../examples'
import {
  api, KernelError, setApiUser,
  type AgentBackendNode, type AgentBackendEdge, type CanvasFile, type CanvasRole, type DpUser,
} from '../api/client'
import { crdtUndo, crdtUndoActive, collabApply } from '../collab/undo'

export type PanelKind = 'data' | 'run' | 'history' | 'lineage' | 'section'

export type CanvasPersistence = 'remote' | 'local-draft'

export type CanvasCreationResult =
  | { ok: true; canvasId: string; persistence: CanvasPersistence }
  | { ok: false }

const LS_KEY = 'dp-canvas'       // offline cache of the open doc
const USER_KEY = 'dp-user'       // last-selected user id
const OPEN_KEY = (uid: string) => `dp-open-${uid}`  // last-opened file per user
const ROLE_KEY = (userId: string, canvasId: string) => `dp-canvas-role-${encodeURIComponent(userId)}-${encodeURIComponent(canvasId)}`

export function roleCanEdit(role: CanvasRole | null | undefined): role is 'owner' | 'editor' {
  return role === 'owner' || role === 'editor'
}

function cachedRole(userId: string | null | undefined, canvasId: string): CanvasRole | null {
  if (!userId) return null
  try {
    const value = localStorage.getItem(ROLE_KEY(userId, canvasId))
    return value === 'owner' || value === 'editor' || value === 'viewer' ? value : null
  } catch {
    return null
  }
}

function rememberRole(userId: string | null | undefined, canvasId: string, role: CanvasRole | null | undefined): void {
  if (!userId) return
  try {
    if (role) localStorage.setItem(ROLE_KEY(userId, canvasId), role)
    else localStorage.removeItem(ROLE_KEY(userId, canvasId))
  } catch { /* storage unavailable */ }
}

let _seq = 0
let _cfgEdit = { id: '', t: 0 } // coalesces param-edit undo checkpoints
let _extEditTimer: ReturnType<typeof setTimeout> | null = null // debounces external-edit refetches
let _fileNavigationGeneration = 0 // latest file-open/new navigation wins across async requests
let _fileListGeneration = 0       // stale same-user list responses cannot overwrite a newer refresh
let _previewRequestGeneration = 0 // every preview captures its own generation; latest request for a node wins
let _profileRequestGeneration = 0 // whole-dataset profile jobs use the same latest-wins rule as previews
let _reattachRunsGeneration = 0   // same-canvas reloads also need latest-navigation-wins recovery

/** A canvas position near `base` that doesn't overlap any existing node (so added nodes never stack). */
export function freePosition(nodes: CanvasNode[], base: { x: number; y: number }): { x: number; y: number } {
  const W = 280, H = 180
  const clash = (x: number, y: number) => nodes.some((n) => Math.abs(n.position.x - x) < W && Math.abs(n.position.y - y) < H)
  if (!clash(base.x, base.y)) return base
  const dirs = [[1, 0], [0, 1], [1, 1], [-1, 0], [-1, 1], [0, -1], [1, -1], [-1, -1]]
  for (let r = 1; r < 50; r++) {
    for (const [dx, dy] of dirs) {
      const x = base.x + dx * W * r * 0.75, y = base.y + dy * H * r * 0.9
      if (!clash(x, y)) return { x, y }
    }
  }
  return base
}

/** Whether a node can run/preview: it (or some ancestor) is a source with a configured uri —
 * AND nothing in its upstream chain (including itself) is disabled (disable turns off downstream). */
export function nodeRunnable(doc: CanvasDoc, id: string): boolean {
  if (isDisabled(doc, id)) return false
  const seen = new Set<string>()
  const walk = (nid: string): boolean => {
    if (seen.has(nid)) return false
    seen.add(nid)
    const n = doc.nodes.find((x) => x.id === nid)
    if (!n) return false
    if (n.type === 'source') return !!n.data.config.uri
    return doc.edges.filter((e) => e.target === nid).map((e) => e.source).some(walk)
  }
  return walk(id)
}

/** A node is disabled if it, or ANY of its upstream ancestors, is flagged disabled — disabling a
 * node turns off everything downstream of it (the whole branch stops), mirroring ComfyUI. */
export function isDisabled(doc: CanvasDoc, id: string): boolean {
  const seen = new Set<string>()
  const walk = (nid: string): boolean => {
    if (seen.has(nid)) return false
    seen.add(nid)
    const n = doc.nodes.find((x) => x.id === nid)
    if (!n) return false
    if (n.data.disabled) return true
    return doc.edges.filter((e) => e.target === nid).map((e) => e.source).some(walk)
  }
  return walk(id)
}

export function newId(kind: string): string {
  _seq += 1
  return `${kind}-${_seq}-${Math.floor(performance.now() % 100000)}`
}

// In-app clipboard for copy/paste of a node selection — lives in the module so it works across canvases
// in the same tab (the system clipboard can't hold graph structure).
let _clipboard: { nodes: CanvasNode[]; edges: CanvasEdge[] } | null = null
const _clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x))

// Clone a set of nodes + the edges wholly inside that set, remapping every id and offsetting position,
// so a paste/duplicate lands a self-contained copy that never collides with the originals.
function cloneSubgraph(nodes: CanvasNode[], edges: CanvasEdge[], dx = 40, dy = 40): { nodes: CanvasNode[]; edges: CanvasEdge[] } {
  const idMap = new Map<string, string>()
  for (const n of nodes) idMap.set(n.id, newId(n.type))
  const clones = nodes.map((n) => ({
    ..._clone(n),
    id: idMap.get(n.id)!,
    parentId: n.parentId && idMap.has(n.parentId) ? idMap.get(n.parentId)! : null, // keep containment only if the parent came too
    position: { x: n.position.x + dx, y: n.position.y + dy },
    data: { ..._clone(n.data), status: 'draft' as const, history: [] },
  }))
  const clonedEdges = edges
    .filter((e) => idMap.has(e.source) && idMap.has(e.target))
    .map((e) => ({ ..._clone(e), id: `e-${idMap.get(e.source)}-${idMap.get(e.target)}-${Math.floor(performance.now() % 100000)}`, source: idMap.get(e.source)!, target: idMap.get(e.target)! }))
  return { nodes: clones, edges: clonedEdges }
}

// Merge tables into the bounded working-set catalog (dedupe by uri; the fetched copy wins so an edit
// elsewhere refreshes here). Never grows unbounded in practice — it holds canvas refs + recent lookups.
function mergeIntoCatalog(set: (fn: (s: Store) => Partial<Store>) => void, tables: CatalogTable[]): void {
  if (!tables.length) return
  set((s) => {
    const byUri = new Map(s.catalog.map((t) => [t.uri, t]))
    for (const t of tables) byUri.set(t.uri, t)
    return { catalog: Array.from(byUri.values()) }
  })
}

export interface PreviewState {
  canvasId: string
  nodeId: string
  portId?: string
  planIdentity: string
  requestGeneration: number
  loading?: boolean
  result?: SampleResult
  error?: string
  offset?: number
}

function canonicalIdentityValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalIdentityValue)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().flatMap((key) => {
      const normalized = canonicalIdentityValue((value as Record<string, unknown>)[key])
      return normalized === undefined ? [] : [[key, normalized]]
    }))
  }
  return value
}

function compareIdentityText(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}

// Preview and profile requests execute the same target-scoped graph cone on the server. Keep one
// canonical document identity for every client-side consumer: unrelated branches, array ordering,
// positions, edge ids, selection, and transient node status are presentation-only; requirements,
// executable node data, wiring, and section containment affect execution. Titles are included because
// metric output and section-child aliases execute from them.
function targetExecutionPlanIdentity(doc: CanvasDoc, nodeId: string, portId?: string): string {
  const executableNodes = doc.nodes.filter((node) => node.type !== 'note' && node.type !== 'code')
  const byId = new Map(executableNodes.map((node) => [node.id, node]))
  const incoming = new Map<string, string[]>()
  const children = new Map<string, string[]>()
  for (const edge of doc.edges) {
    if (!byId.has(edge.source) || !byId.has(edge.target)) continue
    incoming.set(edge.target, [...(incoming.get(edge.target) ?? []), edge.source])
  }
  for (const node of executableNodes) {
    if (node.parentId && byId.has(node.parentId)) {
      children.set(node.parentId, [...(children.get(node.parentId) ?? []), node.id])
    }
  }

  const nodeIds = new Set<string>()
  const upstream = byId.has(nodeId) ? [nodeId] : []
  while (upstream.length) {
    const current = upstream.pop()!
    if (nodeIds.has(current)) continue
    nodeIds.add(current)
    upstream.push(...(incoming.get(current) ?? []))
  }
  const contained = [...nodeIds]
    .filter((id) => byId.get(id)?.type === 'section')
    .flatMap((id) => children.get(id) ?? [])
  const seenContained = new Set<string>()
  while (contained.length) {
    const current = contained.pop()!
    if (seenContained.has(current)) continue
    seenContained.add(current)
    nodeIds.add(current)
    contained.push(...(children.get(current) ?? []))
  }

  const nodes = [...nodeIds].map((id) => byId.get(id)!).sort((a, b) => compareIdentityText(a.id, b.id))
    .map((node) => ({
      id: node.id,
      type: node.type,
      parentId: node.parentId ?? null,
      title: node.data.title,
      config: node.data.config,
      bypassed: !!node.data.bypassed,
      disabled: !!node.data.disabled,
    }))
  const edges = doc.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target))
    .map((edge) => ({
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle ?? null,
      targetHandle: edge.targetHandle ?? null,
      wire: edge.data?.wire ?? 'dataset',
    }))
    .sort((a, b) => compareIdentityText(
      [a.source, a.target, a.sourceHandle ?? '', a.targetHandle ?? '', a.wire].join('\u0000'),
      [b.source, b.target, b.sourceHandle ?? '', b.targetHandle ?? '', b.wire].join('\u0000'),
    ))

  return JSON.stringify(canonicalIdentityValue({
    schema: 1,
    canvasId: doc.id,
    targetNodeId: nodeId,
    targetPortId: portId,
    requirements: [...(doc.requirements ?? [])].sort(),
    nodes,
    edges,
  }))
}

export function previewPlanIdentity(doc: CanvasDoc, nodeId: string, portId?: string): string {
  return targetExecutionPlanIdentity(doc, nodeId, portId)
}

export function profilePlanIdentity(doc: CanvasDoc, nodeId: string): string {
  return targetExecutionPlanIdentity(doc, nodeId)
}

export function previewIsCurrent(preview: PreviewState, doc: CanvasDoc, nodeId: string, portId = preview.portId): boolean {
  return preview.canvasId === doc.id
    && preview.nodeId === nodeId
    && preview.portId === portId
    && doc.nodes.some((node) => node.id === nodeId)
    && preview.planIdentity === previewPlanIdentity(doc, nodeId, portId)
}

// Schema hints and editor completions must follow the same reuse rule as the data panel. The
// consumer of this map additionally matches its source handle, so a current preview for one named
// output cannot supply columns for a sibling port.
export function currentPreviews(doc: CanvasDoc, previews: Record<string, PreviewState>): Record<string, PreviewState> {
  return Object.fromEntries(Object.entries(previews).filter(([nodeId, preview]) => {
    const node = doc.nodes.find((candidate) => candidate.id === nodeId)
    return !!node && previewIsCurrent(preview, doc, nodeId)
  }))
}
interface RunState {
  estimate?: RunEstimate
  status?: RunStatus
  phase: 'idle' | 'estimating' | 'estimated' | 'confirm' | 'running' | 'done' | 'failed'
  error?: string
}

export interface ProfileJobState {
  canvasId: string
  nodeId: string
  // Every lifecycle request for this job is fenced to the user that created or recovered it.
  // A user transition stops polling/cancellation instead of replaying a run id under another session.
  principalId?: string
  // Authority is captured for this canvas when the job is started/recovered. Detached cleanup must
  // never consult the role of whichever canvas happens to be open later.
  canCancel?: boolean
  // Raw identity remains local for synchronous stale-result checks. The server-minted SHA-256 is the
  // durable authority used to bind a recovered result to the graph and source content it profiled.
  planIdentity: string
  planDigest?: string
  // A submission id survives an ambiguous POST response so every automatic or explicit retry adopts
  // the same server-side job. It is replaced only by a new preflight/start intent.
  submissionId?: string
  submissionUnresolved?: boolean
  cancelRequested?: boolean
  // Recovered profile payloads are fail-closed until the server confirms their current source digest.
  // `false` means `status` is deliberately sanitized and exists only for identity/cancellation.
  identityVerified?: boolean
  requestGeneration: number
  phase: 'idle' | 'estimating' | 'preflight' | 'verifying' | 'queued' | 'running' | 'cancelling' | 'done' | 'failed' | 'cancelled'
  estimate?: RunEstimate
  status?: RunStatus
  error?: string
}

export function profileJobIsCurrent(job: ProfileJobState, doc: CanvasDoc, nodeId: string): boolean {
  return job.canvasId === doc.id
    && job.nodeId === nodeId
    && doc.nodes.some((node) => node.id === nodeId)
    && job.planIdentity === profilePlanIdentity(doc, nodeId)
}

function profilePhase(status: RunStatus): ProfileJobState['phase'] {
  return status.status === 'done' ? 'done'
    : status.status === 'failed' ? 'failed'
      : status.status === 'cancelled' ? 'cancelled'
        : status.status === 'queued' ? 'queued' : 'running'
}

function profileStatusRank(status: RunStatus['status']): number {
  return status === 'queued' ? 0 : status === 'running' ? 1 : 2
}

function sameProfileAttempt(existing: RunStatus, incoming: RunStatus): boolean {
  return existing.jobType === 'profile' && incoming.jobType === 'profile'
    && existing.runId === incoming.runId
    && existing.targetNodeId === incoming.targetNodeId
    && !!existing.planDigest && existing.planDigest === incoming.planDigest
    && Number.isSafeInteger(existing.profileAttemptOrder)
    && existing.profileAttemptOrder === incoming.profileAttemptOrder
}

function profileStatusCanAdvance(existing: RunStatus, incoming: RunStatus): boolean {
  if (!sameProfileAttempt(existing, incoming)) return false
  const existingRank = profileStatusRank(existing.status)
  const incomingRank = profileStatusRank(incoming.status)
  if (incomingRank < existingRank) return false
  return !(existingRank === 2 && incomingRank === 2 && existing.status !== incoming.status)
}

const PROFILE_RETRY_DELAYS_MS = [100, 300]
let _profileSubmissionUserId: string | null = null

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function retryableProfileRequest(error: unknown): boolean {
  return !(error instanceof KernelError) || error.status === 429 || error.status >= 500
}

async function submitFullProfile(
  doc: CanvasDoc,
  nodeId: string,
  planDigest: string,
  submissionId: string,
  userId: string,
): Promise<RunStatus> {
  for (let attempt = 0; ; attempt += 1) {
    if (_profileSubmissionUserId !== userId) {
      throw new KernelError(401, 'User changed while the full profile was being submitted')
    }
    try {
      return await api.fullProfile(doc, nodeId, planDigest, submissionId, true)
    } catch (error) {
      if (!retryableProfileRequest(error) || attempt >= PROFILE_RETRY_DELAYS_MS.length) throw error
      await wait(PROFILE_RETRY_DELAYS_MS[attempt])
    }
  }
}

function sanitizeUnverifiedProfileStatus(status: RunStatus): RunStatus {
  // Keep only the durable attempt identity and lifecycle needed to track/cancel the job. All measured
  // data (including per-node rows/errors) stays hidden until the current source digest is verified.
  return {
    ...status,
    rowsProcessed: 0,
    totalRows: undefined,
    ms: 0,
    perNode: [],
    progress: undefined,
    stalled: undefined,
    error: undefined,
    profile: undefined,
    outputs: [],
  }
}

interface PendingProfileSubmission {
  doc: CanvasDoc
  nodeId: string
  planDigest: string
  submissionId: string
  userId: string
  canCancel: boolean
  cancelRequested: boolean
  reconciling: boolean
}

const _pendingProfileSubmissions = new Map<string, PendingProfileSubmission>()
const PROFILE_ORPHAN_RETRY_DELAYS_MS = [100, 300, 1_000, 3_000, 5_000]

interface ProfileAttemptIdentity {
  targetNodeId: string | null
  planDigest: string
  attemptOrder: number
}

interface DetachedProfileCancellation {
  runId: string
  userId: string
  identity?: ProfileAttemptIdentity
  reconcileTerminal?: (status: RunStatus) => void
}

const _detachedProfileCancellations = new Map<string, DetachedProfileCancellation>()

function validProfileSubmissionStatus(
  status: RunStatus,
  nodeId: string,
  planDigest: string,
): boolean {
  return status.jobType === 'profile'
    && status.targetNodeId === nodeId
    && status.planDigest === planDigest
    && Number.isSafeInteger(status.profileAttemptOrder)
    && status.profileAttemptOrder! > 0
}

function profileAttemptIdentity(status: RunStatus): ProfileAttemptIdentity | undefined {
  return status.jobType === 'profile'
      && typeof status.planDigest === 'string'
      && Number.isSafeInteger(status.profileAttemptOrder)
    ? {
        targetNodeId: status.targetNodeId ?? null,
        planDigest: status.planDigest,
        attemptOrder: status.profileAttemptOrder!,
      }
    : undefined
}

function exactDetachedStatus(
  entry: DetachedProfileCancellation,
  value: unknown,
): value is RunStatus {
  if (!value || typeof value !== 'object') return false
  const status = value as Partial<RunStatus>
  if (status.runId !== entry.runId
      || !['queued', 'running', 'done', 'failed', 'cancelled'].includes(status.status ?? '')) return false
  if (status.status === 'queued' || status.status === 'running') {
    if (status.jobType !== 'profile') return false
    if (entry.identity) {
      return (status.targetNodeId ?? null) === entry.identity.targetNodeId
        && status.planDigest === entry.identity.planDigest
        && status.profileAttemptOrder === entry.identity.attemptOrder
    }
  }
  // A compact terminal fence can intentionally identify only runId + lifecycle after detail pruning.
  if (status.jobType === 'profile' && entry.identity) {
    return (status.targetNodeId ?? null) === entry.identity.targetNodeId
      && status.planDigest === entry.identity.planDigest
      && status.profileAttemptOrder === entry.identity.attemptOrder
  }
  return true
}

function terminalRunStatus(status: RunStatus): boolean {
  return status.status === 'done' || status.status === 'failed' || status.status === 'cancelled'
}

function exactProfileTerminal(expected: RunStatus, observed: RunStatus): boolean {
  return observed.runId === expected.runId && terminalRunStatus(observed)
    && (observed.jobType !== 'profile' || sameProfileAttempt(expected, observed))
}

function superviseDetachedProfileCancellation(
  status: RunStatus,
  userId: string | null | undefined,
  canCancel: boolean,
  reconcileTerminal?: (status: RunStatus) => void,
): void {
  if (!userId || _profileSubmissionUserId !== userId
      || (status.status !== 'queued' && status.status !== 'running') || !status.runId) return
  if (!canCancel) {
    // A read-only recovery can observe an active run but must never turn it into a mutation loop.
    const existing = _detachedProfileCancellations.get(status.runId)
    if (existing?.userId === userId) _detachedProfileCancellations.delete(status.runId)
    return
  }
  const existing = _detachedProfileCancellations.get(status.runId)
  if (existing) {
    // A run is owned by the principal that first detached it. Never replay its id under another user.
    if (existing.userId !== userId) return
    if (!existing.identity) existing.identity = profileAttemptIdentity(status)
    if (reconcileTerminal) existing.reconcileTerminal = reconcileTerminal
    return
  }
  const entry: DetachedProfileCancellation = {
    runId: status.runId,
    userId,
    identity: profileAttemptIdentity(status),
    reconcileTerminal,
  }
  _detachedProfileCancellations.set(entry.runId, entry)
  void (async () => {
    let failures = 0
    while (_detachedProfileCancellations.get(entry.runId) === entry) {
      if (_profileSubmissionUserId !== entry.userId) {
        _detachedProfileCancellations.delete(entry.runId)
        return
      }
      let observed: RunStatus | undefined
      try {
        const cancelled = await api.cancelRun(entry.runId)
        if (exactDetachedStatus(entry, cancelled)) observed = cancelled
      } catch (error) {
        if (!retryableProfileRequest(error)) {
          _detachedProfileCancellations.delete(entry.runId)
          return
        }
        // Ambiguous transport/server failures are reconciled from the run endpoint below.
      }
      if (observed && terminalRunStatus(observed)) {
        _detachedProfileCancellations.delete(entry.runId)
        try { entry.reconcileTerminal?.(observed) } catch { /* reconciliation is best-effort */ }
        return
      }
      if (_profileSubmissionUserId !== entry.userId) {
        _detachedProfileCancellations.delete(entry.runId)
        return
      }
      try {
        const current = await api.runStatus(entry.runId)
        if (exactDetachedStatus(entry, current)) observed = current
      } catch (error) {
        if (!retryableProfileRequest(error)) {
          _detachedProfileCancellations.delete(entry.runId)
          return
        }
        // Retry ambiguous status failures with bounded backoff.
      }
      if (observed && terminalRunStatus(observed)) {
        _detachedProfileCancellations.delete(entry.runId)
        try { entry.reconcileTerminal?.(observed) } catch { /* reconciliation is best-effort */ }
        return
      }
      const delay = PROFILE_ORPHAN_RETRY_DELAYS_MS[
        Math.min(failures, PROFILE_ORPHAN_RETRY_DELAYS_MS.length - 1)
      ]
      failures += 1
      await wait(delay)
    }
  })()
}

function forgetProfileSubmission(entry: PendingProfileSubmission): void {
  if (_pendingProfileSubmissions.get(entry.submissionId) === entry) {
    _pendingProfileSubmissions.delete(entry.submissionId)
  }
}

function reconcileAndCancelProfileSubmission(entry: PendingProfileSubmission): void {
  entry.cancelRequested = true
  if (entry.reconciling) return
  entry.reconciling = true
  void (async () => {
    let failures = 0
    while (_pendingProfileSubmissions.get(entry.submissionId) === entry) {
      if (_profileSubmissionUserId !== entry.userId) {
        forgetProfileSubmission(entry)
        return
      }
      let status: RunStatus
      try {
        status = await api.fullProfile(
          entry.doc, entry.nodeId, entry.planDigest, entry.submissionId, true,
        )
      } catch (error) {
        if (!retryableProfileRequest(error)) {
          forgetProfileSubmission(entry)
          return
        }
        const delay = PROFILE_ORPHAN_RETRY_DELAYS_MS[
          Math.min(failures, PROFILE_ORPHAN_RETRY_DELAYS_MS.length - 1)
        ]
        failures += 1
        await wait(delay)
        continue
      }
      if (!validProfileSubmissionStatus(status, entry.nodeId, entry.planDigest)) {
        // Never cancel an unrelated ordinary run. A malformed profile response that identifies itself
        // as a profile is supervised by exact run id until its terminal lifecycle is observed.
        if (status.jobType === 'profile' && status.runId
            && (status.status === 'queued' || status.status === 'running')) {
          superviseDetachedProfileCancellation(status, entry.userId, entry.canCancel)
        }
        forgetProfileSubmission(entry)
        return
      }
      if (terminalRunStatus(status)) {
        forgetProfileSubmission(entry)
        return
      }
      superviseDetachedProfileCancellation(status, entry.userId, entry.canCancel)
      forgetProfileSubmission(entry)
      return
    }
  })()
}

function cancelDetachedProfileJob(job: ProfileJobState | undefined): void {
  if (!job) return
  if (job.status && (job.status.status === 'queued' || job.status.status === 'running')) {
    superviseDetachedProfileCancellation(job.status, job.principalId, job.canCancel === true)
  }
  if (job.submissionId && !job.status) {
    const pending = _pendingProfileSubmissions.get(job.submissionId)
    if (pending) reconcileAndCancelProfileSubmission(pending)
  }
}

export interface AgentMsg { role: 'user' | 'agent'; text: string; plan?: string[] }

interface Store {
  doc: CanvasDoc
  canvasRole: CanvasRole | null     // authoritative role for the open canvas; null fails closed
  kernelInfo: KernelInfo | null
  kernelUp: boolean
  accessDenied: boolean  // server rejected the save with 401/403 (session/access changed) — NOT offline
  catalog: CatalogTable[]
  processors: ProcessorDescriptor[]
  specsVersion: number
  schemas: SchemaMap               // per-node, per-output-port columns; null port entry = untyped
  sizes: Record<string, { rows: number | null; confidence: string }>  // per-node size estimate (card hint)

  selectedId: string | null        // primary selection (drives panels)
  selectedIds: string[]            // full multi-selection (box/shift-select)
  openPanels: Record<string, PanelKind>
  previews: Record<string, PreviewState>
  runs: Record<string, RunState>
  profileJobs: Record<string, ProfileJobState>
  past: CanvasDoc[]
  future: CanvasDoc[]
  saved: boolean          // auto-save state (localStorage), shown subtly in the top bar

  agentOpen: boolean
  agentLog: AgentMsg[]

  // -- graph mutation --
  setNodes: (nodes: CanvasNode[]) => void
  setEdges: (edges: CanvasEdge[]) => void
  addNode: (kind: string, position: { x: number; y: number }, config?: Partial<NodeConfig>, title?: string) => CanvasNode | null
  setParent: (id: string, parentId: string | null, position: { x: number; y: number }) => void
  updateConfig: (id: string, patch: Partial<NodeConfig>) => void
  updateData: (id: string, patch: Partial<NodeData>) => void
  removeNode: (id: string) => void
  connect: (edge: CanvasEdge) => void
  removeEdge: (id: string) => void
  select: (id: string | null) => void
  setSelection: (ids: string[]) => void
  selectAll: () => void
  removeSelected: () => void
  copySelection: () => void
  cutSelection: () => void
  paste: () => void
  duplicateSelected: () => void

  bypass: (id: string) => void
  disable: (id: string) => void
  rename: (id: string, title: string) => void
  duplicate: (id: string) => void

  commit: () => void
  undo: () => void
  redo: () => void

  togglePanel: (id: string, kind: PanelKind) => void
  openPanel: (id: string, kind: PanelKind) => void
  closePanel: (id: string) => void

  // -- execution --
  runPreview: (id: string, offset?: number, portId?: string) => Promise<void>
  requestRun: (id: string) => Promise<void>
  estimate: (id: string) => Promise<void>
  run: (id: string, confirmed?: boolean) => Promise<void>
  rerunAll: () => void
  cancelRun: (id: string) => Promise<void>
  clearRun: (id: string) => void
  prepareFullProfile: (id: string) => Promise<void>
  startFullProfile: (id: string) => Promise<void>
  cancelFullProfile: (id: string) => Promise<void>
  promote: (id: string) => Promise<void>
  restoreVersion: (id: string, versionId: string) => void

  // -- kernel + catalog --
  // `catalog` is a bounded WORKING SET — the tables referenced by the open canvas + recently
  // fetched/searched ones — NOT the whole catalog (which can be thousands of tables and is browsed
  // server-side, paginated, in the Tables view). It exists so canvas source nodes can resolve their
  // columns and pickers have a warm cache; it is never assumed to be complete.
  bootstrap: () => Promise<void>
  refreshCatalog: () => Promise<void>
  // ensure the tables a canvas's source nodes point at are in the working set (fetched on demand)
  ensureCanvasTables: (doc: CanvasDoc, opts?: { force?: boolean }) => Promise<void>
  // remember tables the user just picked/searched so the canvas + pickers resolve them from the cache
  rememberTables: (tables: CatalogTable[]) => void
  refreshSchemas: () => Promise<void>
  // upload a dataset file → shared storage + catalog; returns the new table (null on failure/offline)
  uploadDataset: (file: File) => Promise<CatalogTable | null>

  // -- agent --
  setAgentOpen: (v: boolean) => void
  pushAgent: (m: AgentMsg) => void

  // -- persistence --
  save: () => Promise<void>
  loadDoc: (doc: CanvasDoc, role?: CanvasRole | null) => void
  applyExternalEdit: (canvasId?: string) => void
  // `targetCanvasId` binds a destructive replacement to the canvas the caller created. Imports use
  // it so a late response can never replace whichever canvas became active in the meantime.
  applyAgentGraph: (graph: { nodes: AgentBackendNode[]; edges: AgentBackendEdge[] }, targetCanvasId?: string) => boolean

  // -- app shell (Figma-style views) --
  view: DpView
  setView: (v: DpView) => void
  erFocusUri: string | null                       // the table the relationship graph opens focused on (null = global)
  openRelationships: (uri: string | null) => void
  // drop a catalog dataset / library transform onto the open canvas and navigate to it (Tables/Transforms)
  addToCanvas: (kind: string, config: Partial<NodeConfig>, title?: string) => void
  // a full-viewport Monaco editor for one node's code param (opened from the Inspector)
  fullscreenCode: { nodeId: string; param: string; lang?: string } | null
  openCodeFullscreen: (nodeId: string, param: string, lang?: string) => void
  closeCodeFullscreen: () => void
  // transient notifications surfaced as toasts (errors/info) — so failures aren't silent
  toasts: { id: string; kind: 'error' | 'info' | 'success'; msg: string }[]
  pushToast: (msg: string, kind?: 'error' | 'info' | 'success') => void
  dismissToast: (id: string) => void
  // realtime collaboration presence: other people currently on this canvas (live cursors + avatars)
  peers: Record<string, { name: string; color: string; cursor?: { x: number; y: number } }>
  setPeer: (id: string, p: { name: string; color: string; cursor?: { x: number; y: number } }) => void
  dropPeer: (id: string) => void
  clearPeers: () => void

  // -- users + files (per-user, multi-file) --
  authEnabled: boolean            // whether a real login/session is in force (→ show Log out)
  setAuthEnabled: (v: boolean) => void
  currentUser: DpUser | null
  users: DpUser[]
  files: CanvasFile[]
  refreshFiles: () => Promise<boolean>  // true only when this user's authoritative list was refreshed
  refreshUsers: () => Promise<void>
  openFile: (id: string) => Promise<boolean>
  newFile: (options?: { signal?: AbortSignal }) => Promise<CanvasCreationResult>
  newFromExample: (key: string) => Promise<CanvasCreationResult>
  renameFile: (name: string) => void
  setRequirements: (reqs: string[]) => void
  deleteFile: (id: string) => Promise<void>
}

// Top-level views (like Figma's Recents / Design surfaces). 'canvas' is the editor; settings is a modal.
export type DpView = 'canvas' | 'files' | 'tables' | 'transforms' | 'relationships'

function emptyDoc(): CanvasDoc {
  // a random suffix keeps ids unique — performance.now() resets per page load, so a bare timestamp can
  // collide across freshly-loaded tabs/tests and leak one canvas's runs/history into another
  return { id: `canvas_${Math.floor(performance.now())}_${Math.random().toString(36).slice(2, 8)}`, name: 'untitled', version: 1, nodes: [], edges: [] }
}

// true if the node, or anything feeding it, has an unmet required param — so running the pipeline
// through it would fail. Keeps rerun-all consistent with the disabled ▶ on the cards.
function hasInvalidUpstream(doc: CanvasDoc, id: string): boolean {
  const seen = new Set<string>()
  const walk = (nid: string): boolean => {
    if (seen.has(nid)) return false
    seen.add(nid)
    const n = doc.nodes.find((x) => x.id === nid)
    if (!n) return false
    if (nodeInvalidReason(n)) return true
    return doc.edges.filter((e) => e.target === nid).map((e) => e.source).some(walk)
  }
  return walk(id)
}

// downstream node ids (BFS over edges)
function downstream(doc: CanvasDoc, id: string): Set<string> {
  const out = new Set<string>()
  const q = [id]
  while (q.length) {
    const cur = q.shift()!
    for (const e of doc.edges) {
      if (e.source === cur && !out.has(e.target)) {
        out.add(e.target)
        q.push(e.target)
      }
    }
  }
  return out
}

async function superviseTrackedProfileCancellation(
  get: () => Store,
  set: (partial: Partial<Store> | ((state: Store) => Partial<Store>)) => void,
  nodeId: string,
  requestGeneration: number,
  runId: string,
  principalId: string,
  observedTerminal?: RunStatus,
): Promise<void> {
  const current = get().profileJobs[nodeId]
  if (_profileSubmissionUserId !== principalId || get().currentUser?.id !== principalId
      || current?.principalId !== principalId || current.canCancel !== true
      || current.requestGeneration !== requestGeneration || current.status?.runId !== runId
      || current.cancelRequested !== true
      || (current.status.status !== 'queued' && current.status.status !== 'running')) return
  const expectedStatus = current.status
  const expectedSubmissionId = current.submissionId
  const expectedCanvasId = current.canvasId
  const boundTrackedJob = (state: Store): ProfileJobState | undefined => {
    const tracked = state.profileJobs[nodeId]
    return _profileSubmissionUserId === principalId && state.currentUser?.id === principalId
        && tracked?.principalId === principalId && tracked.canCancel === true
        && tracked.canvasId === expectedCanvasId && tracked.nodeId === nodeId
        && tracked.requestGeneration === requestGeneration
        && tracked.submissionId === expectedSubmissionId && tracked.status?.runId === runId
        && tracked.cancelRequested === true
      ? tracked
      : undefined
  }
  const stopTrackedPoll = () => {
    const polling = _profilePolling.get(runId)
    if (polling?.requestGeneration === requestGeneration && polling.principalId === principalId) {
      _profilePolling.delete(runId)
    }
  }
  const installExactTerminal = (terminal: RunStatus) => {
    if (!terminalRunStatus(terminal) || !sameProfileAttempt(expectedStatus, terminal)) return
    set((state) => {
      const tracked = boundTrackedJob(state)
      if (!tracked || !sameProfileAttempt(expectedStatus, tracked.status!)
          || !profileStatusCanAdvance(tracked.status!, terminal)) return {}
      const identityVerified = tracked.identityVerified !== false
      const status = identityVerified ? terminal : sanitizeUnverifiedProfileStatus(terminal)
      const phase = identityVerified
        ? profilePhase(status)
        : status.status === 'cancelled' ? 'cancelled' : 'failed'
      return { profileJobs: { ...state.profileJobs, [nodeId]: {
        ...tracked, status, phase,
        error: !identityVerified && phase !== 'cancelled'
          ? tracked.error
          : status.error ?? undefined,
      } } }
    })
    // The cancellation supervisor is now the terminal authority for this exact attempt. Retire the
    // ordinary poll token immediately so a scheduled tick cannot later treat the settled run as a
    // detached active job and restart cancellation after navigation or state replacement.
    stopTrackedPoll()
  }
  const installCompactFence = (terminal: RunStatus, reason: string) => {
    const status = sanitizeUnverifiedProfileStatus({
      ...expectedStatus,
      jobType: 'profile',
      status: terminal.status,
      error: terminal.error,
    })
    set((state) => {
      const tracked = boundTrackedJob(state)
      if (!tracked || !sameProfileAttempt(expectedStatus, tracked.status!)
          || !profileStatusCanAdvance(tracked.status!, status)) return {}
      const cancelled = status.status === 'cancelled'
      return { profileJobs: { ...state.profileJobs, [nodeId]: {
        ...tracked,
        status,
        identityVerified: false,
        phase: cancelled ? 'cancelled' : 'failed',
        error: cancelled
          ? undefined
          : `The run reached ${status.status}, but its durable full-profile projection could not be recovered: ${reason}`,
      } } }
    })
    stopTrackedPoll()
  }
  const reconcileTerminal = async (terminal: RunStatus) => {
    if (!terminalRunStatus(terminal)) return
    if (sameProfileAttempt(expectedStatus, terminal)) {
      installExactTerminal(terminal)
      return
    }
    if (!exactProfileTerminal(expectedStatus, terminal)) return

    let reason = 'the exact attempt was not present in the durable projection'
    for (let attempt = 0; attempt <= PROFILE_RETRY_DELAYS_MS.length; attempt += 1) {
      if (!boundTrackedJob(get())) return
      try {
        const projected = (await api.profileJobs(expectedCanvasId)).find(
          (candidate) => sameProfileAttempt(expectedStatus, candidate),
        )
        if (!boundTrackedJob(get())) return
        if (projected && terminalRunStatus(projected)) {
          installExactTerminal(projected)
          return
        }
        if (!projected) {
          installCompactFence(terminal, 'the exact attempt was not present in the durable projection')
          return
        }
        reason = projected
          ? 'the exact durable projection had not reached terminal state'
          : 'the exact attempt was not present in the durable projection'
      } catch (error) {
        reason = (error as Error).message || 'the durable projection request failed'
        if (!retryableProfileRequest(error)) break
      }
      if (attempt < PROFILE_RETRY_DELAYS_MS.length) await wait(PROFILE_RETRY_DELAYS_MS[attempt])
    }
    installCompactFence(terminal, reason)
  }

  if (observedTerminal) {
    await reconcileTerminal(observedTerminal)
    return
  }
  superviseDetachedProfileCancellation(current.status, principalId, true, (terminal) => {
    void reconcileTerminal(terminal)
  })
}

export const useStore = create<Store>((set, get) => ({
  doc: emptyDoc(),
  canvasRole: null,
  view: 'canvas',
  setView: (view) => {
    if (get().view !== view) _fileNavigationGeneration += 1
    set({ view })
  },
  erFocusUri: null,
  openRelationships: (uri) => {
    if (get().view !== 'relationships') _fileNavigationGeneration += 1
    set({ erFocusUri: uri, view: 'relationships' })
  },
  addToCanvas: (kind, config, title) => {
    if (!roleCanEdit(get().canvasRole)) {
      get().pushToast('This canvas is view-only', 'info')
      return
    }
    const pos = freePosition(get().doc.nodes, { x: 160, y: 160 })
    get().addNode(kind, pos, config, title)  // commits + selects the new node
    set({ view: 'canvas' })
  },
  fullscreenCode: null,
  openCodeFullscreen: (nodeId, param, lang) => set({ fullscreenCode: { nodeId, param, lang } }),
  closeCodeFullscreen: () => set({ fullscreenCode: null }),
  peers: {},
  setPeer: (id, p) => set((s) => ({ peers: { ...s.peers, [id]: p } })),
  dropPeer: (id) => set((s) => { const peers = { ...s.peers }; delete peers[id]; return { peers } }),
  clearPeers: () => set({ peers: {} }),
  toasts: [],
  pushToast: (msg, kind = 'info') => {
    const id = `t_${Math.floor(performance.now())}_${Math.random().toString(36).slice(2, 6)}`
    set((s) => ({ toasts: [...s.toasts, { id, kind, msg }] }))
    setTimeout(() => get().dismissToast(id), kind === 'error' ? 7000 : 4000)
  },
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  authEnabled: false,
  setAuthEnabled: (v) => set({ authEnabled: v }),
  currentUser: null,
  users: [],
  files: [],
  kernelInfo: null,
  kernelUp: false,
  accessDenied: false,
  catalog: [],
  processors: [],
  specsVersion: 0,
  schemas: {},
  sizes: {},
  selectedId: null,
  selectedIds: [],
  openPanels: {},
  previews: {},
  runs: {},
  profileJobs: {},
  past: [],
  future: [],
  saved: true,
  agentOpen: false,
  agentLog: [],

  setNodes: (nodes) => { if (roleCanEdit(get().canvasRole)) set((s) => ({ doc: { ...s.doc, nodes } })) },
  setEdges: (edges) => { if (roleCanEdit(get().canvasRole)) set((s) => ({ doc: { ...s.doc, edges } })) },

  // push the current doc onto the undo stack (called before a structural mutation). While co-editing,
  // also mark a checkpoint in the CRDT UndoManager so undo granularity matches these boundaries.
  commit: () => {
    if (!roleCanEdit(get().canvasRole)) return
    crdtUndo.boundary?.()
    set((s) => ({ past: [...s.past, s.doc].slice(-50), future: [] }))
  },

  undo: () => {
    if (!roleCanEdit(get().canvasRole)) return
    _cfgEdit = { id: '', t: 0 }  // a following edit starts a fresh undo checkpoint
    // co-editing: undo via the CRDT manager, scoped to MY edits — never deletes a peer's concurrent
    // node/edge (the full-doc snapshot below would). The Y→store bridge updates the doc.
    if (crdtUndoActive()) { crdtUndo.undo!(); set({ openPanels: {} }); return }
    set((s) => {
      if (s.past.length === 0) return {}
      const prev = s.past[s.past.length - 1]
      return { doc: prev, past: s.past.slice(0, -1), future: [s.doc, ...s.future].slice(0, 50), openPanels: {} }
    })
  },

  redo: () => {
    if (!roleCanEdit(get().canvasRole)) return
    _cfgEdit = { id: '', t: 0 }
    if (crdtUndoActive()) { crdtUndo.redo!(); set({ openPanels: {} }); return }
    set((s) => {
      if (s.future.length === 0) return {}
      const next = s.future[0]
      return { doc: next, future: s.future.slice(1), past: [...s.past, s.doc].slice(-50), openPanels: {} }
    })
  },

  addNode: (kind, position, config, title) => {
    if (!roleCanEdit(get().canvasRole)) return null
    const spec = getSpec(kind)
    if (!spec) return null
    get().commit()
    const base = spec.defaultData()
    const node: CanvasNode = {
      id: newId(kind),
      type: kind,
      position,
      data: {
        ...base,
        title: title ?? base.title,
        config: { ...base.config, ...(config ?? {}) },
      },
    }
    set((s) => ({ doc: { ...s.doc, nodes: [...s.doc.nodes, node] }, selectedId: node.id, selectedIds: [node.id] }))
    return node
  },

  updateConfig: (id, patch) => {
    if (!roleCanEdit(get().canvasRole)) return
    // coalesced undo checkpoint: one per editing burst (new node, or >700ms idle) so a param
    // edit is its own undo step instead of discarding an unrelated earlier change.
    const now = performance.now()
    if (_cfgEdit.id !== id || now - _cfgEdit.t > 700) get().commit()
    _cfgEdit = { id, t: now }
    set((s) => {
      const stale = downstream(s.doc, id)
      const nodes: CanvasNode[] = s.doc.nodes.map((n) => {
        if (n.id === id) {
          const status: NodeStatus = n.data.status === 'draft' ? 'draft' : 'stale'
          return { ...n, data: { ...n.data, config: { ...n.data.config, ...patch }, status } }
        }
        if (stale.has(n.id) && n.data.status === 'latest') {
          return { ...n, data: { ...n.data, status: 'stale' } }
        }
        return n
      })
      // If this edit shrinks/renames the node's declared output ports, drop edges leaving a port
      // that no longer exists. When a previously single-output Section becomes multi-output, bind an
      // old implicit edge to that exact former port; never leave a missing sourceHandle that the new
      // graph contract would reject or guess on the backend.
      let edges = s.doc.edges
      const previousNode = s.doc.nodes.find((node) => node.id === id)
      const editedNode = nodes.find((node) => node.id === id)
      if (editedNode?.type === 'section' && Array.isArray(patch.outputs)) {
        const ports = new Set(nodeOutputs(editedNode).map((port) => port.id))
        const previousPorts = previousNode ? nodeOutputs(previousNode) : []
        edges = edges.flatMap((edge) => {
          if (edge.source !== id) return [edge]
          const priorPortId = edge.sourceHandle
            ?? (previousPorts.length === 1 ? previousPorts[0].id : undefined)
          return priorPortId && ports.has(priorPortId)
            ? [{ ...edge, sourceHandle: priorPortId }]
            : []
        })
      }
      return { doc: { ...s.doc, nodes, edges } }
    })
  },

  updateData: (id, patch) => {
    if (!roleCanEdit(get().canvasRole)) return
    set((s) => ({
      doc: { ...s.doc, nodes: s.doc.nodes.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)) },
    }))
  },

  removeNode: (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    cancelDetachedProfileJob(get().profileJobs[id])
    get().commit()
    set((s) => {
      const previews = { ...s.previews }; delete previews[id]
      const runs = { ...s.runs }; delete runs[id]
      const profileJobs = { ...s.profileJobs }; delete profileJobs[id]
      return {
        doc: {
          ...s.doc,
          nodes: s.doc.nodes.filter((n) => n.id !== id),
          edges: s.doc.edges.filter((e) => e.source !== id && e.target !== id),
        },
        selectedId: s.selectedId === id ? null : s.selectedId,
        selectedIds: s.selectedIds.filter((x) => x !== id),
        openPanels: Object.fromEntries(Object.entries(s.openPanels).filter(([k]) => k !== id)),
        previews, runs, profileJobs,
      }
    })
  },

  connect: (edge) => {
    if (!roleCanEdit(get().canvasRole)) return
    get().commit()
    set((s) => {
      // one edge per (target, targetHandle) for single-input ports; joins allow two.
      const stale = downstream(s.doc, edge.target)
      const nodes = s.doc.nodes.map((n) =>
        (n.id === edge.target || stale.has(n.id)) && n.data.status === 'latest'
          ? { ...n, data: { ...n.data, status: 'stale' as NodeStatus } }
          : n,
      )
      return { doc: { ...s.doc, edges: [...s.doc.edges, edge], nodes } }
    })
  },

  removeEdge: (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    get().commit()
    set((s) => ({ doc: { ...s.doc, edges: s.doc.edges.filter((e) => e.id !== id) } }))
  },

  // Move a node into a section (parentId set, position now relative to the section) or back out to
  // the top-level canvas (parentId null, position absolute). Marks the section + downstream stale.
  setParent: (id, parentId, position) => {
    if (!roleCanEdit(get().canvasRole)) return
    get().commit()
    set((s) => {
      const stale = parentId ? downstream(s.doc, parentId) : new Set<string>()
      return {
        doc: {
          ...s.doc,
          nodes: s.doc.nodes.map((n) => {
            if (n.id === id) return { ...n, parentId: parentId ?? null, position }
            if (parentId && (n.id === parentId || stale.has(n.id)) && n.data.status === 'latest') {
              return { ...n, data: { ...n.data, status: 'stale' as NodeStatus } }
            }
            return n
          }),
        },
      }
    })
  },

  select: (id) => set({ selectedId: id, selectedIds: id ? [id] : [] }),

  setSelection: (ids) => set({ selectedIds: ids, selectedId: ids[ids.length - 1] ?? null }),

  selectAll: () => set((s) => ({ selectedIds: s.doc.nodes.map((n) => n.id), selectedId: s.doc.nodes[s.doc.nodes.length - 1]?.id ?? null })),

  copySelection: () => {
    const s = get()
    const ids = new Set(s.selectedIds.length ? s.selectedIds : (s.selectedId ? [s.selectedId] : []))
    if (!ids.size) return
    _clipboard = {
      nodes: s.doc.nodes.filter((n) => ids.has(n.id)).map(_clone),
      edges: s.doc.edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map(_clone),
    }
  },

  cutSelection: () => {
    if (!roleCanEdit(get().canvasRole)) return
    get().copySelection()
    get().removeSelected()
  },

  paste: () => {
    if (!roleCanEdit(get().canvasRole)) return
    if (!_clipboard || !_clipboard.nodes.length) return
    get().commit()
    const { nodes, edges } = cloneSubgraph(_clipboard.nodes, _clipboard.edges)
    set((s) => ({
      doc: { ...s.doc, nodes: [...s.doc.nodes, ...nodes], edges: [...s.doc.edges, ...edges] },
      selectedIds: nodes.map((n) => n.id), selectedId: nodes[nodes.length - 1]?.id ?? null,
    }))
  },

  duplicateSelected: () => {
    if (!roleCanEdit(get().canvasRole)) return
    const s = get()
    const ids = s.selectedIds.length ? s.selectedIds : (s.selectedId ? [s.selectedId] : [])
    if (ids.length <= 1) { if (ids[0]) get().duplicate(ids[0]); return }  // single → reuse the existing path
    get().commit()
    const sel = new Set(ids)
    const { nodes, edges } = cloneSubgraph(
      s.doc.nodes.filter((n) => sel.has(n.id)),
      s.doc.edges.filter((e) => sel.has(e.source) && sel.has(e.target)),
    )
    set((st) => ({
      doc: { ...st.doc, nodes: [...st.doc.nodes, ...nodes], edges: [...st.doc.edges, ...edges] },
      selectedIds: nodes.map((n) => n.id), selectedId: nodes[nodes.length - 1]?.id ?? null,
    }))
  },

  removeSelected: () => {
    if (!roleCanEdit(get().canvasRole)) return
    const ids = get().selectedIds.length ? get().selectedIds : (get().selectedId ? [get().selectedId!] : [])
    if (!ids.length) return
    for (const id of ids) cancelDetachedProfileJob(get().profileJobs[id])
    get().commit()
    const kill = new Set(ids)
    set((s) => {
      const previews = Object.fromEntries(Object.entries(s.previews).filter(([k]) => !kill.has(k)))
      const runs = Object.fromEntries(Object.entries(s.runs).filter(([k]) => !kill.has(k)))
      const profileJobs = Object.fromEntries(Object.entries(s.profileJobs).filter(([k]) => !kill.has(k)))
      return {
        doc: {
          ...s.doc,
          nodes: s.doc.nodes.filter((n) => !kill.has(n.id)),
          edges: s.doc.edges.filter((e) => !kill.has(e.source) && !kill.has(e.target)),
        },
        selectedId: null, selectedIds: [],
        openPanels: Object.fromEntries(Object.entries(s.openPanels).filter(([k]) => !kill.has(k))),
        previews, runs, profileJobs,
      }
    })
  },

  bypass: (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    get().commit()
    set((s) => ({
      doc: {
        ...s.doc,
        nodes: s.doc.nodes.map((n) => (n.id === id ? { ...n, data: { ...n.data, bypassed: !n.data.bypassed, disabled: false } } : n)),
      },
    }))
  },

  disable: (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    get().commit()
    set((s) => ({
      doc: {
        ...s.doc,
        nodes: s.doc.nodes.map((n) => (n.id === id ? { ...n, data: { ...n.data, disabled: !n.data.disabled, bypassed: false } } : n)),
      },
    }))
  },

  rename: (id, title) => {
    if (!roleCanEdit(get().canvasRole)) return
    get().commit()
    get().updateData(id, { title })
  },

  duplicate: (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    const n = get().doc.nodes.find((x) => x.id === id)
    if (!n) return
    get().commit()
    const copy: CanvasNode = {
      ...n,
      id: newId(n.type),
      parentId: null, // a duplicate lands on the top-level canvas (absolute coords below)
      // land in a clear spot near the original, never stacked on top of it
      position: freePosition(get().doc.nodes, { x: n.position.x + 40, y: n.position.y + 40 }),
      data: { ...n.data, status: 'draft', history: [] },
    }
    set((s) => ({ doc: { ...s.doc, nodes: [...s.doc.nodes, copy] }, selectedId: copy.id, selectedIds: [copy.id] }))
  },

  // one panel open at a time across the whole canvas — never overlapping
  togglePanel: (id, kind) =>
    set((s) => (s.openPanels[id] === kind ? { openPanels: {} } : { openPanels: { [id]: kind }, selectedId: id })),

  openPanel: (id, kind) => set({ openPanels: { [id]: kind }, selectedId: id }),

  closePanel: (id) =>
    set((s) => (s.openPanels[id] ? { openPanels: {} } : {})),

  runPreview: async (id: string, offset = 0, requestedPortId?: string) => {
    // offset lives in the preview state (single source of truth) so an external Refresh (which
    // re-fetches page 0) and the panel's page controls never disagree.
    const doc = get().doc
    const node = doc.nodes.find((candidate) => candidate.id === id)
    if (!node) return
    const ports = nodeOutputs(node)
    const currentPortId = get().previews[id]?.portId
    const defaultPortId = ports.find((port) => port.id === 'out')?.id ?? ports[0]?.id
    const portId = requestedPortId ?? (ports.length > 1
      ? ports.find((port) => port.id === currentPortId)?.id ?? defaultPortId
      : undefined)
    const planIdentity = previewPlanIdentity(doc, id, portId)
    const requestGeneration = ++_previewRequestGeneration
    const isCurrent = () => {
      const state = get()
      const preview = state.previews[id]
      return preview?.requestGeneration === requestGeneration
        && previewIsCurrent(preview, state.doc, id, portId)
    }
    set((s) => ({
      previews: {
        ...s.previews,
        [id]: { canvasId: doc.id, nodeId: id, portId, planIdentity, requestGeneration, loading: true, offset },
      },
      openPanels: { [id]: 'data' },
    }))
    const spec = getSpec(node.type)
    if (spec?.previewable === false) {
      set((s) => ({
        previews: {
          ...s.previews,
          [id]: {
            canvasId: doc.id, nodeId: id, portId, planIdentity, requestGeneration, offset,
            result: {
              columns: [],
              rows: [],
              truncated: false,
              completeness: 'unknown',
              notPreviewable: true,
              reason: `${spec.title} is not sample-previewable — run a full pass`,
              wire: 'dataset',
            },
          },
        },
      }))
      return
    }
    try {
      // A preview is a bounded peek (a page of rows), NOT a full materialized run — we deliberately
      // do NOT flip status to 'latest' (that green state means a real run). Paginated via `offset`.
      // A chart renders its visible series at once, so request the explicit 2,000-point presentation
      // budget instead of a 50-row page. Durable run artifacts retain every group.
      const k = node.type === 'chart' ? 2000 : 50
      const result = await api.preview(doc, id, k, offset, portId)
      if (!isCurrent()) return
      set((s) => ({
        previews: { ...s.previews, [id]: { canvasId: doc.id, nodeId: id, portId, planIdentity, requestGeneration, result, offset } },
      }))
    } catch (e) {
      if (!isCurrent()) return
      set((s) => ({
        previews: {
          ...s.previews,
          [id]: { canvasId: doc.id, nodeId: id, portId, planIdentity, requestGeneration, error: (e as Error).message, offset },
        },
      }))
    }
  },

  // The play action: estimate, then start immediately for cheap work; only gate on expensive
  // runs (FR-E3). Do NOT auto-open the run panel — the card shows status; the user opens details
  // if interested. A confirm gate is the one exception (it needs the panel to show the button).
  requestRun: async (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'estimating' } } }))
    let estimate
    try {
      estimate = await api.estimate(get().doc, id)
    } catch (e) {
      set((s) => ({ runs: { ...s.runs, [id]: { phase: 'failed', error: (e as Error).message } } }))
      get().pushToast((e as Error).message || 'Could not estimate the run', 'error')
      return
    }
    if (estimate.needsConfirm) {
      set((s) => ({ runs: { ...s.runs, [id]: { estimate, phase: 'confirm' } }, openPanels: { [id]: 'run' } }))
    } else {
      set((s) => ({ runs: { ...s.runs, [id]: { estimate, phase: 'running' } } }))
      await get().run(id, false)
    }
  },

  estimate: async (id) => {
    set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'estimating' } }, openPanels: { [id]: 'run' } }))
    try {
      const estimate = await api.estimate(get().doc, id)
      set((s) => ({
        runs: { ...s.runs, [id]: { estimate, phase: estimate.needsConfirm ? 'confirm' : 'estimated' } },
      }))
    } catch (e) {
      set((s) => ({ runs: { ...s.runs, [id]: { phase: 'failed', error: (e as Error).message } } }))
    }
  },

  run: async (id, confirmed = false) => {
    if (!roleCanEdit(get().canvasRole)) return
    // no openPanels here — status shows on the card; the user opens the run panel if they want detail
    set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'running' } } }))
    get().updateData(id, { status: 'running' })
    try {
      const status = await api.run(get().doc, id, confirmed)
      set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), status, phase: 'running' } } }))
      pollRun(get, set, id, status.runId)
    } catch (e) {
      if (e instanceof KernelError && e.status === 409) {
        set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'confirm' } } }))
        get().updateData(id, { status: 'stale' })
        return
      }
      set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'failed', error: (e as Error).message } } }))
      get().updateData(id, { status: 'failed' })
      get().pushToast((e as Error).message || 'Run failed to start', 'error')
    }
  },

  // Re-run the whole graph: kick every runnable sink (a node with no outgoing edge); each pulls
  // its upstream, so the full pipeline re-executes. Notes/unconnected nodes aren't runnable → skipped.
  rerunAll: () => {
    if (!roleCanEdit(get().canvasRole)) return
    const { doc } = get()
    const hasOutgoing = new Set(doc.edges.map((e) => e.source))
    // a section's contained children are run by the section, not as top-level sinks
    const sinks = doc.nodes.filter((n) => !n.parentId && !hasOutgoing.has(n.id) && nodeRunnable(doc, n.id))
    // don't kick off pipelines that would fail on a missing required field (matches the disabled ▶)
    const valid = sinks.filter((n) => !hasInvalidUpstream(doc, n.id))
    valid.forEach((n) => get().requestRun(n.id))
    const invalidSkipped = sinks.length - valid.length
    if (invalidSkipped) get().pushToast(`Skipped ${invalidSkipped} pipeline${invalidSkipped > 1 ? 's' : ''} with a required field still empty`, 'info')
  },

  cancelRun: async (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    const st = get().runs[id]?.status
    if (!st) return
    await api.cancelRun(st.runId).catch(() => {})
    set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'idle' } } }))
    get().updateData(id, { status: 'stale' })
    settleAnimatingNodes(set)  // clear intermediate nodes' animation now, not only when the next poll lands
  },

  clearRun: (id) =>
    set((s) => {
      const next = { ...s.runs }
      delete next[id]
      return { runs: next }
    }),

  // A whole-dataset profile is always a two-step interaction: preflight first, then an explicit Start.
  // Capture graph identity around both calls and cancel any superseded scan without ever auto-submitting.
  prepareFullProfile: async (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    const doc = get().doc
    if (!doc.nodes.some((node) => node.id === id)) return
    const planIdentity = profilePlanIdentity(doc, id)
    const requestGeneration = ++_profileRequestGeneration
    const previous = get().profileJobs[id]
    cancelDetachedProfileJob(previous)
    const isCurrent = () => {
      const job = get().profileJobs[id]
      return job?.requestGeneration === requestGeneration && profileJobIsCurrent(job, get().doc, id)
    }
    set((s) => ({ profileJobs: {
      ...s.profileJobs,
      [id]: {
        canvasId: doc.id, nodeId: id, principalId: s.currentUser?.id,
        canCancel: roleCanEdit(s.canvasRole), planIdentity, requestGeneration, phase: 'estimating',
      },
    } }))
    let estimate: RunEstimate
    let planDigest: string
    try {
      const preflight = await api.profileEstimate(doc, id)
      estimate = preflight
      planDigest = preflight.planDigest
    } catch (e) {
      if (!isCurrent()) return
      set((s) => ({ profileJobs: { ...s.profileJobs, [id]: {
        ...(s.profileJobs[id]!), phase: 'failed', error: (e as Error).message || 'Could not estimate full profile',
      } } }))
      return
    }
    if (!isCurrent()) return
    set((s) => ({ profileJobs: { ...s.profileJobs, [id]: {
      ...(s.profileJobs[id]!), estimate, planDigest, phase: 'preflight', error: undefined,
    } } }))
  },

  startFullProfile: async (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    const job = get().profileJobs[id]
    const doc = get().doc
    const retryingUnknownSubmission = job?.phase === 'failed' && job.submissionUnresolved === true
    if (!job?.estimate || !job.planDigest || (job.phase !== 'preflight' && !retryingUnknownSubmission)
        || !profileJobIsCurrent(job, doc, id)) return
    const submissionUserId = get().currentUser?.id
    if (!submissionUserId) {
      set((s) => ({ profileJobs: { ...s.profileJobs, [id]: {
        ...(s.profileJobs[id]!), phase: 'failed', error: 'A confirmed user is required to start a full profile',
      } } }))
      return
    }
    const { planDigest, requestGeneration } = job
    const submissionId = retryingUnknownSubmission && job.submissionId
      ? job.submissionId
      : globalThis.crypto.randomUUID()
    let pendingSubmission = _pendingProfileSubmissions.get(submissionId)
    if (!pendingSubmission) {
      pendingSubmission = {
        doc: _clone(doc), nodeId: id, planDigest, submissionId,
        userId: submissionUserId, canCancel: true,
        cancelRequested: false, reconciling: false,
      }
      _pendingProfileSubmissions.set(submissionId, pendingSubmission)
    }
    const isSameSubmission = () => {
      const current = get().profileJobs[id]
      return current?.requestGeneration === requestGeneration
        && current.submissionId === submissionId
        && current.principalId === submissionUserId
    }
    const isCurrent = () => {
      const current = get().profileJobs[id]
      return isSameSubmission()
        && _profileSubmissionUserId === submissionUserId
        && profileJobIsCurrent(current, get().doc, id)
    }
    set((s) => ({ profileJobs: { ...s.profileJobs, [id]: {
      ...(s.profileJobs[id]!), principalId: submissionUserId,
      canCancel: true,
      submissionId, submissionUnresolved: false,
      cancelRequested: retryingUnknownSubmission ? job.cancelRequested : false,
      phase: 'queued', error: undefined,
    } } }))
    let status: RunStatus
    try {
      // This click is the explicit confirmation. The server recomputes admission from the submitted
      // graph and still rejects a large/unknown direct API call that omits ``confirmed``.
      status = await submitFullProfile(doc, id, planDigest, submissionId, submissionUserId)
    } catch (e) {
      const unresolved = retryableProfileRequest(e)
      if (!unresolved) forgetProfileSubmission(pendingSubmission)
      const current = get().profileJobs[id]
      const sameSubmission = current?.requestGeneration === requestGeneration
        && current.submissionId === submissionId
      const currentPlan = sameSubmission && profileJobIsCurrent(current, get().doc, id)
      const cancelRequested = current?.cancelRequested === true
      if (_profileSubmissionUserId !== submissionUserId) {
        forgetProfileSubmission(pendingSubmission)
        return
      }
      if (!sameSubmission || !currentPlan || cancelRequested) {
        if (unresolved) reconcileAndCancelProfileSubmission(pendingSubmission)
        return
      }
      set((s) => {
        const current = s.profileJobs[id]
        if (!current || current.requestGeneration !== requestGeneration
            || current.submissionId !== submissionId
            || s.currentUser?.id !== submissionUserId
            || !profileJobIsCurrent(current, s.doc, id)) return {}
        return { profileJobs: { ...s.profileJobs, [id]: {
          ...current, phase: 'failed', submissionUnresolved: unresolved,
          error: unresolved
            ? cancelRequested
              ? 'Could not confirm or cancel the full-profile submission. Retry to reconcile the same submission.'
              : 'Could not confirm whether the full profile started. Retry to reconcile the same submission.'
            : (e as Error).message || 'Could not start full profile',
        } } }
      })
      return
    }
    const validIdentity = validProfileSubmissionStatus(status, id, planDigest)
    if (!validIdentity) {
      forgetProfileSubmission(pendingSubmission)
      if (status.jobType === 'profile' && status.runId
          && (status.status === 'queued' || status.status === 'running')) {
        superviseDetachedProfileCancellation(status, submissionUserId, true)
      }
      if (!isCurrent()) return
      set((s) => {
        const current = s.profileJobs[id]
        if (!current || current.requestGeneration !== requestGeneration
            || current.submissionId !== submissionId
            || s.currentUser?.id !== submissionUserId
            || !profileJobIsCurrent(current, s.doc, id)) return {}
        return { profileJobs: { ...s.profileJobs, [id]: {
          ...current, phase: 'failed', submissionUnresolved: false,
          error: 'Full profile started with an invalid durable identity',
        } } }
      })
      return
    }
    forgetProfileSubmission(pendingSubmission)
    if (!isCurrent()) {
      superviseDetachedProfileCancellation(status, submissionUserId, true)
      return
    }
    let installed = false
    let cancelRequested = false
    set((s) => {
      const current = s.profileJobs[id]
      if (!current || current.requestGeneration !== requestGeneration
          || current.submissionId !== submissionId
          || s.currentUser?.id !== submissionUserId
          || !profileJobIsCurrent(current, s.doc, id)) return {}
      installed = true
      cancelRequested = current.cancelRequested === true
      const active = status.status === 'queued' || status.status === 'running'
      return { profileJobs: { ...s.profileJobs, [id]: {
        ...current, status, identityVerified: true, submissionUnresolved: false,
        phase: active && cancelRequested ? 'cancelling' : profilePhase(status),
        error: status.error ?? undefined,
      } } }
    })
    if (!installed) {
      superviseDetachedProfileCancellation(status, submissionUserId, true)
      return
    }
    if (status.status === 'queued' || status.status === 'running') {
      pollProfile(get, set, id, status.runId, requestGeneration, submissionUserId, true)
      if (cancelRequested) {
        if (_profileSubmissionUserId !== submissionUserId) return
        try {
          const cancelled = await api.cancelRun(status.runId)
          set((s) => {
            const current = s.profileJobs[id]
            if (current?.requestGeneration !== requestGeneration
                || current.submissionId !== submissionId
                || current.principalId !== submissionUserId
                || s.currentUser?.id !== submissionUserId
                || current.status?.runId !== status.runId
                || !sameProfileAttempt(current.status, cancelled)
                || !profileStatusCanAdvance(current.status, cancelled)) return {}
            const active = cancelled.status === 'queued' || cancelled.status === 'running'
            return { profileJobs: { ...s.profileJobs, [id]: {
              ...current, status: cancelled,
              phase: active ? 'cancelling' : profilePhase(cancelled),
              error: cancelled.error ?? undefined,
            } } }
          })
          await superviseTrackedProfileCancellation(
            get, set, id, requestGeneration, status.runId, submissionUserId,
            exactProfileTerminal(status, cancelled) ? cancelled : undefined,
          )
        } catch (e) {
          if (retryableProfileRequest(e)) {
            await superviseTrackedProfileCancellation(get, set, id, requestGeneration, status.runId, submissionUserId)
          }
          set((s) => {
            const current = s.profileJobs[id]
            if (current?.requestGeneration !== requestGeneration
                || current.submissionId !== submissionId
                || current.principalId !== submissionUserId
                || s.currentUser?.id !== submissionUserId
                || current.status?.runId !== status.runId
                || !['queued', 'running'].includes(current.status.status)) return {}
            return { profileJobs: { ...s.profileJobs, [id]: {
              ...current, phase: 'cancelling',
              error: `Cancellation request could not be confirmed; still checking run status: ${(e as Error).message || 'request failed'}`,
            } } }
          })
        }
      }
    }
  },

  cancelFullProfile: async (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    const job = get().profileJobs[id]
    if (!job) return
    const principalId = job.principalId
    if (!principalId || !job.canCancel || _profileSubmissionUserId !== principalId) return
    const { requestGeneration, submissionId } = job
    if (!job.status) {
      // The POST may have been accepted even though its response has not arrived. Record intent now;
      // startFullProfile will reconcile the stable submission id and cancel as soon as it learns runId.
      if (!submissionId || (job.phase !== 'queued' && job.phase !== 'cancelling')) return
      set((s) => {
        const current = s.profileJobs[id]
        if (current?.requestGeneration !== requestGeneration || current.submissionId !== submissionId
            || current.principalId !== principalId || s.currentUser?.id !== principalId) return {}
        return { profileJobs: { ...s.profileJobs, [id]: {
          ...current, cancelRequested: true, phase: 'cancelling',
        } } }
      })
      return
    }
    if (!['queued', 'running'].includes(job.status.status)) return
    const runId = job.status.runId
    set((s) => {
      const current = s.profileJobs[id]
      if (current?.requestGeneration !== requestGeneration
          || current.submissionId !== submissionId
          || current.principalId !== principalId
          || s.currentUser?.id !== principalId
          || current.status?.runId !== runId
          || !['queued', 'running'].includes(current.status.status)) return {}
      return { profileJobs: { ...s.profileJobs, [id]: {
        ...current, cancelRequested: true, phase: 'cancelling',
      } } }
    })
    // Recovered fail-closed jobs do not start their normal status poll until identity verification.
    // Cancellation still needs an authoritative lifecycle poll because a lost cancel response is an
    // unknown outcome, not evidence that the job failed.
    pollProfile(get, set, id, runId, requestGeneration, principalId, true)
    if (_profileSubmissionUserId !== principalId) return
    try {
      const cancelled = await api.cancelRun(runId)
      set((s) => {
        const current = s.profileJobs[id]
        if (current?.requestGeneration !== requestGeneration
            || current.submissionId !== submissionId
            || current.principalId !== principalId
            || s.currentUser?.id !== principalId
            || current.status?.runId !== runId
            || !sameProfileAttempt(current.status, cancelled)
            || !profileStatusCanAdvance(current.status, cancelled)) return {}
        const status = current.identityVerified === false
          ? sanitizeUnverifiedProfileStatus(cancelled)
          : cancelled
        const active = status.status === 'queued' || status.status === 'running'
        return { profileJobs: { ...s.profileJobs, [id]: {
          ...current, status, phase: active ? 'cancelling' : profilePhase(status),
          error: status.error ?? undefined,
        } } }
      })
      await superviseTrackedProfileCancellation(
        get, set, id, requestGeneration, runId, principalId,
        exactProfileTerminal(job.status, cancelled) ? cancelled : undefined,
      )
    } catch (e) {
      if (retryableProfileRequest(e)) {
        await superviseTrackedProfileCancellation(get, set, id, requestGeneration, runId, principalId)
      }
      set((s) => {
        const current = s.profileJobs[id]
        if (current?.requestGeneration !== requestGeneration
            || current.submissionId !== submissionId
            || current.principalId !== principalId
            || s.currentUser?.id !== principalId
            || current.status?.runId !== runId
            || !['queued', 'running'].includes(current.status.status)) return {}
        return { profileJobs: { ...s.profileJobs, [id]: {
          ...current, phase: 'cancelling',
          error: `Cancellation request could not be confirmed; still checking run status: ${(e as Error).message || 'request failed'}`,
        } } }
      })
    }
  },

  promote: async (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    const n = get().doc.nodes.find((x) => x.id === id)
    if (!n) return
    const cfg = n.data.config
    const pid = `user.${(n.data.title || 'op').toLowerCase().replace(/[^a-z0-9]+/g, '-')}`
    const desc = await api.promote({
      id: pid,
      title: n.data.title,
      mode: (cfg.mode as string) ?? 'map',
      code: (cfg.code as string) ?? '',
      inputColumns: [],
      outputSchema: Array.isArray(cfg.outputSchema) ? (cfg.outputSchema as any) : [],  // a {ref} contract doesn't inline here
      blurb: 'promoted from an ad-hoc cell',
    })
    // KEEP the original code on the node (don't null it): the promote is in-memory server-side, so
    // after a kernel restart the library id may be gone — the kept code lets the node still run
    // (engine falls back to it) instead of the user's code being destroyed.
    get().updateConfig(id, { source: 'library', processor: desc.id, version: desc.version })
    // refresh ONLY the processor list for the library picker — do NOT call bootstrap(), which
    // would re-hydrate the doc from (debounced, still-stale) localStorage and revert this node.
    try {
      set({ processors: await api.processors() })
    } catch { /* offline */ }
  },

  restoreVersion: (id, versionId) => {
    if (!roleCanEdit(get().canvasRole)) return
    get().commit()  // Restore is undoable
    set((s) => {
      const stale = downstream(s.doc, id)
      return {
        doc: {
          ...s.doc,
          nodes: s.doc.nodes.map((n) => {
            if (n.id === id) {
              const v = (n.data.history ?? []).find((h) => h.id === versionId)
              return v ? { ...n, data: { ...n.data, config: { ...v.config }, status: 'latest' } } : n
            }
            // restoring a node's config invalidates its dependents
            if (stale.has(n.id) && n.data.status === 'latest') {
              return { ...n, data: { ...n.data, status: 'stale' } }
            }
            return n
          }),
        },
      }
    })
  },

  bootstrap: async () => {
    setApiUser(localStorage.getItem(USER_KEY))  // restore chosen user (server defaults to 'local')
    try {
      // NOTE: we deliberately do NOT load the whole catalog here — it can be thousands of tables. The
      // Tables view browses it server-side (paginated + faceted); the working set is filled on demand
      // (ensureCanvasTables when a canvas opens, search results, uploads).
      const [kernelInfo, processors, nodes] = await Promise.all([
        api.kernel(), api.processors(), api.nodes(),
      ])
      const added = registerGenericNodes(nodes)
      set((s) => ({ kernelInfo, kernelUp: true, processors,
        specsVersion: added ? s.specsVersion + 1 : s.specsVersion }))
    } catch {
      set({ kernelUp: false })
    }
    try {
      // resolve identity, load this user's files, open the last-opened (or newest, or a fresh one)
      const me = await api.me()
      setApiUser(me.id); localStorage.setItem(USER_KEY, me.id)
      // Identity is server-confirmed now. Set it before the remaining calls so an offline failure may
      // use only THIS user's cached canvas role; an unknown identity always stays fail-closed.
      set({ currentUser: me })
      const users = await api.users()
      set({ users })
      await get().refreshFiles()
      const files = get().files
      // honor a deep link (#/canvas/<id>, incl. a shared canvas resolved server-side); else the
      // last-opened / newest / a fresh file. A #/tables|#/transforms|#/files link still loads a
      // current canvas underneath, then switches to that shell view below.
      const route = parseHash()
      const last = localStorage.getItem(OPEN_KEY(me.id))
      const fallback = last && files.some((f) => f.id === last) ? last : files[0]?.id
      // a deep-linked canvas that can't be opened (bad/revoked/other-user's link) must NOT discard
      // the last-opened file into a throwaway blank — fall back cleanly.
      const opened = (route.view === 'canvas' && route.canvasId) ? await get().openFile(route.canvasId) : false
      if (!opened) {
        if (fallback) await get().openFile(fallback)
        else await get().newFile()
        if (route.view !== 'canvas') get().setView(route.view)
      }
    } catch {
      // offline / no kernel: fall back to the local cached doc so work survives a refresh
      try {
        const saved = localStorage.getItem(LS_KEY)
        if (saved) {
          const doc = JSON.parse(saved) as CanvasDoc
          if (doc?.nodes) {
            const role = cachedRole(get().currentUser?.id, doc.id)
            set({ doc, canvasRole: role, agentOpen: false })
          }
        }
      } catch { /* ignore corrupt state */ }
    }
    _bootstrapped = true  // now the real doc is loaded → autosave may persist edits (not the throwaway empty doc)
    void get().refreshSchemas()
  },

  refreshFiles: async () => {
    const generation = ++_fileListGeneration
    const userId = get().currentUser?.id
    if (!userId) {
      set({ canvasRole: null, agentOpen: false })
      return false
    }
    try {
      const files = await api.listCanvases()
      // A response started under another identity must never populate this user's files or role cache.
      if (generation !== _fileListGeneration || get().currentUser?.id !== userId) return false
      for (const file of files) rememberRole(userId, file.id, file.role)
      const currentId = get().doc.id
      if (!files.some((file) => file.id === currentId)) rememberRole(userId, currentId, null)
      set((state) => {
        if (generation !== _fileListGeneration || state.currentUser?.id !== userId) return {}
        const open = files.find((file) => file.id === state.doc.id)
        // The list is the server's current authority. Missing means deleted or access revoked; keep
        // rendering the snapshot for inspection, but close the local edit window immediately.
        if (!open) return { files, canvasRole: null, agentOpen: false }
        const role = open.role ?? null
        return { files, canvasRole: role, agentOpen: roleCanEdit(role) ? state.agentOpen : false }
      })
      return true
    } catch {
      // A transport/server failure is not an authoritative revocation. Retain the last confirmed
      // files, open role, and per-user role cache; callers decide whether an individual action must
      // fail closed until a fresh role can be obtained.
      return false
    }
  },
  refreshUsers: async () => { try { set({ users: await api.users() }) } catch { /* offline */ } },

  openFile: async (id) => {
    const generation = ++_fileNavigationGeneration
    const userId = get().currentUser?.id
    if (!userId) {
      get().pushToast('Your identity is not available yet', 'error')
      return false
    }
    const isCurrent = () => generation === _fileNavigationGeneration && get().currentUser?.id === userId
    try {
      const doc = await api.getCanvas(id)
      if (!isCurrent()) return false

      // getCanvas proves read access to the document, but does not return the effective role. Refresh
      // the list now rather than trusting a stale editor/owner entry left from an earlier session.
      const roleRefreshed = await get().refreshFiles()
      if (!isCurrent()) return false
      const file = roleRefreshed ? get().files.find((candidate) => candidate.id === id) : undefined
      const role = file?.role ?? null
      const accessRemoved = roleRefreshed && !file
      if (accessRemoved) rememberRole(userId, id, null) // authoritative revoke/delete
      get().loadDoc(doc, role)
      const uid = get().currentUser?.id
      if (uid) localStorage.setItem(OPEN_KEY(uid), id)
      set({ view: 'canvas' })  // opening a file navigates to the editor
      if (accessRemoved) get().pushToast('This canvas is no longer in your accessible files. Opened the fetched snapshot read-only.', 'error')
      else if (!roleRefreshed || !role) get().pushToast('Opened read-only because your current access could not be confirmed', 'error')
      return true
    } catch {
      if (!isCurrent()) return false
      // not found / no access / deleted elsewhere → leave the current canvas & view untouched, prune
      // the stale card, and tell the user. The caller decides where to land (never a silent blank).
      await get().refreshFiles()
      if (!isCurrent()) return false
      get().pushToast('That canvas could not be opened (not found or no access)', 'error')
      return false
    }
  },

  newFile: async (options) => {
    const generation = ++_fileNavigationGeneration
    const userId = get().currentUser?.id ?? null
    const doc = emptyDoc()
    const signal = options?.signal
    const isCurrent = () => !signal?.aborted
      && generation === _fileNavigationGeneration
      && (get().currentUser?.id ?? null) === userId
    const cleanUpCancelledRemoteDraft = async () => {
      // Called only after the create response proves this request inserted doc.id. If this best-effort
      // cleanup fails, the server keeps an empty, recoverable draft; the import graph is never applied.
      try { await api.deleteCanvas(doc.id) } catch { /* retain the empty remote draft */ }
    }
    let persistence: CanvasPersistence = 'remote'
    if (signal?.aborted) return { ok: false }
    try {
      // Do not abort this POST: once the server may have committed, an AbortError cannot tell us whether
      // this request owns doc.id. Wait for explicit insert evidence; a lost response leaves the empty
      // draft recoverable rather than risking a speculative DELETE of a pre-existing canvas.
      const created = await api.createCanvas(doc)
      if (!created.ok || !created.created || created.id !== doc.id) return { ok: false }
      if (!isCurrent()) {
        if (signal) await cleanUpCancelledRemoteDraft()
        return { ok: false }
      }
      rememberRole(userId, doc.id, 'owner') // create response confirms ownership
      // A cancellable import must not leave an await gap between the final validity check and
      // activation: Cancel/navigation could otherwise interleave after the remote canvas exists.
      // Refresh the list after activation instead; its own generation/user guards make it safe.
      if (!signal) await get().refreshFiles()
    } catch (e) {
      if (!isCurrent() || (e as Error)?.name === 'AbortError') {
        // The create outcome is unknown: retain a possible empty draft. Without a positive response we
        // cannot distinguish our committed insert from a collision with somebody else's canvas.
        return { ok: false }
      }
      if (e instanceof KernelError) {
        if (e.status === 401) {
          rememberRole(userId, get().doc.id, null)
          set({ canvasRole: null, agentOpen: false, accessDenied: true, kernelUp: true })
          get().pushToast('Your session no longer permits creating canvases. The current canvas is now read-only.', 'error')
        } else if (e.status === 403) {
          set({ kernelUp: true })
          get().pushToast('You do not have permission to create a canvas.', 'error')
        } else {
          set({ kernelUp: true })
          get().pushToast(`Could not create canvas: ${e.message}`, 'error')
        }
        return { ok: false }
      }
      // A transport failure is the one case where local-first creation is truthful: this is a new,
      // collision-resistant local draft and a later PUT can create it as the current user's canvas.
      persistence = 'local-draft'
    }
    if (!isCurrent()) {
      return { ok: false }
    }
    get().loadDoc(doc, 'owner')
    const uid = get().currentUser?.id
    if (uid) localStorage.setItem(OPEN_KEY(uid), doc.id)
    set({ view: 'canvas' })
    if (signal && persistence === 'remote') void get().refreshFiles()
    return { ok: true, canvasId: doc.id, persistence }
  },

  newFromExample: async (key) => {
    const generation = ++_fileNavigationGeneration
    const userId = get().currentUser?.id ?? null
    const id = `canvas_${Math.floor(performance.now())}_${Math.random().toString(36).slice(2, 8)}`
    const doc = exampleDoc(key, id)  // a runnable starter on the seeded data; falls back to a blank file
    if (!doc) return get().newFile()
    let persistence: CanvasPersistence = 'remote'
    try {
      const created = await api.createCanvas(doc)
      if (!created.ok || !created.created || created.id !== doc.id) return { ok: false }
      if (generation !== _fileNavigationGeneration || (get().currentUser?.id ?? null) !== userId) return { ok: false }
      rememberRole(userId, doc.id, 'owner') // create response confirms ownership
      await get().refreshFiles()
    } catch (e) {
      if (generation !== _fileNavigationGeneration || (get().currentUser?.id ?? null) !== userId) return { ok: false }
      if (e instanceof KernelError) {
        if (e.status === 401) {
          rememberRole(userId, get().doc.id, null)
          set({ canvasRole: null, agentOpen: false, accessDenied: true, kernelUp: true })
          get().pushToast('Your session no longer permits creating canvases. The current canvas is now read-only.', 'error')
        } else if (e.status === 403) {
          set({ kernelUp: true })
          get().pushToast('You do not have permission to create a canvas.', 'error')
        } else {
          set({ kernelUp: true })
          get().pushToast(`Could not create canvas: ${e.message}`, 'error')
        }
        return { ok: false }
      }
      // Transport failure: keep the runnable example as an offline local-first draft.
      persistence = 'local-draft'
    }
    if (generation !== _fileNavigationGeneration || (get().currentUser?.id ?? null) !== userId) return { ok: false }
    get().loadDoc(doc, 'owner')
    const uid = get().currentUser?.id
    if (uid) localStorage.setItem(OPEN_KEY(uid), doc.id)
    set({ view: 'canvas' })
    return { ok: true, canvasId: doc.id, persistence }
  },

  renameFile: (name) => {
    if (roleCanEdit(get().canvasRole)) set((s) => ({ doc: { ...s.doc, name } }))
  },  // autosave PUTs + refreshes the list
  setRequirements: (reqs) => {
    if (roleCanEdit(get().canvasRole)) set((s) => ({ doc: { ...s.doc, requirements: reqs } }))
  },  // canvas pip deps; autosave persists

  deleteFile: async (id) => {
    const targetRole = get().files.find((file) => file.id === id)?.role
      ?? (get().doc.id === id ? get().canvasRole : null)
    if (targetRole !== 'owner') {
      get().pushToast('Only the canvas owner can delete it', 'error')
      return
    }
    // permanent + not undoable → confirm first (guards both the file menu and the Recents trash)
    const f = get().files.find((x) => x.id === id)
    if (typeof window !== 'undefined' && !window.confirm(`Delete "${f?.name || 'this canvas'}"? This can't be undone.`)) return
    try { await api.deleteCanvas(id); await get().refreshFiles() } catch { /* offline */ }
    // only load a replacement (which navigates to the editor) if the deleted file was the one open
    // IN the editor; deleting from the Recents grid should just drop the card and stay in the shell.
    if (get().doc.id === id && get().view === 'canvas') {
      const next = get().files[0]?.id
      if (next) await get().openFile(next)
      else await get().newFile()
    }
  },

  // Refresh the WORKING SET (not the whole catalog): re-fetch the tables the open canvas references,
  // so declared-key / schema / organization edits made elsewhere show up. The Tables view + ER view
  // do their own server-side paginated fetches — they don't depend on this.
  refreshCatalog: async () => {
    await get().ensureCanvasTables(get().doc, { force: true })
  },

  rememberTables: (tables) => mergeIntoCatalog(set, tables),

  ensureCanvasTables: async (doc, opts) => {
    const force = (opts as { force?: boolean } | undefined)?.force
    // a source ref can be a uri, a bare catalog name, or a tableId — count all three as "have"
    const have = new Set(get().catalog.flatMap((t) => [t.uri, t.name, t.id]))
    const wanted = Array.from(new Set(
      doc.nodes.filter((n) => n.type === 'source' && n.data.config.uri).map((n) => String(n.data.config.uri)),
    ))
    const need = force ? wanted : wanted.filter((u) => !have.has(u))
    if (!need.length) return
    // bare names/ids (no path separator or scheme — agent/MCP/example sources) can't match the exact
    // `uris` filter, so they resolve via individual lookups instead
    const bare = need.filter((u) => !u.includes('/') && !u.includes('\\'))
    const uris = need.filter((u) => u.includes('/') || u.includes('\\'))
    try {
      const found: CatalogTable[] = []
      if (uris.length) {
        // ONE batched request (repeated ?uris=…); an unregistered source uri is simply absent from the
        // result — never a per-uri 404 (which would pollute the console + cost N round-trips).
        const page = await api.tablesPage({ uris, limit: uris.length })
        found.push(...page.items)
      }
      if (bare.length) {
        const results = await Promise.allSettled(bare.map((r) => api.table(r)))
        for (const r of results) if (r.status === 'fulfilled') found.push(r.value)
      }
      if (found.length) mergeIntoCatalog(set, found)
    } catch { /* offline: canvas still resolves columns from server schema + last preview */ }
  },

  uploadDataset: async (file) => {
    if (!get().kernelUp) { get().pushToast('Kernel offline — cannot upload a file', 'error'); return null }
    try {
      const t = await api.uploadFile(file)
      mergeIntoCatalog(set, [t])  // so the new dataset appears in pickers / the open canvas immediately
      return t
    } catch (e) {
      get().pushToast(`Upload failed: ${e instanceof Error ? e.message : String(e)}`, 'error')
      return null
    }
  },

  refreshSchemas: async () => {
    // guard against out-of-order responses: only the latest request may write the schema map
    const seq = ++_schemaSeq
    try { const schemas = await api.schema(get().doc); if (seq === _schemaSeq) set({ schemas }) }
    catch { /* offline: keep last-known */ }
    // size estimate for the card "~N rows" hint — same trigger, independent (a failure never affects schemas)
    try { const sizes = await api.graphSizes(get().doc); if (seq === _schemaSeq) set({ sizes }) }
    catch { /* offline / no sources countable: keep last-known */ }
  },

  setAgentOpen: (v) => {
    if (v && !roleCanEdit(get().canvasRole)) return
    set({ agentOpen: v })
  },
  pushAgent: (m) => set((s) => ({ agentLog: [...s.agentLog, m] })),

  save: async () => {
    if (!roleCanEdit(get().canvasRole)) return
    try {
      await api.saveCanvas(get().doc)
    } catch { /* offline: keep in memory */ }
  },

  loadDoc: (doc, role = get().canvasRole) => {
    _cfgEdit = { id: '', t: 0 }
    const d = doc
    const agentLog = d.id === get().doc.id ? get().agentLog : []
    set({
      doc: d,
      canvasRole: role,
      accessDenied: false,
      saved: true,
      agentOpen: roleCanEdit(role) ? get().agentOpen : false,
      // Agent requests are independent. A record from another canvas must never be displayed as
      // context for this one (or suggest that it will be sent with a future request).
      agentLog,
      previews: {}, runs: {}, profileJobs: {}, openPanels: {}, selectedId: null, selectedIds: [], past: [], future: [],
    })
    reattachRuns(get, set, d.id)  // a run that outlived a hub restart on its kernel keeps animating here
    void get().ensureCanvasTables(d)  // warm the working set for this canvas's source nodes (on demand)
  },

  // An MCP client (the user's own agent) edited THIS canvas out-of-band — the collab room relayed an
  // 'external-edit' nudge. Debounce a burst of agent edits into one refetch, re-apply the server's doc
  // (so nodes appear live, as if you watched it build), and tell the user. Guarded to the open canvas.
  applyExternalEdit: (canvasId) => {
    const cur = get().doc.id
    if (!cur || (canvasId && canvasId !== cur)) return
    if (_extEditTimer) clearTimeout(_extEditTimer)
    _extEditTimer = setTimeout(async () => {
      _extEditTimer = null
      if (get().doc.id !== cur) return  // navigated away while debouncing
      try {
        get().loadDoc(await api.getCanvas(cur))
        get().pushToast('Canvas updated by your agent', 'info')
      } catch { /* offline / deleted — leave the current view untouched */ }
    }, 250)
  },

  // Apply a graph the LLM agent built (extends the canvas). Undoable; preserves UI state of nodes
  // whose ids already exist, and marks touched nodes stale so the user can preview/run them.
  applyAgentGraph: (bg, targetCanvasId) => {
    if (!roleCanEdit(get().canvasRole)) return false
    if (targetCanvasId && (get().doc.id !== targetCanvasId || get().view !== 'canvas')) return false
    get().commit()
    set((s) => {
      const existing = new Map(s.doc.nodes.map((n) => [n.id, n]))
      const nodes: CanvasNode[] = bg.nodes.map((n) => {
        const prev = existing.get(n.id)
        if (prev) return { ...prev, position: n.position, data: { ...prev.data, title: n.data.title ?? prev.data.title, config: { ...(n.data.config ?? {}) } as CanvasNode['data']['config'], status: 'stale' } }
        return { id: n.id, type: n.type, position: n.position, data: { title: n.data.title ?? n.type, config: (n.data.config ?? {}) as CanvasNode['data']['config'], status: 'stale', history: [] } }
      })
      const edges: CanvasEdge[] = bg.edges.map((e) => ({ id: e.id, source: e.source, target: e.target, sourceHandle: e.sourceHandle ?? null, targetHandle: e.targetHandle ?? null, data: { wire: (e.data?.wire ?? 'dataset') as WireType } }))
      return { doc: { ...s.doc, nodes, edges } }
    })
    return true
  },
}))

// A role belongs to (user, canvas), never just the canvas. Any identity transition invalidates the
// open role synchronously; refreshFiles/openFile must then install the new user's server-reported role.
let _roleUserId = useStore.getState().currentUser?.id ?? null
_profileSubmissionUserId = _roleUserId
useStore.subscribe((state) => {
  const userId = state.currentUser?.id ?? null
  _profileSubmissionUserId = userId
  if (userId === _roleUserId) return
  _roleUserId = userId
  _fileNavigationGeneration += 1
  _fileListGeneration += 1
  // Profile state belongs to a principal, not merely to the open canvas. Remove it synchronously so
  // React can never render Alice's recovered statistics during Bob's file/role refresh window.
  for (const [submissionId, pending] of _pendingProfileSubmissions) {
    if (pending.userId !== userId) _pendingProfileSubmissions.delete(submissionId)
  }
  for (const [runId, detached] of _detachedProfileCancellations) {
    if (detached.userId !== userId) _detachedProfileCancellations.delete(runId)
  }
  for (const [runId, poll] of _profilePolling) {
    if (poll.principalId !== userId) _profilePolling.delete(runId)
  }
  useStore.setState({
    canvasRole: null,
    agentOpen: false,
    profileJobs: {},
  })
})

// Auto-persist the canvas to localStorage (debounced) so a refresh keeps your work.
let _saveTimer: ReturnType<typeof setTimeout> | undefined
let _cacheTimer: ReturnType<typeof setTimeout> | undefined
let _lastDoc: CanvasDoc | undefined
let _bootstrapped = false  // don't autosave the throwaway initial empty doc before the real one loads
useStore.subscribe((s) => {
  if (s.doc === _lastDoc) return
  _lastDoc = s.doc
  if (!_bootstrapped) return  // bootstrap will load & set the real doc; skip persisting anything before that
  // Always keep THIS browser's offline cache current — INCLUDING a peer's merged edit. It's network-free
  // (no PUT), so it causes no write amplification, but it stops an offline reload from losing peer edits
  // received this session.
  clearTimeout(_cacheTimer)
  _cacheTimer = setTimeout(() => {
    try { localStorage.setItem(LS_KEY, JSON.stringify(useStore.getState().doc)) } catch { /* quota */ }
  }, 400)
  // a peer's edit was merged into our doc (via the CRDT) — the editing peer PUTs it, so we must NOT also
  // PUT it. Without this guard, N co-editors each write the whole doc on every edit (N-way amplification).
  // Local edits + local undo/redo (collabApply.remote === false) still PUT. (Cache above is unconditional.)
  if (collabApply.remote) return
  // Viewer/unknown access is fail-closed before any PUT. Store-level mutation guards mean this is
  // normally just a safety net for a server/external document refresh or a role changing mid-debounce.
  if (!roleCanEdit(s.canvasRole)) {
    clearTimeout(_saveTimer)
    if (!s.saved) useStore.setState({ saved: true })
    return
  }
  if (s.saved) useStore.setState({ saved: false })  // dirty → "saving…"
  clearTimeout(_saveTimer)
  _saveTimer = setTimeout(async () => {
    const state = useStore.getState()
    if (!roleCanEdit(state.canvasRole)) {
      useStore.setState({ saved: true })
      return
    }
    const doc = state.doc
    try {
      await api.saveCanvas(doc)  // PUT to the metadata DB (per-user, upsert)
      useStore.setState((st) => ({
        saved: true,
        kernelUp: true,  // a successful save confirms the kernel is reachable (clears the offline banner)
        accessDenied: false,  // a save went through → we clearly still have edit access
        files: st.files.map((f) => (f.id === doc.id ? { ...f, name: doc.name ?? f.name, version: doc.version } : f)),
      }))
    } catch (e) {
      if (e instanceof KernelError && (e.status === 401 || e.status === 403)) {
        // Permission/session rejection, NOT connectivity. Fail closed immediately; refreshFiles may
        // then recover the precise viewer/editor role without allowing another local edit first.
        if (!useStore.getState().accessDenied) useStore.getState().pushToast(
          e.status === 401
            ? 'Your session no longer permits editing. This canvas is now read-only.'
            : 'Your editing access changed. This canvas is now read-only and the last change was not saved.',
          'error',
        )
        rememberRole(useStore.getState().currentUser?.id, doc.id, null)
        useStore.setState({ saved: true, kernelUp: true, accessDenied: true, canvasRole: null, agentOpen: false })
        void useStore.getState().refreshFiles()
      } else {
        // offline: the localStorage cache still holds it; flag the kernel down so the banner shows
        useStore.setState({ saved: true, kernelUp: false })
      }
    }
  }, 400)
})

// Flush a pending local save on tab close, so an edit made inside the 400ms debounce isn't lost. This
// also closes the collab case where the originating editor — the only client that PUTs its own edit —
// disconnects mid-debounce. Fires ONLY when there's an unsaved LOCAL edit (saved === false); a client
// that merely merged peer edits stays saved:true, so it won't redundantly PUT. keepalive lets the
// request outlive the unloading page.
if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', () => {
    const state = useStore.getState()
    if (!_bootstrapped || state.saved || !roleCanEdit(state.canvasRole)) return
    const doc = state.doc
    try { localStorage.setItem(LS_KEY, JSON.stringify(doc)) } catch { /* quota */ }
    void api.saveCanvas(doc, true).catch(() => {})  // best-effort; can't await on unload
  })
}

// Refresh per-node output schema (column suggestions) a beat after a SCHEMA-RELEVANT change — the
// wiring or any node's config/kind/on-off. Node positions never affect columns, so dragging must
// NOT trigger a fetch: we compare a structure signature (positions excluded) after the cheap ref
// check. Debounced; the fetch itself is guarded against out-of-order responses (refreshSchemas).
let _schemaSeq = 0
let _schemaTimer: ReturnType<typeof setTimeout> | undefined
let _lastNodesRef: CanvasNode[] | undefined
let _lastEdgesRef: CanvasEdge[] | undefined
let _schemaSig: string | undefined
function structSig(doc: CanvasDoc): string {
  const nodes = doc.nodes.map((n) => `${n.id}:${n.type}:${n.data.disabled ? 1 : 0}${n.data.bypassed ? 1 : 0}:${JSON.stringify(n.data.config)}`).join('|')
  const edges = doc.edges.map((e) => `${e.source}>${e.sourceHandle ?? ''}>${e.target}>${e.targetHandle ?? ''}`).sort().join(',')
  return `${nodes}#${edges}`
}
useStore.subscribe((s) => {
  if (s.doc.nodes === _lastNodesRef && s.doc.edges === _lastEdgesRef) return  // cheap: nothing changed
  _lastNodesRef = s.doc.nodes; _lastEdgesRef = s.doc.edges
  if (!_bootstrapped) return
  const sig = structSig(s.doc)
  if (sig === _schemaSig) return  // refs changed but structure didn't (e.g. a drag) → no schema fetch
  _schemaSig = sig
  clearTimeout(_schemaTimer)
  _schemaTimer = setTimeout(() => { if (useStore.getState().kernelUp) void useStore.getState().refreshSchemas() }, 500)
})

// Map backend per-node run states onto each node's card status so the WHOLE graph animates during a
// run (queued → running → done) and a failing INTERMEDIATE node turns red — not just the target sink.
// Transient by design: only nodes that actually advanced get a new object (idle ticks return {} → no
// doc-identity change → no autosave/PUT churn), and terminal states settle to latest/failed/stale.
const _PERNODE_STATUS: Record<string, NodeStatus> = {
  queued: 'queued', running: 'running', done: 'latest', failed: 'failed', cancelled: 'stale',
}

// A user-facing toast message from a runner's raw error: drop the engine's exception-class noise
// ("BinderException: Binder Error: …") and the internal "Candidate bindings" line, keeping the
// "at '<node>':" attribution + the human "Hint:" line. The full raw error still shows in the run panel.
function cleanRunError(raw?: string | null): string {
  if (!raw) return 'Run failed'
  const lines = raw.split('\n').map((l) => l.trim()).filter((l) => l && !/^candidate bindings/i.test(l))
  if (!lines.length) return 'Run failed'
  lines[0] = lines[0].replace(/((?:at '[^']+': )?)[A-Za-z]*(?:Exception|Error): (?:[A-Za-z]+ Error: )?/, '$1')
  return lines.join(' — ')
}
// Flip every still-animating node (queued/running) to a terminal 'stale' — for when a run ends WITHOUT
// a final per-node snapshot to settle them: a user cancel (the optimistic pre-poll window) or the poll
// giving up because the kernel became unreachable. Without it an intermediate node animates forever.
function settleAnimatingNodes(set: (p: Partial<Store> | ((s: Store) => Partial<Store>)) => void) {
  set((s) => {
    let changed = false
    const nodes = s.doc.nodes.map((n) => {
      if (n.data.status === 'running' || n.data.status === 'queued') { changed = true; return { ...n, data: { ...n.data, status: 'stale' as NodeStatus } } }
      return n
    })
    return changed ? { doc: { ...s.doc, nodes } } : {}
  })
}

function applyPerNodeStatus(
  set: (p: Partial<Store> | ((s: Store) => Partial<Store>)) => void,
  perNode: RunStatus['perNode'],
) {
  const next = new Map<string, NodeStatus>()
  for (const p of perNode ?? []) { const ns = _PERNODE_STATUS[p.status]; if (ns) next.set(p.nodeId, ns) }
  if (!next.size) return
  set((s) => {
    let changed = false
    const nodes = s.doc.nodes.map((n) => {
      const ns = next.get(n.id)
      if (!ns || n.data.status === ns) return n
      changed = true
      return { ...n, data: { ...n.data, status: ns } }
    })
    return changed ? { doc: { ...s.doc, nodes } } : {}
  })
}

// On canvas open, recover normal active runs plus the latest durable profile per node/plan. Profiles
// include terminal results because one may finish while the canvas is closed; current-plan identity is
// checked again client-side before any result can enter the view.
function reattachRuns(get: () => Store, set: (p: Partial<Store> | ((s: Store) => Partial<Store>)) => void, canvasId: string) {
  const reattachGeneration = ++_reattachRunsGeneration
  const reattachUserId = _profileSubmissionUserId
  // Capture this canvas's known authority before any recovery request settles. Reading canvasRole later
  // could accidentally use the role of a different canvas after navigation.
  const recoveryCanCancel = roleCanEdit(get().canvasRole)
  // A recovery request started by loadDoc must never replace an explicit estimate/start the user
  // begins while either recovery endpoint is still in flight. Generations installed by this recovery
  // remain mergeable with its other endpoint, while every later local generation wins outright.
  const localIntentWatermark = _profileRequestGeneration
  const recoveredRequestGenerations = new Set<number>()
  const current = () => (
    reattachUserId !== null
    && _profileSubmissionUserId === reattachUserId
    && _reattachRunsGeneration === reattachGeneration
    && get().doc.id === canvasId
  )
  const recoveredAttemptTracked = (status: RunStatus) => Object.values(get().profileJobs).some(
    (job) => job.principalId === reattachUserId
      && !!job.status
      && sameProfileAttempt(job.status, status),
  )
  const superviseRecoveredIfDetached = (status: RunStatus) => {
    if ((status.status === 'queued' || status.status === 'running')
        && !recoveredAttemptTracked(status)) {
      superviseDetachedProfileCancellation(status, reattachUserId, recoveryCanCancel)
    }
  }
  const currentPlanByNode = new Map<string, Promise<{ identity: string; digest: string }>>()

  const currentPlan = (nodeId: string) => {
    let pending = currentPlanByNode.get(nodeId)
    if (!pending) {
      const identity = profilePlanIdentity(get().doc, nodeId)
      const doc = get().doc
      pending = (async () => {
        for (let attempt = 0; ; attempt += 1) {
          try {
            const { planDigest: digest } = await api.profileIdentity(doc, nodeId)
            return { identity, digest }
          } catch (error) {
            if (!current() || identity !== profilePlanIdentity(get().doc, nodeId)
                || !retryableProfileRequest(error) || attempt >= PROFILE_RETRY_DELAYS_MS.length) throw error
            await wait(PROFILE_RETRY_DELAYS_MS[attempt])
          }
        }
      })()
      currentPlanByNode.set(nodeId, pending)
      // A later recovery response must be able to retry after a persistent failure; never retain a
      // rejected Promise as the node's identity authority.
      void pending.catch(() => {
        if (currentPlanByNode.get(nodeId) === pending) currentPlanByNode.delete(nodeId)
      })
    }
    return pending
  }

  type RecoveryVerification = 'verifying' | 'verified' | 'failed'
  const installRecoveredState = (
    st: RunStatus,
    verification: RecoveryVerification,
    error?: string,
  ): { installed: boolean; requestGeneration?: number; blockedByLocalIntent: boolean } => {
    const nodeId = st.targetNodeId!
    const attemptOrder = st.profileAttemptOrder!
    let installed = false
    let installedGeneration: number | undefined
    let blockedByLocalIntent = false
    set((s: Store) => {
      if (_reattachRunsGeneration !== reattachGeneration || s.doc.id !== canvasId
          || s.currentUser?.id !== reattachUserId) return {}
      const existingJob = s.profileJobs[nodeId]
      if (existingJob && existingJob.requestGeneration > localIntentWatermark
          && !recoveredRequestGenerations.has(existingJob.requestGeneration)) {
        blockedByLocalIntent = true
        return {}
      }
      const existing = existingJob?.status
      const existingOrder = existing?.profileAttemptOrder
      let requestGeneration: number
      const sameAttempt = !!existing && sameProfileAttempt(existing, st)
      if (sameAttempt) {
        if (!profileStatusCanAdvance(existing!, st)) return {}
        // Once verified, a duplicate provisional response must never strip trusted statistics.
        if (existingJob!.identityVerified === true && verification !== 'verified') return {}
        requestGeneration = existingJob!.requestGeneration
      } else {
        // Attempt order is scoped to a plan digest. A newer stale-plan attempt must never suppress a
        // lower-order result that matches the graph's current server digest.
        if (existingJob?.identityVerified === true && verification !== 'verified') return {}
        if (existing && existing.planDigest === st.planDigest && Number.isSafeInteger(existingOrder)) {
          if (existingOrder! > attemptOrder || existingOrder === attemptOrder) return {}
        }
        requestGeneration = ++_profileRequestGeneration
      }
      const status = verification === 'verified' ? st : sanitizeUnverifiedProfileStatus(st)
      const active = status.status === 'queued' || status.status === 'running'
      const cancelRequested = sameAttempt && existingJob?.cancelRequested === true
      installed = true
      installedGeneration = requestGeneration
      return { profileJobs: { ...s.profileJobs, [nodeId]: {
        canvasId, nodeId, principalId: reattachUserId!, canCancel: recoveryCanCancel,
        planIdentity: profilePlanIdentity(s.doc, nodeId),
        planDigest: st.planDigest ?? undefined,
        requestGeneration,
        status,
        identityVerified: verification === 'verified',
        cancelRequested,
        phase: verification === 'verified'
          ? active && cancelRequested ? 'cancelling' : profilePhase(status)
          : verification === 'verifying' ? 'verifying' : 'failed',
        error: verification === 'failed'
          ? error
          : verification === 'verified' ? status.error ?? undefined : undefined,
      } } }
    })
    if (installed && installedGeneration !== undefined) recoveredRequestGenerations.add(installedGeneration)
    return { installed, requestGeneration: installedGeneration, blockedByLocalIntent }
  }

  const discardStaleRecoveredAttempt = (st: RunStatus) => {
    const nodeId = st.targetNodeId!
    set((s: Store) => {
      if (_reattachRunsGeneration !== reattachGeneration || s.doc.id !== canvasId
          || s.currentUser?.id !== reattachUserId) return {}
      const existing = s.profileJobs[nodeId]
      if (!existing?.status || existing.identityVerified === true
          || !sameProfileAttempt(existing.status, st)) return {}
      const next = { ...s.profileJobs }
      delete next[nodeId]
      return { profileJobs: next }
    })
  }

  const installProfile = async (st: RunStatus) => {
    const nodeId = st.targetNodeId
    const attemptOrder = st.profileAttemptOrder
    if (st.jobType !== 'profile' || !nodeId
        || !Number.isSafeInteger(attemptOrder) || attemptOrder! < 1) return
    if (!current() || !get().doc.nodes.some((node) => node.id === nodeId)) {
      superviseRecoveredIfDetached(st)
      return
    }
    const provisional = installRecoveredState(st, 'verifying')
    if (provisional.blockedByLocalIntent) {
      superviseRecoveredIfDetached(st)
      return
    }
    let planIdentity: string
    let planDigest: string
    try {
      ({ identity: planIdentity, digest: planDigest } = await currentPlan(nodeId))
    } catch (error) {
      if (!current()) {
        superviseRecoveredIfDetached(st)
        return
      }
      const failed = installRecoveredState(
        st,
        'failed',
        `Could not verify the recovered full profile. Statistics are hidden${error instanceof Error && error.message ? `: ${error.message}` : '.'}`,
      )
      if (!failed.installed) superviseRecoveredIfDetached(st)
      return
    }
    if (!current() || planIdentity !== profilePlanIdentity(get().doc, nodeId)) {
      superviseRecoveredIfDetached(st)
      return
    }
    if (!st.planDigest || st.planDigest !== planDigest) {
      discardStaleRecoveredAttempt(st)
      superviseRecoveredIfDetached(st)
      return
    }
    const { installed, requestGeneration } = installRecoveredState(st, 'verified')
    if (installed && requestGeneration !== undefined && current()
        && (st.status === 'queued' || st.status === 'running')) {
      pollProfile(
        get, set, nodeId, st.runId, requestGeneration, reattachUserId!, recoveryCanCancel,
      )
    } else if (!installed || !current()) {
      superviseRecoveredIfDetached(st)
    }
  }

  // These requests intentionally settle independently: a hung recovery surface must not block the other.
  void api.activeRuns(canvasId).then((statuses) => {
    if (!current()) {
      for (const st of statuses) {
        if (st.jobType === 'profile') superviseRecoveredIfDetached(st)
      }
      return
    }
    for (const st of statuses) {
      if (st.jobType === 'profile') {
        void installProfile(st).catch(() => {})
        continue
      }
      const nodeId = st.targetNodeId
      if (!current() || !nodeId || !get().doc.nodes.some((node) => node.id === nodeId)) continue
      set((s: Store) => {
        if (_reattachRunsGeneration !== reattachGeneration || s.doc.id !== canvasId) return {}
        return { runs: { ...s.runs, [nodeId]: { phase: 'running' as const, status: st } } }
      })
      if (current()) pollRun(get, set, nodeId, st.runId, reattachGeneration)
    }
  }).catch(() => { /* profile projection may still recover; leave current state untouched */ })

  void api.profileJobs(canvasId).then((statuses) => {
    if (!current()) {
      for (const st of statuses) superviseRecoveredIfDetached(st)
      return
    }
    for (const st of statuses) void installProfile(st).catch(() => {})
  }).catch(() => { /* active profiles remain the provisional in-flight fallback */ })
}

const _profilePolling = new Map<string, {
  token: symbol
  requestGeneration: number
  principalId: string
}>()

function pollProfile(get: () => Store, set: (p: Partial<Store> | ((s: Store) => Partial<Store>)) => void,
                     nodeId: string, runId: string, requestGeneration: number,
                     principalId: string, canCancel: boolean) {
  if (_profileSubmissionUserId !== principalId) return
  const existing = _profilePolling.get(runId)
  if (existing?.principalId !== undefined && existing.principalId !== principalId) return
  if (existing?.requestGeneration === requestGeneration) return
  const initialStatus = get().profileJobs[nodeId]?.status
  if (!initialStatus || initialStatus.runId !== runId
      || (initialStatus.status !== 'queued' && initialStatus.status !== 'running')) return
  const token = Symbol(runId)
  _profilePolling.set(runId, { token, requestGeneration, principalId })
  const ownsPoll = () => _profilePolling.get(runId)?.token === token
  const stopPolling = () => { if (ownsPoll()) _profilePolling.delete(runId) }
  const superviseIfDetached = (status: RunStatus) => {
    const tracked = Object.values(get().profileJobs).some(
      (job) => job.principalId === principalId
        && !!job.status
        && sameProfileAttempt(job.status, status),
    )
    if (!tracked) superviseDetachedProfileCancellation(status, principalId, canCancel)
  }
  let failures = 0
  let projectionFailures = 0
  const tick = async () => {
    if (!ownsPoll()) return
    if (_profileSubmissionUserId !== principalId) {
      stopPolling()
      return
    }
    const job = get().profileJobs[nodeId]
    if (!job || job.principalId !== principalId
        || job.requestGeneration !== requestGeneration || job.status?.runId !== runId) {
      superviseIfDetached(initialStatus)
      stopPolling()
      return
    }
    if (job.status.status === 'done' || job.status.status === 'failed' || job.status.status === 'cancelled') {
      stopPolling()
      return
    }
    if (!profileJobIsCurrent(job, get().doc, nodeId)) {
      superviseDetachedProfileCancellation(job.status, principalId, canCancel)
      stopPolling()
      return
    }
    let status: RunStatus
    try {
      status = await api.runStatus(runId)
      failures = 0
    } catch (e) {
      if (!ownsPoll()) return
      if (_profileSubmissionUserId !== principalId) {
        stopPolling()
        return
      }
      if (++failures <= 6) { setTimeout(tick, 800); return }
      const current = get().profileJobs[nodeId]
      if (current?.requestGeneration === requestGeneration && current.status?.runId === runId) {
        set((s) => ({ profileJobs: { ...s.profileJobs, [nodeId]: {
          ...(s.profileJobs[nodeId]!), phase: 'failed', error: (e as Error).message || 'Lost track of full profile',
        } } }))
      }
      stopPolling()
      return
    }
    if (!ownsPoll()) return
    if (_profileSubmissionUserId !== principalId) {
      stopPolling()
      return
    }
    const current = get().profileJobs[nodeId]
    if (!current || current.principalId !== principalId
        || current.requestGeneration !== requestGeneration || current.status?.runId !== runId
        || !profileJobIsCurrent(current, get().doc, nodeId)) {
      const exactTerminal = status.runId === runId && terminalRunStatus(status)
        && (status.jobType === 'run' || sameProfileAttempt(job.status, status))
      if (!exactTerminal) superviseIfDetached(job.status)
      stopPolling()
      return
    }
    if (!sameProfileAttempt(current.status, status)) {
      // Once globally bounded RunState detail is pruned, GET /run/{id} intentionally returns only a
      // compact terminal fence. The independent profile projection retains the exact identity + stats;
      // recover it instead of replacing the profile with a synthetic generic terminal document.
      if (status.jobType === 'run' && status.runId === runId
          && profileStatusRank(status.status) === 2) {
        try {
          const projected = (await api.profileJobs(current.canvasId)).find(
            (candidate) => sameProfileAttempt(current.status!, candidate),
          )
          if (!ownsPoll()) return
          if (_profileSubmissionUserId !== principalId) {
            stopPolling()
            return
          }
          const latest = get().profileJobs[nodeId]
          if (!latest || latest.requestGeneration !== requestGeneration
              || latest.status?.runId !== runId || !projected) {
            if (latest?.requestGeneration !== requestGeneration || latest?.status?.runId !== runId) {
              stopPolling()
              return
            }
            set((s) => ({ profileJobs: { ...s.profileJobs, [nodeId]: {
              ...(s.profileJobs[nodeId]!), phase: 'failed',
              error: 'Full profile status identity changed unexpectedly',
            } } }))
            stopPolling()
            return
          }
          status = projected
          projectionFailures = 0
        } catch (e) {
          if (!ownsPoll()) return
          if (++projectionFailures <= 6) { setTimeout(tick, 800); return }
          set((s) => ({ profileJobs: { ...s.profileJobs, [nodeId]: {
            ...(s.profileJobs[nodeId]!), phase: 'failed',
            error: (e as Error).message || 'Lost the durable full-profile projection',
          } } }))
          stopPolling()
          return
        }
      } else {
        set((s) => ({ profileJobs: { ...s.profileJobs, [nodeId]: {
          ...(s.profileJobs[nodeId]!), phase: 'failed',
          error: 'Full profile status identity changed unexpectedly',
        } } }))
        stopPolling()
        return
      }
    }
    if (!sameProfileAttempt(current.status, status)) {
      set((s) => ({ profileJobs: { ...s.profileJobs, [nodeId]: {
        ...(s.profileJobs[nodeId]!), phase: 'failed',
        error: 'Full profile status identity changed unexpectedly',
      } } }))
      stopPolling()
      return
    }
    if (!profileStatusCanAdvance(current.status, status)) {
      setTimeout(tick, 300)
      return
    }
    const identityVerified = current.identityVerified !== false
    const storedStatus = identityVerified ? status : sanitizeUnverifiedProfileStatus(status)
    const active = status.status === 'queued' || status.status === 'running'
    const phase = !identityVerified
      ? active
        ? current.cancelRequested === true ? 'cancelling' : 'failed'
        : status.status === 'cancelled' ? 'cancelled' : 'failed'
      : current.cancelRequested === true && active
        ? 'cancelling'
        : profilePhase(status)
    set((s) => ({ profileJobs: { ...s.profileJobs, [nodeId]: {
      ...(s.profileJobs[nodeId]!), status: storedStatus, phase,
      error: !identityVerified && phase !== 'cancelled'
        ? s.profileJobs[nodeId]?.error
        : phase === 'cancelling' ? s.profileJobs[nodeId]?.error : status.error ?? undefined,
    } } }))
    if (status.status === 'done' || status.status === 'failed' || status.status === 'cancelled') {
      stopPolling()
      return
    }
    setTimeout(tick, 300)
  }
  void tick()
}

const _polling = new Map<string, { token: symbol; reattachGeneration?: number }>()

function pollRun(get: () => Store, set: (p: Partial<Store> | ((s: Store) => Partial<Store>)) => void,
                 nodeId: string, runId: string, reattachGeneration?: number) {
  const existing = _polling.get(runId)
  if (existing && (reattachGeneration === undefined
      || existing.reattachGeneration === reattachGeneration)) return
  const token = Symbol(runId)
  _polling.set(runId, { token, reattachGeneration })
  const ownsPoll = () => _polling.get(runId)?.token === token
  const stopPolling = () => { if (ownsPoll()) _polling.delete(runId) }
  let fails = 0
  const tick = async () => {
    if (!ownsPoll()) return
    if (reattachGeneration !== undefined && _reattachRunsGeneration !== reattachGeneration) {
      stopPolling()
      return
    }
    // stop polling if the node was deleted mid-run (don't re-insert a runs entry for it)
    if (!get().doc.nodes.some((n) => n.id === nodeId)) { stopPolling(); return }
    let status: RunStatus
    try {
      status = await api.runStatus(runId)
      fails = 0
    } catch {
      if (reattachGeneration !== undefined && _reattachRunsGeneration !== reattachGeneration) {
        stopPolling()
        return
      }
      // a transient blip (network hiccup / brief kernel restart) must not strand the node spinning
      // forever — retry a few times with backoff, then give up and surface it instead of hanging.
      if (++fails <= 6) { setTimeout(tick, 800); return }
      set((s: Store) => ({ runs: { ...s.runs, [nodeId]: { ...(s.runs[nodeId] ?? { phase: 'idle' as const }), phase: 'idle' } } }))
      get().updateData(nodeId, { status: 'stale' })
      settleAnimatingNodes(set)  // no final status will arrive — clear every still-animating node, not just the target
      get().pushToast('Lost track of the run — the kernel became unreachable', 'error')
      stopPolling()
      return
    }
    if (!ownsPoll()) return
    if (reattachGeneration !== undefined && _reattachRunsGeneration !== reattachGeneration) {
      stopPolling()
      return
    }
    set((s: Store) => ({ runs: { ...s.runs, [nodeId]: { ...(s.runs[nodeId] ?? { phase: 'running' as const }), status } } }))
    applyPerNodeStatus(set, status.perNode)  // animate every node on the canvas, not just the target
    if (status.status === 'done' || status.status === 'failed' || status.status === 'cancelled') {
      const phase = status.status === 'done' ? 'done' : status.status === 'failed' ? 'failed' : 'idle'
      // rowsProcessed is execution work, not result cardinality. A named multi-output result is
      // summarized by its number of outputs; a single output uses only measured result rows.
      const resultRows = status.totalRows
        ?? (status.outputs.length === 1 ? status.outputs[0]?.rows ?? undefined : undefined)
      const resultOutputCount = status.outputs.length > 1 ? status.outputs.length : undefined
      set((s: Store) => ({ runs: { ...s.runs, [nodeId]: { ...(s.runs[nodeId] ?? { phase } as any), status, phase } } }))
      if (status.status === 'failed') get().pushToast(cleanRunError(status.error), 'error')
      const g = get()
      g.updateData(nodeId, {
        status: status.status === 'done' ? 'latest' : status.status === 'failed' ? 'failed' : 'stale',
        lastRun: status.status === 'done'
          ? {
              ...(resultRows !== undefined ? { rows: resultRows } : {}),
              ...(resultOutputCount !== undefined ? { outputCount: resultOutputCount } : {}),
              ms: status.ms,
              placement: status.placement,
            }
          : undefined,
      })
      if (status.status === 'done') {
        // snapshot a version (time-travel, FR-C5)
        const node = g.doc.nodes.find((n) => n.id === nodeId)
        if (node) {
          const version: NodeVersion = {
            id: `v_${Math.floor(performance.now())}`,
            ts: Date.now(),
            rows: resultRows,
            outputCount: resultOutputCount,
            label: resultOutputCount !== undefined
              ? `run · ${resultOutputCount} outputs`
              : resultRows !== undefined
                ? `run · ${resultRows} ${resultRows === 1 ? 'row' : 'rows'}`
                : 'run · result',
            config: { ...node.data.config },
          }
          g.updateData(nodeId, { history: [...(node.data.history ?? []), version] })
        }
        void g.refreshCatalog()
      }
      stopPolling()
      return
    }
    setTimeout(tick, 300)
  }
  setTimeout(tick, 200)
}
