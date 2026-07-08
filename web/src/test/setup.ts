import '@testing-library/jest-dom/vitest'

// jsdom is missing a few browser APIs Radix (Dialog/Select/Popover) touches — polyfill no-ops so
// component tests can render them.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub
if (!window.matchMedia) {
  window.matchMedia = () => ({ matches: false, media: '', onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false }) as unknown as MediaQueryList
}
for (const m of ['scrollIntoView', 'hasPointerCapture', 'releasePointerCapture'] as const) {
  if (!(Element.prototype as unknown as Record<string, unknown>)[m]) {
    (Element.prototype as unknown as Record<string, unknown>)[m] = m === 'hasPointerCapture' ? () => false : () => {}
  }
}
