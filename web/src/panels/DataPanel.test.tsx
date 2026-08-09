import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ColumnSchema } from '../types/graph'
import { ChartView, RowDetail, RowsTable } from './DataPanel'

const col = (name: string, type: string): ColumnSchema => ({
  name, type, capabilities: [], provenance: 'inferred',
})

describe('Rows grid', () => {
  it('renders integers beyond 2^53 as the exact digits the kernel sent', () => {
    render(<RowsTable
      columns={[col('id', 'int'), col('u', 'int'), col('n', 'int')]}
      rows={[
        { id: '9223372036854775807', u: '18446744073709551615', n: 42 },
        { id: '9223372036854775806', u: '0', n: -7 },
        { id: '-9223372036854775808', u: '18446744073709551614', n: null },
      ]}
      onRowClick={() => {}} />)

    for (const digits of ['9223372036854775807', '9223372036854775806', '-9223372036854775808',
      '18446744073709551615', '18446744073709551614']) {
      expect(screen.getByText(digits)).toBeInTheDocument()
    }
    expect(screen.queryByText('9223372036854776000')).not.toBeInTheDocument()
    expect(screen.getByText('42')).toBeInTheDocument()
  })

  it('right-aligns an integer column whose out-of-range cells arrive as strings', () => {
    render(<RowsTable columns={[col('id', 'int')]} rows={[{ id: '9223372036854775807' }]}
      onRowClick={() => {}} />)

    const cell = screen.getByText('9223372036854775807').closest('td')
    expect(cell).toHaveClass('text-right', 'tabular-nums')
  })
})

describe('temporal chart axis', () => {
  const temporalColumns = [col('x', 'timestamp[us]'), col('y', 'double')]
  const rows = [
    { x: '2024-03-01T00:00:00', y: 3 },
    { x: null, y: 2 },
    { x: '2024-02-29T00:00:00', y: 1 },
  ]

  it('sorts bucketed groups chronologically with nulls as one explicit trailing group', () => {
    const { container } = render(<ChartView type="bar" xLabel="created_at" yLabel="count(*)" grouped
      timeBucket="day" columns={temporalColumns} rows={[...rows]} scope="full-result" />)
    const barTitles = [...container.querySelectorAll('rect > title')].map((t) => t.textContent)
    expect(barTitles).toEqual([
      'created_at · by day (UTC): 2024-02-29T00:00:00; count(*): 1',
      'created_at · by day (UTC): 2024-03-01T00:00:00; count(*): 3',
      'created_at · by day (UTC): No date; count(*): 2',
    ])
    const texts = [...container.querySelectorAll('text')].map((t) => t.textContent)
    expect(texts.indexOf('Feb 29')).toBeGreaterThan(-1)
    expect(texts.indexOf('Feb 29')).toBeLessThan(texts.indexOf('Mar 1'))
    expect(texts.indexOf('Mar 1')).toBeLessThan(texts.indexOf('No date'))
    expect(texts).toContain('created_at · by day (UTC)')
  })

  it('orders schema-typed temporal results chronologically without claiming a bucket', () => {
    const { container } = render(<ChartView type="line" xLabel="created_at" yLabel="count(*)" grouped
      columns={temporalColumns} scope="full-result"
      rows={[{ x: '2025-06-01T00:00:00', y: 2 }, { x: '2024-01-01T00:00:00', y: 1 }]} />)
    const texts = [...container.querySelectorAll('text')].map((t) => t.textContent)
    expect(texts.indexOf('Jan 2024')).toBeLessThan(texts.indexOf('Jun 2025'))
    expect(texts).toContain('created_at')
    expect(texts.some((t) => t?.includes('(UTC)'))).toBe(false)
  })

  it('keeps non-temporal axes on the existing label contract', () => {
    const { container } = render(<ChartView type="bar" xLabel="event" yLabel="count(*)" grouped
      columns={[col('x', 'string'), col('y', 'double')]} scope="full-result"
      rows={[{ x: 'view', y: 2 }, { x: 'click', y: 1 }]} />)
    const texts = [...container.querySelectorAll('text')].map((t) => t.textContent)
    expect(texts.indexOf('view')).toBeLessThan(texts.indexOf('click'))
    expect(texts).toContain('event')
  })
})

const whitespaceColumns: ColumnSchema[] = [{ name: 'val', type: 'string', capabilities: [] }]
const whitespaceValues = ['trail  ', '  lead', '  ', '\t', 'a\nb', 'plain text', '', null]
const whitespaceRows = whitespaceValues.map((val) => ({ val }))

describe('whitespace in string cells', () => {
  it('shows hidden whitespace in the grid and keeps NULL, empty and clean values apart', () => {
    render(<RowsTable columns={whitespaceColumns} rows={whitespaceRows} onRowClick={() => {}} />)
    expect(screen.getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      'trail␣␣', '␣␣lead', '␣␣', '⇥', 'a↵\nb', 'plain text', '', '·',
    ])
  })

  it('names the whitespace it marks', () => {
    render(<RowsTable columns={whitespaceColumns} rows={whitespaceRows} onRowClick={() => {}} />)
    expect(screen.getAllByTitle('2 spaces')).toHaveLength(3)
    expect(screen.getByTitle('1 tab')).toBeTruthy()
    expect(screen.getByTitle('1 newline')).toBeTruthy()
  })

  it('shows hidden whitespace in the row detail', () => {
    const { container } = render(<RowDetail columns={whitespaceColumns} row={{ val: 'trail  ' }} />)
    expect(container.textContent).toContain('trail␣␣')
  })
})
