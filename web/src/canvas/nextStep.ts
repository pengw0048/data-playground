import { canConnect, getSpec, nodeOutputs } from '../nodes/registry'
import type { CanvasEdge, CanvasNode, PortSpec } from '../types/graph'
import type { WireType } from '../theme/tokens'

export type NextStepConnection = {
  sourceHandle: string
  targetHandle: string
  wire: WireType
}

/**
 * A selected node can imply a next step only when there is one, and only one,
 * concrete output-to-input connection. Keeping this stricter than the port-started
 * picker avoids silently choosing a branch or a target input for the researcher.
 */
export function uniqueNextStepConnection(
  source: CanvasNode,
  targetKind: string,
  edges: readonly CanvasEdge[] = [],
): NextStepConnection | null {
  if (edges.some((edge) => edge.source === source.id)) return null
  const target = getSpec(targetKind)
  if (!target) return null
  const pairs: Array<{ output: PortSpec; input: PortSpec }> = []
  for (const output of nodeOutputs(source)) {
    for (const input of target.inputs) {
      if (canConnect(output.wire, targetKind, input.id)) pairs.push({ output, input })
    }
  }
  if (pairs.length !== 1) {
    // Join is the one built-in exception to the otherwise deliberately strict rule below. Its
    // registry defines `a` as the left dataset input, so a selected result has an unambiguous
    // contextual meaning without guessing the second dataset, keys, or join type. Keep this
    // scoped to the built-in kind rather than inventing primary-input metadata for plugins.
    const left = target.kind === 'join' && (target.source == null || target.source === 'builtin')
      ? target.inputs.find((input) => input.id === 'a')
      : undefined
    const datasetOutputs = nodeOutputs(source).filter((output) => (
      output.wire === 'dataset' && left && canConnect(output.wire, targetKind, left.id)
    ))
    const [output] = datasetOutputs
    if (!left || !output || datasetOutputs.length !== 1) return null
    return { sourceHandle: output.id, targetHandle: left.id, wire: output.wire }
  }
  const [{ output, input }] = pairs
  return { sourceHandle: output.id, targetHandle: input.id, wire: output.wire }
}
