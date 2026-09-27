import { getSpec, nodeOutputs, type NodeSpec } from '../nodes/registry'
import type { CanvasDoc, CanvasEdge, NodeData } from '../types/graph'
import { cycleConnectionReason } from './connectionCycle'

export type EdgeInsertionSnapshot = {
  canvasId: string
  edge: Pick<CanvasEdge, 'id' | 'source' | 'target' | 'sourceHandle' | 'targetHandle'>
}

/** An open picker owns these endpoints, not whichever connection later reuses the edge id. */
export function insertionEdge(doc: CanvasDoc, snapshot: EdgeInsertionSnapshot): CanvasEdge | undefined {
  if (doc.id !== snapshot.canvasId) return undefined
  const expected = snapshot.edge
  return doc.edges.find((edge) => edge.id === expected.id
    && edge.source === expected.source && edge.target === expected.target
    && (edge.sourceHandle ?? null) === (expected.sourceHandle ?? null)
    && (edge.targetHandle ?? null) === (expected.targetHandle ?? null))
}

/** Only unambiguous linear steps can replace a wire without leaving another input unconnected. */
export function insertionPorts(doc: CanvasDoc, edge: CanvasEdge, spec: NodeSpec, data?: NodeData) {
  if (spec.inputs.length !== 1 || cycleConnectionReason(doc.edges, edge, edge.id)) return null
  const source = doc.nodes.find((node) => node.id === edge.source)
  const target = doc.nodes.find((node) => node.id === edge.target)
  if (!source || !target) return null
  const sourceOutputs = nodeOutputs(source)
  const targetInputs = getSpec(target.type)?.inputs ?? []
  const upstream = edge.sourceHandle != null
    ? sourceOutputs.find((port) => port.id === edge.sourceHandle)
    : sourceOutputs.length === 1 ? sourceOutputs[0] : undefined
  const downstream = edge.targetHandle != null
    ? targetInputs.find((port) => port.id === edge.targetHandle)
    : targetInputs.length === 1 ? targetInputs[0] : undefined
  const outputs = nodeOutputs({ id: '', type: spec.kind, position: { x: 0, y: 0 }, data: data ?? spec.defaultData() })
  if (!upstream || !downstream || outputs.length !== 1) return null
  const input = spec.inputs[0]
  const output = outputs[0]
  if (!(input.accepts ?? [input.wire]).includes(upstream.wire)
      || !(downstream.accepts ?? [downstream.wire]).includes(output.wire)) return null
  if (!downstream.multi && doc.edges.some((candidate) => candidate.id !== edge.id
      && candidate.target === target.id
      && (candidate.targetHandle ?? downstream.id) === downstream.id)) return null
  return { input, output, sourceWire: upstream.wire }
}
