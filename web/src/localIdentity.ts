export const LOCAL_MODE_CACHE_KEY = 'dp-confirmed-local-mode-v1'
export const LAST_USER_KEY = 'dp-user'

export function rememberAuthMode(authEnabled: boolean): void {
  try {
    if (authEnabled) localStorage.removeItem(LOCAL_MODE_CACHE_KEY)
    else localStorage.setItem(LOCAL_MODE_CACHE_KEY, '1')
  } catch { /* auth remains authoritative even when browser preferences cannot persist */ }
}

export function confirmedLocalMode(): boolean {
  try { return localStorage.getItem(LOCAL_MODE_CACHE_KEY) === '1' } catch { return false }
}
