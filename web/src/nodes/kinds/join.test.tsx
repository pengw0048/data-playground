import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ReactFlowProvider } from '@xyflow/react'

// importing the store triggers autosave side-effects → stub the api client (mirrors store.test.ts)
vi.mock('../../api/client', () => ({ api: new Proxy({}, { get: () => async () => ({}) }) }))

import './join'                                  // registers the hand-built Join card via register()
import { getComponent } from '../registry'
import { registerGenericNodes } from '../generic'
import { useStore } from '../../store/graph'

describe('Join card — join types come from the backend spec (UX-05)', () => {
  beforeEach(() => {
    // seed the backend spec the card derives its `how` options from (source of truth = all 4 types)
    registerGenericNodes([{
      kind: 'join', title: 'join', category: 'compute',
      inputs: [{ id: 'a', wire: 'dataset' }, { id: 'b', wire: 'dataset' }],
      outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false, blurb: '',
      params: [{ name: 'how', type: 'select', default: 'inner', options: ['inner', 'left', 'right', 'outer'] },
               { name: 'on', type: 'string' }, { name: 'condition', type: 'string' }],
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    }] as any)
    useStore.setState({
      doc: { id: 'c', version: 1, name: 't', requirements: [],
             nodes: [{ id: 'j', type: 'join', position: { x: 0, y: 0 },
                       data: { title: 'join', status: 'draft', config: { how: 'inner', on: 'id' } } },
                      { id: 'left', type: 'source', position: { x: 0, y: 0 }, data: { title: 'left', status: 'draft', config: {} } },
                      { id: 'right', type: 'source', position: { x: 0, y: 0 }, data: { title: 'right', status: 'draft', config: {} } }],
             edges: [{ id: 'a', source: 'left', target: 'j', targetHandle: 'a', data: { wire: 'dataset' } },
                     { id: 'b', source: 'right', target: 'j', targetHandle: 'b', data: { wire: 'dataset' } }] },
      schemas: {
        left: { out: [{ name: '_rowid', type: 'string', capabilities: [] }, { name: 'id', type: 'string', capabilities: [] }, { name: 'region', type: 'string', capabilities: [] }] },
        right: { out: [{ name: 'original_row_id', type: 'string', capabilities: [] }, { name: 'id', type: 'string', capabilities: [] }, { name: 'shared', type: 'string', capabilities: [] }, { name: 'region_id', type: 'string', capabilities: [] }] },
      },
      canvasRole: 'owner',
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } as any)
  })

  it('offers every backend join type (inner/left/right/outer), not a hardcoded subset', () => {
    const Join = getComponent('join')!
    const data = useStore.getState().doc.nodes[0].data
    render(<ReactFlowProvider><Join id="j" data={data} /></ReactFlowProvider>)
    for (const h of ['inner', 'left', 'right', 'outer']) {
      expect(screen.getByRole('option', { name: h })).toBeInTheDocument()
    }
  })

  it('uses separate port-aware selectors and serializes ordered heterogeneous pairs', () => {
    const Join = getComponent('join')!
    const data = useStore.getState().doc.nodes[0].data
    render(<ReactFlowProvider><Join id="j" data={data} /></ReactFlowProvider>)
    const left = screen.getByLabelText('Left key 1')
    const right = screen.getByLabelText('Right key 1')
    expect(left).toHaveValue('id')
    expect(right).toHaveValue('id')
    expect(left).not.toHaveTextContent('original_row_id')
    expect(right).not.toHaveTextContent('_rowid')

    fireEvent.change(left, { target: { value: '_rowid' } })
    fireEvent.change(right, { target: { value: 'original_row_id' } })
    expect(useStore.getState().doc.nodes[0].data.config).toMatchObject({ on: '', condition: 'a._rowid = b.original_row_id' })

    fireEvent.click(screen.getByRole('button', { name: 'Add key pair' }))
    fireEvent.change(screen.getByLabelText('Left key 2'), { target: { value: 'region' } })
    fireEvent.change(screen.getByLabelText('Right key 2'), { target: { value: 'region_id' } })
    expect(useStore.getState().doc.nodes[0].data.config).toMatchObject({
      condition: 'a._rowid = b.original_row_id AND a.region = b.region_id', on: '',
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add key pair' }))
    expect(screen.getByLabelText('Left key 3')).toHaveValue('')
    expect(screen.getByLabelText('Right key 3')).toHaveValue('')
    expect(useStore.getState().doc.nodes[0].data.config).toMatchObject({
      condition: 'a._rowid = b.original_row_id AND a.region = b.region_id', on: '',
    })
  })

  it('keeps an incomplete pair locally until either selection order makes it executable', async () => {
    useStore.getState().updateConfig('j', { on: '', condition: '' })
    const Join = getComponent('join')!
    const data = useStore.getState().doc.nodes[0].data
    const { unmount } = render(<ReactFlowProvider><Join id="j" data={data} /></ReactFlowProvider>)
    fireEvent.change(screen.getByLabelText('Left key 1'), { target: { value: '_rowid' } })
    await waitFor(() => expect(screen.getByLabelText('Left key 1')).toHaveValue('_rowid'))
    expect(useStore.getState().doc.nodes[0].data.config).toMatchObject({ on: '', condition: '' })
    fireEvent.change(screen.getByLabelText('Right key 1'), { target: { value: 'original_row_id' } })
    expect(useStore.getState().doc.nodes[0].data.config.condition).toBe('a._rowid = b.original_row_id')
    unmount()

    useStore.getState().updateConfig('j', { on: '', condition: '' })
    render(<ReactFlowProvider><Join id="j" data={useStore.getState().doc.nodes[0].data} /></ReactFlowProvider>)
    fireEvent.change(screen.getByLabelText('Right key 1'), { target: { value: 'original_row_id' } })
    await waitFor(() => expect(screen.getByLabelText('Right key 1')).toHaveValue('original_row_id'))
    expect(useStore.getState().doc.nodes[0].data.config).toMatchObject({ on: '', condition: '' })
    fireEvent.change(screen.getByLabelText('Left key 1'), { target: { value: '_rowid' } })
    expect(useStore.getState().doc.nodes[0].data.config.condition).toBe('a._rowid = b.original_row_id')
  })

  it('keeps stored keys visible when either input schema is unavailable', () => {
    useStore.setState({ schemas: {} as never })
    const Join = getComponent('join')!
    render(<ReactFlowProvider><Join id="j" data={useStore.getState().doc.nodes[0].data} /></ReactFlowProvider>)
    expect(screen.getByLabelText('Left key 1')).toHaveValue('id')
    expect(screen.getByLabelText('Right key 1')).toHaveValue('id')
    expect(screen.getAllByText('id (schema unavailable)')).toHaveLength(2)
  })

  it('lets the last configured pair become an empty local draft', () => {
    const Join = getComponent('join')!
    render(<ReactFlowProvider><Join id="j" data={useStore.getState().doc.nodes[0].data} /></ReactFlowProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Remove key pair 1' }))
    expect(screen.getByLabelText('Left key 1')).toHaveValue('')
    expect(screen.getByLabelText('Right key 1')).toHaveValue('')
    expect(useStore.getState().doc.nodes[0].data.config).toMatchObject({ on: '', condition: '' })
  })

  it('keeps an arbitrary existing condition raw instead of silently rewriting it', () => {
    useStore.getState().updateConfig('j', { on: '', condition: 'a.id = b.id OR a.region = b.region_id' })
    const Join = getComponent('join')!
    const data = useStore.getState().doc.nodes[0].data
    render(<ReactFlowProvider><Join id="j" data={data} /></ReactFlowProvider>)
    expect(screen.getByLabelText('advanced ON condition')).toHaveValue('a.id = b.id OR a.region = b.region_id')
    expect(screen.queryByLabelText('Left key 1')).not.toBeInTheDocument()
    expect(useStore.getState().doc.nodes[0].data.config.condition).toBe('a.id = b.id OR a.region = b.region_id')
  })

  it('keeps a user in Advanced while a raw condition becomes representable', async () => {
    const Join = getComponent('join')!
    render(<ReactFlowProvider><Join id="j" data={useStore.getState().doc.nodes[0].data} /></ReactFlowProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Advanced condition' }))
    const raw = screen.getByLabelText('advanced ON condition')
    raw.focus()
    fireEvent.change(raw, { target: { value: 'a._rowid = b.original_row_id' } })
    await waitFor(() => expect(screen.getByLabelText('advanced ON condition')).toHaveValue('a._rowid = b.original_row_id'))
    expect(screen.getByLabelText('advanced ON condition')).toHaveFocus()
    fireEvent.click(screen.getByRole('button', { name: 'Use key builder' }))
    expect(screen.getByLabelText('Left key 1')).toHaveValue('_rowid')
  })

  it('exposes an effective on predicate in Advanced and clears hidden on at first edit', async () => {
    useStore.getState().updateConfig('j', { on: 'shared', condition: '' })
    useStore.setState((state) => ({
      schemas: {
        ...state.schemas,
        left: { out: [...(state.schemas.left?.out ?? []), { name: 'shared', type: 'string', capabilities: [] }] },
      },
    }))
    const Join = getComponent('join')!
    render(<ReactFlowProvider><Join id="j" data={useStore.getState().doc.nodes[0].data} /></ReactFlowProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Advanced condition' }))
    const raw = screen.getByLabelText('advanced ON condition')
    expect(raw).toHaveValue('a.shared = b.shared')
    fireEvent.change(raw, { target: { value: '' } })
    expect(useStore.getState().doc.nodes[0].data.config).toMatchObject({ on: '', condition: '' })
    fireEvent.change(screen.getByLabelText('advanced ON condition'), {
      target: { value: 'a.shared = b.shared OR a.region = b.region_id' },
    })
    expect(useStore.getState().doc.nodes[0].data.config).toMatchObject({
      on: '', condition: 'a.shared = b.shared OR a.region = b.region_id',
    })
    fireEvent.change(screen.getByLabelText('advanced ON condition'), { target: { value: 'a.shared = b.shared' } })
    await waitFor(() => expect(screen.getByLabelText('advanced ON condition')).toHaveValue('a.shared = b.shared'))
    expect(screen.getByLabelText('advanced ON condition')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Use key builder' }))
    expect(screen.getByLabelText('Left key 1')).toHaveValue('shared')
    expect(screen.getByLabelText('Right key 1')).toHaveValue('shared')
  })
})
