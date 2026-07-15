import { useStore } from '../store/graph'
import {
  applyYUpdate, completeYSync, encodeYState, encodeYStateVector, hydrateIfEmpty,
  isYSyncReady, startYSync, stopYSync, yUpdateB64,
} from './ydoc'

// Realtime collaboration over the kernel's per-canvas room (/ws/collab/{id}): PRESENCE (who's here +
// live cursors) AND live co-editing (a Yjs CRDT — see ydoc.ts). The relay owns the bootstrap state
// machine: one synchronized writer is selected for each directed sync, while a unique writer seeds an
// empty room. Clients never infer authority from peer counts or peer-supplied control frames.

const COLORS = ['#e5484d', '#0091ff', '#30a46c', '#f76b15', '#8e4ec6', '#e5b100', '#d6409f', '#12a594']
const clientId = Math.random().toString(36).slice(2, 10)
const color = COLORS[Math.floor(Math.random() * COLORS.length)]

let ws: WebSocket | null = null
let roomId = ''
let cursorTimer: ReturnType<typeof setTimeout> | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let latestYSyncRequestId: string | null = null
let completedYSyncRequestId: string | null = null
let unavailableNoticeShown = false
let pendingYUpdates: string[] = []
const MAX_PENDING_Y_UPDATES = 256

function myName(): string {
  return useStore.getState().currentUser?.name ?? 'Someone'
}

function send(msg: Record<string, unknown>): void {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ ...msg, clientId }))
}

function requestYSync(requestId: string): boolean {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false
  try {
    send({ type: 'ysync', requestId, sv: encodeYStateVector() })
  } catch {
    return false
  }
  latestYSyncRequestId = requestId
  return true
}

function resetHandshake(): void {
  latestYSyncRequestId = null
  completedYSyncRequestId = null
  pendingYUpdates = []
}

function startReplica(): void {
  stopYSync()
  resetHandshake()
  startYSync((u) => {
    // Hydration itself emits a Yjs update before the seed is marked ready. Suppress that implicit
    // frame; the explicit plan-correlated seed snapshot below is the only legal bootstrap frame.
    if (isYSyncReady()) send({ type: 'yjs', update: yUpdateB64(u) })
  })
}

function drainPendingYUpdates(): void {
  const updates = pendingYUpdates
  pendingYUpdates = []
  for (const update of updates) applyYUpdate(update)
}

function stopAfterProtocolError(sock: WebSocket, code: unknown): void {
  if (ws !== sock) return
  roomId = ''
  resetHandshake()
  ws = null
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  useStore.getState().clearPeers()
  useStore.getState().pushToast(
    `Collaboration stopped because the relay rejected a protocol frame${typeof code === 'string' ? ` (${code})` : ''}`,
    'error',
  )
  stopYSync()
  try { sock.onclose = null; sock.close() } catch { /* already closed by the relay */ }
}

function scheduleReconnect(canvasId: string): void {
  if (roomId !== canvasId || reconnectTimer) return
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    if (roomId !== canvasId) return
    startReplica()
    openSocket(canvasId)
  }, 1500)
}

function openSocket(canvasId: string): void {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  let sock: WebSocket
  try {
    sock = new WebSocket(`${proto}://${location.host}/ws/collab/${encodeURIComponent(canvasId)}`)
  } catch {
    stopYSync()
    resetHandshake()
    scheduleReconnect(canvasId)
    return
  }
  ws = sock
  sock.onopen = () => {
    send({ type: 'presence', name: myName(), color })
  }
  sock.onmessage = (ev) => {
    if (ws !== sock) return
    let msg: any
    try { msg = JSON.parse(ev.data) } catch { return }
    if (!msg || typeof msg !== 'object') return
    const st = useStore.getState()

    // Only this envelope carries relay control events. The server rejects client-supplied envelopes,
    // and legacy top-level room-state/leave/external-edit frames are deliberately ignored below.
    if (msg.type === 'server') {
      if (msg.event === 'protocol-error') {
        stopAfterProtocolError(sock, msg.code)
        return
      }
      if (msg.event === 'room-state') {
        if (msg.mode === 'seed' && typeof msg.requestId === 'string') {
          if (completedYSyncRequestId === msg.requestId) return
          latestYSyncRequestId = msg.requestId
          // Persisted state is the base. Buffered deltas are applied only after hydration so a
          // pre-baseline update can never make the document look non-empty and suppress recovery.
          hydrateIfEmpty()
          drainPendingYUpdates()
          const update = encodeYState()
          if (update === null) return
          // The explicit full snapshot lets the relay prove the elected seed initialized its replica;
          // it is not broadcast. Ordinary updates remain gated until sync-ready is acknowledged.
          send({ type: 'yjs', seed: true, requestId: msg.requestId, update })
          send({ type: 'sync-ready', requestId: msg.requestId })
          completedYSyncRequestId = msg.requestId
          return
        }
        if (msg.mode === 'sync' && typeof msg.requestId === 'string') {
          if (completedYSyncRequestId !== msg.requestId && latestYSyncRequestId !== msg.requestId) {
            if (!requestYSync(msg.requestId)) {
              // A request that never entered the socket queue must not consume the relay's five-second
              // lease. Closing starts a fresh authenticated plan through the normal reconnect path.
              try { sock.close() } catch {
                if (ws === sock) {
                  ws = null
                  useStore.getState().clearPeers()
                  stopYSync()
                  resetHandshake()
                  scheduleReconnect(canvasId)
                }
              }
            }
          }
          return
        }
        if (msg.mode === 'wait' || msg.mode === 'ready') {
          latestYSyncRequestId = null
          return
        }
        if (msg.mode === 'unavailable') {
          latestYSyncRequestId = null
          if (!unavailableNoticeShown) {
            unavailableNoticeShown = true
            st.pushToast('Live collaboration is waiting for an available synchronized peer', 'info')
          }
          return
        }
        return
      }
      if (msg.event === 'external-edit' && typeof msg.canvasId === 'string') {
        st.applyExternalEdit(msg.canvasId)
        return
      }
      if (msg.event === 'leave' && typeof msg.clientId === 'string') {
        st.dropPeer(msg.clientId)
        return
      }
      return
    }

    if (msg.clientId === clientId) return
    if (msg.type === 'yjs' && typeof msg.update === 'string') {
      if (msg.sync === true) {
        if (typeof msg.replyTo !== 'string' || msg.replyTo !== latestYSyncRequestId) return
        completeYSync(msg.update)
        drainPendingYUpdates()
        send({ type: 'sync-ready', requestId: msg.replyTo })
        completedYSyncRequestId = msg.replyTo
        latestYSyncRequestId = null
        return
      }
      if (!isYSyncReady()) {
        if (pendingYUpdates.length >= MAX_PENDING_Y_UPDATES) {
          stopAfterProtocolError(sock, 'pre-sync-update-overflow')
          return
        }
        pendingYUpdates.push(msg.update)
        return
      }
      applyYUpdate(msg.update)
      return
    }
    // The relay sends a sync request only to its selected, synchronized writer. An unsynchronized
    // replica cannot produce a reply because encodeYState returns null until the readiness invariant
    // has been established.
    if (msg.type === 'ysync' && typeof msg.requestId === 'string' && typeof msg.sv === 'string') {
      const update = encodeYState(msg.sv)
      if (update !== null) send({ type: 'yjs', update, sync: true, replyTo: msg.requestId })
      return
    }
    if (msg.type === 'presence' && typeof msg.clientId === 'string') {
      const prev = st.peers[msg.clientId]
      st.setPeer(msg.clientId, {
        name: msg.name ?? prev?.name ?? 'Someone',
        color: msg.color ?? prev?.color ?? '#888',
        cursor: msg.cursor ?? prev?.cursor,
      })
      if (!prev) send({ type: 'presence', name: myName(), color })
    }
  }
  sock.onclose = (event) => {
    if (ws !== sock) return
    ws = null
    useStore.getState().clearPeers()
    stopYSync()
    resetHandshake()
    if (event.code === 1008) {
      roomId = ''
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
      useStore.getState().pushToast('Live collaboration access was revoked', 'error')
      return
    }
    if (roomId === canvasId) {
      scheduleReconnect(canvasId)
    }
  }
}

export function connectCollab(canvasId: string): void {
  if (!canvasId || (roomId === canvasId && ws && ws.readyState <= WebSocket.OPEN)) return
  disconnectCollab()
  roomId = canvasId
  unavailableNoticeShown = false
  startReplica()
  openSocket(canvasId)
}

export function disconnectCollab(): void {
  roomId = ''
  resetHandshake()
  unavailableNoticeShown = false
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  useStore.getState().clearPeers()
  stopYSync()
  if (ws) {
    const s = ws
    ws = null
    try { s.onclose = null; s.close() } catch { /* ignore */ }
  }
}

// Throttled cursor broadcast (flow coordinates — each peer maps to its own screen via React Flow).
export function sendCursor(x: number, y: number): void {
  if (cursorTimer) return
  cursorTimer = setTimeout(() => { cursorTimer = null }, 50)
  send({ type: 'presence', name: myName(), color, cursor: { x, y } })
}
