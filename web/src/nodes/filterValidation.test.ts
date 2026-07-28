import { describe, expect, it } from 'vitest'
import { filterBuilderReason, serializeFilterConditions } from './filterValidation'

const columns = [
  { name: 'id', type: 'BIGINT', capabilities: [] },
  { name: 'active', type: 'BOOLEAN', capabilities: [] },
  { name: 'event', type: 'VARCHAR', capabilities: [] },
]

describe('structured Filter conditions', () => {
  it.each([
    [[{ col: 'id', op: '=', val: '' }], 'Enter a number for id'],
    [[{ col: 'id', op: '=', val: 'not-a-number' }], 'Enter a number for id'],
    [[{ col: 'active', op: '=', val: '' }], 'Enter true or false for active'],
    [[{ col: 'active', op: '=', val: 'yes' }], 'Enter true or false for active'],
    [[{ col: 'event', op: '=', val: '' }], 'Enter a value for event'],
    [[{ col: '', op: '=', val: '1' }], 'Choose a column'],
  ])('rejects incomplete or mistyped conditions %#', (conditions, reason) => {
    expect(filterBuilderReason(conditions, columns)).toBe(reason)
  })

  it.each([
    [[{ col: 'id', op: '=', val: '42' }]],
    [[{ col: 'active', op: '=', val: 'false' }]],
    [[{ col: 'event', op: '=', val: 'purchase' }]],
    [[{ col: 'id', op: 'IS NULL', val: '' }]],
    [[{ col: 'id', op: 'IS NOT NULL', val: '' }]],
  ])('accepts valid numeric, boolean, string, and null-aware conditions %#', (conditions) => {
    expect(filterBuilderReason(conditions, columns)).toBeNull()
  })

  it('never serializes an incomplete numeric condition as an empty string comparison', () => {
    expect(serializeFilterConditions([{ col: 'id', op: '=', val: '' }], columns)).toBe('')
  })
})
