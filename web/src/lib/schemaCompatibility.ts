import type { ColumnSchema } from '../types/graph'
import type {
  SchemaCompatibility, SchemaCompatibilityStatus, SchemaFieldCompatibility,
} from '../types/api'

const NUMERIC_TYPE_RANK: Record<string, number> = {
  tinyint: 0, int8: 0,
  smallint: 1, int16: 1,
  int: 2, integer: 2, int32: 2,
  bigint: 3, int64: 3,
  float: 4, real: 4, float32: 4,
  double: 5, float64: 5,
}

function typeChange(beforeRaw: string, afterRaw: string): [SchemaCompatibilityStatus, string] {
  const before = beforeRaw.trim().toLowerCase()
  const after = afterRaw.trim().toLowerCase()
  if (!before || !after) return ['unknown', 'logical type is unknown']
  if (before === after) return ['compatible', 'logical type is unchanged']
  const oldRank = NUMERIC_TYPE_RANK[before]
  const newRank = NUMERIC_TYPE_RANK[after]
  if (oldRank != null && newRank != null) {
    if (newRank === oldRank) return ['compatible', `logical types ${before} and ${after} are equivalent`]
    if (newRank > oldRank) return ['compatible', `logical type widens from ${before} to ${after}`]
    return ['breaking', `logical type narrows from ${before} to ${after}`]
  }
  return ['breaking', `logical type changes from ${before} to ${after}`]
}

function matchedField(before: ColumnSchema, after: ColumnSchema): [SchemaCompatibilityStatus, string] {
  const [typeStatus, reason] = typeChange(before.type, after.type)
  if (typeStatus === 'breaking') return [typeStatus, reason]
  if (before.nullable == null || after.nullable == null) {
    return ['unknown', `${reason}; nullability is not proven on both versions`]
  }
  if (before.nullable && !after.nullable) return ['breaking', `${reason}; field became non-nullable`]
  if (!before.nullable && after.nullable) return ['compatible', `${reason}; field became nullable`]
  return [typeStatus, reason]
}

function addition(field: ColumnSchema): [SchemaCompatibilityStatus, string] {
  if (field.nullable === true) return ['compatible', 'nullable field was added']
  if (field.nullable === false && field.hasDefault === true) {
    return ['compatible', 'non-nullable field was added with a default']
  }
  if (field.nullable === false && field.hasDefault === false) {
    return ['breaking', 'non-nullable field was added without a default']
  }
  return ['unknown', 'added field has unknown nullability or default evidence']
}

function overall(fields: SchemaFieldCompatibility[]): SchemaCompatibilityStatus {
  if (fields.some((field) => field.status === 'breaking')) return 'breaking'
  if (fields.some((field) => field.status === 'unknown')) return 'unknown'
  return 'compatible'
}

// Keep this evaluator aligned with the #125 server contract. Names are used only when neither side
// supplies conflicting stable identity; evidence-poor disappearance remains unknown rather than a
// claimed remove/rename.
export function compareSchemas(before: ColumnSchema[], after: ColumnSchema[]): SchemaCompatibility {
  const beforeIds = new Set(before.flatMap((field) => field.fieldId ? [field.fieldId] : []))
  const afterIds = new Set(after.flatMap((field) => field.fieldId ? [field.fieldId] : []))
  const duplicateIds = [...new Set([...beforeIds, ...afterIds])].filter((fieldId) =>
    before.filter((field) => field.fieldId === fieldId).length > 1
    || after.filter((field) => field.fieldId === fieldId).length > 1)
  if (duplicateIds.length) {
    const uncertain: SchemaFieldCompatibility[] = duplicateIds.sort().map((fieldId) => ({
      kind: 'changed', status: 'unknown', fieldId,
      reason: 'stable field identity is duplicated and cannot prove a match',
    }))
    const remaining = compareSchemas(
      before.filter((field) => !field.fieldId || !duplicateIds.includes(field.fieldId)),
      after.filter((field) => !field.fieldId || !duplicateIds.includes(field.fieldId)),
    )
    const fields = [...uncertain, ...remaining.fields]
    return { status: overall(fields), fields }
  }

  const byId = new Map(after.flatMap((field, index) => field.fieldId ? [[field.fieldId, index] as const] : []))
  const matchedAfter = new Set<number>()
  const stableMatches = new Map<number, number>()
  const fields: SchemaFieldCompatibility[] = []
  before.forEach((field, index) => {
    const next = field.fieldId ? byId.get(field.fieldId) : undefined
    if (next != null) { stableMatches.set(index, next); matchedAfter.add(next) }
  })

  before.forEach((old, oldIndex) => {
    let newIndex = stableMatches.get(oldIndex)
    const matchedById = newIndex != null
    if (newIndex == null) {
      newIndex = after.findIndex((field, index) => !matchedAfter.has(index) && field.name === old.name)
      if (newIndex < 0) newIndex = undefined
    }
    const next = newIndex == null ? undefined : after[newIndex]
    if (next && !matchedById && (old.fieldId || next.fieldId)) {
      fields.push({
        kind: 'changed', status: 'unknown', oldName: old.name, newName: next.name,
        fieldId: old.fieldId || next.fieldId,
        reason: 'field identity is missing or changed, so the name match is not proven stable',
      })
      matchedAfter.add(newIndex!)
      return
    }
    if (!next) {
      const completeStableIdentity = !!old.fieldId && afterIds.size === after.length
      fields.push({
        kind: 'removed', status: completeStableIdentity ? 'breaking' : 'unknown',
        oldName: old.name, fieldId: old.fieldId,
        reason: completeStableIdentity
          ? 'stable field identity is absent from the newer complete schema'
          : 'field is absent by name; no stable identity proves removal versus rename',
      })
      return
    }
    matchedAfter.add(newIndex!)
    const [status, reason] = matchedField(old, next)
    const renamed = matchedById && old.name !== next.name
    fields.push({
      kind: renamed ? 'renamed' : old.name === next.name ? 'unchanged' : 'changed',
      status, fieldId: matchedById ? old.fieldId : null, oldName: old.name, newName: next.name,
      reason: `${renamed ? `renamed from ${old.name}; ` : ''}${reason}`,
    })
  })

  after.forEach((field, index) => {
    if (matchedAfter.has(index)) return
    const [status, reason] = addition(field)
    fields.push({ kind: 'added', status, newName: field.name, fieldId: field.fieldId, reason })
  })
  return { status: overall(fields), fields }
}
