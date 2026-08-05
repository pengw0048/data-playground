/** Stable Series / Color by helpers shared by ChartView and tests. */

export const CHART_SERIES_OTHER = 'Other'
export const CHART_SERIES_BLANK = '(blank)'

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

/**
 * Derive color from the label itself, not from the labels visible on the current page. Full-result
 * pagination can expose only a subset of categories; position-based palettes make a category change
 * color between pages even though the retained result is unchanged.
 */
export function chartSeriesColor(label: string): string {
  if (label === CHART_SERIES_OTHER) return CHART_SERIES_OTHER_COLOR
  let hash = 2166136261
  for (let index = 0; index < label.length; index += 1) {
    hash = Math.imul(hash ^ label.charCodeAt(index), 16777619)
  }
  const stable = hash >>> 0
  const hue = stable % 360
  const saturation = 58 + ((stable >>> 9) % 12)
  const lightness = 40 + ((stable >>> 17) % 8)
  return `hsl(${hue} ${saturation}% ${lightness}%)`
}

export function summarizeChartSeries(ordered: readonly string[]): string {
  const named = ordered.filter((label) => label !== CHART_SERIES_OTHER)
  if (ordered.includes(CHART_SERIES_OTHER)) {
    return `${named.length.toLocaleString()} series + Other`
  }
  return named.length === 1 ? '1 series' : `${named.length.toLocaleString()} series`
}
