// Decorative Canvas URL title slug. Lookup, auth, drafts, CAS, sharing, and recovery use only the
// immutable file key; this helper never participates in identity.

const MAX_CODE_POINTS = 80
const UNTITLED = /^untitled$/iu
// URL delimiters plus ASCII controls. Remaining punctuation is collapsed to '-' as a separator.
const FORBIDDEN = /[\u0000-\u001f\u007f#/?&=%\\]/u
const SEPARATORS = /[\s._,;:|+~`'"/\\]+/gu

export function canvasTitleSlug(title: string | null | undefined): string | undefined {
  if (title == null) return undefined
  const normalized = title.normalize('NFC').trim()
  if (!normalized || UNTITLED.test(normalized)) return undefined

  const cleaned = Array.from(normalized)
    .map((char) => (FORBIDDEN.test(char) ? '' : char))
    .join('')
    .replace(SEPARATORS, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')

  if (!cleaned) return undefined
  const capped = Array.from(cleaned).slice(0, MAX_CODE_POINTS).join('').replace(/-$/u, '')
  return capped || undefined
}
