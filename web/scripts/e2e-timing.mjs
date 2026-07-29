import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
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

function markdownTable(headers, rows) {
  if (rows.length === 0) return '_No data recorded._\n'
  return [
    `| ${headers.join(' | ')} |`,
    `| ${headers.map(() => '---').join(' | ')} |`,
    ...rows.map((row) => `| ${row.join(' | ')} |`),
    '',
  ].join('\n')
}

export function renderTimingSummary(events) {
  const phases = events.filter((event) => event.kind === 'phase')
  const finalTests = aggregateTestAttempts(events)
  const projects = aggregateTests(finalTests, ['project'])
  const files = aggregateTests(finalTests, ['project', 'file'])
  const retried = finalTests.filter((test) => test.attempts > 1)
  const discardedDuration = finalTests.reduce(
    (total, test) => total + test.discardedAttemptDurationMs,
    0,
  )

  return [
    '## Playwright timing',
    '',
    'Test totals use only each testcase\'s final Playwright attempt. Earlier retries remain in `events.jsonl` and are excluded from duration totals so a retry is never double-counted.',
    '',
    '### Global phases',
    '',
    markdownTable(
      ['Phase', 'Status', 'Duration', 'Notes'],
      phases.map((phase) => [
        phase.name,
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
  const exitCode = await new Promise((resolveExit) => child.on('exit', resolveExit))
  const durationMs = Math.round(performance.now() - started)
  appendTimingEvent(outputFile, {
    kind: 'phase',
    name,
    status: exitCode === 0 ? 'passed' : 'failed',
    startedAt,
    finishedAt: new Date().toISOString(),
    durationMs,
  })
  process.exitCode = exitCode ?? 1
}

function render() {
  const outputFile = outputFileFromEnvironment()
  const summaryFile = resolve(process.argv[3] ?? 'e2e-timings/summary.md')
  writeFileSync(summaryFile, renderTimingSummary(readTimingEvents(outputFile)))
}

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
