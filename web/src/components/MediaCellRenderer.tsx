import type { ColumnSchema } from '../types/graph'

type MediaKind = NonNullable<ColumnSchema['mediaKind']>
type DisplayKind = Exclude<MediaKind, 'unknown'>
type Viewport = 'grid' | 'table' | 'detail' | 'compact'

export interface MediaCellRendererProps {
  value: unknown
  column: string
  mediaKind?: ColumnSchema['mediaKind']
  viewport?: Viewport
}

type DirectMedia = { source: string; kind: DisplayKind }

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

// A Canvas result has no exact-revision cell locator. Keep generic media evidence-based: only a
// browser-displayable data or public URL earns a media surface.
export function canRenderDirectMedia(value: unknown, mediaKind?: ColumnSchema['mediaKind']): boolean {
  return directMedia(value, mediaKind) !== null
}

function frameClass(viewport: Viewport) {
  if (viewport === 'compact') return 'min-h-10 w-[150px] text-[9px]'
  if (viewport === 'detail') return 'min-h-[150px] w-full max-w-[360px] text-[11px]'
  if (viewport === 'table') return 'min-h-[112px] w-[180px] text-[10px]'
  return 'min-h-[132px] w-full text-[10.5px]'
}

function MediaElement({ media, viewport }: { media: DirectMedia; viewport: Viewport }) {
  const fit = viewport === 'compact' ? 'h-10 w-[56px]' : 'h-full w-full'
  if (media.kind === 'video') return <video aria-label="Media video" src={media.source} controls preload="metadata" playsInline className={`${fit} rounded object-cover`} />
  return <img alt="Media image" src={media.source} loading="lazy" className={`${fit} rounded object-cover`} />
}

function State({ children }: { children: string }) {
  return <div role="status" className="grid place-items-center p-2 text-center leading-snug text-muted-foreground"><div>{children}</div></div>
}

// Binary cells remain raw data until a future provider capability can address one exact row without
// asking the researcher to certify a whole revision.
export function MediaCellRenderer({ value, column: _column, mediaKind, viewport = 'grid' }: MediaCellRendererProps) {
  void _column
  const direct = directMedia(value, mediaKind)
  return <div className={`grid overflow-hidden rounded-md border border-border/60 bg-muted/30 ${frameClass(viewport)}`}>
    {value == null ? <State>Media value is empty.</State>
      : direct ? <MediaElement media={direct} viewport={viewport} />
        : <State>Binary media preview is unavailable.</State>}
  </div>
}
