import { useEffect } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput, MiniSelect, miniSelectClass } from '../../ui/controls'
import { useInputColumns } from '../fields'
import { Icon } from '../../ui/Icon'
import type { ColumnSchema } from '../../types/graph'
import { cn } from '@/lib/utils'

type ChartType = 'bar' | 'line' | 'scatter' | 'area'
type Agg = 'none' | 'count' | 'sum' | 'mean' | 'min' | 'max'
type AxisMode = 'column' | 'expression'

const NESTED_OR_BINARY = /(?:list|array|struct|map|union|blob|binary)/i
const NUMERIC = /(?:^|\W)(?:u?int(?:8|16|32|64|128)?|tinyint|smallint|integer|bigint|hugeint|float(?:16|32|64)?|double|real|decimal|numeric)(?:\W|$)/i
const TEMPORAL = /(?:date|time|timestamp|duration|interval)/i
const CATEGORICAL = /(?:string|varchar|char|text|enum|bool)/i
const ID_LIKE = /(?:^id$|_id$|^uuid$|row_?id|index$)/i
const MEASURE_LIKE = /(?:amount|value|score|total|price|revenue|cost|duration|latency|size|width|height|rate|count)/i

export function chartableColumns(columns: ColumnSchema[]): ColumnSchema[] {
  return columns.filter((column) => !NESTED_OR_BINARY.test(column.type))
}

export function numericChartColumns(columns: ColumnSchema[]): ColumnSchema[] {
  return chartableColumns(columns).filter((column) => NUMERIC.test(column.type))
}

/** A useful zero-config dimension: a category first, then time, then any scalar field. */
export function suggestedChartDimension(columns: ColumnSchema[]): ColumnSchema | undefined {
  const scalar = chartableColumns(columns)
  return scalar.find((column) => CATEGORICAL.test(column.type) && !ID_LIKE.test(column.name))
    ?? scalar.find((column) => TEMPORAL.test(column.type))
    ?? scalar.find((column) => !ID_LIKE.test(column.name))
    ?? scalar[0]
}

/** Prefer a business measure over an identifier when a Y value is required. */
export function suggestedChartMeasure(columns: ColumnSchema[], x?: string): ColumnSchema | undefined {
  const numeric = numericChartColumns(columns).filter((column) => column.name !== x)
  return numeric.find((column) => MEASURE_LIKE.test(column.name) && !ID_LIKE.test(column.name))
    ?? numeric.find((column) => !ID_LIKE.test(column.name))
    ?? numeric[0]
}

function typeLabel(type: string): string {
  if (NUMERIC.test(type)) return 'Number'
  if (TEMPORAL.test(type)) return 'Date/time'
  if (/(?:bool)/i.test(type)) return 'Boolean'
  return 'Text'
}

function AxisEditor({
  axis, value, mode, columns, placeholder, fallback, onChange,
}: {
  axis: 'X' | 'Y'
  value: string
  mode: AxisMode
  columns: ColumnSchema[]
  placeholder: string
  fallback?: string
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
          {!value && <option value="">{columns.length ? 'Choose a column' : 'Connect input'}</option>}
          {value && !known && <option value={value}>{value} · unavailable</option>}
          {columns.map((column) => (
            <option key={column.name} value={column.name}>{column.name} · {typeLabel(column.type)}</option>
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
  const inputColumns = useInputColumns(id)
  const columns = chartableColumns(inputColumns)
  const measures = numericChartColumns(inputColumns)
  const defaultX = suggestedChartDimension(inputColumns)
  const defaultY = suggestedChartMeasure(inputColumns, x)

  // A newly connected Chart should already be useful. Persist the recommendation so the visible
  // controls, saved Canvas, durable run, and reopened result all agree on the same fields.
  useEffect(() => {
    const patch: Record<string, unknown> = {}
    if (xMode === 'column' && !x && defaultX) patch.x = defaultX.name
    if (agg !== 'count' && yMode === 'column' && !y && defaultY) patch.y = defaultY.name
    if (Object.keys(patch).length) updateConfig(id, patch)
  }, [agg, defaultX, defaultY, id, updateConfig, x, xMode, y, yMode])

  const axisName = (value: string, mode: AxisMode) => mode === 'expression' ? 'SQL' : (value || '…')
  const summary = agg === 'count'
    ? `Count rows by ${axisName(x || defaultX?.name || '', xMode)}`
    : agg === 'none'
      ? `${axisName(y || defaultY?.name || '', yMode)} by ${axisName(x || defaultX?.name || '', xMode)}`
      : `${AGG_LABELS[agg]} ${axisName(y || defaultY?.name || '', yMode)} by ${axisName(x || defaultX?.name || '', xMode)}`

  const changeAgg = (next: Agg) => {
    const patch: Record<string, unknown> = { agg: next }
    if (next !== 'count' && yMode === 'column' && !y && defaultY) patch.y = defaultY.name
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
          placeholder="date_trunc('day', created_at)"
          onChange={(value, mode) => updateConfig(id, { x: value, xMode: mode })} />
        {agg !== 'count' && (
          <AxisEditor axis="Y" value={y || defaultY?.name || ''} mode={yMode} columns={measures}
            fallback={defaultY?.name}
            placeholder="amount * exchange_rate"
            onChange={(value, mode) => updateConfig(id, { y: value, yMode: mode })} />
        )}
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
