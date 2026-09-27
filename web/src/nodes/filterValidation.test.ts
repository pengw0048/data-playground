import { describe, expect, it } from 'vitest'
import {
  filterBuilderConditions, filterBuilderReason, isScalarNumericType, parseFilterConditions,
  serializeFilterConditions,
} from './filterValidation'

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

  it.each(['total cost', 'a"b', 'select', 'nested.value', 'where OR (AND)', ' padded '])(
    'quotes the whole schema column %s and preserves its name while editing', (name) => {
      const schema = [{ name, type: 'int' }]
      const conditions = [{ col: name, op: '>=', val: '9007199254740993', type: 'int' }]
      const config = { filterBuilder: { conditions } }
      const edited = filterBuilderConditions(config)!.map((condition) => ({ ...condition, val: '9007199254740995' }))
      const quoted = `"${name.replaceAll('"', '""')}"`
      expect(serializeFilterConditions(conditions, schema)).toBe(`${quoted} >= 9007199254740993`)
      expect(serializeFilterConditions(edited, schema)).toBe(`${quoted} >= 9007199254740995`)
      expect(conditions[0].col).toBe(name)
    },
  )

  it('quotes known columns for null conditions and preserves unknown expression spelling', () => {
    expect(serializeFilterConditions([{ col: 'select', op: 'IS NULL', val: '' }], [{ name: 'select' }]))
      .toBe('"select" IS NULL')
    expect(serializeFilterConditions([{ col: 'length(event)', op: '>', val: '3' }], columns))
      .toBe('length(event) > 3')
    expect(serializeFilterConditions([{ col: 'a.id', op: '>', val: '3' }], columns))
      .toBe('a.id > 3')
  })

  it('round-trips quoted identifiers, escaped quotes, and quoted conjunctions', () => {
    const predicate = '"OR (AND)" >= 1 AND "say ""yes""" = 2 AND "select" IS NOT NULL'
    const schema = ['OR (AND)', 'say "yes"', 'select'].map((name) => ({ name, type: 'int' }))
    const parsed = parseFilterConditions(predicate)
    expect(parsed).toHaveLength(3)
    expect(serializeFilterConditions(parsed!, schema)).toBe(predicate)
    expect(serializeFilterConditions(parsed!, [])).toBe(predicate)
    const stringPredicate = '"event" = \'OR (AND) and it\'\'s quoted\''
    expect(serializeFilterConditions(parseFilterConditions(stringPredicate)!, columns)).toBe(stringPredicate)
  })

  it('applies schema-backed validation to a parsed quoted identifier', () => {
    const parsed = parseFilterConditions('"id" > nope')!
    expect(filterBuilderReason(parsed, columns)).toBe('Enter a number for "id"')
  })

  it.each(['id > 1 OR id < 0', '(id > 1)', 'length(event) > 1', '"unfinished > 1', "event = 'unfinished"])(
    'leaves unsupported raw SQL in the raw editor: %s', (predicate) => {
      expect(parseFilterConditions(predicate)).toBeNull()
    },
  )

  it.each(['1 OR TRUE', '1; SELECT 2', "1'", 'Infinity', 'NaN', '0x10'])(
    'rejects expressions and non-decimal input as numeric values: %s', (val) => {
      expect(filterBuilderReason([{ col: 'id', op: '>', val }], columns)).toBe('Enter a number for id')
      expect(serializeFilterConditions([{ col: 'id', op: '>', val }], columns)).toBe('')
    },
  )

  it('preserves ordinary numeric threshold spelling without converting through Number', () => {
    for (const val of ['9007199254740993', '-9007199254740993', '+12.3400', '.125', '99999999999999999999999999999999999999']) {
      expect(serializeFilterConditions([{ col: 'id', op: '>', val }], columns)).toBe(`"id" > ${val}`)
    }
  })

  it('uses an exact decimal cast only when literal spelling would make DuckDB choose DOUBLE', () => {
    const val = '0.12345678901234567890123456789012345678'
    const schema = [{ name: 'amount', type: 'float', physicalType: 'DECIMAL(38,38)' }]
    const condition = { col: 'amount', op: '>=', val, type: 'DECIMAL(38,38)' }
    const saved = filterBuilderConditions({ filterBuilder: { conditions: [condition] } })!
    expect(serializeFilterConditions(saved, schema)).toBe(`"amount" >= CAST('${val}' AS DECIMAL(38,38))`)
    expect(saved[0].val).toBe(val)
    expect(serializeFilterConditions([{ ...saved[0], val: '1.2500' }], schema)).toBe('"amount" >= 1.2500')
  })

  it('prefers the physical numeric type when logical schema is coarser', () => {
    expect(filterBuilderReason([{ col: 'amount', op: '>', val: 'not-a-number' }], [
      { name: 'amount', type: 'number', physicalType: 'DECIMAL(20,4)' },
    ])).toBe('Enter a number for amount')
  })

  it.each([
    ['1e3', 4, 0], ['9007199254740993e0', 16, 0], ['-1.25e2', 3, 0], ['1.25e-2', 4, 4],
    ['0.001e3', 1, 0], ['1e-38', 38, 38], ['+1e+0003', 4, 0],
  ])('preserves exact scientific input %s for integer and decimal columns', (val, precision, scale) => {
    const condition = { col: 'id', op: '>=', val }
    expect(filterBuilderReason([condition], columns)).toBeNull()
    expect(serializeFilterConditions([condition], columns))
      .toBe(`"id" >= CAST('${val}' AS DECIMAL(${precision},${scale}))`)
  })

  it('keeps floating scientific notation and rejects non-finite floating values', () => {
    const schema = [{ name: 'ratio', type: 'float', physicalType: 'DOUBLE' }]
    expect(serializeFilterConditions([{ col: 'ratio', op: '>', val: '1e100' }], schema)).toBe('"ratio" > 1e100')
    expect(filterBuilderReason([{ col: 'ratio', op: '>', val: '1e999' }], schema)).toBe('Enter a number for ratio')
  })

  it('rejects numeric spellings outside the exact builder range with useful messages', () => {
    expect(filterBuilderReason([{ col: 'id', op: '=', val: '1e-39' }], columns))
      .toBe('Use at most 38 digits for id, including decimal places')
    expect(filterBuilderReason([{ col: 'id', op: '=', val: '1e99999999' }], columns))
      .toBe('Use at most 38 digits for id, including decimal places')
    expect(filterBuilderReason([{ col: 'id', op: '=', val: `0.${'1'.repeat(39)}` }], columns))
      .toBe('Use at most 38 digits for id, including decimal places')
    expect(filterBuilderReason([{ col: 'id', op: '=', val: '1'.repeat(39) }], columns))
      .toBe('Use at most 38 digits for id, including decimal places')
  })
})

describe('scalar numeric column types', () => {
  it.each(['int', 'uint64', 'BIGINT', 'UBIGINT', 'HUGEINT', 'float', 'float32', 'DOUBLE', 'double precision', 'real',
    'DECIMAL(38, 18)', 'decimal128(38, 18)', 'decimal256(76, 38)', 'numeric', 'numeric(12, 2)'])(
    'accepts %s', (type) => { expect(isScalarNumericType(type)).toBe(true) },
  )
  it.each(['list<int>', 'int[]', 'STRUCT(amount BIGINT)', 'map<string, int>', 'interval', 'point', 'boolean', 'string',
    'decimal128(38, 18)[]', 'array<float>', 'DECIMAL(38,2); SELECT 1', 'not-a-number', '', undefined])(
    'rejects %s', (type) => { expect(isScalarNumericType(type)).toBe(false) },
  )
})
