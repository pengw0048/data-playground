import assert from 'node:assert/strict'
import test from 'node:test'
import { aggregateTestAttempts, aggregateTests } from './e2e-timing-lib.mjs'

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
