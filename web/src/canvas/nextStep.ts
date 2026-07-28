import { canConnect, getSpec, nodeOutputs } from '../nodes/registry'
import type { CanvasNode, PortSpec } from '../types/graph'
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
export function uniqueNextStepConnection(source: CanvasNode, targetKind: string): NextStepConnection | null {
  const target = getSpec(targetKind)
  if (!target) return null
  const pairs: Array<{ output: PortSpec; input: PortSpec }> = []
  for (const output of nodeOutputs(source)) {
    for (const input of target.inputs) {
      if (canConnect(output.wire, targetKind, input.id)) pairs.push({ output, input })
    }
  }
  if (pairs.length !== 1) return null
  const [{ output, input }] = pairs
  return { sourceHandle: output.id, targetHandle: input.id, wire: output.wire }
}
