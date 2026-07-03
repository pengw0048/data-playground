// The canvas document model (PRD §8). Node ids are stable + globally unique (CRDT-friendly).
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
  outputSchema?: ColumnSchema[]
  // join
  on?: string
  how?: 'inner' | 'left'
  // sql
  sql?: string
  // metric
  agg?: 'count' | 'mean' | 'sum' | 'min' | 'max'
  column?: string
  // write
  name?: string
  writeMode?: 'append' | 'merge' | 'overwrite'
  // loop
  maxIters?: number
  budgetUsd?: number
  // generic
  [k: string]: unknown
}

export interface LastRun {
  rows: number
  ms: number
  cost: number
  placement: 'local' | 'distributed'
}

export interface NodeVersion {
  id: string
  ts: number
  rows?: number
  cost?: number
  label: string
  config: NodeConfig
}

export interface NodeData {
  title: string
  status: NodeStatus
  config: NodeConfig
  meta?: string
  bypassed?: boolean
  muted?: boolean
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
