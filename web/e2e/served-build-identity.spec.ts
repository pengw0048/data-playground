import { expect, test } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'

// The kernel's packaged SPA is a force-included wheel artifact. Compare the served entrypoint to
// the build immediately before Playwright started so a cached local wheel cannot silently exercise
// an older frontend. Vite content-hashes all referenced JS/CSS assets, making this a bounded identity
// check for the full entrypoint asset graph without adding a production-only build marker.
test('serves the current built SPA entrypoint @first-run', async ({ request }) => {
  const expected = await readFile(resolve('dist/index.html'), 'utf8')
  const response = await request.get('/')

  expect(response.ok()).toBe(true)
  expect(await response.text()).toBe(expected)
})
