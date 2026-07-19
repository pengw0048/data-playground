import { readFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { resolve } from 'node:path'
import { expect, test } from '@playwright/test'

test('Chromium loads the vendored CC0 WebM over local HTTP', async ({ page }) => {
  const asset = await readFile(resolve(process.cwd(), '..', 'fixtures', 'compound', 'flower.webm'))
  const server = createServer((request, response) => {
    if (request.url === '/flower.webm') {
      response.writeHead(200, { 'content-length': asset.byteLength, 'content-type': 'video/webm' })
      response.end(asset)
      return
    }
    response.writeHead(200, { 'content-type': 'text/html' })
    response.end('<video id="fixture-video" muted></video>')
  })
  await new Promise<void>((resolveListen) => server.listen(0, '127.0.0.1', resolveListen))
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('local compound fixture server did not bind')
  const origin = `http://127.0.0.1:${address.port}`

  try {
    await page.route('**/*', (route) => route.request().url().startsWith(origin)
      ? route.continue() : route.abort())
    await page.goto(origin)
    const facts = await page.locator('#fixture-video').evaluate(async (video) => {
      const loaded = new Promise<void>((resolveLoaded, rejectLoaded) => {
        const timer = setTimeout(() => rejectLoaded(new Error('video metadata timeout')), 10_000)
        video.addEventListener('loadedmetadata', () => { clearTimeout(timer); resolveLoaded() }, { once: true })
        video.addEventListener('error', () => {
          clearTimeout(timer)
          rejectLoaded(new Error(`video error ${video.error?.code}`))
        }, { once: true })
      })
      video.src = '/flower.webm'
      video.load()
      await loaded
      await video.play()
      await new Promise((resolveFrame) => setTimeout(resolveFrame, 250))
      return {
        currentTime: video.currentTime,
        duration: video.duration,
        height: video.videoHeight,
        readyState: video.readyState,
        width: video.videoWidth,
      }
    })
    expect(facts).toMatchObject({ height: 540, readyState: 4, width: 960 })
    expect(facts.duration).toBeCloseTo(5.059, 3)
    expect(facts.currentTime).toBeGreaterThan(0)
  } finally {
    await new Promise<void>((resolveClose, rejectClose) => server.close((error) => error ? rejectClose(error) : resolveClose()))
  }
})
