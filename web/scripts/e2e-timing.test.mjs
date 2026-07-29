import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import {
  aggregateTestAttempts,
  aggregateTests,
  readTimingEvents,
} from './e2e-timing-lib.mjs'
import { renderTimingSummary } from './e2e-timing.mjs'

function temporaryDirectory() {
  return mkdtempSync(join(tmpdir(), 'dp-e2e-timing-'))
}

test('final attempt aggregation reports project and spec totals without retry double counting', () => {
  const events = [
    { kind: 'test-attempt', project: 'smoke', file: 'canvas.spec.ts', testId: 'a', title: 'canvas', retry: 0, status: 'failed', durationMs: 1000 },
    { kind: 'test-attempt', project: 'smoke', file: 'canvas.spec.ts', testId: 'a', title: 'canvas', retry: 1, status: 'passed', durationMs: 400 },
    { kind: 'test-attempt', project: 'smoke', file: 'canvas.spec.ts', testId: 'b', title: 'catalog', retry: 0, status: 'passed', durationMs: 600 },
    { kind: 'test-attempt', project: 'chromium', file: 'jobs.spec.ts', testId: 'c', title: 'jobs', retry: 0, status: 'timedOut', durationMs: 900 },
  ]

  const finalTests = aggregateTestAttempts(events)
  assert.deepEqual(finalTests.map(({ testId, status, durationMs, attempts, discardedAttemptDurationMs }) => ({ testId, status, durationMs, attempts, discardedAttemptDurationMs })), [
    { testId: 'c', status: 'timedOut', durationMs: 900, attempts: 1, discardedAttemptDurationMs: 0 },
    { testId: 'b', status: 'passed', durationMs: 600, attempts: 1, discardedAttemptDurationMs: 0 },
    { testId: 'a', status: 'passed', durationMs: 400, attempts: 2, discardedAttemptDurationMs: 1000 },
  ])

  assert.deepEqual(aggregateTests(finalTests, ['project']), [
    { values: ['smoke'], tests: 2, durationMs: 1000, failed: 0, retried: 1, discardedAttemptDurationMs: 1000 },
    { values: ['chromium'], tests: 1, durationMs: 900, failed: 1, retried: 0, discardedAttemptDurationMs: 0 },
  ])
  assert.deepEqual(aggregateTests(finalTests, ['project', 'file']), [
    { values: ['smoke', 'canvas.spec.ts'], tests: 2, durationMs: 1000, failed: 0, retried: 1, discardedAttemptDurationMs: 1000 },
    { values: ['chromium', 'jobs.spec.ts'], tests: 1, durationMs: 900, failed: 1, retried: 0, discardedAttemptDurationMs: 0 },
  ])
})

test('a truncated JSONL tail preserves valid events and reports only line diagnostics', () => {
  const directory = temporaryDirectory()
  try {
    const artifact = join(directory, 'events.jsonl')
    writeFileSync(
      artifact,
      [
        JSON.stringify({ kind: 'phase', name: 'install-web-deps', durationMs: 10 }),
        JSON.stringify({ kind: 'phase', name: 'build-spa', durationMs: 20 }),
        '{"kind":"phase","detail":"do-not-repeat',
      ].join('\n'),
    )

    const timingData = readTimingEvents(artifact)
    assert.deepEqual(timingData.events.map((event) => event.name), [
      'install-web-deps',
      'build-spa',
    ])
    assert.deepEqual(timingData.parseDiagnostic, {
      partial: true,
      invalidLineCount: 1,
      invalidLineNumbers: [3],
    })

    const summary = renderTimingSummary(timingData)
    assert.match(summary, /Partial timing data: skipped 1 invalid JSONL line\(s\) at line\(s\) 3/)
    assert.doesNotMatch(summary, /do-not-repeat/)
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
})

test('summary separates nested phases and escapes Markdown table cells', () => {
  const summary = renderTimingSummary({
    events: [
      {
        kind: 'phase',
        scope: 'job-step',
        name: 'install|web',
        status: 'passed',
        durationMs: 100,
        detail: 'top\r\nline|detail',
      },
      {
        kind: 'phase',
        scope: 'playwright-webserver',
        parent: 'playwright|run',
        name: 'fixture|build',
        status: 'passed',
        durationMs: 40,
        detail: 'nested\nline|detail',
      },
      {
        kind: 'test-attempt',
        project: 'chromium|smoke',
        file: 'e2e/canvas\r\nname|.spec.ts',
        testId: 'canvas',
        title: 'canvas',
        retry: 0,
        status: 'passed',
        durationMs: 25,
      },
    ],
    parseDiagnostic: {
      partial: false,
      invalidLineCount: 0,
      invalidLineNumbers: [],
    },
  })

  const topLevel = summary.slice(
    summary.indexOf('### Top-level E2E job phases'),
    summary.indexOf('### Nested Playwright webServer phases'),
  )
  const nested = summary.slice(
    summary.indexOf('### Nested Playwright webServer phases'),
    summary.indexOf('### Final testcase duration by Playwright project'),
  )
  assert.match(topLevel, /install\\\|web/)
  assert.match(topLevel, /top<br>line\\\|detail/)
  assert.doesNotMatch(topLevel, /fixture/)
  assert.match(nested, /fixture\\\|build/)
  assert.match(nested, /playwright\\\|run/)
  assert.match(nested, /nested<br>line\\\|detail/)
  assert.match(nested, /Do not add their durations to the parent duration/)
  assert.match(summary, /chromium\\\|smoke/)
  assert.match(summary, /e2e\/canvas<br>name\\\|\.spec\.ts/)
})

test('phase wrapper preserves child exit codes and records spawn failures without hanging', () => {
  const directory = temporaryDirectory()
  try {
    const artifact = join(directory, 'events.jsonl')
    const cli = fileURLToPath(new URL('./e2e-timing.mjs', import.meta.url))
    const environment = { ...process.env, DP_E2E_TIMINGS_FILE: artifact }
    const failedExit = spawnSync(
      process.execPath,
      [cli, 'phase', 'child-exit', '--', process.execPath, '-e', 'process.exit(23)'],
      { env: environment, timeout: 5_000 },
    )
    assert.equal(failedExit.status, 23)
    assert.equal(failedExit.signal, null)

    const spawnFailure = spawnSync(
      process.execPath,
      [cli, 'phase', 'spawn-failure', '--', 'dp-command-that-does-not-exist'],
      { env: environment, timeout: 5_000 },
    )
    assert.equal(spawnFailure.status, 1)
    assert.equal(spawnFailure.signal, null)

    const timingData = readTimingEvents(artifact)
    assert.deepEqual(
      timingData.events.map(({ name, scope, status }) => ({ name, scope, status })),
      [
        { name: 'child-exit', scope: 'job-step', status: 'failed' },
        { name: 'spawn-failure', scope: 'job-step', status: 'failed' },
      ],
    )
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
})
