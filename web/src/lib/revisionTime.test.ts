import { describe, expect, it } from 'vitest'
import { datasetRevisionDate, datasetRevisionTimeLabel } from './revisionTime'

describe('dataset revision time', () => {
  it('treats legacy core-owned offset-free timestamps as UTC instants', () => {
    expect(datasetRevisionDate('2026-07-31T21:37:00', 'core')?.toISOString())
      .toBe('2026-07-31T21:37:00.000Z')
    expect(datasetRevisionDate('2026-07-31 21:37:00.123456', 'core')?.toISOString())
      .toBe('2026-07-31T21:37:00.123Z')
  })

  it('preserves explicit core offsets and provider-owned naive semantics', () => {
    expect(datasetRevisionDate('2026-07-31T17:37:00-04:00', 'core')?.toISOString())
      .toBe('2026-07-31T21:37:00.000Z')
    expect(datasetRevisionDate('2026-07-31T21:37:00', 'provider')?.valueOf())
      .toBe(new Date('2026-07-31T21:37:00').valueOf())
  })

  it('keeps invalid provider evidence visible instead of inventing an instant', () => {
    expect(datasetRevisionTimeLabel('provider-clock-value', 'provider')).toBe('provider-clock-value')
    expect(datasetRevisionTimeLabel(null, 'core')).toBeNull()
  })
})
