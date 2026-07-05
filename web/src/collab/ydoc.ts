// Live co-editing via a Yjs CRDT. The canvas is mirrored into a Y.Doc (nodes/edges/meta) so two
// people editing at once MERGE instead of clobbering (the old last-write-wins autosave). The Y.Doc
// syncs over the existing per-canvas collab websocket; the Zustand store stays the app's source of
// truth and is bridged to the Y.Doc both ways, with an origin guard to avoid loops. Persistence is
// unchanged — the store still autosaves the canvas snapshot to the DB.
import * as Y from 'yjs'
import { useStore } from '../store/graph'
import type { CanvasDoc, CanvasNode, CanvasEdge } from '../types/graph'

let ydoc = new Y.Doc()
let applying = false        // a store change currently originates from Y → don't push it back
let active = false
let lastDoc: CanvasDoc | undefined
let broadcast: ((u: Uint8Array) => void) | null = null
let unsub: (() => void) | null = null

const b64 = {
  enc: (u: Uint8Array) => {
    let s = ''
    for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode(...u.subarray(i, i + 0x8000))  // chunk: avoid arg-count limit
    return btoa(s)
  },
  dec: (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0)),
}

function nodes() { return ydoc.getMap<Y.Map<unknown>>('nodes') }
function edges() { return ydoc.getMap<CanvasEdge>('edges') }
function meta() { return ydoc.getMap<unknown>('meta') }

// -- Y → CanvasDoc (rebuild the store doc from the shared state) -------------- //
function yToDoc(base: CanvasDoc): CanvasDoc {
  const ns: CanvasNode[] = []
  nodes().forEach((yn, id) => {
    ns.push({
      id, type: String(yn.get('type')),
      position: { x: Number(yn.get('x')) || 0, y: Number(yn.get('y')) || 0 },
      parentId: (yn.get('parentId') as string | null) ?? null,
      data: JSON.parse((yn.get('dataJson') as string) || '{}'),
    })
  })
  const es: CanvasEdge[] = []
  edges().forEach((e) => es.push(e))
  return { ...base, name: (meta().get('name') as string) ?? base.name, nodes: ns, edges: es }
}

// -- CanvasDoc → Y (diff the store doc into the shared state) ----------------- //
function pushDocToY(doc: CanvasDoc): void {
  ydoc.transact(() => {
    if (meta().get('name') !== doc.name) meta().set('name', doc.name)
    const nmap = nodes()
    const ids = new Set(doc.nodes.map((n) => n.id))
    nmap.forEach((_v, id) => { if (!ids.has(id)) nmap.delete(id) })
    for (const n of doc.nodes) {
      let yn = nmap.get(n.id)
      if (!yn) { yn = new Y.Map(); nmap.set(n.id, yn) }
      if (yn.get('type') !== n.type) yn.set('type', n.type)
      if (yn.get('x') !== n.position.x) yn.set('x', n.position.x)  // position is its own field, so a
      if (yn.get('y') !== n.position.y) yn.set('y', n.position.y)  // drag never clobbers a config edit
      const pid = n.parentId ?? null
      if ((yn.get('parentId') ?? null) !== pid) yn.set('parentId', pid)
      const dj = JSON.stringify(n.data)
      if (yn.get('dataJson') !== dj) yn.set('dataJson', dj)
    }
    const emap = edges()
    const eids = new Set(doc.edges.map((e) => e.id))
    emap.forEach((_v, id) => { if (!eids.has(id)) emap.delete(id) })
    for (const e of doc.edges) {
      if (JSON.stringify(emap.get(e.id)) !== JSON.stringify(e)) emap.set(e.id, e)
    }
  }, 'store')
}

/** Start CRDT sync for a canvas. `send` broadcasts a binary Y update to the room. Returns handlers
 * the transport calls for incoming messages. Seeds the shared doc from the store's current doc. */
export function startYSync(send: (u: Uint8Array) => void): void {
  ydoc = new Y.Doc()
  broadcast = send
  active = true
  const doc0 = useStore.getState().doc
  ydoc.transact(() => pushDocToY(doc0), 'hydrate')  // seed Y from the loaded canvas (does NOT broadcast)

  ydoc.on('update', (u: Uint8Array, origin) => {
    if (origin !== 'store') {          // remote edit (or local hydrate) → reflect into the store
      applying = true
      try { useStore.setState({ doc: yToDoc(useStore.getState().doc) }) } finally { applying = false }
    }
    if (origin === 'store' && broadcast) broadcast(u)  // only genuine local edits go on the wire
  })

  lastDoc = useStore.getState().doc
  unsub = useStore.subscribe((s) => {
    if (s.doc === lastDoc) return
    lastDoc = s.doc
    if (applying || !active) return    // this change came from Y → don't echo it back
    pushDocToY(s.doc)
  })
}

export function stopYSync(): void {
  active = false
  unsub?.(); unsub = null
  broadcast = null
  ydoc.destroy()
  ydoc = new Y.Doc()
}

/** A peer sent a binary Y update — merge it (marks origin 'remote' so it flows into the store). */
export function applyYUpdate(update: string): void {
  if (!active) return
  Y.applyUpdate(ydoc, b64.dec(update), 'remote')
}

/** A peer joined and asked for state (their state vector) — reply with everything they're missing. */
export function encodeYState(theirStateVector?: string): string {
  const sv = theirStateVector ? b64.dec(theirStateVector) : undefined
  return b64.enc(Y.encodeStateAsUpdate(ydoc, sv))
}

/** Our state vector, to ask peers for what we're missing when we join. */
export function encodeYStateVector(): string {
  return b64.enc(Y.encodeStateVector(ydoc))
}

export const yUpdateB64 = b64.enc
