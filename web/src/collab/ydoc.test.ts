import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as Y from 'yjs'
import { api } from '../api/client'
import { useStore } from '../store/graph'
import {
  completeYSync, encodeYStateVector, hydrateIfEmpty, startYSync, stopYSync, YSyncReplica,
} from './ydoc'

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

  afterEach(() => {
    stopYSync()
    vi.restoreAllMocks()
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

  it('revalidates a ready peer latest-result claim after directed state-vector sync', async () => {
    const local = {
      ...doc,
      nodes: [{
        ...doc.nodes[0],
        data: { ...doc.nodes[0].data, status: 'stale' as const },
      }],
    }
    useStore.setState({ doc: local })
    const currentResults = vi.spyOn(api, 'currentResults').mockResolvedValue({
      latestNodeIds: [], failedNodeIds: [], staleNodeIds: ['source'], unknownNodeIds: [], results: [],
    })
    const authority = new YSyncReplica()
    const peerNode = new Y.Map<unknown>()
    peerNode.set('type', 'source')
    peerNode.set('x', 0)
    peerNode.set('y', 0)
    peerNode.set('parentId', null)
    peerNode.set('dataJson', JSON.stringify({
      ...doc.nodes[0].data,
      title: 'Peer source',
      status: 'latest',
      lastRun: { rows: 4, ms: 12, placement: 'local' },
    }))
    authority.doc.getMap<Y.Map<unknown>>('nodes').set('source', peerNode)
    authority.doc.getMap('meta').set('name', 'Peer document')
    authority.markSeedReady()
    startYSync(() => undefined)

    const reply = authority.encodeState(encodeYStateVector())
    expect(reply).not.toBeNull()
    completeYSync(reply!)

    expect(useStore.getState().doc.name).toBe('Peer document')
    expect(useStore.getState().doc.nodes[0].data).toMatchObject({
      title: 'Peer source', status: 'checking',
      lastRun: { rows: 4, ms: 12, placement: 'local' },
    })
    await vi.waitFor(() => expect(useStore.getState().doc.nodes[0].data.status).toBe('stale'))
    expect(currentResults).toHaveBeenCalledOnce()
    authority.destroy()
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
