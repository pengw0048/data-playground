import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import {
  previewIsCurrent, previewPlanIdentity, profileJobIsCurrent, roleCanEdit, useStore,
} from '../store/graph'
import { capabilitiesFor, nodeOutputs } from '../nodes/registry'
import { api } from '../api/client'
import { Icon } from '../ui/Icon'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import type { ColumnSchema, PortSpec } from '../types/graph'
import type { ProfileResult, RunOutput, RunState, SampleProvenance, SampleResult } from '../types/api'

const PAGE = 50
const CHART_DISPLAY_LIMIT = 2_000

export function DataPanel({ nodeId }: { nodeId: string }) {
  const preview = useStore((s) => s.previews[nodeId])
  const runPreview = useStore((s) => s.runPreview)
  const requestRun = useStore((s) => s.requestRun)
  const doc = useStore((s) => s.doc)
  const node = doc.nodes.find((n) => n.id === nodeId)
  const outputPorts = node ? nodeOutputs(node) : []
  const [portSelection, setPortSelection] = useState<{ nodeId: string; portId?: string }>(() => ({
    nodeId, portId: preview?.portId,
  }))
  const selectedPort = portSelection.nodeId === nodeId ? portSelection.portId : preview?.portId
  const selectedPortId = outputPorts.some((port) => port.id === selectedPort)
    ? selectedPort
    : outputPorts.find((port) => port.id === 'out')?.id ?? outputPorts[0]?.id
  // Single-output requests may omit the port. Multi-output requests never rely on backend ordering:
  // the visible tab selection is carried on every preview and sampled-profile request.
  const requestPortId = outputPorts.length > 1 ? selectedPortId : undefined
  const run = useStore((s) => s.runs[nodeId])
  const runOutputs = run?.status?.outputs.filter((output) => output.nodeId === nodeId) ?? []
  const selectedRunOutput = runOutputs.find((output) => (
    output.nodeId === nodeId && (selectedPortId === undefined || output.portId === selectedPortId)
  )) ?? (outputPorts.length <= 1 && runOutputs.length === 1 ? runOutputs[0] : undefined)
  // A terminal run can fail or be cancelled after another named output was durably committed.
  // Keep that artifact readable without implying that non-committed sibling ports succeeded.
  const selectedOutput = selectedRunOutput?.outcome === 'committed' && selectedRunOutput.uri
    ? selectedRunOutput
    : undefined
  const pushToast = useStore((s) => s.pushToast)
  const [tab, setTab] = useState('rows')
  const [resultMode, setResultMode] = useState<'sample' | 'full'>('sample')
  const [detail, setDetail] = useState<number | null>(null)  // index of the row whose detail is open
  const previousOffsets = useRef<number[]>([])
  const offset = preview?.offset ?? 0  // the page is owned by the store, so an external Refresh can't desync it

  useEffect(() => {
    if (!preview || preview.portId !== requestPortId) runPreview(nodeId, 0, requestPortId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId, requestPortId, preview?.portId])
  useEffect(() => setResultMode('sample'), [nodeId])
  useEffect(() => { previousOffsets.current = [] }, [nodeId, requestPortId])
  useEffect(() => {
    // Refresh and other callers reset the store-owned page to zero. Discard navigation state too.
    if (offset === 0) previousOffsets.current = []
  }, [offset])
  useEffect(() => {
    // A node that cannot produce a bounded preview should reveal the exact artifact as soon as its
    // durable run finishes. A later explicit click on Sample remains sticky until the artifact changes.
    if (preview?.result?.notPreviewable && selectedOutput?.uri) setResultMode('full')
  }, [nodeId, requestPortId, selectedOutput?.uri, preview?.result?.notPreviewable])
  const nextPage = (rowsRead: number) => {
    previousOffsets.current.push(offset)
    setDetail(null)
    runPreview(nodeId, offset + rowsRead, requestPortId)
  }
  const previousPage = () => {
    const prior = previousOffsets.current.pop() ?? Math.max(0, offset - PAGE)
    setDetail(null)
    runPreview(nodeId, prior, requestPortId)
  }
  const choosePort = (portId: string) => {
    setPortSelection({ nodeId, portId })
    setDetail(null)
    setResultMode('sample')
  }
  const withOutputPorts = (content: ReactNode) => (
    <>
      <OutputPortSelector ports={outputPorts} outputs={runOutputs}
        selectedPortId={selectedPortId} onSelect={choosePort} />
      <SelectedOutputOutcome runStatus={run?.status?.status} output={selectedRunOutput} />
      {content}
    </>
  )

  if (!preview || preview.portId !== requestPortId) return withOutputPorts(<Skeleton />)
  if (!previewIsCurrent(preview, doc, nodeId, requestPortId)) {
    return withOutputPorts(<StalePreview onRefresh={() => runPreview(nodeId, 0, requestPortId)} />)
  }
  if (preview.loading) return withOutputPorts(<Skeleton />)
  if (preview.error) return withOutputPorts(<ErrorState reason={preview.error} onRetry={() => runPreview(nodeId, offset, requestPortId)} />)
  const res = preview.result!
  if (res.error) return withOutputPorts(<ErrorState reason={res.reason ?? 'preview failed'} onRetry={() => runPreview(nodeId, offset, requestPortId)} />)
  const isMetric = node?.type === 'metric'
  const isChart = node?.type === 'chart'
  const artifactPresentation: ArtifactPresentation | undefined = isChart
    ? {
        kind: 'chart',
        type: String(node?.data.config.chartType ?? 'bar'),
        xLabel: String(node?.data.config.x ?? 'x'),
        grouped: node?.data.config.agg !== 'none',
        yLabel: String(node?.data.config.agg && node?.data.config.agg !== 'none'
          ? `${node?.data.config.agg}(${node?.data.config.y ?? '*'})`
          : (node?.data.config.y ?? 'y')),
      }
    : isMetric ? { kind: 'metric' } : undefined
  const resultModeToggle = selectedOutput?.uri
    ? <ResultModeToggle mode={resultMode} onChange={setResultMode}
        fullLabel={selectedOutput.publicationKind === 'catalog' ? 'Published dataset' : 'Full result'} />
    : undefined
  if (res.notPreviewable) {
    // P0-UX-01: a sample can't preview this node (an aggregate/sort), but a full run MATERIALIZES the
    // result to a durable artifact — so if this node's last run is done and produced one, show the exact
    // Full result (restorable after a restart via the persisted run status) instead of a dead end.
    if (selectedOutput?.uri && resultMode === 'full') {
      return withOutputPorts(<FullResult uri={selectedOutput.uri}
        total={selectedOutput.publicationKind === 'result' ? selectedOutput.rows ?? null : null}
        runId={run?.status?.runId} nodeId={selectedOutput.nodeId} portId={selectedOutput.portId}
        publicationKind={selectedOutput.publicationKind}
        name={String(node?.data.title || node?.id || 'result')}
        modeToggle={resultModeToggle} presentation={artifactPresentation} />)
    }
    return withOutputPorts(<NotPreviewable reason={res.reason ?? 'needs a full pass'}
      onRun={() => requestRun(nodeId)} modeToggle={resultModeToggle} />)
  }
  if (selectedOutput?.uri && resultMode === 'full') {
    return withOutputPorts(<FullResult uri={selectedOutput.uri}
      total={selectedOutput.publicationKind === 'result' ? selectedOutput.rows ?? null : null}
      runId={run?.status?.runId} nodeId={selectedOutput.nodeId} portId={selectedOutput.portId}
      publicationKind={selectedOutput.publicationKind}
      name={String(node?.data.title || node?.id || 'result')}
      modeToggle={resultModeToggle} presentation={artifactPresentation} />)
  }

  const columns = res.columns
  const caps = capabilitiesFor(columns as ColumnSchema[])
  // gate the scalar/chart views on the NODE TYPE, not a column-name heuristic — otherwise any
  // 2-column dataset that happens to have columns named 'metric'+'value' was hijacked (F42).
  const special = isMetric || isChart
  const tabs = [{ id: 'rows', label: 'Rows' }, ...caps.map((c) => ({ id: c.id, label: c.label })), { id: 'stats', label: 'Stats' }]
  // a refresh may drop the capability whose tab was selected — fall back to Rows
  const activeTab = tab === 'rows' || tab === 'stats' || caps.some((c) => c.id === tab) ? tab : 'rows'
  const canTryNext = res.hasMore === true || (res.hasMore == null && res.rows.length > 0)

  return withOutputPorts(
    <div className="dp-dark text-foreground">
      {/* tab bar + row-count */}
      <div className="flex items-center gap-1.5 border-b border-border px-[11px] py-2">
        {!special && detail == null && tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              'rounded-md px-2.5 py-1 text-[11.5px] font-semibold',
              activeTab === t.id ? 'bg-primary/10 text-primary' : 'text-muted-foreground',
            )}
          >
            {t.label}
          </button>
        ))}
        {detail != null && (
          <button onClick={() => setDetail(null)} className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11.5px] font-semibold text-primary">
            <Icon name="chevronLeft" size={12} /> Row {offset + detail + 1}
          </button>
        )}
        {selectedOutput?.uri && detail == null && (
          resultModeToggle
        )}
        <span className="flex-1" />
        {!special && detail == null && activeTab !== 'stats' && (
          <>
            <span className="text-[10.5px] text-muted-foreground">
              {pageRangeLabel('rows', offset, res.rows.length)}
            </span>
            {activeTab === 'rows' && (
              <span className="ml-1 inline-flex gap-0.5">
                <PageBtn dir="prev" disabled={offset === 0} onClick={previousPage} />
                <PageBtn dir="next" disabled={!canTryNext} onClick={() => nextPage(res.rows.length)} />
              </span>
            )}
            {activeTab === 'rows' && res.rows.length > 0 && (
              <ExportCluster columns={columns as ColumnSchema[]} rows={res.rows}
                name={String(node?.data.title || node?.id || 'data')} offset={offset}
                scope="preview" sampleProvenance={res.sampleProvenance} pushToast={pushToast} />
            )}
          </>
        )}
      </div>

      <DataScopeBanner data={res} offset={offset} unit={isChart ? 'points' : 'rows'} scope="preview"
        allowNextAttempt={!special} />

      {isChart ? (
        <ChartView rows={res.rows} type={String(node?.data.config.chartType ?? 'bar')}
          grouped={node?.data.config.agg !== 'none'} completeness={res.completeness}
          total={res.rowCount ?? null} scope="preview"
          xLabel={String(node?.data.config.x ?? 'x')}
          yLabel={String(node?.data.config.agg && node?.data.config.agg !== 'none' ? `${node?.data.config.agg}(${node?.data.config.y ?? '*'})` : (node?.data.config.y ?? 'y'))} />
      ) : isMetric ? (
        <MetricValue rows={res.rows} />
      ) : detail != null && res.rows[detail] ? (
        <RowDetail columns={columns as ColumnSchema[]} row={res.rows[detail]} />
      ) : activeTab === 'rows' ? (
        <>
          {/* an empty result over a PREVIEWED SAMPLE isn't necessarily 'nothing matches' — a selective
              filter whose matches are past the scanned prefix reads as empty. Say so, don't mislead. */}
          {res.rows.length === 0 && res.truncated && offset === 0 && node?.type !== 'source' && node?.type !== 'note' && (
            <div className="border-b border-border px-3 py-2 text-[11px] leading-snug text-muted-foreground">
              No rows in the previewed sample. A selective step can match rows beyond the sampled prefix —
              run this node to check the full dataset.
            </div>
          )}
          <RowsTable columns={columns as ColumnSchema[]} rows={res.rows} onRowClick={setDetail} />
        </>
      ) : activeTab === 'stats' ? (
        <StatsView key={`${nodeId}:${requestPortId ?? ''}:${outputPorts.length > 1 ? 'multi' : 'single'}`}
          nodeId={nodeId} portId={requestPortId} multiOutput={outputPorts.length > 1} />
      ) : (
        (() => {
          const cap = caps.find((c) => c.id === activeTab)
          const Tab = cap?.viewerTab
          return Tab ? <Tab columns={columns as ColumnSchema[]} rows={res.rows} /> : null
        })()
      )}
    </div>,
  )
}

function OutputPortSelector({ ports, outputs, selectedPortId, onSelect }: {
  ports: PortSpec[]
  outputs: RunOutput[]
  selectedPortId?: string
  onSelect: (portId: string) => void
}) {
  if (ports.length <= 1) return null
  return (
    <div className="dp-dark flex items-center gap-1.5 border-b border-border px-[11px] py-2 text-foreground">
      <span className="mr-1 text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground">Output</span>
      <div role="group" aria-label="Output ports" className="flex min-w-0 items-center gap-1 overflow-x-auto">
        {ports.map((port) => {
          const output = outputs.find((candidate) => candidate.portId === port.id)
          const label = port.label || port.id
          const title = port.label && port.label !== port.id ? `${port.label} (${port.id})` : port.id
          return (
            <button key={port.id} aria-label={label} aria-pressed={selectedPortId === port.id}
              title={output ? `${title} · ${output.outcome}` : title}
              onClick={() => onSelect(port.id)}
              className={cn(
                'dp-mono inline-flex items-center gap-1.5 whitespace-nowrap rounded-md border px-2 py-1 text-[10.5px] font-semibold',
                selectedPortId === port.id
                  ? 'border-primary/40 bg-primary/10 text-primary'
                  : 'border-transparent text-muted-foreground hover:border-border hover:text-foreground',
              )}>
              {label}
              {output && <OutputOutcomeBadge outcome={output.outcome} />}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function SelectedOutputOutcome({ runStatus, output }: { runStatus?: RunState; output?: RunOutput }) {
  if (!runStatus && !output) return null
  const label = output?.portLabel || output?.portId
  return (
    <div aria-label="Selected output status"
      className="dp-dark border-b border-border px-[11px] py-1.5 text-[10.5px] text-muted-foreground">
      <div className="flex flex-wrap items-center gap-1.5">
        <span>Latest run</span>
        <span className={cn(
          'rounded px-1 py-px text-[9px] font-semibold uppercase tracking-[0.3px]',
          runStatus === 'done' ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
            : runStatus === 'failed' ? 'bg-destructive/10 text-destructive'
              : 'bg-muted text-muted-foreground',
        )}>{runStatus}</span>
        {output && (
          <>
            <span>·</span>
            <span className="dp-mono font-semibold text-foreground">{label}</span>
            <OutputOutcomeBadge outcome={output.outcome} />
            {output.rows != null && (
              <span>
                {output.rows.toLocaleString()} {output.rows === 1 ? 'row' : 'rows'}
                {output.publicationKind === 'catalog' ? ' written' : ''}
              </span>
            )}
          </>
        )}
      </div>
      {output?.error && <div className="dp-mono mt-1 whitespace-pre-wrap text-destructive">{output.error}</div>}
    </div>
  )
}

function OutputOutcomeBadge({ outcome }: { outcome: RunOutput['outcome'] }) {
  return (
    <span className={cn(
      'shrink-0 rounded px-1 py-px text-[8.5px] font-semibold uppercase tracking-[0.3px]',
      outcome === 'committed' ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
        : outcome === 'failed' ? 'bg-destructive/10 text-destructive'
          : 'bg-muted text-muted-foreground',
    )}>{outcome}</span>
  )
}

function StalePreview({ onRefresh }: { onRefresh: () => void }) {
  return (
    <div role="status" className="flex flex-col items-start gap-2 px-4 py-5 text-[12px] text-muted-foreground">
      <span className="font-medium text-foreground">Preview out of date</span>
      <span>The graph changed after these rows were fetched. Refresh to inspect the current result.</span>
      <Button size="sm" onClick={onRefresh}><Icon name="refresh" size={13} /> Refresh preview</Button>
    </div>
  )
}

function PageBtn({ dir, disabled, onClick }: { dir: 'prev' | 'next'; disabled: boolean; onClick: () => void }) {
  return (
    <button aria-label={dir === 'prev' ? 'Previous page' : 'Next page'} onClick={onClick} disabled={disabled}
      className={cn(
        'grid h-5 w-[22px] place-items-center rounded-[5px]',
        disabled ? 'cursor-default text-muted-foreground/40' : 'cursor-pointer text-muted-foreground',
      )}>
      <Icon name={dir === 'prev' ? 'chevronLeft' : 'chevronRight'} size={13} />
    </button>
  )
}

function DataScopeBanner({ data, offset, unit, scope, allowNextAttempt = true }: {
  data: SampleResult
  offset: number
  unit: 'rows' | 'points' | 'groups'
  scope: 'preview' | 'full-result' | 'published-dataset'
  allowNextAttempt?: boolean
}) {
  const end = offset + data.rows.length
  const total = data.rowCount ?? null
  const sourceCapped = data.limitScope === 'each-source' || data.limitReason === 'preview-scan'
  const provenance = data.sampleProvenance
  const provenanceCounts = provenance
    ? `Requested ${provenance.requestedRows.toLocaleString()} rows · scanned ${provenance.scannedRows?.toLocaleString() ?? 'unknown'} · returned ${provenance.returnedRows.toLocaleString()} · total ${provenance.totalRows?.toLocaleString() ?? 'unknown'}.`
    : null
  const resultCapped = data.limitScope === 'result-window'
    || data.limitReason === 'interactive-row-budget'
    || (data.completeness === 'capped' && !sourceCapped)
  const label = scope === 'preview' ? (provenance?.strategy === 'reservoir' ? 'Random sample' : 'Preview prefix')
    : scope === 'published-dataset' ? 'Published dataset' : 'Full result artifact'
  const range = pageRangeLabel(unit, offset, data.rows.length)
  let detail: string
  if (scope === 'preview') {
    detail = provenance?.strategy === 'reservoir'
      ? `${range} · Deterministic reservoir sample.`
      : `${range} · Full dataset not scanned.`
  } else if (total == null) {
    detail = `Current page · ${range} · Total ${unit} unknown.`
  } else if (data.completeness === 'complete') {
    detail = `Complete artifact · ${total.toLocaleString()} ${unit}.`
  } else {
    detail = `Current page · ${range} of ${total.toLocaleString()}.`
  }
  return (
    <div role="status" className="border-b border-border bg-muted/30 px-[11px] py-1.5 text-[10.5px] text-muted-foreground">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="rounded bg-muted px-1.5 py-px font-semibold text-foreground">{label}</span>
        <span>{detail}</span>
      </div>
      {sourceCapped && (
        <div className="mt-1 font-medium text-amber-700 dark:text-amber-300">
          Each source read was limited to at most {(data.rowLimit ?? end).toLocaleString()} rows before this preview was computed.
          {' '}Output rows are not necessarily the first {(data.rowLimit ?? end).toLocaleString()} rows of the final result.
        </div>
      )}
      {provenance && (
        <div className="mt-1 space-y-0.5 text-muted-foreground">
          <div>{provenance.strategy === 'reservoir' ? `Reservoir sample · seed ${provenance.seed}.` : 'Prefix preview.'} {provenanceCounts}</div>
          <div className="break-all">Input {provenance.datasetIdentity ?? 'unknown'} · revision {provenance.datasetRevision ?? 'unknown'}.</div>
          {provenance.limitations.map((limitation) => <div key={limitation}>{limitation}</div>)}
        </div>
      )}
      {resultCapped && (
        <div className="mt-1 font-medium text-amber-700 dark:text-amber-300">
          Interactive view stopped at {(data.rowLimit ?? end).toLocaleString()} {unit}
          {total != null ? ` of ${total.toLocaleString()}` : '; total is unknown'}.
          {' '}{scope === 'preview'
            ? 'Run the node to materialize and inspect the complete result.'
            : scope === 'published-dataset'
              ? 'The published dataset retains rows beyond this interactive window.'
              : 'The committed artifact retains the complete result.'}
        </div>
      )}
      {data.hasMore == null && (
        <div className="mt-1 font-medium text-muted-foreground">
          Next page availability unknown{allowNextAttempt && data.rows.length > 0 ? ' · You can try the next offset.' : '.'}
        </div>
      )}
    </div>
  )
}

function pageRangeLabel(unit: 'rows' | 'points' | 'groups', offset: number, count: number) {
  if (count === 0) return offset === 0 ? `No ${unit} returned` : `No ${unit} at offset ${offset.toLocaleString()}`
  return `${unit} ${(offset + 1).toLocaleString()}–${(offset + count).toLocaleString()}`
}

function ResultModeToggle({ mode, onChange, fullLabel = 'Full result' }: {
  mode: 'sample' | 'full'; onChange: (mode: 'sample' | 'full') => void; fullLabel?: string
}) {
  return (
    <div className="flex items-center gap-0.5 rounded-md border border-border p-0.5 text-[10px]">
      {(['sample', 'full'] as const).map((value) => (
        <button key={value} onClick={() => onChange(value)} aria-pressed={mode === value}
          className={`rounded px-1.5 py-0.5 ${mode === value ? 'bg-muted font-semibold text-foreground' : 'text-muted-foreground'}`}>
          {value === 'sample' ? 'Preview sample' : fullLabel}
        </button>
      ))}
    </div>
  )
}

const fmtNum = (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: 3 })

// Per-column stats over the previewed sample (null%/distinct/min/max/mean). Whole-dataset stats are a
// cancellable job: every row is covered, while distinct remains approximate.
function StatsView({ nodeId, portId, multiOutput }: { nodeId: string; portId?: string; multiOutput: boolean }) {
  const doc = useStore((s) => s.doc)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const currentUserId = useStore((s) => s.currentUser?.id)
  const profileJob = useStore((s) => s.profileJobs[nodeId])
  const prepareFullProfile = useStore((s) => s.prepareFullProfile)
  const startFullProfile = useStore((s) => s.startFullProfile)
  const cancelFullProfile = useStore((s) => s.cancelFullProfile)
  const [full, setFull] = useState(false)
  const fullProfileUnavailableId = useId()
  const planIdentity = previewPlanIdentity(doc, nodeId, portId)
  const sampleRequestGeneration = useRef(0)
  const [sampleState, setSampleState] = useState<{
    planIdentity: string; loading: boolean; res?: ProfileResult; err?: string
  }>({ planIdentity, loading: true })
  const loadSample = () => {
    const requestDoc = doc
    const requestIdentity = previewPlanIdentity(requestDoc, nodeId, portId)
    const requestGeneration = ++sampleRequestGeneration.current
    setSampleState({ planIdentity: requestIdentity, loading: true })
    api.profile(requestDoc, nodeId, portId)
      .then((res) => {
        if (sampleRequestGeneration.current !== requestGeneration
            || previewPlanIdentity(useStore.getState().doc, nodeId, portId) !== requestIdentity) return
        setSampleState({ planIdentity: requestIdentity, loading: false, res })
      })
      .catch((e) => {
        if (sampleRequestGeneration.current !== requestGeneration
            || previewPlanIdentity(useStore.getState().doc, nodeId, portId) !== requestIdentity) return
        setSampleState({ planIdentity: requestIdentity, loading: false, err: e?.message ?? String(e) })
      })
  }
  useEffect(() => {
    if (!full) loadSample()
    return () => { sampleRequestGeneration.current += 1 }
  }, [nodeId, portId, full, planIdentity])  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => setFull(false), [nodeId, portId, multiOutput])
  // Never paint a sample-profile response bound to another node or execution plan, even for the render
  // before the effect above starts its replacement request.
  const st = sampleState.planIdentity === planIdentity ? sampleState : { planIdentity, loading: true }
  const job = currentUserId && profileJob?.principalId === currentUserId
      && profileJobIsCurrent(profileJob, doc, nodeId)
    ? profileJob
    : undefined
  const selectMode = (v: boolean) => {
    setFull(v)
  }
  const toggle = (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-1 rounded-md border border-border p-0.5 text-[10px]">
        {([['sample', false], ['full dataset', true]] as const).map(([label, v]) => (
          <button key={label} onClick={() => selectMode(v)} disabled={v && multiOutput}
            aria-describedby={v && multiOutput ? fullProfileUnavailableId : undefined}
            title={v && !multiOutput && !canEdit ? 'View full-dataset profile results' : undefined}
            className={`rounded px-1.5 py-0.5 disabled:cursor-not-allowed disabled:opacity-45 ${full === v ? 'bg-muted font-semibold text-foreground' : 'text-muted-foreground'}`}>
            {label}
          </button>
        ))}
      </div>
      {multiOutput && (
        <span id={fullProfileUnavailableId} className="max-w-[260px] text-right text-[9.5px] leading-snug text-muted-foreground">
          Whole-dataset profiles are not available for multi-output nodes. Inspect each port’s sample statistics instead.
        </span>
      )}
    </div>
  )
  if (full) {
    if (!job || job.phase === 'cancelled') {
      return <FullProfilePrompt toggle={toggle}
        onEstimate={() => prepareFullProfile(nodeId)} disabled={!canEdit} />
    }
    if (job.phase === 'preflight') {
      return canEdit
        ? <FullProfilePreflight job={job} toggle={toggle} onStart={() => startFullProfile(nodeId)} />
        : <FullProfilePrompt toggle={toggle} onEstimate={() => {}} disabled />
    }
    if (job.phase === 'verifying') {
      const activeRun = job.status?.status === 'queued' || job.status?.status === 'running'
      return <FullProfileProgress job={job} toggle={toggle}
        onCancel={canEdit && job.canCancel === true && activeRun ? () => cancelFullProfile(nodeId) : undefined} />
    }
    if (job.phase === 'estimating' || job.phase === 'queued' || job.phase === 'running' || job.phase === 'cancelling') {
      return <FullProfileProgress job={job} toggle={toggle}
        onCancel={canEdit ? () => cancelFullProfile(nodeId) : undefined} />
    }
    if (job.phase === 'failed') {
      const activeRun = job.status && (job.status.status === 'queued' || job.status.status === 'running')
      const retry = job.submissionUnresolved
        ? () => startFullProfile(nodeId)
        : () => prepareFullProfile(nodeId)
      return <div><div className="flex justify-end px-[11px] py-1.5">{toggle}</div><ErrorState
        title={job.identityVerified === false ? 'Full profile not verified' : 'Full profile failed'}
        reason={job.error ?? 'full profile failed'}
        onRetry={canEdit ? retry : undefined}
        onCancel={canEdit && activeRun ? () => cancelFullProfile(nodeId) : undefined} /></div>
    }
    const res = job.identityVerified === false ? undefined : job.status?.profile
    if (!res) return <div><div className="flex justify-end px-[11px] py-1.5">{toggle}</div><ErrorState title="Full profile failed" reason="full profile completed without statistics" onRetry={canEdit ? () => prepareFullProfile(nodeId) : undefined} /></div>
    return <ProfileTable res={res} toggle={toggle} />
  }
  if (st.loading) return <div><div className="flex justify-end px-[11px] py-1.5">{toggle}</div><Skeleton /></div>
  if (st.err) return <div><div className="flex justify-end px-[11px] py-1.5">{toggle}</div><ErrorState reason={st.err} onRetry={loadSample} /></div>
  const res = st.res!
  if (res.error) return <div><div className="flex justify-end px-[11px] py-1.5">{toggle}</div><ErrorState reason={res.reason ?? 'profile failed'} onRetry={loadSample} /></div>
  if (res.notPreviewable) return <NotPreviewable
    reason={`${res.reason ?? 'Sample statistics need a full pass.'} Switch to full dataset to estimate a whole-dataset profile.`}
    modeToggle={toggle} />
  return <ProfileTable res={res} toggle={toggle} />
}

function FullProfilePrompt({ toggle, onEstimate, disabled = false }: {
  toggle: ReactNode; onEstimate: () => void; disabled?: boolean
}) {
  return (
    <div className="px-5 py-6 text-center">
      <div className="mb-1 text-[12px] font-semibold text-foreground">Whole-dataset profile</div>
      <p className="mb-3 text-[11px] text-muted-foreground">
        {disabled ? 'Editors can estimate and start full-dataset profile jobs.' : 'Estimate the scan first. Starting it is a separate choice.'}
      </p>
      <div className="flex items-center justify-center gap-2">
        <button onClick={onEstimate} disabled={disabled}
          className="rounded-md bg-primary px-2.5 py-1 text-[11px] font-semibold text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50">
          Estimate full profile
        </button>
        {toggle}
      </div>
    </div>
  )
}

function profileEstimateLabel(estimate?: { rows: number | null; bytes?: number | null }): string {
  const rows = estimate?.rows == null ? 'rows unknown' : `${estimate.rows.toLocaleString()} rows`
  if (estimate?.bytes == null) return `Estimated ${rows} · size unknown`
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  const unit = estimate.bytes > 0
    ? Math.min(Math.floor(Math.log(estimate.bytes) / Math.log(1024)), units.length - 1)
    : 0
  const bytes = (estimate.bytes / 1024 ** unit).toLocaleString(undefined, { maximumFractionDigits: 1 })
  return `Estimated ${rows} · ${bytes} ${units[unit]}`
}

function FullProfilePreflight({ job, toggle, onStart }: {
  job: { estimate?: { rows: number | null; bytes?: number | null; needsConfirm: boolean } }
  toggle: ReactNode
  onStart: () => void
}) {
  const confirmation = job.estimate?.needsConfirm
    ? 'Large or unknown scan — confirmation is required.'
    : 'Known-small scan.'
  return (
    <div className="px-5 py-6 text-center">
      <div className="mb-1 text-[12px] font-semibold text-foreground">Profile preflight</div>
      <p className="mb-1 text-[11px] text-muted-foreground">{profileEstimateLabel(job.estimate)} · {confirmation}</p>
      <p className="mb-3 text-[11px] text-muted-foreground">The job will scan every row; distinct counts are approximate.</p>
      <div className="flex items-center justify-center gap-2">
        <button onClick={onStart}
          className="rounded-md bg-primary px-2.5 py-1 text-[11px] font-semibold text-primary-foreground">
          Start whole-dataset profile
        </button>
        {toggle}
      </div>
    </div>
  )
}

function FullProfileProgress({ job, toggle, onCancel }: {
  job: { phase: string; estimate?: { rows: number | null; bytes?: number | null }; error?: string }; toggle: ReactNode; onCancel?: () => void
}) {
  const estimate = profileEstimateLabel(job.estimate)
  const label = job.phase === 'estimating' ? 'Estimating full profile…'
    : job.phase === 'queued' ? 'Full profile queued…'
      : job.phase === 'cancelling' ? 'Cancelling full profile…'
        : job.phase === 'verifying' ? 'Verifying recovered full profile…' : 'Full profile running…'
  return (
    <div className="px-5 py-6 text-center">
      <div className="mb-1 text-[12px] font-semibold text-foreground">{label}</div>
      <p className="mb-3 text-[11px] text-muted-foreground">
        {job.phase === 'verifying'
          ? 'Statistics remain hidden until verification completes.'
          : `${estimate} · whole-dataset scan; distinct counts are approximate`}
      </p>
      {job.error && <p role="status" className="mx-auto mb-3 max-w-[380px] text-[11px] text-destructive">{job.error}</p>}
      <div className="flex items-center justify-center gap-2">
        {onCancel && (
          <button onClick={onCancel} disabled={job.phase === 'cancelling' || job.phase === 'estimating'}
            className="rounded-md border border-border px-2.5 py-1 text-[11px] font-semibold text-foreground disabled:cursor-not-allowed disabled:opacity-50">
            Cancel
          </button>
        )}
        {toggle}
      </div>
    </div>
  )
}

function ProfileTable({ res, toggle }: { res: ProfileResult; toggle: ReactNode }) {
  const pct = (n: number) => (res.rowCount ? Math.round((n / res.rowCount) * 100) : 0)
  const wholeDataset = res.completeness === 'complete'
  const previewSample = res.completeness === 'sample'
  const completeSample = wholeDataset && !!res.sampleProvenance
  const scopeLabel = completeSample ? 'Complete sampled result' : wholeDataset ? 'Whole dataset' : previewSample ? 'Preview sample' : 'Profile scope unknown'
  const rowVerb = wholeDataset ? 'scanned' : previewSample ? 'inspected' : 'reported'
  return (
    <div className="max-h-[360px] overflow-auto">
      <div className="flex items-center justify-between px-[11px] py-1.5 text-[10.5px] text-muted-foreground">
        <div>
          <div className="font-medium text-foreground">
            {scopeLabel}
            {' · '}{res.rowCount.toLocaleString()} rows {rowVerb}
          </div>
          <div>
            {completeSample
              ? 'All rows of this sampled result were scanned; approximate distinct counts are marked ≈.'
              : wholeDataset
              ? 'All rows were scanned; approximate distinct counts are marked ≈.'
              : previewSample
                ? 'All metrics describe this preview sample only; approximate counts are marked ≈.'
                : 'The kernel did not report whether these statistics cover a sample or the whole dataset.'}
          </div>
          {res.sampleProvenance && <SampleProvenanceSummary provenance={res.sampleProvenance} />}
        </div>
        {toggle}
      </div>
      <table className="w-full text-[11.5px] tabular-nums">
        <thead className="sticky top-0 bg-card text-[10px] uppercase tracking-wide text-muted-foreground">
          <tr>{['Column', 'Type', 'Nulls', 'Distinct', 'Min', 'Max', 'Mean'].map((h) => (
            <th key={h} className="px-2 py-1 text-left font-semibold">{h}</th>
          ))}</tr>
        </thead>
        <tbody>
          {res.columns.map((c) => (
            <tr key={c.name} className="border-t border-border/60">
              <td className="px-2 py-1 font-medium text-foreground">{c.name}</td>
              <td className="px-2 py-1 text-muted-foreground">{c.type}</td>
              <td className="px-2 py-1 text-muted-foreground">{c.nulls ? `${c.nulls} · ${pct(c.nulls)}%` : '—'}</td>
              <td className="px-2 py-1 text-muted-foreground">
                {c.distinct == null ? '—' : c.distinctIsApproximate
                  ? <span aria-label={`Estimated distinct count: ${c.distinct.toLocaleString()}`}>≈ {c.distinct.toLocaleString()}</span>
                  : c.distinct.toLocaleString()}
              </td>
              <td className="max-w-[120px] truncate px-2 py-1 text-muted-foreground" title={c.min ?? ''}>{c.min ?? '—'}</td>
              <td className="max-w-[120px] truncate px-2 py-1 text-muted-foreground" title={c.max ?? ''}>{c.max ?? '—'}</td>
              <td className="px-2 py-1 text-muted-foreground">{c.mean != null ? fmtNum(c.mean) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function SampleProvenanceSummary({ provenance }: { provenance: SampleProvenance }) {
  const counts = `requested ${provenance.requestedRows.toLocaleString()} · scanned ${provenance.scannedRows?.toLocaleString() ?? 'unknown'} · returned ${provenance.returnedRows.toLocaleString()} · total ${provenance.totalRows?.toLocaleString() ?? 'unknown'}`
  return (
    <div className="max-w-[290px] text-right text-[9.5px] leading-snug text-muted-foreground">
      <div>{provenance.strategy === 'reservoir' ? 'Deterministic reservoir sample' : 'Bounded preview prefix'}{provenance.seed != null ? ` · seed ${provenance.seed}` : ''}</div>
      <div>{counts}</div>
      <div className="break-all">input {provenance.datasetIdentity ?? 'unknown'}</div>
      {provenance.datasetRevision && <div title={provenance.datasetRevision}>revision {provenance.datasetRevision}</div>}
      {provenance.limitations.map((limitation) => <div key={limitation}>{limitation}</div>)}
    </div>
  )
}

// Full detail for one row — every column with its full value (untruncated array / url / etc.).
function RowDetail({ columns, row }: { columns: ColumnSchema[]; row: Record<string, unknown> }) {
  return (
    <div className="max-h-[440px] overflow-auto py-1">
      {columns.map((c) => (
        <div key={c.name} className="flex gap-2.5 border-b border-border px-3 py-2">
          <div className="w-[130px] flex-[0_0_130px]">
            <div className="break-words text-[11.5px] font-semibold text-foreground">{c.name}</div>
            <div className="text-[9.5px] text-muted-foreground">{c.type}</div>
          </div>
          <div className="min-w-0 flex-1 text-[11.5px]">
            {c.capabilities.includes('media') && row[c.name] != null && (
              <img src={String(row[c.name])} loading="lazy" className="mb-1.5 block max-h-[140px] max-w-[200px] rounded-md bg-muted" onError={(e) => (e.currentTarget.style.display = 'none')} />
            )}
            <div className="dp-mono whitespace-pre-wrap break-words text-foreground">
              {row[c.name] == null ? '·' : typeof row[c.name] === 'object' ? JSON.stringify(row[c.name], null, 2) : String(row[c.name])}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

// a scalar numeric column → right-align its cells (matches the Stats tab; eases scanning). Lists excluded.
const isNumericCol = (t: string) => !t.includes('[]') && /\b(?:u?int\w*|bigint|smallint|tinyint|hugeint|long|float\w*|double|real|decimal|numeric)\b/i.test(t)

// --- previewed-rows export (client-side; the rows are already in memory) --------------------------
function _csvCell(v: unknown): string {
  if (v == null) return ''
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v)  // list/struct cells → JSON
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}
function rowsToCsv(cols: ColumnSchema[], rows: Record<string, unknown>[]): string {
  const head = cols.map((c) => _csvCell(c.name)).join(',')
  return [head, ...rows.map((r) => cols.map((c) => _csvCell(r[c.name])).join(','))].join('\n')
}
function _slug(s: string): string { return (s.replace(/[^\w.-]+/g, '_').replace(/^_+|_+$/g, '') || 'data') }
function _download(name: string, text: string, mime: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: mime }))
  const a = document.createElement('a')
  a.href = url; a.download = name; a.click()
  URL.revokeObjectURL(url)
}

function ExportCluster({ columns, rows, name, offset, scope, sampleProvenance, pushToast }: {
  columns: ColumnSchema[]; rows: Record<string, unknown>[]; name: string; offset: number
  scope: 'preview' | 'full-result' | 'published-dataset'
  sampleProvenance?: SampleProvenance | null
  pushToast: (m: string, k?: 'error' | 'info' | 'success') => void
}) {
  const start = rows.length ? offset + 1 : 0
  const end = offset + rows.length
  const range = `rows ${start}–${end}`
  const fileBase = `${_slug(name)}-${scope}-page-${start}-${end}`
  const scopeLabel = scope === 'preview' ? 'preview page'
    : scope === 'published-dataset' ? 'published dataset page' : 'full-result page'
  const copy = () => {
    // navigator.clipboard is undefined in an insecure context (plain http on a LAN IP — a supported
    // `--host 0.0.0.0` deployment), where `.writeText` would throw synchronously past the .catch.
    if (!navigator.clipboard) { pushToast('Copy failed — clipboard needs https or localhost', 'error'); return }
    navigator.clipboard.writeText(rowsToCsv(columns, rows))
      .then(() => pushToast(`Copied ${scopeLabel} (${range}) as CSV.`, 'success'))
      .catch(() => pushToast('Copy failed — clipboard unavailable', 'error'))
  }
  const exportPage = (format: 'CSV' | 'JSON') => {
    if (format === 'CSV') {
      _download(`${fileBase}.csv`, rowsToCsv(columns, rows), 'text/csv')
    } else {
      _download(`${fileBase}.json`, JSON.stringify(rows, null, 2), 'application/json')
    }
    if (scope === 'preview' && sampleProvenance) {
      _download(`${fileBase}.provenance.json`, JSON.stringify({ sampleProvenance }, null, 2), 'application/json')
    }
    pushToast(`Exported ${scopeLabel} (${range}) as ${format}.${scope === 'preview' ? ' This is not the full result.' : ''}`, 'success')
  }
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button aria-label={`Export this ${scopeLabel}`}
          className="ml-1.5 inline-flex items-center gap-1 rounded border-l border-border px-1.5 py-1 text-[10.5px] font-semibold text-muted-foreground hover:bg-accent hover:text-foreground">
          Export this page <Icon name="chevronDown" size={11} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-[220px]">
        <DropdownMenuItem onSelect={copy}>Copy {scopeLabel} as CSV</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => exportPage('CSV')}>Download {scopeLabel} as CSV</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => exportPage('JSON')}>Download {scopeLabel} as JSON</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function RowsTable({ columns, rows, onRowClick }: { columns: ColumnSchema[]; rows: Record<string, unknown>[]; onRowClick: (i: number) => void }) {
  return (
    <div className="max-h-[440px] overflow-auto">
      <table className="w-full border-collapse text-[11px]">
        <thead>
          <tr>
            {columns.map((c) => {
              const num = isNumericCol(c.type)
              return (
                <th key={c.name} className={cn('sticky top-0 whitespace-nowrap border-b border-border bg-muted px-2.5 py-[6px] font-semibold text-muted-foreground', num ? 'text-right' : 'text-left')}>
                  <div className={cn('flex items-center', num && 'justify-end')}>
                    {c.name}
                    {c.capabilities.includes('media') && <span title="media column — thumbnails in the Media tab" className="ml-[5px] cursor-help opacity-60">▦</span>}
                    {c.capabilities.includes('vector') && <span title="vector / embedding column" className="ml-[5px] cursor-help opacity-60">⋮⋮</span>}
                  </div>
                  <div className="dp-mono text-[9px] font-normal lowercase tracking-tight opacity-55" title={c.type}>{c.type}</div>
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} onClick={() => onRowClick(i)} title="Click for row detail"
              className="cursor-pointer border-b border-border hover:bg-muted">
              {columns.map((c) => (
                <td key={c.name} className={cn('max-w-[260px] overflow-hidden text-ellipsis whitespace-nowrap px-2.5 py-1.5', c.type.includes('[]') && 'dp-mono', isNumericCol(c.type) && 'text-right tabular-nums')}>
                  <Cell col={c} value={r[c.name]} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Cell({ col, value }: { col: ColumnSchema; value: unknown }) {
  if (value == null) return <span className="text-muted-foreground/60">·</span>
  if (col.capabilities.includes('media')) {
    const url = String(value)
    return (
      <span className="inline-flex items-center gap-1.5">
        <img src={url} loading="lazy" className="h-6 w-[34px] rounded-sm bg-muted object-cover" onError={(e) => (e.currentTarget.style.display = 'none')} />
        <span className="max-w-[150px] overflow-hidden text-ellipsis text-muted-foreground">{url.split('/').slice(-1)[0]}</span>
      </span>
    )
  }
  if (col.capabilities.includes('vector') && Array.isArray(value)) {
    return <span className="rounded bg-primary/10 px-1.5 py-px text-[10px] font-semibold text-primary">[{(value as number[]).length}]</span>
  }
  // a MAP arrives as [[k,v],…] — an array, but it's a struct-like value, so show its JSON, not a [N] badge
  if (col.type === 'map') return <span className="dp-mono">{JSON.stringify(value)}</span>
  if (Array.isArray(value)) return <span>[{value.length}]</span>
  if (value === true) return <span className="text-[#2f9e5f]">true</span>
  if (value === false) return <span className="text-destructive">false</span>
  if (typeof value === 'object') return <span className="dp-mono">{JSON.stringify(value)}</span>  // struct/map — not [object Object]
  return <span>{String(value)}</span>
}

// A dependency-free SVG chart of the (x, y) series the `chart` node emits — bar / line / area /
// scatter. Colors are theme tokens so it works in dark mode; the axis labels come from the node's
// chosen columns. Kept simple on purpose (the heavy lifting — grouping/aggregation — is server-side).
function ChartView({ rows, type, xLabel, yLabel, grouped = false, completeness = 'unknown', total = null, scope = 'preview' }: {
  rows: Record<string, unknown>[]
  type: string
  xLabel: string
  yLabel: string
  grouped?: boolean
  completeness?: SampleResult['completeness']
  total?: number | null
  scope?: 'preview' | 'full-result' | 'published-dataset'
}) {
  const pts = rows.flatMap((row) => {
    const raw = row.y
    if (raw == null || (typeof raw === 'string' && raw.trim() === '')) return []
    if (typeof raw !== 'number' && typeof raw !== 'string') return []
    const y = Number(raw)
    return Number.isFinite(y) ? [{ x: row.x, y }] : []
  })
  const omitted = rows.length - pts.length
  const omittedMessage = omitted === 1
    ? '1 Y value omitted because it was null, blank, or non-numeric.'
    : `${omitted.toLocaleString()} Y values omitted because they were null, blank, or non-numeric.`
  if (!pts.length) return (
    <div role="status" className="px-4 py-10 text-center text-[12px] text-muted-foreground">
      <div className="font-medium text-foreground">No numeric Y values to chart.</div>
      {omitted > 0 && <div className="mt-1">{omittedMessage}</div>}
    </div>
  )
  const W = 640, H = 320, padL = 48, padR = 16, padT = 16, padB = 44
  const plotW = W - padL - padR, plotH = H - padT - padB
  const ys = pts.map((p) => p.y)
  // bar/area fill to the zero baseline (0 must be in range); line/scatter scale to the DATA range so
  // a far-from-zero or all-negative series isn't squashed into a flat band at one edge.
  const baseline = type === 'bar' || type === 'area'
  const dMax = Math.max(...ys), dMin = Math.min(...ys)
  const yMax = baseline ? Math.max(0, dMax) : dMax, yMin = baseline ? Math.min(0, dMin) : dMin
  const ySpan = (yMax - yMin) || 1
  const yPix = (v: number) => padT + plotH - ((v - yMin) / ySpan) * plotH
  const y0 = yPix(Math.min(Math.max(0, yMin), yMax))  // 0 clamped into the plotted range → the baseline row
  const numX = pts.every((p) => typeof p.x === 'number')
  const xs = pts.map((p) => Number(p.x)), xMin = Math.min(...xs), xMax = Math.max(...xs), xSpan = xMax - xMin || 1
  const xPix = (i: number) => (type === 'scatter' && numX)
    ? padL + ((xs[i] - xMin) / xSpan) * plotW
    : (pts.length === 1 ? padL + plotW / 2 : padL + (i / (pts.length - 1)) * plotW)
  const fmt = (v: number) => (Math.abs(v) >= 1000 || (v !== 0 && Math.abs(v) < 0.01) ? v.toExponential(1) : (Math.round(v * 100) / 100).toString())
  const line = pts.map((p, i) => `${xPix(i)},${yPix(p.y)}`).join(' ')
  const barW = Math.max(2, (plotW / pts.length) * 0.7)
  const tickIdx = Array.from(new Set([0, ...Array.from({ length: Math.min(8, pts.length) }, (_, k) => Math.round(k * (pts.length - 1) / Math.max(1, Math.min(8, pts.length) - 1)))]))
  const unit = grouped ? 'group' : 'point'
  const units = pts.length === 1 ? unit : `${unit}s`
  const scopeSummary = completeness === 'capped'
    ? `Showing ${pts.length.toLocaleString()}${total != null ? ` of ${total.toLocaleString()}` : ''} ${units} · display capped`
    : scope === 'preview'
      ? `${pts.length.toLocaleString()} ${units} · Preview sample; full dataset not scanned`
      : completeness === 'complete'
        ? `${pts.length.toLocaleString()} ${units} · ${scope === 'published-dataset' ? 'Complete published dataset' : 'Complete full result'}`
        : `${pts.length.toLocaleString()} ${units} · Total ${units} unknown`
  const ariaScope = completeness === 'capped'
    ? `showing ${pts.length} capped ${units}`
    : completeness === 'complete' ? `complete ${units}` : `${units}, completeness unknown`

  return (
    <div className="p-3">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: 340 }} role="img" aria-label={`${type} chart, ${ariaScope}`}>
        {/* y axis: zero/baseline + min/max labels */}
        <line x1={padL} y1={padT} x2={padL} y2={padT + plotH} stroke="hsl(var(--border))" />
        <line x1={padL} y1={y0} x2={W - padR} y2={y0} stroke="hsl(var(--border))" />
        {[yMax, yMin].map((v, k) => (
          <text key={k} x={padL - 6} y={yPix(v) + 3} textAnchor="end" fontSize="10" fill="hsl(var(--muted-foreground))">{fmt(v)}</text>
        ))}
        {(type === 'bar') && pts.map((p, i) => (
          <rect key={i} x={xPix(i) - barW / 2} y={Math.min(yPix(p.y), y0)} width={barW}
            height={Math.abs(yPix(p.y) - y0)} fill="hsl(var(--primary))" opacity={0.85} />
        ))}
        {(type === 'area') && <polygon points={`${padL},${y0} ${line} ${xPix(pts.length - 1)},${y0}`} fill="hsl(var(--primary))" opacity={0.2} />}
        {(type === 'line' || type === 'area') && <polyline points={line} fill="none" stroke="hsl(var(--primary))" strokeWidth={1.75} />}
        {(type === 'scatter' || type === 'line' || type === 'area') && pts.map((p, i) => (
          <circle key={i} cx={xPix(i)} cy={yPix(p.y)} r={type === 'scatter' ? 3 : 2.2} fill="hsl(var(--primary))" opacity={0.85} />
        ))}
        {/* x tick labels */}
        {tickIdx.map((i) => (
          <text key={i} x={xPix(i)} y={padT + plotH + 16} textAnchor="middle" fontSize="10" fill="hsl(var(--muted-foreground))">
            {String(pts[i]?.x).slice(0, 10)}
          </text>
        ))}
        <text x={padL + plotW / 2} y={H - 4} textAnchor="middle" fontSize="10.5" fill="hsl(var(--muted-foreground))" fontWeight="600">{xLabel}</text>
      </svg>
      <div className="mt-1 text-center text-[10.5px] text-muted-foreground">{yLabel} vs {xLabel} · {scopeSummary}</div>
      {omitted > 0 && (
        <div role="status" className="mt-1 text-center text-[10.5px] font-medium text-amber-700 dark:text-amber-300">
          {omittedMessage}
        </div>
      )}
    </div>
  )
}

function MetricValue({ rows }: { rows: Record<string, unknown>[] }) {
  const v = rows[0]?.value
  return (
    <div className="px-4 py-7 text-center">
      <div className="text-[34px] font-bold text-foreground">{typeof v === 'number' ? v.toLocaleString() : String(v)}</div>
      <div className="mt-1.5 text-[11px] text-muted-foreground">{String(rows[0]?.metric ?? 'metric')} · over the full dataset</div>
    </div>
  )
}

function Skeleton() {
  return (
    <div className="dp-dark p-4">
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="my-2.5 h-3 rounded bg-muted" style={{ width: `${90 - i * 8}%`, animation: 'dp-pulse 1.2s infinite' }} />
      ))}
    </div>
  )
}

function ErrorState({ title = 'Preview failed', reason, onRetry, onCancel }: {
  title?: string; reason: string; onRetry?: () => void; onCancel?: () => void
}) {
  return (
    <div className="dp-dark px-5 py-6 text-center text-muted-foreground">
      <div className="mb-3 inline-grid h-10 w-10 place-items-center rounded-[10px] bg-destructive/10 text-destructive">
        <Icon name="close" size={18} />
      </div>
      <div className="text-[13px] font-semibold text-destructive">{title}</div>
      <div className="dp-mono mx-auto mt-2 max-w-[380px] whitespace-pre-wrap rounded-lg border border-destructive/20 bg-destructive/10 p-2.5 text-left text-[11px] leading-normal text-muted-foreground">{reason}</div>
      {(onRetry || onCancel) && <div className="mt-3.5 flex items-center justify-center gap-2">
        {onCancel && <Button variant="outline" size="sm" onClick={onCancel}>Cancel run</Button>}
        {onRetry && <Button variant="outline" size="sm" onClick={onRetry}>Retry</Button>}
      </div>}
    </div>
  )
}

// Page a durable run output through its server-owned run/node/port identity. The kernel resolves the
// URI after authorization, so a stale or tampered client URI cannot redirect this result view.
type ArtifactPresentation =
  | { kind: 'chart'; type: string; xLabel: string; yLabel: string; grouped: boolean }
  | { kind: 'metric' }

export function FullResult({
  uri, total, runId, nodeId, portId, publicationKind, name = 'result', modeToggle, presentation,
}: {
  uri: string
  total: number | null
  runId?: string
  nodeId?: string
  portId?: string
  publicationKind?: RunOutput['publicationKind']
  name?: string
  modeToggle?: ReactNode
  presentation?: ArtifactPresentation
}) {
  const [data, setData] = useState<import('../types/api').SampleResult | null>(null)
  const [err, setErr] = useState<(Error & { status?: number }) | null>(null)
  const [detail, setDetail] = useState<number | null>(null)
  const [offset, setOffset] = useState(0)
  const previousOffsets = useRef<number[]>([])
  const [retry, setRetry] = useState(0)
  const [exporting, setExporting] = useState(false)
  const pushToast = useStore((s) => s.pushToast)
  const pageSize = presentation?.kind === 'chart' ? CHART_DISPLAY_LIMIT : PAGE
  const publishedDataset = publicationKind === 'catalog'
  const viewLabel = publishedDataset ? 'Published dataset' : 'Full result'
  const pageScope = publishedDataset ? 'published-dataset' as const : 'full-result' as const
  const hasRunIdentity = !!runId && !!nodeId && !!portId
  const canExportFull = publicationKind === 'result' && hasRunIdentity
  const reportedTotal = data?.rowCount ?? (publicationKind === 'result' ? total : null) ?? null

  useEffect(() => {
    previousOffsets.current = []
    setOffset(0)
  }, [uri, runId, nodeId, portId])
  useEffect(() => {
    let live = true
    setData(null); setErr(null); setDetail(null)
    if (!runId || !nodeId || !portId) return () => { live = false }
    api.runOutputSample(runId, nodeId, portId, pageSize, offset)
      .then((r) => { if (live) setData(r) })
      .catch((e) => { if (live) setErr(e instanceof Error ? e : new Error(String(e))) })
    return () => { live = false }
  }, [uri, runId, nodeId, portId, offset, retry, pageSize])

  const exportFull = async () => {
    if (!runId || !nodeId || !portId || !canExportFull || exporting) return
    setExporting(true)
    try {
      const downloadUrl = await api.preflightFullResultExport(runId, nodeId, portId, name)
      const frame = document.createElement('iframe')
      frame.hidden = true
      frame.setAttribute('aria-hidden', 'true')
      frame.dataset.fullResultDownload = ''
      frame.src = downloadUrl
      document.body.appendChild(frame)
      // Keep the inert frame for the page lifetime. Removing it on a timer can cancel a slow native
      // artifact stream; one tiny frame per user-requested download is the safer trade-off.
      pushToast(
        `Full-result artifact download requested${reportedTotal == null ? ' · row count unknown.' : ` · ${reportedTotal.toLocaleString()} rows.`}`,
        'info',
      )
    } catch (error) {
      const reason = error instanceof Error && error.message ? error.message : String(error)
      pushToast(`Could not start full-result export: ${reason}`, 'error')
    } finally {
      setExporting(false)
    }
  }
  const exportAction = canExportFull ? (
    <Button variant="outline" size="sm" className="h-6 px-2 text-[10.5px]"
      disabled={exporting} onClick={exportFull}>
      {exporting ? 'Preparing export…' : 'Export full result'}
    </Button>
  ) : undefined

  if (!hasRunIdentity) return (
    <FullResultMessage title={`${viewLabel} identity unavailable`}
      reason="This history entry has no durable run identity, so the kernel cannot verify which output to read. Run the node again to create a verifiable result."
      modeToggle={modeToggle} />
  )
  if (err) return <ArtifactUnavailable error={err} modeToggle={modeToggle}
    label={viewLabel} action={exportAction} onRetry={() => setRetry((n) => n + 1)} />
  if (!data) return <div className="dp-dark text-foreground">
    <div className="flex items-center gap-1.5 border-b border-border px-[11px] py-2">
      <span className="rounded bg-emerald-100 px-1.5 py-px text-[10.5px] font-semibold text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300">{viewLabel}</span>
      {modeToggle}
    </div>
    <Skeleton />
  </div>
  if (data.error) return (
    <FullResultMessage title={`Couldn’t read ${viewLabel.toLowerCase()}`}
      reason={data.reason ?? 'The kernel reported an error while reading this run output.'}
      modeToggle={modeToggle} action={exportAction}
      onRetry={() => setRetry((n) => n + 1)} />
  )
  if (data.notPreviewable) return (
    <FullResultMessage title={`${viewLabel} cannot be previewed`}
      reason={data.reason ?? 'This artifact adapter does not provide a bounded interactive preview.'}
      modeToggle={modeToggle} action={exportAction} />
  )
  const cols = (data.columns ?? []) as ColumnSchema[]
  const rows = data.rows ?? []
  const canTryNext = data.hasMore === true || (data.hasMore == null && rows.length > 0)
  const nextPage = () => {
    previousOffsets.current.push(offset)
    setData(null); setDetail(null); setOffset(offset + rows.length)
  }
  const previousPage = () => {
    const prior = previousOffsets.current.pop() ?? Math.max(0, offset - pageSize)
    setData(null); setDetail(null); setOffset(prior)
  }
  return (
    <div className="dp-dark text-foreground">
      <div className="flex items-center gap-1.5 border-b border-border px-[11px] py-2">
        <span className="rounded bg-emerald-100 px-1.5 py-px text-[10.5px] font-semibold text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300">{viewLabel}</span>
        {modeToggle}
        {detail != null && (
          <button onClick={() => setDetail(null)} className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11.5px] font-semibold text-primary">
            <Icon name="chevronLeft" size={12} /> Row {offset + detail + 1}
          </button>
        )}
        <span className="flex-1" />
        {detail == null && canExportFull && (
          exportAction
        )}
        {detail == null && presentation?.kind !== 'chart' && (
          <span className="inline-flex gap-0.5">
            <PageBtn dir="prev" disabled={offset === 0} onClick={previousPage} />
            <PageBtn dir="next" disabled={!canTryNext} onClick={nextPage} />
          </span>
        )}
        {detail == null && rows.length > 0 && (
          <ExportCluster columns={cols} rows={rows} name={name} offset={offset}
            scope={pageScope} pushToast={pushToast} />
        )}
      </div>
      <DataScopeBanner data={{ ...data, rowCount: reportedTotal }} offset={offset}
        unit={presentation?.kind === 'chart' ? (presentation.grouped ? 'groups' : 'points') : 'rows'}
        scope={pageScope} allowNextAttempt={presentation?.kind !== 'chart'} />
      {presentation?.kind === 'chart'
        ? <ChartView rows={rows} type={presentation.type}
          xLabel={presentation.xLabel} yLabel={presentation.yLabel} grouped={presentation.grouped}
          completeness={data.completeness} total={reportedTotal} scope={pageScope} />
        : presentation?.kind === 'metric'
          ? <MetricValue rows={rows} />
          : detail != null && rows[detail]
            ? <RowDetail columns={cols} row={rows[detail]} />
            : <RowsTable columns={cols} rows={rows} onRowClick={setDetail} />}
    </div>
  )
}

function FullResultMessage({ title, reason, onRetry, modeToggle, action }: {
  title: string
  reason: string
  onRetry?: () => void
  modeToggle?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="dp-dark px-5 py-6 text-center text-muted-foreground">
      {modeToggle && <div className="mb-3 flex justify-center">{modeToggle}</div>}
      <div className="mb-3 inline-grid h-10 w-10 place-items-center rounded-[10px] bg-amber-100 text-amber-600 dark:bg-amber-500/15 dark:text-amber-300">
        <Icon name="power" size={18} />
      </div>
      <div className="text-[13px] font-semibold text-foreground">{title}</div>
      <div className="mx-auto mt-1.5 max-w-[380px] text-[11.5px] leading-normal">{reason}</div>
      {(onRetry || action) && (
        <div className="mt-3.5 flex items-center justify-center gap-2">
          {onRetry && <Button variant="outline" size="sm" onClick={onRetry}>Retry</Button>}
          {action}
        </div>
      )}
    </div>
  )
}

function ArtifactUnavailable({ error, onRetry, modeToggle, action, label }: {
  error: Error & { status?: number }
  onRetry: () => void
  modeToggle?: ReactNode
  action?: ReactNode
  label: string
}) {
  const status = error.status
  const denied = status === 401 || status === 403
  const missing = !denied && (status === 404 || status === 410 || /no such file|not found|missing|expired/i.test(error.message))
  const title = denied ? `${label} access denied` : missing ? `${label} expired or removed` : `Couldn’t load ${label.toLowerCase()}`
  const note = denied
    ? 'You no longer have access to this artifact. Switch back to the sample or ask the owner for access.'
    : missing
      ? 'The stored artifact is no longer available. Run the node again to create a new full result.'
      : 'The artifact may still exist. Check the connection and retry, or switch back to the sample.'
  return (
    <div className="dp-dark px-5 py-6 text-center text-muted-foreground">
      {modeToggle && <div className="mb-3 flex justify-center">{modeToggle}</div>}
      <div className="mb-3 inline-grid h-10 w-10 place-items-center rounded-[10px] bg-amber-100 text-amber-600 dark:bg-amber-500/15 dark:text-amber-300">
        <Icon name="clock" size={18} />
      </div>
      <div className="text-[13px] font-semibold text-foreground">{title}</div>
      <div className="mx-auto mt-1.5 max-w-[360px] text-[11.5px] leading-normal">{note}</div>
      <div title={error.message} className="dp-mono mx-auto mt-2 max-w-[380px] overflow-hidden text-ellipsis whitespace-nowrap text-[10px] text-muted-foreground/70">{error.message}</div>
      <div className="mt-3.5 flex items-center justify-center gap-2">
        <Button variant="outline" size="sm" onClick={onRetry}>Retry</Button>
        {action}
      </div>
    </div>
  )
}

function NotPreviewable({ reason, onRun, modeToggle }: {
  reason: string
  onRun?: () => void
  modeToggle?: ReactNode
}) {
  return (
    <div className="px-5 py-7 text-center text-muted-foreground">
      {modeToggle && <div className="mb-3 flex justify-center">{modeToggle}</div>}
      <div className="mb-3 inline-grid h-10 w-10 place-items-center rounded-[10px] bg-amber-100 text-amber-600 dark:bg-amber-500/15 dark:text-amber-300">
        <Icon name="power" size={18} />
      </div>
      <div className="text-[13px] font-semibold text-foreground">Not sample-previewable</div>
      <div className="mx-auto mt-[5px] max-w-[320px] text-[11.5px] leading-normal">{reason}</div>
      {onRun && <Button variant="outline" size="sm" onClick={onRun} className="mt-3.5">Run a full pass →</Button>}
    </div>
  )
}
