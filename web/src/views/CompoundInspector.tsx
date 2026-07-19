import { useCallback, useEffect, useRef, useState } from 'react'
import { api, KernelError } from '../api/client'
import type {
  CompoundFixtureDetail, CompoundFixtureEpisode, InspectionEvidenceStream,
  InspectionWindowObservation, InspectionWindowResponse, InspectionWindowStream,
} from '../types/api'

const GAP_THRESHOLD = '3000'
const TOLERANCE = '1'

type Window = { start: number; end: number }

const message = (error: unknown) => error instanceof Error ? error.message : String(error)
const stateLabel = (state: string) => state.replaceAll('_', ' ')
const bounded = (value: number, start: number, end: number) => Math.min(end - 1, Math.max(start, value))
const displayValue = (value: unknown) => value == null ? 'null' : typeof value === 'object' ? JSON.stringify(value) : String(value)

function defaultWindow(episode: CompoundFixtureEpisode): Window {
  return { start: Number(episode.startTick), end: Number(episode.endTick) }
}

function windowPresets(episode: CompoundFixtureEpisode): Array<{ label: string; value: Window }> {
  const full = defaultWindow(episode)
  const middle = full.start + Math.floor((full.end - full.start) / 2)
  return [
    { label: 'Whole episode', value: full },
    { label: 'First half', value: { start: full.start, end: middle } },
    { label: 'Second half', value: { start: middle, end: full.end } },
  ]
}

function nextSelected(current: string[], streamId: string, checked: boolean): string[] {
  return checked ? [...current, streamId] : current.filter((item) => item !== streamId)
}

function isRequestedWindow(response: InspectionWindowResponse, detail: CompoundFixtureDetail,
  episode: CompoundFixtureEpisode, window: Window, streamIds: string[]): boolean {
  const identity = response.identity
  return identity.compoundDatasetId === detail.datasetId && identity.compoundRevision === detail.revisionId
    && identity.episodeId === episode.id && identity.startTick === window.start && identity.endTick === window.end
    && identity.referenceClockId === episode.referenceClockId && identity.streamIds.join('\u0000') === streamIds.join('\u0000')
}

/**
 * The exact compound fixture is an intentionally small but complete vertical slice of the eventual
 * generic compound viewer: all browser state is reducible to opaque API identity + reference ticks.
 */
export function CompoundInspector() {
  const [detail, setDetail] = useState<CompoundFixtureDetail | null>(null)
  const [episodeId, setEpisodeId] = useState<string | null>(null)
  const [window, setWindow] = useState<Window | null>(null)
  const [selectedStreams, setSelectedStreams] = useState<string[]>([])
  const [cursor, setCursor] = useState(0)
  const [document, setDocument] = useState<InspectionWindowResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [videoError, setVideoError] = useState<string | null>(null)
  const generation = useRef(0)
  const video = useRef<HTMLVideoElement>(null)
  const videoCursorEvent = useRef(false)
  const programmaticVideoTime = useRef<number | null>(null)
  const suppressVideoEvents = useRef(false)

  const episode = detail?.episodes.find((item) => item.id === episodeId) ?? null
  const resetSelection = useCallback((nextDetail: CompoundFixtureDetail, preferredEpisode?: string) => {
    const nextEpisode = nextDetail.episodes.find((item) => item.id === preferredEpisode) ?? nextDetail.episodes[0] ?? null
    generation.current += 1
    setDocument(null); setError(null); setVideoError(null)
    setDetail(nextDetail)
    setEpisodeId(nextEpisode?.id ?? null)
    setWindow(nextEpisode ? defaultWindow(nextEpisode) : null)
    setSelectedStreams(nextEpisode?.streams.map((stream) => stream.id) ?? [])
    setCursor(nextEpisode ? Number(nextEpisode.startTick) : 0)
  }, [])

  const loadReference = useCallback(async (preferredEpisode?: string) => {
    const request = ++generation.current
    setDocument(null); setError(null); setVideoError(null); setLoading(true)
    try {
      const next = await api.compoundReference()
      if (request !== generation.current) return
      resetSelection(next, preferredEpisode)
    } catch (caught) {
      if (request === generation.current) setError(`Couldn't open the exact compound reference: ${message(caught)}`)
    } finally { if (request === generation.current) setLoading(false) }
  }, [resetSelection])

  useEffect(() => { void loadReference(); return () => { generation.current += 1 } }, [loadReference])

  const requestWindow = useCallback(async (currentDetail: CompoundFixtureDetail, currentEpisode: CompoundFixtureEpisode,
    currentWindow: Window, streamIds: string[], signal: AbortSignal) => {
    if (!streamIds.length) {
      setDocument(null); setError('Choose at least one stream to inspect.'); setLoading(false)
      return
    }
    const request = ++generation.current
    setDocument(null); setError(null); setVideoError(null); setLoading(true)
    const pair = streamIds.length > 1 ? { leftStreamId: streamIds[0], rightStreamId: streamIds[streamIds.length - 1] } : null
    try {
      const next = await api.compoundInspectionWindow(currentDetail.datasetId, currentDetail.revisionId, {
        episodeId: currentEpisode.id, startTick: String(currentWindow.start), endTick: String(currentWindow.end),
        streamIds, pair, gapThresholdTicks: GAP_THRESHOLD, toleranceTicks: TOLERANCE,
      }, { signal })
      if (request !== generation.current) return
      if (!isRequestedWindow(next, currentDetail, currentEpisode, currentWindow, streamIds)) {
        setError('The inspection response does not match the selected exact revision, episode, window, and streams.')
        return
      }
      setDocument(next)
      setCursor((current) => bounded(current, currentWindow.start, currentWindow.end))
    } catch (caught) {
      if (request !== generation.current || signal.aborted
          || (caught instanceof DOMException && caught.name === 'AbortError')) return
      const stale = caught instanceof KernelError && caught.status === 409
      setError(stale
        ? 'This exact revision is stale. Reopen the reference; no latest revision was substituted.'
        : `Couldn't load this bounded inspection window: ${message(caught)}`)
    } finally { if (request === generation.current) setLoading(false) }
  }, [])

  useEffect(() => {
    if (!detail || !episode || !window) return
    const controller = new AbortController()
    void requestWindow(detail, episode, window, selectedStreams, controller.signal)
    return () => { controller.abort(); generation.current += 1 }
  }, [detail, episode, window, selectedStreams, requestWindow])

  const changeEpisode = (nextId: string) => {
    const next = detail?.episodes.find((item) => item.id === nextId)
    if (!next) return
    generation.current += 1
    setDocument(null); setError(null); setVideoError(null)
    setEpisodeId(next.id); setWindow(defaultWindow(next)); setSelectedStreams(next.streams.map((stream) => stream.id)); setCursor(Number(next.startTick))
  }
  const changeWindow = (next: Window) => {
    generation.current += 1
    setDocument(null); setError(null); setVideoError(null); setWindow(next); setCursor(next.start)
  }
  const changeStreams = (streamId: string, checked: boolean) => {
    generation.current += 1
    setDocument(null); setError(null); setVideoError(null)
    setSelectedStreams((current) => nextSelected(current, streamId, checked))
  }

  const activeGeneration = generation.current
  const seek = useCallback((tick: number) => {
    if (!window) return
    videoCursorEvent.current = false
    setCursor(bounded(Math.round(tick), window.start, window.end))
  }, [window])
  const seekFromVideo = useCallback((tick: number) => {
    if (!window) return
    videoCursorEvent.current = true
    setCursor(bounded(Math.round(tick), window.start, window.end))
  }, [window])
  const keyboardSeek = (event: React.KeyboardEvent<HTMLElement>) => {
    if (!window || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    if (event.key === 'Home') seek(window.start)
    else if (event.key === 'End') seek(window.end - 1)
    else seek(cursor + (event.key === 'ArrowLeft' ? -100 : 100))
  }

  const videoStream = document?.observations.find((stream) => stream.streamId === 'video') ?? null
  const videoObservation = videoStream?.observations.find((item) => item.assets.some((asset) => asset.mediaType.startsWith('video/'))) ?? null
  const videoAsset = videoObservation?.assets.find((asset) => asset.mediaType.startsWith('video/')) ?? null
  const videoUrl = detail && episode && videoAsset && videoAsset.status === 'available'
    ? api.compoundAssetUrl(detail.datasetId, detail.revisionId, episode.id, 'video', videoAsset.id) : null
  const videoIdentity = `${activeGeneration}:${detail?.datasetId ?? ''}:${detail?.revisionId ?? ''}:${episodeId ?? ''}:${window?.start ?? ''}:${window?.end ?? ''}:${videoUrl ?? ''}`

  useEffect(() => {
    if (!video.current || !videoObservation || !window) return
    if (videoCursorEvent.current) {
      videoCursorEvent.current = false
      return
    }
    const end = videoObservation.endTick ?? window.end
    if (cursor < videoObservation.startTick || cursor >= end) {
      suppressVideoEvents.current = true
      programmaticVideoTime.current = null
      if (!video.current.paused) video.current.pause()
      return
    }
    const duration = video.current.duration
    if (!Number.isFinite(duration) || duration <= 0) return
    const start = videoObservation.startTick
    const target = ((cursor - start) / Math.max(1, end - start)) * duration
    suppressVideoEvents.current = false
    if (Math.abs(video.current.currentTime - target) > 0.04) {
      programmaticVideoTime.current = target
      video.current.currentTime = target
    }
  }, [cursor, videoIdentity, videoObservation, window])

  if (!detail || !episode || !window) return <main className="grid h-full place-items-center p-8 text-sm text-muted-foreground" data-testid="compound-inspector">
    {error ? <div role="alert" className="grid max-w-md gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-5 text-center text-destructive"><span>{error}</span><button type="button" className="font-semibold underline" onClick={() => void loadReference()}>Retry</button></div> : 'Opening exact compound reference…'}
  </main>
  const revision = detail.revisionId

  return <main data-testid="compound-inspector" className="h-full min-w-0 overflow-auto bg-background p-6" onKeyDown={keyboardSeek} tabIndex={0}>
    <header className="mx-auto flex max-w-[1440px] flex-wrap items-start gap-3 border-b border-border pb-4">
      <div className="min-w-[280px] flex-1">
        <h1 className="text-xl font-bold text-foreground">Inspect episode</h1>
        <p className="mt-1 text-xs text-muted-foreground">Exact compound revision · reference-clock timeline · bounded raw observations</p>
        <div data-testid="compound-revision-identity" className="mt-2 break-all font-mono text-[10px] text-muted-foreground">{detail.datasetId} · revision {revision}</div>
      </div>
      <button type="button" onClick={() => void loadReference(episode.id)} disabled={loading} className="rounded-md border border-border bg-card px-3 py-1.5 text-xs font-semibold hover:bg-accent disabled:opacity-50">Reopen exact reference</button>
    </header>

    {error && <div role="alert" className="mx-auto mt-3 flex max-w-[1440px] items-center justify-between gap-3 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive"><span>{error}</span><button type="button" className="font-semibold underline" onClick={() => void loadReference(episode.id)}>Retry</button></div>}

    <section aria-label="Inspection controls" className="mx-auto mt-4 grid max-w-[1440px] gap-3 rounded-lg border border-border bg-card p-3 lg:grid-cols-[minmax(240px,0.9fr)_minmax(320px,1.1fr)_minmax(360px,1.4fr)]">
      <label className="grid gap-1 text-xs font-semibold text-foreground">Episode
        <select aria-label="Episode" value={episode.id} onChange={(event) => changeEpisode(event.target.value)} className="rounded border border-border bg-background px-2 py-1.5 text-xs font-normal">
          {detail.episodes.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.startTick}–{item.endTick} {item.referenceClockId}</option>)}
        </select>
      </label>
      <fieldset className="min-w-0"><legend className="text-xs font-semibold text-foreground">Bounded window</legend><div className="mt-1 flex flex-wrap gap-1.5">{windowPresets(episode).map((preset) => <button key={preset.label} type="button" onClick={() => changeWindow(preset.value)} aria-pressed={window.start === preset.value.start && window.end === preset.value.end} className="rounded border border-border px-2 py-1 text-[11px] hover:bg-accent aria-pressed:bg-accent">{preset.label}</button>)}</div><div className="mt-2 font-mono text-[10.5px] text-muted-foreground">[{window.start}, {window.end}) {episode.referenceClockId}</div></fieldset>
      <fieldset className="min-w-0"><legend className="text-xs font-semibold text-foreground">Streams</legend><div className="mt-1 flex flex-wrap gap-x-3 gap-y-1.5">{episode.streams.map((stream) => <label key={stream.id} className="flex items-center gap-1.5 text-[11px] text-foreground"><input type="checkbox" checked={selectedStreams.includes(stream.id)} onChange={(event) => changeStreams(stream.id, event.target.checked)} />{stream.id} <span className="text-muted-foreground">({stream.state})</span></label>)}</div></fieldset>
    </section>

    <section className="mx-auto mt-4 max-w-[1440px] rounded-lg border border-border bg-card p-3" aria-label="Reference clock cursor">
      <div className="flex flex-wrap items-center justify-between gap-2"><div><strong className="text-xs text-foreground">Reference cursor</strong><span className="ml-2 font-mono text-xs text-foreground" data-testid="compound-cursor">{cursor} {episode.referenceClockId}</span></div><span className="text-[10.5px] text-muted-foreground">Arrow keys: ±100 ticks · Home/End: window edges</span></div>
      <input aria-label="Reference clock cursor" className="mt-2 w-full accent-primary" type="range" min={window.start} max={window.end - 1} value={cursor} onChange={(event) => seek(Number(event.target.value))} />
      {loading && <div role="status" className="mt-1 text-[11px] text-muted-foreground">Loading this exact bounded window…</div>}
      {document && <EvidenceSummary document={document} />}
    </section>

    {document && <div className={`mx-auto mt-4 grid max-w-[1440px] gap-4 ${selectedStreams.includes('video') ? 'xl:grid-cols-[minmax(420px,0.95fr)_minmax(520px,1.2fr)]' : ''}`}>
      {selectedStreams.includes('video') && <VideoPane key={videoIdentity} videoRef={video} generation={activeGeneration} currentGeneration={generation}
        stream={videoStream} observation={videoObservation} url={videoUrl} cursor={cursor} window={window}
        programmaticTime={programmaticVideoTime} suppressEvents={suppressVideoEvents} onSeek={seekFromVideo}
        error={videoError} onError={setVideoError} onRetry={() => void loadReference(episode.id)} />}
      <div className="grid min-w-0 gap-4">{document.observations.filter((stream) => stream.streamId !== 'video').map((stream) => <StreamPane key={stream.streamId} stream={stream} evidence={document.evidence.streams.find((item) => item.streamId === stream.streamId)} referenceClockId={document.identity.referenceClockId} cursor={cursor} tolerance={document.evidence.pair?.toleranceTicks ?? Number(TOLERANCE)} onSeek={seek} />)}</div>
    </div>}
  </main>
}

function EvidenceSummary({ document }: { document: InspectionWindowResponse }) {
  return <div className="mt-3 grid gap-2"><div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px] text-muted-foreground"><span>Response: {document.complete ? 'complete' : 'partial or bounded'}</span><span>row cap {document.limits.maxRowsPerStream.toLocaleString()}</span><span>raw cap {(document.limits.maxRawBytesPerStream / 1_000_000).toFixed(1)} MB/stream</span></div>{document.evidence.streams.map((stream) => <CoverageBand key={stream.streamId} stream={stream} start={document.identity.startTick} end={document.identity.endTick} />)}</div>
}

function CoverageBand({ stream, start, end }: { stream: InspectionEvidenceStream; start: number; end: number }) {
  const width = Math.max(1, end - start)
  const placement = (tick: number) => `${Math.max(0, Math.min(100, ((tick - start) / width) * 100))}%`
  return <div data-testid={`coverage-${stream.streamId}`} className="grid grid-cols-[112px_minmax(0,1fr)] items-center gap-2 text-[10px]"><span className="truncate text-muted-foreground">{stream.streamId} · {stateLabel(stream.state)}</span><div className="relative h-3 rounded bg-muted/70" title={stream.reason ?? undefined}>{stream.coverageIntervals.map(([left, right], index) => <span key={index} className="absolute top-0 h-3 rounded bg-emerald-500/55" style={{ left: placement(left), width: `${Math.max(0, Math.min(100, ((right - left) / width) * 100))}%` }} />)}</div><span className="col-start-2 text-[9.5px] text-muted-foreground">{stream.gaps.length ? stream.gaps.map((gap) => `gap ${gap.durationTicks} ticks ≥ ${gap.thresholdTicks}`).join(' · ') : stream.reason ?? 'no declared gaps'}</span></div>
}

function StateNotice({ state, reason }: { state: InspectionWindowStream['state']; reason?: string | null }) {
  if (state === 'present') return null
  const absent = state === 'absent'
  return <div data-testid={`stream-state-${state}`} className={`rounded-md border px-2 py-1.5 text-[11px] ${absent ? 'border-border bg-muted/30 text-muted-foreground' : 'border-amber-400/40 bg-amber-50 text-amber-900 dark:bg-amber-950/30 dark:text-amber-100'}`}>{absent ? 'Absent for this episode; no placeholder data or player is shown.' : `${stateLabel(state)}: ${reason ?? 'the exact source did not provide a usable bounded response.'}`}</div>
}

function VideoPane({ videoRef, generation, currentGeneration, stream, observation, url, cursor, window, programmaticTime, suppressEvents, onSeek, error, onError, onRetry }: {
  videoRef: React.RefObject<HTMLVideoElement>; generation: number; currentGeneration: React.MutableRefObject<number>
  stream: InspectionWindowStream | null; observation: InspectionWindowObservation | null; url: string | null; cursor: number; window: Window
  programmaticTime: React.MutableRefObject<number | null>; suppressEvents: React.MutableRefObject<boolean>
  onSeek: (tick: number) => void; error: string | null; onError: (value: string | null) => void; onRetry: () => void
}) {
  const state = stream?.state ?? 'unknown'
  const assetUnavailable = observation?.assets.some((asset) => asset.status !== 'available') ?? false
  return <section className="min-w-0 rounded-lg border border-border bg-card p-3" data-testid="compound-video-pane"><div className="flex items-baseline justify-between gap-2"><h2 className="text-sm font-semibold text-foreground">Video</h2><span className="font-mono text-[10px] text-muted-foreground">reference cursor {cursor}</span></div><div className="mt-2"><StateNotice state={state} reason={stream?.reason} />{state === 'present' && assetUnavailable && <StateNotice state="partial" reason="The declared exact asset is unavailable; playback was not substituted." />}{state === 'present' && url && !assetUnavailable && <video ref={videoRef} controls preload="metadata" src={url} data-testid="compound-video" className="mt-2 aspect-video w-full rounded bg-black" onLoadedMetadata={(event) => { if (generation !== currentGeneration.current || !observation) return; const end = observation.endTick ?? window.end; if (cursor < observation.startTick || cursor >= end) { suppressEvents.current = true; return } const duration = event.currentTarget.duration; const target = ((cursor - observation.startTick) / Math.max(1, end - observation.startTick)) * duration; suppressEvents.current = false; programmaticTime.current = target; event.currentTarget.currentTime = target }} onPlay={() => { suppressEvents.current = false; programmaticTime.current = null }} onPointerDown={() => { suppressEvents.current = false; programmaticTime.current = null }} onTimeUpdate={(event) => { if (generation !== currentGeneration.current || !observation || suppressEvents.current) return; const expected = programmaticTime.current; if (expected != null) { if (Math.abs(event.currentTarget.currentTime - expected) <= 0.08) programmaticTime.current = null; return } const duration = event.currentTarget.duration; const end = observation.endTick ?? window.end; if (Number.isFinite(duration) && duration > 0) onSeek(observation.startTick + (event.currentTarget.currentTime / duration) * (end - observation.startTick)) }} onError={() => { if (generation === currentGeneration.current) onError('The exact video could not be decoded or was removed. No alternate asset was substituted.') }} />}{state === 'present' && !url && !assetUnavailable && <StateNotice state="unknown" reason="No declared video asset was returned for this bounded window." />}{(error || (state === 'present' && assetUnavailable)) && <div role="alert" className="mt-2 flex items-center justify-between gap-2 text-[11px] text-destructive"><span>{error}</span><button type="button" onClick={onRetry} className="font-semibold underline">Reopen exact reference</button></div>}</div></section>
}

function StreamPane({ stream, evidence, referenceClockId, cursor, tolerance, onSeek }: { stream: InspectionWindowStream; evidence?: InspectionEvidenceStream; referenceClockId: string; cursor: number; tolerance: number; onSeek: (tick: number) => void }) {
  const nearest = nearestObservation(stream.observations, cursor)
  return <section className="min-w-0 rounded-lg border border-border bg-card p-3" data-testid={`compound-stream-${stream.streamId}`}><div className="flex flex-wrap items-baseline justify-between gap-2"><h2 className="text-sm font-semibold text-foreground">{stream.streamId}</h2><span className="text-[10.5px] text-muted-foreground">{stream.observations.length} bounded rows · {stream.complete ? 'complete' : 'incomplete'}</span></div><div className="mt-2"><StateNotice state={stream.state} reason={stream.reason} /></div>{stream.state !== 'absent' && <><ClockMapping streamId={stream.streamId} mapping={evidence?.clockMapping ?? null} referenceClockId={referenceClockId} /><Nearest observation={nearest} cursor={cursor} tolerance={tolerance} /><IntervalMarks observations={stream.observations.filter((item) => item.kind === 'interval')} cursor={cursor} onSeek={onSeek} /><Rows observations={stream.observations} cursor={cursor} onSeek={onSeek} columns={stream.columns.map((column) => column.name)} /><div className="mt-2 text-[10px] text-muted-foreground">Coverage is server-measured on the reference clock: {evidence?.coverageIntervals.map(([start, end]) => `[${start}, ${end})`).join(', ') || 'not available'}.</div></>}</section>
}

function ClockMapping({ streamId, mapping, referenceClockId }: { streamId: string; mapping: InspectionEvidenceStream['clockMapping']; referenceClockId: string }) {
  if (!mapping) return null
  const matches = mapping.targetClockId === referenceClockId
  return <div data-testid={`clock-mapping-${streamId}`} className={`mt-2 rounded px-2 py-1.5 font-mono text-[10px] ${matches ? 'bg-muted/40 text-muted-foreground' : 'bg-destructive/5 text-destructive'}`}>{matches ? `${mapping.sourceClockId} → ${mapping.targetClockId} (reference ${referenceClockId}) · scale ${mapping.scaleNumerator}/${mapping.scaleDenominator} · offset ${mapping.offsetTick}` : `Clock mapping target ${mapping.targetClockId} does not match reference ${referenceClockId}; mapped rows are not presented as aligned.`}</div>
}

function nearestObservation(observations: InspectionWindowObservation[], cursor: number) {
  return observations.filter((item) => item.kind === 'point').map((item) => ({ item, delta: Math.abs(item.startTick - cursor) })).sort((left, right) => left.delta - right.delta || left.item.startTick - right.item.startTick || left.item.observationId.localeCompare(right.item.observationId))[0] ?? null
}

function Nearest({ observation, cursor, tolerance }: { observation: ReturnType<typeof nearestObservation>; cursor: number; tolerance: number }) {
  if (!observation) return <div className="mt-2 rounded bg-muted/40 px-2 py-1.5 text-[11px] text-muted-foreground">No point observation is available in this bounded window.</div>
  return <div data-testid="nearest-observation" className="mt-2 rounded bg-muted/40 px-2 py-1.5 text-[11px] text-foreground"><strong>Nearest point:</strong> {observation.item.observationId} at {observation.item.startTick}; Δ {observation.delta} ticks from cursor {cursor} · tolerance {tolerance} ticks. Values are not interpolated.</div>
}

function IntervalMarks({ observations, cursor, onSeek }: { observations: InspectionWindowObservation[]; cursor: number; onSeek: (tick: number) => void }) {
  if (!observations.length) return null
  return <div className="mt-2"><div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Interval marks</div><div className="mt-1 flex flex-wrap gap-1">{observations.map((item) => <button key={item.observationId} type="button" onClick={() => onSeek(item.startTick)} aria-pressed={cursor >= item.startTick && cursor < (item.endTick ?? item.startTick)} className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] hover:bg-accent aria-pressed:bg-primary/10">[{item.startTick}, {item.endTick}) {item.observationId}</button>)}</div></div>
}

function Rows({ observations, cursor, onSeek, columns }: { observations: InspectionWindowObservation[]; cursor: number; onSeek: (tick: number) => void; columns: string[] }) {
  if (!observations.length) return null
  const visible = columns.filter((column) => !['observation_id', 'episode_id', 'device_tick', 'start_tick', 'end_tick'].includes(column))
  const cells = visible.length ? visible : ['values']
  return <div className="mt-3 overflow-auto rounded border border-border"><table className="w-full text-left text-[10.5px]"><thead className="sticky top-0 bg-muted"><tr><th className="px-2 py-1">reference tick</th><th className="px-2 py-1">kind</th>{cells.map((column) => <th key={column} className="px-2 py-1">{column}</th>)}</tr></thead><tbody>{observations.map((item) => <tr key={item.observationId} data-testid={`observation-${item.observationId}`} className={cursor >= item.startTick && cursor < (item.endTick ?? item.startTick + 1) ? 'bg-primary/10' : 'border-t border-border/50'}><td className="whitespace-nowrap px-2 py-1 font-mono"><button type="button" onClick={() => onSeek(item.startTick)} className="underline decoration-dotted underline-offset-2">{item.kind === 'interval' ? `[${item.startTick}, ${item.endTick})` : item.startTick}</button></td><td className="px-2 py-1">{item.kind}</td>{cells.map((column) => <td key={column} className="max-w-[220px] truncate px-2 py-1 font-mono" title={column === 'values' ? JSON.stringify(item.values) : displayValue(item.values[column])}>{column === 'values' ? JSON.stringify(item.values) : displayValue(item.values[column])}</td>)}</tr>)}</tbody></table></div>
}
