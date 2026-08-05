/** Read the immutable Canvas file key from either an absolute URL or a hash route. */
export function canvasIdFromLocation(value: string | URL): string {
  const hash = value instanceof URL
    ? value.hash
    : value.startsWith('#')
      ? value
      : new URL(value).hash
  const [segment, encodedId] = hash.replace(/^#\/?/, '').split('?', 1)[0].split('/')
  if (segment !== 'canvas' || !encodedId) {
    throw new Error(`Expected a Canvas route, received ${hash || '<empty hash>'}`)
  }
  return decodeURIComponent(encodedId)
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Match one Canvas destination while treating its decorative title slug as non-identity. */
export function canvasRoutePattern(fileKey: string, nodeId?: string): RegExp {
  const encodedKey = escapeRegExp(encodeURIComponent(fileKey))
  const slug = '(?:/[^/?#]+)?'
  const query = nodeId
    ? `\\?${escapeRegExp(new URLSearchParams({ node: nodeId }).toString())}`
    : ''
  return new RegExp(`/#/canvas/${encodedKey}${slug}${query}$`)
}
