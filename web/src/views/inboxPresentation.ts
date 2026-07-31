import type { InboxItemDto, InboxTaskKind } from '../api/client'

export function inboxOutcomeLabel(item: InboxItemDto): string {
  if (item.outcome === 'completed') return 'Completed'
  if (item.outcome === 'cancelled') return 'Cancelled'
  return 'Failed'
}

export function inboxOutcomeSummary(item: InboxItemDto): string {
  if (item.taskKind === 'restore_revision_write' && item.outcome === 'completed') return 'Revision restored'
  if (item.taskKind === 'keyed_upsert_write' && item.outcome === 'completed') return 'Revision upserted'
  if (item.completedWrite) {
    return `“${item.completedWrite.outputName}” written · ${item.completedWrite.rowCount} rows`
  }
  if (item.outcome === 'failed' && item.diagnosticCode) return item.diagnosticCode.replace(/_/g, ' ')
  if (item.outcome === 'failed') return 'Work failed'
  return item.outcome === 'cancelled' ? 'Cancelled before completion' : 'Finished successfully'
}

const TASK_KIND_LABELS: Record<InboxTaskKind, string> = {
  managed_local_write: 'Managed local write',
  external_wait: 'External wait',
  linear_checkpoint_write: 'Checkpointed write',
  bounded_fanout_write: 'Bounded fan-out write',
  merge_columns_write: 'Merge columns write',
  restore_revision_write: 'Dataset restore',
  keyed_upsert_write: 'Keyed upsert',
}

export function inboxKindLabel(kind: InboxItemDto['taskKind'] | string): string {
  return TASK_KIND_LABELS[kind as InboxTaskKind] ?? `Unknown task type: ${kind}`
}

export function inboxRelativeTime(iso: string): string {
  const timestamp = Date.parse(iso)
  if (Number.isNaN(timestamp)) return ''
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000))
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  if (seconds < 604800) return `${Math.round(seconds / 86400)}d ago`
  return `${Math.round(seconds / 604800)}w ago`
}
