import { expect, test } from '@playwright/test'

test('inspects one exact compound episode with linked truthful timeline states @ux-smoke', async ({ page }) => {
  const inspectionBodies: unknown[] = []
  page.on('request', (request) => {
    if (request.url().includes('/inspection-window')) inspectionBodies.push(request.postDataJSON())
  })

  const firstWindow = page.waitForResponse((response) => response.url().includes('/inspection-window') && response.status() === 200)
  await page.goto('/#/compound-inspector')
  const liveWindow = await (await firstWindow).json()
  expect(liveWindow.evidence.streams.find((stream: { streamId: string }) => stream.streamId === 'numeric-sensor').clockMapping).toEqual({
    sourceClockId: 'sensor-device-us', targetClockId: 'reference-ms',
    scaleNumerator: 1001, scaleDenominator: 1_000_000, offsetTick: -125,
  })
  await expect(page.getByTestId('compound-inspector')).toBeVisible()
  await expect(page.getByTestId('compound-revision-identity')).toContainText('revision')
  await expect(page.getByTestId('compound-video')).toBeVisible()
  await expect(page.getByTestId('nearest-observation')).toContainText('not interpolated')
  await expect(page.getByTestId('coverage-numeric-sensor')).toContainText(/gap 4004 ticks/)
  await expect(page.getByTestId('clock-mapping-numeric-sensor')).toContainText('sensor-device-us → reference-ms (reference reference-ms)')
  await expect(page.getByTestId('clock-mapping-numeric-sensor')).toContainText('scale 1001/1000000 · offset -125')

  const player = page.getByTestId('compound-video')
  await expect.poll(() => player.evaluate((video: HTMLVideoElement) => video.duration)).toBeGreaterThan(0)
  await page.getByRole('slider', { name: 'Reference clock cursor' }).fill('8000')
  await expect(page.getByTestId('compound-cursor')).toContainText('8000')
  await player.evaluate((video: HTMLVideoElement) => {
    video.currentTime = video.duration
    video.dispatchEvent(new Event('timeupdate'))
  })
  await expect(page.getByTestId('compound-cursor')).toContainText('8000')

  await player.evaluate(async (video: HTMLVideoElement) => {
    video.muted = true
    await video.play()
  })
  await expect.poll(async () => page.getByTestId('compound-cursor').textContent()).not.toContain('8000')
  await player.evaluate((video: HTMLVideoElement) => video.pause())

  await page.getByTestId('observation-episode-1-sensor-001').getByRole('button').click()
  await expect(page.getByTestId('compound-cursor')).toContainText('876')
  await page.getByTestId('compound-inspector').press('ArrowRight')
  await expect(page.getByTestId('compound-cursor')).toContainText('976')

  await page.getByRole('checkbox', { name: /video/ }).uncheck()
  await expect(page.getByTestId('compound-video-pane')).toHaveCount(0)
  await expect(page.getByText('No declared video asset')).toHaveCount(0)

  await page.getByLabel('Episode').selectOption('episode-2')
  await expect(page.getByTestId('stream-state-absent')).toContainText('Absent for this episode')
  await expect(page.getByTestId('compound-video')).toHaveCount(0)
  expect(JSON.stringify(inspectionBodies)).not.toMatch(/manifest|fixture:\/\//)
})
