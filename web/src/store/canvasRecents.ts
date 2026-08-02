const OPENED_AT_KEY = (uid: string) => `dp-opened-at-${uid}`
const MAX_OPENED_AT_ENTRIES = 200

function openedAtMap(uid: string): Record<string, string> {
  try {
    const parsed = JSON.parse(localStorage.getItem(OPENED_AT_KEY(uid)) ?? '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const valid: Record<string, string> = {}
    for (const [canvasId, value] of Object.entries(parsed)) {
      if (canvasId.length <= 512 && typeof value === 'string' && Number.isFinite(Date.parse(value))) {
        valid[canvasId] = value
      }
    }
    return valid
  } catch {
    return {}
  }
}

export function rememberCanvasOpenedAt(uid: string, canvasId: string, openedAt = new Date()): void {
  try {
    const next = { ...openedAtMap(uid), [canvasId]: openedAt.toISOString() }
    const bounded = Object.fromEntries(Object.entries(next)
      .sort((left, right) => right[1].localeCompare(left[1]))
      .slice(0, MAX_OPENED_AT_ENTRIES))
    localStorage.setItem(OPENED_AT_KEY(uid), JSON.stringify(bounded))
  } catch { /* recency is optional device-local presentation metadata */ }
}

export function canvasOpenedAt(uid: string | undefined, canvasId: string): string | null {
  if (!uid) return null
  return openedAtMap(uid)[canvasId] ?? null
}
