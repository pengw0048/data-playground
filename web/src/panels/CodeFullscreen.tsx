import { Suspense, lazy, useEffect } from 'react'
import { useStore, nodeRunnable } from '../store/graph'
import { useInputColumns } from '../nodes/fields'
import { Icon } from '../ui/Icon'
import { MiniSelect } from '../ui/controls'
import { DataPanel } from './DataPanel'
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
  const inputCols = useInputColumns(fs?.nodeId ?? '')  // THIS node's input schema — the precise completions
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
  // seed Monaco autocomplete with THIS node's input columns (precise — what a filter/select/sql/transform
  // references). Fall back to THIS node's own last-preview columns when the input schema isn't resolved yet
  // — NOT every node's previews (that leaked unrelated columns from across the whole graph).
  const inputNames = inputCols.map((c) => c.name)
  const own = (previews[fs.nodeId]?.result?.columns ?? []).map((c) => c.name)
  const completions = [...new Set(inputNames.length ? inputNames : own)]

  return (
    <div className="fixed inset-0 z-[60] flex flex-col bg-[#10141e]/45 p-7" onClick={close}>
      <div onClick={(e) => e.stopPropagation()}
        className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl bg-card shadow-2xl">
        <div className="flex items-center gap-2 border-b border-border px-3.5 py-2.5">
          <span className="flex items-center text-muted-foreground"><Icon name="code" size={14} /></span>
          <span className="text-[13px] font-semibold text-foreground">{node.data.title}</span>
          <span className="text-[12.5px] text-muted-foreground">· {fs.param} · {language}</span>
          {isLibrary && <span className="inline-flex items-center gap-[5px] text-[11px] text-muted-foreground">· read-only · {proc?.title} {proc?.version} (registry)</span>}
          <span className="flex-1" />
          <button onClick={close} aria-label="Close" title="Close (Esc)"
            className="grid h-[26px] w-7 place-items-center rounded-md border-0 bg-transparent text-muted-foreground hover:text-foreground">
            <Icon name="close" size={15} />
          </button>
        </div>
        <div className="flex min-h-0 flex-1">
          <div className="min-h-0 flex-1">
            <Suspense fallback={<div className="grid h-full place-items-center text-xs text-muted-foreground">loading editor…</div>}>
              <CodeEditor language={language} height="100%" value={value} readOnly={isLibrary} completions={completions}
                onChange={(v) => updateConfig(fs.nodeId, { [fs.param]: v })} />
            </Suspense>
          </div>
          {/* run + see results without leaving the editor — the node runs on its current input */}
          {canPreview && (
            <div className="flex min-h-0 w-[42%] max-w-[640px] flex-col overflow-auto border-l border-border">
              <DataPanel nodeId={fs.nodeId} />
            </div>
          )}
        </div>

        {/* operator controls — Python transforms get mode/on_error/Promote; anything runnable gets Preview */}
        {(isTransform && !isLibrary) || canPreview ? (
          <div className="flex items-center gap-2.5 border-t border-border px-3.5 py-2.5">
            {isTransform && !isLibrary && (
              <>
                <span className="text-[10.5px] text-muted-foreground">mode</span>
                <div className="w-[130px]">
                  <MiniSelect<ProcessorMode> value={(cfg.mode as ProcessorMode) ?? 'map'} onChange={(v) => updateConfig(fs.nodeId, { mode: v })}
                    options={[{ value: 'map', label: 'map' }, { value: 'map_batches', label: 'map_batches' }, { value: 'filter', label: 'filter' }, { value: 'flat_map', label: 'flat_map' }]} />
                </div>
                <span className="text-[10.5px] text-muted-foreground">on_error</span>
                <div className="w-[88px]">
                  <MiniSelect value={(cfg.onError as 'raise' | 'skip') ?? 'raise'} onChange={(v) => updateConfig(fs.nodeId, { onError: v })}
                    options={[{ value: 'raise', label: 'raise' }, { value: 'skip', label: 'skip' }]} />
                </div>
              </>
            )}
            <span className="flex-1" />
            {isTransform && !isLibrary && (
              <button onClick={() => promote(fs.nodeId)}
                className="inline-flex items-center gap-[5px] rounded-md border border-border bg-background px-3.5 py-2 text-xs font-semibold text-primary hover:bg-accent">
                Promote to library <Icon name="external" size={12} />
              </button>
            )}
            {canPreview && (
              <button onClick={() => runPreview(fs.nodeId)}
                className="inline-flex items-center gap-[5px] rounded-md bg-primary px-4 py-2 text-[12.5px] font-semibold text-primary-foreground hover:bg-primary/90">
                <Icon name="eye" size={12} /> Preview
              </button>
            )}
          </div>
        ) : null}
      </div>
    </div>
  )
}
