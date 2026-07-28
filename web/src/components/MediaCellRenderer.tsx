import { useEffect, useRef, useState } from 'react'
import { api, KernelError } from '../api/client'
import type { MediaCellIdentityValue } from '../types/api'
import type { ColumnSchema } from '../types/graph'

type MediaKind = NonNullable<ColumnSchema['mediaKind']>
type DisplayKind = Exclude<MediaKind, 'unknown'>
type Viewport = 'grid' | 'table' | 'detail' | 'compact'

export interface ExactMediaCellContext {
  datasetId: string
  revisionId: string
  identity: MediaCellIdentityValue[] | null
  proofStatus: 'certified' | 'unavailable'
  certificationSupported: boolean
  mediaCellSupported: boolean
  onOpenCertification?: () => void
}

export interface MediaCellRendererProps {
  value: unknown
  column: string
  mediaKind?: ColumnSchema['mediaKind']
  exact?: ExactMediaCellContext
  viewport?: Viewport
}

type DirectMedia = { source: string; kind: DisplayKind }
type RemoteMedia = DirectMedia & { requestKey: string }
type Failure = { requestKey: string; message: string; canOpenCertification?: boolean }

const imageExtensions = new Set(['avif', 'bmp', 'gif', 'jpeg', 'jpg', 'png', 'svg', 'webp'])
const videoExtensions = new Set(['m4v', 'mkv', 'mov', 'mp4', 'webm'])

function kindFromMime(value: string): DisplayKind | null {
  const mime = value.split(';', 1)[0].trim().toLowerCase()
  if (mime.startsWith('image/')) return 'image'
  if (mime.startsWith('video/')) return 'video'
  return null
}

function kindFromDirectUrl(value: string): DisplayKind | null {
  if (value.toLowerCase().startsWith('data:')) return kindFromMime(value.slice(5).split(/[;,]/, 1)[0])
  try {
    const url = new URL(value)
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
    const extension = url.pathname.split('.').pop()?.toLowerCase()
    if (extension && imageExtensions.has(extension)) return 'image'
    if (extension && videoExtensions.has(extension)) return 'video'
  } catch {
    return null
  }
  return null
}

function directMedia(value: unknown, mediaKind?: ColumnSchema['mediaKind']): DirectMedia | null {
  if (typeof value !== 'string') return null
  const isData = value.toLowerCase().startsWith('data:')
  if (!isData) {
    try {
      const url = new URL(value)
      if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
    } catch {
      return null
    }
  }
  const inferred = kindFromDirectUrl(value)
  const kind = mediaKind && mediaKind !== 'unknown' ? mediaKind : inferred
  return kind === 'image' || kind === 'video' ? { source: value, kind } : null
}

// A Canvas result has no exact-revision media-cell context. It may therefore advertise media only
// when the value itself is independently displayable by the browser. Keep this evidence-based:
// a capability tag alone is not permission to promise a working media viewer.
export function canRenderDirectMedia(value: unknown, mediaKind?: ColumnSchema['mediaKind']): boolean {
  return directMedia(value, mediaKind) !== null
}

function requestFingerprint(exact: ExactMediaCellContext | undefined, column: string) {
  if (!exact) return ''
  // This is only a React request-generation key. The request below receives the canonical sidecar
  // identity object itself, including its decimal strings, without rebuilding it from preview rows.
  return JSON.stringify([
    exact.datasetId, exact.revisionId, column,
    exact.identity?.map(({ name, arrowType, value }) => [name, arrowType, value]) ?? null,
  ])
}

function endpointFailure(error: unknown): string {
  if (error instanceof KernelError) {
    if (error.status === 413) return 'This media cell is larger than the safe preview limit.'
    if (error.status === 415) return 'This media cell uses an unsupported image or video format.'
    if (error.status === 401 || error.status === 403) return 'You are not authorized to open this media cell.'
    if (error.status === 410) return 'This exact revision is no longer available.'
    if (error.status === 409 && error.code === 'media_cell_identity_unavailable') {
      return 'The certified row identity is no longer available for this exact revision.'
    }
    if (error.status === 409 && error.code === 'media_cell_row_ambiguous') {
      return 'The certified row identity matches more than one row, so this media cannot be opened.'
    }
    if (error.status === 409) {
      return 'This exact media cell cannot be opened because its row identity is unavailable or ambiguous.'
    }
    if (error.status === 404 || error.status === 422) {
      return 'This exact media cell could not be found for its certified row identity.'
    }
    if (error.status >= 500) return 'The media service failed while loading this cell. Try again.'
  }
  return 'This media cell could not be loaded. Check your connection and try again.'
}

function frameClass(viewport: Viewport) {
  if (viewport === 'compact') return 'min-h-10 w-[150px] text-[9px]'
  if (viewport === 'detail') return 'min-h-[150px] w-full max-w-[360px] text-[11px]'
  if (viewport === 'table') return 'min-h-[112px] w-[180px] text-[10px]'
  return 'min-h-[132px] w-full text-[10.5px]'
}

function MediaElement({ media, onError, viewport }: {
  media: DirectMedia
  onError: () => void
  viewport: Viewport
}) {
  const fit = viewport === 'compact' ? 'h-10 w-[56px]' : 'h-full w-full'
  if (media.kind === 'video') return (
    <video aria-label="Media video" src={media.source} controls preload="metadata" playsInline
      className={`${fit} rounded object-cover`} onError={onError} />
  )
  return <img alt="Media image" src={media.source} loading="lazy" className={`${fit} rounded object-cover`} onError={onError} />
}

function State({ children, action }: { children: string; action?: () => void }) {
  return <div role="status" className="grid place-items-center p-2 text-center leading-snug text-muted-foreground">
    <div>
      <div>{children}</div>
      {action && <button type="button" onClick={action} className="mt-1 font-semibold text-primary underline">Open certification</button>}
    </div>
  </div>
}

// This component is deliberately the only media renderer used by generic and exact previews. A
// generic surface has no certified exact context, so it can render only public/data URLs; the exact
// context is the narrow opt-in that may use the bounded cell endpoint.
export function MediaCellRenderer({ value, column, mediaKind, exact, viewport = 'grid' }: MediaCellRendererProps) {
  const direct = directMedia(value, mediaKind)
  const publicDirect = typeof value === 'string' && (() => {
    if (value.toLowerCase().startsWith('data:')) return true
    try {
      const url = new URL(value)
      return url.protocol === 'http:' || url.protocol === 'https:'
    } catch {
      return false
    }
  })()
  const requestKey = requestFingerprint(exact, column)
  const endpointNeeded = !publicDirect && value != null && exact?.mediaCellSupported === true
  const displayKey = publicDirect ? `direct:${value}` : requestKey
  const [remote, setRemote] = useState<RemoteMedia | null>(null)
  const [failure, setFailure] = useState<Failure | null>(null)
  const [activatedRequest, setActivatedRequest] = useState('')
  const generation = useRef(0)
  const objectUrl = useRef<string | null>(null)
  const frame = useRef<HTMLDivElement>(null)

  const revoke = () => {
    if (objectUrl.current) {
      URL.revokeObjectURL(objectUrl.current)
      objectUrl.current = null
    }
  }

  // An exact preview may contain 100 rows and several media fields. Do not turn opening that
  // bounded table into an eager burst of byte reads: activate a cell only when it enters the
  // scroll viewport. Older/test browsers without IntersectionObserver retain the direct fallback.
  const hasIntersectionObserver = typeof IntersectionObserver !== 'undefined'
  const endpointActive = endpointNeeded && (!hasIntersectionObserver || activatedRequest === requestKey)
  useEffect(() => {
    if (!endpointNeeded) return
    if (!hasIntersectionObserver) {
      setActivatedRequest(requestKey)
      return
    }
    const target = frame.current
    if (!target) return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setActivatedRequest(requestKey)
        observer.disconnect()
      }
    }, { rootMargin: '160px 0px' })
    observer.observe(target)
    return () => observer.disconnect()
  }, [endpointNeeded, hasIntersectionObserver, requestKey])

  useEffect(() => {
    const current = ++generation.current
    revoke()
    if (!endpointActive || !exact || !exact.identity) return
    const controller = new AbortController()
    api.openMediaCell(exact.datasetId, exact.revisionId, { identity: exact.identity, column }, { signal: controller.signal })
      .then((blob) => {
        const kind = kindFromMime(blob.type)
        if (!kind) throw new KernelError(415, 'media_cell_unsupported', 'media_cell_unsupported')
        if (generation.current !== current) return
        const source = URL.createObjectURL(blob)
        if (generation.current !== current) {
          URL.revokeObjectURL(source)
          return
        }
        objectUrl.current = source
        setRemote({ source, kind, requestKey })
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || generation.current !== current) return
        revoke()
        setFailure({
          requestKey,
          message: endpointFailure(error),
          canOpenCertification: error instanceof KernelError
            && error.code === 'media_cell_identity_unavailable'
            && exact.certificationSupported,
        })
      })
    return () => {
      controller.abort()
      revoke()
      if (generation.current === current) generation.current += 1
    }
  // The canonical identity values are represented in requestKey; sending uses exact.identity above.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [endpointActive, requestKey])

  const remoteForRequest = remote?.requestKey === requestKey ? remote : null
  const failureForRequest = failure?.requestKey === displayKey ? failure : null
  const media = failureForRequest ? null : (direct ?? remoteForRequest)
  const unrepresentable = endpointNeeded && exact && (!exact.identity || exact.proofStatus !== 'certified')
  const source = media?.source

  const loadFailed = () => {
    if (!source) return
    revoke()
    setRemote(null)
    setFailure({ requestKey: displayKey, message: 'The browser could not display this media.' })
  }

  return <div ref={frame} className={`grid overflow-hidden rounded-md border border-border/60 bg-muted/30 ${frameClass(viewport)}`}>
    {value == null ? <State>Media value is empty.</State>
      : media ? <MediaElement key={source} media={media} viewport={viewport} onError={loadFailed} />
        : failureForRequest ? <State action={failureForRequest.canOpenCertification ? exact?.onOpenCertification : undefined}>{failureForRequest.message}</State>
            : publicDirect ? <State>This public media URL is not a supported image or video.</State>
              : !exact ? <State>Open an exact certified revision to view this media.</State>
                : !exact.mediaCellSupported ? <State>This exact revision does not support bounded media-cell reads.</State>
            : exact.proofStatus !== 'certified' ? <State action={exact.certificationSupported ? exact.onOpenCertification : undefined}>
              {exact.certificationSupported
                ? 'Certify row identity to open media from this exact revision.'
                : 'This exact revision cannot certify row identity, so this media cannot be opened.'}
            </State>
              : unrepresentable ? <State>This preview row has no representable certified identity.</State>
                : !exact.identity ? <State>This preview row has no representable certified identity.</State>
                  : <State>Loading exact media…</State>}
  </div>
}
