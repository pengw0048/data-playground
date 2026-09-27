import { describe, expect, it } from 'vitest'
import { parseSortKeys, serializeSortKeys } from './sortKeys'

describe('column sort keys', () => {
  it('reads directions and null placement separately from exact quoted column names', () => {
    expect(parseSortKeys(' "kind, ""label""" desc nulls last, "ASC" NULLS FIRST, "name DESC", id ')).toEqual([
      { col: 'kind, "label"', dir: 'DESC', nulls: 'LAST' },
      { col: 'ASC', dir: 'ASC', nulls: 'FIRST' },
      { col: 'name DESC', dir: 'ASC' },
      { col: 'id', dir: 'ASC' },
    ])
  })

  it('quotes every selected column, including keywords, while preserving order and null placement', () => {
    const keys = [
      { col: 'a, "quoted" name', dir: 'DESC', nulls: 'LAST' },
      { col: 'DESC', dir: 'ASC', nulls: 'FIRST' },
      { col: 'contains (parentheses)', dir: 'ASC' },
      { col: ' leading and trailing ', dir: 'ASC' },
    ] as const
    const sql = serializeSortKeys([...keys])
    expect(sql).toBe('"a, ""quoted"" name" DESC NULLS LAST, "DESC" ASC NULLS FIRST, "contains (parentheses)" ASC, " leading and trailing " ASC')
    expect(parseSortKeys(sql)).toEqual(keys)
  })

  it.each([
    "coalesce(score, 0) DESC", 'score + penalty DESC', 'payload.score DESC',
    'CASE WHEN score IS NULL THEN 0 ELSE score END', 'name COLLATE nocase', '1 DESC',
    'id,', 'id,, name', '"unclosed', 'id DESC NULLS', 'id DESC; SELECT 1',
    'ALL DESC', 'NULL', 'TRUE DESC', 'CURRENT_DATE DESC', 'CURRENT_ROLE', 'USER',
  ])('leaves expressions and incomplete SQL to the raw editor: %s', (sql) => {
    expect(parseSortKeys(sql)).toBeNull()
  })

  it('still accepts quoted expression words and ordinary date-related column names', () => {
    expect(parseSortKeys('"ALL" DESC, "NULL", date, month')).toEqual([
      { col: 'ALL', dir: 'DESC' }, { col: 'NULL', dir: 'ASC' },
      { col: 'date', dir: 'ASC' }, { col: 'month', dir: 'ASC' },
    ])
  })

  it('accepts an empty order and omits only truly empty draft fields', () => {
    expect(parseSortKeys('  ')).toEqual([])
    expect(serializeSortKeys([{ col: '', dir: 'ASC' }, { col: ' ', dir: 'DESC' }])).toBe('" " DESC')
  })
})
