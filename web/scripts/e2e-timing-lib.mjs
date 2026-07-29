import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname } from 'node:path'

export function appendTimingEvent(outputFile, event) {
  mkdirSync(dirname(outputFile), { recursive: true })
  appendFileSync(outputFile, `${JSON.stringify({ version: 1, ...event })}\n`)
}

export function readTimingEvents(outputFile) {
  if (!existsSync(outputFile)) return []
  return readFileSync(outputFile, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((line) => JSON.parse(line))
}

const FINAL_NON_FAILURE_STATUSES = new Set(['passed', 'skipped'])

export function aggregateTestAttempts(events) {
  const attempts = events.filter((event) => event.kind === 'test-attempt')
  const byTest = new Map()

  for (const attempt of attempts) {
    const key = `${attempt.project}\u0000${attempt.file}\u0000${attempt.testId}`
    const attemptsForTest = byTest.get(key) ?? []
    attemptsForTest.push(attempt)
    byTest.set(key, attemptsForTest)
  }

  const finalTests = [...byTest.values()].map((attemptsForTest) => {
    // Playwright numbers attempts from zero. Keeping only the highest retry makes the reported
    // duration describe the final outcome while the raw JSONL keeps every retry for flake analysis.
    const finalAttempt = attemptsForTest.reduce((latest, attempt) =>
      attempt.retry >= latest.retry ? attempt : latest,
    )
    const discardedAttempts = attemptsForTest.filter((attempt) => attempt !== finalAttempt)
    return {
      ...finalAttempt,
      attempts: attemptsForTest.length,
      discardedAttemptDurationMs: discardedAttempts.reduce(
        (total, attempt) => total + attempt.durationMs,
        0,
      ),
      failed: !FINAL_NON_FAILURE_STATUSES.has(finalAttempt.status),
    }
  })

  return finalTests.sort(
    (left, right) => right.durationMs - left.durationMs || left.title.localeCompare(right.title),
  )
}

export function aggregateTests(finalTests, fields) {
  const groups = new Map()
  for (const test of finalTests) {
    const values = fields.map((field) => test[field])
    const key = values.join('\u0000')
    const group = groups.get(key) ?? {
      values,
      tests: 0,
      durationMs: 0,
      failed: 0,
      retried: 0,
      discardedAttemptDurationMs: 0,
    }
    group.tests += 1
    group.durationMs += test.durationMs
    group.failed += Number(test.failed)
    group.retried += Number(test.attempts > 1)
    group.discardedAttemptDurationMs += test.discardedAttemptDurationMs
    groups.set(key, group)
  }
  return [...groups.values()].sort((left, right) => right.durationMs - left.durationMs)
}

export function formatDuration(durationMs) {
  return `${(durationMs / 1000).toFixed(2)}s`
}
