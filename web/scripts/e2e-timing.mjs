import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  aggregateTestAttempts,
  aggregateTests,
  appendTimingEvent,
  formatDuration,
  readTimingEvents,
} from './e2e-timing-lib.mjs'

function outputFileFromEnvironment() {
  return resolve(process.env.DP_E2E_TIMINGS_FILE ?? 'e2e-timings/events.jsonl')
}

function markdownCell(value) {
  return String(value ?? '')
    .replace(/\\/g, '\\\\')
    .replace(/\r\n|\r|\n/g, '<br>')
    .replace(/\|/g, '\\|')
}

function markdownTable(headers, rows) {
  if (rows.length === 0) return '_No data recorded._\n'
  return [
    `| ${headers.map(markdownCell).join(' | ')} |`,
    `| ${headers.map(() => '---').join(' | ')} |`,
    ...rows.map((row) => `| ${row.map(markdownCell).join(' | ')} |`),
    '',
  ].join('\n')
}

const NESTED_WEBSERVER_PHASES = new Set([
  'e2e-workspace-cleanup',
  'webserver-fixture-build',
  'webserver-database-migrate',
  'candidate-wheel-build',
  'candidate-wheel-install',
  'server-ready',
])

function phaseScope(phase) {
  if (phase.scope) return phase.scope
  // Keep previously uploaded v1 artifacts readable after the scope field is introduced.
  return NESTED_WEBSERVER_PHASES.has(phase.name) ? 'playwright-webserver' : 'job-step'
}

export function renderTimingSummary({ events, parseDiagnostic }) {
  const phases = events.filter((event) => event.kind === 'phase')
  const jobPhases = phases.filter((phase) => phaseScope(phase) === 'job-step')
  const webServerPhases = phases.filter((phase) => phaseScope(phase) === 'playwright-webserver')
  const finalTests = aggregateTestAttempts(events)
  const projects = aggregateTests(finalTests, ['project'])
  const files = aggregateTests(finalTests, ['project', 'file'])
  const retried = finalTests.filter((test) => test.attempts > 1)
  const discardedDuration = finalTests.reduce(
    (total, test) => total + test.discardedAttemptDurationMs,
    0,
  )

  const partialWarning = parseDiagnostic?.partial
    ? [
        '> [!WARNING]',
        `> Partial timing data: skipped ${parseDiagnostic.invalidLineCount} invalid JSONL line(s) at line(s) ${parseDiagnostic.invalidLineNumbers.join(', ')}. Valid events are shown below.`,
        '',
      ]
    : []

  return [
    '## Playwright timing',
    '',
    ...partialWarning,
    'Test totals use only each testcase\'s final Playwright attempt. Earlier retries remain in `events.jsonl` and are excluded from duration totals so a retry is never double-counted.',
    '',
    '### Top-level E2E job phases',
    '',
    'These are sequential command boundaries from the existing Actions job steps.',
    '',
    markdownTable(
      ['Phase', 'Status', 'Duration', 'Notes'],
      jobPhases.map((phase) => [
        phase.name,
        phase.status,
        formatDuration(phase.durationMs),
        phase.detail ?? '',
      ]),
    ),
    '### Nested Playwright webServer phases',
    '',
    'These rows are contained within the top-level `playwright-run` phase. Do not add their durations to the parent duration.',
    '',
    markdownTable(
      ['Phase', 'Parent', 'Status', 'Duration', 'Notes'],
      webServerPhases.map((phase) => [
        phase.name,
        phase.parent ?? 'playwright-run',
        phase.status,
        formatDuration(phase.durationMs),
        phase.detail ?? '',
      ]),
    ),
    '### Final testcase duration by Playwright project',
    '',
    markdownTable(
      ['Project', 'Tests', 'Final duration', 'Failed final', 'Retried tests', 'Discarded retry duration'],
      projects.map((group) => [
        group.values[0],
        group.tests,
        formatDuration(group.durationMs),
        group.failed,
        group.retried,
        formatDuration(group.discardedAttemptDurationMs),
      ]),
    ),
    '### Final testcase duration by project and spec file',
    '',
    markdownTable(
      ['Project', 'Spec file', 'Tests', 'Final duration', 'Failed final', 'Retried tests'],
      files.map((group) => [
        group.values[0],
        group.values[1],
        group.tests,
        formatDuration(group.durationMs),
        group.failed,
        group.retried,
      ]),
    ),
    `Final tests: ${finalTests.length}; retried tests: ${retried.length}; discarded retry-attempt duration: ${formatDuration(discardedDuration)}.`,
    '',
    'The dependency install, SPA build, and browser-install rows mirror their existing Actions steps. The webServer rows measure the real fixture build, candidate-wheel build, candidate-wheel install, and server-ready path; they do not change test selection, worker count, timeouts, retries, or project ordering.',
    '',
  ].join('\n')
}

async function runPhase(name, command) {
  const outputFile = outputFileFromEnvironment()
  const startedAt = new Date().toISOString()
  const started = performance.now()
  const child = spawn(command[0], command.slice(1), { stdio: 'inherit' })
  const outcome = await new Promise((resolveOutcome) => {
    child.once('error', () => resolveOutcome({ exitCode: 1, detail: 'Command failed to start.' }))
    child.once('exit', (exitCode, signal) =>
      resolveOutcome({
        exitCode: exitCode ?? 1,
        detail: signal ? `Command exited after signal ${signal}.` : undefined,
      }),
    )
  })
  const durationMs = Math.round(performance.now() - started)
  appendTimingEvent(outputFile, {
    kind: 'phase',
    scope: 'job-step',
    name,
    status: outcome.exitCode === 0 ? 'passed' : 'failed',
    detail: outcome.detail,
    startedAt,
    finishedAt: new Date().toISOString(),
    durationMs,
  })
  process.exitCode = outcome.exitCode
}

function render() {
  const outputFile = outputFileFromEnvironment()
  const summaryFile = resolve(process.argv[3] ?? 'e2e-timings/summary.md')
  mkdirSync(dirname(summaryFile), { recursive: true })
  writeFileSync(summaryFile, renderTimingSummary(readTimingEvents(outputFile)))
}

async function main() {
  const [command, ...args] = process.argv.slice(2)
  if (command === 'init') {
    const outputFile = outputFileFromEnvironment()
    mkdirSync(dirname(outputFile), { recursive: true })
    writeFileSync(outputFile, '')
  } else if (command === 'phase') {
    const separator = args.indexOf('--')
    if (separator < 1) throw new Error('usage: phase <name> -- <command> [args...]')
    await runPhase(args[0], args.slice(separator + 1))
  } else if (command === 'render') {
    render()
  } else {
    throw new Error('usage: init | phase <name> -- <command> | render [summary-file]')
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main()
}
