import { Suspense, lazy, useEffect } from 'react'
import { useStore, nodeRunnable } from '../store/graph'
import { color } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { MiniSelect } from '../ui/controls'
import type { ProcessorMode } from '../types/graph'

const CodeEditor = lazy(() => import('../ui/CodeEditor').then((m) => ({ default: m.CodeEditor })))

// The single code editor (decision: one place to edit code). A full-viewport Monaco with the
// operator controls (mode / on_error / Preview / Promote) that used to live in a floating panel —
// opened from the node card, the Inspector, and code-on-canvas. Edits write straight to the config.
export function CodeFullscreen() {
  const fs = useStore((s) => s.fullscreenCode)
  const node = useStore((s) => (fs ? s.doc.nodes.find((n) => n.id === fs.nodeId) : undefined))
  const runnable = useStore((s) => (fs ? nodeRunnable(s.doc, fs.nodeId) : false))
  const previews = useStore((s) => s.previews)
  const processors = useStore((s) => s.processors)
  const { updateConfig, closeCodeFullscreen: close, runPreview, promote } = useStore.getState()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [close])
  if (!fs || !node) return null

  const cfg = node.data.config as Record<string, unknown>
  const language = fs.lang === 'sql' ? 'sql' : 'python'
  const value = String(cfg[fs.param] ?? '')
  const isTransform = node.type === 'transform'
  const isLibrary = isTransform && cfg.source === 'library'
  const proc = processors.find((p) => p.id === cfg.processor)
  // annotation `code` nodes and library transforms don't run/preview here
  const canPreview = runnable && node.type !== 'code' && !isLibrary
  // seed Monaco autocomplete with every column seen in previews so far
  const completions = [...new Set(Object.values(previews).flatMap((p) => (p.result?.columns ?? []).map((c) => c.name)))]

  return (
    <div style={{ position: 'fixed', inset: 0, zIndex: 60, background: 'rgba(16,20,30,0.45)', display: 'flex', flexDirection: 'column', padding: 28 }}
      onClick={close}>
      <div onClick={(e) => e.stopPropagation()}
        style={{ flex: 1, minHeight: 0, background: '#fff', borderRadius: 12, overflow: 'hidden', display: 'flex', flexDirection: 'column', boxShadow: '0 20px 60px rgba(16,20,30,0.35)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px', borderBottom: `1px solid ${color.hairline}` }}>
          <Icon name="code" size={14} style={{ color: color.text3 }} />
          <span style={{ fontSize: 13, fontWeight: 600, color: color.ink }}>{node.data.title}</span>
          <span style={{ fontSize: 12.5, color: color.text3 }}>· {fs.param} · {language}</span>
          {isLibrary && <span style={{ fontSize: 11, color: color.text3, display: 'inline-flex', alignItems: 'center', gap: 5 }}>· read-only · {proc?.title} {proc?.version} (registry)</span>}
          <span style={{ flex: 1 }} />
          <button onClick={close} aria-label="Close" title="Close (Esc)"
            style={{ width: 28, height: 26, display: 'grid', placeItems: 'center', border: 'none', borderRadius: 7, background: 'transparent', color: color.text2, cursor: 'pointer' }}>
            <Icon name="close" size={15} />
          </button>
        </div>
        <div style={{ flex: 1, minHeight: 0 }}>
          <Suspense fallback={<div style={{ height: '100%', display: 'grid', placeItems: 'center', color: color.text3, fontSize: 12 }}>loading editor…</div>}>
            <CodeEditor language={language} height="100%" value={value} readOnly={isLibrary} completions={completions}
              onChange={(v) => updateConfig(fs.nodeId, { [fs.param]: v })} />
          </Suspense>
        </div>

        {/* operator controls — Python transforms get mode/on_error/Promote; anything runnable gets Preview */}
        {(isTransform && !isLibrary) || canPreview ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', borderTop: `1px solid ${color.hairline}` }}>
            {isTransform && !isLibrary && (
              <>
                <span style={{ fontSize: 10.5, color: color.text3 }}>mode</span>
                <div style={{ width: 130 }}>
                  <MiniSelect<ProcessorMode> value={(cfg.mode as ProcessorMode) ?? 'map'} onChange={(v) => updateConfig(fs.nodeId, { mode: v })}
                    options={[{ value: 'map', label: 'map' }, { value: 'map_batches', label: 'map_batches' }, { value: 'filter', label: 'filter' }, { value: 'flat_map', label: 'flat_map' }]} />
                </div>
                <span style={{ fontSize: 10.5, color: color.text3 }}>on_error</span>
                <div style={{ width: 88 }}>
                  <MiniSelect value={(cfg.onError as 'raise' | 'skip') ?? 'raise'} onChange={(v) => updateConfig(fs.nodeId, { onError: v })}
                    options={[{ value: 'raise', label: 'raise' }, { value: 'skip', label: 'skip' }]} />
                </div>
              </>
            )}
            <span style={{ flex: 1 }} />
            {isTransform && !isLibrary && (
              <button onClick={() => promote(fs.nodeId)}
                style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '8px 14px', border: `1px solid ${color.border}`, borderRadius: 8, background: '#fff', color: color.focus, fontSize: 12, fontWeight: 600 }}>
                Promote to library <Icon name="external" size={12} />
              </button>
            )}
            {canPreview && (
              <button onClick={() => runPreview(fs.nodeId)}
                style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '8px 16px', border: 'none', borderRadius: 8, background: color.ink, color: '#fff', fontSize: 12.5, fontWeight: 600 }}>
                <Icon name="eye" size={12} /> Preview
              </button>
            )}
          </div>
        ) : null}
      </div>
    </div>
  )
}
