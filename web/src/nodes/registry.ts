// Node & Capability registries. A node kind = a registered (spec, component)
// pair. New capability = register(), never a new hardcoded card type (P3). The rest of the
// frontend must not branch on specific kinds outside their own plugin file.
import type { ComponentType } from 'react'
import type { WireType, Category } from '../theme/tokens'
import type { CanvasNode, NodeData, PortSpec } from '../types/graph'
import type { ColumnSchema } from '../types/graph'

export interface NodeSpec {
  kind: string
  title: string
  category: Category
  tag?: string // uppercase type tag shown on the card; defaults to kind
  inputs: PortSpec[]
  outputs: PortSpec[]
  canBypass: boolean
  defaultData: () => NodeData
  blurb: string
  // Optional: dynamically resolve output wire types (e.g. a source's out depends on nothing,
  // but a transform's could vary). Defaults to static `outputs`.
}

export interface NodeComponentProps {
  id: string
  data: NodeData
  selected?: boolean
}

const specs = new Map<string, NodeSpec>()
const components = new Map<string, ComponentType<NodeComponentProps>>()

export function register(spec: NodeSpec, component: ComponentType<NodeComponentProps>): void {
  specs.set(spec.kind, spec)
  components.set(spec.kind, component)
}

export function allSpecs(): NodeSpec[] {
  return [...specs.values()]
}

export function getSpec(kind: string): NodeSpec | undefined {
  return specs.get(kind)
}

export function getComponent(kind: string): ComponentType<NodeComponentProps> | undefined {
  return components.get(kind)
}

/**
 * A node's output ports. Usually the static spec, but a node may declare instance-specific
 * output ports via `config.outputs` (a multi-output node — e.g. a section that emit()s several
 * named result sets). All instance ports carry the `dataset` wire.
 */
export function nodeOutputs(node: CanvasNode): PortSpec[] {
  const declared = (node.data?.config as Record<string, unknown> | undefined)?.outputs
  if (Array.isArray(declared) && declared.length > 0) {
    return declared.map((h) => ({ id: String(h), label: String(h), wire: 'dataset' as WireType }))
  }
  return specs.get(node.type)?.outputs ?? []
}

/** Drives connection validity (§5.3): the wire type of one port on a node. */
export function portWire(
  nodes: CanvasNode[],
  nodeId: string,
  handleId: string | null | undefined,
  side: 'source' | 'target',
): WireType | null {
  const node = nodes.find((n) => n.id === nodeId)
  if (!node) return null
  const spec = specs.get(node.type)
  if (!spec) return null
  const ports = side === 'source' ? nodeOutputs(node) : spec.inputs
  if (ports.length === 0) return null
  const port = handleId ? ports.find((p) => p.id === handleId) : ports[0]
  return (port ?? ports[0])?.wire ?? null
}

/** Types the target port accepts (defaults to its primary wire). */
export function portAccepts(kind: string, handleId: string | null | undefined): WireType[] {
  const spec = specs.get(kind)
  if (!spec || spec.inputs.length === 0) return []
  const port = handleId ? spec.inputs.find((p) => p.id === handleId) : spec.inputs[0]
  const p = port ?? spec.inputs[0]
  return p.accepts ?? [p.wire]
}

/** Does the target port accept MANY incoming edges (e.g. union)? Then the one-edge-per-port rule
 * doesn't apply and several wires can land on the same handle. */
export function portMulti(kind: string, handleId: string | null | undefined): boolean {
  const spec = specs.get(kind)
  if (!spec || spec.inputs.length === 0) return false
  const port = handleId ? spec.inputs.find((p) => p.id === handleId) : spec.inputs[0]
  return !!(port ?? spec.inputs[0])?.multi
}

/** Is a connection from a source wire type into (kind, handle) valid? (§5.3 / FR-W1) */
export function canConnect(sourceWire: WireType | null, targetKind: string, targetHandle: string | null | undefined): boolean {
  if (!sourceWire) return false
  return portAccepts(targetKind, targetHandle).includes(sourceWire)
}

/** Node kinds whose first input accepts the given wire — drives the connect-from-port menu. */
export function kindsAcceptingWire(w: WireType): NodeSpec[] {
  return [...specs.values()].filter((s) => {
    if (s.inputs.length === 0) return false
    const first = s.inputs[0]
    return (first.accepts ?? [first.wire]).includes(w)
  })
}

// ---- Capability registry (§5.4) ----
export interface CapabilitySpec {
  id: string
  label: string
  predicate: (columns: ColumnSchema[]) => boolean
  viewerTab?: ComponentType<{ columns: ColumnSchema[]; rows: Record<string, unknown>[] }>
}

const capabilities = new Map<string, CapabilitySpec>()

export function registerCapability(spec: CapabilitySpec): void {
  capabilities.set(spec.id, spec)
}

export function allCapabilities(): CapabilitySpec[] {
  return [...capabilities.values()]
}

export function capabilitiesFor(columns: ColumnSchema[]): CapabilitySpec[] {
  return [...capabilities.values()].filter((c) => c.predicate(columns))
}
