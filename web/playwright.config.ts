import { defineConfig, devices } from '@playwright/test'
import { MIN_VIEWPORT } from './support/min-viewport'

// End-to-end tests drive the REAL app: the kernel (FastAPI + engine) serving the built SPA.
// `npm run build` must run first (the kernel serves web/dist). The webServer block boots the
// kernel on a test port and waits for /api/health before the specs run.
const PORT = process.env.DP_E2E_PORT ?? '8899'

const chromiumLaunch = process.env.DP_E2E_CHROME
  ? { launchOptions: { executablePath: process.env.DP_E2E_CHROME } }
  : {}

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 8_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'on-first-retry',
  },
  // DP_E2E_CHROME lets an environment with a PREBUILT Chromium (a locked-down CI image, a dev
  // container) point Playwright at it instead of downloading one; unset → Playwright's own browser.
  projects: [
    {
      // Default suite: Desktop Chrome at its device viewport (today identical to MIN_VIEWPORT).
      name: 'chromium',
      testIgnore: '**/viewport-support.spec.ts',
      use: {
        ...devices['Desktop Chrome'],
        ...chromiumLaunch,
      },
    },
    {
      // Explicit minimum-viewport proof for docs/BROWSER_SUPPORT.md — imports MIN_VIEWPORT so the
      // documented claim and this project cannot drift without a failing review/test.
      name: 'chromium-min-viewport',
      testMatch: '**/viewport-support.spec.ts',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { ...MIN_VIEWPORT },
        ...chromiumLaunch,
      },
    },
  ],
  webServer: {
    // fresh metadata DB per run (the metadata DB persists canvases; tests need a clean slate)
    command: `cd ../kernel && rm -f e2e-test.db && DP_DATABASE_URL=sqlite:///e2e-test.db uv run dataplay --workspace "$PWD" --port ${PORT} --no-open`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
