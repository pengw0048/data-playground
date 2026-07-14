/**
 * Declared minimum supported viewport for the Data Playground workbench.
 *
 * Product docs (docs/BROWSER_SUPPORT.md) and the Playwright min-viewport project
 * must both reference this value so the documented claim and the CI proof stay aligned.
 */
export const MIN_VIEWPORT = { width: 1280, height: 720 } as const
