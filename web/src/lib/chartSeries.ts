/** Stable Series / Color by helpers shared by ChartView and tests. */

export const CHART_SERIES_OTHER = 'Other'
export const CHART_SERIES_BLANK = '(blank)'

/** Fixed categorical palette (12 named series). Other uses a muted token separately. */
export const CHART_SERIES_COLORS = [
  'hsl(211 72% 48%)',
  'hsl(162 55% 38%)',
  'hsl(28 78% 48%)',
  'hsl(280 45% 48%)',
  'hsl(348 65% 48%)',
  'hsl(190 60% 40%)',
  'hsl(45 80% 42%)',
  'hsl(320 50% 48%)',
  'hsl(95 45% 38%)',
  'hsl(230 55% 55%)',
  'hsl(15 70% 46%)',
  'hsl(175 40% 36%)',
] as const

export const CHART_SERIES_OTHER_COLOR = 'hsl(var(--muted-foreground))'

export function chartSeriesLabel(value: unknown): string {
  if (value == null) return CHART_SERIES_BLANK
  const text = String(value).trim()
  return text === '' ? CHART_SERIES_BLANK : text
}

/** Named series sort alphabetically; Other always last so color slots stay stable. */
export function orderChartSeriesLabels(labels: Iterable<string>): string[] {
  const unique = [...new Set(labels)]
  const named = unique.filter((label) => label !== CHART_SERIES_OTHER).sort((a, b) => (
    a < b ? -1 : a > b ? 1 : 0
  ))
  return unique.includes(CHART_SERIES_OTHER) ? [...named, CHART_SERIES_OTHER] : named
}

export function chartSeriesColor(label: string, ordered: readonly string[]): string {
  if (label === CHART_SERIES_OTHER) return CHART_SERIES_OTHER_COLOR
  const index = ordered.indexOf(label)
  if (index < 0) return CHART_SERIES_COLORS[0]
  return CHART_SERIES_COLORS[index % CHART_SERIES_COLORS.length]
}

export function summarizeChartSeries(ordered: readonly string[]): string {
  const named = ordered.filter((label) => label !== CHART_SERIES_OTHER)
  if (ordered.includes(CHART_SERIES_OTHER)) {
    return `${named.length.toLocaleString()} series + Other`
  }
  return named.length === 1 ? '1 series' : `${named.length.toLocaleString()} series`
}
