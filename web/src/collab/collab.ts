import { useStore } from '../store/graph'

// Realtime presence over the kernel's per-canvas collab room (/ws/collab/{id}). This first version
// carries PRESENCE (who's here + live cursors); the same relay also carries doc messages, so live
// co-editing / a Yjs CRDT binding is the next step. One connection per open canvas.

const COLORS = ['#e5484d', '#0091ff', '#30a46c', '#f76b15', '#8e4ec6', '#e5b100', '#d6409f', '#12a594']
const clientId = Math.random().toString(36).slice(2, 10)
const color = COLORS[Math.floor(Math.random() * COLORS.length)]

let ws: WebSocket | null = null
let roomId = ''
let cursorTimer: ReturnType<typeof setTimeout> | null = null

function myName(): string {
  return useStore.getState().currentUser?.name ?? 'Someone'
}

function send(msg: Record<string, unknown>): void {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ ...msg, clientId }))
}

export function connectCollab(canvasId: string): void {
  if (!canvasId || (roomId === canvasId && ws && ws.readyState <= WebSocket.OPEN)) return
  disconnectCollab()
  roomId = canvasId
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws/collab/${encodeURIComponent(canvasId)}`)
  } catch {
    return
  }
  ws.onopen = () => send({ type: 'presence', name: myName(), color })  // announce arrival
  ws.onmessage = (ev) => {
    let msg: any
    try { msg = JSON.parse(ev.data) } catch { return }
    if (!msg || msg.clientId === clientId) return
    const st = useStore.getState()
    if (msg.type === 'leave') st.dropPeer(msg.clientId)
    else if (msg.type === 'presence') {
      const known = !!st.peers[msg.clientId]
      st.setPeer(msg.clientId, { name: msg.name ?? 'Someone', color: msg.color ?? '#888', cursor: msg.cursor })
      if (!known) send({ type: 'presence', name: myName(), color })  // greet the newcomer so they see us too
    }
  }
  ws.onclose = () => { if (roomId === canvasId) ws = null }
}

export function disconnectCollab(): void {
  roomId = ''
  useStore.getState().clearPeers()
  if (ws) { try { ws.close() } catch { /* ignore */ } ws = null }
}

// throttled cursor broadcast (flow coordinates — each peer maps to its own screen via React Flow)
export function sendCursor(x: number, y: number): void {
  if (cursorTimer) return
  cursorTimer = setTimeout(() => { cursorTimer = null }, 50)
  send({ type: 'presence', name: myName(), color, cursor: { x, y } })
}
