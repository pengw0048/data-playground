import { beforeEach, describe, expect, it } from 'vitest'
import * as Y from 'yjs'
import { useStore } from '../store/graph'
import { completeYSync, hydrateIfEmpty, startYSync, stopYSync, YSyncReplica } from './ydoc'

const doc = {
  id: 'collab-test', version: 1, name: 'Collab test', edges: [], requirements: [],
  nodes: [{
    id: 'source', type: 'source', position: { x: 0, y: 0 },
    data: { title: 'Source', status: 'draft' as const, config: {}, history: [] },
  }],
}

const b64 = (update: Uint8Array): string => btoa(String.fromCharCode(...update))

describe('Yjs hydration decisions', () => {
  beforeEach(() => {
    stopYSync()
    useStore.setState({ doc })
  })

  it('seeds only after the relay explicitly elects this replica', () => {
    const sent: Uint8Array[] = []
    startYSync((update) => sent.push(update))
    expect(sent).toEqual([])

    hydrateIfEmpty()
    expect(sent).toHaveLength(1)
    expect(sent[0].byteLength).toBeGreaterThan(0)
  })

  it('unblocks local edits after an authoritative empty sync reply', () => {
    const sent: Uint8Array[] = []
    startYSync((update) => sent.push(update))

    completeYSync(b64(Y.encodeStateAsUpdate(new Y.Doc())))
    useStore.setState({ doc: { ...doc, name: 'Edited after empty sync' } })

    expect(sent).toHaveLength(1)
    expect(sent[0].byteLength).toBeGreaterThan(0)
  })

  it('keeps the saved compute target when syncing with a legacy replica', () => {
    useStore.setState({ doc: { ...doc, executionBackend: 'ray-data' } })
    const legacy = new Y.Doc()
    legacy.getMap('meta').set('name', 'Legacy peer')
    startYSync(() => undefined)

    completeYSync(b64(Y.encodeStateAsUpdate(legacy)))

    expect(useStore.getState().doc.executionBackend).toBe('ray-data')
  })

  it('writes Automatic as an explicit collaborative value', () => {
    useStore.setState({ doc: { ...doc, executionBackend: 'ray-data' } })
    const sent: Uint8Array[] = []
    startYSync((update) => sent.push(update))
    hydrateIfEmpty()
    useStore.setState({ doc: { ...doc, executionBackend: undefined } })

    const replica = new Y.Doc()
    for (const update of sent) Y.applyUpdate(replica, update)
    expect(replica.getMap('meta').has('executionBackend')).toBe(true)
    expect(replica.getMap('meta').get('executionBackend')).toBeNull()
    replica.destroy()
  })

  it('fails closed a peer result claim when the peer changes the execution target', () => {
    useStore.setState({
      doc: {
        ...doc,
        nodes: [{
          ...doc.nodes[0],
          data: {
            ...doc.nodes[0].data,
            status: 'latest',
            lastRun: { rows: 4, ms: 12, placement: 'local' as const },
          },
        }],
      },
    })
    const peer = new Y.Doc()
    peer.getMap('meta').set('name', doc.name)
    peer.getMap('meta').set('executionBackend', 'local-subprocess')
    const node = new Y.Map<unknown>()
    node.set('type', 'source')
    node.set('x', 0)
    node.set('y', 0)
    node.set('parentId', null)
    node.set('dataJson', JSON.stringify({
      ...useStore.getState().doc.nodes[0].data,
      status: 'latest',
    }))
    peer.getMap<Y.Map<unknown>>('nodes').set('source', node)
    startYSync(() => undefined)

    completeYSync(b64(Y.encodeStateAsUpdate(peer)))

    expect(useStore.getState().doc.executionBackend).toBe('local-subprocess')
    expect(useStore.getState().doc.nodes[0].data.status).toBe('stale')
    expect(useStore.getState().doc.nodes[0].data.lastRun).toEqual({
      rows: 4, ms: 12, placement: 'local',
    })
    peer.destroy()
  })

  it('round-trips the Canvas result-retention override', () => {
    useStore.setState({ doc: { ...doc, resultRetention: { history: 'recent' } } })
    const sent: Uint8Array[] = []
    startYSync((update) => sent.push(update))
    hydrateIfEmpty()

    const replica = new Y.Doc()
    for (const update of sent) Y.applyUpdate(replica, update)
    expect(JSON.parse(String(replica.getMap('meta').get('resultRetention')))).toEqual({
      history: 'recent',
    })
    replica.destroy()
  })
})

describe('YSyncReplica readiness', () => {
  it('keeps two simultaneous joiners non-authoritative until a slow ready peer answers both', () => {
    const authority = new YSyncReplica()
    authority.doc.getMap<string>('meta').set('unpersistedRevision', 'newer-than-db')
    authority.markSeedReady()

    const joinerA = new YSyncReplica()
    const joinerB = new YSyncReplica()
    const vectorA = joinerA.encodeStateVector()
    const vectorB = joinerB.encodeStateVector()

    // The authoritative peer is deliberately "slow": before its replies arrive, neither empty
    // joiner can answer the other or claim readiness. This is the simultaneous-join regression.
    expect(joinerA.isReady()).toBe(false)
    expect(joinerB.isReady()).toBe(false)
    expect(joinerA.encodeState(vectorB)).toBeNull()
    expect(joinerB.encodeState(vectorA)).toBeNull()
    expect(joinerA.doc.getMap('meta').size).toBe(0)
    expect(joinerB.doc.getMap('meta').size).toBe(0)

    const replyA = authority.encodeState(vectorA)
    const replyB = authority.encodeState(vectorB)
    expect(replyA).not.toBeNull()
    expect(replyB).not.toBeNull()
    joinerA.completeSync(replyA!)
    joinerB.completeSync(replyB!)

    for (const joiner of [joinerA, joinerB]) {
      expect(joiner.isReady()).toBe(true)
      expect(joiner.doc.getMap('meta').get('unpersistedRevision')).toBe('newer-than-db')
    }
    authority.destroy(); joinerA.destroy(); joinerB.destroy()
  })
})
