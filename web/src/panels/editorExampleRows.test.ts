import { describe, expect, it } from 'vitest'
import {
  EDITOR_EXAMPLE_MAX_BYTES,
  EDITOR_EXAMPLE_MAX_ROWS,
  editorExampleRowsStarter,
  validateEditorExampleRows,
} from './editorExampleRows'

describe('editor Example rows fixture', () => {
  it('reports a bounded object-row schema', () => {
    expect(validateEditorExampleRows('[{"name":"Ada","score":1},{"score":2,"name":"Lin"}]'))
      .toMatchObject({
        ok: true,
        rowCount: 2,
        fields: ['name', 'score'],
      })
  })

  it.each([
    ['{', 'valid JSON'],
    ['{"value":1}', 'JSON array'],
    ['[]', 'at least one'],
    ['[1]', 'JSON object'],
    ['[{}]', 'at least one field'],
    ['[{"a":1},{"b":2}]', 'same fields'],
    [JSON.stringify(Array.from(
      { length: EDITOR_EXAMPLE_MAX_ROWS + 1 }, (_, value) => ({ value })),
    ), 'at most 20 rows'],
    ['[{"value":"' + '界'.repeat(EDITOR_EXAMPLE_MAX_BYTES / 2) + '"}]', 'UTF-8 bytes'],
  ])('rejects %s with actionable feedback', (fixture, message) => {
    const result = validateEditorExampleRows(fixture)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.error).toContain(message)
  })

  it('builds a small useful starter from the known upstream schema', () => {
    const starter = editorExampleRowsStarter([
      {
        name: 'count', type: 'int', physicalType: 'int64',
        capabilities: [],
      },
      {
        name: 'active', type: 'bool', physicalType: 'boolean',
        capabilities: [],
      },
      {
        name: 'label', type: 'string', physicalType: 'varchar',
        capabilities: [],
      },
    ])

    expect(JSON.parse(starter)).toEqual([{
      count: 1,
      active: true,
      label: 'example',
    }])
    expect(validateEditorExampleRows(starter).ok).toBe(true)
  })

  it('keeps known Join integer columns numeric for an int + 1 code test', () => {
    const starter = JSON.parse(editorExampleRowsStarter([
      { name: 'id', type: 'bigint', capabilities: [] },
      { name: 'user_id', type: 'int64', capabilities: [] },
      { name: 'width', type: 'smallint', capabilities: [] },
      { name: 'height', type: 'hugeint', capabilities: [] },
      { name: 'ratio', type: 'double', capabilities: [] },
      { name: 'enabled', type: 'boolean', capabilities: [] },
      { name: 'tags', type: 'list<string>', capabilities: [] },
      { name: 'metadata', type: 'struct<kind: string>', capabilities: [] },
      { name: 'opaque', type: 'unknown', capabilities: [] },
    ])) as Array<Record<string, unknown>>

    const row = starter[0]
    expect(row).toMatchObject({
      id: 1, user_id: 1, width: 1, height: 1, ratio: 1.5, enabled: true,
      tags: [], metadata: { example: 'value' }, opaque: null,
    })
    expect((row.id as number) + 1).toBe(2)
    expect(validateEditorExampleRows(JSON.stringify(starter)).ok).toBe(true)
  })
})
