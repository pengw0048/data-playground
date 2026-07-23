import { defineConfig, devices } from '@playwright/test'
import { MIN_VIEWPORT } from './support/min-viewport'

// End-to-end tests drive the REAL app: the kernel (FastAPI + engine) serving the built SPA.
// `npm run build` must run first (the kernel serves web/dist). The webServer block boots the
// kernel on a test port and waits for /api/livez before the specs run.
const PORT = process.env.DP_E2E_PORT ?? '8899'
const fixtureProfile = process.env.DP_E2E_FIXTURE_PROFILE ?? 'smoke'
const REFERENCE_VIEWPORT = { width: 1440, height: 900 }
const providerAcceptanceDependency = process.env.DP_E2E_PROVIDER_ACCEPTANCE
  ? ' --with "$REPO_ROOT/examples/plugins/dp_file_catalog_provider"'
  : ''
// Metadata DB for the shared kernel. Defaults to the throwaway SQLite file; the Postgres acceptance
// variant points it at a live server, which needs the psycopg extra and an explicit up-front migrate
// (SQLite auto-migrates on first run; a production Postgres DB does not).
const databaseUrl = process.env.DP_E2E_DATABASE_URL ?? 'sqlite:///e2e-test.db'
const kernelPackage = databaseUrl.startsWith('postgres') ? "'.[postgres]'" : '.'
// The smoke fixture intentionally converts events.parquet to Lance, so the packaged runner must
// install the same Lance adapter it exercises rather than treating its source as unavailable.
const kernelWheelExtras = databaseUrl.startsWith('postgres')
  ? '[lance,postgres]'
  : '[lance]'
const migrateStep = databaseUrl.startsWith('postgres')
  ? `DP_DATABASE_URL=${databaseUrl} uv run --with ${kernelPackage} dataplay migrate && `
  : ''

const chromiumLaunch = process.env.DP_E2E_CHROME
  ? { launchOptions: { executablePath: process.env.DP_E2E_CHROME } }
  : {}

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 8_000 },
  retries: process.env.CI ? 1 : 0,
  // Every project drives one shared kernel + catalog, so parallel workers contend on that single
  // kernel and bleed route/catalog state across specs. Serialize in CI (and for the mutating full
  // profile); local smoke runs may still parallelize for iteration speed.
  workers: process.env.CI || fixtureProfile === 'full' ? 1 : undefined,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'on-first-retry',
  },
  // DP_E2E_CHROME lets an environment with a PREBUILT Chromium (a locked-down CI image, a dev
  // container) point Playwright at it instead of downloading one; unset → Playwright's own browser.
  projects: [
    {
      // This one journey needs the initial empty metadata DB. It runs before every other project
      // that creates Canvas records, so the assertion is a real first-run check rather than a mock.
      name: 'chromium-first-run',
      testMatch: ['**/canvas.spec.ts', '**/served-build-identity.spec.ts'],
      grep: /@first-run/,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1280, height: 720 },
        ...chromiumLaunch,
      },
    },
    {
      // Required researcher workflows run first. Keeping them in a dependency project gives CI a
      // focused failure and prevents another project's dependency graph from rerunning fixed-id fixtures.
      name: 'chromium-ux-smoke',
      dependencies: ['chromium-first-run'],
      testIgnore: '**/viewport-support.spec.ts',
      grep: /@ux-smoke/,
      use: {
        ...devices['Desktop Chrome'],
        ...chromiumLaunch,
      },
    },
    {
      // Remaining default suite: Desktop Chrome at its device viewport. The smoke dependency must
      // pass first, and grepInvert makes the two projects a complete, non-overlapping partition.
      name: 'chromium',
      dependencies: ['chromium-ux-smoke'],
      testIgnore: '**/viewport-support.spec.ts',
      grepInvert: /@ux-smoke|@first-run/,
      use: {
        ...devices['Desktop Chrome'],
        ...chromiumLaunch,
      },
    },
    {
      // Explicit minimum-viewport proof for docs/BROWSER_SUPPORT.md — imports MIN_VIEWPORT so the
      // documented claim and this project cannot drift without a failing review/test. Depends on
      // chromium so the shared e2e kernel DB is not mutated by both projects at once.
      name: 'chromium-min-viewport',
      dependencies: ['chromium'],
      testMatch: '**/viewport-support.spec.ts',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { ...MIN_VIEWPORT },
        ...chromiumLaunch,
      },
    },
    {
      // Exercise the same researcher journeys at the normal desktop reference viewport so making the
      // 1280px shell responsive cannot regress the established 1440px layout.
      name: 'chromium-reference-viewport',
      dependencies: ['chromium-min-viewport'],
      testMatch: '**/viewport-support.spec.ts',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { ...REFERENCE_VIEWPORT },
        ...chromiumLaunch,
      },
    },
  ],
  webServer: {
    // fresh metadata DB per run (the metadata DB persists canvases; tests need a clean slate).
    // `web/dist` is force-included in the kernel wheel, but its gitignored contents do not
    // invalidate uv's cached local build. Build one disposable wheel inside this fresh E2E
    // workspace and run that exact artifact: dependencies stay cached while the served SPA cannot be stale.
    command: `cd ../kernel && REPO_ROOT="$(cd .. && pwd)" && WORKSPACE="$REPO_ROOT/web/.e2e-workspace" && rm -rf "$WORKSPACE" && uv run python "$REPO_ROOT/scripts/build_ux_fixtures.py" --profile ${fixtureProfile} --output "$WORKSPACE/data" && ${migrateStep}WHEEL_DIR="$WORKSPACE/kernel-wheel" && uv build --wheel --clear --no-create-gitignore --out-dir "$WHEEL_DIR" . && WHEEL=$(find "$WHEEL_DIR" -maxdepth 1 -name 'data_playground-*.whl' -print -quit) && test -n "$WHEEL" && cd "$WORKSPACE" && DP_DATABASE_URL=${databaseUrl} uv run --with "\${WHEEL}${kernelWheelExtras}" --with "$REPO_ROOT/examples/plugins/dp_descriptor_contract" --with "$REPO_ROOT/examples/plugins/dp_sidecar_fixture"${providerAcceptanceDependency} dataplay --workspace "$WORKSPACE" --port ${PORT} --no-open`,
    url: `http://127.0.0.1:${PORT}/api/livez`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
