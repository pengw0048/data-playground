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
}

export interface NodeConfig {
  // source
  uri?: string
  table?: string
  tableId?: string
  // sample
  n?: number
  seed?: number
  method?: string
  // filter / branch
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
  outputSchema?: ColumnSchema[]
  // join
  on?: string
  how?: 'inner' | 'left'
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
  // loop
  maxIters?: number
  budgetUsd?: number
  // generic
  [k: string]: unknown
}

export interface LastRun {
  rows: number
  ms: number
  placement: 'local' | 'distributed'
}

export interface NodeVersion {
  id: string
  ts: number
  rows?: number
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
}

export interface ColumnSchema {
  name: string
  type: string
  capabilities: string[]
}
