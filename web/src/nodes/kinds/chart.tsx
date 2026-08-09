import { useEffect } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput, MiniSelect, miniSelectClass } from '../../ui/controls'
import { useInputColumns } from '../fields'
import { Icon } from '../../ui/Icon'
import type { ColumnSchema } from '../../types/graph'
import { isTemporalColumnType, normalizeTimeBucket, type TimeBucket } from '../../lib/chartTemporal'
import { cn } from '@/lib/utils'

type ChartType = 'bar' | 'line' | 'scatter' | 'area'
type Agg = 'none' | 'count' | 'sum' | 'mean' | 'min' | 'max'
type AxisMode = 'column' | 'expression'

const NESTED_OR_BINARY = /(?:list|array|struct|map|union|blob|bytes?|binary|varbinary|bytea|bit)/i
const NUMERIC = /(?:^|\W)(?:u?int(?:8|16|32|64|128)?|tinyint|smallint|integer|bigint|hugeint|float(?:16|32|64)?|double|real|decimal|numeric)(?:\W|$)/i
const TEMPORAL = /(?:date|time|timestamp|duration|interval)/i
const CATEGORICAL = /(?:string|varchar|char|text|enum|bool)/i
const ID_LIKE = /(?:^id$|_id$|^uuid$|row_?id|index$)/i
const MEASURE_LIKE = /(?:amount|value|score|total|price|revenue|cost|duration|latency|size|width|height|rate|count)/i

function chartColumnType(column: ColumnSchema): string {
  return `${column.type} ${column.physicalType ?? ''}`
}

export function chartableColumns(columns: ColumnSchema[]): ColumnSchema[] {
  return columns.filter((column) => !NESTED_OR_BINARY.test(chartColumnType(column)))
}

export function numericChartColumns(columns: ColumnSchema[]): ColumnSchema[] {
  return chartableColumns(columns).filter((column) => NUMERIC.test(chartColumnType(column)))
}

/** True for a typed date/timestamp column a UTC time bucket can group. */
export function timeBucketableChartColumn(column: ColumnSchema): boolean {
  return isTemporalColumnType(chartColumnType(column))
}

/** Scalar categorical columns suitable for Series / Color by (no SQL expressions in this slice). */
export function seriesChartColumns(columns: ColumnSchema[], exclude?: string): ColumnSchema[] {
  return chartableColumns(columns).filter((column) => (
    column.name !== exclude
    && CATEGORICAL.test(chartColumnType(column))
  ))
}

/** A useful zero-config dimension: a category first, then time, then any scalar field. */
export function suggestedChartDimension(columns: ColumnSchema[]): ColumnSchema | undefined {
  const scalar = chartableColumns(columns)
  return scalar.find((column) => CATEGORICAL.test(chartColumnType(column)) && !ID_LIKE.test(column.name))
    ?? scalar.find((column) => TEMPORAL.test(chartColumnType(column)) && !ID_LIKE.test(column.name))
    ?? scalar.find((column) => !ID_LIKE.test(column.name))
}

/** Prefer a business measure over an identifier when a Y value is required. */
export function suggestedChartMeasure(columns: ColumnSchema[], x?: string): ColumnSchema | undefined {
  const numeric = numericChartColumns(columns).filter((column) => column.name !== x)
  return numeric.find((column) => MEASURE_LIKE.test(column.name) && !ID_LIKE.test(column.name))
    ?? numeric.find((column) => !ID_LIKE.test(column.name))
}

function typeLabel(column: ColumnSchema): string {
  const type = chartColumnType(column)
  if (NUMERIC.test(type)) return 'Number'
  if (TEMPORAL.test(type)) return 'Date/time'
  if (/(?:bool)/i.test(type)) return 'Boolean'
  return 'Text'
}

function AxisEditor({
  axis, value, mode, columns, placeholder, fallback, allowEmpty = false, onChange,
}: {
  axis: 'X' | 'Y'
  value: string
  mode: AxisMode
  columns: ColumnSchema[]
  placeholder: string
  fallback?: string
  allowEmpty?: boolean
  onChange: (value: string, mode: AxisMode) => void
}) {
  const known = columns.some((column) => column.name === value)
  const columnLabel = axis === 'X' ? 'Group by (X)' : 'Value (Y)'
  if (mode === 'expression') {
    return (
      <div className="flex flex-col gap-[3px]">
        <span className="text-[9.5px] font-semibold uppercase tracking-[0.4px] text-muted-foreground">{columnLabel}</span>
        <div className="flex gap-1">
          <div className="min-w-0 flex-1">
            <MiniInput mono value={value} ariaLabel={`${axis} SQL expression`} placeholder={placeholder}
              onChange={(next) => onChange(next, 'expression')} />
          </div>
          <button type="button" className="nodrag inline-flex h-7 shrink-0 items-center gap-1 rounded-md border border-input px-2 text-[10px] font-semibold text-muted-foreground hover:bg-accent hover:text-foreground"
            aria-label={`Choose ${axis} from input columns`} title="Choose from input columns"
            onClick={(event) => {
              event.stopPropagation()
              onChange(known ? value : (fallback && columns.some((column) => column.name === fallback)
                ? fallback : (columns[0]?.name ?? '')), 'column')
            }}>
            Columns
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-[3px]">
      <span className="text-[9.5px] font-semibold uppercase tracking-[0.4px] text-muted-foreground">{columnLabel}</span>
      <div className="flex gap-1">
        <select aria-label={`${axis} column`} value={value} disabled={columns.length === 0}
          onClick={(event) => event.stopPropagation()}
          onChange={(event) => onChange(event.target.value, 'column')}
          className={cn('nodrag min-w-0 flex-1', miniSelectClass)}>
          {allowEmpty && <option value="">No grouping · one total</option>}
          {!allowEmpty && !value && <option value="">{columns.length ? 'Choose a column' : 'Connect input'}</option>}
          {value && !known && <option value={value}>{value} · unavailable</option>}
          {columns.map((column) => (
            <option key={column.name} value={column.name}>{column.name} · {typeLabel(column)}</option>
          ))}
        </select>
        <button type="button" className="nodrag inline-flex h-7 shrink-0 items-center gap-1 rounded-md border border-input px-2 text-[10px] font-semibold text-muted-foreground hover:bg-accent hover:text-foreground"
          aria-label={`Use SQL expression for ${axis}`} title="Use a SQL expression"
          onClick={(event) => { event.stopPropagation(); onChange(value, 'expression') }}>
          <Icon name="fx" size={10} /> SQL
        </button>
      </div>
    </div>
  )
}

const AGG_LABELS: Record<Agg, string> = {
  count: 'Count rows', sum: 'Sum', mean: 'Average', min: 'Minimum', max: 'Maximum', none: 'Raw values',
}
const TIME_BUCKET_LABELS: Array<[TimeBucket, string]> = [
  ['hour', 'Hour'], ['day', 'Day'], ['week', 'Week (ISO, Mon)'], ['month', 'Month'],
  ['quarter', 'Quarter'], ['year', 'Year'],
]
const TYPE_LABELS: Record<ChartType, string> = {
  bar: 'Bars', line: 'Line', scatter: 'Points', area: 'Area',
}

// The `chart` node turns a column pair into a visualization (rendered in the data panel). Simple
// charts are configured from the input schema; SQL expressions are an explicit per-axis escape hatch.
function Chart({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((state) => state.updateConfig)
  const chartType = (data.config.chartType as ChartType) ?? 'bar'
  const agg = (data.config.agg as Agg) ?? 'count'
  const xMode = (data.config.xMode as AxisMode) ?? 'column'
  const yMode = (data.config.yMode as AxisMode) ?? 'column'
  const x = String(data.config.x ?? '')
  const y = String(data.config.y ?? '')
  const series = String(data.config.series ?? '')
  const timeBucket = normalizeTimeBucket(data.config.timeBucket)
  const inputColumns = useInputColumns(id)
  const columns = chartableColumns(inputColumns)
  const measures = numericChartColumns(inputColumns)
  const seriesColumns = seriesChartColumns(inputColumns, x || undefined)
  const defaultX = suggestedChartDimension(inputColumns)
  const defaultY = suggestedChartMeasure(inputColumns, x)
  const inputHasX = !!x && inputColumns.some((column) => column.name === x)
  const inputHasY = !!y && inputColumns.some((column) => column.name === y)
  const xIsChartable = !!x && columns.some((column) => column.name === x)
  const yIsChartable = !!y && measures.some((column) => column.name === y)
  const seriesAvailable = agg !== 'none'
  const seriesKnown = seriesColumns.some((column) => column.name === series)
  const xColumn = columns.find((column) => column.name === (x || defaultX?.name || ''))
  const bucketAvailable = xMode === 'column' && xColumn != null && timeBucketableChartColumn(xColumn)
  // Only a positively known non-temporal X (or an expression, which we never reinterpret) clears a
  // saved bucket; a disconnected input keeps the config intact.
  const bucketBlocked = xMode === 'expression'
    || (xColumn != null && !timeBucketableChartColumn(xColumn))

  // A newly connected Chart should already be useful. Persist the recommendation so the visible
  // controls, saved Canvas, durable run, and reopened result all agree on the same fields.
  useEffect(() => {
    const patch: Record<string, unknown> = {}
    if (xMode === 'column' && inputHasX && !xIsChartable) patch.x = defaultX?.name ?? ''
    else if (xMode === 'column' && !x && defaultX) patch.x = defaultX.name
    if (agg !== 'count' && yMode === 'column' && inputHasY && !yIsChartable) patch.y = defaultY?.name ?? ''
    else if (agg !== 'count' && yMode === 'column' && !y && defaultY) patch.y = defaultY.name
    // Raw-value mode cannot color by Series in this slice; clear a leftover selection so the
    // saved Canvas never claims a semantic Series that the engine would refuse.
    if (!seriesAvailable && series) patch.series = ''
    else if (series && series === (patch.x ?? x)) patch.series = ''
    if (timeBucket && bucketBlocked) patch.timeBucket = 'none'
    if (Object.keys(patch).length) updateConfig(id, patch)
  }, [agg, bucketBlocked, defaultX, defaultY, id, inputHasX, inputHasY, series, seriesAvailable,
    timeBucket, updateConfig, x, xIsChartable, xMode, y, yIsChartable, yMode])

  const axisName = (value: string, mode: AxisMode) => mode === 'expression' ? 'SQL' : (value || '…')
  const seriesSuffix = seriesAvailable && series ? ` · color by ${series}` : ''
  const xShown = axisName(x || defaultX?.name || '', xMode)
  const xSummary = timeBucket && !bucketBlocked ? `${timeBucket}(${xShown})` : xShown
  const summary = (agg === 'count'
    ? (x || defaultX?.name
        ? `Count rows by ${xSummary}`
        : 'Count all rows')
    : agg === 'none'
      ? `${axisName(y || defaultY?.name || '', yMode)} by ${xSummary}`
      : `${AGG_LABELS[agg]} ${axisName(y || defaultY?.name || '', yMode)} by ${xSummary}`)
    + seriesSuffix

  const changeAgg = (next: Agg) => {
    const patch: Record<string, unknown> = { agg: next }
    if (next !== 'count' && yMode === 'column' && !y && defaultY) patch.y = defaultY.name
    if (next === 'none') patch.series = ''
    updateConfig(id, patch)
  }

  return (
    <NodeCard id={id} data={data} metaOverride={`${TYPE_LABELS[chartType]} · ${summary}`}>
      <div className="flex gap-2">
        <Field label="Chart" style={{ flex: 1 }}>
          <MiniSelect<ChartType> value={chartType} onChange={(value) => updateConfig(id, { chartType: value })}
            options={[{ value: 'bar', label: 'Bars' }, { value: 'line', label: 'Line' }, { value: 'scatter', label: 'Points' }, { value: 'area', label: 'Area' }]} />
        </Field>
        <Field label="Summary" style={{ flex: 1 }}>
          <MiniSelect<Agg> value={agg} onChange={changeAgg}
            options={[{ value: 'count', label: 'Count rows' }, { value: 'sum', label: 'Sum' }, { value: 'mean', label: 'Average' }, { value: 'min', label: 'Minimum' }, { value: 'max', label: 'Maximum' }, { value: 'none', label: 'Raw values' }]} />
        </Field>
      </div>
      <div className="mt-2 flex flex-col gap-2">
        <AxisEditor axis="X" value={x || defaultX?.name || ''} mode={xMode} columns={columns}
          fallback={defaultX?.name}
          allowEmpty={agg === 'count'}
          placeholder="date_trunc('day', created_at)"
          onChange={(value, mode) => {
            const patch: Record<string, unknown> = { x: value, xMode: mode }
            if (series && series === value) patch.series = ''
            updateConfig(id, patch)
          }} />
        {bucketAvailable && (
          <div className="flex flex-col gap-[3px]">
            <span className="text-[9.5px] font-semibold uppercase tracking-[0.4px] text-muted-foreground">
              Time bucket · UTC
            </span>
            <select aria-label="X time bucket" value={timeBucket ?? 'none'}
              onClick={(event) => event.stopPropagation()}
              onChange={(event) => updateConfig(id, { timeBucket: event.target.value })}
              className={cn('nodrag min-w-0', miniSelectClass)}>
              <option value="none">No bucket · exact values</option>
              {TIME_BUCKET_LABELS.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </div>
        )}
        {agg !== 'count' && (
          <AxisEditor axis="Y" value={y || defaultY?.name || ''} mode={yMode} columns={measures}
            fallback={defaultY?.name}
            placeholder="amount * exchange_rate"
            onChange={(value, mode) => updateConfig(id, { y: value, yMode: mode })} />
        )}
        <div className="flex flex-col gap-[3px]">
          <span className="text-[9.5px] font-semibold uppercase tracking-[0.4px] text-muted-foreground">
            Series / Color by
          </span>
          {seriesAvailable ? (
            <select aria-label="Series / Color by column" value={series}
              disabled={seriesColumns.length === 0 && !series}
              onClick={(event) => event.stopPropagation()}
              onChange={(event) => updateConfig(id, { series: event.target.value })}
              className={cn('nodrag min-w-0', miniSelectClass)}>
              <option value="">One series · no color split</option>
              {series && !seriesKnown && <option value={series}>{series} · unavailable</option>}
              {seriesColumns.map((column) => (
                <option key={column.name} value={column.name}>{column.name} · {typeLabel(column)}</option>
              ))}
            </select>
          ) : (
            <div role="status" className="rounded-md border border-dashed border-border px-2 py-1.5 text-[10.5px] text-muted-foreground">
              Series needs a Summary aggregate. Switch away from Raw values to color by a category.
            </div>
          )}
        </div>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'chart',
    title: 'chart',
    category: 'inspect',
    tag: 'chart',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'Create a chart from selected columns',
    defaultData: () => ({
      title: 'chart', status: 'draft',
      config: { chartType: 'bar', agg: 'count', xMode: 'column', yMode: 'column' },
      meta: 'Bars · choose input',
    }),
  },
  Chart,
)
