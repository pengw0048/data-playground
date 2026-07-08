import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { syncPluginCapabilities } from './capabilities'
import { allCapabilities, capabilitiesFor } from './registry'
import type { ColumnSchema } from '../types/graph'

const col = (name: string, caps: string[] = []) => ({ name, type: 'string', capabilities: caps }) as ColumnSchema

describe('plugin capability viewer tabs (syncPluginCapabilities)', () => {
  it('registers a generic tab for a known viewer kind; skips an unknown kind', () => {
    syncPluginCapabilities([
      { id: 'json-doc', label: 'JSON', viewer: { kind: 'json' } },
      { id: 'weird', label: 'Weird', viewer: { kind: 'no-such-renderer' } },
    ])
    const ids = allCapabilities().map((c) => c.id)
    expect(ids).toContain('json-doc')   // known kind → tab registered from a generic renderer
    expect(ids).not.toContain('weird')  // unknown kind → skipped (detector still tags cols; just no tab)

    // the tab shows only for columns that carry the capability id
    expect(capabilitiesFor([col('payload', ['json-doc'])]).map((c) => c.id)).toContain('json-doc')
    expect(capabilitiesFor([col('payload', [])]).map((c) => c.id)).not.toContain('json-doc')
  })

  it('the json renderer pretty-prints the tagged column cell', () => {
    syncPluginCapabilities([{ id: 'json-doc', label: 'JSON', viewer: { kind: 'json' } }])
    const Tab = allCapabilities().find((c) => c.id === 'json-doc')!.viewerTab!
    render(<Tab columns={[col('payload', ['json-doc'])]} rows={[{ payload: '{"a":1}' }]} />)
    expect(screen.getByText(/"a": 1/)).toBeInTheDocument()  // JSON.stringify(…, null, 2) indentation
  })
})
