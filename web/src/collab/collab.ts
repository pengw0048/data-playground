import { useStore } from '../store/graph'
import { startYSync, stopYSync, applyYUpdate, encodeYState, encodeYStateVector, yUpdateB64, hydrateFromRoomState, markYSyncReady } from './ydoc'

// Realtime collaboration over the kernel's per-canvas room (/ws/collab/{id}): PRESENCE (who's here +
// live cursors) AND live co-editing (a Yjs CRDT — see ydoc.ts). One connection per open canvas, with
// reconnect. Doc edits merge; on (re)connect we run a Yjs sync handshake so late joiners catch up.

const COLORS = ['#e5484d', '#0091ff', '#30a46c', '#f76b15', '#8e4ec6', '#e5b100', '#d6409f', '#12a594']
const clientId = Math.random().toString(36).slice(2, 10)
const color = COLORS[Math.floor(Math.random() * COLORS.length)]

let ws: WebSocket | null = null
let roomId = ''
let cursorTimer: ReturnType<typeof setTimeout> | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let latestYSyncRequestId: string | null = null
let ysyncRequestSequence = 0

function myName(): string {
  return useStore.getState().currentUser?.name ?? 'Someone'
}

function send(msg: Record<string, unknown>): void {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ ...msg, clientId }))
}

function requestYSync(): void {
  latestYSyncRequestId = `${clientId}:${++ysyncRequestSequence}`
  send({ type: 'ysync', requestId: latestYSyncRequestId, sv: encodeYStateVector() })
}

function openSocket(canvasId: string): void {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  let sock: WebSocket
  try {
    sock = new WebSocket(`${proto}://${location.host}/ws/collab/${encodeURIComponent(canvasId)}`)
  } catch {
    return
  }
  ws = sock
  sock.onopen = () => {
    send({ type: 'presence', name: myName(), color })   // announce arrival
    requestYSync()  // ask peers for edits we're missing (CRDT sync step 1)
  }
  sock.onmessage = (ev) => {
    let msg: any
    try { msg = JSON.parse(ev.data) } catch { return }
    if (!msg || msg.clientId === clientId) return
    const st = useStore.getState()
    if (msg.type === 'room-state' && Number.isInteger(msg.peerCount) && msg.peerCount >= 0) {
      hydrateFromRoomState(msg.peerCount)
      return
    }
    if (msg.type === 'yjs' && typeof msg.update === 'string') {
      // A sync response is broadcast by the relay, so only its intended requester may apply it or
      // become ready. Ordinary live Yjs edits remain room-wide and are handled below.
      if (msg.sync === true) {
        if (msg.targetId !== clientId || msg.replyTo !== latestYSyncRequestId) return
        applyYUpdate(msg.update)
        markYSyncReady()  // a matching empty Y.Doc reply still confirms this request was answered
        return
      }
      applyYUpdate(msg.update)
      return
    }
    // Every peer acknowledges ysync, including an empty document. `replyTo` + `targetId` correlate
    // that broadcast reply to the requester's latest state vector, so another joiner cannot unlock it.
    if (msg.type === 'ysync' && typeof msg.requestId === 'string' && typeof msg.clientId === 'string') {
      send({ type: 'yjs', update: encodeYState(msg.sv), sync: true, replyTo: msg.requestId, targetId: msg.clientId })
      return
    }
    if (msg.type === 'external-edit') { st.applyExternalEdit(msg.canvasId); return }  // an MCP agent edited this canvas out-of-band → refetch + apply live
    if (msg.type === 'leave') { st.dropPeer(msg.clientId); return }
    if (msg.type === 'presence') {
      const prev = st.peers[msg.clientId]
      st.setPeer(msg.clientId, {
        name: msg.name ?? prev?.name ?? 'Someone',
        color: msg.color ?? prev?.color ?? '#888',
        cursor: msg.cursor ?? prev?.cursor,  // a plain presence (no cursor) must not blank the cursor
      })
      if (!prev) { send({ type: 'presence', name: myName(), color }); requestYSync() }  // greet + resync
    }
  }
  sock.onclose = () => {
    if (ws !== sock) return  // a stale socket closed — don't disturb the current one
    ws = null
    if (roomId === canvasId) {  // unexpected drop while we still want this room → clear + retry
      useStore.getState().clearPeers()
      reconnectTimer = setTimeout(() => { if (roomId === canvasId) openSocket(canvasId) }, 1500)
    }
  }
}

export function connectCollab(canvasId: string): void {
  if (!canvasId || (roomId === canvasId && ws && ws.readyState <= WebSocket.OPEN)) return
  disconnectCollab()
  roomId = canvasId
  startYSync((u) => send({ type: 'yjs', update: yUpdateB64(u) }))  // CRDT bound to the store; edits go on the wire
  openSocket(canvasId)
}

export function disconnectCollab(): void {
  roomId = ''
  latestYSyncRequestId = null
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  useStore.getState().clearPeers()
  stopYSync()
  if (ws) {
    const s = ws
    ws = null
    try { s.onclose = null; s.close() } catch { /* ignore */ }  // deliberate close: don't trigger reconnect
  }
}

// throttled cursor broadcast (flow coordinates — each peer maps to its own screen via React Flow)
export function sendCursor(x: number, y: number): void {
  if (cursorTimer) return
  cursorTimer = setTimeout(() => { cursorTimer = null }, 50)
  send({ type: 'presence', name: myName(), color, cursor: { x, y } })
}
