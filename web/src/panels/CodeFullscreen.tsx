import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'
import {
  previewIsCurrent, previewPlanIdentity, useStore, nodeRunnable, roleCanEdit,
} from '../store/graph'
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

interface EditorUpstreamRequest {
  editorNodeId: string
  upstreamNodeId: string
  upstreamPortId?: string
  upstreamPlanIdentity: string
  baselineRunId?: string
  cancelled?: boolean
}

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
  const runs = useStore((s) => s.runs)
  const processors = useStore((s) => s.processors)
  const kernelUp = useStore((s) => s.kernelUp)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const inputCols = useInputColumns(fs?.nodeId ?? '')  // THIS node's input schema — the precise completions
  const [testInput, setTestInput] = useState<'upstream' | 'example'>('upstream')
  const [exampleRowsJson, setExampleRowsJson] = useState('')
  const [testedExampleRowsJson, setTestedExampleRowsJson] = useState<string | null>(null)
  // This is deliberately request-local. The authoritative run lifecycle stays in the graph store;
  // the editor only remembers that this surface initiated it so an unrelated Canvas run is not
  // presented as its test input.
  const [upstreamRequest, setUpstreamRequest] = useState<EditorUpstreamRequest>()
  const refreshedUpstreamRunId = useRef<string | null>(null)
  const upstreamDispatching = useRef(false)
  const upstreamConfirming = useRef(false)
  const upstreamCancelling = useRef(false)
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
    setUpstreamRequest(undefined)
    refreshedUpstreamRunId.current = null
    upstreamDispatching.current = false
    upstreamConfirming.current = false
    upstreamCancelling.current = false
  }, [fs?.nodeId])
  const editorUpstreamEdge = fs
    ? (() => {
        const upstreamEdges = doc.edges.filter((edge) => edge.target === fs.nodeId)
        return upstreamEdges.length === 1 ? upstreamEdges[0] : undefined
      })()
    : undefined
  const editorUpstreamNodeId = editorUpstreamEdge?.source
  const editorUpstreamPortId = editorUpstreamEdge?.sourceHandle ?? undefined
  const upstreamRun = editorUpstreamNodeId ? runs[editorUpstreamNodeId] : undefined
  const upstreamRunId = upstreamRun?.status?.runId
  const requestIsCurrent = Boolean(
    fs && upstreamRequest
    && upstreamRequest.editorNodeId === fs.nodeId
    && upstreamRequest.upstreamNodeId === editorUpstreamNodeId
    && upstreamRequest.upstreamPortId === editorUpstreamPortId
    && upstreamRequest.upstreamPlanIdentity === previewPlanIdentity(
      doc, upstreamRequest.upstreamNodeId, upstreamRequest.upstreamPortId,
    ),
  )
  const freshUpstreamRunDone = Boolean(
    requestIsCurrent && upstreamRun?.phase === 'done' && upstreamRunId
    && upstreamRunId !== upstreamRequest?.baselineRunId,
  )
  useEffect(() => {
    // Re-select through the server-owned retained-result contract after the exact upstream run
    // succeeds. This neither runs nor persists the downstream Transform as a full Canvas run.
    if (!fs || testInput !== 'upstream' || !editorUpstreamNodeId || !freshUpstreamRunDone
        || !upstreamRunId || refreshedUpstreamRunId.current === upstreamRunId) return
    refreshedUpstreamRunId.current = upstreamRunId
    void runEditorPreview(fs.nodeId)
  }, [editorUpstreamNodeId, freshUpstreamRunDone, fs, runEditorPreview, testInput, upstreamRunId])
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
  const syntaxErrorLine = preview?.result?.failureCategory === 'syntax_error'
    ? preview.result.syntaxError?.line : undefined
  const freshUpstreamResultReady = Boolean(
    freshUpstreamRunDone && upstreamRunId
    && preview?.result?.editorTestInput?.runId === upstreamRunId,
  )
  const upstreamSelectionFailed = Boolean(
    freshUpstreamRunDone && refreshedUpstreamRunId.current === upstreamRunId
    && preview && !preview.loading && !freshUpstreamResultReady,
  )
  const upstreamAttemptBusy = Boolean(
    requestIsCurrent && !upstreamRequest?.cancelled && (
      upstreamRun?.phase === 'estimating'
      || upstreamRun?.phase === 'confirm'
      || upstreamRun?.phase === 'running'
      || (freshUpstreamRunDone && !freshUpstreamResultReady && !upstreamSelectionFailed)
      || upstreamRun === undefined
    ),
  )
  const upstreamAttemptBlocksTest = Boolean(
    isTransform && testInput === 'upstream' && requestIsCurrent && !freshUpstreamResultReady,
  )
  const upstreamInputUnavailable = Boolean(
    isTransform && testInput === 'upstream'
    && !preview?.result?.editorTestInput
    && (!preview || preview.loading || preview.error || preview.result?.notPreviewable),
  )
  const canRunUpstream = Boolean(
    canEdit && kernelUp && editorUpstreamNodeId
    && nodeRunnable(doc, editorUpstreamNodeId),
  )
  const own = preview && previewIsCurrent(preview, doc, fs.nodeId) ? (preview.result?.columns ?? []).map((c) => c.name) : []
  const completions = [...new Set(inputNames.length ? inputNames : own)]
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
  const runUpstream = () => {
    if (!editorUpstreamNodeId || !canRunUpstream || upstreamDispatching.current) return
    const phase = useStore.getState().runs[editorUpstreamNodeId]?.phase
    if (phase === 'estimating' || phase === 'confirm' || phase === 'running') return
    upstreamDispatching.current = true
    upstreamConfirming.current = false
    upstreamCancelling.current = false
    refreshedUpstreamRunId.current = null
    setUpstreamRequest({
      editorNodeId: fs.nodeId,
      upstreamNodeId: editorUpstreamNodeId,
      upstreamPortId: editorUpstreamPortId,
      upstreamPlanIdentity: previewPlanIdentity(
        doc, editorUpstreamNodeId, editorUpstreamPortId,
      ),
      baselineRunId: useStore.getState().runs[editorUpstreamNodeId]?.status?.runId,
    })
    void requestRun(editorUpstreamNodeId).finally(() => {
      upstreamDispatching.current = false
    })
  }
  const confirmUpstream = () => {
    if (!editorUpstreamNodeId || upstreamConfirming.current
        || useStore.getState().runs[editorUpstreamNodeId]?.phase !== 'confirm') return
    upstreamConfirming.current = true
    void useStore.getState().run(editorUpstreamNodeId, true).finally(() => {
      upstreamConfirming.current = false
    })
  }
  const cancelUpstreamConfirmation = () => {
    if (!editorUpstreamNodeId) return
    setUpstreamRequest((current) => current && current.editorNodeId === fs.nodeId
      ? { ...current, cancelled: true }
      : current)
    upstreamConfirming.current = false
    useStore.getState().clearRun(editorUpstreamNodeId)
  }
  const cancelUpstreamRun = () => {
    if (!editorUpstreamNodeId || upstreamCancelling.current) return
    upstreamCancelling.current = true
    void useStore.getState().cancelRun(editorUpstreamNodeId).finally(() => {
      upstreamCancelling.current = false
    })
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
                errorLine={syntaxErrorLine}
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
                    These rows are used only for this test and are not saved to the Canvas. Edit them, then choose Test code.
                  </div>
                ) : (
                  <>
                    {isTransform && testInput === 'upstream' && requestIsCurrent && editorUpstreamNodeId && (
                      <EditorUpstreamRunStatus nodeId={editorUpstreamNodeId} run={upstreamRun}
                        resultReady={freshUpstreamResultReady}
                        selectionFailed={upstreamSelectionFailed}
                        cancelled={upstreamRequest?.cancelled === true}
                        onConfirm={confirmUpstream}
                        onCancelConfirmation={cancelUpstreamConfirmation}
                        onCancelRun={cancelUpstreamRun} />
                    )}
                    <DataPanel key={testInput} nodeId={fs.nodeId} editorPreview={isTransform ? (
                    usingExampleRows
                      ? {
                          autoLoad: false,
                          allowStats: false,
                          resultContext: 'example-rows',
                          onPreview: (offset, portId) => testExampleRows(offset, portId),
                        }
                      : {
                          onRunUpstream: canRunUpstream && !upstreamAttemptBusy
                            ? runUpstream
                            : undefined,
                        }
                    ) : undefined} />
                  </>
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
              <button disabled={(usingExampleRows && !exampleValidation.ok)
                  || upstreamAttemptBlocksTest || upstreamInputUnavailable}
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

function EditorUpstreamRunStatus({
  nodeId, run, resultReady, selectionFailed, cancelled,
  onConfirm, onCancelConfirmation, onCancelRun,
}: {
  nodeId: string
  run?: {
    phase?: string
    error?: string
    estimate?: { rows?: number | null; bytes?: number | null; confirmationReasons?: string[] }
    status?: { runId?: string; progress?: number | null; rowsProcessed?: number; totalRows?: number | null }
  }
  resultReady: boolean
  selectionFailed: boolean
  cancelled: boolean
  onConfirm: () => void
  onCancelConfirmation: () => void
  onCancelRun: () => void
}) {
  const phase = run?.phase
  const estimate = run?.estimate
  const status = run?.status
  const upstreamLabel = useStore((s) => s.doc.nodes.find((node) => node.id === nodeId)?.data.title ?? nodeId)
  const rows = estimate?.rows == null ? 'an unknown number of rows' : `${estimate.rows.toLocaleString()} rows`

  if (phase === 'confirm') return (
    <section aria-label="Confirm upstream run" className="border-b border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-[11px]">
      <div className="font-semibold text-foreground">Confirm upstream run</div>
      <p className="mt-1 text-muted-foreground">{upstreamLabel} will process {rows}. This upstream run needs your explicit confirmation.</p>
      <div className="mt-2 flex gap-2">
        <button type="button" onClick={onConfirm}
          className="rounded bg-[#d99a2b] px-2.5 py-1.5 font-semibold text-white hover:bg-[#c98d24]">
          Run upstream
        </button>
        <button type="button" onClick={onCancelConfirmation}
          className="rounded border border-border bg-background px-2.5 py-1.5 font-semibold text-foreground hover:bg-accent">
          Cancel
        </button>
      </div>
    </section>
  )

  if (phase === 'failed') return (
    <section aria-label="Upstream run failed" role="alert" className="border-b border-destructive/30 bg-destructive/10 px-3 py-2.5 text-[11px]">
      <div className="font-semibold text-destructive">Upstream run failed</div>
      <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{run?.error ?? 'The upstream result could not be produced.'}</p>
    </section>
  )

  if (phase === 'done' && resultReady) return (
    null
  )

  if (phase === 'done' && selectionFailed) return (
    <section aria-label="Upstream result unavailable" role="alert" className="border-b border-destructive/30 bg-destructive/10 px-3 py-2.5 text-[11px]">
      <div className="font-semibold text-destructive">Fresh upstream result unavailable</div>
      <p className="mt-1 text-muted-foreground">The run finished, but its retained result could not be selected for this editor. Retry the input or run {upstreamLabel} again.</p>
    </section>
  )

  if (phase === 'running' || phase === 'estimating' || (phase === 'done' && !resultReady)) return (
    <section aria-label="Upstream run progress" role="status" className="border-b border-primary/20 bg-primary/5 px-3 py-2.5 text-[11px]">
      <div className="flex items-center gap-2 font-semibold text-foreground">
        <span className="dp-running-glyph text-primary">●</span>
        {phase === 'estimating' || phase == null
          ? 'Preparing upstream run…'
          : phase === 'done' ? 'Selecting fresh upstream result…' : 'Running upstream…'}
      </div>
      {phase === 'running' && status && <p className="mt-1 text-muted-foreground">
        {(status.rowsProcessed ?? 0).toLocaleString()}{status.totalRows != null ? ` / ${status.totalRows.toLocaleString()} rows` : ' rows processed'}
        {status.progress != null ? ` · ${Math.round(status.progress * 100)}%` : ''}
      </p>}
      {phase === 'running' && status?.runId && <button type="button" onClick={onCancelRun}
        className="mt-2 rounded border border-border bg-background px-2.5 py-1.5 font-semibold text-foreground hover:bg-accent">Stop</button>}
    </section>
  )

  if (phase === 'parameters' || phase === 'drift' || phase === 'estimated') return (
    <section aria-label="Upstream run needs attention" role="alert" className="border-b border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-[11px] text-muted-foreground">
      This upstream run needs attention before it can start. Its existing run controls remain authoritative.
    </section>
  )

  if (cancelled || phase === 'idle') return (
    <section aria-label="Upstream run cancelled" role="status" className="border-b border-border bg-muted/40 px-3 py-2.5 text-[11px] text-muted-foreground">
      Upstream run cancelled. Choose Run upstream to try again.
    </section>
  )

  return (
    <section aria-label="Upstream run progress" role="status" className="border-b border-primary/20 bg-primary/5 px-3 py-2.5 text-[11px]">
      <div className="flex items-center gap-2 font-semibold text-foreground">
        <span className="dp-running-glyph text-primary">●</span>
        Preparing upstream run…
      </div>
    </section>
  )
}
