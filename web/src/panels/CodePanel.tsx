import { Suspense, lazy } from 'react'
import { useStore } from '../store/graph'
import { color } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { MiniSelect } from '../ui/controls'
import type { ProcessorMode } from '../types/graph'

// Monaco is heavy — code-split it so it loads only when a code panel is opened.
const CodeEditor = lazy(() => import('../ui/CodeEditor').then((m) => ({ default: m.CodeEditor })))

// The {} panel: edit the operator body / SQL. Library form is read-only ("view source");
// ad-hoc form is an editable cell that declares its I/O and can be promoted (§ transform forms).
export function CodePanel({ nodeId }: { nodeId: string }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const runPreview = useStore((s) => s.runPreview)
  const promote = useStore((s) => s.promote)
  const processors = useStore((s) => s.processors)
  if (!node) return null

  const cfg = node.data.config
  const isSql = node.type === 'sql'
  const isLibrary = node.type === 'transform' && cfg.source === 'library'
  const proc = processors.find((p) => p.id === cfg.processor)
  const value = isSql ? String(cfg.sql ?? '') : String(cfg.code ?? '')
  const readOnly = isLibrary

  // columns seen in previews → Monaco's SQL/Python autocomplete
  const previews = useStore((s) => s.previews)
  const completions = [...new Set(Object.values(previews).flatMap((p) => (p.result?.columns ?? []).map((c) => c.name)))]

  return (
    <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>
      {isLibrary && (
        <div style={{ fontSize: 11, color: color.text3, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="code" size={12} /> read-only · {proc?.title} {proc?.version} · lives in the registry
        </div>
      )}
      <Suspense fallback={<div style={{ height: 220, border: `1px solid ${color.border}`, borderRadius: 8, display: 'grid', placeItems: 'center', color: color.text3, fontSize: 12 }}>loading editor…</div>}>
        <CodeEditor
          language={isSql ? 'sql' : 'python'}
          value={value}
          readOnly={readOnly}
          height={220}
          completions={completions}
          onChange={(v) => updateConfig(nodeId, isSql ? { sql: v } : { code: v })}
        />
      </Suspense>

      {node.type === 'transform' && !isLibrary && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 10.5, color: color.text3 }}>mode</span>
          <div style={{ width: 140 }}>
            <MiniSelect<ProcessorMode>
              value={(cfg.mode as ProcessorMode) ?? 'map'}
              onChange={(v) => updateConfig(nodeId, { mode: v })}
              options={[{ value: 'map', label: 'map' }, { value: 'map_batches', label: 'map_batches' }, { value: 'filter', label: 'filter' }, { value: 'flat_map', label: 'flat_map' }]}
            />
          </div>
          <span style={{ fontSize: 10.5, color: color.text3 }}>on_error</span>
          <div style={{ width: 90 }}>
            <MiniSelect
              value={(cfg.onError as 'raise' | 'skip') ?? 'raise'}
              onChange={(v) => updateConfig(nodeId, { onError: v })}
              options={[{ value: 'raise', label: 'raise' }, { value: 'skip', label: 'skip' }]}
            />
          </div>
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button onClick={() => runPreview(nodeId)} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '8px 16px', border: 'none', borderRadius: 8, background: color.ink, color: '#fff', fontSize: 12.5, fontWeight: 600 }}>
          <Icon name="play" size={12} /> Run
        </button>
        {node.type === 'transform' && !isLibrary && (
          <button onClick={() => promote(nodeId)} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '8px 14px', border: `1px solid ${color.border}`, borderRadius: 8, background: '#fff', color: color.focus, fontSize: 12, fontWeight: 600 }}>
            Promote to library <Icon name="external" size={12} />
          </button>
        )}
      </div>
    </div>
  )
}
