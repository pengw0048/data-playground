import { describe, expect, it } from 'vitest'
import {
  formatTemporalTick, isTemporalColumnType, NO_DATE_LABEL, normalizeTimeBucket, parseTemporalUtc,
  temporalTickBudget, temporalTickGranularity,
} from './chartTemporal'

const HOUR = 3_600_000
const DAY = 24 * HOUR

describe('temporal chart axis helpers', () => {
  it('recognizes typed date/timestamp columns only', () => {
    expect(isTemporalColumnType('timestamp[us]')).toBe(true)
    expect(isTemporalColumnType('TIMESTAMP WITH TIME ZONE')).toBe(true)
    expect(isTemporalColumnType('date32[day] DATE')).toBe(true)
    expect(isTemporalColumnType('datetime')).toBe(true)
    expect(isTemporalColumnType('time64[us]')).toBe(false)
    expect(isTemporalColumnType('interval')).toBe(false)
    expect(isTemporalColumnType('string VARCHAR')).toBe(false)
    expect(isTemporalColumnType('int64')).toBe(false)
  })

  it('bounds persisted bucket values to the supported set', () => {
    expect(normalizeTimeBucket('month')).toBe('month')
    expect(normalizeTimeBucket('none')).toBeUndefined()
    expect(normalizeTimeBucket('fortnight')).toBeUndefined()
    expect(normalizeTimeBucket(undefined)).toBeUndefined()
  })

  it('parses ISO cells as UTC regardless of the browser time zone', () => {
    expect(parseTemporalUtc('2024-01-05')).toBe(Date.UTC(2024, 0, 5))
    expect(parseTemporalUtc('2024-02-29T14:30:00')).toBe(Date.UTC(2024, 1, 29, 14, 30))
    expect(parseTemporalUtc('2024-02-29 14:30:00')).toBe(Date.UTC(2024, 1, 29, 14, 30))
    expect(parseTemporalUtc('2024-01-05T03:00:00+05:00')).toBe(Date.UTC(2024, 0, 4, 22))
    expect(parseTemporalUtc('2024-01-05T00:00:00Z')).toBe(Date.UTC(2024, 0, 5))
    expect(parseTemporalUtc(null)).toBeNull()
    expect(parseTemporalUtc(42)).toBeNull()
    expect(parseTemporalUtc('view')).toBeNull()
    expect(parseTemporalUtc('2024-13-45')).toBeNull()
  })

  it('formats ticks at the bucket granularity in UTC', () => {
    const leapNoon = Date.UTC(2024, 1, 29, 14, 0)
    expect(formatTemporalTick(leapNoon, 'hour')).toBe('Feb 29 14:00')
    expect(formatTemporalTick(leapNoon, 'hour', true)).toBe("Feb 29 '24 14:00")
    expect(formatTemporalTick(leapNoon, 'day')).toBe('Feb 29')
    expect(formatTemporalTick(leapNoon, 'day', true)).toBe("Feb 29 '24")
    expect(formatTemporalTick(leapNoon, 'month')).toBe('Feb 2024')
    expect(formatTemporalTick(Date.UTC(2024, 9, 1), 'quarter')).toBe('Q4 2024')
    expect(formatTemporalTick(leapNoon, 'year')).toBe('2024')
    expect(formatTemporalTick(null, 'day')).toBe(NO_DATE_LABEL)
  })

  it('sizes inferred granularity to the axis span and keeps week buckets on day labels', () => {
    expect(temporalTickGranularity('week', 0)).toBe('day')
    expect(temporalTickGranularity('hour', 400 * DAY)).toBe('hour')
    expect(temporalTickGranularity(undefined, 10 * 365 * DAY)).toBe('year')
    expect(temporalTickGranularity(undefined, 365 * DAY)).toBe('month')
    expect(temporalTickGranularity(undefined, 30 * DAY)).toBe('day')
    expect(temporalTickGranularity(undefined, 6 * HOUR)).toBe('hour')
  })

  it('bounds tick density so the widest label cannot overlap', () => {
    expect(temporalTickBudget(4, 576)).toBe(8)
    expect(temporalTickBudget(12, 576)).toBe(6)
    expect(temporalTickBudget(15, 576)).toBe(5)
    expect(temporalTickBudget(200, 576)).toBe(2)
  })
})
