import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CompoundFixtureDetail, InspectionWindowResponse } from '../types/api'

const mocks = vi.hoisted(() => ({
  compoundReference: vi.fn(), compoundInspectionWindow: vi.fn(), compoundAssetUrl: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks, KernelError: class KernelError extends Error { status = 500 } }))

import { CompoundInspector } from './CompoundInspector'

const detail: CompoundFixtureDetail = {
  datasetId: 'compound-public', revisionId: 'a'.repeat(64), assets: [{ id: 'flower-webm', mediaType: 'video/webm', byteLength: 4, sha256: 'b'.repeat(64), status: 'available' }],
  episodes: [
    { id: 'episode-1', referenceClockId: 'reference-ms', startTick: '0', endTick: '10000', streams: [{ id: 'numeric-sensor', kind: 'numeric', clockId: 'sensor', state: 'present', assetIds: [] }, { id: 'interval-annotation', kind: 'annotation', clockId: 'reference-ms', state: 'present', assetIds: [] }, { id: 'video', kind: 'video', clockId: 'reference-ms', state: 'present', assetIds: ['flower-webm'] }] },
    { id: 'episode-2', referenceClockId: 'reference-ms', startTick: '20000', endTick: '27000', streams: [{ id: 'numeric-sensor', kind: 'numeric', clockId: 'sensor', state: 'present', assetIds: [] }, { id: 'interval-annotation', kind: 'annotation', clockId: 'reference-ms', state: 'present', assetIds: [] }, { id: 'video', kind: 'video', clockId: 'reference-ms', state: 'absent', assetIds: [] }] },
  ],
}

const ALL_STREAMS = ['numeric-sensor', 'interval-annotation', 'video']

function response(episodeId = 'episode-1', streamIds = ALL_STREAMS): InspectionWindowResponse {
  const absent = episodeId === 'episode-2'
  const evidenceStreams: InspectionWindowResponse['evidence']['streams'] = [
    { streamId: 'numeric-sensor', state: 'available', coverageIntervals: absent ? [[20876, 21878]] : [[876, 2878], [6882, 8884]], gaps: absent ? [] : [{ afterObservationId: 'sensor-3', beforeObservationId: 'sensor-4', durationTicks: 4004, thresholdTicks: 3000 }], clockMapping: { sourceClockId: 'sensor-device-us', targetClockId: 'reference-ms', scaleNumerator: 1001, scaleDenominator: 1_000_000, offsetTick: -125 }, complete: true },
    { streamId: 'interval-annotation', state: 'available', coverageIntervals: [], gaps: [], clockMapping: null, complete: true },
    { streamId: 'video', state: absent ? 'absent' : 'available', coverageIntervals: absent ? [] : [[1000, 3000]], gaps: [], clockMapping: null, complete: !absent },
  ]
  const observations: InspectionWindowResponse['observations'] = [
    { streamId: 'numeric-sensor', state: 'present', complete: true, corruptCount: 0, columns: [{ name: 'value', type: 'float64', nullable: false, provenance: 'declared' }], observations: [{ observationId: `${episodeId}-sensor-1`, kind: 'point', startTick: absent ? 20876 : 876, values: { value: 1.25 }, assets: [] }] },
    { streamId: 'interval-annotation', state: 'present', complete: true, corruptCount: 0, columns: [{ name: 'phase', type: 'string', nullable: false, provenance: 'declared' }], observations: [{ observationId: `${episodeId}-phase-1`, kind: 'interval', startTick: absent ? 21000 : 1000, endTick: absent ? 22000 : 2500, values: { phase: 'protocol' }, assets: [] }] },
    { streamId: 'video', state: absent ? 'absent' : 'present', complete: true, corruptCount: 0, columns: [], observations: absent ? [] : [{ observationId: 'video-1', kind: 'interval', startTick: 1000, endTick: 3000, values: { asset_id: 'flower-webm' }, assets: detail.assets }] },
  ]
  return {
    schemaVersion: 1,
    identity: { compoundDatasetId: detail.datasetId, compoundRevision: detail.revisionId, episodeId, referenceClockId: 'reference-ms', startTick: absent ? 20000 : 0, endTick: absent ? 27000 : 10000, streamIds },
    complete: !absent, limits: { maxRowsPerStream: 10_000, maxRawBytesPerStream: 1_000_000 },
    evidence: { complete: !absent, approximation: { pointCoverage: 'first-to-last', pairwise: 'exact' }, pair: streamIds.length > 1 ? { state: 'available', complete: true, toleranceTicks: 1, nearestDelta: { tieBreak: 'distance,startTick,endTick,observationId' } } : null, streams: evidenceStreams.filter((stream) => streamIds.includes(stream.streamId)) },
    observations: observations.filter((stream) => streamIds.includes(stream.streamId)),
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((next) => { resolve = next })
  return { promise, resolve }
}

describe('CompoundInspector', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('keeps a truthful exact timeline linked across rows, intervals, and video', async () => {
    mocks.compoundReference.mockResolvedValue(detail)
    mocks.compoundInspectionWindow.mockImplementation((_dataset: string, _revision: string,
      body: { episodeId: string; streamIds: string[] }) => Promise.resolve(response(body.episodeId, body.streamIds)))
    mocks.compoundAssetUrl.mockReturnValue('/api/compound-datasets/opaque/asset')
    render(<CompoundInspector />)

    expect(await screen.findByTestId('compound-revision-identity')).toHaveTextContent(detail.revisionId)
    expect(await screen.findByTestId('compound-video')).toHaveAttribute('src', '/api/compound-datasets/opaque/asset')
    expect(screen.getByTestId('nearest-observation')).toHaveTextContent('Δ')
    expect(screen.getByTestId('nearest-observation')).toHaveTextContent('not interpolated')
    expect(screen.getByTestId('coverage-numeric-sensor')).toHaveTextContent('gap 4004 ticks')
    expect(screen.getByTestId('clock-mapping-numeric-sensor')).toHaveTextContent('sensor-device-us → reference-ms (reference reference-ms)')
    expect(screen.getByTestId('clock-mapping-numeric-sensor')).toHaveTextContent('scale 1001/1000000 · offset -125')
    fireEvent.click(within(screen.getByTestId('observation-episode-1-sensor-1')).getByRole('button'))
    expect(screen.getByTestId('compound-cursor')).toHaveTextContent('876')
    fireEvent.keyDown(screen.getByTestId('compound-inspector'), { key: 'ArrowRight' })
    expect(screen.getByTestId('compound-cursor')).toHaveTextContent('976')
    fireEvent.error(screen.getByTestId('compound-video'))
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be decoded or was removed')
    expect(screen.getAllByRole('button', { name: 'Reopen exact reference' })).toHaveLength(2)
    expect(JSON.stringify(mocks.compoundInspectionWindow.mock.calls)).not.toMatch(/manifest|fixture:\/\//)
  })

  it('fences a late old-episode response and never invents an episode-2 player', async () => {
    const first = deferred<InspectionWindowResponse>()
    mocks.compoundReference.mockResolvedValue(detail)
    mocks.compoundInspectionWindow.mockImplementation((_dataset: string, _revision: string, body: { episodeId: string }) => body.episodeId === 'episode-1' ? first.promise : Promise.resolve(response('episode-2')))
    mocks.compoundAssetUrl.mockReturnValue('/opaque-video')
    render(<CompoundInspector />)

    await waitFor(() => expect(mocks.compoundInspectionWindow).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByLabelText('Episode'), { target: { value: 'episode-2' } })
    expect(await screen.findByTestId('stream-state-absent')).toHaveTextContent('Absent for this episode')
    first.resolve(response('episode-1'))
    await waitFor(() => expect(screen.queryByTestId('compound-video')).not.toBeInTheDocument())
    expect(screen.getByTestId('compound-cursor')).toHaveTextContent('20000')
  })

  it('keeps a cursor outside video coverage until the user plays or seeks the video', async () => {
    mocks.compoundReference.mockResolvedValue(detail)
    mocks.compoundInspectionWindow.mockResolvedValue(response())
    mocks.compoundAssetUrl.mockReturnValue('/opaque-video')
    render(<CompoundInspector />)

    const player = await screen.findByTestId('compound-video') as HTMLVideoElement
    Object.defineProperty(player, 'duration', { configurable: true, value: 5 })
    Object.defineProperty(player, 'paused', { configurable: true, value: false })
    player.pause = vi.fn()
    fireEvent.change(screen.getByRole('slider', { name: 'Reference clock cursor' }), { target: { value: '8000' } })
    expect(screen.getByTestId('compound-cursor')).toHaveTextContent('8000')
    player.currentTime = 5
    fireEvent.timeUpdate(player)
    expect(screen.getByTestId('compound-cursor')).toHaveTextContent('8000')
    expect(player.pause).toHaveBeenCalled()

    fireEvent.play(player)
    player.currentTime = 2.5
    fireEvent.timeUpdate(player)
    expect(screen.getByTestId('compound-cursor')).toHaveTextContent('2000')
  })

  it('aborts the superseded request and hides video when that stream is not selected', async () => {
    const first = deferred<InspectionWindowResponse>()
    let firstSignal: AbortSignal | undefined
    mocks.compoundReference.mockResolvedValue(detail)
    mocks.compoundInspectionWindow.mockImplementation((_dataset: string, _revision: string,
      body: { episodeId: string; streamIds: string[] }, options: { signal: AbortSignal }) => {
      if (!firstSignal) { firstSignal = options.signal; return first.promise }
      return Promise.resolve(response(body.episodeId, body.streamIds))
    })
    mocks.compoundAssetUrl.mockReturnValue('/opaque-video')
    render(<CompoundInspector />)

    await waitFor(() => expect(mocks.compoundInspectionWindow).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('checkbox', { name: /video/ }))
    await waitFor(() => expect(mocks.compoundInspectionWindow).toHaveBeenCalledTimes(2))
    expect(firstSignal?.aborted).toBe(true)
    expect(await screen.findByTestId('compound-stream-numeric-sensor')).toBeVisible()
    expect(screen.queryByTestId('compound-video-pane')).not.toBeInTheDocument()
    expect(screen.queryByText('No declared video asset')).not.toBeInTheDocument()
    first.resolve(response())
  })

  it('offers a reachable retry when exact-reference discovery initially fails', async () => {
    mocks.compoundReference.mockRejectedValueOnce(new Error('Service Unavailable')).mockResolvedValueOnce(detail)
    mocks.compoundInspectionWindow.mockResolvedValue(response())
    mocks.compoundAssetUrl.mockReturnValue('/opaque-video')
    render(<CompoundInspector />)

    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't open the exact compound reference: Service Unavailable")
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByTestId('compound-revision-identity')).toHaveTextContent(detail.revisionId)
  })
})
