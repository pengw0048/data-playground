// Plugin loading: an eager glob over nodes/kinds/* runs each module's register()
// side effect, so adding a node kind is just dropping a file. Capabilities register the same way.
import type { ComponentType } from 'react'
import { allSpecs, getComponent } from './registry'

// eager side-effect imports — every kind + capability registers on load
import.meta.glob('./kinds/*.tsx', { eager: true })
import './capabilities'

/** React Flow nodeTypes map, derived from the registry. */
export function buildNodeTypes(): Record<string, ComponentType<any>> {
  const out: Record<string, ComponentType<any>> = {}
  for (const spec of allSpecs()) {
    const c = getComponent(spec.kind)
    if (c) out[spec.kind] = c as ComponentType<any>
  }
  return out
}

export { allSpecs }
