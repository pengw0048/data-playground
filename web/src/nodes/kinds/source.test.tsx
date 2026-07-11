import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ReactFlowProvider } from '@xyflow/react'

// importing the store triggers autosave side-effects → stub the api client
vi.mock('../../api/client', () => ({ api: new Proxy({}, { get: () => async () => ({}) }) }))

import './source'                          // registers the Source card via register()
import { getComponent } from '../registry'
import { useStore } from '../../store/graph'

const Source = getComponent('source')!
const render1 = (data: object) =>
  render(<ReactFlowProvider><Source id="s1" data={data as never} /></ReactFlowProvider>)

describe('Source card — honest counts + empty/offline (UX-14)', () => {
  beforeEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ kernelUp: true, catalog: [
      { id: 't1', name: 'orders', uri: 'mem://orders', rowCount: null, version: 'v1', columns: [{ name: 'a', type: 'int', capabilities: [] }] },
    ] } as any)
  })
  afterEach(() => cleanup())

  it('shows "—" for an unknown row count, not a fake "0 rows"', () => {
    render1({ title: 'source', status: 'draft', config: { tableId: 't1' } })
    expect(screen.getByText(/—\s*rows/)).toBeInTheDocument()
    expect(screen.queryByText(/\b0\s*rows/)).toBeNull()
  })

  it('still shows "0 rows" for a genuinely empty table', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ kernelUp: true, catalog: [
      { id: 't1', name: 'orders', uri: 'mem://orders', rowCount: 0, version: 'v1', columns: [{ name: 'a', type: 'int', capabilities: [] }] },
    ] } as any)
    render1({ title: 'source', status: 'draft', config: { tableId: 't1' } })
    expect(screen.getByText(/\b0\s*rows/)).toBeInTheDocument()
  })

  it('says "No datasets yet" when the kernel is up but the catalog is empty (not "offline")', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ kernelUp: true, catalog: [] } as any)
    render1({ title: 'source', status: 'draft', config: {} })
    fireEvent.click(screen.getByText(/select dataset/i))
    expect(screen.getByText(/No datasets yet/i)).toBeInTheDocument()
  })

  it('says "Kernel offline" only when the kernel is actually down', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    useStore.setState({ kernelUp: false, catalog: [] } as any)
    render1({ title: 'source', status: 'draft', config: {} })
    fireEvent.click(screen.getByText(/select dataset/i))
    expect(screen.getByText(/Kernel offline/i)).toBeInTheDocument()
  })
})
