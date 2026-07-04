// Generic node rendering (PRD §4.2, §8.7): any node the frontend doesn't have a hand-built
// card for — including plugin nodes — is rendered from its /api/nodes schema. A plugin that
// registers a typed node therefore appears in the canvas, typed and wired, with NO frontend code.
import { register, getSpec, type NodeComponentProps } from './registry'
import { NodeCard } from './NodeCard'
import { useStore } from '../store/graph'
import { Field, MiniInput, MiniSelect } from '../ui/controls'
import { color } from '../theme/tokens'
import type { BackendNodeSpec } from '../api/client'
import type { WireType } from '../theme/tokens'

let backendSpecs: Record<string, BackendNodeSpec> = {}

/** The backend /api/nodes spec for a kind (param schema, ports, blurb) — used by the Inspector too. */
export function getBackendSpec(kind: string): BackendNodeSpec | undefined {
  return backendSpecs[kind]
}

/** Editable form fields for a node's non-code params (from the backend schema). Reused by the
 * generic node card and the Inspector so param editing stays in one place. */
export function NodeParamFields({ nodeId }: { nodeId: string }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const editable = (backendSpecs[node?.type ?? '']?.params ?? []).filter((p) => p.type !== 'code')
  if (editable.length === 0) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
      {editable.map((p) => {
        const val = (node?.data.config as any)?.[p.name] ?? p.default ?? ''
        return (
          <Field key={p.name} label={p.label ?? p.name}>
            {p.type === 'select' && p.options ? (
              <MiniSelect value={String(val)} options={p.options.map((o) => ({ value: o, label: o }))}
                onChange={(v) => updateConfig(nodeId, { [p.name]: v })} />
            ) : p.type === 'bool' ? (
              <button onClick={(e) => { e.stopPropagation(); updateConfig(nodeId, { [p.name]: !val }) }}
                style={{ fontSize: 11.5, textAlign: 'left', padding: '5px 7px', border: `1px solid ${color.border}`, borderRadius: 6, background: '#fff', color: color.ink }}>
                {val ? 'true' : 'false'}
              </button>
            ) : (
              <MiniInput mono={p.type !== 'string'} value={String(val)}
                onChange={(v) => {
                  if (p.type === 'int' || p.type === 'float') {
                    if (v.trim() === '') return  // don't coerce an empty field to 0
                    const n = p.type === 'int' ? parseInt(v, 10) : parseFloat(v)
                    if (!Number.isNaN(n)) updateConfig(nodeId, { [p.name]: n })  // skip partial/NaN
                  } else {
                    updateConfig(nodeId, { [p.name]: v })
                  }
                }} />
            )}
          </Field>
        )
      })}
    </div>
  )
}

function GenericNode({ id, data }: NodeComponentProps) {
  const spec = backendSpecs[useStore((s) => s.doc.nodes.find((n) => n.id === id))?.type ?? '']
  return (
    <NodeCard id={id} data={data} metaOverride={spec?.blurb}>
      <NodeParamFields nodeId={id} />
    </NodeCard>
  )
}

/** Register any backend node kind we don't already have a hand-built card for. */
export function registerGenericNodes(specs: BackendNodeSpec[]): number {
  let added = 0
  for (const b of specs) {
    backendSpecs[b.kind] = b
    if (getSpec(b.kind)) continue // a hand-built card wins
    register(
      {
        kind: b.kind, title: b.title, category: b.category as any, tag: b.tag ?? b.kind,
        inputs: b.inputs.map((p) => ({ id: p.id, label: p.label, wire: p.wire as WireType, accepts: p.accepts as WireType[] | undefined })),
        outputs: b.outputs.map((p) => ({ id: p.id, label: p.label, wire: p.wire as WireType })),
        canBypass: b.canBypass, blurb: b.blurb,
        defaultData: () => ({
          title: b.title, status: 'draft',
          config: Object.fromEntries(b.params.filter((p) => p.default != null).map((p) => [p.name, p.default])),
          meta: b.blurb,
        }),
      },
      GenericNode,
    )
    added += 1
  }
  return added
}
