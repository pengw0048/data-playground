// Kernel HTTP client. The canvas builds fine with no kernel; data/preview/run need it.
import type {
  CatalogTable, CompilePlan, KernelInfo, LineageResult, PipelineImport,
  ProcessorDescriptor, RunEstimate, RunStatus, SampleResult,
} from '../types/api'
import type { CanvasDoc, ColumnSchema } from '../types/graph'

const BASE = '/api'

export class KernelError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
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
  return res.json() as Promise<T>
}

// Strip transient UI-only fields the kernel does not need before sending a graph.
// `note` nodes are canvas annotations with no ports/lowering — the engine never sees them.
function toGraph(doc: CanvasDoc) {
  const dataNodes = doc.nodes.filter((n) => n.type !== 'note')
  const dataIds = new Set(dataNodes.map((n) => n.id))
  return {
    id: doc.id,
    version: doc.version,
    nodes: dataNodes.map((n) => ({
      id: n.id,
      type: n.type,
      position: n.position,
      data: { title: n.data.title, config: n.data.config, bypassed: n.data.bypassed },
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

export interface BackendPort { id: string; label?: string; wire: string; accepts?: string[] }
export interface BackendParam { name: string; type: string; default?: unknown; options?: string[]; label?: string; lang?: string }
export interface BackendNodeSpec {
  kind: string; title: string; category: string; tag?: string
  inputs: BackendPort[]; outputs: BackendPort[]; params: BackendParam[]
  canBypass: boolean; previewable: boolean; blurb: string
}

export interface AgentBackendNode { id: string; type: string; position: { x: number; y: number }; data: { title?: string; config?: Record<string, unknown> } }
export interface AgentBackendEdge { id: string; source: string; target: string; sourceHandle?: string | null; targetHandle?: string | null; data?: { wire: string } }
export interface AgentResult {
  available: boolean
  reason?: string
  summary?: string
  transcript?: { tool: string; input: Record<string, unknown>; result: Record<string, unknown> }[]
  graph?: { nodes: AgentBackendNode[]; edges: AgentBackendEdge[] }
}

export const api = {
  kernel: () => req<KernelInfo>('/kernel'),
  health: () => req<{ ok: boolean }>('/health'),
  nodes: () => req<BackendNodeSpec[]>('/nodes'),
  registerFile: (uri: string, name?: string) =>
    req<CatalogTable>('/catalog/register', { method: 'POST', body: JSON.stringify({ uri, name }) }),

  tables: (q?: string) => req<CatalogTable[]>(`/catalog/tables${q ? `?q=${encodeURIComponent(q)}` : ''}`),
  table: (id: string) => req<CatalogTable>(`/catalog/tables/${encodeURIComponent(id)}`),
  lineage: (uri: string) => req<LineageResult>(`/catalog/lineage?uri=${encodeURIComponent(uri)}`),

  sample: (uri: string, k = 50, columns?: string[]) =>
    req<SampleResult>('/data/sample', { method: 'POST', body: JSON.stringify({ uri, k, columns }) }),

  processors: () => req<ProcessorDescriptor[]>('/processors'),
  promote: (body: {
    id: string; title: string; mode: string; code: string
    inputColumns: string[]; outputSchema: ColumnSchema[]; blurb?: string
  }) => req<ProcessorDescriptor>('/processors/promote', { method: 'POST', body: JSON.stringify(body) }),

  importPipeline: (config: string, params?: Record<string, unknown>) =>
    req<PipelineImport>('/pipelines/import', { method: 'POST', body: JSON.stringify({ config, params }) }),

  compile: (doc: CanvasDoc, targetNodeId?: string) =>
    req<CompilePlan>('/graph/compile', { method: 'POST', body: JSON.stringify({ graph: toGraph(doc), targetNodeId }) }),

  preview: (doc: CanvasDoc, nodeId: string, k = 50) =>
    req<SampleResult>('/run/preview', { method: 'POST', body: JSON.stringify({ graph: toGraph(doc), nodeId, k }) }),

  estimate: (doc: CanvasDoc, targetNodeId?: string) =>
    req<RunEstimate>('/run/estimate', { method: 'POST', body: JSON.stringify({ graph: toGraph(doc), targetNodeId }) }),

  run: (doc: CanvasDoc, targetNodeId?: string, confirmed = false) =>
    req<RunStatus>('/run', { method: 'POST', body: JSON.stringify({ graph: toGraph(doc), targetNodeId, confirmed }) }),

  runStatus: (runId: string) => req<RunStatus>(`/run/${runId}`),
  cancelRun: (runId: string) => req<RunStatus>(`/run/${runId}/cancel`, { method: 'POST' }),

  agentStatus: () => req<{ available: boolean; reason: string; model?: string }>('/agent'),
  agentAct: (doc: CanvasDoc, outcome: string) =>
    req<AgentResult>('/agent', { method: 'POST', body: JSON.stringify({ outcome, graph: toGraph(doc) }) }),

  listCanvases: () => req<{ id: string; name: string; version: number }[]>('/canvas'),
  getCanvas: (id: string) => req<CanvasDoc>(`/canvas/${id}`),
  saveCanvas: (doc: CanvasDoc) =>
    req<{ ok: boolean; id: string }>(`/canvas/${doc.id}`, { method: 'PUT', body: JSON.stringify(doc) }),
}

export { toGraph }
