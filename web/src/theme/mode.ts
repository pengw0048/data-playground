// Light / dark theme controller. The palette lives in CSS: `:root` (light) + `[data-theme='dark']`
// (index.css) for the shadcn tokens and the legacy vars. This module only decides WHICH is active by
// stamping `data-theme` on <html>, persists the user's choice, and follows the OS when set to 'system'.

export type ThemeMode = 'light' | 'dark' | 'system'
const KEY = 'dp-theme'
const mql = () => window.matchMedia('(prefers-color-scheme: dark)')

export function getThemeMode(): ThemeMode {
  const v = localStorage.getItem(KEY)
  return v === 'light' || v === 'dark' || v === 'system' ? v : 'system'
}

/** The theme actually showing right now ('system' resolved against the OS). */
export function resolvedTheme(): 'light' | 'dark' {
  const m = getThemeMode()
  return m === 'system' ? (mql().matches ? 'dark' : 'light') : m
}

function apply(mode: ThemeMode): void {
  const resolved = mode === 'system' ? (mql().matches ? 'dark' : 'light') : mode
  // light is the default (no attribute) so nothing regresses if JS is disabled/mid-load
  if (resolved === 'dark') document.documentElement.setAttribute('data-theme', 'dark')
  else document.documentElement.removeAttribute('data-theme')
}

export function setThemeMode(mode: ThemeMode): void {
  if (mode === 'system') localStorage.removeItem(KEY)
  else localStorage.setItem(KEY, mode)
  apply(mode)
  window.dispatchEvent(new Event('dp-theme-change'))  // let toggles re-render
}

/** Toggle between light and dark (collapses 'system' to its opposite of what's showing). */
export function toggleTheme(): void {
  setThemeMode(resolvedTheme() === 'dark' ? 'light' : 'dark')
}

/** Apply the saved choice on boot and keep following the OS while in 'system' mode. Call once. */
export function initTheme(): void {
  apply(getThemeMode())
  mql().addEventListener('change', () => { if (getThemeMode() === 'system') apply('system') })
}
