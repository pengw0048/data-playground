import { create } from 'zustand'
import type { WireType } from '../theme/tokens'
import type {
  CanvasDoc, CanvasEdge, CanvasNode, NodeConfig, NodeData, NodeStatus, NodeVersion,
} from '../types/graph'
import type {
  CatalogTable, KernelInfo, ProcessorDescriptor, RunEstimate, RunStatus, SampleResult,
} from '../types/api'
import { getSpec } from '../nodes/registry'
import { registerGenericNodes, nodeInvalidReason } from '../nodes/generic'
import type { SchemaMap } from '../nodes/schema'
import { parseHash } from '../router'
import { api, KernelError, setApiUser, type AgentBackendNode, type AgentBackendEdge, type DpUser, type CanvasFile } from '../api/client'

export type PanelKind = 'data' | 'run' | 'history' | 'lineage' | 'section'

const LS_KEY = 'dp-canvas'       // offline cache of the open doc
const USER_KEY = 'dp-user'       // last-selected user id
const OPEN_KEY = (uid: string) => `dp-open-${uid}`  // last-opened file per user

let _seq = 0
let _cfgEdit = { id: '', t: 0 } // coalesces param-edit undo checkpoints

/** A canvas position near `base` that doesn't overlap any existing node (so added nodes never stack). */
export function freePosition(nodes: CanvasNode[], base: { x: number; y: number }): { x: number; y: number } {
  const W = 280, H = 180
  const clash = (x: number, y: number) => nodes.some((n) => Math.abs(n.position.x - x) < W && Math.abs(n.position.y - y) < H)
  if (!clash(base.x, base.y)) return base
  const dirs = [[1, 0], [0, 1], [1, 1], [-1, 0], [-1, 1], [0, -1], [1, -1], [-1, -1]]
  for (let r = 1; r < 50; r++) {
    for (const [dx, dy] of dirs) {
      const x = base.x + dx * W * r * 0.75, y = base.y + dy * H * r * 0.9
      if (!clash(x, y)) return { x, y }
    }
  }
  return base
}

/** Whether a node can run/preview: it (or some ancestor) is a source with a configured uri —
 * AND nothing in its upstream chain (including itself) is disabled (disable turns off downstream). */
export function nodeRunnable(doc: CanvasDoc, id: string): boolean {
  if (isDisabled(doc, id)) return false
  const seen = new Set<string>()
  const walk = (nid: string): boolean => {
    if (seen.has(nid)) return false
    seen.add(nid)
    const n = doc.nodes.find((x) => x.id === nid)
    if (!n) return false
    if (n.type === 'source') return !!n.data.config.uri
    return doc.edges.filter((e) => e.target === nid).map((e) => e.source).some(walk)
  }
  return walk(id)
}

/** A node is disabled if it, or ANY of its upstream ancestors, is flagged disabled — disabling a
 * node turns off everything downstream of it (the whole branch stops), mirroring ComfyUI. */
export function isDisabled(doc: CanvasDoc, id: string): boolean {
  const seen = new Set<string>()
  const walk = (nid: string): boolean => {
    if (seen.has(nid)) return false
    seen.add(nid)
    const n = doc.nodes.find((x) => x.id === nid)
    if (!n) return false
    if (n.data.disabled) return true
    return doc.edges.filter((e) => e.target === nid).map((e) => e.source).some(walk)
  }
  return walk(id)
}

export function newId(kind: string): string {
  _seq += 1
  return `${kind}-${_seq}-${Math.floor(performance.now() % 100000)}`
}

interface PreviewState { loading?: boolean; result?: SampleResult; error?: string; offset?: number }
interface RunState {
  estimate?: RunEstimate
  status?: RunStatus
  phase: 'idle' | 'estimating' | 'estimated' | 'confirm' | 'running' | 'done' | 'failed'
  error?: string
}

export interface AgentMsg { role: 'user' | 'agent'; text: string; plan?: string[] }

interface Store {
  doc: CanvasDoc
  kernelInfo: KernelInfo | null
  kernelUp: boolean
  catalog: CatalogTable[]
  processors: ProcessorDescriptor[]
  specsVersion: number
  schemas: SchemaMap               // per-node output columns (typed ports); null entry = untyped

  selectedId: string | null        // primary selection (drives panels)
  selectedIds: string[]            // full multi-selection (box/shift-select)
  openPanels: Record<string, PanelKind>
  previews: Record<string, PreviewState>
  runs: Record<string, RunState>
  past: CanvasDoc[]
  future: CanvasDoc[]
  saved: boolean          // auto-save state (localStorage), shown subtly in the top bar

  agentOpen: boolean
  agentMode: 'plan' | 'build'
  agentLog: AgentMsg[]

  // -- graph mutation --
  setNodes: (nodes: CanvasNode[]) => void
  setEdges: (edges: CanvasEdge[]) => void
  addNode: (kind: string, position: { x: number; y: number }, config?: Partial<NodeConfig>, title?: string) => CanvasNode | null
  setParent: (id: string, parentId: string | null, position: { x: number; y: number }) => void
  updateConfig: (id: string, patch: Partial<NodeConfig>) => void
  updateData: (id: string, patch: Partial<NodeData>) => void
  removeNode: (id: string) => void
  connect: (edge: CanvasEdge) => void
  removeEdge: (id: string) => void
  select: (id: string | null) => void
  setSelection: (ids: string[]) => void
  removeSelected: () => void

  bypass: (id: string) => void
  disable: (id: string) => void
  rename: (id: string, title: string) => void
  duplicate: (id: string) => void

  commit: () => void
  undo: () => void
  redo: () => void

  togglePanel: (id: string, kind: PanelKind) => void
  openPanel: (id: string, kind: PanelKind) => void
  closePanel: (id: string) => void

  // -- execution --
  runPreview: (id: string, offset?: number) => Promise<void>
  requestRun: (id: string) => Promise<void>
  estimate: (id: string) => Promise<void>
  run: (id: string, confirmed?: boolean) => Promise<void>
  rerunAll: () => void
  cancelRun: (id: string) => Promise<void>
  clearRun: (id: string) => void
  promote: (id: string) => Promise<void>
  restoreVersion: (id: string, versionId: string) => void

  // -- kernel + catalog --
  bootstrap: () => Promise<void>
  refreshCatalog: () => Promise<void>
  refreshSchemas: () => Promise<void>

  // -- agent --
  setAgentOpen: (v: boolean) => void
  setAgentMode: (m: 'plan' | 'build') => void
  pushAgent: (m: AgentMsg) => void

  // -- persistence --
  save: () => Promise<void>
  loadDoc: (doc: CanvasDoc) => void
  applyAgentGraph: (graph: { nodes: AgentBackendNode[]; edges: AgentBackendEdge[] }) => void

  // -- app shell (Figma-style views) --
  view: DpView
  setView: (v: DpView) => void
  // drop a catalog dataset / library transform onto the open canvas and navigate to it (Tables/Transforms)
  addToCanvas: (kind: string, config: Partial<NodeConfig>, title?: string) => void
  // a full-viewport Monaco editor for one node's code param (opened from the Inspector)
  fullscreenCode: { nodeId: string; param: string; lang?: string } | null
  openCodeFullscreen: (nodeId: string, param: string, lang?: string) => void
  closeCodeFullscreen: () => void
  // transient notifications surfaced as toasts (errors/info) — so failures aren't silent
  toasts: { id: string; kind: 'error' | 'info' | 'success'; msg: string }[]
  pushToast: (msg: string, kind?: 'error' | 'info' | 'success') => void
  dismissToast: (id: string) => void
  // realtime collaboration presence: other people currently on this canvas (live cursors + avatars)
  peers: Record<string, { name: string; color: string; cursor?: { x: number; y: number } }>
  setPeer: (id: string, p: { name: string; color: string; cursor?: { x: number; y: number } }) => void
  dropPeer: (id: string) => void
  clearPeers: () => void

  // -- users + files (per-user, multi-file) --
  authEnabled: boolean            // whether a real login/session is in force (→ show Log out)
  setAuthEnabled: (v: boolean) => void
  currentUser: DpUser | null
  users: DpUser[]
  files: CanvasFile[]
  refreshFiles: () => Promise<void>
  openFile: (id: string) => Promise<boolean>
  newFile: () => Promise<void>
  renameFile: (name: string) => void
  deleteFile: (id: string) => Promise<void>
}

// Top-level views (like Figma's Recents / Design surfaces). 'canvas' is the editor; settings is a modal.
export type DpView = 'canvas' | 'files' | 'tables' | 'transforms'

function emptyDoc(): CanvasDoc {
  return { id: `canvas_${Math.floor(performance.now())}`, name: 'untitled', version: 1, nodes: [], edges: [] }
}

// Fold legacy documents into the current node model on load:
//  - the old `notebook` kind is now just a `transform` scoped to a sample (they ran identically).
//  - the old `muted` flag was purely visual (never affected execution); drop it so it doesn't get
//    mistaken for the new `disabled` semantics.
function migrateDoc(doc: CanvasDoc): CanvasDoc {
  let changed = false
  const nodes = doc.nodes.map((n) => {
    let node = n
    if (n.type === 'notebook') {
      changed = true
      node = { ...node, type: 'transform', data: { ...node.data, config: { source: 'adhoc', scope: 'sample', ...node.data.config } } }
    }
    if ((node.data as { muted?: boolean }).muted !== undefined) {
      changed = true
      const { muted: _drop, ...rest } = node.data as NodeData & { muted?: boolean }
      node = { ...node, data: rest }
    }
    return node
  })
  return changed ? { ...doc, nodes } : doc
}

// true if the node, or anything feeding it, has an unmet required param — so running the pipeline
// through it would fail. Keeps rerun-all consistent with the disabled ▶ on the cards.
function hasInvalidUpstream(doc: CanvasDoc, id: string): boolean {
  const seen = new Set<string>()
  const walk = (nid: string): boolean => {
    if (seen.has(nid)) return false
    seen.add(nid)
    const n = doc.nodes.find((x) => x.id === nid)
    if (!n) return false
    if (nodeInvalidReason(n)) return true
    return doc.edges.filter((e) => e.target === nid).map((e) => e.source).some(walk)
  }
  return walk(id)
}

// downstream node ids (BFS over edges)
function downstream(doc: CanvasDoc, id: string): Set<string> {
  const out = new Set<string>()
  const q = [id]
  while (q.length) {
    const cur = q.shift()!
    for (const e of doc.edges) {
      if (e.source === cur && !out.has(e.target)) {
        out.add(e.target)
        q.push(e.target)
      }
    }
  }
  return out
}

export const useStore = create<Store>((set, get) => ({
  doc: emptyDoc(),
  view: 'canvas',
  setView: (view) => set({ view }),
  addToCanvas: (kind, config, title) => {
    const pos = freePosition(get().doc.nodes, { x: 160, y: 160 })
    get().addNode(kind, pos, config, title)  // commits + selects the new node
    set({ view: 'canvas' })
  },
  fullscreenCode: null,
  openCodeFullscreen: (nodeId, param, lang) => set({ fullscreenCode: { nodeId, param, lang } }),
  closeCodeFullscreen: () => set({ fullscreenCode: null }),
  peers: {},
  setPeer: (id, p) => set((s) => ({ peers: { ...s.peers, [id]: p } })),
  dropPeer: (id) => set((s) => { const peers = { ...s.peers }; delete peers[id]; return { peers } }),
  clearPeers: () => set({ peers: {} }),
  toasts: [],
  pushToast: (msg, kind = 'info') => {
    const id = `t_${Math.floor(performance.now())}_${Math.random().toString(36).slice(2, 6)}`
    set((s) => ({ toasts: [...s.toasts, { id, kind, msg }] }))
    setTimeout(() => get().dismissToast(id), kind === 'error' ? 7000 : 4000)
  },
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  authEnabled: false,
  setAuthEnabled: (v) => set({ authEnabled: v }),
  currentUser: null,
  users: [],
  files: [],
  kernelInfo: null,
  kernelUp: false,
  catalog: [],
  processors: [],
  specsVersion: 0,
  schemas: {},
  selectedId: null,
  selectedIds: [],
  openPanels: {},
  previews: {},
  runs: {},
  past: [],
  future: [],
  saved: true,
  agentOpen: false,
  agentMode: 'build',
  agentLog: [],

  setNodes: (nodes) => set((s) => ({ doc: { ...s.doc, nodes } })),
  setEdges: (edges) => set((s) => ({ doc: { ...s.doc, edges } })),

  // push the current doc onto the undo stack (called before a structural mutation)
  commit: () => set((s) => ({ past: [...s.past, s.doc].slice(-50), future: [] })),

  undo: () => {
    _cfgEdit = { id: '', t: 0 }  // a following edit starts a fresh undo checkpoint
    set((s) => {
      if (s.past.length === 0) return {}
      const prev = s.past[s.past.length - 1]
      return { doc: prev, past: s.past.slice(0, -1), future: [s.doc, ...s.future].slice(0, 50), openPanels: {} }
    })
  },

  redo: () => {
    _cfgEdit = { id: '', t: 0 }
    set((s) => {
      if (s.future.length === 0) return {}
      const next = s.future[0]
      return { doc: next, future: s.future.slice(1), past: [...s.past, s.doc].slice(-50), openPanels: {} }
    })
  },

  addNode: (kind, position, config, title) => {
    const spec = getSpec(kind)
    if (!spec) return null
    get().commit()
    const base = spec.defaultData()
    const node: CanvasNode = {
      id: newId(kind),
      type: kind,
      position,
      data: {
        ...base,
        title: title ?? base.title,
        config: { ...base.config, ...(config ?? {}) },
      },
    }
    set((s) => ({ doc: { ...s.doc, nodes: [...s.doc.nodes, node] }, selectedId: node.id, selectedIds: [node.id] }))
    return node
  },

  updateConfig: (id, patch) => {
    // coalesced undo checkpoint: one per editing burst (new node, or >700ms idle) so a param
    // edit is its own undo step instead of discarding an unrelated earlier change.
    const now = performance.now()
    if (_cfgEdit.id !== id || now - _cfgEdit.t > 700) get().commit()
    _cfgEdit = { id, t: now }
    set((s) => {
      const stale = downstream(s.doc, id)
      const nodes: CanvasNode[] = s.doc.nodes.map((n) => {
        if (n.id === id) {
          const status: NodeStatus = n.data.status === 'draft' ? 'draft' : 'stale'
          return { ...n, data: { ...n.data, config: { ...n.data.config, ...patch }, status } }
        }
        if (stale.has(n.id) && n.data.status === 'latest') {
          return { ...n, data: { ...n.data, status: 'stale' } }
        }
        return n
      })
      // If this edit shrinks/renames the node's declared output ports, drop edges leaving a port
      // that no longer exists — otherwise they become invisible orphans (no handle to select) and
      // fail the run with "output port not produced". A null sourceHandle maps to the default port.
      let edges = s.doc.edges
      if (Array.isArray(patch.outputs)) {
        const ports = new Set((patch.outputs as unknown[]).map((h) => String(h)))
        edges = edges.filter((e) => e.source !== id || e.sourceHandle == null || ports.has(e.sourceHandle))
      }
      return { doc: { ...s.doc, nodes, edges } }
    })
  },

  updateData: (id, patch) =>
    set((s) => ({
      doc: { ...s.doc, nodes: s.doc.nodes.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)) },
    })),

  removeNode: (id) => {
    get().commit()
    set((s) => {
      const previews = { ...s.previews }; delete previews[id]
      const runs = { ...s.runs }; delete runs[id]
      return {
        doc: {
          ...s.doc,
          nodes: s.doc.nodes.filter((n) => n.id !== id),
          edges: s.doc.edges.filter((e) => e.source !== id && e.target !== id),
        },
        selectedId: s.selectedId === id ? null : s.selectedId,
        selectedIds: s.selectedIds.filter((x) => x !== id),
        openPanels: Object.fromEntries(Object.entries(s.openPanels).filter(([k]) => k !== id)),
        previews, runs,
      }
    })
  },

  connect: (edge) => {
    get().commit()
    set((s) => {
      // one edge per (target, targetHandle) for single-input ports; joins allow two.
      const stale = downstream(s.doc, edge.target)
      const nodes = s.doc.nodes.map((n) =>
        (n.id === edge.target || stale.has(n.id)) && n.data.status === 'latest'
          ? { ...n, data: { ...n.data, status: 'stale' as NodeStatus } }
          : n,
      )
      return { doc: { ...s.doc, edges: [...s.doc.edges, edge], nodes } }
    })
  },

  removeEdge: (id) => { get().commit(); set((s) => ({ doc: { ...s.doc, edges: s.doc.edges.filter((e) => e.id !== id) } })) },

  // Move a node into a section (parentId set, position now relative to the section) or back out to
  // the top-level canvas (parentId null, position absolute). Marks the section + downstream stale.
  setParent: (id, parentId, position) => {
    get().commit()
    set((s) => {
      const stale = parentId ? downstream(s.doc, parentId) : new Set<string>()
      return {
        doc: {
          ...s.doc,
          nodes: s.doc.nodes.map((n) => {
            if (n.id === id) return { ...n, parentId: parentId ?? null, position }
            if (parentId && (n.id === parentId || stale.has(n.id)) && n.data.status === 'latest') {
              return { ...n, data: { ...n.data, status: 'stale' as NodeStatus } }
            }
            return n
          }),
        },
      }
    })
  },

  select: (id) => set({ selectedId: id, selectedIds: id ? [id] : [] }),

  setSelection: (ids) => set({ selectedIds: ids, selectedId: ids[ids.length - 1] ?? null }),

  removeSelected: () => {
    const ids = get().selectedIds.length ? get().selectedIds : (get().selectedId ? [get().selectedId!] : [])
    if (!ids.length) return
    get().commit()
    const kill = new Set(ids)
    set((s) => {
      const previews = Object.fromEntries(Object.entries(s.previews).filter(([k]) => !kill.has(k)))
      const runs = Object.fromEntries(Object.entries(s.runs).filter(([k]) => !kill.has(k)))
      return {
        doc: {
          ...s.doc,
          nodes: s.doc.nodes.filter((n) => !kill.has(n.id)),
          edges: s.doc.edges.filter((e) => !kill.has(e.source) && !kill.has(e.target)),
        },
        selectedId: null, selectedIds: [],
        openPanels: Object.fromEntries(Object.entries(s.openPanels).filter(([k]) => !kill.has(k))),
        previews, runs,
      }
    })
  },

  bypass: (id) => {
    get().commit()
    set((s) => ({
      doc: {
        ...s.doc,
        nodes: s.doc.nodes.map((n) => (n.id === id ? { ...n, data: { ...n.data, bypassed: !n.data.bypassed, disabled: false } } : n)),
      },
    }))
  },

  disable: (id) => {
    get().commit()
    set((s) => ({
      doc: {
        ...s.doc,
        nodes: s.doc.nodes.map((n) => (n.id === id ? { ...n, data: { ...n.data, disabled: !n.data.disabled, bypassed: false } } : n)),
      },
    }))
  },

  rename: (id, title) => { get().commit(); get().updateData(id, { title }) },

  duplicate: (id) => {
    const n = get().doc.nodes.find((x) => x.id === id)
    if (!n) return
    get().commit()
    const copy: CanvasNode = {
      ...n,
      id: newId(n.type),
      parentId: null, // a duplicate lands on the top-level canvas (absolute coords below)
      // land in a clear spot near the original, never stacked on top of it
      position: freePosition(get().doc.nodes, { x: n.position.x + 40, y: n.position.y + 40 }),
      data: { ...n.data, status: 'draft', history: [] },
    }
    set((s) => ({ doc: { ...s.doc, nodes: [...s.doc.nodes, copy] }, selectedId: copy.id, selectedIds: [copy.id] }))
  },

  // one panel open at a time across the whole canvas — never overlapping
  togglePanel: (id, kind) =>
    set((s) => (s.openPanels[id] === kind ? { openPanels: {} } : { openPanels: { [id]: kind }, selectedId: id })),

  openPanel: (id, kind) => set({ openPanels: { [id]: kind }, selectedId: id }),

  closePanel: (id) =>
    set((s) => (s.openPanels[id] ? { openPanels: {} } : {})),

  runPreview: async (id: string, offset = 0) => {
    // offset lives in the preview state (single source of truth) so an external Refresh (which
    // re-fetches page 0) and the panel's page controls never disagree.
    set((s) => ({ previews: { ...s.previews, [id]: { loading: true, offset } }, openPanels: { [id]: 'data' } }))
    try {
      // A preview is a bounded peek (a page of rows), NOT a full materialized run — we deliberately
      // do NOT flip status to 'latest' (that green state means a real run). Paginated via `offset`.
      const result = await api.preview(get().doc, id, 50, offset)
      set((s) => ({ previews: { ...s.previews, [id]: { result, offset } } }))
    } catch (e) {
      set((s) => ({ previews: { ...s.previews, [id]: { error: (e as Error).message, offset } } }))
    }
  },

  // The play action: estimate, then start immediately for cheap work; only gate on expensive
  // runs (FR-E3). Do NOT auto-open the run panel — the card shows status; the user opens details
  // if interested. A confirm gate is the one exception (it needs the panel to show the button).
  requestRun: async (id) => {
    set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'estimating' } } }))
    let estimate
    try {
      estimate = await api.estimate(get().doc, id)
    } catch (e) {
      set((s) => ({ runs: { ...s.runs, [id]: { phase: 'failed', error: (e as Error).message } } }))
      get().pushToast((e as Error).message || 'Could not estimate the run', 'error')
      return
    }
    if (estimate.needsConfirm) {
      set((s) => ({ runs: { ...s.runs, [id]: { estimate, phase: 'confirm' } }, openPanels: { [id]: 'run' } }))
    } else {
      set((s) => ({ runs: { ...s.runs, [id]: { estimate, phase: 'running' } } }))
      await get().run(id, false)
    }
  },

  estimate: async (id) => {
    set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'estimating' } }, openPanels: { [id]: 'run' } }))
    try {
      const estimate = await api.estimate(get().doc, id)
      set((s) => ({
        runs: { ...s.runs, [id]: { estimate, phase: estimate.needsConfirm ? 'confirm' : 'estimated' } },
      }))
    } catch (e) {
      set((s) => ({ runs: { ...s.runs, [id]: { phase: 'failed', error: (e as Error).message } } }))
    }
  },

  run: async (id, confirmed = false) => {
    // no openPanels here — status shows on the card; the user opens the run panel if they want detail
    set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'running' } } }))
    get().updateData(id, { status: 'running' })
    try {
      const status = await api.run(get().doc, id, confirmed)
      set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), status, phase: 'running' } } }))
      pollRun(get, set, id, status.runId)
    } catch (e) {
      if (e instanceof KernelError && e.status === 409) {
        set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'confirm' } } }))
        get().updateData(id, { status: 'stale' })
        return
      }
      set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'failed', error: (e as Error).message } } }))
      get().updateData(id, { status: 'failed' })
      get().pushToast((e as Error).message || 'Run failed to start', 'error')
    }
  },

  // Re-run the whole graph: kick every runnable sink (a node with no outgoing edge); each pulls
  // its upstream, so the full pipeline re-executes. Notes/unconnected nodes aren't runnable → skipped.
  rerunAll: () => {
    const { doc } = get()
    const hasOutgoing = new Set(doc.edges.map((e) => e.source))
    // a section's contained children are run by the section, not as top-level sinks
    const sinks = doc.nodes.filter((n) => !n.parentId && !hasOutgoing.has(n.id) && nodeRunnable(doc, n.id))
    // don't kick off pipelines that would fail on a missing required field (matches the disabled ▶)
    const runnable = sinks.filter((n) => !hasInvalidUpstream(doc, n.id))
    runnable.forEach((n) => get().requestRun(n.id))
    const skipped = sinks.length - runnable.length
    if (skipped) get().pushToast(`Skipped ${skipped} pipeline${skipped > 1 ? 's' : ''} with a required field still empty`, 'info')
  },

  cancelRun: async (id) => {
    const st = get().runs[id]?.status
    if (!st) return
    await api.cancelRun(st.runId).catch(() => {})
    set((s) => ({ runs: { ...s.runs, [id]: { ...(s.runs[id] ?? {}), phase: 'idle' } } }))
    get().updateData(id, { status: 'stale' })
  },

  clearRun: (id) =>
    set((s) => {
      const next = { ...s.runs }
      delete next[id]
      return { runs: next }
    }),

  promote: async (id) => {
    const n = get().doc.nodes.find((x) => x.id === id)
    if (!n) return
    const cfg = n.data.config
    const pid = `user.${(n.data.title || 'op').toLowerCase().replace(/[^a-z0-9]+/g, '-')}`
    const desc = await api.promote({
      id: pid,
      title: n.data.title,
      mode: (cfg.mode as string) ?? 'map',
      code: (cfg.code as string) ?? '',
      inputColumns: [],
      outputSchema: (cfg.outputSchema as any) ?? [],
      blurb: 'promoted from an ad-hoc cell',
    })
    get().updateConfig(id, { source: 'library', processor: desc.id, version: desc.version, code: null })
    // refresh ONLY the processor list for the library picker — do NOT call bootstrap(), which
    // would re-hydrate the doc from (debounced, still-stale) localStorage and revert this node.
    try {
      set({ processors: await api.processors() })
    } catch { /* offline */ }
  },

  restoreVersion: (id, versionId) => {
    get().commit()  // Restore is undoable
    set((s) => {
      const stale = downstream(s.doc, id)
      return {
        doc: {
          ...s.doc,
          nodes: s.doc.nodes.map((n) => {
            if (n.id === id) {
              const v = (n.data.history ?? []).find((h) => h.id === versionId)
              return v ? { ...n, data: { ...n.data, config: { ...v.config }, status: 'latest' } } : n
            }
            // restoring a node's config invalidates its dependents
            if (stale.has(n.id) && n.data.status === 'latest') {
              return { ...n, data: { ...n.data, status: 'stale' } }
            }
            return n
          }),
        },
      }
    })
  },

  bootstrap: async () => {
    setApiUser(localStorage.getItem(USER_KEY))  // restore chosen user (server defaults to 'local')
    try {
      const [kernelInfo, catalog, processors, nodes] = await Promise.all([
        api.kernel(), api.tables(), api.processors(), api.nodes(),
      ])
      const added = registerGenericNodes(nodes)
      set((s) => ({ kernelInfo, kernelUp: true, catalog, processors,
        specsVersion: added ? s.specsVersion + 1 : s.specsVersion }))
    } catch {
      set({ kernelUp: false })
    }
    try {
      // resolve identity, load this user's files, open the last-opened (or newest, or a fresh one)
      const me = await api.me()
      setApiUser(me.id); localStorage.setItem(USER_KEY, me.id)
      const users = await api.users()
      set({ currentUser: me, users })
      await get().refreshFiles()
      const files = get().files
      // honor a deep link (#/canvas/<id>, incl. a shared canvas resolved server-side); else the
      // last-opened / newest / a fresh file. A #/tables|#/transforms|#/files link still loads a
      // current canvas underneath, then switches to that shell view below.
      const route = parseHash()
      const last = localStorage.getItem(OPEN_KEY(me.id))
      const fallback = last && files.some((f) => f.id === last) ? last : files[0]?.id
      // a deep-linked canvas that can't be opened (bad/revoked/other-user's link) must NOT discard
      // the last-opened file into a throwaway blank — fall back cleanly.
      const opened = (route.view === 'canvas' && route.canvasId) ? await get().openFile(route.canvasId) : false
      if (!opened) {
        if (fallback) await get().openFile(fallback)
        else await get().newFile()
        if (route.view !== 'canvas') get().setView(route.view)
      }
    } catch {
      // offline / no kernel: fall back to the local cached doc so work survives a refresh
      try {
        const saved = localStorage.getItem(LS_KEY)
        if (saved) { const doc = JSON.parse(saved) as CanvasDoc; if (doc?.nodes) set({ doc: migrateDoc(doc) }) }
      } catch { /* ignore corrupt state */ }
    }
    _bootstrapped = true  // now the real doc is loaded → autosave may persist edits (not the throwaway empty doc)
    void get().refreshSchemas()
  },

  refreshFiles: async () => { try { set({ files: await api.listCanvases() }) } catch { /* offline */ } },

  openFile: async (id) => {
    try {
      const doc = await api.getCanvas(id)
      get().loadDoc(doc)
      const uid = get().currentUser?.id
      if (uid) localStorage.setItem(OPEN_KEY(uid), id)
      set({ view: 'canvas' })  // opening a file navigates to the editor
      return true
    } catch {
      // not found / no access / deleted elsewhere → leave the current canvas & view untouched, prune
      // the stale card, and tell the user. The caller decides where to land (never a silent blank).
      await get().refreshFiles()
      get().pushToast('That canvas could not be opened (not found or no access)', 'error')
      return false
    }
  },

  newFile: async () => {
    const doc = emptyDoc()
    try { await api.createCanvas(doc); await get().refreshFiles() } catch { /* offline: PUT will create it */ }
    get().loadDoc(doc)
    const uid = get().currentUser?.id
    if (uid) localStorage.setItem(OPEN_KEY(uid), doc.id)
    set({ view: 'canvas' })
  },

  renameFile: (name) => set((s) => ({ doc: { ...s.doc, name } })),  // autosave PUTs + refreshes the list

  deleteFile: async (id) => {
    // permanent + not undoable → confirm first (guards both the file menu and the Recents trash)
    const f = get().files.find((x) => x.id === id)
    if (typeof window !== 'undefined' && !window.confirm(`Delete "${f?.name || 'this canvas'}"? This can't be undone.`)) return
    try { await api.deleteCanvas(id); await get().refreshFiles() } catch { /* offline */ }
    // only load a replacement (which navigates to the editor) if the deleted file was the one open
    // IN the editor; deleting from the Recents grid should just drop the card and stay in the shell.
    if (get().doc.id === id && get().view === 'canvas') {
      const next = get().files[0]?.id
      if (next) await get().openFile(next)
      else await get().newFile()
    }
  },

  refreshCatalog: async () => {
    try {
      const catalog = await api.tables()
      set({ catalog })
    } catch { /* noop */ }
  },

  refreshSchemas: async () => {
    // guard against out-of-order responses: only the latest request may write the schema map
    const seq = ++_schemaSeq
    try { const schemas = await api.schema(get().doc); if (seq === _schemaSeq) set({ schemas }) }
    catch { /* offline: keep last-known */ }
  },

  setAgentOpen: (v) => set({ agentOpen: v }),
  setAgentMode: (m) => set({ agentMode: m }),
  pushAgent: (m) => set((s) => ({ agentLog: [...s.agentLog, m] })),

  save: async () => {
    try {
      await api.saveCanvas(get().doc)
    } catch { /* offline: keep in memory */ }
  },

  loadDoc: (doc) => { _cfgEdit = { id: '', t: 0 }; set({ doc: migrateDoc(doc), previews: {}, runs: {}, openPanels: {}, selectedId: null, selectedIds: [], past: [], future: [] }) },

  // Apply a graph the LLM agent built (extends the canvas). Undoable; preserves UI state of nodes
  // whose ids already exist, and marks touched nodes stale so the user can preview/run them.
  applyAgentGraph: (bg) => {
    get().commit()
    set((s) => {
      const existing = new Map(s.doc.nodes.map((n) => [n.id, n]))
      const nodes: CanvasNode[] = bg.nodes.map((n) => {
        const prev = existing.get(n.id)
        if (prev) return { ...prev, position: n.position, data: { ...prev.data, title: n.data.title ?? prev.data.title, config: { ...(n.data.config ?? {}) } as CanvasNode['data']['config'], status: 'stale' } }
        return { id: n.id, type: n.type, position: n.position, data: { title: n.data.title ?? n.type, config: (n.data.config ?? {}) as CanvasNode['data']['config'], status: 'stale', history: [] } }
      })
      const edges: CanvasEdge[] = bg.edges.map((e) => ({ id: e.id, source: e.source, target: e.target, sourceHandle: e.sourceHandle ?? null, targetHandle: e.targetHandle ?? null, data: { wire: (e.data?.wire ?? 'dataset') as WireType } }))
      return { doc: { ...s.doc, nodes, edges } }
    })
  },
}))

// Auto-persist the canvas to localStorage (debounced) so a refresh keeps your work.
let _saveTimer: ReturnType<typeof setTimeout> | undefined
let _lastDoc: CanvasDoc | undefined
let _bootstrapped = false  // don't autosave the throwaway initial empty doc before the real one loads
useStore.subscribe((s) => {
  if (s.doc === _lastDoc) return
  _lastDoc = s.doc
  if (!_bootstrapped) return  // bootstrap will load & set the real doc; skip persisting anything before that
  if (s.saved) useStore.setState({ saved: false })  // dirty → "saving…"
  clearTimeout(_saveTimer)
  _saveTimer = setTimeout(async () => {
    const doc = useStore.getState().doc
    try { localStorage.setItem(LS_KEY, JSON.stringify(doc)) } catch { /* quota */ }
    try {
      await api.saveCanvas(doc)  // PUT to the metadata DB (per-user, upsert)
      useStore.setState((st) => ({
        saved: true,
        kernelUp: true,  // a successful save confirms the kernel is reachable (clears the offline banner)
        files: st.files.map((f) => (f.id === doc.id ? { ...f, name: doc.name ?? f.name, version: doc.version } : f)),
      }))
    } catch {
      // offline: the localStorage cache still holds it; flag the kernel down so the banner shows
      useStore.setState({ saved: true, kernelUp: false })
    }
  }, 400)
})

// Refresh per-node output schema (column suggestions) a beat after a SCHEMA-RELEVANT change — the
// wiring or any node's config/kind/on-off. Node positions never affect columns, so dragging must
// NOT trigger a fetch: we compare a structure signature (positions excluded) after the cheap ref
// check. Debounced; the fetch itself is guarded against out-of-order responses (refreshSchemas).
let _schemaSeq = 0
let _schemaTimer: ReturnType<typeof setTimeout> | undefined
let _lastNodesRef: CanvasNode[] | undefined
let _lastEdgesRef: CanvasEdge[] | undefined
let _schemaSig: string | undefined
function structSig(doc: CanvasDoc): string {
  const nodes = doc.nodes.map((n) => `${n.id}:${n.type}:${n.data.disabled ? 1 : 0}${n.data.bypassed ? 1 : 0}:${JSON.stringify(n.data.config)}`).join('|')
  const edges = doc.edges.map((e) => `${e.source}>${e.sourceHandle ?? ''}>${e.target}>${e.targetHandle ?? ''}`).sort().join(',')
  return `${nodes}#${edges}`
}
useStore.subscribe((s) => {
  if (s.doc.nodes === _lastNodesRef && s.doc.edges === _lastEdgesRef) return  // cheap: nothing changed
  _lastNodesRef = s.doc.nodes; _lastEdgesRef = s.doc.edges
  if (!_bootstrapped) return
  const sig = structSig(s.doc)
  if (sig === _schemaSig) return  // refs changed but structure didn't (e.g. a drag) → no schema fetch
  _schemaSig = sig
  clearTimeout(_schemaTimer)
  _schemaTimer = setTimeout(() => { if (useStore.getState().kernelUp) void useStore.getState().refreshSchemas() }, 500)
})

function pollRun(get: () => Store, set: (p: Partial<Store> | ((s: Store) => Partial<Store>)) => void, nodeId: string, runId: string) {
  const tick = async () => {
    // stop polling if the node was deleted mid-run (don't re-insert a runs entry for it)
    if (!get().doc.nodes.some((n) => n.id === nodeId)) return
    let status: RunStatus
    try {
      status = await api.runStatus(runId)
    } catch {
      return
    }
    set((s: Store) => ({ runs: { ...s.runs, [nodeId]: { ...(s.runs[nodeId] ?? { phase: 'running' as const }), status } } }))
    if (status.status === 'done' || status.status === 'failed' || status.status === 'cancelled') {
      const phase = status.status === 'done' ? 'done' : status.status === 'failed' ? 'failed' : 'idle'
      set((s: Store) => ({ runs: { ...s.runs, [nodeId]: { ...(s.runs[nodeId] ?? { phase } as any), status, phase } } }))
      if (status.status === 'failed') get().pushToast(status.error ?? 'Run failed', 'error')
      const g = get()
      g.updateData(nodeId, {
        status: status.status === 'done' ? 'latest' : status.status === 'failed' ? 'failed' : 'stale',
        lastRun: status.status === 'done'
          ? { rows: status.totalRows ?? status.rowsProcessed, ms: status.ms, cost: status.costUsd, placement: status.placement }
          : undefined,
      })
      if (status.status === 'done') {
        // snapshot a version (time-travel, FR-C5)
        const node = g.doc.nodes.find((n) => n.id === nodeId)
        if (node) {
          const version: NodeVersion = {
            id: `v_${Math.floor(performance.now())}`,
            ts: Date.now(),
            rows: status.totalRows ?? undefined,
            cost: status.costUsd,
            label: `run · ${status.totalRows ?? status.rowsProcessed} rows`,
            config: { ...node.data.config },
          }
          g.updateData(nodeId, { history: [...(node.data.history ?? []), version] })
        }
        void g.refreshCatalog()
      }
      return
    }
    setTimeout(tick, 300)
  }
  setTimeout(tick, 200)
}
