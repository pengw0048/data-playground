import { describe, expect, it } from 'vitest'
import { failedRunNode, presentRunError } from './runErrors'
import type { CanvasNode } from '../types/graph'

describe('presentRunError', () => {
  it('turns an engine function signature into an actionable column error', () => {
    const raw = `at 'aggregate': BinderException: Binder Error: No function matches the given name and argument types 'avg(VARCHAR)'.
Candidate functions:
avg(DECIMAL) -> DECIMAL
avg(DOUBLE) -> DOUBLE`

    const result = presentRunError(raw, { config: { aggs: 'avg(subject) AS average_subject' } })

    expect(result.summary).toBe('“subject” is a text column. Average needs a number column. Choose a numeric column or change the summary.')
    expect(result.details).toBe(raw)
    expect(result.summary).not.toContain('VARCHAR')
    expect(result.summary).not.toContain('BinderException')
  })

  it('explains sandbox time limits without exposing the exception class', () => {
    expect(presentRunError("at 'transform': SandboxError: cell exceeded the 8s time budget").summary)
      .toBe('This code exceeded the 8s time limit. Make the operation smaller or use a different compute backend.')
  })

  it('keeps the raw diagnostic behind details for unfamiliar errors', () => {
    const result = presentRunError("at 'filter': ConversionException: bad value", { nodeTitle: 'Keep paid rows' })
    expect(result.summary).toBe('Keep paid rows: bad value')
    expect(result.details).toContain('ConversionException')
  })

  it('preserves the server attribution when its failing node is not available', () => {
    const result = presentRunError("at 'Calculate amount': KeyError: 'missing_amount'")
    expect(result.summary).toBe("Calculate amount: 'missing_amount'")
    expect(result.details).toBe("at 'Calculate amount': KeyError: 'missing_amount'")
  })

  it('keeps graph internals out of the primary explanation', () => {
    const raw = "invalid graph: edge 'e-9' references missing source node 'gone'"
    const result = presentRunError(raw)

    expect(result.summary).toBe('This branch is not ready to run. Check its connections and required fields.')
    expect(result.details).toBe(raw)
  })
})

describe('failedRunNode', () => {
  const nodes: CanvasNode[] = [{ id: 'python', type: 'transform', position: { x: 0, y: 0 },
    data: { title: 'Calculate amount', status: 'failed', config: { source: 'adhoc' } } },
  { id: 'write', type: 'write', position: { x: 300, y: 0 },
    data: { title: 'Save rows', status: 'failed', config: {} } }]

  it('uses the error-bearing step rather than a failed or blocked downstream target', () => {
    expect(failedRunNode({ status: 'failed', perNode: [
      { nodeId: 'write', status: 'failed' },
      { nodeId: 'python', status: 'failed', error: "KeyError: 'missing_amount'" },
    ] }, nodes)).toBe(nodes[0])
  })

  it('does not guess when attribution is missing, deleted, ambiguous, or not terminal', () => {
    expect(failedRunNode(undefined, nodes)).toBeUndefined()
    expect(failedRunNode({ status: 'failed' } as never, nodes)).toBeUndefined()
    expect(failedRunNode({ status: 'failed', perNode: [{ nodeId: 'python', status: 'failed', error: '  ' }] }, nodes)).toBeUndefined()
    expect(failedRunNode({ status: 'failed', perNode: [{ nodeId: 'deleted', status: 'failed', error: 'Bad column' }] }, nodes)).toBeUndefined()
    expect(failedRunNode({ status: 'running', perNode: [{ nodeId: 'python', status: 'failed', error: 'Bad column' }] }, nodes)).toBeUndefined()
    expect(failedRunNode({ status: 'failed', perNode: [
      { nodeId: 'python', status: 'failed', error: 'Bad column' },
      { nodeId: 'write', status: 'failed', error: 'Another failure' },
    ] }, nodes)).toBeUndefined()
  })
})
