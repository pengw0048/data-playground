/** Temporal Chart X helpers shared by the node editor, the renderer, and tests. All parsing and
 * formatting is UTC so tick labels match the engine's UTC bucket boundaries. */

export type TimeBucket = 'hour' | 'day' | 'week' | 'month' | 'quarter' | 'year'

export const TIME_BUCKETS: TimeBucket[] = ['hour', 'day', 'week', 'month', 'quarter', 'year']

export const NO_DATE_LABEL = 'No date'

const TEMPORAL_TYPE = /(?:^|[^a-z0-9_])(?:date|datetime|timestamp)/i

export function normalizeTimeBucket(value: unknown): TimeBucket | undefined {
  return TIME_BUCKETS.includes(value as TimeBucket) ? value as TimeBucket : undefined
}

/** True for typed date/timestamp columns (not TIME/INTERVAL/DURATION). */
export function isTemporalColumnType(type: string): boolean {
  return TEMPORAL_TYPE.test(type)
}

const ISO_VALUE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/

/** Epoch ms for an ISO date/timestamp cell; offset-less values are read as UTC. Null when absent
 * or not temporal. */
export function parseTemporalUtc(value: unknown): number | null {
  if (value instanceof Date) return Number.isFinite(value.getTime()) ? value.getTime() : null
  if (typeof value !== 'string') return null
  const match = ISO_VALUE.exec(value.trim())
  if (!match) return null
  const [, year, month, day, hour, minute, second, offset] = match
  if (offset) {
    const epoch = Date.parse(value.trim().replace(' ', 'T'))
    return Number.isFinite(epoch) ? epoch : null
  }
  const epoch = Date.UTC(
    Number(year), Number(month) - 1, Number(day),
    Number(hour ?? 0), Number(minute ?? 0), Number(second ?? 0),
  )
  if (!Number.isFinite(epoch)) return null
  const date = new Date(epoch)
  // Date.UTC rolls invalid components over (2024-13-45, 2023-02-29); require a round-trip.
  const roundTrips = date.getUTCFullYear() === Number(year)
    && date.getUTCMonth() === Number(month) - 1
    && date.getUTCDate() === Number(day)
    && date.getUTCHours() === Number(hour ?? 0)
    && date.getUTCMinutes() === Number(minute ?? 0)
    && date.getUTCSeconds() === Number(second ?? 0)
  return roundTrips ? epoch : null
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

const DAY_MS = 86_400_000

/** Tick granularity: the bucket when one is chosen, else sized to the axis span. */
export function temporalTickGranularity(bucket: TimeBucket | undefined, spanMs: number): TimeBucket {
  if (bucket) return bucket === 'week' ? 'day' : bucket
  const spacing = spanMs / 7
  if (spacing >= 270 * DAY_MS) return 'year'
  if (spacing >= 27 * DAY_MS) return 'month'
  if (spacing >= 22 * 3_600_000) return 'day'
  return 'hour'
}

/** A short, fixed-locale UTC tick label ("Jan 5 '24", "Q1 2024", "Feb 29 14:00"). */
export function formatTemporalTick(
  epochMs: number | null,
  granularity: TimeBucket,
  withYear = false,
): string {
  if (epochMs == null || !Number.isFinite(epochMs)) return NO_DATE_LABEL
  const date = new Date(epochMs)
  const year = date.getUTCFullYear()
  const month = MONTHS[date.getUTCMonth()]
  const day = date.getUTCDate()
  const shortYear = ` '${String(year % 100).padStart(2, '0')}`
  if (granularity === 'year') return String(year)
  if (granularity === 'quarter') return `Q${Math.floor(date.getUTCMonth() / 3) + 1} ${year}`
  if (granularity === 'month') return `${month} ${year}`
  if (granularity === 'hour') {
    const time = `${String(date.getUTCHours()).padStart(2, '0')}:${String(date.getUTCMinutes()).padStart(2, '0')}`
    return `${month} ${day}${withYear ? shortYear : ''} ${time}`
  }
  return `${month} ${day}${withYear ? shortYear : ''}`
}

/** Ticks that fit the plot without overlapping at the widest label, between 2 and 8. */
export function temporalTickBudget(maxLabelChars: number, plotWidth: number): number {
  const slot = Math.max(1, maxLabelChars) * 6.2 + 12
  return Math.max(2, Math.min(8, Math.floor(plotWidth / slot)))
}
