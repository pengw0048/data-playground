import { describe, expect, it } from 'vitest'
import type { ColumnSchema } from '../types/graph'
import { compareSchemas } from './schemaCompatibility'

const field = (name: string, overrides: Partial<ColumnSchema> = {}): ColumnSchema => ({
  name, type: 'int', nullable: true, provenance: 'provider', capabilities: [], ...overrides,
})

describe('compareSchemas evidence semantics', () => {
  it('recognizes a rename only from stable identity', () => {
    const result = compareSchemas(
      [field('old_name', { fieldId: 'field-1' })],
      [field('new_name', { fieldId: 'field-1' })],
    )
    expect(result.status).toBe('compatible')
    expect(result.fields[0]).toMatchObject({ kind: 'renamed', status: 'compatible', fieldId: 'field-1' })

    const uncertain = compareSchemas([field('old_name')], [field('new_name')])
    expect(uncertain.status).toBe('unknown')
    expect(uncertain.fields[0]).toMatchObject({ kind: 'removed', status: 'unknown' })
  })

  it('reports widening as compatible and narrowing as breaking', () => {
    expect(compareSchemas([field('value', { type: 'int' })], [field('value', { type: 'bigint' })]).status).toBe('compatible')
    expect(compareSchemas([field('value', { type: 'bigint' })], [field('value', { type: 'int' })]).status).toBe('breaking')
  })

  it('does not guess whether evidence-poor additions are safe', () => {
    const unknown = compareSchemas([], [field('value', { nullable: null, hasDefault: null })])
    expect(unknown.status).toBe('unknown')
    const breaking = compareSchemas([], [field('value', { nullable: false, hasDefault: false })])
    expect(breaking.status).toBe('breaking')
  })
})
