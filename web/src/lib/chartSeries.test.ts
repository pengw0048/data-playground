import { describe, expect, it } from 'vitest'
import {
  CHART_SERIES_COLORS,
  CHART_SERIES_OTHER,
  CHART_SERIES_OTHER_COLOR,
  chartSeriesColor,
  chartSeriesLabel,
  orderChartSeriesLabels,
  summarizeChartSeries,
} from './chartSeries'

describe('chartSeries helpers', () => {
  it('orders named series alphabetically and keeps Other last', () => {
    expect(orderChartSeriesLabels(['zeta', CHART_SERIES_OTHER, 'alpha', 'alpha'])).toEqual([
      'alpha', 'zeta', CHART_SERIES_OTHER,
    ])
  })

  it('maps stable colors from ordered labels and reserves muted Other', () => {
    const ordered = orderChartSeriesLabels(['b', 'a', CHART_SERIES_OTHER])
    expect(chartSeriesColor('a', ordered)).toBe(CHART_SERIES_COLORS[0])
    expect(chartSeriesColor('b', ordered)).toBe(CHART_SERIES_COLORS[1])
    expect(chartSeriesColor(CHART_SERIES_OTHER, ordered)).toBe(CHART_SERIES_OTHER_COLOR)
    expect(chartSeriesColor('a', orderChartSeriesLabels(['b', 'a']))).toBe(
      chartSeriesColor('a', orderChartSeriesLabels(['a', 'b'])),
    )
  })

  it('labels blank series values explicitly', () => {
    expect(chartSeriesLabel(null)).toBe('(blank)')
    expect(chartSeriesLabel('  ')).toBe('(blank)')
    expect(chartSeriesLabel('click')).toBe('click')
  })

  it('summarizes bounded series counts truthfully', () => {
    expect(summarizeChartSeries(['a', 'b'])).toBe('2 series')
    expect(summarizeChartSeries(['a', CHART_SERIES_OTHER])).toBe('1 series + Other')
  })
})
