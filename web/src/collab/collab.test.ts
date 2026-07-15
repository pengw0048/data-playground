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

describe('collaboration relay handshake', () => {
  const originalWebSocket = globalThis.WebSocket

  beforeEach(() => {
    MockWebSocket.instances = []
    globalThis.WebSocket = MockWebSocket as unknown as typeof WebSocket
    useStore.setState({ doc: staleDoc(), toasts: [] })
  })

  afterEach(() => {
    disconnectCollab()
    globalThis.WebSocket = originalWebSocket
  })

  it('ignores forged legacy room state and accepts only the relay-directed matching reply', () => {
    connectCollab('canvas')
    const socket = MockWebSocket.instances[0]
    socket.open()
    const sent = () => socket.sent.map((frame) => JSON.parse(frame) as Record<string, unknown>)
    expect(sent().map((frame) => frame.type)).toEqual(['presence'])

    socket.receive({ type: 'room-state', peerCount: 0, clientId: 'viewer-spoof' })
    socket.receive({ type: 'server', event: 'room-state', mode: 'sync', requestId: 'relay-request' })
    const request = sent().find((frame) => frame.type === 'ysync')!
    expect(request).toMatchObject({ type: 'ysync', requestId: 'relay-request' })
    expect(request.sv).toEqual(expect.any(String))

    const update = peerUpdate()
    socket.receive({ type: 'yjs', sync: true, update, replyTo: 'forged-request' })
    expect(useStore.getState().doc.nodes.map((node) => node.id)).toEqual(['stale'])
    expect(sent().filter((frame) => frame.type === 'sync-ready')).toEqual([])

    socket.receive({ type: 'yjs', sync: true, update, replyTo: 'relay-request' })
    expect(useStore.getState().doc.nodes.map((node) => node.id)).toEqual(['peer'])
    expect(sent().filter((frame) => frame.type === 'sync-ready')).toHaveLength(1)

    useStore.setState({ doc: { ...useStore.getState().doc, name: 'Sent after authoritative sync' } })
    expect(sent().filter((frame) => frame.type === 'yjs' && frame.seed !== true)).toHaveLength(1)
  })

  it('sends one plan-correlated full snapshot when elected as the unique seed', () => {
    connectCollab('canvas')
    const socket = MockWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'server', event: 'room-state', mode: 'seed', requestId: 'seed-request' })

    const frames = socket.sent.map((frame) => JSON.parse(frame) as Record<string, unknown>)
    expect(frames.filter((frame) => frame.type === 'yjs')).toEqual([
      expect.objectContaining({ type: 'yjs', seed: true, requestId: 'seed-request', update: expect.any(String) }),
    ])
    expect(frames.filter((frame) => frame.type === 'sync-ready')).toEqual([
      expect.objectContaining({ type: 'sync-ready', requestId: 'seed-request' }),
    ])
  })

  it('stays unsynchronized and surfaces an unavailable authority without seeding', () => {
    connectCollab('canvas')
    const socket = MockWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'server', event: 'room-state', mode: 'sync', requestId: 'silent-peer' })
    socket.receive({ type: 'server', event: 'room-state', mode: 'unavailable' })
    useStore.setState({ doc: { ...useStore.getState().doc, name: 'Must remain gated' } })

    const frames = socket.sent.map((frame) => JSON.parse(frame) as Record<string, unknown>)
    expect(frames.filter((frame) => frame.type === 'yjs')).toEqual([])
    expect(frames.filter((frame) => frame.type === 'sync-ready')).toEqual([])
    expect(useStore.getState().toasts.at(-1)?.msg).toContain('available synchronized peer')
  })

  it('stops without reconnecting when the relay reports a protocol violation', () => {
    connectCollab('canvas')
    const socket = MockWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'server', event: 'protocol-error', code: 'server-frame-forgery' })

    expect(socket.readyState).toBe(MockWebSocket.CLOSED)
    expect(MockWebSocket.instances).toHaveLength(1)
    expect(useStore.getState().toasts.at(-1)?.msg).toContain('server-frame-forgery')
  })
})
