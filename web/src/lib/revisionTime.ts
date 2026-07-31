export type RevisionRetentionOwner = 'core' | 'provider'

const CORE_NAIVE_ISO_DATETIME = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)$/

/** Core commit timestamps are UTC instants; provider-owned naive values keep provider semantics. */
export function datasetRevisionDate(
  value: string | null | undefined,
  retentionOwner: RevisionRetentionOwner,
): Date | null {
  if (!value) return null
  const coreNaive = retentionOwner === 'core' ? value.match(CORE_NAIVE_ISO_DATETIME) : null
  const parsed = new Date(coreNaive ? `${coreNaive[1]}T${coreNaive[2]}Z` : value)
  return Number.isNaN(parsed.valueOf()) ? null : parsed
}

export function datasetRevisionTimeLabel(
  value: string | null | undefined,
  retentionOwner: RevisionRetentionOwner,
): string | null {
  if (!value) return null
  return datasetRevisionDate(value, retentionOwner)?.toLocaleString() ?? value
}
