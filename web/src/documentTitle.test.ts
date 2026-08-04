import { describe, expect, it } from 'vitest'
import {
  MAX_CANVAS_TITLE_GRAPHEMES,
  NEUTRAL_DOCUMENT_TITLE,
  PRODUCT_TITLE,
  UNTITLED_CANVAS_TITLE,
  displayCanvasTitleName,
  productDocumentTitle,
  projectDocumentTitle,
} from './documentTitle'

describe('displayCanvasTitleName', () => {
  it('maps empty and whitespace-only names to Untitled', () => {
    expect(displayCanvasTitleName(undefined)).toBe(UNTITLED_CANVAS_TITLE)
    expect(displayCanvasTitleName(null)).toBe(UNTITLED_CANVAS_TITLE)
    expect(displayCanvasTitleName('')).toBe(UNTITLED_CANVAS_TITLE)
    expect(displayCanvasTitleName('   \t\n')).toBe(UNTITLED_CANVAS_TITLE)
  })

  it('preserves meaningful Unicode and emoji without mutating the stored string', () => {
    expect(displayCanvasTitleName('研究笔记')).toBe('研究笔记')
    expect(displayCanvasTitleName('Pipeline 🧪')).toBe('Pipeline 🧪')
  })

  it('bounds grapheme length without changing the original beyond the rendered label', () => {
    const long = `${'名'.repeat(MAX_CANVAS_TITLE_GRAPHEMES + 8)}尾`
    const rendered = displayCanvasTitleName(long)
    expect(rendered.endsWith('…')).toBe(true)
    expect([...new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(rendered)].length)
      .toBe(MAX_CANVAS_TITLE_GRAPHEMES)
    expect(long.endsWith('…')).toBe(false)
  })
})

describe('projectDocumentTitle', () => {
  const base = {
    view: 'canvas' as const,
    canvasId: 'canvas-a',
    canvasName: 'Private alpha',
  }

  it('keeps auth and bootstrap phases neutral', () => {
    for (const phase of ['checking', 'unavailable', 'bootstrapping'] as const) {
      expect(projectDocumentTitle({ ...base, phase })).toBe(NEUTRAL_DOCUMENT_TITLE)
    }
  })

  it('titles the login gate without leaking a Canvas name', () => {
    expect(projectDocumentTitle({ ...base, phase: 'login' }))
      .toBe(productDocumentTitle('Sign in'))
  })

  it('projects Canvas and shell titles from committed view state', () => {
    expect(projectDocumentTitle({ ...base, phase: 'ready' }))
      .toBe(`Private alpha · ${PRODUCT_TITLE}`)
    expect(projectDocumentTitle({ ...base, phase: 'ready', canvasName: '  ' }))
      .toBe(`${UNTITLED_CANVAS_TITLE} · ${PRODUCT_TITLE}`)
    expect(projectDocumentTitle({ ...base, phase: 'ready', view: 'workspace' }))
      .toBe(`Workspace · ${PRODUCT_TITLE}`)
    expect(projectDocumentTitle({ ...base, phase: 'ready', view: 'jobs' }))
      .toBe(`Jobs · ${PRODUCT_TITLE}`)
    expect(projectDocumentTitle({ ...base, phase: 'ready', view: 'inbox' }))
      .toBe(`Inbox · ${PRODUCT_TITLE}`)
    expect(projectDocumentTitle({ ...base, phase: 'ready', view: 'transforms' }))
      .toBe(`Transforms · ${PRODUCT_TITLE}`)
    expect(projectDocumentTitle({ ...base, phase: 'ready', view: 'relationships' }))
      .toBe(`Relationships · ${PRODUCT_TITLE}`)
    expect(projectDocumentTitle({
      ...base, phase: 'ready', view: 'relationships', relationshipsMode: 'lineage',
    })).toBe(`Lineage · ${PRODUCT_TITLE}`)
    expect(projectDocumentTitle({ ...base, phase: 'ready', view: 'files' }))
      .toBe(`Workspace · ${PRODUCT_TITLE}`)
  })

  it('uses a neutral title while a deep-linked Canvas id is still unresolved', () => {
    expect(projectDocumentTitle({
      ...base,
      phase: 'ready',
      routeCanvasId: 'canvas-b',
    })).toBe(NEUTRAL_DOCUMENT_TITLE)
  })

  it('shows the Canvas name once the committed id matches the route claim', () => {
    expect(projectDocumentTitle({
      ...base,
      phase: 'ready',
      routeCanvasId: 'canvas-a',
    })).toBe(`Private alpha · ${PRODUCT_TITLE}`)
  })

  it('clears a Canvas name as soon as a shell view owns navigation', () => {
    expect(projectDocumentTitle({
      phase: 'ready',
      view: 'workspace',
      canvasId: 'canvas-a',
      canvasName: 'Private alpha',
      routeCanvasId: 'canvas-a',
    })).toBe(`Workspace · ${PRODUCT_TITLE}`)
  })
})
