import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'
import {
  previewIsCurrent, previewPlanIdentity, useStore, nodeRunnable, roleCanEdit,
} from '../store/graph'
import { useInputColumns } from '../nodes/fields'
import { Icon } from '../ui/Icon'
import { MiniSelect } from '../ui/controls'
import { DataPanel } from './DataPanel'
import type { ProcessorMode } from '../types/graph'
import type { InstalledProcessorSource, ProcessorDescriptor } from '../types/api'
import { configuredProcessorRef, exactProcessor } from '../nodes/processorIdentity'
import { api } from '../api/client'
import {
  EDITOR_EXAMPLE_MAX_BYTES,
  editorExampleRowsStarter,
  validateEditorExampleRows,
} from './editorExampleRows'

const CodeEditor = lazy(() => import('../ui/CodeEditor').then((m) => ({ default: m.CodeEditor })))

interface EditorUpstreamRequest {
  sequence: number
  editorNodeId: string
  upstreamNodeId: string
  upstreamPortId?: string
  upstreamPlanIdentity: string
  baselineRunId?: string
  baselineEditorInputRunId?: string
  baselineUpstreamStatus?: string
  refreshPreviewGeneration?: number
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
  const transformReferences = useStore((s) => s.canvasTransformReferences)
  const kernelUp = useStore((s) => s.kernelUp)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const inputCols = useInputColumns(fs?.nodeId ?? '')  // THIS node's input schema — the precise completions
  const [testInput, setTestInput] = useState<'upstream' | 'example'>('upstream')
  const [exampleRowsJson, setExampleRowsJson] = useState('')
  const [testedExampleRowsJson, setTestedExampleRowsJson] = useState<string | null>(null)
  const [promotionOpen, setPromotionOpen] = useState(false)
  const [promotionDescription, setPromotionDescription] = useState('')
  const [promotionBusy, setPromotionBusy] = useState(false)
  const [promotionError, setPromotionError] = useState('')
  // This is deliberately request-local. The authoritative run lifecycle stays in the graph store;
  // the editor only remembers that this surface initiated it so an unrelated Canvas run is not
  // presented as its test input.
  const [upstreamRequest, setUpstreamRequest] = useState<EditorUpstreamRequest>()
  const refreshedUpstreamRunId = useRef<string | null>(null)
  const refreshedUpstreamStatusRequest = useRef<number | null>(null)
  const upstreamRequestSequence = useRef(0)
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
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (promotionOpen && !promotionBusy) setPromotionOpen(false)
      else if (!promotionOpen) close()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [close, promotionBusy, promotionOpen])
  useEffect(() => {
    setTestInput('upstream')
    setExampleRowsJson('')
    setTestedExampleRowsJson(null)
    setPromotionOpen(false)
    setPromotionDescription('')
    setPromotionBusy(false)
    setPromotionError('')
    setUpstreamRequest(undefined)
    refreshedUpstreamRunId.current = null
    refreshedUpstreamStatusRequest.current = null
    upstreamRequestSequence.current = 0
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
  const editorUpstreamNode = editorUpstreamNodeId
    ? doc.nodes.find((candidate) => candidate.id === editorUpstreamNodeId)
    : undefined
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
  const upstreamStatusReachedLatest = Boolean(
    requestIsCurrent
    && upstreamRequest?.baselineUpstreamStatus !== 'latest'
    && editorUpstreamNode?.data.status === 'latest',
  )
  useEffect(() => {
    // Re-select through the server-owned retained-result contract after the exact upstream run
    // succeeds. Provider-backed graph state can expose the retained result before its initiating
    // run-phase event catches up, so a request-local draft/stale → latest transition is also proof
    // that it is time to ask the server for the exact retained input.
    if (!fs || testInput !== 'upstream' || !editorUpstreamNodeId || !upstreamRequest) return
    const refreshForRun = Boolean(
      freshUpstreamRunDone && upstreamRunId
      && refreshedUpstreamRunId.current !== upstreamRunId,
    )
    const refreshForStatus = Boolean(
      upstreamStatusReachedLatest
      && refreshedUpstreamStatusRequest.current !== upstreamRequest.sequence,
    )
    if (!refreshForRun && !refreshForStatus) return
    if (refreshForRun && upstreamRunId) refreshedUpstreamRunId.current = upstreamRunId
    if (refreshForStatus) {
      refreshedUpstreamStatusRequest.current = upstreamRequest.sequence
      if (upstreamRunId && upstreamRunId !== upstreamRequest.baselineRunId) {
        refreshedUpstreamRunId.current = upstreamRunId
      }
    }
    const priorGeneration = useStore.getState().editorPreviews[fs.nodeId]?.requestGeneration
    void runEditorPreview(fs.nodeId)
    const refreshPreviewGeneration = useStore.getState()
      .editorPreviews[fs.nodeId]?.requestGeneration
    if (refreshPreviewGeneration == null || refreshPreviewGeneration === priorGeneration) return
    setUpstreamRequest((current) => (
      current?.sequence === upstreamRequest.sequence
        ? { ...current, refreshPreviewGeneration }
        : current
    ))
  }, [
    editorUpstreamNodeId, freshUpstreamRunDone, fs, runEditorPreview, testInput,
    upstreamRequest, upstreamRunId, upstreamStatusReachedLatest,
  ])
  const candidateCfg = (node?.data.config ?? {}) as Record<string, unknown>
  const candidateIsTransform = node?.type === 'transform'
  const candidateIsLibrary = candidateIsTransform && candidateCfg.source === 'library'
  const configuredRef = configuredProcessorRef(candidateCfg.processor, candidateCfg.version)
  const listedProcessor = exactProcessor(
    processors, candidateCfg.processor, candidateCfg.version,
  ) ?? transformReferences.find((reference) => (
    reference.id === candidateCfg.processor && reference.version === candidateCfg.version
  ))?.descriptor ?? undefined
  const {
    descriptor: libraryDescriptor,
    loading: libraryDescriptorLoading,
    error: libraryDescriptorError,
  } = useExactLibraryDescriptor(
    candidateIsLibrary ? candidateCfg.processor : undefined,
    candidateIsLibrary ? candidateCfg.version : undefined,
    listedProcessor,
  )
  const {
    source: installedSource,
    loading: installedSourceLoading,
    error: installedSourceError,
  } = useInstalledProcessorSource(
    candidateIsLibrary ? candidateCfg.processor : undefined,
    candidateIsLibrary ? candidateCfg.version : undefined,
    candidateIsLibrary,
  )
  if (!fs || !node) return null

  const cfg = candidateCfg
  const language = fs.lang === 'sql' ? 'sql' : 'python'
  const value = String(cfg[fs.param] ?? '')
  const isTransform = candidateIsTransform
  const isLibrary = candidateIsLibrary
  // Annotation `code` nodes don't run here. A Library Transform can use the same bounded retained-
  // upstream test as an ad-hoc Transform only when its exact registry descriptor allows preview.
  const canPreview = runnable && node.type !== 'code'
    && (!isLibrary || libraryDescriptor?.previewable === true)
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
  const selectedEditorInputRunId = preview?.result?.editorTestInput?.runId
  const freshUpstreamResultReady = Boolean(
    requestIsCurrent && !upstreamRequest?.cancelled
    && upstreamRun?.phase !== 'failed'
    && preview && previewIsCurrent(preview, doc, fs.nodeId)
    && upstreamRequest?.refreshPreviewGeneration != null
    && preview.requestGeneration === upstreamRequest.refreshPreviewGeneration
    && selectedEditorInputRunId
    && selectedEditorInputRunId !== upstreamRequest?.baselineEditorInputRunId
    && selectedEditorInputRunId !== upstreamRequest?.baselineRunId,
  )
  const upstreamSelectionFailed = Boolean(
    requestIsCurrent && upstreamRequest?.refreshPreviewGeneration != null
    && preview?.requestGeneration === upstreamRequest.refreshPreviewGeneration
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
    const current = useStore.getState()
    const phase = current.runs[editorUpstreamNodeId]?.phase
    if (phase === 'estimating' || phase === 'confirm' || phase === 'running') return
    upstreamDispatching.current = true
    upstreamConfirming.current = false
    upstreamCancelling.current = false
    refreshedUpstreamRunId.current = null
    refreshedUpstreamStatusRequest.current = null
    const baselinePreview = current.editorPreviews[fs.nodeId]
    setUpstreamRequest({
      sequence: ++upstreamRequestSequence.current,
      editorNodeId: fs.nodeId,
      upstreamNodeId: editorUpstreamNodeId,
      upstreamPortId: editorUpstreamPortId,
      upstreamPlanIdentity: previewPlanIdentity(
        current.doc, editorUpstreamNodeId, editorUpstreamPortId,
      ),
      baselineRunId: current.runs[editorUpstreamNodeId]?.status?.runId,
      baselineEditorInputRunId: baselinePreview?.result?.editorTestInput?.runId,
      baselineUpstreamStatus: current.doc.nodes.find(
        (candidate) => candidate.id === editorUpstreamNodeId,
      )?.data.status,
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
  const submitPromotion = async () => {
    const description = promotionDescription.trim()
    if (!description || promotionBusy) return
    setPromotionBusy(true)
    setPromotionError('')
    try {
      await promote(fs.nodeId, description)
      setPromotionOpen(false)
    } catch (error) {
      setPromotionError((error as Error).message || 'Could not promote this Transform')
    } finally {
      setPromotionBusy(false)
    }
  }
  return (
    <div className="fixed inset-0 z-[60] flex flex-col bg-[#10141e]/45 p-7" onClick={close}>
      <div onClick={(e) => e.stopPropagation()}
        className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl bg-card shadow-2xl">
        <div className="flex items-center gap-2 border-b border-border px-3.5 py-2.5">
          <span className="flex items-center text-muted-foreground"><Icon name="code" size={14} /></span>
          <span className="text-[13px] font-semibold text-foreground">{node.data.title}</span>
          <span className="text-[12.5px] text-muted-foreground">
            {isLibrary ? '· Library processor' : `· ${fs.param} · ${language}`}
          </span>
          {(isLibrary || !canEdit) && <span className="inline-flex items-center gap-[5px] text-[11px] text-muted-foreground">· read-only{isLibrary ? ` · ${libraryDescriptor ? `${libraryDescriptor.title} ${libraryDescriptor.version}` : `${configuredRef ?? 'unconfigured library transform'} (exact reference)`}` : ''}</span>}
          <span className="flex-1" />
          <button onClick={close} aria-label="Close" title="Close (Esc)"
            className="grid h-[26px] w-7 place-items-center rounded-md border-0 bg-transparent text-muted-foreground hover:text-foreground">
            <Icon name="close" size={15} />
          </button>
        </div>
        <div className="flex min-h-0 flex-1">
          {isLibrary ? (
            <LibraryProcessorDefinition
              configuredRef={configuredRef}
              descriptor={libraryDescriptor}
              loading={libraryDescriptorLoading}
              error={libraryDescriptorError}
              runnable={runnable}
              installedSource={installedSource}
              installedSourceLoading={installedSourceLoading}
              installedSourceError={installedSourceError}
            />
          ) : (
            <div className="min-h-0 flex-1">
              <Suspense fallback={<div className="grid h-full place-items-center text-xs text-muted-foreground">loading editor…</div>}>
                <CodeEditor language={language} height="100%" value={value} readOnly={!canEdit} completions={completions}
                  errorLine={syntaxErrorLine}
                  onChange={(v) => updateConfig(fs.nodeId, { [fs.param]: v })} />
              </Suspense>
            </div>
          )}
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
                          testTarget: isLibrary ? 'transform' : 'code',
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
              <button onClick={() => {
                setPromotionDescription('')
                setPromotionError('')
                setPromotionOpen(true)
              }}
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
                <Icon name="eye" size={12} /> {isLibrary ? 'Test transform' : isTransform ? 'Test code' : 'Preview'}
              </button>
            )}
          </div>
        ) : null}
      </div>
      {promotionOpen && (
        <PromotionDescriptionDialog
          title={String(node.data.title || 'Transform')}
          description={promotionDescription}
          busy={promotionBusy}
          error={promotionError}
          onChange={setPromotionDescription}
          onCancel={() => {
            if (!promotionBusy) setPromotionOpen(false)
          }}
          onSubmit={() => void submitPromotion()}
        />
      )}
    </div>
  )
}

function useExactLibraryDescriptor(
  processor: unknown,
  version: unknown,
  listed: ProcessorDescriptor | undefined,
): { descriptor?: ProcessorDescriptor; loading: boolean; error: string } {
  const id = typeof processor === 'string' && processor ? processor : undefined
  const exactVersion = typeof version === 'string' && version ? version : undefined
  const [state, setState] = useState<{
    descriptor?: ProcessorDescriptor
    loading: boolean
    error: string
  }>({ descriptor: listed, loading: Boolean(id && exactVersion && !listed), error: '' })

  useEffect(() => {
    if (!id || !exactVersion) {
      setState({ descriptor: undefined, loading: false, error: '' })
      return
    }
    if (listed) {
      setState({ descriptor: listed, loading: false, error: '' })
      return
    }
    let current = true
    setState({ descriptor: undefined, loading: true, error: '' })
    void api.transformLibraryDetail(id, exactVersion).then((detail) => {
      if (!current) return
      const descriptor = Array.isArray(detail.versions)
        ? detail.versions.find((candidate) => candidate.version === exactVersion)
        : undefined
      setState(descriptor
        ? { descriptor, loading: false, error: '' }
        : {
            descriptor: undefined,
            loading: false,
            error: `Registry metadata for ${id}@${exactVersion} is unavailable.`,
          })
    }).catch((error) => {
      if (!current) return
      setState({
        descriptor: undefined,
        loading: false,
        error: (error as Error).message || `Could not load ${id}@${exactVersion}.`,
      })
    })
    return () => { current = false }
  }, [exactVersion, id, listed])

  return state
}

function useInstalledProcessorSource(
  processor: unknown,
  version: unknown,
  enabled: boolean,
): { source?: InstalledProcessorSource; loading: boolean; error: string } {
  const id = typeof processor === 'string' && processor ? processor : undefined
  const exactVersion = typeof version === 'string' && version ? version : undefined
  const [state, setState] = useState<{
    key: string
    source?: InstalledProcessorSource
    loading: boolean
    error: string
  }>({ key: '', loading: false, error: '' })
  const key = enabled && id && exactVersion ? `${id}\u0000${exactVersion}` : ''

  useEffect(() => {
    if (!key || !id || !exactVersion) {
      setState({ key: '', source: undefined, loading: false, error: '' })
      return
    }
    let current = true
    setState({ key, source: undefined, loading: true, error: '' })
    void api.installedProcessorSource(id, exactVersion).then((source) => {
      if (current) setState({ key, source, loading: false, error: '' })
    }).catch((error) => {
      if (!current) return
      setState({
        key,
        source: undefined,
        loading: false,
        error: typeof error === 'object' && error !== null
          && 'status' in error && error.status === 404
          ? ''
          : (error as Error).message || `Could not load installed source for ${id}@${exactVersion}.`,
      })
    })
    return () => { current = false }
  }, [exactVersion, id, key])

  return state.key === key
    ? state
    : { source: undefined, loading: Boolean(key), error: '' }
}

function LibraryProcessorDefinition({
  configuredRef,
  descriptor,
  loading,
  error,
  runnable,
  installedSource,
  installedSourceLoading,
  installedSourceError,
}: {
  configuredRef?: string
  descriptor?: ProcessorDescriptor
  loading: boolean
  error: string
  runnable: boolean
  installedSource?: InstalledProcessorSource
  installedSourceLoading: boolean
  installedSourceError: string
}) {
  const parameters = descriptor ? processorParameterEntries(descriptor.paramsSchema) : []
  const status = loading
    ? 'Loading the exact processor definition before enabling a bounded test.'
    : error || !descriptor
      ? 'The exact processor definition is unavailable, so bounded testing cannot be enabled safely.'
      : descriptor.previewable === false
        ? 'This processor does not support bounded preview tests. Run it from the Canvas to produce a result.'
        : !runnable
      ? 'Connect one dataset input before testing this processor.'
      : 'Use Test transform to run this exact processor against a bounded upstream result.'

  return (
    <section aria-label="Library processor definition"
      className="min-h-0 flex-1 overflow-auto bg-background/30 px-7 py-6">
      <div className="mx-auto max-w-[760px]">
        <div className="text-[10px] font-bold uppercase tracking-[0.7px] text-muted-foreground">
          Library processor
        </div>
        <h2 className="mt-1 text-xl font-semibold text-foreground">
          {descriptor?.title ?? 'Exact Library processor'}
        </h2>
        <div className="mt-1 text-[11.5px] text-muted-foreground">
          Immutable version {descriptor?.version
            ?? (configuredRef?.includes('@')
              ? configuredRef.slice(configuredRef.lastIndexOf('@') + 1)
              : 'not available')}
        </div>

        {loading && (
          <div role="status" className="mt-5 rounded-md border border-border bg-card px-4 py-3 text-xs text-muted-foreground">
            Loading the exact registry definition…
          </div>
        )}
        {error && (
          <div role="alert" className="mt-5 rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-xs text-destructive">
            {error}
          </div>
        )}

        {descriptor && (
          <>
            <p className="mt-5 text-[13px] leading-relaxed text-muted-foreground">
              {descriptor.blurb || 'No registry description was supplied for this processor.'}
            </p>

            <dl className="mt-5 grid grid-cols-2 gap-2 sm:grid-cols-4">
              <DefinitionFact label="Registry source" value={descriptor.provenance === 'plugin' ? 'Plugin' : 'Promoted'} />
              <DefinitionFact label="Mode" value={descriptor.mode} />
              <DefinitionFact label="Category" value={descriptor.category} />
              <DefinitionFact label="Bounded test" value={descriptor.previewable ? 'Supported' : 'Unavailable'} />
            </dl>

            <div className="mt-6 grid gap-5 lg:grid-cols-2">
              <ProcessorSchema
                title="Input contract"
                columns={descriptor.inputSchema}
                fallbackNames={descriptor.inputColumns}
              />
              <ProcessorSchema
                title="Output contract"
                columns={descriptor.outputSchema}
                fallbackNames={[]}
              />
            </div>

            <section className="mt-6">
              <h3 className="text-[10px] font-bold uppercase tracking-[0.6px] text-muted-foreground">
                Parameters
              </h3>
              {parameters.length ? (
                <div className="mt-2 overflow-hidden rounded-md border border-border bg-card">
                  {parameters.map(([name, definition], index) => (
                    <div key={name}
                      className={`grid grid-cols-[minmax(0,1fr)_auto] gap-3 px-3 py-2.5 text-[11.5px] ${
                        index ? 'border-t border-border' : ''
                      }`}>
                      <div className="min-w-0">
                        <div className="break-all font-mono font-semibold text-foreground">{name}</div>
                        {typeof definition.description === 'string' && definition.description && (
                          <div className="mt-0.5 leading-relaxed text-muted-foreground">
                            {definition.description}
                          </div>
                        )}
                      </div>
                      <div className="text-right text-muted-foreground">
                        <div>{parameterType(definition)}</div>
                        {Object.prototype.hasOwnProperty.call(definition, 'default') && (
                          <div className="mt-0.5 font-mono text-[10.5px]">
                            default {formatDefinitionValue(definition.default)}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mt-2 text-xs text-muted-foreground">No configurable parameters declared.</div>
              )}
            </section>

            {descriptor.requirements.length > 0 && (
              <section className="mt-6">
                <h3 className="text-[10px] font-bold uppercase tracking-[0.6px] text-muted-foreground">
                  Requirements
                </h3>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {descriptor.requirements.map((requirement) => (
                    <span key={requirement}
                      className="rounded border border-border bg-card px-2 py-1 font-mono text-[10.5px] text-foreground">
                      {requirement}
                    </span>
                  ))}
                </div>
              </section>
            )}
          </>
        )}

        <details className="mt-6 rounded-md border border-border bg-card px-3 py-2 text-[11px] text-muted-foreground">
          <summary className="cursor-pointer font-medium text-foreground">Technical details</summary>
          <div className="mt-2">
            <div>Processor reference</div>
            <div className="break-all font-mono text-foreground">
              {configuredRef ?? 'No exact processor selected'}
            </div>
          </div>
        </details>
        <InstalledSourcePanel
          source={installedSource}
          loading={installedSourceLoading}
          error={installedSourceError}
        />
        <div className={`mt-3 rounded-md px-4 py-3 text-[11.5px] leading-relaxed ${
          error || !descriptor || descriptor.previewable === false
            ? 'border border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-200'
            : 'border border-primary/20 bg-primary/5 text-muted-foreground'
        }`}>
          {status}
        </div>
      </div>
    </section>
  )
}

function InstalledSourcePanel({
  source,
  loading,
  error,
}: {
  source?: InstalledProcessorSource
  loading: boolean
  error: string
}) {
  if (source) {
    return (
      <section aria-label="Installed processor source"
        className="mt-6 rounded-md border border-border bg-card px-4 py-3 text-[11.5px] leading-relaxed text-muted-foreground">
        <div className="font-semibold text-foreground">Installed processor source</div>
        <div className="mt-1">
          This is the exact local implementation installed for this Canvas processor. It does not
          indicate remote or distributed dispatch.
        </div>
        <pre className="mt-3 max-h-[420px] overflow-auto rounded-md border border-border bg-background p-3 text-[11px] leading-relaxed text-foreground">
          <code>{source.source}</code>
        </pre>
        <details className="mt-2">
          <summary className="cursor-pointer font-medium text-foreground">Source integrity</summary>
          <div className="mt-1">
            <span className="mr-2 uppercase">{source.language}</span>
            <span className="break-all font-mono">SHA-256 {source.sha256}</span>
          </div>
        </details>
      </section>
    )
  }
  if (loading) {
    return (
      <div role="status"
        className="mt-6 rounded-md border border-border bg-card px-4 py-3 text-[11.5px] text-muted-foreground">
        Loading installed processor source…
      </div>
    )
  }
  if (error) {
    return (
      <div role="alert"
        className="mt-6 rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-[11.5px] text-destructive">
        <div className="font-semibold">Installed processor source could not be loaded</div>
        <div className="mt-1">{error}</div>
      </div>
    )
  }
  return (
    <div className="mt-6 rounded-md border border-border bg-card px-4 py-3 text-[11.5px] leading-relaxed text-muted-foreground">
      <div className="font-semibold text-foreground">Implementation source unavailable</div>
      <div className="mt-1">
        This registry entry does not publish executable source. Canvas execution uses the exact
        registered processor shown above.
      </div>
    </div>
  )
}

function DefinitionFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-card px-3 py-2">
      <dt className="text-[9.5px] font-semibold uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-words text-[11.5px] font-medium text-foreground">{value}</dd>
    </div>
  )
}

function ProcessorSchema({
  title,
  columns,
  fallbackNames,
}: {
  title: string
  columns: ProcessorDescriptor['inputSchema']
  fallbackNames: string[]
}) {
  return (
    <section>
      <h3 className="text-[10px] font-bold uppercase tracking-[0.6px] text-muted-foreground">{title}</h3>
      <div className="mt-2 overflow-hidden rounded-md border border-border bg-card">
        {columns.length ? columns.map((column, index) => (
          <div key={`${column.name}-${index}`}
            className={`flex items-baseline gap-3 px-3 py-2 text-[11.5px] ${index ? 'border-t border-border' : ''}`}>
            <span className="min-w-0 flex-1 break-all font-mono font-semibold text-foreground">{column.name}</span>
            <span className="font-mono text-[10.5px] text-muted-foreground">{column.type}</span>
            <span className="text-[10px] text-muted-foreground">{column.nullable === false ? 'required' : 'nullable'}</span>
          </div>
        )) : fallbackNames.length ? fallbackNames.map((name, index) => (
          <div key={name}
            className={`break-all px-3 py-2 font-mono text-[11.5px] text-foreground ${index ? 'border-t border-border' : ''}`}>
            {name}
          </div>
        )) : (
          <div className="px-3 py-2 text-[11.5px] text-muted-foreground">Not declared.</div>
        )}
      </div>
    </section>
  )
}

function processorParameterEntries(schema: Record<string, unknown>) {
  const nested = schema.properties
  const source = nested && typeof nested === 'object' && !Array.isArray(nested)
    ? nested as Record<string, unknown>
    : schema
  return Object.entries(source).flatMap(([name, value]) => (
    value && typeof value === 'object' && !Array.isArray(value)
      ? [[name, value as Record<string, unknown>] as const]
      : []
  ))
}

function parameterType(definition: Record<string, unknown>) {
  if (Array.isArray(definition.enum) && definition.enum.length) {
    return definition.enum.map(formatDefinitionValue).join(' | ')
  }
  return typeof definition.type === 'string' ? definition.type : 'value'
}

function formatDefinitionValue(value: unknown) {
  if (typeof value === 'string') return JSON.stringify(value)
  if (value === undefined) return 'undefined'
  try { return JSON.stringify(value) }
  catch { return String(value) }
}

function PromotionDescriptionDialog({
  title,
  description,
  busy,
  error,
  onChange,
  onCancel,
  onSubmit,
}: {
  title: string
  description: string
  busy: boolean
  error: string
  onChange: (value: string) => void
  onCancel: () => void
  onSubmit: () => void
}) {
  return (
    <div className="fixed inset-0 z-[70] grid place-items-center bg-black/35 p-5"
      onMouseDown={(event) => {
        event.stopPropagation()
        if (event.target === event.currentTarget) onCancel()
      }}>
      <section role="dialog" aria-modal="true" aria-labelledby="promote-transform-title"
        className="w-full max-w-[520px] rounded-lg border border-border bg-card p-5 shadow-2xl"
        onMouseDown={(event) => event.stopPropagation()}>
        <h2 id="promote-transform-title" className="text-[15px] font-semibold text-foreground">
          Promote {title} to the Library
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
          Describe what the processor does and what a researcher should expect. This description is
          stored with the immutable Library version.
        </p>
        <label htmlFor="promotion-description"
          className="mt-4 block text-[11px] font-semibold text-foreground">
          Description
        </label>
        <textarea
          id="promotion-description"
          autoFocus
          maxLength={2000}
          value={description}
          onChange={(event) => onChange(event.target.value)}
          placeholder="For example: Adds normalized country and region fields from the event location."
          className="mt-1.5 h-28 w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-[12px] leading-relaxed text-foreground outline-none focus:border-primary"
        />
        <div className="mt-1 text-right text-[10px] text-muted-foreground">
          {description.length.toLocaleString()} / 2,000
        </div>
        {error && (
          <div role="alert" className="mt-3 rounded border border-destructive/30 bg-destructive/10 px-3 py-2 text-[11.5px] text-destructive">
            {error}
          </div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" onClick={onCancel} disabled={busy}
            className="rounded-md border border-border bg-background px-3 py-2 text-xs font-semibold text-foreground hover:bg-accent disabled:opacity-50">
            Cancel
          </button>
          <button type="button" onClick={onSubmit} disabled={busy || !description.trim()}
            className="rounded-md bg-primary px-3.5 py-2 text-xs font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
            {busy ? 'Promoting…' : 'Promote'}
          </button>
        </div>
      </section>
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

  // A server-owned editor input is stronger evidence than a lagging run-status event. The graph
  // may already expose the retained result while the initiating surface still says "running".
  if (resultReady) return null

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
