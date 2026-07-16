// The canvas document model. Node ids are stable + globally unique (CRDT-friendly).
import type { WireType } from '../theme/tokens'

export type NodeStatus = 'draft' | 'latest' | 'stale' | 'queued' | 'running' | 'failed'
export type TransformSource = 'library' | 'adhoc'
export type ProcessorMode =
  | 'map' | 'map_batches' | 'filter' | 'flat_map' | 'flat_map_generator'
  | 'callable' | 'aggregate' | string

export interface PortSpec {
  id: string
  label?: string
  wire: WireType // primary type — decides the port's shape/color
  accepts?: WireType[] // input ports may accept a small compatible set (defaults to [wire])
  optional?: boolean
  multi?: boolean // an input port that accepts MANY incoming edges (e.g. union stacks N inputs)
}

export interface NodeConfig {
  // source
  uri?: string
  tableId?: string
  // sample
  n?: number
  seed?: number
  method?: string
  // filter
  predicate?: string
  // transform (two forms)
  source?: TransformSource
  processor?: string
  version?: string
  params?: Record<string, unknown>
  code?: string | null
  io?: { inputs: PortSpec[]; outputs: PortSpec[] } | null
  mode?: ProcessorMode
  onError?: 'raise' | 'skip'
  scope?: 'dataset' | 'sample'  // code node: label for whether it works over the full dataset or a sample
  outputSchema?: ColumnSchema[] | { ref: string; version?: number }  // inline contract, OR a ref to a named workspace contract
  outputSchemaSource?: 'declared' | 'inferred'  // how outputSchema was filled (for the UI hint)
  outputSchemaCodeHash?: string           // hash of the cell when the contract was pinned → detect drift
  // join
  on?: string
  how?: 'inner' | 'left' | 'right' | 'outer'
  // sql
  sql?: string
  // metric / chart
  agg?: 'none' | 'count' | 'mean' | 'sum' | 'min' | 'max'
  column?: string
  // chart
  chartType?: 'bar' | 'line' | 'scatter' | 'area'
  x?: string
  y?: string
  // write
  name?: string
  writeMode?: 'append' | 'overwrite'
  // generic
  [k: string]: unknown
}

export interface LastRun {
  rows?: number
  outputCount?: number
  ms: number
  placement: 'local' | 'distributed'
}

export interface NodeVersion {
  id: string
  ts: number
  rows?: number
  outputCount?: number
  label: string
  config: NodeConfig
}

export interface NodeData {
  title: string
  status: NodeStatus
  config: NodeConfig
  meta?: string
  bypassed?: boolean   // skip this node — its input flows straight through to its output
  disabled?: boolean   // turn this node (and everything downstream of it) OFF — nothing runs
  lastRun?: LastRun
  needsFullPass?: boolean
  history?: NodeVersion[]
  [k: string]: unknown
}

export interface CanvasNode {
  id: string
  type: string
  position: { x: number; y: number }
  data: NodeData
  parentId?: string | null // visual containment: lives inside a section (position is then relative to it)
}

export interface CanvasEdge {
  id: string
  source: string
  target: string
  sourceHandle?: string | null
  targetHandle?: string | null
  data?: { wire: WireType }
}

export interface CanvasDoc {
  id: string
  name?: string
  version: number
  nodes: CanvasNode[]
  edges: CanvasEdge[]
  requirements?: string[]  // pip specs this canvas needs; its kernel installs them (travels with the canvas)
}

export interface ColumnSchema {
  fieldId?: string | null
  name: string
  type: string // logical type
  physicalType?: string | null
  nullable?: boolean | null
  hasDefault?: boolean | null
  provenance?: 'inferred' | 'declared' | 'provider'
  capabilities: string[]
}
