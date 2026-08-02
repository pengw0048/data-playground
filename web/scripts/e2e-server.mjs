import { spawn } from 'node:child_process'
import { existsSync, readdirSync, rmSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { appendTimingEvent } from './e2e-timing-lib.mjs'

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = resolve(webRoot, '..')
const kernelRoot = join(repoRoot, 'kernel')
const workspace = join(webRoot, '.e2e-workspace')
const timingFile = process.env.DP_E2E_TIMINGS_FILE
  ? resolve(process.env.DP_E2E_TIMINGS_FILE)
  : undefined
const profile = process.env.DP_E2E_FIXTURE_PROFILE ?? 'smoke'
const port = process.env.DP_E2E_PORT ?? '8899'
const databaseUrl = process.env.DP_E2E_DATABASE_URL ?? 'sqlite:///e2e-test.db'
const providerAcceptance = Boolean(process.env.DP_E2E_PROVIDER_ACCEPTANCE)
const kernelPackage = databaseUrl.startsWith('postgres') ? '.[postgres]' : '.'
const wheelExtras = databaseUrl.startsWith('postgres') ? '[lance,postgres]' : '[lance]'

async function timedPhase(name, action, detail) {
  const started = performance.now()
  const startedAt = new Date().toISOString()
  try {
    const result = await action()
    if (timingFile) {
      appendTimingEvent(timingFile, {
        kind: 'phase', scope: 'playwright-webserver', parent: 'playwright-run',
        name, status: 'passed', detail, startedAt,
        finishedAt: new Date().toISOString(), durationMs: Math.round(performance.now() - started),
      })
    }
    return result
  } catch (error) {
    if (timingFile) {
      appendTimingEvent(timingFile, {
        kind: 'phase', scope: 'playwright-webserver', parent: 'playwright-run',
        name, status: 'failed', detail, startedAt,
        finishedAt: new Date().toISOString(), durationMs: Math.round(performance.now() - started),
      })
    }
    throw error
  }
}

function run(command, args, options = {}) {
  return new Promise((resolveRun, rejectRun) => {
    const child = spawn(command, args, { stdio: 'inherit', ...options })
    child.on('error', rejectRun)
    child.on('exit', (code, signal) => {
      if (code === 0) resolveRun()
      else rejectRun(new Error(`${command} ${args[0]} exited with ${code ?? signal}`))
    })
  })
}

async function waitForServer(child) {
  const deadline = Date.now() + 120_000
  const url = `http://127.0.0.1:${port}/api/livez`
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`dataplay exited before ${url} became ready`)
    try {
      const response = await fetch(url)
      if (response.ok) return
    } catch {
      // The process has not bound the port yet.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 250))
  }
  throw new Error(`timed out waiting for ${url}`)
}

async function main() {
  await timedPhase('e2e-workspace-cleanup', async () => rmSync(workspace, { recursive: true, force: true }))
  await timedPhase(
    'candidate-spa-build',
    () => run('npm', ['run', 'build'], { cwd: webRoot }),
    'Rebuilds the SPA immediately before packaging so local Playwright runs cannot serve stale UI code.',
  )
  await timedPhase(
    'webserver-fixture-build',
    () => run('uv', ['run', 'python', join(repoRoot, 'scripts/build_ux_fixtures.py'), '--profile', profile, '--output', join(workspace, 'data')], { cwd: kernelRoot }),
    'Real UX fixture build inside the fresh shared E2E workspace.',
  )
  if (databaseUrl.startsWith('postgres')) {
    await timedPhase(
      'webserver-database-migrate',
      () => run('uv', ['run', '--with', kernelPackage, 'dataplay', 'migrate'], { cwd: kernelRoot, env: { ...process.env, DP_DATABASE_URL: databaseUrl } }),
    )
  }
  const wheelDir = join(workspace, 'kernel-wheel')
  await timedPhase(
    'candidate-wheel-build',
    () => run('uv', ['build', '--wheel', '--clear', '--no-create-gitignore', '--out-dir', wheelDir, '.'], { cwd: kernelRoot }),
    'Builds the disposable candidate wheel containing the current built SPA.',
  )
  const wheel = readdirSync(wheelDir).find((entry) => /^data_playground-.*\.whl$/.test(entry))
  if (!wheel) throw new Error(`candidate wheel missing from ${wheelDir}`)
  const wheelPath = join(wheelDir, wheel)
  if (!existsSync(wheelPath)) throw new Error(`candidate wheel missing: ${wheelPath}`)
  const withDependencies = [
    '--with', `${wheelPath}${wheelExtras}`,
    '--with', join(repoRoot, 'examples/plugins/dp_descriptor_contract'),
    '--with', join(repoRoot, 'examples/plugins/dp_sidecar_fixture'),
  ]
  if (providerAcceptance) withDependencies.push('--with', join(repoRoot, 'examples/plugins/dp_file_catalog_provider'))

  await timedPhase(
    'candidate-wheel-install',
    () => run('uv', ['run', ...withDependencies, 'python', '-c', 'import hub'], {
      cwd: workspace,
      env: { ...process.env, DP_DATABASE_URL: databaseUrl },
    }),
    'Resolves and installs the exact candidate wheel, extras, and fixture plugins used by the real server command.',
  )
  let server
  await timedPhase(
    'server-ready',
    async () => {
      server = spawn('uv', ['run', ...withDependencies, 'dataplay', '--workspace', workspace, '--port', port, '--no-open'], {
        cwd: workspace,
        env: { ...process.env, DP_DATABASE_URL: databaseUrl },
        stdio: 'inherit',
      })
      const spawnFailure = new Promise((_, rejectSpawn) => server.once('error', rejectSpawn))
      await Promise.race([waitForServer(server), spawnFailure])
    },
    'Real kernel readiness after the exact candidate wheel and plugin set has been installed.',
  )
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.kill(signal))
  server.on('exit', (code, signal) => process.exit(code ?? (signal ? 1 : 0)))
}

await main()
