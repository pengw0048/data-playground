import { create } from 'zustand'
import type { WireType } from '../theme/tokens'
import type {
  CanvasDoc, CanvasEdge, CanvasNode, CanvasParameterBinding, CanvasParameterDeclaration,
  NodeConfig, NodeData, NodeStatus, NodeVersion,
} from '../types/graph'
import type {
  CanvasTransformReference, CatalogTable, InputDrift, KernelInfo, ProcessorDescriptor, ProfileResult, RunEstimate,
  RunInputManifestItem, RunStatus, SampleResult, WriteAdmission,
} from '../types/api'
import { getSpec, nodeOutputs } from '../nodes/registry'
import { getBackendSpec, registerGenericNodes, nodeInvalidReason, numericDraftInvalidReason } from '../nodes/generic'
import type { SchemaMap } from '../nodes/schema'
import { parseHash } from '../router'
import { exampleDoc } from '../examples'
import {
  api, KernelError, setApiUser,
  type AgentBackendNode, type AgentBackendEdge, type CanvasFile, type CanvasRole, type DpUser,
} from '../api/client'
import { crdtUndo, crdtUndoActive, collabApply } from '../collab/undo'
import {
  canvasDocsEqual, canvasEditableContentEqual, deleteCanvasDraft, readCanvasDrafts, writeCanvasDraft,
  type LocalCanvasDraft,
} from './canvasDrafts'
import {
  isPristineExampleReplacement,
  isSameExampleReplacementSnapshot,
  type ExampleCreationIntent,
  type ExampleReplacementSnapshot,
} from './exampleReplacement'
import { confirmedLocalMode, LAST_USER_KEY } from '../localIdentity'

export type PanelKind = 'data' | 'run' | 'history' | 'lineage' | 'section'

export type CanvasPersistence = 'remote' | 'local-draft'

export type CanvasCreationResult =
  | { ok: true; canvasId: string; persistence: CanvasPersistence }
  | { ok: false }

const OPEN_KEY = (uid: string) => `dp-open-${uid}`  // last-opened file per user
const ROLE_KEY = (userId: string, canvasId: string) => `dp-canvas-role-${encodeURIComponent(userId)}-${encodeURIComponent(canvasId)}`
const PREVIEW_BINDINGS_KEY = (userId: string, canvasId: string) => `dp-preview-bindings-${encodeURIComponent(userId)}-${encodeURIComponent(canvasId)}`

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
let _nodeRevealGeneration = 0     // consumed requests still need unique IDs for later routes
let _viewportFitGeneration = 0    // example fits are one-shot even when the same Canvas is reused
const _draftSyncInFlight = new Set<string>()
// True only while loadDoc synchronously installs an in-memory settled copy. The autosave subscriber
// still refreshes the browser cache, but must not PUT that presentation-only normalization back into
// the authoritative canvas or create a misleading Version history snapshot.
let _settlingLoadedDoc = false
let _acceptingServerVersion = false

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

/**
 * Column merges have a separate certified admission protocol.  Keeping this predicate next to
 * the generic run entry points prevents alternate UI affordances (node play, inspector play, or
 * rerun-all) from accidentally sending that configuration through the ordinary Write runner.
 */
export function hasConfiguredMergeColumnsWrite(doc: CanvasDoc, id: string): boolean {
  const node = doc.nodes.find((item) => item.id === id)
  if (node?.type !== 'write') return false
  const value = node.data.config.mergeColumns
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const rules = (value as { rules?: unknown }).rules
  return Array.isArray(rules) && rules.length > 0
}

export function hasConfiguredUpsertWrite(doc: CanvasDoc, id: string): boolean {
  const node = doc.nodes.find((item) => item.id === id)
  if (node?.type !== 'write') return false
  const value = node.data.config.keyedUpsert
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const keys = (value as { keys?: unknown }).keys
  return Array.isArray(keys) && keys.length > 0
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

/** Stable logical identity for one promoted Canvas node. Display text is deliberately excluded: two
 * same-title cells stay distinct, while a rename or a response-loss retry reuses the same identity. */
export function promotedTransformKey(canvasId: string, nodeId: string): string {
  const identity = JSON.stringify([canvasId, nodeId])
  let hash = 0xcbf29ce484222325n
  for (const byte of new TextEncoder().encode(identity)) {
    hash ^= BigInt(byte)
    hash = BigInt.asUintN(64, hash * 0x100000001b3n)
  }
  return `canvas-node.${hash.toString(16).padStart(16, '0')}`
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
  parameterBindings?: CanvasParameterBinding[]
  requestGeneration: number
  loading?: boolean
  result?: SampleResult
  error?: string
  offset?: number
}

export interface PreviewBindingState {
  canvasId: string
  nodeId: string
  portId?: string
  planIdentity: string
  parameterBindings?: CanvasParameterBinding[]
  inputManifest: RunInputManifestItem[]
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

export function profilePlanIdentity(doc: CanvasDoc, nodeId: string, portId?: string): string {
  return targetExecutionPlanIdentity(doc, nodeId, portId)
}

export function parameterBindingsIdentity(bindings?: CanvasParameterBinding[]): string {
  return JSON.stringify([...(bindings ?? [])]
    .sort((left, right) => compareIdentityText(left.name, right.name))
    .map((binding) => canonicalIdentityValue(binding)))
}

export function profileJobKey(nodeId: string, portId?: string): string {
  return portId === undefined ? nodeId : JSON.stringify([nodeId, portId])
}

function resolvedProfilePort(doc: CanvasDoc, nodeId: string, portId?: string): string | undefined {
  if (portId !== undefined) return portId
  const node = doc.nodes.find((candidate) => candidate.id === nodeId)
  const outputs = node ? nodeOutputs(node) : []
  if (outputs.length === 1) return outputs[0].id
  return node && node.type !== 'section' && outputs.length === 0 ? 'out' : undefined
}

function profileJobKeyForDoc(doc: CanvasDoc, nodeId: string, portId?: string): string {
  const node = doc.nodes.find((candidate) => candidate.id === nodeId)
  return node && nodeOutputs(node).length <= 1 ? nodeId : profileJobKey(nodeId, portId)
}

export function previewIsCurrent(preview: PreviewState, doc: CanvasDoc, nodeId: string, portId = preview.portId): boolean {
  return preview.canvasId === doc.id
    && preview.nodeId === nodeId
    && preview.portId === portId
    && doc.nodes.some((node) => node.id === nodeId)
    && preview.planIdentity === previewPlanIdentity(doc, nodeId, portId)
}

function previewBindingIsCurrent(binding: PreviewBindingState, doc: CanvasDoc, nodeId: string,
                                 parameterBindings?: CanvasParameterBinding[]): boolean {
  return binding.canvasId === doc.id
    && binding.nodeId === nodeId
    && doc.nodes.some((node) => node.id === nodeId)
    && binding.planIdentity === previewPlanIdentity(doc, nodeId, binding.portId)
    && parameterBindingsIdentity(binding.parameterBindings) === parameterBindingsIdentity(parameterBindings)
}

function readPreviewBindings(userId: string | undefined, doc: CanvasDoc): Record<string, PreviewBindingState> {
  if (!userId || !doc.id) return {}
  try {
    const parsed = JSON.parse(localStorage.getItem(PREVIEW_BINDINGS_KEY(userId, doc.id)) ?? '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return Object.fromEntries(Object.entries(parsed).filter(([nodeId, value]) => {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return false
      const binding = value as PreviewBindingState
      return typeof binding.canvasId === 'string'
        && binding.nodeId === nodeId
        && (binding.portId === undefined || typeof binding.portId === 'string')
        && typeof binding.planIdentity === 'string'
        && Array.isArray(binding.parameterBindings)
        && binding.parameterBindings.every((item) => item && typeof item === 'object'
          && typeof item.name === 'string' && Object.prototype.hasOwnProperty.call(item, 'value'))
        && Array.isArray(binding.inputManifest)
        && binding.inputManifest.every((item) => item && typeof item === 'object'
          && ['node_id', 'dataset_id', 'revision_id', 'provider', 'resolved_at']
            .every((field) => typeof item[field as keyof RunInputManifestItem] === 'string'))
        && previewBindingIsCurrent(binding, doc, nodeId, binding.parameterBindings)
    })) as Record<string, PreviewBindingState>
  } catch {
    return {}
  }
}

function writePreviewBindings(userId: string | undefined, canvasId: string,
  bindings: Record<string, PreviewBindingState>): void {
  if (!userId || !canvasId) return
  try { localStorage.setItem(PREVIEW_BINDINGS_KEY(userId, canvasId), JSON.stringify(bindings)) } catch { /* storage unavailable */ }
}

function currentPreviewBinding(state: Store, nodeId: string): PreviewBindingState | undefined {
  const live = state.previews[nodeId]
  const manifest = live?.result?.inputManifest
  const parameterBindings = state.runs[nodeId]?.parameterBindings
  if (live && manifest && previewIsCurrent(live, state.doc, nodeId)
      && parameterBindingsIdentity(live.parameterBindings) === parameterBindingsIdentity(parameterBindings)) {
    return {
      canvasId: live.canvasId, nodeId, portId: live.portId,
      planIdentity: live.planIdentity, parameterBindings: live.parameterBindings, inputManifest: manifest,
    }
  }
  const retained = state.previewBindings[nodeId]
  return retained && previewBindingIsCurrent(retained, state.doc, nodeId, parameterBindings) ? retained : undefined
}

function writeAdmissionFingerprint(doc: CanvasDoc, parameterBindings?: CanvasParameterBinding[]): string {
  const { version: _version, ...executionDoc } = doc
  return JSON.stringify({
    ...executionDoc,
    nodes: doc.nodes.map((node) => {
      const { status: _status, ...data } = node.data
      return { ...node, data }
    }),
    parameterBindings: parameterBindings ?? [],
  })
}

function sameInputManifest(
  left: RunInputManifestItem[] | undefined,
  right: RunInputManifestItem[] | undefined,
): boolean {
  if (!left || !right || left.length !== right.length) return false
  return left.every((item, index) => {
    const other = right[index]
    return item.node_id === other.node_id
      && item.dataset_id === other.dataset_id
      && item.revision_id === other.revision_id
      && item.provider === other.provider
      && item.resolved_at === other.resolved_at
  })
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
  phase: 'idle' | 'parameters' | 'estimating' | 'estimated' | 'confirm' | 'drift' | 'running' | 'done' | 'failed'
  error?: string
  inputDrift?: InputDrift
  driftInputManifest?: RunInputManifestItem[]
  writeAdmission?: WriteAdmission
  writeSubmissionId?: string
  writeAdmissionFingerprint?: string
  parameterBindings?: CanvasParameterBinding[]
  parametersReady?: boolean
  parameterContractFingerprint?: string
  parameterContinuation?: { kind: 'run' | 'estimate' } | { kind: 'profile'; portId?: string }
}

export function targetParameterDeclarations(doc: CanvasDoc, targetNodeId: string): CanvasParameterDeclaration[] {
  const incoming = new Map<string, string[]>()
  for (const edge of doc.edges) incoming.set(edge.target, [...(incoming.get(edge.target) ?? []), edge.source])
  const selected = new Set<string>()
  const pending = [targetNodeId]
  while (pending.length) {
    const id = pending.pop()!
    if (selected.has(id)) continue
    selected.add(id)
    pending.push(...(incoming.get(id) ?? []))
    for (const child of doc.nodes) if (child.parentId === id) pending.push(child.id)
  }
  const used = new Set<string>()
  const visit = (value: unknown) => {
    if (value && typeof value === 'object') {
      if (!Array.isArray(value) && Object.keys(value).length === 1
          && typeof (value as { parameterRef?: unknown }).parameterRef === 'string') {
        used.add((value as { parameterRef: string }).parameterRef)
      } else if (Array.isArray(value)) value.forEach(visit)
      else Object.values(value as Record<string, unknown>).forEach(visit)
    }
  }
  for (const node of doc.nodes) if (selected.has(node.id)) visit(node.data.config)
  return (doc.parameters ?? []).filter((item) => used.has(item.name))
}

type ParameterRefUse = {
  nodeId: string
  path: string[]
  expectedTypes: CanvasParameterDeclaration['type'][] | null
}

function parameterRefUses(doc: CanvasDoc, name: string): ParameterRefUse[] {
  const uses: ParameterRefUse[] = []
  const visit = (node: CanvasNode, value: unknown, path: string[]) => {
    if (!value || typeof value !== 'object') return
    if (!Array.isArray(value) && Object.keys(value).length === 1
        && (value as { parameterRef?: unknown }).parameterRef === name) {
      let expectedTypes: ParameterRefUse['expectedTypes'] = null
      if (node.type === 'source' && path.length === 1 && path[0] === 'datasetRef') expectedTypes = ['dataset']
      else if (path.length === 1) {
        const parameter = getBackendSpec(node.type)?.params.find((item) => item.name === path[0])
        expectedTypes = parameter?.type === 'int' ? ['integer']
          : parameter?.type === 'float' ? ['float', 'integer']
            : parameter?.type === 'bool' ? ['boolean']
              : parameter && parameter.type !== 'columns' && parameter.type !== 'code' ? ['string'] : null
      }
      uses.push({ nodeId: node.id, path, expectedTypes })
      return
    }
    if (Array.isArray(value)) value.forEach((item, index) => visit(node, item, [...path, String(index)]))
    else Object.entries(value as Record<string, unknown>)
      .forEach(([key, item]) => visit(node, item, [...path, key]))
  }
  for (const node of doc.nodes) visit(node, node.data.config, [])
  return uses
}

function rewriteParameterRef(value: unknown, renames: Map<string, string>): unknown {
  if (!value || typeof value !== 'object') return value
  if (!Array.isArray(value) && Object.keys(value).length === 1
      && typeof (value as { parameterRef?: unknown }).parameterRef === 'string') {
    const current = (value as { parameterRef: string }).parameterRef
    return renames.has(current) ? { parameterRef: renames.get(current)! } : value
  }
  if (Array.isArray(value)) return value.map((item) => rewriteParameterRef(item, renames))
  return Object.fromEntries(Object.entries(value as Record<string, unknown>)
    .map(([key, item]) => [key, rewriteParameterRef(item, renames)]))
}

function parameterDeclarationMutation(doc: CanvasDoc, next: CanvasParameterDeclaration[]): {
  error: string | null
  renames: Map<string, string>
  changedTypes: Set<string>
} {
  const previous = doc.parameters ?? []
  const previousByName = new Map(previous.map((item) => [item.name, item]))
  const nextByName = new Map(next.map((item) => [item.name, item]))
  const removed = previous.filter((item) => !nextByName.has(item.name))
  const added = next.filter((item) => !previousByName.has(item.name))
  const renames = new Map<string, string>()
  if (removed.length === 1 && added.length === 1 && previous.length === next.length) {
    renames.set(removed[0].name, added[0].name)
  } else {
    for (const declaration of removed) {
      const uses = parameterRefUses(doc, declaration.name)
      if (uses.length) {
        return {
          error: `Cannot remove '${declaration.name}': ${uses.length} Canvas configuration ${uses.length === 1 ? 'field still references' : 'fields still reference'} it.`,
          renames, changedTypes: new Set(),
        }
      }
    }
  }
  const changedTypes = new Set<string>()
  for (const declaration of next) {
    const priorName = [...renames.entries()].find(([, renamed]) => renamed === declaration.name)?.[0]
      ?? declaration.name
    const prior = previousByName.get(priorName)
    if (!prior || prior.type === declaration.type) continue
    changedTypes.add(declaration.name)
    const uses = parameterRefUses(doc, prior.name)
    const incompatible = uses.find((use) => !use.expectedTypes?.includes(declaration.type))
    if (incompatible) {
      return {
        error: `Cannot change '${prior.name}' to ${declaration.type}: ${incompatible.nodeId}.${incompatible.path.join('.')} requires ${incompatible.expectedTypes?.join(' or ') ?? 'its existing type'}.`,
        renames, changedTypes,
      }
    }
  }
  return { error: null, renames, changedTypes }
}

function parameterRequestFingerprint(doc: CanvasDoc, targetNodeId: string,
                                     bindings?: CanvasParameterBinding[]): string {
  return JSON.stringify({
    plan: targetExecutionPlanIdentity(doc, targetNodeId),
    declarations: targetParameterDeclarations(doc, targetNodeId),
    bindings: bindings ?? [],
  })
}

export interface ProfileJobState {
  canvasId: string
  nodeId: string
  portId?: string
  // Every lifecycle request for this job is fenced to the user that created or recovered it.
  // A user transition stops polling/cancellation instead of replaying a run id under another session.
  principalId?: string
  // Authority is captured for this canvas when the job is started/recovered. Detached cleanup must
  // never consult the role of whichever canvas happens to be open later.
  canCancel?: boolean
  parameterBindings?: CanvasParameterBinding[]
  // Raw identity remains local for synchronous stale-result checks. The server-minted SHA-256 is the
  // durable authority used to bind a recovered result to the graph and source content it profiled.
  planIdentity: string
  planDigest?: string
  inputManifest?: RunInputManifestItem[] | null
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

export function profileJobIsCurrent(
  job: ProfileJobState, doc: CanvasDoc, nodeId: string, portId = job.portId,
): boolean {
  return job.canvasId === doc.id
    && job.nodeId === nodeId
    && job.portId === portId
    && doc.nodes.some((node) => node.id === nodeId)
    && job.planIdentity === profilePlanIdentity(doc, nodeId, portId)
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
    && existing.targetPortId === incoming.targetPortId
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
  portId: string | undefined,
  planDigest: string,
  submissionId: string,
  userId: string,
  inputManifest?: RunInputManifestItem[] | null,
  parameterBindings?: CanvasParameterBinding[],
): Promise<RunStatus> {
  for (let attempt = 0; ; attempt += 1) {
    if (_profileSubmissionUserId !== userId) {
      throw new KernelError(401, 'User changed while the full profile was being submitted')
    }
    try {
      return await (inputManifest
        ? parameterBindings?.length
          ? api.fullProfile(doc, nodeId, portId, planDigest, submissionId, true, inputManifest, parameterBindings)
          : api.fullProfile(doc, nodeId, portId, planDigest, submissionId, true, inputManifest)
        : parameterBindings?.length
          ? api.fullProfile(doc, nodeId, portId, planDigest, submissionId, true, undefined, parameterBindings)
          : api.fullProfile(doc, nodeId, portId, planDigest, submissionId, true))
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
  portId?: string
  planDigest: string
  inputManifest?: RunInputManifestItem[] | null
  parameterBindings?: CanvasParameterBinding[]
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
  targetPortId: string | null
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
  portId: string | undefined,
  planDigest: string,
): boolean {
  return status.jobType === 'profile'
    && status.targetNodeId === nodeId
    && status.targetPortId === portId
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
        targetPortId: status.targetPortId ?? null,
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
        && (status.targetPortId ?? null) === entry.identity.targetPortId
        && status.planDigest === entry.identity.planDigest
        && status.profileAttemptOrder === entry.identity.attemptOrder
    }
  }
  // A compact terminal fence can intentionally identify only runId + lifecycle after detail pruning.
  if (status.jobType === 'profile' && entry.identity) {
    return (status.targetNodeId ?? null) === entry.identity.targetNodeId
      && (status.targetPortId ?? null) === entry.identity.targetPortId
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
        status = await (entry.inputManifest
          ? entry.parameterBindings?.length
            ? api.fullProfile(
              entry.doc, entry.nodeId, entry.portId, entry.planDigest, entry.submissionId, true,
              entry.inputManifest, entry.parameterBindings,
            )
            : api.fullProfile(
              entry.doc, entry.nodeId, entry.portId, entry.planDigest, entry.submissionId, true,
              entry.inputManifest,
            )
          : entry.parameterBindings?.length
            ? api.fullProfile(
              entry.doc, entry.nodeId, entry.portId, entry.planDigest, entry.submissionId, true,
              undefined, entry.parameterBindings,
            )
            : api.fullProfile(
              entry.doc, entry.nodeId, entry.portId, entry.planDigest, entry.submissionId, true,
            ))
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
      if (!validProfileSubmissionStatus(
        status, entry.nodeId, entry.portId, entry.planDigest,
      )) {
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

export interface NodeRevealRequest { id: number; canvasId: string; nodeId: string }
export interface CanvasViewportFitRequest { id: number; canvasId: string; documentIdentity: string }

// Fit identity is deliberately geometry-only: transient run badges may settle while React Flow is
// measuring, but a different node set or position must never consume an example's viewport request.
export function canvasViewportDocumentIdentity(doc: CanvasDoc): string {
  return JSON.stringify([
    doc.id,
    doc.version,
    doc.nodes.map((node) => [node.id, node.type, node.parentId ?? null, node.position.x, node.position.y]),
  ])
}

interface Store {
  doc: CanvasDoc
  canvasRole: CanvasRole | null     // authoritative role for the open canvas; null fails closed
  kernelInfo: KernelInfo | null
  kernelUp: boolean
  accessDenied: boolean  // server rejected the save with 401/403 (session/access changed) — NOT offline
  catalog: CatalogTable[]
  processors: ProcessorDescriptor[]
  canvasTransformReferences: CanvasTransformReference[]
  specsVersion: number
  schemas: SchemaMap               // per-node, per-output-port columns; null port entry = untyped
  sizes: Record<string, { rows: number | null; confidence: string }>  // per-node size estimate (card hint)

  selectedId: string | null        // primary selection (drives panels)
  selectedIds: string[]            // full multi-selection (box/shift-select)
  nodeRevealRequest: NodeRevealRequest | null // URL-originated only; Canvas consumes it without autosaving
  viewportFitRequest: CanvasViewportFitRequest | null // successful example open only; consumed once after measurement
  openPanels: Record<string, PanelKind>
  previews: Record<string, PreviewState>
  previewBindings: Record<string, PreviewBindingState>
  runs: Record<string, RunState>
  profileJobs: Record<string, ProfileJobState>
  past: CanvasDoc[]
  future: CanvasDoc[]
  saved: boolean          // auto-save state (localStorage), shown subtly in the top bar
  serverVersion: number | null
  currentDraftId: string | null
  localDrafts: LocalCanvasDraft[]
  draftStorageErrors: string[]
  // Set only after bootstrap has authoritatively established that this principal has neither a
  // remote Canvas nor a recoverable local draft.  The Workspace consumes it as the first-run choice.
  firstRunChoice: boolean
  numericParamDrafts: Record<string, Record<string, string>>  // invalid/pending text; never persisted

  agentOpen: boolean
  agentLog: AgentMsg[]

  // -- graph mutation --
  setNodes: (nodes: CanvasNode[]) => void
  setEdges: (edges: CanvasEdge[]) => void
  addNode: (kind: string, position: { x: number; y: number }, config?: Partial<NodeConfig>, title?: string) => CanvasNode | null
  setParent: (id: string, parentId: string | null, position: { x: number; y: number }) => void
  updateConfig: (id: string, patch: Partial<NodeConfig>) => void
  setNumericParamDraft: (id: string, param: string, text: string | undefined) => void
  updateData: (id: string, patch: Partial<NodeData>) => void
  removeNode: (id: string) => void
  connect: (edge: CanvasEdge) => void
  removeEdge: (id: string) => void
  select: (id: string | null) => void
  requestNodeReveal: (canvasId: string, nodeId: string) => void
  acknowledgeNodeReveal: (requestId: number) => void
  clearNodeReveal: () => void
  requestViewportFit: (doc: CanvasDoc) => void
  acknowledgeViewportFit: (requestId: number) => void
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
  runPreview: (id: string, offset?: number, portId?: string, refreshLatest?: boolean) => Promise<void>
  refreshPreviewInputs: (id: string) => Promise<void>
  requestRun: (id: string) => Promise<void>
  setRunParameterBinding: (id: string, binding: CanvasParameterBinding) => void
  clearRunParameterBinding: (id: string, name: string) => void
  editRunParameters: (id: string) => void
  submitRunParameters: (id: string) => Promise<void>
  estimate: (id: string) => Promise<void>
  prepareWrite: (id: string) => Promise<WriteAdmission | undefined>
  run: (id: string, confirmed?: boolean, acceptPreviewDrift?: boolean) => Promise<void>
  rerunAll: () => void
  cancelRun: (id: string) => Promise<void>
  clearRun: (id: string) => void
  prepareFullProfile: (id: string, portId?: string) => Promise<void>
  startFullProfile: (id: string, portId?: string) => Promise<void>
  cancelFullProfile: (id: string, portId?: string) => Promise<void>
  promote: (id: string) => Promise<void>
  restoreVersion: (id: string, versionId: string) => void

  // -- kernel + catalog --
  // `catalog` is a bounded WORKING SET — the tables referenced by the open canvas + recently
  // fetched/searched ones — NOT the whole catalog (which can be thousands of tables and is browsed
  // server-side, paginated, in the Workspace dataset scope). It exists so canvas source nodes can resolve their
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
  workspaceResourceId: string | null
  setWorkspaceResource: (resourceId: string | null) => void
  workspaceSearchQuery: string
  setWorkspaceSearchQuery: (query: string) => void
  workspaceScope: 'all' | 'datasets'
  setWorkspaceScope: (scope: 'all' | 'datasets') => void
  /** Switch the two Workspace lenses atomically. The optional opaque resource is context, never a path. */
  switchWorkspaceScope: (scope: 'all' | 'datasets', context?: {
    resourceId?: string | null
    datasetQuery?: string
  }) => void
  workspaceDatasetQuery: string
  setWorkspaceDatasetQuery: (query: string) => void
  jobsQuery: string
  setJobsQuery: (query: string) => void
  inboxQuery: string
  setInboxQuery: (query: string) => void
  transformResourceId: string | null
  transformVersion: string | null
  transformUpgradeCanvasId: string | null
  transformUpgradeNodeId: string | null
  transformLibraryQuery: string
  setTransformResource: (resourceId: string | null, version?: string | null,
    upgrade?: { canvasId: string; nodeId: string } | null) => void
  setTransformLibraryQuery: (query: string) => void
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
  openFile: (id: string, options?: { serverCopy?: boolean }) => Promise<boolean>
  newFile: (options?: { signal?: AbortSignal }) => Promise<CanvasCreationResult>
  newFromExample: (key: string, intent?: ExampleCreationIntent) => Promise<CanvasCreationResult>
  renameFile: (name: string) => void
  setRequirements: (reqs: string[]) => void
  setParameters: (parameters: CanvasParameterDeclaration[]) => string | null
  deleteFile: (id: string) => Promise<void>
  refreshLocalDrafts: () => void
  openLocalDraft: (draftId: string) => boolean
  retryLocalDraft: (draftId: string) => Promise<void>
  forkLocalDraft: (draftId: string) => Promise<void>
  discardLocalDraft: (draftId: string) => Promise<void>
  exportLocalDraft: (draftId: string) => void
}

// Top-level views (like Figma's Recents / Design surfaces). 'canvas' is the editor; settings is a modal.
export type DpView = 'canvas' | 'workspace' | 'jobs' | 'inbox' | 'files' | 'transforms' | 'relationships'

function emptyDoc(): CanvasDoc {
  // a random suffix keeps ids unique — performance.now() resets per page load, so a bare timestamp can
  // collide across freshly-loaded tabs/tests and leak one canvas's runs/history into another
  return { id: `canvas_${Math.floor(performance.now())}_${Math.random().toString(36).slice(2, 8)}`, name: 'untitled', version: 1, nodes: [], edges: [] }
}

function replaceDraft(drafts: LocalCanvasDraft[], draft: LocalCanvasDraft): LocalCanvasDraft[] {
  return [draft, ...drafts.filter((candidate) => candidate.draftId !== draft.draftId)]
    .sort((a, b) => b.lastLocalEditAt.localeCompare(a.lastLocalEditAt))
}

function draftAfterStorageWrite(
  draft: LocalCanvasDraft, result: { ok: boolean; error?: string },
): LocalCanvasDraft {
  return result.ok ? draft : { ...draft, syncState: 'error', lastError: result.error }
}

function draftForDoc(principalId: string, doc: CanvasDoc, baseVersion: number | null,
                     previous?: LocalCanvasDraft, createAttemptDoc?: CanvasDoc | null): LocalCanvasDraft {
  return {
    draftId: doc.id,
    principalId,
    canvasId: doc.id,
    baseCanvasId: baseVersion == null ? null : doc.id,
    baseVersion,
    name: doc.name || 'untitled',
    doc,
    createAttemptDoc: createAttemptDoc === undefined ? previous?.createAttemptDoc ?? null : createAttemptDoc,
    syncState: 'dirty',
    lastLocalEditAt: new Date().toISOString(),
  }
}

function downloadDraft(draft: LocalCanvasDraft): void {
  const blob = new Blob([JSON.stringify(draft.doc, null, 2)], { type: 'application/json' })
  const href = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = href
  anchor.download = `${draft.name || draft.canvasId}.canvas.json`
  anchor.click()
  URL.revokeObjectURL(href)
}

// true if the node, or anything feeding it, has an unmet required param — so running the pipeline
// through it would fail. Keeps rerun-all consistent with the disabled ▶ on the cards.
function hasInvalidUpstream(doc: CanvasDoc, id: string, numericDrafts: Store['numericParamDrafts']): boolean {
  const seen = new Set<string>()
  const walk = (nid: string): boolean => {
    if (seen.has(nid)) return false
    seen.add(nid)
    const n = doc.nodes.find((x) => x.id === nid)
    if (!n) return false
    if (nodeInvalidReason(n, undefined, numericDrafts[n.id])) return true
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
  jobKey: string,
  requestGeneration: number,
  runId: string,
  principalId: string,
  observedTerminal?: RunStatus,
): Promise<void> {
  const current = get().profileJobs[jobKey]
  if (_profileSubmissionUserId !== principalId || get().currentUser?.id !== principalId
      || current?.principalId !== principalId || current.canCancel !== true
      || current.requestGeneration !== requestGeneration || current.status?.runId !== runId
      || current.cancelRequested !== true
      || (current.status.status !== 'queued' && current.status.status !== 'running')) return
  const expectedStatus = current.status
  const expectedSubmissionId = current.submissionId
  const expectedCanvasId = current.canvasId
  const boundTrackedJob = (state: Store): ProfileJobState | undefined => {
    const tracked = state.profileJobs[jobKey]
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
      return { profileJobs: { ...state.profileJobs, [jobKey]: {
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
      return { profileJobs: { ...state.profileJobs, [jobKey]: {
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
    set(view === 'transforms' && get().view !== 'transforms'
      ? { view, transformResourceId: null, transformVersion: null,
          transformUpgradeCanvasId: null, transformUpgradeNodeId: null }
      : { view })
  },
  erFocusUri: null,
  openRelationships: (uri) => {
    if (get().view !== 'relationships') _fileNavigationGeneration += 1
    set({ erFocusUri: uri, view: 'relationships' })
  },
  workspaceResourceId: null,
  setWorkspaceResource: (resourceId) => {
    if (get().view !== 'workspace') _fileNavigationGeneration += 1
    set({ workspaceResourceId: resourceId, view: 'workspace' })
  },
  workspaceSearchQuery: '',
  setWorkspaceSearchQuery: (query) => {
    if (get().view !== 'workspace') _fileNavigationGeneration += 1
    set({ workspaceSearchQuery: query.trim().replace(/\s+/g, ' '), view: 'workspace' })
  },
  workspaceScope: 'all',
  setWorkspaceScope: (workspaceScope) => {
    if (get().view !== 'workspace') _fileNavigationGeneration += 1
    set({ workspaceScope, view: 'workspace' })
  },
  switchWorkspaceScope: (workspaceScope, context) => {
    if (get().view !== 'workspace') _fileNavigationGeneration += 1
    set({
      workspaceScope,
      workspaceResourceId: context?.resourceId ?? null,
      ...(context?.datasetQuery !== undefined ? { workspaceDatasetQuery: context.datasetQuery } : {}),
      view: 'workspace',
    })
  },
  workspaceDatasetQuery: '',
  setWorkspaceDatasetQuery: (workspaceDatasetQuery) => {
    if (get().view !== 'workspace') _fileNavigationGeneration += 1
    set({ workspaceDatasetQuery, view: 'workspace' })
  },
  jobsQuery: '',
  setJobsQuery: (query) => {
    if (get().view !== 'jobs') _fileNavigationGeneration += 1
    set({ jobsQuery: query, view: 'jobs' })
  },
  inboxQuery: '',
  setInboxQuery: (query) => {
    if (get().view !== 'inbox') _fileNavigationGeneration += 1
    set({ inboxQuery: query, view: 'inbox' })
  },
  transformResourceId: null,
  transformVersion: null,
  transformUpgradeCanvasId: null,
  transformUpgradeNodeId: null,
  transformLibraryQuery: '',
  setTransformResource: (transformResourceId, transformVersion = null, upgrade = null) => {
    if (get().view !== 'transforms') _fileNavigationGeneration += 1
    set({
      transformResourceId, transformVersion,
      transformUpgradeCanvasId: upgrade?.canvasId ?? null,
      transformUpgradeNodeId: upgrade?.nodeId ?? null,
      view: 'transforms',
    })
  },
  setTransformLibraryQuery: (transformLibraryQuery) => {
    if (get().view !== 'transforms') _fileNavigationGeneration += 1
    set({ transformLibraryQuery, view: 'transforms' })
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
  canvasTransformReferences: [],
  specsVersion: 0,
  schemas: {},
  sizes: {},
  selectedId: null,
  selectedIds: [],
  nodeRevealRequest: null,
  viewportFitRequest: null,
  openPanels: {},
  previews: {},
  previewBindings: {},
  runs: {},
  profileJobs: {},
  past: [],
  future: [],
  saved: true,
  serverVersion: null,
  currentDraftId: null,
  localDrafts: [],
  draftStorageErrors: [],
  firstRunChoice: false,
  numericParamDrafts: {},
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
      const runs = { ...s.runs }
      for (const node of nodes) {
        if (node.type !== 'write' || (!stale.has(node.id) && node.id !== id)) continue
        const current = runs[node.id]
        if (current) runs[node.id] = {
          ...current,
          writeAdmission: undefined,
          writeSubmissionId: undefined,
          writeAdmissionFingerprint: undefined,
        }
      }
      return { doc: { ...s.doc, nodes, edges }, runs }
    })
  },

  setNumericParamDraft: (id, param, text) => {
    if (!roleCanEdit(get().canvasRole)) return
    set((s) => {
      const numericParamDrafts = { ...s.numericParamDrafts }
      const nodeDrafts = { ...(numericParamDrafts[id] ?? {}) }
      if (text === undefined) delete nodeDrafts[param]
      else nodeDrafts[param] = text
      if (Object.keys(nodeDrafts).length) numericParamDrafts[id] = nodeDrafts
      else delete numericParamDrafts[id]
      return { numericParamDrafts }
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
    Object.values(get().profileJobs).filter((job) => job.nodeId === id)
      .forEach(cancelDetachedProfileJob)
    get().commit()
    set((s) => {
      const previews = { ...s.previews }; delete previews[id]
      const runs = { ...s.runs }; delete runs[id]
      const profileJobs = Object.fromEntries(
        Object.entries(s.profileJobs).filter(([, job]) => job.nodeId !== id),
      )
      const numericParamDrafts = { ...s.numericParamDrafts }; delete numericParamDrafts[id]
      return {
        doc: {
          ...s.doc,
          nodes: s.doc.nodes.filter((n) => n.id !== id),
          edges: s.doc.edges.filter((e) => e.source !== id && e.target !== id),
        },
        selectedId: s.selectedId === id ? null : s.selectedId,
        selectedIds: s.selectedIds.filter((x) => x !== id),
        openPanels: Object.fromEntries(Object.entries(s.openPanels).filter(([k]) => k !== id)),
        previews, runs, profileJobs, numericParamDrafts,
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

  requestNodeReveal: (canvasId, nodeId) => set({
    nodeRevealRequest: { id: ++_nodeRevealGeneration, canvasId, nodeId },
  }),

  // Acknowledge only the request that actually moved the viewport. If a newer route arrived while
  // React Flow was mounting, the older effect must not erase that replacement request.
  acknowledgeNodeReveal: (requestId) => set((state) => (
    state.nodeRevealRequest?.id === requestId ? { nodeRevealRequest: null } : {}
  )),

  clearNodeReveal: () => set({ nodeRevealRequest: null }),

  requestViewportFit: (doc) => set({
    viewportFitRequest: {
      id: ++_viewportFitGeneration,
      canvasId: doc.id,
      documentIdentity: canvasViewportDocumentIdentity(doc),
    },
  }),

  acknowledgeViewportFit: (requestId) => set((state) => (
    state.viewportFitRequest?.id === requestId ? { viewportFitRequest: null } : {}
  )),

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
    Object.values(get().profileJobs).filter((job) => ids.includes(job.nodeId))
      .forEach(cancelDetachedProfileJob)
    get().commit()
    const kill = new Set(ids)
    set((s) => {
      const previews = Object.fromEntries(Object.entries(s.previews).filter(([k]) => !kill.has(k)))
      const runs = Object.fromEntries(Object.entries(s.runs).filter(([k]) => !kill.has(k)))
      const profileJobs = Object.fromEntries(
        Object.entries(s.profileJobs).filter(([, job]) => !kill.has(job.nodeId)),
      )
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

  runPreview: async (id: string, offset = 0, requestedPortId?: string, refreshLatest = false) => {
    // offset lives in the preview state (single source of truth) so an external Refresh (which
    // re-fetches page 0) and the panel's page controls never disagree.
    const doc = get().doc
    const node = doc.nodes.find((candidate) => candidate.id === id)
    if (!node) return
    if (hasInvalidUpstream(doc, id, get().numericParamDrafts)) {
      get().pushToast('Fix invalid node parameters before previewing.', 'error')
      return
    }
    const ports = nodeOutputs(node)
    const previousPreview = get().previews[id]
    const currentPortId = previousPreview?.portId
    const defaultPortId = ports.find((port) => port.id === 'out')?.id ?? ports[0]?.id
    const portId = requestedPortId ?? (ports.length > 1
      ? ports.find((port) => port.id === currentPortId)?.id ?? defaultPortId
      : undefined)
    const planIdentity = previewPlanIdentity(doc, id, portId)
    const parameterBindings = get().runs[id]?.parameterBindings ?? []
    const parameterIdentity = parameterBindingsIdentity(parameterBindings)
    const requestGeneration = ++_previewRequestGeneration
    const isCurrent = () => {
      const state = get()
      const preview = state.previews[id]
      return preview?.requestGeneration === requestGeneration
        && previewIsCurrent(preview, state.doc, id, portId)
        && parameterBindingsIdentity(state.runs[id]?.parameterBindings) === parameterIdentity
    }
    set((s) => ({
      previews: {
        ...s.previews,
        [id]: refreshLatest && previousPreview
          ? { ...previousPreview, parameterBindings, requestGeneration, loading: true, error: undefined }
          : { canvasId: doc.id, nodeId: id, portId, planIdentity, parameterBindings, requestGeneration, loading: true, offset },
      },
      openPanels: { [id]: 'data' },
    }))
    const spec = getSpec(node.type)
    if (spec?.previewable === false) {
      set((s) => ({
        previews: {
          ...s.previews,
          [id]: {
            canvasId: doc.id, nodeId: id, portId, planIdentity, parameterBindings, requestGeneration, offset,
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
      const retainedBinding = refreshLatest ? undefined : currentPreviewBinding(get(), id)?.inputManifest
      const result = retainedBinding
        ? parameterBindings.length
          ? await api.preview(doc, id, k, offset, portId, retainedBinding, parameterBindings)
          : await api.preview(doc, id, k, offset, portId, retainedBinding)
        : parameterBindings.length
          ? await api.preview(doc, id, k, offset, portId, undefined, parameterBindings)
          : await api.preview(doc, id, k, offset, portId)
      if (!isCurrent()) return
      const binding = result.inputManifest ? {
        canvasId: doc.id, nodeId: id, portId, planIdentity,
        parameterBindings,
        inputManifest: result.inputManifest,
      } : undefined
      const clearRetainedBinding = refreshLatest && !binding && !result.error && !result.notPreviewable
      set((s) => ({
        previews: { ...s.previews, [id]: {
          canvasId: doc.id, nodeId: id, portId, planIdentity, parameterBindings, requestGeneration, result, offset,
        } },
        previewBindings: (() => {
          if (binding) return { ...s.previewBindings, [id]: binding }
          if (!clearRetainedBinding) return s.previewBindings
          const retained = { ...s.previewBindings }
          delete retained[id]
          return retained
        })(),
      }))
      if (binding || clearRetainedBinding) {
        writePreviewBindings(get().currentUser?.id, doc.id, get().previewBindings)
      }
    } catch (e) {
      if (!isCurrent()) return
      set((s) => ({
        previews: {
          ...s.previews,
          [id]: refreshLatest && previousPreview
            ? { ...previousPreview, requestGeneration, error: (e as Error).message, loading: false }
            : { canvasId: doc.id, nodeId: id, portId, planIdentity, parameterBindings, requestGeneration, error: (e as Error).message, offset },
        },
      }))
    }
  },

  refreshPreviewInputs: async (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    const previous = currentPreviewBinding(get(), id)
    if (!previous) return
    await get().runPreview(id, 0, previous.portId, true)
    const refreshed = get().previews[id]
    if (!refreshed || !previewIsCurrent(refreshed, get().doc, id, previous.portId)
        || refreshed.loading || refreshed.error || !refreshed.result
        || refreshed.result.error || refreshed.result.notPreviewable) return
    const next = currentPreviewBinding(get(), id)
    if (next && sameInputManifest(next.inputManifest, previous.inputManifest)) return
    const before = new Map(previous.inputManifest.map((item) => [item.node_id, item]))
    const changedSources = next ? next.inputManifest.filter((item) => {
      const prior = before.get(item.node_id)
      return !prior || prior.dataset_id !== item.dataset_id
        || prior.revision_id !== item.revision_id || prior.provider !== item.provider
    }) : previous.inputManifest
    if (!changedSources.length) return
    set((s) => {
      const stale = new Set<string>()
      for (const source of changedSources) {
        stale.add(source.node_id)
        for (const nodeId of downstream(s.doc, source.node_id)) stale.add(nodeId)
      }
      return {
        doc: { ...s.doc, nodes: s.doc.nodes.map((node) => stale.has(node.id) && node.data.status !== 'draft'
          ? { ...node, data: { ...node.data, status: 'stale' as NodeStatus } }
          : node) },
        runs: { ...s.runs, [id]: {
          ...(s.runs[id] ?? {}), phase: 'idle', estimate: undefined,
          inputDrift: undefined, driftInputManifest: undefined, error: undefined,
        } },
      }
    })
    get().pushToast(`Refreshed ${changedSources.length} preview input${changedSources.length === 1 ? '' : 's'} to latest`, 'success')
  },

  // The play action: estimate, then start immediately for cheap work; only gate on expensive
  // runs (FR-E3). Do NOT auto-open the run panel — the card shows status; the user opens details
  // if interested. A confirm gate is the one exception (it needs the panel to show the button).
  requestRun: async (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    if (hasConfiguredMergeColumnsWrite(get().doc, id)) {
      set(() => ({ openPanels: { [id]: 'run' } }))
      get().pushToast('Column merge uses its certified admission flow.', 'info')
      return
    }
    if (hasConfiguredUpsertWrite(get().doc, id)) {
      set(() => ({ openPanels: { [id]: 'run' } }))
      get().pushToast('Keyed upsert uses its certified admission flow.', 'info')
      return
    }
    if (hasInvalidUpstream(get().doc, id, get().numericParamDrafts)) {
      get().pushToast('Fix invalid node parameters before running.', 'error')
      return
    }
    const declarations = targetParameterDeclarations(get().doc, id)
    const parameterContractFingerprint = JSON.stringify(declarations)
    if (declarations.length > 0 && (!get().runs[id]?.parametersReady
        || get().runs[id]?.parameterContractFingerprint !== parameterContractFingerprint)) {
      set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? {}), phase: 'parameters',
        parametersReady: false, parameterContractFingerprint,
        parameterContinuation: { kind: 'run' },
      } }, openPanels: { [id]: 'run' } }))
      return
    }
    set((s) => ({ runs: { ...s.runs, [id]: {
      ...(s.runs[id] ?? {}), phase: 'estimating', parametersReady: false,
    } } }))
    let estimate
    const requestFingerprint = parameterRequestFingerprint(
      get().doc, id, get().runs[id]?.parameterBindings)
    try {
      const doc = get().doc
      const binding = currentPreviewBinding(get(), id)
      const parameters = get().runs[id]?.parameterBindings
      estimate = binding
        ? parameters?.length
          ? await api.estimate(doc, id, binding.inputManifest, parameters)
          : await api.estimate(doc, id, binding.inputManifest)
        : parameters?.length ? await api.estimate(doc, id, undefined, parameters) : await api.estimate(doc, id)
    } catch (e) {
      if (parameterRequestFingerprint(get().doc, id, get().runs[id]?.parameterBindings) !== requestFingerprint) return
      set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? {}), phase: 'failed', error: (e as Error).message,
      } } }))
      get().pushToast((e as Error).message || 'Could not estimate the run', 'error')
      return
    }
    if (parameterRequestFingerprint(get().doc, id, get().runs[id]?.parameterBindings) !== requestFingerprint) return
    if (estimate.needsConfirm) {
      set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? {}), estimate, phase: 'confirm',
      } }, openPanels: { [id]: 'run' } }))
    } else {
      set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? {}), estimate, phase: 'running',
      } } }))
      await get().run(id, false)
    }
  },

  setRunParameterBinding: (id, binding) => {
    set((s) => {
      const current = s.runs[id]?.parameterBindings ?? []
      const parameterBindings = [...current.filter((item) => item.name !== binding.name), binding]
      if (parameterBindingsIdentity(parameterBindings) === parameterBindingsIdentity(current)) return {}
      const previews = { ...s.previews }; delete previews[id]
      const previewBindings = { ...s.previewBindings }; delete previewBindings[id]
      return {
        previews, previewBindings,
        runs: { ...s.runs, [id]: {
          ...(s.runs[id] ?? { phase: 'parameters' as const }),
          parameterBindings, parametersReady: false, estimate: undefined,
          inputDrift: undefined, driftInputManifest: undefined,
          writeAdmission: undefined, writeSubmissionId: undefined, writeAdmissionFingerprint: undefined,
        } },
      }
    })
    writePreviewBindings(get().currentUser?.id, get().doc.id, get().previewBindings)
  },

  clearRunParameterBinding: (id, name) => {
    set((s) => {
      const current = s.runs[id]?.parameterBindings ?? []
      const parameterBindings = current.filter((item) => item.name !== name)
      if (parameterBindings.length === current.length) return {}
      const previews = { ...s.previews }; delete previews[id]
      const previewBindings = { ...s.previewBindings }; delete previewBindings[id]
      return {
        previews, previewBindings,
        runs: { ...s.runs, [id]: {
          ...(s.runs[id] ?? { phase: 'parameters' as const }),
          parameterBindings, parametersReady: false, estimate: undefined,
          inputDrift: undefined, driftInputManifest: undefined,
          writeAdmission: undefined, writeSubmissionId: undefined, writeAdmissionFingerprint: undefined,
        } },
      }
    })
    writePreviewBindings(get().currentUser?.id, get().doc.id, get().previewBindings)
  },

  editRunParameters: (id) => {
    const declarations = targetParameterDeclarations(get().doc, id)
    if (!declarations.length) return
    set((s) => ({ runs: { ...s.runs, [id]: {
      ...(s.runs[id] ?? {}), phase: 'parameters', parametersReady: false,
      parameterContractFingerprint: JSON.stringify(declarations),
      parameterContinuation: { kind: 'estimate' },
    } }, openPanels: { [id]: 'run' } }))
  },

  submitRunParameters: async (id) => {
    const parameterContractFingerprint = JSON.stringify(
      targetParameterDeclarations(get().doc, id))
    const continuation = get().runs[id]?.parameterContinuation
    set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? { phase: 'parameters' as const }), phase: 'idle',
        parametersReady: true, parameterContractFingerprint, parameterContinuation: undefined,
    } } }))
    const metadataDoc = get().doc
    const metadataBindings = get().runs[id]?.parameterBindings
    const metadataFingerprint = parameterRequestFingerprint(metadataDoc, id, metadataBindings)
    void api.schema(metadataDoc, id, undefined, metadataBindings).then((schemas) => {
      if (parameterRequestFingerprint(get().doc, id, get().runs[id]?.parameterBindings) !== metadataFingerprint) return
      set((s) => ({ schemas: { ...s.schemas, ...schemas } }))
    }).catch(() => {})
    void api.graphSizes(metadataDoc, id, metadataBindings).then((sizes) => {
      if (parameterRequestFingerprint(get().doc, id, get().runs[id]?.parameterBindings) !== metadataFingerprint) return
      set((s) => ({ sizes: { ...s.sizes, ...sizes } }))
    }).catch(() => {})
    if (continuation?.kind === 'profile') {
      get().closePanel(id)
      await get().prepareFullProfile(id, continuation.portId)
    } else if (continuation?.kind === 'estimate') await get().estimate(id)
    else await get().requestRun(id)
  },

  estimate: async (id) => {
    if (hasInvalidUpstream(get().doc, id, get().numericParamDrafts)) return
    const declarations = targetParameterDeclarations(get().doc, id)
    const parameterContractFingerprint = JSON.stringify(declarations)
    if (declarations.length > 0 && (!get().runs[id]?.parametersReady
        || get().runs[id]?.parameterContractFingerprint !== parameterContractFingerprint)) {
      set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? {}), phase: 'parameters',
        parametersReady: false, parameterContractFingerprint,
        parameterContinuation: { kind: 'estimate' },
      } }, openPanels: { [id]: 'run' } }))
      return
    }
    set((s) => ({ runs: { ...s.runs, [id]: {
      ...(s.runs[id] ?? {}), phase: 'estimating', parametersReady: false,
    } }, openPanels: { [id]: 'run' } }))
    const requestFingerprint = parameterRequestFingerprint(
      get().doc, id, get().runs[id]?.parameterBindings)
    try {
      const doc = get().doc
      const binding = currentPreviewBinding(get(), id)
      const parameters = get().runs[id]?.parameterBindings
      const estimate = binding
        ? parameters?.length
          ? await api.estimate(doc, id, binding.inputManifest, parameters)
          : await api.estimate(doc, id, binding.inputManifest)
        : parameters?.length ? await api.estimate(doc, id, undefined, parameters) : await api.estimate(doc, id)
      if (parameterRequestFingerprint(get().doc, id, get().runs[id]?.parameterBindings) !== requestFingerprint) return
      set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? {}), estimate,
        phase: estimate.needsConfirm ? 'confirm' : 'estimated',
      } } }))
    } catch (e) {
      if (parameterRequestFingerprint(get().doc, id, get().runs[id]?.parameterBindings) !== requestFingerprint) return
      set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? {}), phase: 'failed', error: (e as Error).message,
      } } }))
    }
  },

  prepareWrite: async (id) => {
    const doc = get().doc
    const node = doc.nodes.find((candidate) => candidate.id === id)
    if (node?.type !== 'write') return undefined
    const parameterBindings = get().runs[id]?.parameterBindings
    const fingerprint = writeAdmissionFingerprint(doc, parameterBindings)
    const existing = get().runs[id]
    if (existing?.writeAdmission && existing.writeAdmissionFingerprint === fingerprint) {
      return existing.writeAdmission
    }
    const submissionId = globalThis.crypto.randomUUID()
    set((s) => ({ runs: { ...s.runs, [id]: {
      ...(s.runs[id] ?? { phase: 'idle' as const }),
      writeSubmissionId: submissionId,
      writeAdmissionFingerprint: fingerprint,
      writeAdmission: undefined,
    } } }))
    const binding = currentPreviewBinding(get(), id)
    const admission = parameterBindings?.length
      ? await api.writeAdmission(doc, id, submissionId, binding?.inputManifest, parameterBindings)
      : await api.writeAdmission(doc, id, submissionId, binding?.inputManifest)
    const current = get().runs[id]
    if (current?.writeSubmissionId !== submissionId
        || current.writeAdmissionFingerprint !== fingerprint
        || writeAdmissionFingerprint(
          get().doc, get().runs[id]?.parameterBindings) !== fingerprint) return undefined
    set((s) => ({ runs: { ...s.runs, [id]: {
      ...(s.runs[id] ?? { phase: 'idle' as const }), writeAdmission: admission,
    } } }))
    return admission
  },

  run: async (id, confirmed = false, acceptPreviewDrift = false) => {
    if (!roleCanEdit(get().canvasRole)) return
    if (hasConfiguredMergeColumnsWrite(get().doc, id)) {
      set(() => ({ openPanels: { [id]: 'run' } }))
      get().pushToast('Column merge uses its certified admission flow.', 'info')
      return
    }
    if (hasConfiguredUpsertWrite(get().doc, id)) {
      set(() => ({ openPanels: { [id]: 'run' } }))
      get().pushToast('Keyed upsert uses its certified admission flow.', 'info')
      return
    }
    if (hasInvalidUpstream(get().doc, id, get().numericParamDrafts)) return
    const doc = get().doc
    const binding = currentPreviewBinding(get(), id)
    if (binding && !acceptPreviewDrift) {
      try {
        const parameters = get().runs[id]?.parameterBindings
        const inputDrift = parameters?.length
          ? await api.inputDrift(doc, id, binding.inputManifest, parameters)
          : await api.inputDrift(doc, id, binding.inputManifest)
        if (inputDrift.drifted) {
          set((s) => ({
            runs: { ...s.runs, [id]: {
              ...(s.runs[id] ?? {}), phase: 'drift', inputDrift,
              driftInputManifest: binding.inputManifest, error: undefined,
            } },
            openPanels: { [id]: 'run' },
          }))
          return
        }
        if (!previewBindingIsCurrent(binding, get().doc, id, get().runs[id]?.parameterBindings)) return
      } catch (e) {
        set((s) => ({ runs: { ...s.runs, [id]: {
          ...(s.runs[id] ?? {}), phase: 'failed',
          error: (e as Error).message || 'Could not verify preview input drift',
        } } }))
        get().pushToast((e as Error).message || 'Could not verify preview input drift', 'error')
        return
      }
    }
    if (acceptPreviewDrift && (!binding
        || !sameInputManifest(binding.inputManifest, get().runs[id]?.driftInputManifest))) {
      set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? {}), phase: 'failed', estimate: undefined,
        inputDrift: undefined, driftInputManifest: undefined,
        error: 'Preview inputs changed; preview again before running.',
      } } }))
      get().pushToast('Preview inputs changed; preview again before running.', 'info')
      return
    }
    let writeAdmission: WriteAdmission | undefined
    if (doc.nodes.find((node) => node.id === id)?.type === 'write') {
      try {
        writeAdmission = await get().prepareWrite(id)
        if (!writeAdmission) throw new Error('Write configuration changed during admission; retry.')
        if (writeAdmission.blocker) throw new Error(writeAdmission.blocker)
      } catch (e) {
        set((s) => ({ runs: { ...s.runs, [id]: {
          ...(s.runs[id] ?? {}), phase: 'failed', error: (e as Error).message,
        } } }))
        get().pushToast((e as Error).message || 'Could not admit the write', 'error')
        return
      }
    }
    // no openPanels here — status shows on the card; the user opens details if they want them
    set((s) => ({ runs: { ...s.runs, [id]: {
      ...(s.runs[id] ?? {}), phase: 'running', inputDrift: undefined,
      driftInputManifest: undefined, error: undefined,
    } } }))
    get().updateData(id, { status: 'running' })
    try {
      const submissionId = writeAdmission
        ? get().runs[id]?.writeSubmissionId ?? globalThis.crypto.randomUUID()
        : globalThis.crypto.randomUUID()
      const status = writeAdmission
        ? get().runs[id]?.parameterBindings?.length
          ? await api.run(
            doc, id, confirmed, submissionId, binding?.inputManifest,
            writeAdmission.intent ?? undefined, get().runs[id]?.parameterBindings,
          )
          : await api.run(
            doc, id, confirmed, submissionId, binding?.inputManifest,
            writeAdmission.intent ?? undefined,
          )
        : binding
          ? get().runs[id]?.parameterBindings?.length
            ? await api.run(doc, id, confirmed, submissionId, binding.inputManifest, undefined, get().runs[id]?.parameterBindings)
            : await api.run(doc, id, confirmed, submissionId, binding.inputManifest)
          : get().runs[id]?.parameterBindings?.length
            ? await api.run(doc, id, confirmed, submissionId, undefined, undefined, get().runs[id]?.parameterBindings)
            : await api.run(doc, id, confirmed, submissionId)
      set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), status, phase: 'running' } } }))
      pollRun(get, set, id, status.runId)
    } catch (e) {
      if (e instanceof KernelError && e.status === 409 && !e.message.includes('write admission')) {
        set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'confirm' } } }))
        get().updateData(id, { status: 'stale' })
        return
      }
      const preserveWriteSubmission = Boolean(
        writeAdmission?.managed && writeAdmission.intent
        && (!(e instanceof KernelError) || e.status >= 500),
      )
      set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? {}), phase: 'failed', error: (e as Error).message,
        ...(!preserveWriteSubmission ? {
          writeAdmission: undefined, writeSubmissionId: undefined,
          writeAdmissionFingerprint: undefined,
        } : {}),
      } } }))
      get().updateData(id, { status: 'failed' })
      get().pushToast((e as Error).message || 'Run failed to start', 'error')
    }
  },

  // Re-run the whole graph: kick every runnable sink (a node with no outgoing edge); each pulls
  // its upstream, so the full pipeline re-executes. Notes/unconnected nodes aren't runnable → skipped.
  rerunAll: () => {
    if (!roleCanEdit(get().canvasRole)) return
    const { doc, numericParamDrafts } = get()
    const hasOutgoing = new Set(doc.edges.map((e) => e.source))
    // a section's contained children are run by the section, not as top-level sinks
    const sinks = doc.nodes.filter((n) => !n.parentId && !hasOutgoing.has(n.id) && nodeRunnable(doc, n.id))
    // don't kick off pipelines that would fail on a missing required field (matches the disabled ▶)
    const valid = sinks.filter((n) => !hasInvalidUpstream(doc, n.id, numericParamDrafts))
    valid.forEach((n) => get().requestRun(n.id))
    const invalidSkipped = sinks.length - valid.length
    if (invalidSkipped) get().pushToast(`Skipped ${invalidSkipped} pipeline${invalidSkipped > 1 ? 's' : ''} with invalid node parameters`, 'info')
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
  prepareFullProfile: async (id, requestedPortId) => {
    if (!roleCanEdit(get().canvasRole)) return
    const doc = get().doc
    if (!doc.nodes.some((node) => node.id === id)) return
    const declarations = targetParameterDeclarations(doc, id)
    const parameterContractFingerprint = JSON.stringify(declarations)
    if (declarations.length > 0 && (!get().runs[id]?.parametersReady
        || get().runs[id]?.parameterContractFingerprint !== parameterContractFingerprint)) {
      set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? {}), phase: 'parameters',
        parametersReady: false, parameterContractFingerprint,
        parameterContinuation: { kind: 'profile', portId: requestedPortId },
      } }, openPanels: { [id]: 'run' } }))
      return
    }
    if (declarations.length > 0) {
      set((s) => ({ runs: { ...s.runs, [id]: {
        ...(s.runs[id] ?? {}), parametersReady: false,
      } } }))
    }
    const portId = resolvedProfilePort(doc, id, requestedPortId)
    const initialJobKey = profileJobKeyForDoc(doc, id, portId)
    const planIdentity = profilePlanIdentity(doc, id, portId)
    const requestGeneration = ++_profileRequestGeneration
    const previous = get().profileJobs[initialJobKey]
    cancelDetachedProfileJob(previous)
    const parameterBindings = get().runs[id]?.parameterBindings
    const parameterIdentity = parameterBindingsIdentity(parameterBindings)
    const isCurrent = () => {
      const job = get().profileJobs[initialJobKey]
      return job?.requestGeneration === requestGeneration
        && profileJobIsCurrent(job, get().doc, id, portId)
        && parameterBindingsIdentity(get().runs[id]?.parameterBindings) === parameterIdentity
    }
    set((s) => ({ profileJobs: {
      ...s.profileJobs,
      [initialJobKey]: {
        canvasId: doc.id, nodeId: id, portId,
        principalId: s.currentUser?.id,
        canCancel: roleCanEdit(s.canvasRole), planIdentity, parameterBindings,
        requestGeneration, phase: 'estimating',
      },
    } }))
    let estimate: RunEstimate
    let planDigest: string
    let inputManifest: RunInputManifestItem[] | null | undefined
    try {
      const retainedManifest = currentPreviewBinding(get(), id)?.inputManifest
      const preflight = await (retainedManifest
        ? parameterBindings?.length
          ? api.profileEstimate(doc, id, portId, retainedManifest, parameterBindings)
          : api.profileEstimate(doc, id, portId, retainedManifest)
        : parameterBindings?.length
          ? api.profileEstimate(doc, id, portId, undefined, parameterBindings)
          : api.profileEstimate(doc, id, portId))
      if (portId !== undefined && preflight.targetPortId !== portId) {
        throw new Error('Profile estimate returned a different output port')
      }
      estimate = preflight
      planDigest = preflight.planDigest
      inputManifest = preflight.inputManifest
    } catch (e) {
      if (!isCurrent()) return
      set((s) => ({ profileJobs: { ...s.profileJobs, [initialJobKey]: {
        ...(s.profileJobs[initialJobKey]!), phase: 'failed', error: (e as Error).message || 'Could not estimate full profile',
      } } }))
      return
    }
    if (!isCurrent()) return
    set((s) => ({ profileJobs: { ...s.profileJobs, [initialJobKey]: {
      ...(s.profileJobs[initialJobKey]!), estimate, planDigest, inputManifest,
      parameterBindings,
      phase: 'preflight', error: undefined,
    } } }))
  },

  startFullProfile: async (id, portId) => {
    if (!roleCanEdit(get().canvasRole)) return
    const doc = get().doc
    portId = resolvedProfilePort(doc, id, portId)
    const jobKey = profileJobKeyForDoc(doc, id, portId)
    const job = get().profileJobs[jobKey]
    const retryingUnknownSubmission = job?.phase === 'failed' && job.submissionUnresolved === true
    if (!job?.estimate || !job.planDigest || (job.phase !== 'preflight' && !retryingUnknownSubmission)
        || !profileJobIsCurrent(job, doc, id, portId)) return
    const submissionUserId = get().currentUser?.id
    if (!submissionUserId) {
      set((s) => ({ profileJobs: { ...s.profileJobs, [jobKey]: {
        ...(s.profileJobs[jobKey]!), phase: 'failed', error: 'A confirmed user is required to start a full profile',
      } } }))
      return
    }
    const { planDigest, requestGeneration, inputManifest, parameterBindings } = job
    const submissionId = retryingUnknownSubmission && job.submissionId
      ? job.submissionId
      : globalThis.crypto.randomUUID()
    let pendingSubmission = _pendingProfileSubmissions.get(submissionId)
    if (!pendingSubmission) {
      pendingSubmission = {
        doc: _clone(doc), nodeId: id, portId, planDigest, inputManifest,
        parameterBindings, submissionId,
        userId: submissionUserId, canCancel: true,
        cancelRequested: false, reconciling: false,
      }
      _pendingProfileSubmissions.set(submissionId, pendingSubmission)
    }
    const isSameSubmission = () => {
      const current = get().profileJobs[jobKey]
      return current?.requestGeneration === requestGeneration
        && current.submissionId === submissionId
        && current.principalId === submissionUserId
    }
    const isCurrent = () => {
      const current = get().profileJobs[jobKey]
      return isSameSubmission()
        && _profileSubmissionUserId === submissionUserId
        && profileJobIsCurrent(current, get().doc, id, portId)
    }
    set((s) => ({ profileJobs: { ...s.profileJobs, [jobKey]: {
      ...(s.profileJobs[jobKey]!), principalId: submissionUserId,
      canCancel: true,
      submissionId, submissionUnresolved: false,
      cancelRequested: retryingUnknownSubmission ? job.cancelRequested : false,
      phase: 'queued', error: undefined,
    } } }))
    let status: RunStatus
    try {
      // This click is the explicit confirmation. The server recomputes admission from the submitted
      // graph and still rejects a large/unknown direct API call that omits ``confirmed``.
      status = await submitFullProfile(
        doc, id, portId, planDigest, submissionId, submissionUserId, inputManifest,
        parameterBindings,
      )
    } catch (e) {
      const unresolved = retryableProfileRequest(e)
      if (!unresolved) forgetProfileSubmission(pendingSubmission)
      const current = get().profileJobs[jobKey]
      const sameSubmission = current?.requestGeneration === requestGeneration
        && current.submissionId === submissionId
      const currentPlan = sameSubmission && profileJobIsCurrent(current, get().doc, id, portId)
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
        const current = s.profileJobs[jobKey]
        if (!current || current.requestGeneration !== requestGeneration
            || current.submissionId !== submissionId
            || s.currentUser?.id !== submissionUserId
            || !profileJobIsCurrent(current, s.doc, id, portId)) return {}
        return { profileJobs: { ...s.profileJobs, [jobKey]: {
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
    const validIdentity = validProfileSubmissionStatus(status, id, portId, planDigest)
    if (!validIdentity) {
      forgetProfileSubmission(pendingSubmission)
      if (status.jobType === 'profile' && status.runId
          && (status.status === 'queued' || status.status === 'running')) {
        superviseDetachedProfileCancellation(status, submissionUserId, true)
      }
      if (!isCurrent()) return
      set((s) => {
        const current = s.profileJobs[jobKey]
        if (!current || current.requestGeneration !== requestGeneration
            || current.submissionId !== submissionId
            || s.currentUser?.id !== submissionUserId
            || !profileJobIsCurrent(current, s.doc, id, portId)) return {}
        return { profileJobs: { ...s.profileJobs, [jobKey]: {
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
      const current = s.profileJobs[jobKey]
      if (!current || current.requestGeneration !== requestGeneration
          || current.submissionId !== submissionId
          || s.currentUser?.id !== submissionUserId
          || !profileJobIsCurrent(current, s.doc, id, portId)) return {}
      installed = true
      cancelRequested = current.cancelRequested === true
      const active = status.status === 'queued' || status.status === 'running'
      return { profileJobs: { ...s.profileJobs, [jobKey]: {
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
      pollProfile(get, set, id, jobKey, status.runId, requestGeneration, submissionUserId, true)
      if (cancelRequested) {
        if (_profileSubmissionUserId !== submissionUserId) return
        try {
          const cancelled = await api.cancelRun(status.runId)
          set((s) => {
            const current = s.profileJobs[jobKey]
            if (current?.requestGeneration !== requestGeneration
                || current.submissionId !== submissionId
                || current.principalId !== submissionUserId
                || s.currentUser?.id !== submissionUserId
                || current.status?.runId !== status.runId
                || !sameProfileAttempt(current.status, cancelled)
                || !profileStatusCanAdvance(current.status, cancelled)) return {}
            const active = cancelled.status === 'queued' || cancelled.status === 'running'
            return { profileJobs: { ...s.profileJobs, [jobKey]: {
              ...current, status: cancelled,
              phase: active ? 'cancelling' : profilePhase(cancelled),
              error: cancelled.error ?? undefined,
            } } }
          })
          await superviseTrackedProfileCancellation(
            get, set, id, jobKey, requestGeneration, status.runId, submissionUserId,
            exactProfileTerminal(status, cancelled) ? cancelled : undefined,
          )
        } catch (e) {
          if (retryableProfileRequest(e)) {
            await superviseTrackedProfileCancellation(get, set, id, jobKey, requestGeneration, status.runId, submissionUserId)
          }
          set((s) => {
            const current = s.profileJobs[jobKey]
            if (current?.requestGeneration !== requestGeneration
                || current.submissionId !== submissionId
                || current.principalId !== submissionUserId
                || s.currentUser?.id !== submissionUserId
                || current.status?.runId !== status.runId
                || !['queued', 'running'].includes(current.status.status)) return {}
            return { profileJobs: { ...s.profileJobs, [jobKey]: {
              ...current, phase: 'cancelling',
              error: `Cancellation request could not be confirmed; still checking run status: ${(e as Error).message || 'request failed'}`,
            } } }
          })
        }
      }
    }
  },

  cancelFullProfile: async (id, portId) => {
    if (!roleCanEdit(get().canvasRole)) return
    portId = resolvedProfilePort(get().doc, id, portId)
    const jobKey = profileJobKeyForDoc(get().doc, id, portId)
    const job = get().profileJobs[jobKey]
    if (!job) return
    const principalId = job.principalId
    if (!principalId || !job.canCancel || _profileSubmissionUserId !== principalId) return
    const { requestGeneration, submissionId } = job
    if (!job.status) {
      // The POST may have been accepted even though its response has not arrived. Record intent now;
      // startFullProfile will reconcile the stable submission id and cancel as soon as it learns runId.
      if (!submissionId || (job.phase !== 'queued' && job.phase !== 'cancelling')) return
      set((s) => {
        const current = s.profileJobs[jobKey]
        if (current?.requestGeneration !== requestGeneration || current.submissionId !== submissionId
            || current.principalId !== principalId || s.currentUser?.id !== principalId) return {}
        return { profileJobs: { ...s.profileJobs, [jobKey]: {
          ...current, cancelRequested: true, phase: 'cancelling',
        } } }
      })
      return
    }
    if (!['queued', 'running'].includes(job.status.status)) return
    const runId = job.status.runId
    set((s) => {
      const current = s.profileJobs[jobKey]
      if (current?.requestGeneration !== requestGeneration
          || current.submissionId !== submissionId
          || current.principalId !== principalId
          || s.currentUser?.id !== principalId
          || current.status?.runId !== runId
          || !['queued', 'running'].includes(current.status.status)) return {}
      return { profileJobs: { ...s.profileJobs, [jobKey]: {
        ...current, cancelRequested: true, phase: 'cancelling',
      } } }
    })
    // Recovered fail-closed jobs do not start their normal status poll until identity verification.
    // Cancellation still needs an authoritative lifecycle poll because a lost cancel response is an
    // unknown outcome, not evidence that the job failed.
    pollProfile(get, set, id, jobKey, runId, requestGeneration, principalId, true)
    if (_profileSubmissionUserId !== principalId) return
    try {
      const cancelled = await api.cancelRun(runId)
      set((s) => {
        const current = s.profileJobs[jobKey]
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
        return { profileJobs: { ...s.profileJobs, [jobKey]: {
          ...current, status, phase: active ? 'cancelling' : profilePhase(status),
          error: status.error ?? undefined,
        } } }
      })
      await superviseTrackedProfileCancellation(
        get, set, id, jobKey, requestGeneration, runId, principalId,
        exactProfileTerminal(job.status, cancelled) ? cancelled : undefined,
      )
    } catch (e) {
      if (retryableProfileRequest(e)) {
        await superviseTrackedProfileCancellation(get, set, id, jobKey, requestGeneration, runId, principalId)
      }
      set((s) => {
        const current = s.profileJobs[jobKey]
        if (current?.requestGeneration !== requestGeneration
            || current.submissionId !== submissionId
            || current.principalId !== principalId
            || s.currentUser?.id !== principalId
            || current.status?.runId !== runId
            || !['queued', 'running'].includes(current.status.status)) return {}
        return { profileJobs: { ...s.profileJobs, [jobKey]: {
          ...current, phase: 'cancelling',
          error: `Cancellation request could not be confirmed; still checking run status: ${(e as Error).message || 'request failed'}`,
        } } }
      })
    }
  },

  promote: async (id) => {
    if (!roleCanEdit(get().canvasRole)) return
    const doc = get().doc
    const n = doc.nodes.find((x) => x.id === id)
    if (!n) return
    const cfg = n.data.config
    const desc = await api.promote({
      id: promotedTransformKey(doc.id, n.id),
      title: n.data.title,
      mode: (cfg.mode as string) ?? 'map',
      code: (cfg.code as string) ?? '',
      inputColumns: [],
      outputSchema: Array.isArray(cfg.outputSchema) ? (cfg.outputSchema as any) : [],  // a {ref} contract doesn't inline here
      requirements: doc.requirements ?? [],
      blurb: 'promoted from an ad-hoc cell',
    })
    // The durable exact reference is now the execution definition. Inline code remains an ad-hoc
    // representation only and must not silently become a fallback for an unavailable library version.
    get().updateConfig(id, {
      source: 'library', processor: desc.id, version: desc.version, mode: desc.mode, code: null,
    })
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
    const rememberedUser = localStorage.getItem(LAST_USER_KEY)
    setApiUser(rememberedUser)  // restore chosen user (server defaults to 'local')
    if (!get().authEnabled && confirmedLocalMode() && rememberedUser) {
      // A server-confirmed local-mode workstation has no login transition. This cached principal is
      // used only to enumerate its own Canvas draft index while the hub is unavailable on reload.
      set({ currentUser: { id: rememberedUser, name: rememberedUser } })
      get().refreshLocalDrafts()
    }
    try {
      // NOTE: we deliberately do NOT load the whole catalog here — it can be thousands of tables. The
      // Workspace browses it server-side (paginated + faceted); the working set is filled on demand
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
      setApiUser(me.id); localStorage.setItem(LAST_USER_KEY, me.id)
      // Identity is server-confirmed now. Set it before the remaining calls so an offline failure may
      // use only THIS user's cached canvas role; an unknown identity always stays fail-closed.
      set({ currentUser: me })
      get().refreshLocalDrafts()
      const users = await api.users()
      set({ users })
      const filesRefreshed = await get().refreshFiles()
      const files = get().files
      // Do not infer a fresh workspace from an empty stale list: only an authoritative list can
      // establish that there is no existing Canvas. A local draft is user work too.
      set({ firstRunChoice: filesRefreshed && files.length === 0 && get().localDrafts.length === 0 })
      // honor a deep link (#/canvas/<id>, incl. a shared canvas resolved server-side); else the
      // last-opened / newest / a fresh file. A #/workspace or #/transforms link still loads a
      // current canvas underneath, then switches to that shell view below.
      const route = parseHash()
      const last = localStorage.getItem(OPEN_KEY(me.id))
      const fallbackDraft = last ? get().localDrafts.find((draft) => draft.canvasId === last) : undefined
      const fallback = last && files.some((f) => f.id === last) ? last : files[0]?.id
      // a deep-linked canvas that can't be opened (bad/revoked/other-user's link) must NOT discard
      // the last-opened file into a throwaway blank — fall back cleanly.
      const routeDraft = route.view === 'canvas' && route.canvasId
        ? get().localDrafts.find((draft) => draft.canvasId === route.canvasId)
        : undefined
      const opened = routeDraft ? get().openLocalDraft(routeDraft.draftId)
        : (route.view === 'canvas' && route.canvasId) ? await get().openFile(route.canvasId) : false
      if (opened && route.view === 'canvas' && route.canvasId && route.nodeId
          && get().doc.id === route.canvasId) {
        const nodeExists = get().doc.nodes.some((node) => node.id === route.nodeId)
        if (nodeExists) {
          get().select(route.nodeId)
          get().requestNodeReveal(route.canvasId, route.nodeId)
        } else get().pushToast('The requested node is no longer in this Canvas.', 'info')
      }
      if (!opened) {
        if (fallbackDraft) get().openLocalDraft(fallbackDraft.draftId)
        else if (fallback) await get().openFile(fallback)
        // A first-run workspace is an intentional choice, not an implicit remote "untitled" Canvas.
        else get().setView('workspace')
        // A Workspace dataset/container deep link carries more identity than the top-level view.
        // Preserve it before initRouter reflects bootstrapped state back into the hash; setting only
        // the view here would replace a reload of #/workspace/<resource> with bare #/workspace.
        if (route.view === 'workspace') {
          get().setWorkspaceResource(route.workspaceResourceId ?? null)
          get().setWorkspaceScope(route.workspaceScope ?? 'all')
          if ((route.workspaceScope ?? 'all') === 'datasets') {
            get().setWorkspaceDatasetQuery(route.workspaceDatasetQuery ?? '')
          } else get().setWorkspaceSearchQuery(route.workspaceQuery ?? '')
        }
        else if (route.view === 'jobs') get().setJobsQuery(route.jobsQuery ?? '')
        else if (route.view === 'inbox') get().setInboxQuery(route.inboxQuery ?? '')
        else if (route.view === 'transforms') {
          get().setTransformLibraryQuery(route.transformQuery ?? '')
          get().setTransformResource(
            route.transformId ?? null, route.transformVersion ?? null,
            route.transformCanvasId && route.transformNodeId
              ? { canvasId: route.transformCanvasId, nodeId: route.transformNodeId } : null,
          )
        }
        else if (route.view !== 'canvas') get().setView(route.view)
      }
    } catch {
      // Only a server-confirmed principal may see its local drafts. If identity was confirmed before
      // another bootstrap request failed, recover that principal's last draft without revealing any
      // other principal's index.
      const principalId = get().currentUser?.id
      if (principalId) {
        get().refreshLocalDrafts()
        const last = localStorage.getItem(OPEN_KEY(principalId))
        const draft = get().localDrafts.find((candidate) => candidate.canvasId === last)
          ?? get().localDrafts[0]
        if (draft) get().openLocalDraft(draft.draftId)
      }
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

  openFile: async (id, options) => {
    const generation = ++_fileNavigationGeneration
    const userId = get().currentUser?.id
    if (!userId) {
      get().pushToast('Your identity is not available yet', 'error')
      return false
    }
    const localDraft = get().localDrafts.find((draft) => draft.canvasId === id && draft.principalId === userId)
    if (localDraft && !options?.serverCopy) return get().openLocalDraft(localDraft.draftId)
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
      const selectedId = get().doc.id === id ? get().selectedId : null
      const inspectConflict = options?.serverCopy === true && localDraft?.syncState === 'conflict'
      get().loadDoc(doc, inspectConflict ? 'viewer' : role)
      try {
        const references = await api.canvasTransformReferences(id)
        if (!isCurrent()) return false
        set({ canvasTransformReferences: references })
      } catch {
        if (isCurrent()) set({ canvasTransformReferences: [] })
      }
      if (selectedId && doc.nodes.some((node) => node.id === selectedId)) get().select(selectedId)
      const uid = get().currentUser?.id
      if (uid) localStorage.setItem(OPEN_KEY(uid), id)
      set({ view: 'canvas' })  // opening a file navigates to the editor
      if (inspectConflict) get().pushToast('Opened the server copy read-only. Resolve or fork the local draft before editing this Canvas.', 'info')
      else if (accessRemoved) get().pushToast('This canvas is no longer in your accessible files. Opened the fetched snapshot read-only.', 'error')
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
        if (e.status >= 500) {
          // A proxy/hub 5xx has an unknown commit outcome just like response loss. Preserve the exact
          // attempted document under its stable ID; idempotent create retry will prove insert vs.
          // existing content before any later update.
          persistence = 'local-draft'
        } else if (e.status === 401) {
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
        if (persistence !== 'local-draft') return { ok: false }
      }
      // A transport failure is the one case where local-first creation is truthful: this is a new,
      // collision-resistant local draft and a later idempotent POST can prove or create its server copy.
      persistence = 'local-draft'
    }
    if (!isCurrent()) {
      return { ok: false }
    }
    get().loadDoc(doc, 'owner')
    if (persistence === 'local-draft' && userId) {
      const draft = draftForDoc(userId, doc, null, undefined, doc)
      const stored = writeCanvasDraft(draft)
      const visibleDraft = draftAfterStorageWrite(draft, stored)
      set((state) => ({
        currentDraftId: draft.draftId,
        serverVersion: null,
        localDrafts: replaceDraft(state.localDrafts, visibleDraft),
        saved: stored.ok,
        draftStorageErrors: stored.ok ? state.draftStorageErrors : [...state.draftStorageErrors, stored.error!],
      }))
      if (!stored.ok) get().pushToast(stored.error!, 'error')
    }
    const uid = get().currentUser?.id
    if (uid) localStorage.setItem(OPEN_KEY(uid), doc.id)
    set({ view: 'canvas', firstRunChoice: false })
    if (signal && persistence === 'remote') void get().refreshFiles()
    return { ok: true, canvasId: doc.id, persistence }
  },

  newFromExample: async (key, intent = 'create-separate') => {
    const generation = ++_fileNavigationGeneration
    const userId = get().currentUser?.id ?? null
    const current = get()
    const candidate: ExampleReplacementSnapshot = {
      doc: current.doc,
      canvasRole: current.canvasRole,
      currentDraftId: current.currentDraftId,
      serverVersion: current.serverVersion,
    }
    // The mutation may downgrade a UI-confirmed replacement, but it must never upgrade an action
    // that the UI described as creating a separate Canvas.
    let replacePristine = intent === 'replace-pristine' && isPristineExampleReplacement(candidate)
    // A blank graph is not necessarily pristine: a run is durable user work.  Fail closed when
    // history cannot be read, so an offline/revoked tab creates a separate example instead.
    if (replacePristine) {
      let runsEmpty = false
      try { runsEmpty = (await api.listRuns(current.doc.id)).length === 0 }
      catch { /* fail closed below */ }
      const latest = get()
      if (generation !== _fileNavigationGeneration || (latest.currentUser?.id ?? null) !== userId
          || latest.doc.id !== current.doc.id) return { ok: false }
      const latestCandidate: ExampleReplacementSnapshot = {
        doc: latest.doc,
        canvasRole: latest.canvasRole,
        currentDraftId: latest.currentDraftId,
        serverVersion: latest.serverVersion,
      }
      const sameCandidate = isPristineExampleReplacement(latestCandidate)
        && isSameExampleReplacementSnapshot(candidate, latestCandidate)
      // A user edit can still be inside the autosave debounce window. Navigating to a newly-created
      // example here would replace the in-memory document before that edit is persisted. Cancel this
      // click instead; the edited Canvas stays mounted and autosave can complete normally.
      if (!sameCandidate) {
        get().pushToast('Canvas changed while preparing the example; your edit was kept. Choose the example again.', 'info')
        return { ok: false }
      }
      replacePristine = runsEmpty
    }
    const id = replacePristine ? current.doc.id : `canvas_${Math.floor(performance.now())}_${Math.random().toString(36).slice(2, 8)}`
    const doc = exampleDoc(key, id)  // a runnable starter on the seeded data; falls back to a blank file
    if (!doc) return get().newFile()
    // A response-lost in-place save must retain the known server base. Retrying it as a create could
    // collide with the original Canvas and turn an uncertain update into a different document.
    if (replacePristine) doc.version = current.serverVersion!
    let persistence: CanvasPersistence = 'remote'
    try {
      if (replacePristine) {
        const saved = await api.saveCanvas(doc, false, current.serverVersion ?? undefined)
        if (!saved.ok || saved.id !== doc.id) return { ok: false }
        doc.version = saved.version
      } else {
        const created = await api.createCanvas(doc)
        if (!created.ok || !created.created || created.id !== doc.id) return { ok: false }
      }
      if (generation !== _fileNavigationGeneration || (get().currentUser?.id ?? null) !== userId) return { ok: false }
      rememberRole(userId, doc.id, 'owner') // create response confirms ownership
      await get().refreshFiles()
    } catch (e) {
      if (generation !== _fileNavigationGeneration || (get().currentUser?.id ?? null) !== userId) return { ok: false }
      if (e instanceof KernelError) {
        if (e.status >= 500) {
          persistence = 'local-draft'
        } else if (e.status === 401) {
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
        if (persistence !== 'local-draft') return { ok: false }
      }
      // Transport failure: keep the runnable example as an offline local-first draft.
      persistence = 'local-draft'
    }
    if (generation !== _fileNavigationGeneration || (get().currentUser?.id ?? null) !== userId) return { ok: false }
    get().loadDoc(doc, 'owner')
    if (persistence === 'local-draft' && userId) {
      const draft = draftForDoc(
        userId, doc, replacePristine ? current.serverVersion : null, undefined,
        replacePristine ? null : doc,
      )
      const stored = writeCanvasDraft(draft)
      const visibleDraft = draftAfterStorageWrite(draft, stored)
      set((state) => ({
        currentDraftId: draft.draftId,
        serverVersion: replacePristine ? current.serverVersion : null,
        localDrafts: replaceDraft(state.localDrafts, visibleDraft),
        saved: stored.ok,
        draftStorageErrors: stored.ok ? state.draftStorageErrors : [...state.draftStorageErrors, stored.error!],
      }))
      if (!stored.ok) get().pushToast(stored.error!, 'error')
    }
    const uid = get().currentUser?.id
    if (uid) localStorage.setItem(OPEN_KEY(uid), doc.id)
    set({ view: 'canvas', firstRunChoice: false })
    get().requestViewportFit(get().doc)
    return { ok: true, canvasId: doc.id, persistence }
  },

  renameFile: (name) => {
    if (roleCanEdit(get().canvasRole)) set((s) => ({ doc: { ...s.doc, name } }))
  },  // autosave PUTs + refreshes the list
  setRequirements: (reqs) => {
    if (roleCanEdit(get().canvasRole)) set((s) => ({ doc: { ...s.doc, requirements: reqs } }))
  },  // canvas pip deps; autosave persists
  setParameters: (parameters) => {
    if (!roleCanEdit(get().canvasRole)) return 'You do not have permission to edit Canvas parameters.'
    const currentDoc = get().doc
    const mutation = parameterDeclarationMutation(currentDoc, parameters)
    if (mutation.error) return mutation.error
    const executionDefinition = (value: CanvasParameterDeclaration | undefined) => value && JSON.stringify({
      type: value.type, required: value.required === true,
      default: value.default, constraints: value.constraints,
    })
    const previousByName = new Map((currentDoc.parameters ?? []).map((value) => [value.name, value]))
    const nextByName = new Map(parameters.map((value) => [value.name, value]))
    const changedNames = new Set([...previousByName.keys(), ...nextByName.keys()].filter((name) => (
      executionDefinition(previousByName.get(name)) !== executionDefinition(nextByName.get(name))
    )))
    for (const [previous, next] of mutation.renames) { changedNames.add(previous); changedNames.add(next) }
    const executionChanged = [...changedNames].some((name) => parameterRefUses(currentDoc, name).length > 0)
    set((s) => {
      const nodes = mutation.renames.size
        ? s.doc.nodes.map((node) => ({ ...node, data: {
            ...node.data,
            config: rewriteParameterRef(node.data.config, mutation.renames) as NodeConfig,
          } }))
        : s.doc.nodes
      const runs = executionChanged
        ? Object.fromEntries(Object.entries(s.runs).map(([nodeId, run]) => [nodeId, {
            ...run,
            parametersReady: false,
            estimate: undefined,
            parameterBindings: run.parameterBindings?.flatMap((binding) => {
              const renamed = mutation.renames.get(binding.name) ?? binding.name
              return mutation.changedTypes.has(renamed) ? [] : [{ ...binding, name: renamed }]
            }),
            writeAdmission: undefined,
            writeSubmissionId: undefined,
            writeAdmissionFingerprint: undefined,
          }]))
        : s.runs
      return {
        doc: { ...s.doc, nodes, parameters }, runs,
        ...(executionChanged ? { previews: {}, previewBindings: {}, schemas: {}, sizes: {} } : {}),
      }
    })
    if (executionChanged) writePreviewBindings(get().currentUser?.id, get().doc.id, {})
    return null
  },

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
    let filesRefreshed = false
    try {
      await api.deleteCanvas(id)
      filesRefreshed = await get().refreshFiles()
    } catch { return } // do not navigate away from a Canvas whose deletion was not confirmed
    // only load a replacement (which navigates to the editor) if the deleted file was the one open
    // IN the editor; deleting from the Recents grid should just drop the card and stay in the shell.
    if (get().doc.id === id && get().view === 'canvas') {
      const next = get().files[0]?.id
      if (next) await get().openFile(next)
      // Deleting the final Canvas must not manufacture a replacement. Reuse the complete first-run
      // choice only when the now-empty list and local-draft index were both read authoritatively.
      else set({
        view: 'workspace',
        firstRunChoice: filesRefreshed && get().localDrafts.length === 0,
      })
    }
  },

  refreshLocalDrafts: () => {
    const principalId = get().currentUser?.id
    if (!principalId) {
      set({ localDrafts: [], draftStorageErrors: [], currentDraftId: null })
      return
    }
    const result = readCanvasDrafts(principalId)
    set({ localDrafts: result.drafts, draftStorageErrors: result.errors })
    for (const error of result.errors) get().pushToast(error, 'error')
  },

  openLocalDraft: (draftId) => {
    const principalId = get().currentUser?.id
    const draft = get().localDrafts.find((candidate) => (
      candidate.draftId === draftId && candidate.principalId === principalId
    ))
    if (!draft || !principalId) return false
    const role = draft.baseCanvasId === null ? 'owner' : cachedRole(principalId, draft.canvasId)
    get().loadDoc(draft.doc, role)
    set({
      currentDraftId: draft.draftId,
      serverVersion: draft.baseVersion,
      saved: true,
      view: 'canvas',
    })
    try { localStorage.setItem(OPEN_KEY(principalId), draft.canvasId) } catch { /* visible writes happen through the draft store */ }
    if (!roleCanEdit(role)) get().pushToast('Opened the local draft read-only because current edit access is not confirmed', 'error')
    return true
  },

  retryLocalDraft: async (draftId) => {
    const principalId = get().currentUser?.id
    const original = get().localDrafts.find((candidate) => (
      candidate.draftId === draftId && candidate.principalId === principalId
    ))
    if (!original || !principalId || original.syncState === 'syncing') return
    const syncKey = `${principalId}:${draftId}`
    if (_draftSyncInFlight.has(syncKey)) return
    _draftSyncInFlight.add(syncKey)
    const stillCurrentPrincipal = () => get().currentUser?.id === principalId

    const syncing: LocalCanvasDraft = { ...original, syncState: 'syncing', lastError: undefined }
    const started = writeCanvasDraft(syncing)
    if (!started.ok) {
      _draftSyncInFlight.delete(syncKey)
      const failed = draftAfterStorageWrite(original, started)
      set((state) => ({
        localDrafts: replaceDraft(state.localDrafts, failed),
        saved: false,
        draftStorageErrors: [...state.draftStorageErrors, started.error!],
      }))
      get().pushToast(started.error!, 'error')
      return
    }
    set((state) => ({ localDrafts: replaceDraft(state.localDrafts, syncing) }))

    const finish = async (version: number) => {
      if (!stillCurrentPrincipal()) {
        _draftSyncInFlight.delete(syncKey)
        return
      }
      const latest = get().localDrafts.find((draft) => draft.draftId === draftId)
      if (latest && latest.lastLocalEditAt !== original.lastLocalEditAt
        && !canvasDocsEqual(latest.doc, original.doc)) {
        const rebased: LocalCanvasDraft = {
          ...latest,
          doc: { ...latest.doc, version },
          baseCanvasId: latest.canvasId,
          baseVersion: version,
          syncState: 'dirty',
          lastError: undefined,
        }
        const stored = writeCanvasDraft(rebased)
        const visibleDraft = draftAfterStorageWrite(rebased, stored)
        _acceptingServerVersion = true
        try {
          set((state) => ({
            localDrafts: replaceDraft(state.localDrafts, visibleDraft),
            serverVersion: state.doc.id === rebased.canvasId ? version : state.serverVersion,
            doc: state.doc.id === rebased.canvasId ? { ...state.doc, version } : state.doc,
            saved: state.doc.id === rebased.canvasId ? stored.ok : state.saved,
            draftStorageErrors: stored.ok ? state.draftStorageErrors : [...state.draftStorageErrors, stored.error!],
          }))
        } finally {
          _acceptingServerVersion = false
        }
        if (!stored.ok) get().pushToast(stored.error!, 'error')
        else window.setTimeout(() => { void get().retryLocalDraft(draftId) }, 0)
        _draftSyncInFlight.delete(syncKey)
        return
      }
      const removed = deleteCanvasDraft(principalId, draftId)
      if (!removed.ok) {
        const failed = { ...syncing, syncState: 'error' as const, lastError: removed.error }
        set((state) => ({
          localDrafts: replaceDraft(state.localDrafts, failed), saved: false,
          draftStorageErrors: [...state.draftStorageErrors, removed.error!],
        }))
        get().pushToast(removed.error!, 'error')
        _draftSyncInFlight.delete(syncKey)
        return
      }
      if (original.baseCanvasId === null) rememberRole(principalId, original.canvasId, 'owner')
      _acceptingServerVersion = true
      try {
        set((state) => {
          const isOpen = state.doc.id === original.canvasId
          return {
            localDrafts: state.localDrafts.filter((draft) => draft.draftId !== draftId),
            currentDraftId: isOpen ? null : state.currentDraftId,
            serverVersion: isOpen ? version : state.serverVersion,
            doc: isOpen ? { ...state.doc, version } : state.doc,
            saved: isOpen ? true : state.saved,
            kernelUp: true,
          }
        })
      } finally {
        _acceptingServerVersion = false
      }
      await get().refreshFiles()
      if (stillCurrentPrincipal()) get().pushToast(`Synced “${original.name}”`, 'success')
      _draftSyncInFlight.delete(syncKey)
    }

    try {
      if (original.baseCanvasId === null) {
        const created = await api.createCanvas(original.doc)
        if (!created.ok || created.id !== original.canvasId) throw new Error('The hub returned an incompatible create result.')
        if (created.created) {
          await finish(original.doc.version)
          return
        }
        // A false insert result is the expected response-loss retry. Confirm both ownership and the
        // exact first-attempt document before establishing a server base; an unrelated collision is
        // never overwritten merely because it has the same client-generated ID.
        const [files, serverDoc] = await Promise.all([api.listCanvases(), api.getCanvas(original.canvasId)])
        if (files.find((file) => file.id === original.canvasId)?.role !== 'owner'
          || !original.createAttemptDoc || !canvasDocsEqual(serverDoc, original.createAttemptDoc)) {
          throw new KernelError(409, 'The Canvas ID already exists with different server content.')
        }
        if (canvasDocsEqual(serverDoc, original.doc)) {
          await finish(serverDoc.version)
          return
        }
        const saved = await api.saveCanvas(original.doc, false, serverDoc.version)
        await finish(saved.version)
        return
      }
      if (original.baseVersion == null) throw new Error('The draft has no server base version.')
      const saved = await api.saveCanvas(original.doc, false, original.baseVersion)
      await finish(saved.version)
    } catch (error) {
      if (!stillCurrentPrincipal()) {
        _draftSyncInFlight.delete(syncKey)
        return
      }
      const latest = get().localDrafts.find((draft) => (
        draft.draftId === draftId && draft.principalId === principalId
      ))
      // The request may fail after the user has continued editing or deleted the draft. Never restore
      // the retry's older snapshot over newer local work, and never recreate an explicitly deleted row.
      if (!latest) {
        _draftSyncInFlight.delete(syncKey)
        return
      }
      const conflict = error instanceof KernelError && (error.status === 404 || error.status === 409)
      const denied = error instanceof KernelError && (error.status === 401 || error.status === 403)
      const message = conflict
        ? 'The server Canvas changed or was deleted. Open the server copy or keep this draft as a new Canvas.'
        : denied
          ? 'Current access does not permit syncing this draft.'
          : `Sync failed: ${error instanceof Error ? error.message : 'the hub is unreachable'}`
      const failed: LocalCanvasDraft = {
        ...latest,
        syncState: conflict ? 'conflict' : denied ? 'error' : 'dirty',
        lastError: message,
      }
      const stored = writeCanvasDraft(failed)
      const visibleDraft = draftAfterStorageWrite(failed, stored)
      set((state) => ({
        localDrafts: replaceDraft(state.localDrafts, visibleDraft),
        currentDraftId: state.doc.id === failed.canvasId ? failed.draftId : state.currentDraftId,
        saved: state.doc.id === failed.canvasId ? stored.ok : state.saved,
        kernelUp: conflict || denied ? true : false,
        draftStorageErrors: stored.ok ? state.draftStorageErrors : [...state.draftStorageErrors, stored.error!],
      }))
      if (denied) {
        rememberRole(principalId, failed.canvasId, null)
        set({ accessDenied: true, canvasRole: null, agentOpen: false })
        void get().refreshFiles()
      }
      get().pushToast(stored.ok ? message : stored.error!, 'error')
      _draftSyncInFlight.delete(syncKey)
    }
  },

  forkLocalDraft: async (draftId) => {
    const principalId = get().currentUser?.id
    const original = get().localDrafts.find((candidate) => (
      candidate.draftId === draftId && candidate.principalId === principalId
    ))
    if (!original || !principalId) return
    const newId = emptyDoc().id
    const doc: CanvasDoc = {
      ...original.doc,
      id: newId,
      name: `${original.name || 'untitled'} (recovered)`,
      version: 1,
    }
    const fork = draftForDoc(principalId, doc, null, undefined, doc)
    const stored = writeCanvasDraft(fork)
    if (!stored.ok) {
      get().pushToast(stored.error!, 'error')
      set((state) => ({ saved: false, draftStorageErrors: [...state.draftStorageErrors, stored.error!] }))
      return
    }
    const removed = deleteCanvasDraft(principalId, original.draftId)
    if (!removed.ok) {
      deleteCanvasDraft(principalId, fork.draftId)
      get().pushToast(removed.error!, 'error')
      return
    }
    set((state) => ({
      localDrafts: replaceDraft(
        state.localDrafts.filter((draft) => draft.draftId !== original.draftId), fork,
      ),
    }))
    get().openLocalDraft(fork.draftId)
    await get().retryLocalDraft(fork.draftId)
  },

  discardLocalDraft: async (draftId) => {
    const principalId = get().currentUser?.id
    const draft = get().localDrafts.find((candidate) => (
      candidate.draftId === draftId && candidate.principalId === principalId
    ))
    if (!draft || !principalId) return
    if (typeof window !== 'undefined' && !window.confirm(`Delete local draft “${draft.name}”? This can't be undone.`)) return
    const removed = deleteCanvasDraft(principalId, draftId)
    if (!removed.ok) {
      get().pushToast(removed.error!, 'error')
      return
    }
    const wasOpen = get().doc.id === draft.canvasId && get().currentDraftId === draftId
    const inspectingServer = get().doc.id === draft.canvasId && get().currentDraftId === null
    set((state) => ({
      localDrafts: state.localDrafts.filter((candidate) => candidate.draftId !== draftId),
      currentDraftId: wasOpen ? null : state.currentDraftId,
      canvasRole: inspectingServer ? cachedRole(principalId, draft.canvasId) : state.canvasRole,
    }))
    if (!wasOpen) return
    if (draft.baseCanvasId && await get().openFile(draft.baseCanvasId)) return
    const next = get().localDrafts[0]
    if (next) get().openLocalDraft(next.draftId)
    else if (get().files[0]) await get().openFile(get().files[0].id)
    else await get().newFile()
  },

  exportLocalDraft: (draftId) => {
    const principalId = get().currentUser?.id
    const draft = get().localDrafts.find((candidate) => (
      candidate.draftId === draftId && candidate.principalId === principalId
    ))
    if (!draft) return
    downloadDraft(draft)
    get().pushToast(`Exported local draft “${draft.name}”`, 'success')
  },

  // Refresh the WORKING SET (not the whole catalog): re-fetch the tables the open canvas references,
  // so declared-key / schema / organization edits made elsewhere show up. The Workspace dataset scope + ER view
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
    if (get().doc.nodes.some((node) => numericDraftInvalidReason(node, get().numericParamDrafts[node.id]))) return
    const principalId = get().currentUser?.id
    if (!principalId) return
    const previous = get().localDrafts.find((draft) => draft.draftId === get().doc.id)
    const draft = draftForDoc(principalId, get().doc, previous?.baseVersion ?? get().serverVersion, previous)
    const stored = writeCanvasDraft(draft)
    const visibleDraft = draftAfterStorageWrite(draft, stored)
    set((state) => ({
      currentDraftId: draft.draftId,
      localDrafts: replaceDraft(state.localDrafts, visibleDraft),
      saved: stored.ok,
      draftStorageErrors: stored.ok ? state.draftStorageErrors : [...state.draftStorageErrors, stored.error!],
    }))
    if (!stored.ok) {
      get().pushToast(stored.error!, 'error')
      return
    }
    await get().retryLocalDraft(draft.draftId)
  },

  loadDoc: (doc, role = get().canvasRole) => {
    _cfgEdit = { id: '', t: 0 }
    // A canvas document is not execution authority. A queued/running badge saved in a snapshot can
    // outlive its run, so settle it before the document is ever rendered. reattachRuns below may
    // immediately replace these with the live run's authoritative per-node states.
    const d = settleAnimatingDoc(doc)
    const agentLog = d.id === get().doc.id ? get().agentLog : []
    _settlingLoadedDoc = d !== doc
    try {
      const previewBindings = readPreviewBindings(get().currentUser?.id, d)
      const retainedRuns = Object.fromEntries(Object.entries(previewBindings).flatMap(([nodeId, binding]) => (
        binding.parameterBindings?.length
          ? [[nodeId, { phase: 'idle' as const, parameterBindings: binding.parameterBindings }]]
          : []
      )))
      set({
        doc: d,
        canvasRole: role,
        accessDenied: false,
        saved: true,
        serverVersion: d.version,
        currentDraftId: null,
        agentOpen: roleCanEdit(role) ? get().agentOpen : false,
        // Agent requests are independent. A record from another canvas must never be displayed as
        // context for this one (or suggest that it will be sent with a future request).
        agentLog,
        previews: {}, previewBindings, runs: retainedRuns, profileJobs: {}, numericParamDrafts: {}, openPanels: {}, selectedId: null, selectedIds: [], nodeRevealRequest: null, viewportFitRequest: null, past: [], future: [],
        canvasTransformReferences: [],
      })
    } finally {
      _settlingLoadedDoc = false
    }
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
    localDrafts: [],
    draftStorageErrors: [],
    currentDraftId: null,
    serverVersion: null,
  })
})

// Persist every locally edited Canvas in its principal-scoped draft record before attempting a
// version-fenced server save. Drafts are never retried in the background after a transport failure;
// the Workspace/Canvas chrome exposes an explicit Retry action.
let _saveTimer: ReturnType<typeof setTimeout> | undefined
let _lastDoc: CanvasDoc | undefined
let _bootstrapped = false  // don't autosave the throwaway initial empty doc before the real one loads
useStore.subscribe((s) => {
  if (s.doc === _lastDoc) return
  const previousDoc = _lastDoc
  _lastDoc = s.doc
  if (!_bootstrapped) return  // bootstrap will load & set the real doc; skip persisting anything before that
  if (_settlingLoadedDoc || _acceptingServerVersion) return
  // Running a graph changes presentation-only node badges and the server owns the top-level version.
  // Neither is a local edit, so they must not create a draft or compete with an actual autosave CAS.
  if (previousDoc && canvasEditableContentEqual(previousDoc, s.doc)) return
  // a peer's edit was merged into our doc (via the CRDT) — the editing peer PUTs it, so we must NOT also
  // PUT or claim it as this principal's offline edit. Local edits + local undo/redo still persist.
  if (collabApply.remote) return
  // Viewer/unknown access is fail-closed before any PUT. Store-level mutation guards mean this is
  // normally just a safety net for a server/external document refresh or a role changing mid-debounce.
  if (!roleCanEdit(s.canvasRole)) {
    clearTimeout(_saveTimer)
    if (!s.saved) useStore.setState({ saved: true })
    return
  }
  if (s.saved) useStore.setState({ saved: false })
  clearTimeout(_saveTimer)
  _saveTimer = setTimeout(() => {
    const state = useStore.getState()
    const principalId = state.currentUser?.id
    if (!principalId || !roleCanEdit(state.canvasRole)) return
    if (state.doc.nodes.some((node) => numericDraftInvalidReason(node, state.numericParamDrafts[node.id]))) return
    const doc = state.doc
    const previous = state.localDrafts.find((draft) => draft.draftId === doc.id)
    const draft = draftForDoc(principalId, doc, previous?.baseVersion ?? state.serverVersion, previous)
    const stored = writeCanvasDraft(draft)
    const visibleDraft = draftAfterStorageWrite(draft, stored)
    useStore.setState((current) => ({
      currentDraftId: draft.draftId,
      localDrafts: replaceDraft(current.localDrafts, visibleDraft),
      saved: stored.ok,
      draftStorageErrors: stored.ok ? current.draftStorageErrors : [...current.draftStorageErrors, stored.error!],
    }))
    if (!stored.ok) {
      useStore.getState().pushToast(stored.error!, 'error')
      return
    }
    // A new local-only Canvas is synchronized only through its explicit Retry control. Existing
    // online Canvases retain autosave, now protected by the persisted base-version CAS.
    if (draft.baseVersion != null) void useStore.getState().retryLocalDraft(draft.draftId)
  }, 400)
})

// Flush an edit still inside the debounce to its principal-scoped record on tab close. Do not start a
// remote save during unload: its response cannot reliably advance/remove the local draft, and a
// committed keepalive would therefore leave that draft fenced to a stale server version on reload.
if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', () => {
    const state = useStore.getState()
    if (!_bootstrapped || state.saved || !roleCanEdit(state.canvasRole)) return
    const principalId = state.currentUser?.id
    if (!principalId) return
    const previous = state.localDrafts.find((draft) => draft.draftId === state.doc.id)
    const draft = draftForDoc(principalId, state.doc, previous?.baseVersion ?? state.serverVersion, previous)
    const stored = writeCanvasDraft(draft)
    const visibleDraft = draftAfterStorageWrite(draft, stored)
    useStore.setState((current) => ({
      currentDraftId: draft.draftId,
      localDrafts: replaceDraft(current.localDrafts, visibleDraft),
      saved: stored.ok,
      draftStorageErrors: stored.ok
        ? current.draftStorageErrors
        : [...current.draftStorageErrors, stored.error!],
    }))
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
function settleAnimatingDoc(doc: CanvasDoc): CanvasDoc {
  let changed = false
  const nodes = doc.nodes.map((n) => {
    if (n.data.status === 'running' || n.data.status === 'queued') {
      changed = true
      return { ...n, data: { ...n.data, status: 'stale' as NodeStatus } }
    }
    return n
  })
  return changed ? { ...doc, nodes } : doc
}

function settleAnimatingNodes(set: (p: Partial<Store> | ((s: Store) => Partial<Store>)) => void) {
  set((s) => {
    const doc = settleAnimatingDoc(s.doc)
    return doc === s.doc ? {} : { doc }
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
  const currentPlanByTarget = new Map<string, Promise<{ identity: string; digest: string }>>()

  const currentPlan = (nodeId: string, portId: string,
                       parameterBindings?: CanvasParameterBinding[]) => {
    const targetKey = `${profileJobKeyForDoc(get().doc, nodeId, portId)}:${JSON.stringify(parameterBindings ?? [])}`
    let pending = currentPlanByTarget.get(targetKey)
    if (!pending) {
      const identity = profilePlanIdentity(get().doc, nodeId, portId)
      const doc = get().doc
      pending = (async () => {
        for (let attempt = 0; ; attempt += 1) {
          try {
            const retainedManifest = currentPreviewBinding(get(), nodeId)?.inputManifest
            const currentIdentity = await (retainedManifest
              ? parameterBindings?.length
                ? api.profileIdentity(doc, nodeId, portId, retainedManifest, parameterBindings)
                : api.profileIdentity(doc, nodeId, portId, retainedManifest)
              : parameterBindings?.length
                ? api.profileIdentity(doc, nodeId, portId, undefined, parameterBindings)
                : api.profileIdentity(doc, nodeId, portId))
            if (currentIdentity.targetPortId !== portId) {
              throw new Error('Profile identity returned a different output port')
            }
            const { planDigest: digest } = currentIdentity
            return { identity, digest }
          } catch (error) {
            if (!current() || identity !== profilePlanIdentity(get().doc, nodeId, portId)
                || !retryableProfileRequest(error) || attempt >= PROFILE_RETRY_DELAYS_MS.length) throw error
            await wait(PROFILE_RETRY_DELAYS_MS[attempt])
          }
        }
      })()
      currentPlanByTarget.set(targetKey, pending)
      // A later recovery response must be able to retry after a persistent failure; never retain a
      // rejected Promise as the node's identity authority.
      void pending.catch(() => {
        if (currentPlanByTarget.get(targetKey) === pending) currentPlanByTarget.delete(targetKey)
      })
    }
    return pending
  }

  type RecoveryVerification = 'verifying' | 'verified' | 'failed'
  const installRecoveredState = (
    st: RunStatus,
    verification: RecoveryVerification,
    error?: string,
    parameterBindings?: CanvasParameterBinding[],
  ): { installed: boolean; requestGeneration?: number; blockedByLocalIntent: boolean } => {
    const nodeId = st.targetNodeId!
    const portId = st.targetPortId!
    const jobKey = profileJobKeyForDoc(get().doc, nodeId, portId)
    const attemptOrder = st.profileAttemptOrder!
    let installed = false
    let installedGeneration: number | undefined
    let blockedByLocalIntent = false
    set((s: Store) => {
      if (_reattachRunsGeneration !== reattachGeneration || s.doc.id !== canvasId
          || s.currentUser?.id !== reattachUserId) return {}
      const existingJob = s.profileJobs[jobKey]
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
      const recoveredRuns = verification === 'verified' && parameterBindings
          && !s.runs[nodeId]
        ? { ...s.runs, [nodeId]: {
            phase: 'idle' as const, parameterBindings,
          } }
        : s.runs
      return { runs: recoveredRuns, profileJobs: { ...s.profileJobs, [jobKey]: {
        canvasId, nodeId, portId, principalId: reattachUserId!, canCancel: recoveryCanCancel,
        planIdentity: profilePlanIdentity(s.doc, nodeId, portId),
        planDigest: st.planDigest ?? undefined,
        inputManifest: st.profile?.inputManifest,
        parameterBindings,
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
    const portId = st.targetPortId!
    const jobKey = profileJobKeyForDoc(get().doc, nodeId, portId)
    set((s: Store) => {
      if (_reattachRunsGeneration !== reattachGeneration || s.doc.id !== canvasId
          || s.currentUser?.id !== reattachUserId) return {}
      const existing = s.profileJobs[jobKey]
      if (!existing?.status || existing.identityVerified === true
          || !sameProfileAttempt(existing.status, st)) return {}
      const next = { ...s.profileJobs }
      delete next[jobKey]
      return { profileJobs: next }
    })
  }

  const installProfile = async (st: RunStatus) => {
    const nodeId = st.targetNodeId
    const portId = st.targetPortId
    const attemptOrder = st.profileAttemptOrder
    if (st.jobType !== 'profile' || !nodeId || !portId
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
    let parameterBindings: CanvasParameterBinding[] | undefined
    try {
      if (st.executionManifestSha256) {
        const detail = await api.executionManifest(canvasId, st.runId)
        const raw = detail.document?.parameters
        if (Array.isArray(raw)) {
          parameterBindings = raw.flatMap((item) => {
            if (!item || typeof item !== 'object') return []
            const binding = item as { name?: unknown; value?: unknown }
            const value = binding.value && typeof binding.value === 'object'
              && (binding.value as { kind?: unknown }).kind === 'latest'
              ? Object.fromEntries(Object.entries(binding.value as Record<string, unknown>)
                .filter(([key]) => key !== 'resolvedRevisionId'))
              : binding.value
            return typeof binding.name === 'string'
              ? [{ name: binding.name, value }] : []
          })
        }
      }
      ({ identity: planIdentity, digest: planDigest } = await currentPlan(
        nodeId, portId, parameterBindings))
    } catch (error) {
      if (!current()) {
        superviseRecoveredIfDetached(st)
        return
      }
      const failed = installRecoveredState(
        st,
        'failed',
        `Could not verify the recovered full profile. Statistics are hidden${error instanceof Error && error.message ? `: ${error.message}` : '.'}`,
        parameterBindings,
      )
      if (!failed.installed) superviseRecoveredIfDetached(st)
      return
    }
    if (!current() || planIdentity !== profilePlanIdentity(get().doc, nodeId, portId)) {
      superviseRecoveredIfDetached(st)
      return
    }
    if (!st.planDigest || st.planDigest !== planDigest) {
      discardStaleRecoveredAttempt(st)
      superviseRecoveredIfDetached(st)
      return
    }
    const { installed, requestGeneration } = installRecoveredState(
      st, 'verified', undefined, parameterBindings)
    if (installed && requestGeneration !== undefined && current()
        && (st.status === 'queued' || st.status === 'running')) {
      pollProfile(
        get, set, nodeId, profileJobKeyForDoc(get().doc, nodeId, portId), st.runId,
        requestGeneration, reattachUserId!, recoveryCanCancel,
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
      // active-runs is already the backend's current per-node execution authority. Apply it now
      // rather than waiting for the first poll, so a real reattached run replaces the settled
      // snapshot state without a stale visual gap.
      if (current()) {
        const targetReported = st.perNode?.some((item) => item.nodeId === nodeId)
        applyPerNodeStatus(set, targetReported ? st.perNode : [
          ...(st.perNode ?? []), { nodeId, status: st.status },
        ])
      }
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
                     nodeId: string, jobKey: string, runId: string, requestGeneration: number,
                     principalId: string, canCancel: boolean) {
  if (_profileSubmissionUserId !== principalId) return
  const existing = _profilePolling.get(runId)
  if (existing?.principalId !== undefined && existing.principalId !== principalId) return
  if (existing?.requestGeneration === requestGeneration) return
  const initialStatus = get().profileJobs[jobKey]?.status
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
    const job = get().profileJobs[jobKey]
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
      const current = get().profileJobs[jobKey]
      if (current?.requestGeneration === requestGeneration && current.status?.runId === runId) {
        set((s) => ({ profileJobs: { ...s.profileJobs, [jobKey]: {
          ...(s.profileJobs[jobKey]!), phase: 'failed', error: (e as Error).message || 'Lost track of full profile',
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
    const current = get().profileJobs[jobKey]
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
          const latest = get().profileJobs[jobKey]
          if (!latest || latest.requestGeneration !== requestGeneration
              || latest.status?.runId !== runId || !projected) {
            if (latest?.requestGeneration !== requestGeneration || latest?.status?.runId !== runId) {
              stopPolling()
              return
            }
            set((s) => ({ profileJobs: { ...s.profileJobs, [jobKey]: {
              ...(s.profileJobs[jobKey]!), phase: 'failed',
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
          set((s) => ({ profileJobs: { ...s.profileJobs, [jobKey]: {
            ...(s.profileJobs[jobKey]!), phase: 'failed',
            error: (e as Error).message || 'Lost the durable full-profile projection',
          } } }))
          stopPolling()
          return
        }
      } else {
        set((s) => ({ profileJobs: { ...s.profileJobs, [jobKey]: {
          ...(s.profileJobs[jobKey]!), phase: 'failed',
          error: 'Full profile status identity changed unexpectedly',
        } } }))
        stopPolling()
        return
      }
    }
    if (!sameProfileAttempt(current.status, status)) {
      set((s) => ({ profileJobs: { ...s.profileJobs, [jobKey]: {
        ...(s.profileJobs[jobKey]!), phase: 'failed',
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
    set((s) => ({ profileJobs: { ...s.profileJobs, [jobKey]: {
      ...(s.profileJobs[jobKey]!), status: storedStatus, phase,
      error: !identityVerified && phase !== 'cancelled'
        ? s.profileJobs[jobKey]?.error
        : phase === 'cancelling' ? s.profileJobs[jobKey]?.error : status.error ?? undefined,
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
      set((s: Store) => ({ runs: { ...s.runs, [nodeId]: {
        ...(s.runs[nodeId] ?? { phase } as any), status, phase,
        writeAdmission: undefined, writeSubmissionId: undefined,
        writeAdmissionFingerprint: undefined,
      } } }))
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
