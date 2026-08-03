import { describe, expect, it } from 'vitest'
import { presentRunError } from './runErrors'

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

  it('keeps graph internals out of the primary explanation', () => {
    const raw = "invalid graph: edge 'e-9' references missing source node 'gone'"
    const result = presentRunError(raw)

    expect(result.summary).toBe('This branch is not ready to run. Check its connections and required fields.')
    expect(result.details).toBe(raw)
  })
})
