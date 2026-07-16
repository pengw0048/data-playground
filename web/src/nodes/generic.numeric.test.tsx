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

import { NodeParamFields, registerGenericNodes } from './generic'
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
        { name: 'ratio', label: 'Ratio', type: 'float', default: 0.5 },
      ], canBypass: false, previewable: true, blurb: '',
    }])
    useStore.setState({
      canvasRole: 'owner', numericParamDrafts: {}, saved: true,
      doc: { id: 'numeric', name: 'numeric', version: 1, edges: [], nodes: [{
        id: 'plugin', type: 'plugin-numeric-field-contract', position: { x: 0, y: 0 },
        data: { title: 'numeric', status: 'draft', config: { count: 7 } },
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
    const [count, ratio] = screen.getAllByRole('textbox') as HTMLInputElement[]

    expect(screen.getByText('Clear to use the declared default (0.5).')).toBeVisible()
    fireEvent.change(ratio, { target: { value: '' } })
    fireEvent.blur(ratio)
    expect(ratio).toHaveValue('0.5')
    expect(useStore.getState().doc.nodes[0].data.config.ratio).toBe(0.5)

    fireEvent.change(count, { target: { value: '' } })
    fireEvent.blur(count)
    expect(count).toHaveValue('')
    expect(screen.getByRole('alert')).toHaveTextContent('Count is required')
    expect(useStore.getState().doc.nodes[0].data.config.count).toBe(7)
  })
})
