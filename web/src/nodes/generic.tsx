// Generic node rendering: any node the frontend doesn't have a hand-built
// card for — including plugin nodes — is rendered from its /api/nodes schema. A plugin that
// registers a typed node therefore appears in the canvas, typed and wired, with NO frontend code.
import { register, getSpec, type NodeComponentProps } from './registry'
import { NodeCard } from './NodeCard'
import { useStore } from '../store/graph'
import { Field, MiniInput, MiniSelect } from '../ui/controls'
import { ColumnListPicker, useInputColumns } from './fields'
import { CodeSnippet } from '../ui/CodeSnippet'
import { color } from '../theme/tokens'
import type { BackendNodeSpec, BackendParam } from '../api/client'
import type { WireType } from '../theme/tokens'

let backendSpecs: Record<string, BackendNodeSpec> = {}

/** The backend /api/nodes spec for a kind (param schema, ports, blurb) — used by the Inspector too. */
export function getBackendSpec(kind: string): BackendNodeSpec | undefined {
  return backendSpecs[kind]
}

/** Why a node can't run yet (a required param is empty), or null if it's valid. Drives the
 * disabled Run affordance + its reason, from the backend param schema (works for any kind/plugin). */
export function nodeInvalidReason(
  node: { type: string; data: { config: Record<string, unknown> } }, inputColumns?: { name: string }[],
  numericDrafts?: Record<string, string>,
): string | null {
  const spec = backendSpecs[node.type]
  if (!spec) return null
  for (const p of spec.params) {
    if (p.showWhen && !p.showWhen.in.includes(String(node.data.config[p.showWhen.param] ?? ''))) continue
    const v = node.data.config[p.name]
    if (p.type === 'int' || p.type === 'float') {
      const draft = numericDrafts?.[p.name]
      if (draft !== undefined) {
        const reason = numericParamReason(p, draft)
        if (reason) return reason
        continue
      }
      const effective = v ?? p.default
      if (effective == null) {
        if (p.required) return `${p.label ?? p.name} is required`
        continue
      }
      const valid = typeof effective === 'number'
        && (p.type === 'int' ? Number.isSafeInteger(effective) : Number.isFinite(effective))
      if (!valid) return numericTypeReason(p)
      continue
    }
    if (p.required) {
      if (v == null || String(v).trim() === '') return `${p.label ?? p.name} is required`
    }
    if (p.type === 'columns' && v != null) {
      if (!Array.isArray(v) || v.some((column) => typeof column !== 'string' || !column.trim())) {
        return `${p.label ?? p.name} must be an ordered list of column names`
      }
      if (inputColumns?.length) {
        const available = new Set(inputColumns.map((column) => column.name))
        const missing = v.filter((column) => !available.has(column))
        if (missing.length) return `${p.label ?? p.name} references unavailable column${missing.length === 1 ? '' : 's'}: ${missing.join(', ')}`
      }
    }
  }
  return null
}

/** Invalid in-progress numeric text only. Used by autosave so an unrelated doc edit cannot flush it. */
export function numericDraftInvalidReason(
  node: { type: string; data: { config: Record<string, unknown> } }, numericDrafts?: Record<string, string>,
): string | null {
  if (!numericDrafts) return null
  const spec = backendSpecs[node.type]
  if (!spec) return null
  for (const p of spec.params) {
    if (p.type !== 'int' && p.type !== 'float') continue
    if (p.showWhen && !p.showWhen.in.includes(String(node.data.config[p.showWhen.param] ?? ''))) continue
    const draft = numericDrafts[p.name]
    if (draft === undefined) continue
    const reason = numericParamReason(p, draft)
    if (reason) return reason
  }
  return null
}

/** Editable form fields for a node's non-code params (from the backend schema). Reused by the
 * generic node card and the Inspector so param editing stays in one place. */
export function NodeParamFields({ nodeId, omitNames = [] }: { nodeId: string; omitNames?: string[] }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const numericDrafts = useStore((s) => s.numericParamDrafts[nodeId])
  const setNumericDraft = useStore((s) => s.setNumericParamDraft)
  const columns = useInputColumns(nodeId)
  const cfg = (node?.data.config ?? {}) as Record<string, unknown>
  // hide a conditional param whose showWhen dependency isn't met (e.g. batchFormat only for map_batches)
  const visible = (p: { showWhen?: { param: string; in: string[] } }) =>
    !p.showWhen || p.showWhen.in.includes(String(cfg[p.showWhen.param] ?? ''))
  const omitted = new Set(omitNames)
  const editable = (backendSpecs[node?.type ?? '']?.params ?? []).filter(
    (p) => p.type !== 'code' && visible(p) && !omitted.has(p.name))
  if (editable.length === 0) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
      {editable.map((p) => {
        const val = (node?.data.config as any)?.[p.name] ?? p.default ?? (p.type === 'columns' ? [] : '')
        return (
          <Field key={p.name} label={p.label ?? p.name}>
            {p.type === 'select' && p.options ? (
              <MiniSelect value={String(val)} options={p.options.map((o) => ({ value: o, label: o }))}
                onChange={(v) => updateConfig(nodeId, { [p.name]: v })} />
            ) : p.type === 'columns' ? (
              <ColumnListPicker value={val} columns={columns} onChange={(v) => updateConfig(nodeId, { [p.name]: v })} />
            ) : p.type === 'bool' ? (
              <button onClick={(e) => { e.stopPropagation(); updateConfig(nodeId, { [p.name]: !val }) }}
                style={{ fontSize: 11.5, textAlign: 'left', padding: '5px 7px', border: `1px solid ${color.border}`, borderRadius: 6, background: 'transparent', color: color.ink }}>
                {val ? 'true' : 'false'}
              </button>
            ) : p.type === 'int' || p.type === 'float' ? (
              <NumberField param={p} value={val} draft={numericDrafts?.[p.name]}
                onDraft={(text) => setNumericDraft(nodeId, p.name, text)}
                onCommit={(n) => updateConfig(nodeId, { [p.name]: n })} />
            ) : (
              <MiniInput value={String(val)} onChange={(v) => updateConfig(nodeId, { [p.name]: v })} />
            )}
          </Field>
        )
      })}
    </div>
  )
}

export type NumericParamParse = { kind: 'empty' } | { kind: 'invalid' } | { kind: 'valid'; value: number }

/** Parse the complete field text. Prefixes such as `12abc` and non-finite values never coerce. */
export function parseNumericParam(text: string, type: 'int' | 'float'): NumericParamParse {
  const canonical = text.trim()
  if (!canonical) return { kind: 'empty' }
  const grammar = type === 'int'
    ? /^[+-]?\d+$/
    : /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/
  if (!grammar.test(canonical)) return { kind: 'invalid' }
  const value = Number(canonical)
  const valid = type === 'int' ? Number.isSafeInteger(value) : Number.isFinite(value)
  return valid ? { kind: 'valid', value } : { kind: 'invalid' }
}

function numericTypeReason(param: BackendParam): string {
  return `${param.label ?? param.name} must be a ${param.type === 'int' ? 'complete safe integer' : 'finite number'}`
}

function numericParamReason(param: BackendParam, text: string): string | null {
  const parsed = parseNumericParam(text, param.type as 'int' | 'float')
  if (parsed.kind === 'empty') {
    return param.required && param.default == null ? `${param.label ?? param.name} is required` : null
  }
  return parsed.kind === 'invalid' ? numericTypeReason(param) : null
}

// Numeric edits stay outside the canvas document until blur. Invalid text is therefore retained for
// correction without entering autosave/collaboration or becoming executable configuration.
function NumberField({ param, value, draft, onDraft, onCommit }: {
  param: BackendParam; value: unknown; draft?: string
  onDraft: (text: string | undefined) => void; onCommit: (n: number | undefined) => void
}) {
  const text = draft ?? (value == null ? '' : String(value))
  const reason = draft !== undefined
    ? numericParamReason(param, draft)
    : value != null && (typeof value !== 'number'
      || (param.type === 'int' ? !Number.isSafeInteger(value) : !Number.isFinite(value)))
      ? numericTypeReason(param)
      : null
  const clearHint = param.default != null
    ? `Clear to use the declared default (${String(param.default)}).`
    : !param.required ? 'Clear to leave this value unset.' : null
  return (
    <>
      <MiniInput mono value={text} onChange={(v) => onDraft(v)} onBlur={() => {
        if (draft === undefined) return
        const parsed = parseNumericParam(draft, param.type as 'int' | 'float')
        if (parsed.kind === 'valid') {
          onCommit(parsed.value)
          onDraft(undefined)
        } else if (parsed.kind === 'empty' && (param.default != null || !param.required)) {
          onCommit(param.default == null ? undefined : Number(param.default))
          onDraft(undefined)
        }
      }} />
      {reason
        ? <div role="alert" className="text-[10.5px] text-destructive">{reason}.</div>
        : clearHint && <div className="text-[10.5px] text-muted-foreground">{clearHint}</div>}
    </>
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
