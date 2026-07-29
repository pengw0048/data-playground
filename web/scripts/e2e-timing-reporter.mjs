import { appendTimingEvent } from './e2e-timing-lib.mjs'
import { relative } from 'node:path'

export default class E2ETimingReporter {
  constructor(options) {
    this.outputFile = options.outputFile ?? process.env.DP_E2E_TIMINGS_FILE
  }

  onBegin(_config, suite) {
    appendTimingEvent(this.outputFile, {
      kind: 'playwright-run-start',
      startedAt: new Date().toISOString(),
      expectedTests: suite.allTests().length,
    })
  }

  onTestEnd(test, result) {
    const project = test.parent.project()?.name ?? 'unknown-project'
    appendTimingEvent(this.outputFile, {
      kind: 'test-attempt',
      finishedAt: new Date().toISOString(),
      project,
      file: relative(process.cwd(), test.location.file),
      testId: test.id,
      title: test.titlePath().join(' › '),
      retry: result.retry,
      status: result.status,
      durationMs: result.duration,
      errors: result.errors.map((error) => error.message),
    })
  }

  onEnd(result) {
    appendTimingEvent(this.outputFile, {
      kind: 'playwright-run-end',
      finishedAt: new Date().toISOString(),
      status: result.status,
    })
  }
}
