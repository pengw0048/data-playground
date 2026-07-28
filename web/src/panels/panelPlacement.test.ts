import { describe, expect, it } from 'vitest'
import { DATA_PANEL_USEFUL_CONTENT_HEIGHT, dataPanelPlacement } from './panelPlacement'

describe('data panel placement', () => {
  it('docks a below-node panel that would leave less than the useful content height', () => {
    const placement = dataPanelPlacement({
      anchor: { left: 700, right: 920, top: 300, bottom: 420 }, width: 460,
      viewportWidth: 1280, viewportHeight: 720, rightEdge: 980, toolbarTop: 650,
    })

    expect(placement.presentation).toBe('docked')
    expect(placement.top).toBe(12)
    expect(placement.top + placement.maxHeight).toBeLessThanOrEqual(638)
    expect(placement.maxHeight - 41).toBeGreaterThanOrEqual(DATA_PANEL_USEFUL_CONTENT_HEIGHT)
  })

  it('keeps an anchored panel when its below-node content area is useful', () => {
    const placement = dataPanelPlacement({
      anchor: { left: 700, right: 920, top: 180, bottom: 300 }, width: 460,
      viewportWidth: 1440, viewportHeight: 900, rightEdge: 1140, toolbarTop: 830,
    })

    expect(placement).toMatchObject({ presentation: 'anchored', left: 668, top: 312 })
    expect(placement.maxHeight - 41).toBeGreaterThanOrEqual(DATA_PANEL_USEFUL_CONTENT_HEIGHT)
  })

  it('uses the right-side anchor when the Inspector boundary leaves room', () => {
    const placement = dataPanelPlacement({
      anchor: { left: 80, right: 300, top: 180, bottom: 300 }, width: 460,
      viewportWidth: 1440, viewportHeight: 900, rightEdge: 1140, toolbarTop: 830,
    })

    expect(placement).toMatchObject({ presentation: 'anchored', left: 312, top: 180 })
  })

  it('keeps a near-edge anchor when a readable compact width still fits', () => {
    const placement = dataPanelPlacement({
      anchor: { left: 454, right: 686, top: 378, bottom: 553.5 }, width: 460,
      viewportWidth: 1440, viewportHeight: 900, rightEdge: 1140, toolbarTop: 830,
    })

    expect(placement).toMatchObject({ presentation: 'anchored', left: 698, width: 430 })
  })
})
