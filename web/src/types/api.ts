// Kernel API DTOs (PRD §9) — camelCase on the wire, mirrors kernel/models.py.
import type { ColumnSchema } from './graph'

export interface KernelInfo {
  mode: 'local' | 'distributed'
  backend: string
  warm: boolean
  version: string
  adapters: string[]
  runners: string[]
  processors: string[]
  capabilities: string[]
}

export interface CatalogTable {
  id: string
  name: string
  uri: string
  rowCount?: number | null
  version?: string | null
  columns: ColumnSchema[]
  updatedAt?: string | null
  meta?: string | null
}

export interface LineageNode { id: string; name: string; uri: string; kind: string }
export interface LineageEdge { parent: string; child: string; column?: string | null; pipeline?: string | null }
export interface LineageResult { nodes: LineageNode[]; edges: LineageEdge[] }

export interface SampleResult {
  columns: ColumnSchema[]
  rows: Record<string, unknown>[]
  rowCount?: number | null
  truncated: boolean
  previewRef?: string | null
  notPreviewable: boolean
  error?: boolean
  reason?: string | null
  wire: string
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
  rows: number
  seconds: number
  costUsd: number
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
}

export interface RunStatus {
  runId: string
  status: RunState
  rowsProcessed: number
  totalRows?: number | null
  costUsd: number
  ms: number
  placement: Placement
  perNode: PerNodeStatus[]
  error?: string | null
  outputUri?: string | null
  outputTable?: string | null
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
}
