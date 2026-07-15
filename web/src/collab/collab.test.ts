import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import * as Y from 'yjs'
import { useStore } from '../store/graph'
import type { CanvasDoc } from '../types/graph'
import { connectCollab, disconnectCollab } from './collab'

class MockWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  static instances: MockWebSocket[] = []

  readyState = MockWebSocket.CONNECTING
  sent: string[] = []
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null

  constructor(_url: string) { MockWebSocket.instances.push(this) }

  send(data: string): void { this.sent.push(data) }
  close(): void {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.(new CloseEvent('close'))
  }
  open(): void {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }
  receive(message: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(message) } as MessageEvent)
  }
}

const staleDoc = (): CanvasDoc => ({
  id: 'collab-test', version: 1, name: 'Stale local snapshot', edges: [], requirements: [],
  nodes: [{
    id: 'stale', type: 'source', position: { x: 0, y: 0 },
    data: { title: 'Stale', status: 'draft', config: {}, history: [] },
  }],
})

function peerUpdate(): string {
  const peer = new Y.Doc()
  const node = new Y.Map<unknown>()
  node.set('type', 'source')
  node.set('x', 1)
  node.set('y', 2)
  node.set('parentId', null)
  node.set('dataJson', JSON.stringify({ title: 'Peer', status: 'draft', config: {}, history: [] }))
  peer.getMap<Y.Map<unknown>>('nodes').set('peer', node)
  return btoa(String.fromCharCode(...Y.encodeStateAsUpdate(peer)))
}

describe('collaboration sync replies', () => {
  const originalWebSocket = globalThis.WebSocket

  beforeEach(() => {
    MockWebSocket.instances = []
    globalThis.WebSocket = MockWebSocket as unknown as typeof WebSocket
    useStore.setState({ doc: staleDoc() })
  })

  afterEach(() => {
    disconnectCollab()
    globalThis.WebSocket = originalWebSocket
  })

  it('does not release a stale snapshot for a sync reply addressed to another joiner', () => {
    connectCollab('canvas')
    const socket = MockWebSocket.instances[0]
    socket.open()

    const sent = () => socket.sent.map((frame) => JSON.parse(frame) as Record<string, unknown>)
    const request = sent().find((frame) => frame.type === 'ysync')!
    expect(request.requestId).toEqual(expect.any(String))
    expect(request.clientId).toEqual(expect.any(String))

    socket.receive({ type: 'room-state', peerCount: 2 })
    const update = peerUpdate()
    socket.receive({
      type: 'yjs', clientId: 'peer-a', sync: true, update,
      replyTo: request.requestId, targetId: 'other-joiner',
    })
    useStore.setState({ doc: { ...useStore.getState().doc, name: 'Must not send' } })

    expect(useStore.getState().doc.nodes.map((node) => node.id)).toEqual(['stale'])
    expect(sent().filter((frame) => frame.type === 'yjs')).toEqual([])

    socket.receive({
      type: 'yjs', clientId: 'peer-a', sync: true, update,
      replyTo: request.requestId, targetId: request.clientId,
    })
    expect(useStore.getState().doc.nodes.map((node) => node.id)).toEqual(['peer'])

    useStore.setState({ doc: { ...useStore.getState().doc, name: 'Sent after matching sync' } })
    expect(sent().filter((frame) => frame.type === 'yjs')).toHaveLength(1)
  })
})
