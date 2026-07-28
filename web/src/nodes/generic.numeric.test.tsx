import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({ saveCanvas: vi.fn(), estimate: vi.fn() }))
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_target, property) => property === 'saveCanvas' ? apiMocks.saveCanvas
      : property === 'estimate' ? apiMocks.estimate : async () => ({}),
  }),
  KernelError: class KernelError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status } },
  setApiUser: vi.fn(),
}))

import { NodeParamFields, nodeInvalidReason, registerGenericNodes } from './generic'
import { useStore } from '../store/graph'

describe('generic numeric plugin fields', () => {
  beforeEach(() => {
    apiMocks.saveCanvas.mockReset().mockResolvedValue({ ok: true })
    apiMocks.estimate.mockReset().mockResolvedValue({ needsConfirm: false })
    registerGenericNodes([{
      kind: 'plugin-numeric-field-contract', title: 'Plugin numeric fields', category: 'compute',
      inputs: [], outputs: [{ id: 'out', wire: 'dataset' }],
      params: [
        { name: 'count', label: 'Count', type: 'int', required: true },
        { name: 'ratio', label: 'Ratio', type: 'float', required: true, default: 0.5 },
        { name: 'limit', label: 'Limit', type: 'int', required: false },
      ], canBypass: false, previewable: true, blurb: '',
    }])
    useStore.setState({
      canvasRole: 'owner', numericParamDrafts: {}, saved: true,
      doc: { id: 'numeric', name: 'numeric', version: 1, edges: [], nodes: [{
        id: 'plugin', type: 'plugin-numeric-field-contract', position: { x: 0, y: 0 },
        data: { title: 'numeric', status: 'draft', config: { count: 7, limit: 10 } },
      }] },
    })
  })

  it('retains invalid text without saving/running it, then commits complete typed numbers on blur', async () => {
    render(<NodeParamFields nodeId="plugin" />)
    const [count, ratio] = screen.getAllByRole('textbox') as HTMLInputElement[]

    fireEvent.change(count, { target: { value: '12abc' } })
    expect(count).toHaveValue('12abc')
    expect(screen.getByRole('alert')).toHaveTextContent('Count must be a complete safe integer')
    expect(useStore.getState().doc.nodes[0].data.config.count).toBe(7)
    await useStore.getState().save()
    await useStore.getState().requestRun('plugin')
    expect(apiMocks.saveCanvas).not.toHaveBeenCalled()
    expect(apiMocks.estimate).not.toHaveBeenCalled()

    fireEvent.change(count, { target: { value: '  +42  ' } })
    fireEvent.blur(count)
    expect(useStore.getState().doc.nodes[0].data.config.count).toBe(42)
    expect(useStore.getState().numericParamDrafts).toEqual({})

    fireEvent.change(count, { target: { value: '0' } })
    fireEvent.blur(count)
    expect(useStore.getState().doc.nodes[0].data.config.count).toBe(0)

    fireEvent.change(ratio, { target: { value: '-1.25e2' } })
    fireEvent.blur(ratio)
    expect(useStore.getState().doc.nodes[0].data.config.ratio).toBe(-125)

    fireEvent.change(ratio, { target: { value: 'Infinity' } })
    expect(screen.getByRole('alert')).toHaveTextContent('Ratio must be a finite number')
    fireEvent.blur(ratio)
    expect(ratio).toHaveValue('Infinity')
    expect(useStore.getState().doc.nodes[0].data.config.ratio).toBe(-125)
  })

  it('uses the declared default when cleared and keeps a required unset field invalid', () => {
    render(<NodeParamFields nodeId="plugin" />)
    const [count, ratio, limit] = screen.getAllByRole('textbox') as HTMLInputElement[]

    expect(screen.queryByText('Clear to reset to the default (0.5).')).toBeNull()
    expect(screen.getByText('Clear to leave this value unset.')).toBeVisible()

    fireEvent.change(ratio, { target: { value: '0.75' } })
    fireEvent.blur(ratio)
    expect(screen.getByText('Clear to reset to the default (0.5).')).toBeVisible()
    fireEvent.change(ratio, { target: { value: '' } })
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.blur(ratio)
    expect(ratio).toHaveValue('0.5')
    expect(useStore.getState().doc.nodes[0].data.config.ratio).toBe(0.5)
    expect(screen.queryByText('Clear to reset to the default (0.5).')).toBeNull()

    fireEvent.change(limit, { target: { value: '' } })
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.blur(limit)
    expect(limit).toHaveValue('')
    expect(useStore.getState().doc.nodes[0].data.config.limit).toBeUndefined()
    expect(screen.queryByText('Clear to leave this value unset.')).toBeNull()

    fireEvent.change(count, { target: { value: '' } })
    fireEvent.blur(count)
    expect(count).toHaveValue('')
    expect(screen.getByRole('alert')).toHaveTextContent('Count is required')
    expect(useStore.getState().doc.nodes[0].data.config.count).toBe(7)
  })

  it('reports a required field that loads without a value', () => {
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: state.doc.nodes.map((node) => ({
          ...node,
          data: { ...node.data, config: { limit: 10 } },
        })),
      },
    }))

    render(<NodeParamFields nodeId="plugin" />)
    expect(screen.getByRole('alert')).toHaveTextContent('Count is required')
  })

  it('reports a loaded empty optional numeric value as an invalid persisted type', () => {
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: state.doc.nodes.map((node) => ({
          ...node,
          data: { ...node.data, config: { count: 7, ratio: 0.5, limit: '' } },
        })),
      },
    }))

    render(<NodeParamFields nodeId="plugin" />)
    expect(screen.getByRole('alert')).toHaveTextContent('Limit must be a complete safe integer')
    expect(nodeInvalidReason(useStore.getState().doc.nodes[0]))
      .toBe('Limit must be a complete safe integer')
  })

  it('reports a loaded empty required numeric value even when it has a default', () => {
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: state.doc.nodes.map((node) => ({
          ...node,
          data: { ...node.data, config: { count: 7, ratio: '', limit: 10 } },
        })),
      },
    }))

    render(<NodeParamFields nodeId="plugin" />)
    expect(screen.getByRole('alert')).toHaveTextContent('Ratio must be a finite number')
    expect(nodeInvalidReason(useStore.getState().doc.nodes[0]))
      .toBe('Ratio must be a finite number')
  })
})
