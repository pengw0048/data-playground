import { beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { initialRegionCollapsed } from './layoutPreferences'

describe('responsive layout preferences', () => {
  const values = new Map<string, string>()
  beforeAll(() => Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      clear: () => values.clear(),
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    },
  }))
  beforeEach(() => localStorage.clear())

  it('uses compact chrome for a fresh browse-sized viewport', () => {
    expect(initialRegionCollapsed('navigation', 1024)).toBe(true)
    expect(initialRegionCollapsed('inspector', 1024)).toBe(true)
    expect(initialRegionCollapsed('navigation', 1280)).toBe(false)
    expect(initialRegionCollapsed('inspector', 1280)).toBe(false)
  })

  it('lets a persisted choice override the responsive default', () => {
    localStorage.setItem('dp-layout-navigation-collapsed', 'false')
    localStorage.setItem('dp-layout-inspector-collapsed', 'true')

    expect(initialRegionCollapsed('navigation', 1024)).toBe(false)
    expect(initialRegionCollapsed('inspector', 1440)).toBe(true)
  })
})
