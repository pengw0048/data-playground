import { Suspense, lazy, useEffect, useMemo, useState } from 'react'
import { previewIsCurrent, useStore, nodeRunnable, roleCanEdit } from '../store/graph'
import { useInputColumns } from '../nodes/fields'
import { Icon } from '../ui/Icon'
import { MiniSelect } from '../ui/controls'
import { DataPanel } from './DataPanel'
import type { ProcessorMode } from '../types/graph'
import { configuredProcessorRef, exactProcessor } from '../nodes/processorIdentity'
import {
  EDITOR_EXAMPLE_MAX_BYTES,
  editorExampleRowsStarter,
  validateEditorExampleRows,
} from './editorExampleRows'

const CodeEditor = lazy(() => import('../ui/CodeEditor').then((m) => ({ default: m.CodeEditor })))

// The single code editor (decision: one place to edit code). A full-viewport Monaco with the
// operator controls (mode / on_error / Preview / Promote) that used to live in a floating panel —
// opened from the node card, the Inspector, and code-on-canvas. Edits write straight to the config.
export function CodeFullscreen() {
  const fs = useStore((s) => s.fullscreenCode)
  const doc = useStore((s) => s.doc)
  const node = fs ? doc.nodes.find((n) => n.id === fs.nodeId) : undefined
  const runnable = fs ? nodeRunnable(doc, fs.nodeId) : false
  const previews = useStore((s) => s.previews)
  const editorPreviews = useStore((s) => s.editorPreviews)
  const processors = useStore((s) => s.processors)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const inputCols = useInputColumns(fs?.nodeId ?? '')  // THIS node's input schema — the precise completions
  const [testInput, setTestInput] = useState<'upstream' | 'example'>('upstream')
  const [exampleRowsJson, setExampleRowsJson] = useState('')
  const [testedExampleRowsJson, setTestedExampleRowsJson] = useState<string | null>(null)
  const exampleValidation = useMemo(
    () => validateEditorExampleRows(exampleRowsJson),
    [exampleRowsJson],
  )
  const {
    updateConfig, closeCodeFullscreen: close, runPreview, runEditorPreview,
    runEditorExamplePreview, clearEditorPreview, requestRun, promote,
  } = useStore.getState()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [close])
  useEffect(() => {
    setTestInput('upstream')
    setExampleRowsJson('')
    setTestedExampleRowsJson(null)
  }, [fs?.nodeId])
  if (!fs || !node) return null

  const cfg = node.data.config as Record<string, unknown>
  const language = fs.lang === 'sql' ? 'sql' : 'python'
  const value = String(cfg[fs.param] ?? '')
  const isTransform = node.type === 'transform'
  const isLibrary = isTransform && cfg.source === 'library'
  const proc = exactProcessor(processors, cfg.processor, cfg.version)
  const configuredRef = configuredProcessorRef(cfg.processor, cfg.version)
  // annotation `code` nodes and library transforms don't run/preview here
  const canPreview = runnable && node.type !== 'code' && !isLibrary
  // Example rows deliberately do not depend on a runnable upstream graph.
  const canUseExampleRows = isTransform && !isLibrary
  const canTest = canPreview || canUseExampleRows
  // seed Monaco autocomplete with THIS node's input columns (precise — what a filter/select/sql/transform
  // references). Fall back to THIS node's own last-preview columns when the input schema isn't resolved yet
  // — NOT every node's previews (that leaked unrelated columns from across the whole graph).
  const inputNames = inputCols.map((c) => c.name)
  const preview = isTransform ? editorPreviews[fs.nodeId] : previews[fs.nodeId]
  const own = preview && previewIsCurrent(preview, doc, fs.nodeId) ? (preview.result?.columns ?? []).map((c) => c.name) : []
  const completions = [...new Set(inputNames.length ? inputNames : own)]
  const upstreamEdges = isTransform ? doc.edges.filter((edge) => edge.target === fs.nodeId) : []
  const editorUpstreamNodeId = upstreamEdges.length === 1 ? upstreamEdges[0]?.source : undefined
  const usingExampleRows = canUseExampleRows && testInput === 'example'
  const chooseTestInput = (input: 'upstream' | 'example') => {
    clearEditorPreview(fs.nodeId)
    setTestInput(input)
    setTestedExampleRowsJson(null)
    if (input === 'example' && !exampleRowsJson) {
      setExampleRowsJson(editorExampleRowsStarter(inputCols))
    }
  }
  const updateExampleRows = (value: string) => {
    clearEditorPreview(fs.nodeId)
    setExampleRowsJson(value)
    setTestedExampleRowsJson(null)
  }
  const testExampleRows = (offset = 0, portId?: string) => {
    if (!exampleValidation.ok) return
    setTestedExampleRowsJson(exampleRowsJson)
    void runEditorExamplePreview(fs.nodeId, exampleRowsJson, offset, portId)
  }

  return (
    <div className="fixed inset-0 z-[60] flex flex-col bg-[#10141e]/45 p-7" onClick={close}>
      <div onClick={(e) => e.stopPropagation()}
        className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl bg-card shadow-2xl">
        <div className="flex items-center gap-2 border-b border-border px-3.5 py-2.5">
          <span className="flex items-center text-muted-foreground"><Icon name="code" size={14} /></span>
          <span className="text-[13px] font-semibold text-foreground">{node.data.title}</span>
          <span className="text-[12.5px] text-muted-foreground">· {fs.param} · {language}</span>
          {(isLibrary || !canEdit) && <span className="inline-flex items-center gap-[5px] text-[11px] text-muted-foreground">· read-only{isLibrary ? ` · ${proc ? `${proc.title} ${proc.version} (registry)` : `${configuredRef ?? 'unconfigured library transform'} (exact reference)`}` : ''}</span>}
          <span className="flex-1" />
          <button onClick={close} aria-label="Close" title="Close (Esc)"
            className="grid h-[26px] w-7 place-items-center rounded-md border-0 bg-transparent text-muted-foreground hover:text-foreground">
            <Icon name="close" size={15} />
          </button>
        </div>
        <div className="flex min-h-0 flex-1">
          <div className="min-h-0 flex-1">
            <Suspense fallback={<div className="grid h-full place-items-center text-xs text-muted-foreground">loading editor…</div>}>
              <CodeEditor language={language} height="100%" value={value} readOnly={isLibrary || !canEdit} completions={completions}
                onChange={(v) => updateConfig(fs.nodeId, { [fs.param]: v })} />
            </Suspense>
          </div>
          {/* run + see results without leaving the editor — the node runs on its current input */}
          {canTest && (
            <div className="flex min-h-0 w-[42%] max-w-[640px] flex-col overflow-hidden border-l border-border">
              {canUseExampleRows && (
                <div className="border-b border-border bg-background/40 px-3 py-2.5">
                  <div role="group" aria-label="Test input" className="flex gap-1 rounded-md bg-muted/50 p-1">
                    {([
                      ['upstream', 'Upstream result'],
                      ['example', 'Example rows'],
                    ] as const).map(([input, label]) => (
                      <button key={input} type="button" aria-pressed={testInput === input}
                        onClick={() => chooseTestInput(input)}
                        className={`flex-1 rounded px-2.5 py-1.5 text-[11.5px] font-semibold ${
                          testInput === input
                            ? 'bg-card text-foreground shadow-sm'
                            : 'text-muted-foreground hover:text-foreground'
                        }`}>
                        {label}
                      </button>
                    ))}
                  </div>
                  {usingExampleRows && (
                    <div className="mt-2.5">
                      <div className="mb-1.5 flex items-center gap-2">
                        <label htmlFor="editor-example-rows"
                          className="text-[11px] font-semibold text-foreground">
                          Example rows JSON
                        </label>
                        <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[9.5px] font-bold uppercase tracking-wide text-amber-700 dark:text-amber-300">
                          Test only
                        </span>
                      </div>
                      <textarea id="editor-example-rows" aria-label="Example rows JSON"
                        value={exampleRowsJson}
                        onChange={(event) => updateExampleRows(event.target.value)}
                        spellCheck={false}
                        className="h-36 w-full resize-y rounded-md border border-border bg-card px-2.5 py-2 font-mono text-[11px] leading-relaxed text-foreground outline-none focus:border-primary" />
                      {exampleValidation.ok ? (
                        <div role="status" className="mt-1.5 text-[10.5px] text-muted-foreground">
                          {exampleValidation.rowCount} {exampleValidation.rowCount === 1 ? 'row' : 'rows'}
                          {' · '}{exampleValidation.fields.join(', ')}
                          {' · '}{exampleValidation.bytes.toLocaleString()} / {EDITOR_EXAMPLE_MAX_BYTES.toLocaleString()} bytes
                        </div>
                      ) : (
                        <div role="alert" className="mt-1.5 text-[10.5px] text-destructive">
                          {exampleValidation.error}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
              <div className="min-h-0 flex-1 overflow-auto">
                {usingExampleRows && testedExampleRowsJson !== exampleRowsJson ? (
                  <div className="grid h-full min-h-40 place-items-center px-6 text-center text-xs leading-relaxed text-muted-foreground">
                    Edit this request-local fixture, then choose Test code. It is never saved to the Canvas.
                  </div>
                ) : (
                  <DataPanel key={testInput} nodeId={fs.nodeId} editorPreview={isTransform ? (
                    usingExampleRows
                      ? {
                          autoLoad: false,
                          allowStats: false,
                          resultContext: 'example-rows',
                          onPreview: (offset, portId) => testExampleRows(offset, portId),
                        }
                      : {
                          onRunUpstream: editorUpstreamNodeId ? () => {
                            close()
                            void requestRun(editorUpstreamNodeId)
                          } : undefined,
                        }
                  ) : undefined} />
                )}
              </div>
            </div>
          )}
        </div>

        {/* operator controls — Python transforms get mode/on_error/Promote; anything runnable gets Preview */}
        {(canEdit && isTransform && !isLibrary) || canTest ? (
          <div className="flex items-center gap-2.5 border-t border-border px-3.5 py-2.5">
            {canEdit && isTransform && !isLibrary && (
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
            {canEdit && isTransform && !isLibrary && (
              <button onClick={() => promote(fs.nodeId)}
                className="inline-flex items-center gap-[5px] rounded-md border border-border bg-background px-3.5 py-2 text-xs font-semibold text-primary hover:bg-accent">
                Promote to library <Icon name="external" size={12} />
              </button>
            )}
            {canTest && (
              <button disabled={usingExampleRows && !exampleValidation.ok}
                onClick={() => (
                  usingExampleRows
                    ? testExampleRows()
                    : isTransform ? runEditorPreview(fs.nodeId) : runPreview(fs.nodeId)
                )}
                className="inline-flex items-center gap-[5px] rounded-md bg-primary px-4 py-2 text-[12.5px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50">
                <Icon name="eye" size={12} /> {isTransform ? 'Test code' : 'Preview'}
              </button>
            )}
          </div>
        ) : null}
      </div>
    </div>
  )
}
