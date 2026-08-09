import { describe, expect, it } from 'vitest'
import type { ColumnSchema } from '../../types/graph'
import {
  chartableColumns,
  numericChartColumns,
  seriesChartColumns,
  suggestedChartDimension,
  suggestedChartMeasure,
  timeBucketableChartColumn,
} from './chart'

const columns: ColumnSchema[] = [
  { name: 'id', type: 'int64', capabilities: [] },
  { name: 'event', type: 'string', capabilities: [] },
  { name: 'owner_id', type: 'string', capabilities: [] },
  { name: 'user_id', type: 'int64', capabilities: [] },
  { name: 'amount', type: 'float64', capabilities: [] },
  { name: 'created_at', type: 'timestamp[us]', capabilities: [] },
  { name: 'embedding', type: 'list<float32>', capabilities: [] },
]

describe('Chart schema recommendations', () => {
  it('starts with a useful category and business measure instead of identifiers', () => {
    expect(suggestedChartDimension(columns)?.name).toBe('event')
    expect(suggestedChartMeasure(columns, 'event')?.name).toBe('amount')
  })

  it('offers scalar input fields while keeping Y choices numeric', () => {
    expect(chartableColumns(columns).map((column) => column.name)).toEqual([
      'id', 'event', 'owner_id', 'user_id', 'amount', 'created_at',
    ])
    expect(numericChartColumns(columns).map((column) => column.name)).toEqual([
      'id', 'user_id', 'amount',
    ])
    expect(seriesChartColumns(columns).map((column) => column.name)).toEqual(['event', 'owner_id'])
    expect(seriesChartColumns(columns, 'event').map((column) => column.name)).toEqual(['owner_id'])
  })

  it('falls back through time and scalar fields for unfamiliar schemas', () => {
    expect(suggestedChartDimension([
      { name: 'created_at', type: 'timestamp', capabilities: [] },
      { name: 'value', type: 'double', capabilities: [] },
    ])?.name).toBe('created_at')
    expect(suggestedChartDimension([
      { name: 'row_id', type: 'bigint', capabilities: [] },
      { name: 'score', type: 'double', capabilities: [] },
    ])?.name).toBe('score')
  })

  it('offers time buckets for typed date/timestamp X columns only', () => {
    expect(timeBucketableChartColumn({ name: 'created_at', type: 'timestamp[us]', capabilities: [] })).toBe(true)
    expect(timeBucketableChartColumn({
      name: 'seen_at', type: 'timestamp', physicalType: 'TIMESTAMP WITH TIME ZONE', capabilities: [],
    })).toBe(true)
    expect(timeBucketableChartColumn({ name: 'day', type: 'date32[day]', capabilities: [] })).toBe(true)
    expect(timeBucketableChartColumn({ name: 'wake_time', type: 'time64[us]', capabilities: [] })).toBe(false)
    expect(timeBucketableChartColumn({ name: 'gap', type: 'interval', capabilities: [] })).toBe(false)
    expect(timeBucketableChartColumn({ name: 'date_label', type: 'string', capabilities: [] })).toBe(false)
  })

  it('does not recommend image bytes or row identifiers as a chart dimension', () => {
    const providerImageSchema: ColumnSchema[] = [
      { name: 'image_res_2048', type: 'bytes', physicalType: 'BLOB', capabilities: [] },
      { name: 'source_rowid', type: 'int', physicalType: 'BIGINT', capabilities: [] },
      { name: '_rowid', type: 'int', physicalType: 'UBIGINT', capabilities: [] },
    ]

    expect(chartableColumns(providerImageSchema).map((column) => column.name)).toEqual([
      'source_rowid', '_rowid',
    ])
    expect(suggestedChartDimension(providerImageSchema)).toBeUndefined()
    expect(suggestedChartMeasure(providerImageSchema)).toBeUndefined()
  })
})
