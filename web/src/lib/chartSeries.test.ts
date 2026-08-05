import { describe, expect, it } from 'vitest'
import {
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

  it('maps stable colors from labels across page subsets and reserves muted Other', () => {
    expect(chartSeriesColor('alpha')).toBe(chartSeriesColor('alpha'))
    expect(chartSeriesColor('alpha')).not.toBe(chartSeriesColor('beta'))
    expect(chartSeriesColor(CHART_SERIES_OTHER)).toBe(CHART_SERIES_OTHER_COLOR)
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
