// Generic node rendering: any node the frontend doesn't have a hand-built
// card for — including plugin nodes — is rendered from its /api/nodes schema. A plugin that
// registers a typed node therefore appears in the canvas, typed and wired, with NO frontend code.
import { useEffect, useState } from 'react'
import { register, getSpec, type NodeComponentProps } from './registry'
import { NodeCard } from './NodeCard'
import { useStore } from '../store/graph'
import { Field, MiniInput, MiniSelect } from '../ui/controls'
import { CodeSnippet } from '../ui/CodeSnippet'
import { color } from '../theme/tokens'
import type { BackendNodeSpec } from '../api/client'
import type { WireType } from '../theme/tokens'

let backendSpecs: Record<string, BackendNodeSpec> = {}

/** The backend /api/nodes spec for a kind (param schema, ports, blurb) — used by the Inspector too. */
export function getBackendSpec(kind: string): BackendNodeSpec | undefined {
  return backendSpecs[kind]
}

/** Why a node can't run yet (a required param is empty), or null if it's valid. Drives the
 * disabled Run affordance + its reason, from the backend param schema (works for any kind/plugin). */
export function nodeInvalidReason(node: { type: string; data: { config: Record<string, unknown> } }): string | null {
  const spec = backendSpecs[node.type]
  if (!spec) return null
  for (const p of spec.params) {
    if (p.required) {
      const v = node.data.config[p.name]
      if (v == null || String(v).trim() === '') return `${p.label ?? p.name} is required`
    }
  }
  return null
}

/** Editable form fields for a node's non-code params (from the backend schema). Reused by the
 * generic node card and the Inspector so param editing stays in one place. */
export function NodeParamFields({ nodeId }: { nodeId: string }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const cfg = (node?.data.config ?? {}) as Record<string, unknown>
  // hide a conditional param whose showWhen dependency isn't met (e.g. batchFormat only for map_batches)
  const visible = (p: { showWhen?: { param: string; in: string[] } }) =>
    !p.showWhen || p.showWhen.in.includes(String(cfg[p.showWhen.param] ?? ''))
  const editable = (backendSpecs[node?.type ?? '']?.params ?? []).filter((p) => p.type !== 'code' && visible(p))
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
                style={{ fontSize: 11.5, textAlign: 'left', padding: '5px 7px', border: `1px solid ${color.border}`, borderRadius: 6, background: 'transparent', color: color.ink }}>
                {val ? 'true' : 'false'}
              </button>
            ) : p.type === 'int' || p.type === 'float' ? (
              <NumberField value={val} isInt={p.type === 'int'} onCommit={(n) => updateConfig(nodeId, { [p.name]: n })} />
            ) : (
              <MiniInput value={String(val)} onChange={(v) => updateConfig(nodeId, { [p.name]: v })} />
            )}
          </Field>
        )
      })}
    </div>
  )
}

// A numeric field that keeps its own text (so you can type "0.", "-", or clear it) and commits only
// valid parsed numbers — a fully-controlled value={String(n)} reverts partial input mid-keystroke.
function NumberField({ value, isInt, onCommit }: { value: unknown; isInt: boolean; onCommit: (n: number) => void }) {
  const [text, setText] = useState(value == null ? '' : String(value))
  useEffect(() => {
    // resync only on an EXTERNAL change (e.g. a different node selected), not our own commit
    const cur = isInt ? parseInt(text, 10) : parseFloat(text)
    if (cur !== value) setText(value == null ? '' : String(value))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])
  return (
    <MiniInput mono value={text} onChange={(v) => {
      setText(v)
      if (v.trim() === '') return
      const n = isInt ? parseInt(v, 10) : parseFloat(v)
      if (!Number.isNaN(n)) onCommit(n)
    }} />
  )
}

function GenericNode({ id, data }: NodeComponentProps) {
  const spec = backendSpecs[useStore((s) => s.doc.nodes.find((n) => n.id === id))?.type ?? '']
  const openFullscreen = useStore((s) => s.openCodeFullscreen)
  // code params (python/sql) can't render as a plain field — give them the same snippet-preview
  // button the hand-built cards use, opening the one fullscreen editor. Without this a plugin node
  // that declares a code param had no editor at all in the generic card.
  const codeParams = (spec?.params ?? []).filter((p) => p.type === 'code')
  return (
    <NodeCard id={id} data={data} metaOverride={spec?.blurb}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <NodeParamFields nodeId={id} />
        {codeParams.map((p) => {
          const lang = p.lang === 'sql' ? 'sql' : 'python'
          const code = String((data.config as any)[p.name] ?? p.default ?? '')
          return (
            <button key={p.name}
              onClick={(e) => { e.stopPropagation(); openFullscreen(id, p.name, p.lang) }}
              title={`Edit ${p.label ?? p.name}`}
              className="block w-full cursor-text overflow-hidden text-ellipsis whitespace-pre rounded-md border border-border bg-[var(--code-bg)] px-2.5 py-2 text-left text-[10.5px] leading-[1.4]">
              <CodeSnippet code={(code || `# ${p.label ?? p.name}`).split('\n').slice(0, 3).join('\n')} language={lang} />
            </button>
          )
        })}
      </div>
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
        inputs: b.inputs.map((p) => ({
          id: p.id, label: p.label, wire: p.wire as WireType,
          accepts: p.accepts as WireType[] | undefined, multi: p.multi,
        })),
        outputs: b.outputs.map((p) => ({ id: p.id, label: p.label, wire: p.wire as WireType })),
        canBypass: b.canBypass, previewable: b.previewable, requires: b.requires, blurb: b.blurb,
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
