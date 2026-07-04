import { useStore } from '../store/graph'

// Realtime presence over the kernel's per-canvas collab room (/ws/collab/{id}). This first version
// carries PRESENCE (who's here + live cursors); the same relay also carries doc messages, so live
// co-editing / a Yjs CRDT binding is the next step. One connection per open canvas, with reconnect.

const COLORS = ['#e5484d', '#0091ff', '#30a46c', '#f76b15', '#8e4ec6', '#e5b100', '#d6409f', '#12a594']
const clientId = Math.random().toString(36).slice(2, 10)
const color = COLORS[Math.floor(Math.random() * COLORS.length)]

let ws: WebSocket | null = null
let roomId = ''
let cursorTimer: ReturnType<typeof setTimeout> | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null

function myName(): string {
  return useStore.getState().currentUser?.name ?? 'Someone'
}

function send(msg: Record<string, unknown>): void {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ ...msg, clientId }))
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
  sock.onopen = () => send({ type: 'presence', name: myName(), color })  // announce arrival
  sock.onmessage = (ev) => {
    let msg: any
    try { msg = JSON.parse(ev.data) } catch { return }
    if (!msg || msg.clientId === clientId) return
    const st = useStore.getState()
    if (msg.type === 'leave') { st.dropPeer(msg.clientId); return }
    if (msg.type === 'presence') {
      const prev = st.peers[msg.clientId]
      st.setPeer(msg.clientId, {
        name: msg.name ?? prev?.name ?? 'Someone',
        color: msg.color ?? prev?.color ?? '#888',
        cursor: msg.cursor ?? prev?.cursor,  // a plain presence (no cursor) must not blank the cursor
      })
      if (!prev) send({ type: 'presence', name: myName(), color })  // greet the newcomer so they see us
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
  openSocket(canvasId)
}

export function disconnectCollab(): void {
  roomId = ''
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  useStore.getState().clearPeers()
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
