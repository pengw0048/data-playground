import { useEffect, useState } from 'react'

export type CollapsibleRegion = 'navigation' | 'inspector'

const STORAGE_KEY: Record<CollapsibleRegion, string> = {
  navigation: 'dp-layout-navigation-collapsed',
  inspector: 'dp-layout-inspector-collapsed',
}

// A fresh 1024px browse/inspect session starts compact, while the supported authoring viewport keeps
// the familiar expanded chrome. Once a person chooses, ordinary refreshes keep that explicit choice.
export function initialRegionCollapsed(region: CollapsibleRegion, viewportWidth: number) {
  try {
    const stored = localStorage.getItem(STORAGE_KEY[region])
    if (stored === 'true') return true
    if (stored === 'false') return false
  } catch { /* storage unavailable: use the responsive default */ }
  return viewportWidth < 1200
}

export function useCollapsibleRegion(region: CollapsibleRegion) {
  const [collapsed, setCollapsed] = useState(() => initialRegionCollapsed(region, window.innerWidth))

  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY[region], String(collapsed)) } catch { /* non-persistent layout is still usable */ }
  }, [collapsed, region])

  return [collapsed, setCollapsed] as const
}
