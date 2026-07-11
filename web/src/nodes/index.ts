// Plugin loading: an eager glob over nodes/kinds/* runs each module's register()
// side effect, so adding a node kind is just dropping a file. Capabilities register the same way.
import type { ComponentType } from 'react'
import { allSpecs, getComponent } from './registry'

// eager side-effect imports — every kind + capability registers on load. EXCLUDE colocated *.test.tsx:
// a bare './kinds/*.tsx' also matches join.test.tsx / source.test.tsx, pulling vitest + @testing-library
// into the PRODUCTION bundle — `vi.mock()` then throws "Vitest mocker was not initialized" on load and
// the app renders a blank page (and every e2e spec fails). The negative glob keeps tests out of the app.
import.meta.glob(['./kinds/*.tsx', '!./kinds/*.test.tsx'], { eager: true })
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
