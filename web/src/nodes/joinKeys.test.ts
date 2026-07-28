import { describe, expect, it } from 'vitest'
import { parseJoinCondition, parseJoinKeys, serializeJoinKeys } from './joinKeys'

describe('Join key builder contract', () => {
  it('loads legacy same-name on keys and preserves their order', () => {
    expect(parseJoinKeys('account_id, region', '')).toEqual([
      { left: 'account_id', right: 'account_id' }, { left: 'region', right: 'region' },
    ])
    expect(serializeJoinKeys([{ left: 'account_id', right: 'account_id' }, { left: 'region', right: 'region' }]))
      .toEqual({ on: 'account_id, region', condition: '' })
  })

  it('round-trips heterogeneous and quoted key pairs through the existing condition contract', () => {
    const pairs = parseJoinCondition('a._rowid = b.original_row_id AND a."account id" = b."legacy id"')
    expect(pairs).toEqual([{ left: '_rowid', right: 'original_row_id' }, { left: 'account id', right: 'legacy id' }])
    expect(serializeJoinKeys(pairs!)).toEqual({
      on: '', condition: 'a._rowid = b.original_row_id AND a."account id" = b."legacy id"',
    })
  })

  it('declines arbitrary predicates so the caller can preserve them in Advanced mode', () => {
    expect(parseJoinCondition('a.id = b.user_id OR a.email = b.email')).toBeNull()
    expect(parseJoinCondition('lower(a.email) = b.email')).toBeNull()
  })
})
