import type { FinalConnectionState } from '@xyflow/react'
import { Position } from '@xyflow/react'
import { describe, expect, it } from 'vitest'
import { outputConnectionDrop } from './outputConnectionDrop'

const state = {
  isValid: null,
  fromNode: { id: 'source' }, fromHandle: { id: 'out', type: 'source', position: Position.Right },
  toNode: null, toHandle: null,
} as FinalConnectionState

function element(className: string) {
  const target = document.createElement('div')
  target.className = className
  return target
}

const pane = element('react-flow__pane')

describe('output connection released on blank Canvas', () => {
  it('retains the exact source port for a new connected step', () => {
    expect(outputConnectionDrop(state, pane, true, null)).toEqual({ nodeId: 'source', handleId: 'out' })
  })

  it('does not offer new nodes for valid, incompatible, or occupied target handles', () => {
    expect(outputConnectionDrop({ ...state, isValid: true }, pane, true, null)).toBeNull()
    expect(outputConnectionDrop({ ...state, isValid: false, toNode: { id: 'target' } } as FinalConnectionState,
      pane, true, null)).toBeNull()
    expect(outputConnectionDrop({ ...state, isValid: false, toHandle: { id: 'in' } } as FinalConnectionState,
      pane, true, null)).toBeNull()
  })

  it('does not interpret node bodies, existing wires, or toolbar controls as empty Canvas', () => {
    for (const target of [element('react-flow__node'), element('react-flow__edge'), element('toolbar'), null]) {
      expect(outputConnectionDrop(state, target, true, null)).toBeNull()
    }
  })

  it('leaves input gestures, reconnections, and read-only canvases unchanged', () => {
    expect(outputConnectionDrop({ ...state, fromHandle: { ...state.fromHandle, type: 'target' } } as FinalConnectionState,
      pane, true, null)).toBeNull()
    expect(outputConnectionDrop(state, pane, true, 'existing-edge')).toBeNull()
    expect(outputConnectionDrop(state, pane, false, null)).toBeNull()
  })
})
