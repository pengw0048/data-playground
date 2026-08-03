export interface RunErrorContext {
  nodeTitle?: string
  config?: Record<string, unknown>
}

export interface RunErrorPresentation {
  summary: string
  details?: string
}

const NUMBER_SUMMARIES: Record<string, string> = {
  avg: 'Average', mean: 'Average', sum: 'Sum', median: 'Median',
}

function configuredColumn(fn: string, config?: Record<string, unknown>): string | null {
  if (!config) return null
  const aggregate = String(config.agg ?? '').toLowerCase()
  const direct = aggregate === fn ? String(config.y ?? config.column ?? '').trim() : ''
  if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(direct)) return direct
  const pattern = new RegExp(`\\b${fn}\\s*\\(\\s*(?:distinct\\s+)?(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))`, 'i')
  for (const value of Object.values(config)) {
    if (typeof value !== 'string') continue
    const match = pattern.exec(value)
    if (match) return match[1] ?? match[2] ?? null
  }
  return null
}

function readableType(type: string): string {
  if (/(?:var)?char|string|text|enum/i.test(type)) return 'text'
  if (/bool/i.test(type)) return 'true/false'
  if (/date|time|interval/i.test(type)) return 'date/time'
  if (/blob|binary|byte/i.test(type)) return 'binary'
  return type.toLowerCase()
}

function fallbackSummary(raw: string, context: RunErrorContext): string {
  const attributed = /^at '[^']+':/i.test(raw.trim())
  let value = raw
    .split(/\n\s*(?:Candidate functions:|Candidates:|LINE \d+:)/i)[0]
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line && !/^candidate bindings/i.test(line))
    .join(' ')
  value = value
    .replace(/^at '[^']+':\s*/i, '')
    .replace(/^(?:[A-Za-z]*(?:Exception|Error):\s*)+/i, '')
    .replace(/^(?:Binder|Conversion|Catalog) Error:\s*/i, '')
    .trim()
  if (!value) return 'This step could not run.'
  if (attributed && context.nodeTitle && !value.toLowerCase().includes(context.nodeTitle.toLowerCase())) {
    return `${context.nodeTitle}: ${value}`
  }
  return value
}

/** Keep engine diagnostics available without making them the primary product language. */
export function presentRunError(raw?: string | null, context: RunErrorContext = {}): RunErrorPresentation {
  const details = raw?.trim() || undefined
  if (!details) return { summary: 'This step could not run.' }

  const functionMismatch = /No function matches[^']*'([A-Za-z_][A-Za-z0-9_]*)\(([^)]*)\)'/i.exec(details)
  if (functionMismatch) {
    const fn = functionMismatch[1].toLowerCase()
    const operation = NUMBER_SUMMARIES[fn]
    if (operation) {
      const column = configuredColumn(fn, context.config)
      const type = readableType(functionMismatch[2].split(',')[0]?.trim() || 'this type')
      const subject = column ? `“${column}” is a ${type} column.` : `The selected value is ${type}.`
      return {
        summary: `${subject} ${operation} needs a number column. Choose a numeric column or change the summary.`,
        details,
      }
    }
  }

  const timeout = /(?:SandboxError:\s*)?cell exceeded the\s+([0-9.]+)s time budget/i.exec(details)
  if (timeout) return {
    summary: `This code exceeded the ${timeout[1]}s time limit. Make the operation smaller or use a different compute backend.`,
    details,
  }

  if (/invalid graph:.*Join input|'[^']+' requires exactly one incoming edge on input '[ab]'/i.test(details)) {
    return { summary: 'Finish connecting the Join inputs before running this branch.', details }
  }
  if (/Join node '[^']+' needs at least one left and right column or an advanced condition/i.test(details)) {
    return { summary: 'Choose the left and right columns that should match in this Join.', details }
  }
  if (/^invalid graph:/i.test(details.trim())) {
    return {
      summary: 'This branch is not ready to run. Check its connections and required fields.',
      details,
    }
  }

  return { summary: fallbackSummary(details, context), details }
}
