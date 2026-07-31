import { useEffect, useRef, useState } from 'react'
import {
  currentPreviews, parameterBindingsIdentity, previewPlanIdentity, useStore, nodeRunnable, roleCanEdit, hasConfiguredMergeColumnsWrite, hasConfiguredManagedSidecarMerge, hasConfiguredUpsertWrite,
} from '../store/graph'
import { getSpec, nodeOutputs } from '../nodes/registry'
import { getBackendSpec, NodeParamFields, nodeInvalidReason } from '../nodes/generic'
import { useInputColumns, useSchemaWarnings } from '../nodes/fields'
import { codeHash, outputPortId } from '../nodes/schema'
import { color, status as statusTok, kindAccent } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { FileDialog } from '../ui/FileDialog'
import { miniInputClass } from '../ui/controls'
import { api, KernelError } from '../api/client'
import { MergeColumnsControl } from '../components/MergeColumnsControl'
import { ManagedSidecarMergeControl } from '../components/ManagedSidecarMergeControl'
import { UpsertControl } from '../components/UpsertControl'
import { JoinWithRelated } from '../components/JoinWithRelated'
import { WritePublicationSummary } from '../components/WritePublicationSummary'
import type { CatalogTable, DatasetRevisionDetail, JoinAnalysis, JoinSuggestion } from '../types/api'
import { parseJoinKeys, serializeJoinKeys } from '../nodes/joinKeys'
import { datasetRefIdentity, isParameterRef, type CanvasDoc, type ColumnSchema, type DatasetRef } from '../types/graph'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Separator } from '@/components/ui/separator'
import { cn } from '@/lib/utils'
import { FieldEvidenceButton } from '../components/FieldEvidenceDetail'
import { requestSourceEntryAction } from '../nodes/kinds/source'
import { configuredProcessorRef, exactProcessor } from '../nodes/processorIdentity'

export const INSPECTOR_W = 300
export const INSPECTOR_COLLAPSED_W = 44

// Opaque code outputs and SQL projections can carry an explicit output contract. SQL remains
// physically typeable without one, but its row-reference provenance is unknown unless declared.
// Other contract-capable kinds must be backend-registered plugins, not merely unknown node names.
const CONTRACT_KINDS = new Set(['transform', 'vector-search', 'sql'])
const RETAINED_PREVIEW_FALLBACK_CODES = new Set([
  'retained_upstream_unavailable',
  'retained_upstream_stale',
  'retained_upstream_expired',
])
export const canDeclareSchemaKind = (kind: string) => (
  CONTRACT_KINDS.has(kind) || getSpec(kind)?.source?.startsWith('plugin:') === true
)
export const canDeclareNodeSchema = (kind: string, outputCount: number) => (
  canDeclareSchemaKind(kind)
  && outputCount === 1
)

const schemaContractText = (kind: string, cfg: Record<string, unknown>): unknown => (
  kind === 'sql' ? cfg.sql : cfg.code
)

const schemaContractStale = (kind: string, cfg: Record<string, unknown>): boolean => {
  const contractText = schemaContractText(kind, cfg)
  const pinnedHash = cfg.outputSchemaCodeHash
  return Array.isArray(cfg.outputSchema) && cfg.outputSchema.length > 0
    && contractText != null && typeof pinnedHash === 'string' && pinnedHash.length > 0
    && pinnedHash !== codeHash(String(contractText))
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0
}

function sourceDatasetRef(config: Record<string, unknown>): DatasetRef | null {
  const value = config.datasetRef
  if (!value || typeof value !== 'object' || Array.isArray(value) || isParameterRef(value)) return null
  const ref = value as Record<string, unknown>
  if (ref.kind === 'exact' && nonEmptyString(ref.datasetId) && nonEmptyString(ref.revisionId)) {
    return value as DatasetRef
  }
  if (ref.kind !== 'as_of' || !nonEmptyString(ref.asOf)
      || !ref.resolved || typeof ref.resolved !== 'object' || Array.isArray(ref.resolved)) {
    return null
  }
  const resolved = ref.resolved as Record<string, unknown>
  return resolved.selector === 'as_of'
    && nonEmptyString(resolved.datasetId)
    && nonEmptyString(resolved.revisionId)
    ? value as DatasetRef
    : null
}

function sourceDatasetParameter(config: Record<string, unknown>) {
  const value = config.datasetRef
  return isParameterRef(value) && nonEmptyString(value.parameterRef) ? value : null
}

function hasLocalSourceIdentity(config: Record<string, unknown>): boolean {
  return nonEmptyString(config.registrationId)
}

function hasProviderSourceIdentity(config: Record<string, unknown>): boolean {
  return nonEmptyString(config.providerMountId)
    && nonEmptyString(config.providerSourceBindingId)
}

function hasBoundSourceIdentity(config: Record<string, unknown>): boolean {
  return hasLocalSourceIdentity(config)
    || hasProviderSourceIdentity(config)
    || sourceDatasetRef(config) !== null
    || sourceDatasetParameter(config) !== null
}

function sourceUri(config: Record<string, unknown>): string {
  return typeof config.uri === 'string' ? config.uri.trim() : ''
}

function isManualSource(config: Record<string, unknown>): boolean {
  return !hasBoundSourceIdentity(config) && !!sourceUri(config)
}

function isDelimitedTextUri(uri: string): boolean {
  const path = uri.split(/[?#]/, 1)[0]?.toLowerCase() ?? ''
  return path.endsWith('.csv') || path.endsWith('.tsv')
}

function isUnboundSource(config: Record<string, unknown>): boolean {
  return !hasBoundSourceIdentity(config) && !isManualSource(config)
}

// Figma-style right property panel: shows the SELECTED node's properties (params reused from the
// generic editor), a code snippet with "open editor", its ports, and actions. Without one valid
// selected node there is nothing to inspect, so the Canvas keeps the full layout width.
export function Inspector({ collapsed = false, onToggle }: { collapsed?: boolean; onToggle?: () => void }) {
  const selectedIds = useStore((s) => s.selectedIds)
  const nodes = useStore((s) => s.doc.nodes)
  const canvasRole = useStore((s) => s.canvasRole)
  const canEdit = roleCanEdit(canvasRole)
  const id = selectedIds.length === 1 ? selectedIds[0] : null
  const node = id ? nodes.find((n) => n.id === id) : null

  if (!node) return null

  if (collapsed) {
    return (
      <aside data-testid="inspector" data-layout-region="inspector" aria-label="Inspector"
        className="flex h-full flex-col items-center border-l border-border bg-card py-3"
        style={{ width: INSPECTOR_COLLAPSED_W, flex: `0 0 ${INSPECTOR_COLLAPSED_W}px` }}>
        <button type="button" data-testid="inspector-collapse" onClick={onToggle}
          aria-expanded={false}
          aria-label="Expand Inspector" title="Expand Inspector"
          className="grid h-7 w-7 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground">
          <Icon name="chevronLeft" size={14} />
        </button>
        <span aria-hidden className="mt-3 text-[10px] font-semibold uppercase tracking-[0.8px] text-muted-foreground [writing-mode:vertical-rl]">Inspector</span>
      </aside>
    )
  }

  return (
    <aside data-testid="inspector" data-layout-region="inspector"
      className="flex h-full flex-col overflow-hidden border-l border-border bg-card"
      style={{ width: INSPECTOR_W, flex: `0 0 ${INSPECTOR_W}px` }}>
      <div className="flex h-[52px] flex-none items-center border-b border-border px-3.5 text-[13px] font-semibold text-foreground">
        <span className="flex-1">Inspector</span>
        {onToggle && <button type="button" data-testid="inspector-collapse" onClick={onToggle}
          aria-expanded
          aria-label="Collapse Inspector" title="Collapse Inspector"
          className="grid h-7 w-7 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground">
          <Icon name="chevronRight" size={14} />
        </button>}
      </div>
      {!canEdit && (
        <div className="flex-none border-b border-border bg-muted/60 px-3.5 py-2 text-[10.5px] text-muted-foreground">
          {canvasRole === 'viewer' ? 'View-only access' : 'Editing disabled until access is known'}
        </div>
      )}
      <NodeInspector key={node.id} nodeId={node.id} />
    </aside>
  )
}

function EditOnly({ enabled, children }: { enabled: boolean; children: React.ReactNode }) {
  return <fieldset disabled={!enabled} className="contents">{children}</fieldset>
}

function NodeInspector({ nodeId }: { nodeId: string }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const doc = useStore((s) => s.doc)
  const runnable = useStore((s) => nodeRunnable(s.doc, nodeId))
  const runState = useStore((s) => s.runs[nodeId]?.phase)
  const configuredMerge = useStore((s) => hasConfiguredMergeColumnsWrite(s.doc, nodeId))
  const configuredManagedSidecarMerge = useStore((s) => hasConfiguredManagedSidecarMerge(s.doc, nodeId))
  const configuredUpsert = useStore((s) => hasConfiguredUpsertWrite(s.doc, nodeId))
  const allSchemas = useStore((s) => s.schemas)
  const previews = useStore((s) => s.previews)
  const edges = useStore((s) => s.doc.edges)
  const warnings = useSchemaWarnings(nodeId)   // config references a column not in the (known) input
  const inputColumns = useInputColumns(nodeId)
  const catalog = useStore((s) => s.catalog)
  const processors = useStore((s) => s.processors)
  const transformReferences = useStore((s) => s.canvasTransformReferences)
  const numericDrafts = useStore((s) => s.numericParamDrafts[nodeId])
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const kernelUp = useStore((s) => s.kernelUp)
  const { rename, runPreview, requestRun, cancelRun, togglePanel, bypass, disable, duplicate, removeNode, openCodeFullscreen } = useStore.getState()
  const [name, setName] = useState(node?.data.title ?? '')
  const [editingDraftSourceUri, setEditingDraftSourceUri] = useState(false)
  const [advancedExecutionOpen, setAdvancedExecutionOpen] = useState(false)
  const [advancedOutputSchemaOpen, setAdvancedOutputSchemaOpen] = useState(false)
  useEffect(() => setName(node?.data.title ?? ''), [node?.data.title])
  useEffect(() => setEditingDraftSourceUri(false), [nodeId])
  useEffect(() => setAdvancedExecutionOpen(false), [nodeId])
  useEffect(() => setAdvancedOutputSchemaOpen(false), [nodeId])
  if (!node) return null

  const kind = node.type
  const spec = getSpec(kind)
  const cfg = node.data.config as Record<string, unknown>
  const bspec = getBackendSpec(kind)
  const st = statusTok[node.data.status] ?? statusTok.draft
  const libraryTransform = kind === 'transform' && cfg.source === 'library'
  const codeParams = (bspec?.params ?? []).filter((p) => (
    p.type === 'code' && !libraryTransform
  ))
  const invalid = nodeInvalidReason(node, inputColumns, numericDrafts)
  const outputPorts = nodeOutputs(node)
  const unboundSource = kind === 'source' && isUnboundSource(cfg)
  const boundSource = kind === 'source' && hasBoundSourceIdentity(cfg)
  const manualSource = kind === 'source' && isManualSource(cfg)
  const manualDelimitedSource = manualSource && isDelimitedTextUri(sourceUri(cfg))
  const showDraftSourceEntry = unboundSource || (kind === 'source' && editingDraftSourceUri)
  const sourceSummary = kind === 'source' ? sourceInspectorSummary(catalog, cfg) : null
  const libraryProcessor = kind === 'transform' && cfg.source === 'library'
    ? exactProcessor(processors, cfg.processor, cfg.version)
      ?? transformReferences.find((reference) => (
        reference.id === cfg.processor && reference.version === cfg.version
      ))?.descriptor
    : undefined
  const inspectorBlurb = sourceSummary ?? (libraryProcessor?.blurb || spec?.blurb)
  const omittedParamNames = kind === 'write'
    ? ['writeMode']
    : kind === 'source' && !manualDelimitedSource
      ? ['delimiter', 'header']
      : []

  // Code ops and backend-owned plugin kinds can carry a declared/inferred schema contract.
  const canDeclareSchema = canDeclareNodeSchema(kind, outputPorts.length)
  const outputSchemaSummary = outputSchemaContractSummary(cfg)
  const outputSchemaIsStale = schemaContractStale(kind, cfg)
  const resourceRequirements = resourceRequirementSummary(cfg)
  const checkpointed = cfg.checkpoint === true
  const hasResourceControls = kind === 'transform' || kind === 'section'
  const hasCheckpointControls = kind !== 'source' && kind !== 'note' && kind !== 'write'
  // OUTPUT port schema: prefer the node's own declared contract (exact user types, instant) over the
  // server-resolved schema — but only for a contract-capable, non-bypassed node (a bypassed node passes
  // its input through, so its declaration doesn't describe its output). null = untyped, undefined = unknown.
  const declaredOut = Array.isArray(cfg.outputSchema) && (cfg.outputSchema as ColumnSchema[]).length
    ? (cfg.outputSchema as ColumnSchema[]) : null
  // Runtime columns are display-only evidence. Filter stale previews first, then require the
  // observed result's effective port to match the port being rendered.
  const currentPreview = currentPreviews(doc, previews)[nodeId]
  const observedOutFor = (portId: string): ColumnSchema[] | undefined => {
    const columns = currentPreview?.result?.columns
    if (currentPreview?.result?.error || currentPreview?.result?.notPreviewable || !columns?.length
      || outputPortId(doc, nodeId, currentPreview.portId) !== outputPortId(doc, nodeId, portId)) {
      return undefined
    }
    return columns as ColumnSchema[]
  }
  const outSchemaFor = (portId: string): ColumnSchema[] | null | undefined => (
    kind !== 'sql' && canDeclareSchema && declaredOut && !schemaContractStale(kind, cfg)
      && !node.data.bypassed && outputPorts.length === 1
      ? declaredOut
      : canDeclareSchema && outputPorts.length === 1
        ? observedOutFor(portId) ?? allSchemas[nodeId]?.[portId]
        : allSchemas[nodeId]?.[portId]
  )
  // INPUT port schema = the OUTPUT schema of whatever is wired into that port (routed by targetHandle).
  const inputSchemaFor = (portId: string): ColumnSchema[] | null | undefined => {
    const inc = edges.filter((e) => e.target === nodeId)
    const specIn = spec?.inputs ?? []
    const e = inc.find((ed) => (ed.targetHandle ?? specIn[0]?.id ?? 'in') === portId)
      ?? (specIn.length === 1 ? inc[0] : undefined)
    if (!e) return undefined
    const sourcePortId = outputPortId(useStore.getState().doc, e.source, e.sourceHandle)
    return sourcePortId === undefined ? undefined : allSchemas[e.source]?.[sourcePortId]
  }

  return (
    <div className="flex flex-1 flex-col overflow-y-auto">
      {/* header */}
      <div className="flex flex-col gap-2 border-b border-border px-3.5 py-3">
        <div className="flex items-center gap-2">
          <span className="h-[26px] w-1 flex-none rounded-sm" style={{ background: kindAccent[kind] ?? color.text3 }} />
          <input
            value={name}
            disabled={!canEdit}
            aria-label="Node title"
            onChange={(e) => setName(e.target.value)}
            onBlur={() => { if (name.trim() && name !== node.data.title) rename(nodeId, name.trim()) }}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
            className="min-w-0 flex-1 rounded-md border border-transparent bg-transparent px-1.5 py-[3px] text-sm font-semibold text-foreground outline-none transition-colors focus:border-border"
          />
          <span className="rounded bg-muted px-1.5 py-0.5 text-[8.5px] font-semibold tracking-[0.6px] text-muted-foreground">
            {(spec?.tag ?? kind).toUpperCase()}
          </span>
        </div>
        <div className="flex min-w-0 items-center gap-1.5 text-[11.5px] text-muted-foreground">
          {/* a note is an annotation — it never runs, so a run status (draft/stale/…) is meaningless */}
          {kind === 'note' ? <span>annotation</span>
            : <><span style={{ color: st.color }}>{st.glyph}</span> {st.label}</>}
          {inspectorBlurb && <span title={inspectorBlurb} className="min-w-0 leading-relaxed text-muted-foreground/70">· {inspectorBlurb}</span>}
        </div>
      </div>

      {/* properties (reused generic param editor) */}
      {showDraftSourceEntry ? <DraftSourceInspector nodeId={nodeId} canEdit={canEdit}
        onUriEditingChange={setEditingDraftSourceUri} /> : <>
        {kind === 'join' ? <JoinConfigurationSummary nodeId={nodeId} canEdit={canEdit} />
          : !boundSource && <EditOnly enabled={canEdit}>
          <Section title="Properties">
            <NodeParamFields nodeId={nodeId} omitNames={omittedParamNames} />
            {codeParams.length === 0 && (bspec?.params ?? []).length === 0 && kind !== 'write' && (
              <div className="text-[11.5px] text-muted-foreground">No editable parameters.</div>
            )}
          </Section>
        </EditOnly>}
        {kind === 'source' && <SourceConnectionDetails nodeId={nodeId} />}
      </>}

      {!unboundSource && (kind === 'source' || kind === 'join') && <EditOnly enabled={canEdit}>
        <Section title="Related data">
          <JoinWithRelated nodeId={nodeId} />
        </Section>
      </EditOnly>}

      {/* a write node's output destination lives here in the panel, not cluttering the card */}
      {kind === 'write' && <>
        <EditOnly enabled={canEdit}><WriteDestination nodeId={nodeId} /></EditOnly>
        {/* Observation and exact navigation remain available to viewers. MergeColumnsControl owns
            its own edit/action guards; wrapping it in the destination fieldset would also disable
            the read-only Jobs and receipt links. */}
        {configuredManagedSidecarMerge ? <ManagedSidecarMergeControl nodeId={nodeId} />
          : configuredMerge ? <MergeColumnsControl nodeId={nodeId} />
          : configuredUpsert ? <UpsertControl nodeId={nodeId} />
            : <details className="mx-3.5 mt-3 rounded-md border border-border bg-muted/20 px-2 py-1.5 text-[10.5px]">
              <summary className="cursor-pointer font-semibold text-foreground">Advanced write operations</summary>
              <ManagedSidecarMergeControl nodeId={nodeId} />
              <MergeColumnsControl nodeId={nodeId} />
              <UpsertControl nodeId={nodeId} />
            </details>}
      </>}

      {/* code snippet + open the full editor (Monaco panel; fullscreen editor is a later step) */}
      {codeParams.map((p) => {
        const codeText = String(cfg[p.name] ?? p.default ?? '')
        return (
          <Section key={p.name} title={p.label ?? p.name}>
            <pre className="dp-mono m-0 max-h-[120px] overflow-auto whitespace-pre rounded-lg border border-border p-2 text-[10.5px] leading-normal text-muted-foreground"
              style={{ background: 'var(--code-bg, #f7f8fa)' }}>
              {codeText || '(empty)'}
            </pre>
            <div className="mt-1.5 flex gap-1.5">
              {kind === 'section' ? (
                <CodeBtn icon="code" label="Open section editor →" disabled={!canEdit} onClick={() => togglePanel(nodeId, 'section')} />
              ) : (
                <CodeBtn icon="external" label={canEdit ? 'Open fullscreen editor' : 'View full code'} onClick={() => openCodeFullscreen(nodeId, p.name, p.lang)} />
              )}
            </div>
          </Section>
        )
      })}

      {libraryTransform && (
        <Section title="Processor definition">
          <div className="rounded-lg border border-border bg-muted/20 p-2.5">
            <div className="text-[11.5px] font-semibold text-foreground">
              {libraryProcessor?.title ?? 'Exact Library processor'}
            </div>
            <div className="mt-1 break-all font-mono text-[10.5px] text-muted-foreground">
              {configuredProcessorRef(cfg.processor, cfg.version) ?? 'No exact processor selected'}
            </div>
          </div>
          <div className="mt-1.5">
            <CodeBtn icon="external" label="Open processor definition"
              onClick={() => openCodeFullscreen(nodeId, 'code', 'python')} />
          </div>
        </Section>
      )}

      {/* catalog-driven join hints: suggested keys (measured cardinality) + a fan-out warning */}
      {kind === 'join' && <EditOnly enabled={canEdit}><JoinHints nodeId={nodeId} /></EditOnly>}

      {(resourceRequirements || checkpointed) && <ExecutionSummary resourceRequirements={resourceRequirements} checkpointed={checkpointed}
        canEdit={canEdit} onEdit={() => setAdvancedExecutionOpen(true)} />}
      {(hasResourceControls || hasCheckpointControls) && <details open={advancedExecutionOpen}
        onToggle={(event) => setAdvancedExecutionOpen(event.currentTarget.open)}
        className="mx-3.5 mt-3 rounded-md border border-border bg-muted/20 p-3 text-[10.5px]">
        <summary className="cursor-pointer font-semibold text-foreground">Advanced execution</summary>
        <div className="mt-3 grid gap-3">
          {hasResourceControls && <EditOnly enabled={canEdit}><ResourcesSection nodeId={nodeId} embedded /></EditOnly>}
          {hasResourceControls && hasCheckpointControls && <Separator />}
          {hasCheckpointControls && <EditOnly enabled={canEdit}><CheckpointToggle nodeId={nodeId} embedded /></EditOnly>}
        </div>
      </details>}

      {/* run plan: appears only when placement actually splits/routes this run (a cluster backend, an
          engine label, or a checkpoint) — makes the cost-aware scheduler + tiering visible before running */}
      {!unboundSource && kind !== 'note' && <RunPlan nodeId={nodeId} />}

      {/* ports — a real port label (join left/right, metric value) shows as a name; the default
          in/out ports show their wire type + a typed/untyped schema badge (click "N cols" to expand
          the columns). Input badges reflect the upstream's output schema. */}
      {!unboundSource && <Section title="Ports">
        <div className="flex flex-col gap-1 text-[11.5px] text-muted-foreground">
          {(spec?.inputs ?? []).map((p) => <PortRow key={`in-${p.id}`} dir="in" name={portName(p)} wire={p.wire} schema={inputSchemaFor(p.id)} />)}
          {outputPorts.map((p) => (
            <PortRow key={`out-${p.id}`} dir="out" name={portName(p)} wire={p.wire}
              schema={outSchemaFor(p.id)} />
          ))}
          {(spec?.inputs ?? []).length === 0 && outputPorts.length === 0 && <span>—</span>}
        </div>
        {/* editable output ports: only on the section (its driver script emit()s to named ports) —
            fixed-port ops (filter/sort/join) keep their ports as a type contract the wires rely on */}
        {kind === 'section' && <><Separator className="my-1" /><EditOnly enabled={canEdit}><OutputPortsEditor nodeId={nodeId} /></EditOnly></>}
      </Section>}

      {/* Schema contracts are an advanced declaration. Once configured, keep a compact signal and a
          direct route back to it in the normal flow. */}
      {canDeclareSchema && <>
        {outputSchemaSummary && <OutputSchemaSummary summary={outputSchemaSummary} stale={outputSchemaIsStale}
          canEdit={canEdit} onEdit={() => setAdvancedOutputSchemaOpen(true)} />}
        <details open={advancedOutputSchemaOpen}
          onToggle={(event) => setAdvancedOutputSchemaOpen(event.currentTarget.open)}
          className="mx-3.5 mt-3 rounded-md border border-border bg-muted/20 p-3 text-[10.5px]">
          <summary className="cursor-pointer font-semibold text-foreground">Advanced output schema</summary>
          <div className="mt-3"><EditOnly enabled={canEdit}><SchemaContract nodeId={nodeId} runnable={runnable && !invalid} embedded /></EditOnly></div>
        </details>
      </>}

      {/* actions */}
      <Section title="Actions">
        {invalid && <div className="mb-1.5 text-[11px] text-amber-700">⚠ {invalid}</div>}
        {!invalid && warnings.map((w, i) => (
          <div key={i} className="mb-1.5 text-[11px] text-amber-700 dark:text-amber-300">⚠ {w} — not found in the input schema</div>
        ))}
        <div className="flex flex-wrap gap-1.5">
          {/* a note never runs — only offer duplicate / delete for annotations */}
          {!unboundSource && kind !== 'note' && <>
            <Action icon="eye" label={!kernelUp ? 'Hub offline — preview unavailable' : 'View data'} disabled={!kernelUp || !runnable || !!invalid} onClick={() => runPreview(nodeId)} />
          <Action icon={runState === 'running' ? 'stop' : 'play'} label={!kernelUp ? 'Hub offline — run unavailable' : kind === 'source' ? 'Count rows' : runState === 'running' ? 'Stop' : configuredManagedSidecarMerge ? 'Review sidecar merge' : configuredMerge ? 'Review column merge' : configuredUpsert ? 'Review keyed upsert' : 'Run'} disabled={!canEdit || !kernelUp || ((!runnable || !!invalid) && runState !== 'running')}
              onClick={() => (runState === 'running' ? cancelRun(nodeId) : requestRun(nodeId))} />
            {spec?.canBypass && <Action icon="power" label="Bypass" disabled={!canEdit} onClick={() => bypass(nodeId)} />}
            <Action icon="mute" label={node.data.disabled ? 'Enable' : 'Disable'} disabled={!canEdit} onClick={() => disable(nodeId)} />
          </>}
          {!unboundSource && <Action icon="duplicate" label="Duplicate" disabled={!canEdit} onClick={() => duplicate(nodeId)} />}
          <Action icon="trash" label="Delete" disabled={!canEdit} danger onClick={() => removeNode(nodeId)} />
        </div>
      </Section>
    </div>
  )
}

function JoinConfigurationSummary({ nodeId, canEdit }: { nodeId: string; canEdit: boolean }) {
  const node = useStore((state) => state.doc.nodes.find((candidate) => candidate.id === nodeId))
  const canvasId = useStore((state) => state.doc.id)
  const requestNodeReveal = useStore((state) => state.requestNodeReveal)
  const config = (node?.data.config ?? {}) as Record<string, unknown>
  const on = String(config.on ?? '')
  const condition = String(config.condition ?? '')
  const pairs = parseJoinKeys(on, condition)
  const how = String(config.how ?? 'inner')
  const advancedPredicate = condition.trim() ? condition : on

  return <Section title="Join configuration">
    <div className="flex items-center justify-between gap-2 text-[11.5px]">
      <span className="text-muted-foreground">Join type</span>
      <span className="capitalize text-foreground">{how}</span>
    </div>
    {pairs === null ? <>
      <div className="text-[10.5px] font-semibold text-muted-foreground">Advanced condition</div>
      <code className="break-words rounded-md border border-border bg-muted/30 px-2 py-1.5 text-[10.5px] text-foreground">
        {advancedPredicate}
      </code>
    </> : pairs.length > 0 ? (
      <div className="grid gap-1" aria-label="Configured join keys">
        {pairs.map((pair, index) => (
          <code key={`${pair.left}\u0000${pair.right}\u0000${index}`} className="break-words text-[10.5px] text-foreground">
            a.{pair.left} = b.{pair.right}
          </code>
        ))}
      </div>
    ) : <div className="text-[10.5px] text-muted-foreground">No join keys selected.</div>}
    <Button variant="outline" size="sm" className="h-auto self-start px-2.5 py-1.5 text-[11px]"
      onClick={() => requestNodeReveal(canvasId, nodeId)}>
      {canEdit ? 'Edit keys on Join card' : 'Show Join card'}
    </Button>
  </Section>
}

// Add / rename / remove a section's named output ports (config.outputs). The store drops edges
// leaving a port that no longer exists, so a rename/remove can't strand an invisible wire.
function outputPortError(outputs: string[]): string | undefined {
  if (outputs.length > 64) return 'A Section can declare at most 64 output ports.'
  for (const [index, output] of outputs.entries()) {
    if (!output) return `Output port ${index + 1} cannot be empty.`
    if (output.length > 128) return `Output port ${index + 1} must be 128 characters or fewer.`
    if (output.trim() !== output) return `Output port ${index + 1} cannot contain surrounding whitespace.`
    if (outputs.indexOf(output) !== index) return `Output port “${output}” is duplicated. Port IDs must be unique.`
  }
  return undefined
}

function nextOutputPortId(outputs: string[]): string {
  const used = new Set(outputs)
  let suffix = Math.max(2, outputs.length + 1)
  while (used.has(`out${suffix}`)) suffix += 1
  return `out${suffix}`
}

function OutputPortsEditor({ nodeId }: { nodeId: string }) {
  // select the stored value (stable ref) — NOT a freshly-built array, which would loop forever
  const raw = useStore((s) => (s.doc.nodes.find((n) => n.id === nodeId)?.data.config as { outputs?: unknown } | undefined)?.outputs)
  const storedOutputs = Array.isArray(raw) && raw.length ? raw.map(String) : ['out']
  const storedSignature = JSON.stringify(storedOutputs)
  const [draft, setDraft] = useState(storedOutputs)
  const [validationError, setValidationError] = useState(() => outputPortError(storedOutputs))
  const updateConfig = useStore((s) => s.updateConfig)
  useEffect(() => {
    setDraft(storedOutputs)
    setValidationError(outputPortError(storedOutputs))
    // The serialized value is the stable dependency; storedOutputs is rebuilt by the selector render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId, storedSignature])

  const edit = (next: string[]) => {
    setDraft(next)
    setValidationError(outputPortError(next))
  }
  const commit = (next: string[]) => {
    const error = outputPortError(next)
    setDraft(next)
    setValidationError(error)
    if (error) return
    if (JSON.stringify(next) !== storedSignature) updateConfig(nodeId, { outputs: next })
  }
  return (
    <div className="mt-2 flex flex-col gap-1">
      <Label className="text-[9.5px] font-bold uppercase tracking-[0.4px] text-muted-foreground">OUTPUT PORTS (emit)</Label>
      {draft.map((name, i) => (
        <div key={i} className="flex items-center gap-1">
          <Input value={name} aria-invalid={validationError ? true : undefined}
            onChange={(e) => edit(draft.map((x, j) => (j === i ? e.target.value.replace(/\s+/g, '_') : x)))}
            onBlur={() => commit(draft)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur() } }}
            className={cn(miniInputClass, 'dp-mono flex-1 text-[11px] md:text-[11px]')} />
          {draft.length > 1 && (
            <Button variant="ghost" size="icon" onClick={() => commit(draft.filter((_, j) => j !== i))} title="Remove port"
              className="h-5 w-5 flex-none text-muted-foreground [&_svg]:size-3"><Icon name="close" size={11} /></Button>
          )}
        </div>
      ))}
      {validationError && <div role="alert" className="text-[10.5px] leading-snug text-destructive">{validationError}</div>}
      {!validationError && draft.length === 64 && (
        <div role="status" className="text-[10.5px] leading-snug text-muted-foreground">Maximum 64 output ports reached.</div>
      )}
      <Button variant="outline" size="sm" disabled={draft.length >= 64 || !!validationError}
        onClick={() => commit([...draft, nextOutputPortId(draft)])}
        className="h-auto gap-1 self-start border-dashed px-2 py-1 text-[10.5px] font-medium text-muted-foreground shadow-none [&_svg]:size-3">
        <Icon name="plus" size={11} /> add port
      </Button>
    </div>
  )
}

// The Write destination. Admission decides whether the selected execution publishes a managed revision.
function WriteDestination({ nodeId }: { nodeId: string }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const canManageDestinations = useStore(
    (s) => s.currentUser?.capabilities?.includes('global_settings') === true,
  )
  const [dlg, setDlg] = useState(false)
  const cfg = (node?.data.config ?? {}) as Record<string, unknown>
  const filename = String(cfg.filename ?? cfg.name ?? 'output')
  const destName = (cfg.destName as string) ?? 'Workspace outputs'
  const destPath = String(cfg.destPath ?? '')
  const admission = useStore((s) => s.runs[nodeId]?.writeAdmission
    ?? (s.runs[nodeId]?.phase === 'done' ? s.runs[nodeId]?.writeOutcomeAdmission : undefined))
  const outcomeAdmission = useStore((s) => s.runs[nodeId]?.writeOutcomeAdmission)
  const phase = useStore((s) => s.runs[nodeId]?.phase)
  const statusOutputs = useStore((s) => s.runs[nodeId]?.status?.outputs)
  const recoveredOutcome = useStore((s) => s.runs[nodeId]?.writeOutcome)
  const outputs = statusOutputs ?? recoveredOutcome?.outputs ?? []
  const receipt = outputs.find((output) => output.writeReceipt)?.writeReceipt
  const publicationReceipt = receipt ?? recoveredOutcome?.receipt ?? admission?.recoveredReceipt
  const managed = publicationReceipt != null
    || admission?.managed === true
    || (phase === 'done' && outcomeAdmission?.managed === true)
  const destination = `${destName}${destPath ? `/${destPath}` : ''}`
  return (
    <Section title="Output">
      <WritePublicationSummary outputName={filename} destination={destination} admission={admission}
        outcomeAdmission={outcomeAdmission} receipt={publicationReceipt}
        outputs={outputs} completed={phase === 'done'} publishing={managed && phase === 'running'} />
      <div className="mt-2">
        <CodeBtn icon="export" label="Choose destination…" onClick={() => setDlg(true)} />
      </div>
      {dlg && (
        <FileDialog mode="save" defaultName={filename} onClose={() => setDlg(false)}
          onManageDestinations={canManageDestinations
            ? () => {
                setDlg(false)
                window.dispatchEvent(new CustomEvent('dp-open-settings', {
                  detail: {
                    category: 'destinations',
                    trigger: document.querySelector<HTMLElement>('[data-testid="app-menu"]'),
                  },
                }))
              }
            : undefined}
          onPick={(r) => { updateConfig(nodeId, { destId: r.destId, destName: r.destName, destPath: r.path, filename: r.filename }); setDlg(false) }} />
      )}
    </Section>
  )
}

function CodeBtn({ icon, label, onClick, disabled }: { icon: IconName; label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <Button variant="outline" size="sm" onClick={onClick} disabled={disabled}
      className="h-auto gap-1.5 px-2.5 py-1.5 text-[11.5px] font-medium text-primary shadow-none [&_svg]:size-3">
      <Icon name={icon} size={12} /> {label}
    </Button>
  )
}

export function canEnableLinearCheckpoint(doc: CanvasDoc, nodeId: string): boolean {
  const checkpoint = doc.nodes.find((node) => node.id === nodeId)
  if (!checkpoint || checkpoint.type !== 'select' || doc.nodes.length !== 3 || doc.edges.length !== 2) return false
  const otherCheckpoint = doc.nodes.some((node) =>
    node.id !== nodeId && (node.data.config as Record<string, unknown>)?.checkpoint === true)
  if (otherCheckpoint) return false
  const source = doc.nodes.find((node) => node.type === 'source')
  const write = doc.nodes.find((node) => node.type === 'write')
  if (!source || !write || [source, checkpoint, write].some((node) => node.data.bypassed || node.data.disabled)) return false
  const selectIn = doc.edges.find((edge) => edge.target === checkpoint.id)
  const writeIn = doc.edges.find((edge) => edge.target === write.id)
  return selectIn?.source === source.id
    && writeIn?.source === checkpoint.id
    && (selectIn.sourceHandle == null || selectIn.sourceHandle === 'out')
    && (selectIn.targetHandle == null || selectIn.targetHandle === 'in')
    && (writeIn.sourceHandle == null || writeIn.sourceHandle === 'out')
    && (writeIn.targetHandle == null || writeIn.targetHandle === 'in')
}

function resourceRequirementSummary(config: Record<string, unknown>): string | null {
  const requires = config.requires
  if (!requires || typeof requires !== 'object') return null
  const { cpu, gpu, gpuType } = requires as { cpu?: unknown; gpu?: unknown; gpuType?: unknown }
  const parts = [
    typeof gpu === 'number' ? `${gpu} GPU${gpu === 1 ? '' : 's'}` : null,
    typeof gpuType === 'string' && gpuType ? gpuType : null,
    typeof cpu === 'number' ? `${cpu} CPU${cpu === 1 ? '' : 's'}` : null,
  ].filter((part): part is string => part != null)
  return parts.length ? parts.join(' · ') : null
}

function outputSchemaContractSummary(config: Record<string, unknown>): string | null {
  const outputSchema = config.outputSchema
  if (Array.isArray(outputSchema) && outputSchema.length > 0) {
    return `${outputSchema.length} declared column${outputSchema.length === 1 ? '' : 's'}`
  }
  if (outputSchema && typeof outputSchema === 'object' && typeof (outputSchema as { ref?: unknown }).ref === 'string') {
    const ref = (outputSchema as { ref: string }).ref.trim()
    return ref ? `Named contract · ${ref}` : null
  }
  return null
}

function ExecutionSummary({ resourceRequirements, checkpointed, canEdit, onEdit }: {
  resourceRequirements: string | null
  checkpointed: boolean
  canEdit: boolean
  onEdit: () => void
}) {
  return <Section title="Execution">
    <div className="grid gap-1.5 text-[11px]">
      {resourceRequirements && <div className="flex items-center justify-between gap-2">
        <span><strong>Resources</strong> · {resourceRequirements}</span>
        {canEdit && <Button variant="ghost" size="sm" onClick={onEdit} className="h-auto px-2 py-1 text-[10.5px] font-medium text-primary shadow-none">Edit resources</Button>}
      </div>}
      {checkpointed && <div className="flex items-center justify-between gap-2">
        <span><strong>Materialization</strong> · Checkpointed output</span>
        {canEdit && <Button variant="ghost" size="sm" onClick={onEdit} className="h-auto px-2 py-1 text-[10.5px] font-medium text-primary shadow-none">Edit materialization</Button>}
      </div>}
    </div>
  </Section>
}

function OutputSchemaSummary({ summary, stale, canEdit, onEdit }: {
  summary: string
  stale: boolean
  canEdit: boolean
  onEdit: () => void
}) {
  return <Section title="Output schema">
    <div className="grid gap-1.5 text-[11px]">
      <div className="flex items-center justify-between gap-2">
        <span>{summary}{stale && <span className="text-amber-700 dark:text-amber-300"> · Needs review</span>}</span>
        {canEdit && <Button variant="ghost" size="sm" onClick={onEdit} className="h-auto px-2 py-1 text-[10.5px] font-medium text-primary shadow-none">{stale ? 'Review output schema' : 'Edit output schema'}</Button>}
      </div>
    </div>
  </Section>
}

function CheckpointToggle({ nodeId, embedded = false }: { nodeId: string; embedded?: boolean }) {
  const doc = useStore((s) => s.doc)
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const on = !!(node?.data.config as Record<string, unknown>)?.checkpoint
  const available = canEnableLinearCheckpoint(doc, nodeId)
  const disabled = !available && !on
  return (
    <Section title="Materialization" embedded={embedded}>
      <button data-testid="checkpoint-toggle" disabled={disabled}
        onClick={() => updateConfig(nodeId, { checkpoint: on ? undefined : true })}
        className="flex w-full items-start gap-2 rounded-md border border-border px-2.5 py-2 text-left hover:bg-accent disabled:cursor-not-allowed disabled:opacity-60">
        <span className={cn('mt-[3px] h-2.5 w-2.5 shrink-0 rounded-full', on ? 'bg-primary' : 'border border-muted-foreground')} />
        <span className="min-w-0 flex-1">
          <span className="block text-[11.5px] font-medium text-foreground">{on ? 'Checkpointed' : 'Checkpoint here'}</span>
          <span className="mt-0.5 block text-[10.5px] leading-snug text-muted-foreground">
            {on ? 'Output materialized — inspectable and reused across runs.'
              : available ? 'Materialize this step’s output.'
                : 'Checkpoints are available only for Source → Select → Write.'}
          </span>
        </span>
      </button>
    </Section>
  )
}

function ResourcesSection({ nodeId, embedded = false }: { nodeId: string; embedded?: boolean }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const req = ((node?.data.config as Record<string, unknown>)?.requires ?? {}) as { cpu?: number; gpu?: number; gpuType?: string }
  const set = (patch: Record<string, unknown>) => {
    const next: Record<string, unknown> = { ...req, ...patch }
    for (const k of Object.keys(next)) if (next[k] === '' || next[k] == null) delete next[k]
    updateConfig(nodeId, { requires: Object.keys(next).length ? next : undefined })
  }
  const num = (v: string) => (v === '' ? undefined : Number(v))
  return (
    <Section title="Resources (placement)" embedded={embedded}>
      <div className="mb-1.5 text-[10.5px] leading-relaxed text-muted-foreground">
        What this step needs — the run routes to a worker that satisfies it (e.g. a GPU pool). Blank = no requirement.
      </div>
      <div className="grid grid-cols-3 gap-1.5">
        <label className="flex flex-col gap-0.5 text-[10px] text-muted-foreground">GPUs
          <Input type="number" min={0} className="h-7 text-[11.5px] md:text-[11.5px]" value={req.gpu ?? ''} onChange={(e) => set({ gpu: num(e.target.value) })} /></label>
        <label className="flex flex-col gap-0.5 text-[10px] text-muted-foreground">GPU type
          <Input className="h-7 text-[11.5px] md:text-[11.5px]" placeholder="a100" value={req.gpuType ?? ''} onChange={(e) => set({ gpuType: e.target.value || undefined })} /></label>
        <label className="flex flex-col gap-0.5 text-[10px] text-muted-foreground">CPUs
          <Input type="number" min={0} className="h-7 text-[11.5px] md:text-[11.5px]" value={req.cpu ?? ''} onChange={(e) => set({ cpu: num(e.target.value) })} /></label>
      </div>
    </Section>
  )
}

const CARD_TONE: Record<string, string> = {
  '1:1': 'bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-300',
  '1:N': 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
  'N:1': 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
  'N:M': 'bg-rose-100 text-rose-700 dark:bg-rose-500/15 dark:text-rose-300',
  unknown: 'bg-muted text-muted-foreground',
}

function hasCompleteJoinInputs(doc: CanvasDoc, nodeId: string): boolean {
  const incoming = doc.edges.filter((edge) => edge.target === nodeId)
  return incoming.length === 2
    && incoming.filter((edge) => edge.targetHandle === 'a').length === 1
    && incoming.filter((edge) => edge.targetHandle === 'b').length === 1
}

// Join hints (catalog-driven): the backend suggests key columns for the two inputs, with the join
// cardinality MEASURED from the data. Clicking a suggestion fills the same key-pair contract the card uses. A
// non-1:1 join gets a fan-out warning (the result lands at the finer grain — rows multiply).
function JoinHints({ nodeId }: { nodeId: string }) {
  const doc = useStore((s) => s.doc)
  const parameterBindings = useStore((s) => s.runs[nodeId]?.parameterBindings)
  const updateConfig = useStore((s) => s.updateConfig)
  const [analysis, setAnalysis] = useState<JoinAnalysis | null>(null)
  const [analysisFailed, setAnalysisFailed] = useState(false)
  const [loading, setLoading] = useState(true)  // first analysis is pending → show 'Analyzing…', not 'no matches'
  const inputsComplete = hasCompleteJoinInputs(doc, nodeId)
  // re-analyze when the graph shape or any node's config changes (debounced); positions don't matter
  const sig = JSON.stringify([doc.edges.map((e) => [e.source, e.target, e.targetHandle]),
    doc.nodes.map((n) => [n.id, n.type, n.data.config]), parameterBindings ?? []])
  useEffect(() => {
    let off = false
    setAnalysis(null)
    setAnalysisFailed(false)
    setLoading(true)
    if (!inputsComplete) return
    const t = setTimeout(() => {
      api.joinAnalysis(doc, nodeId, parameterBindings)
        .then((a) => { if (!off) setAnalysis(a) })
        .catch(() => { if (!off) setAnalysisFailed(true) })
        .finally(() => { if (!off) setLoading(false) })
    }, 300)
    return () => { off = true; clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId, sig, inputsComplete])

  const apply = (s: JoinSuggestion) => {
    updateConfig(nodeId, serializeJoinKeys(s.leftColumns.map((left, index) => ({ left, right: s.rightColumns[index] ?? '' }))))
  }

  if (!inputsComplete) return null

  const suggestions = analysis?.suggestions ?? []
  return (
    <Section title="Join hints">
      {analysis?.warning && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-[10.5px] leading-relaxed text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300">
          ⚠ {analysis.warning}
        </div>
      )}
      {analysis?.blockingCode && (
        <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-[10.5px] leading-relaxed text-destructive">
          {analysis.blockingCode}: this configured key targets a different retained dataset. Cardinality cannot make it safe.
        </div>
      )}
      {suggestions.length === 0 ? (
        <div className="text-[10.5px] leading-relaxed text-muted-foreground">
          {loading ? 'Analyzing keys…'
            : analysisFailed || !analysis ? 'Key suggestions are unavailable. Choose join keys manually.'
              : (analysis.note ?? 'No matching key columns between the two inputs.')}
        </div>
      ) : (
        <div className="flex flex-col gap-1">
          {suggestions.slice(0, 6).map((s, i) => (
            <button key={i} onClick={() => apply(s)} title={`${s.reason} · apply to the join`}
              className="flex items-center gap-2 rounded-md border border-border px-2 py-1.5 text-left hover:bg-accent">
              <span className="dp-mono flex-1 truncate text-[10.5px] text-foreground">
                {s.leftColumns.join('+')} = {s.rightColumns.join('+')}
              </span>
              <span className={cn('rounded px-1.5 py-px text-[9.5px] font-semibold', CARD_TONE[s.cardinality] ?? CARD_TONE.unknown)}>{s.cardinality}</span>
              {(s.rowReference ?? []).some((diagnosis) => diagnosis.status === 'compatible') && <span className="rounded bg-green-100 px-1.5 py-px text-[9.5px] font-semibold text-green-700 dark:bg-green-500/15 dark:text-green-300">reference match</span>}
              {(s.rowReference ?? []).some((diagnosis) => diagnosis.status === 'unknown') && <span className="rounded bg-muted px-1.5 py-px text-[9.5px] text-muted-foreground">reference unknown</span>}
            </button>
          ))}
          <div className="text-[9.5px] leading-relaxed text-muted-foreground">Cardinality measured from the data · click to fill the join key.</div>
        </div>
      )}
    </Section>
  )
}

// Keep the ordinary Inspector focused on the execution path and actionable warnings. Scheduler
// identities and handoff mechanics remain available in the disclosure when somebody needs them.
type PlanRegion = { id: string; outputNode: string; backend: string; tier: string | null; rows: number | null; confidence: string; requires?: string; unsatisfied?: boolean; available?: string; preflight?: string[] }
function backendLabel(backend: string) {
  if (backend === 'default') return 'local'
  return backend.toLowerCase().startsWith('ray') ? 'Ray' : backend
}

function RunPlan({ nodeId }: { nodeId: string }) {
  const doc = useStore((s) => s.doc)
  const parameterBindings = useStore((s) => s.runs[nodeId]?.parameterBindings)
  const kernelUp = useStore((s) => s.kernelUp)
  const [regions, setRegions] = useState<PlanRegion[] | null>(null)
  const sig = JSON.stringify([doc.edges.map((e) => [e.source, e.target, e.targetHandle]),
    doc.nodes.map((n) => [n.id, n.type, n.data.config]), parameterBindings ?? []])
  useEffect(() => {
    if (!kernelUp) { setRegions(null); return }
    let off = false
    const t = setTimeout(() => {
      api.plan(doc, nodeId, parameterBindings).then((p) => { if (!off) setRegions(p.regions ?? []) }).catch(() => { if (!off) setRegions(null) })
    }, 350)
    return () => { off = true; clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId, sig, kernelUp])

  // trivial = a single region on the local/default backend with no unmet requirement → nothing worth
  // showing (the card already shows ~N rows). Surface when placement split (>1), routed off-local, or a
  // resource requirement went unsatisfied (a pre-flight "this won't fit here" before you run).
  if (!regions || (regions.length <= 1 && regions.every((r) => r.backend === 'default' && !r.unsatisfied && !(r.preflight && r.preflight.length)))) return null
  const fmt = (n: number | null) => (n == null ? '?' : n.toLocaleString())
  const multi = regions.length > 1
  const backends = Array.from(new Set(regions.map((region) => backendLabel(region.backend))))
  const warnings = Array.from(new Set(regions.flatMap((region) => [
    ...(region.unsatisfied
      ? [`Needs ${region.requires || 'resources'} — ${region.available || 'no configured backend provides it'}.`]
      : []),
    ...(region.preflight ?? []),
  ])))
  return (
    <Section title="Execution path">
      <div data-testid="run-plan-summary" className="text-[11px] text-foreground">
        {multi ? `${regions.length} execution groups · ` : ''}{backends.join(' + ')}
      </div>
      {warnings.map((warning) => (
        <div key={warning} role="alert"
          className="rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-[10.5px] leading-relaxed text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300">
          {warning}
        </div>
      ))}
      <details data-testid="run-plan-details" className="text-[10.5px] text-muted-foreground">
        <summary className="cursor-pointer font-semibold text-foreground">Run plan</summary>
        <div className="mt-2 flex flex-col gap-1">
          {regions.map((r, i) => (
            <div key={r.id} className={cn('flex flex-wrap items-center gap-2 rounded-md border px-2 py-1',
              r.unsatisfied ? 'border-amber-300 dark:border-amber-500/40' : 'border-border')}>
              <span className={cn('rounded px-1.5 py-px text-[9.5px] font-semibold',
                r.backend === 'default' ? 'bg-muted text-muted-foreground' : 'bg-blue-100 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300')}>
                {r.backend === 'default' ? 'local' : r.backend}
              </span>
              <span className="dp-mono flex-1 truncate text-foreground">{r.outputNode}</span>
              <span className="tabular-nums" title={r.confidence === 'bounded' ? 'Estimated upper bound' : undefined}>{r.confidence === 'unknown' ? '' : `${r.confidence === 'bounded' ? '≤ ' : ''}${fmt(r.rows)}`}</span>
              {r.requires && (
                <span className="rounded bg-muted px-1.5 py-px text-[9px]" title="declared resource requirement">needs {r.requires}</span>
              )}
              {multi && i < regions.length - 1 && r.tier && (
                <span className="rounded bg-muted px-1.5 py-px text-[9px]" title="materialization tier for the handoff">→ {r.tier}</span>
              )}
            </div>
          ))}
        </div>
      </details>
    </Section>
  )
}

type ExactDetailState = 'idle' | 'loading' | 'available' | 'unavailable' | 'permission' | 'offline' | 'error'

function exactDetailState(error: unknown): Exclude<ExactDetailState, 'idle' | 'loading' | 'available'> {
  const facts = typeof error === 'object' && error !== null ? error as { code?: unknown; status?: unknown } : {}
  if (facts.code === 'permission_denied' || facts.status === 403) return 'permission'
  if (facts.code === 'service_unavailable' || facts.status === 503) return 'offline'
  if (facts.code === 'resource_gone' || facts.status === 404 || facts.status === 410) return 'unavailable'
  return 'error'
}

function sourceTable(catalog: CatalogTable[], config: Record<string, unknown>): CatalogTable | undefined {
  const registrationId = typeof config.registrationId === 'string' ? config.registrationId : undefined
  if (registrationId) return catalog.find((table) => table.registrationId === registrationId)
  const tableId = typeof config.tableId === 'string' ? config.tableId : undefined
  const uri = typeof config.uri === 'string' ? config.uri : ''
  return catalog.find((table) => (tableId && table.id === tableId) || table.uri === uri || table.name === uri)
}

function sourceInspectorSummary(catalog: CatalogTable[], config: Record<string, unknown>): string | null {
  const table = hasLocalSourceIdentity(config) ? sourceTable(catalog, config) : undefined
  const parameter = sourceDatasetParameter(config)
  const selectedRef = sourceDatasetRef(config)
  const exact = selectedRef ? datasetRefIdentity(selectedRef) : null
  const provider = hasProviderSourceIdentity(config)
  const source = provider
    ? (typeof config.providerName === 'string' ? config.providerName : 'Mounted provider')
    : hasLocalSourceIdentity(config)
      ? 'Local catalog'
      : 'Selected dataset'

  if (hasBoundSourceIdentity(config)) {
    if (parameter) return hasLocalSourceIdentity(config) || provider
      ? `${source} · Run-time dataset parameter`
      : 'Run-time dataset parameter'
    const version = exact
      ? exact.revisionId.length > 24 ? 'Selected exact version' : `Exact version ${exact.revisionId}`
      : 'Current version'
    if (!exact && table) {
      const rows = table.rowCount == null
        ? 'Rows unknown'
        : `${table.rowCount.toLocaleString()} ${table.rowCount === 1 ? 'row' : 'rows'}`
      const columns = `${table.columns.length} ${table.columns.length === 1 ? 'column' : 'columns'}`
      return `${source} · ${version} · ${rows} · ${columns}`
    }
    return `${source} · ${version}`
  }

  const uri = sourceUri(config)
  if (uri) return isDelimitedTextUri(uri) ? 'Manual URI · Delimited text' : 'Manual URI'
  return null
}

function DraftSourceInspector({ nodeId, canEdit, onUriEditingChange }: {
  nodeId: string
  canEdit: boolean
  onUriEditingChange: (editing: boolean) => void
}) {
  const config = useStore((s) => (s.doc.nodes.find((node) => node.id === nodeId)?.data.config ?? {}) as Record<string, unknown>)
  const updateConfig = useStore((s) => s.updateConfig)
  return <>
    <Section title="Choose data">
      <div className="text-[11.5px] leading-relaxed text-muted-foreground">
        Choose a dataset in Workspace, upload a local file, or register a path the kernel can access.
      </div>
      <div className="grid gap-1.5">
        <CodeBtn icon="db" label="Select dataset" disabled={!canEdit}
          onClick={() => requestSourceEntryAction(nodeId, 'select')} />
        <CodeBtn icon="export" label="Upload a file…" disabled={!canEdit}
          onClick={() => requestSourceEntryAction(nodeId, 'upload')} />
        <CodeBtn icon="search" label="Register or browse an accessible path…" disabled={!canEdit}
          onClick={() => requestSourceEntryAction(nodeId, 'browse')} />
      </div>
    </Section>
    <details className="mx-3.5 mt-3 rounded-md border border-border bg-muted/20 px-2 py-1.5 text-[10.5px]">
      <summary className="cursor-pointer font-semibold text-foreground">Advanced source configuration</summary>
      <div className="mt-2 grid gap-3">
        <EditOnly enabled={canEdit}>
          <div className="grid gap-2">
            <Label className="text-[9.5px] font-bold uppercase tracking-[0.4px] text-muted-foreground" htmlFor={`source-uri-${nodeId}`}>Dataset URI</Label>
            <Input id={`source-uri-${nodeId}`} aria-label="Dataset URI" value={String(config.uri ?? '')}
              onChange={(event) => {
                onUriEditingChange(true)
                updateConfig(nodeId, { uri: event.target.value })
              }}
              // Keep the advanced editor mounted through the click that moves focus away. A
              // synchronous mode switch here removes an Inspector action between mousedown and
              // click, so the user's first Count/View action is silently lost.
              onBlur={() => window.setTimeout(() => onUriEditingChange(false), 0)}
              className={cn(miniInputClass, 'text-[11px] md:text-[11px]')} />
            {isDelimitedTextUri(sourceUri(config)) && <>
              <Label className="text-[9.5px] font-bold uppercase tracking-[0.4px] text-muted-foreground" htmlFor={`source-delimiter-${nodeId}`}>CSV delimiter</Label>
              <Input id={`source-delimiter-${nodeId}`} aria-label="CSV delimiter" value={String(config.delimiter ?? '')}
                onChange={(event) => updateConfig(nodeId, { delimiter: event.target.value })}
                className={cn(miniInputClass, 'text-[11px] md:text-[11px]')} />
              <Label className="text-[9.5px] font-bold uppercase tracking-[0.4px] text-muted-foreground" htmlFor={`source-header-${nodeId}`}>CSV header row</Label>
              <select id={`source-header-${nodeId}`} aria-label="CSV header row" value={String(config.header ?? 'auto')}
                onChange={(event) => updateConfig(nodeId, { header: event.target.value })}
                className={cn(miniInputClass, 'bg-background text-[11px] md:text-[11px]')}>
                <option value="auto">auto</option><option value="yes">yes</option><option value="no">no</option>
              </select>
            </>}
          </div>
        </EditOnly>
        <SourceConnectionDetails nodeId={nodeId} embedded />
      </div>
    </details>
  </>
}

function SourceConnectionDetails({ nodeId, embedded = false }: { nodeId: string; embedded?: boolean }) {
  const node = useStore((s) => s.doc.nodes.find((candidate) => candidate.id === nodeId))
  const catalog = useStore((s) => s.catalog)
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<DatasetRevisionDetail | null>(null)
  const [detailState, setDetailState] = useState<ExactDetailState>('idle')

  const config = (node?.data.config ?? {}) as Record<string, unknown>
  const local = hasLocalSourceIdentity(config)
  const table = local ? sourceTable(catalog, config) : undefined
  const parameter = sourceDatasetParameter(config)
  const selectedRef = sourceDatasetRef(config)
  const exact = selectedRef ? datasetRefIdentity(selectedRef) : null
  const provider = hasProviderSourceIdentity(config)
  const providerName = typeof config.providerName === 'string' ? config.providerName : undefined
  const sourceLabel = provider
    ? (providerName ?? 'Mounted provider')
    : local
      ? 'Local catalog'
      : parameter
        ? 'Run-time dataset parameter'
        : selectedRef
          ? 'Selected dataset'
          : sourceUri(config)
            ? 'Manual URI'
            : 'Not bound'

  useEffect(() => {
    let live = true
    setDetail(null)
    if (!open || !exact) { setDetailState('idle'); return () => { live = false } }
    setDetailState('loading')
    void api.datasetRevision(exact.datasetId, exact.revisionId).then((next) => {
      if (!live) return
      setDetail(next); setDetailState('available')
    }).catch((error) => {
      if (live) setDetailState(exactDetailState(error))
    })
    return () => { live = false }
  }, [exact?.datasetId, exact?.revisionId, open])

  const columns = exact ? detail?.preview.columns : table?.columns
  const values: Array<[string, string]> = [
    ['Source', sourceLabel],
  ]
  const stringValue = (key: string) => typeof config[key] === 'string' ? config[key] as string : undefined
  const add = (label: string, value: string | null | undefined) => { if (value) values.push([label, value]) }
  if (parameter) add('Dataset parameter', parameter.parameterRef)
  if (provider) {
    add('Provider resource', stringValue('providerResourceRef'))
    add('Provider mount', stringValue('providerMountId'))
    add('Provider source binding', stringValue('providerSourceBindingId'))
  } else if (local) {
    add('Catalog registration', table?.registrationId ?? stringValue('registrationId') ?? table?.id)
  }
  add('Dataset location', stringValue('uri'))
  if (exact) {
    add('Exact dataset identity', exact.datasetId)
    add('Exact revision identity', exact.revisionId)
  }
  if (selectedRef?.kind === 'as_of') add('As-of selection (UTC)', selectedRef.asOf)

  if (!node) return null

  const details = (
    <details className="rounded-md border border-border bg-muted/20 px-2 py-1.5 text-[10.5px]" onToggle={(event) => setOpen(event.currentTarget.open)}>
        <summary className="cursor-pointer font-semibold text-foreground">Connection details</summary>
        <div aria-label="Source connection details" className="mt-2 grid gap-2">
          <div className="text-[10px] leading-relaxed text-muted-foreground">Identifiers are shown here for inspection and copying; they do not replace the selected version.</div>
          <dl className="grid gap-1.5">
            {values.map(([label, value]) => <ConnectionFact key={label} label={label} value={value} />)}
          </dl>
          {exact && detailState === 'loading' && <div role="status" className="text-muted-foreground">Loading selected version fields…</div>}
          {exact && detailState === 'unavailable' && <div role="alert" className="text-destructive">The selected version is unavailable. The current dataset was not substituted.</div>}
          {exact && detailState === 'permission' && <div role="alert" className="text-destructive">Permission to inspect the selected version was lost.</div>}
          {exact && detailState === 'offline' && <div role="alert" className="text-destructive">The provider is offline; selected version fields cannot be checked.</div>}
          {exact && detailState === 'error' && <div role="alert" className="text-destructive">Selected version fields could not be loaded.</div>}
          {columns && <div>
            <div className="mb-1 text-[9px] font-bold uppercase tracking-wide text-muted-foreground">Field evidence · {columns.length} {columns.length === 1 ? 'column' : 'columns'}</div>
            {columns.length ? <div className="grid max-h-32 gap-0.5 overflow-y-auto rounded border border-border bg-background/60 p-1">
              {columns.map((column) => <FieldEvidenceButton key={column.name} column={column} marker className="dp-mono truncate rounded px-1 py-0.5 text-left hover:bg-accent" />)}
            </div> : <div className="text-muted-foreground">No fields were supplied for this version.</div>}
          </div>}
        </div>
    </details>
  )
  return embedded ? details : <Section title="Data source">{details}</Section>
}

function ConnectionFact({ label, value }: { label: string; value: string }) {
  const copy = () => { if (navigator.clipboard) void navigator.clipboard.writeText(value) }
  return <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-2 gap-y-0.5">
    <dt className="text-muted-foreground">{label}</dt>
    <button type="button" aria-label={`Copy ${label}`} title={`Copy ${label}`} onClick={copy}
      className="rounded px-1 text-[9px] font-semibold text-primary hover:bg-accent">Copy</button>
    <dd className="col-span-2 break-all rounded bg-background/70 px-1.5 py-1 font-mono text-[9.5px] text-foreground">{value}</dd>
  </div>
}

function Section({ title, children, embedded = false }: { title: string; children: React.ReactNode; embedded?: boolean }) {
  const contents = <>
    <div className="text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground">{title}</div>
    {children}
  </>
  if (embedded) return <div className="flex flex-col gap-2">{contents}</div>
  return (
    <div className="flex flex-col gap-2 border-b border-border px-3.5 py-3">
      {contents}
    </div>
  )
}

// a port's display name: only real labels (join left/right, metric value) are named; the plain
// default in/out ports are nameless — their wire type is the meaningful label.
function portName(p: { id: string; label?: string }): string | null {
  if (p.label && p.label !== p.id) return p.label
  return p.id === 'in' || p.id === 'out' ? null : p.id
}

export function PortRow({ dir, name, wire, schema }: {
  dir: 'in' | 'out'; name: string | null; wire: string; schema?: ColumnSchema[] | null
}) {
  const [open, setOpen] = useState(false)
  const cols = Array.isArray(schema) ? schema : null
  const badge = schema === undefined ? null : cols === null ? 'untyped' : `${cols.length} cols`
  const expandable = !!cols && cols.length > 0
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center gap-[7px]">
        <span className="w-[26px] text-[8.5px] font-bold tracking-[0.4px] text-muted-foreground">{dir === 'in' ? 'IN' : 'OUT'}</span>
        {name && <span className="text-foreground">{name}</span>}
        <span className="flex-1 text-[10.5px] text-muted-foreground">{wire}</span>
        {badge && (
          <button type="button" disabled={!expandable} onClick={() => setOpen((o) => !o)}
            title={expandable ? (open ? 'Hide columns' : 'Show columns') : undefined}
            className={cn('inline-flex items-center gap-0.5 rounded px-1.5 py-px text-[9.5px]',
              cols === null ? 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300'
                : 'bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-300',
              expandable && 'cursor-pointer hover:opacity-80')}>
            {expandable && <Icon name={open ? 'chevronDown' : 'chevronRight'} size={9} />}
            {badge}
          </button>
        )}
      </div>
      {open && expandable && (
        <div className="ml-[33px] flex flex-col gap-px rounded border border-border bg-muted/40 p-1">
          {cols!.map((c, i) => (
            <div key={i} className="flex items-baseline justify-between gap-2 text-[10px]">
              <FieldEvidenceButton column={c} marker className="dp-mono truncate rounded px-0.5 text-left text-foreground hover:bg-accent" />
              <span className="dp-mono flex-none text-muted-foreground">{c.type}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Schema contract for a code op (transform / plugin / vector-search): untyped until it runs. The user
// can DECLARE the output columns (types this port + everything downstream via a typed stand-in) or
// INFER them from a bounded sample run. Both write config.outputSchema — declaring is just the manual
// path, inferring auto-fills it. Clearing it returns the port to untyped (dynamic) — all fine.
function SchemaContract({ nodeId, runnable, embedded = false }: { nodeId: string; runnable: boolean; embedded?: boolean }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const cfg = (node?.data.config ?? {}) as Record<string, unknown>
  const declared = (Array.isArray(cfg.outputSchema) ? cfg.outputSchema : []) as ColumnSchema[]
  // outputSchema can instead be {ref: name} → this node REFERENCES a named workspace contract
  const os = cfg.outputSchema as { ref?: string } | undefined
  const refName = os && !Array.isArray(os) && typeof os === 'object' ? os.ref : undefined
  const enforce = !!cfg.enforceSchema
  const source = cfg.outputSchemaSource as string | undefined
  const contractText = schemaContractText(node?.type ?? '', cfg)
  const code = contractText == null ? null : String(contractText)
  // the contract may be stale if the code/SQL changed since it was pinned
  const stale = schemaContractStale(node?.type ?? '', cfg)
  const [names, setNames] = useState<string[]>([])       // named contracts available to reference
  const [refCols, setRefCols] = useState<ColumnSchema[]>([])
  const inferRequestGeneration = useRef(0)
  useEffect(() => () => { inferRequestGeneration.current += 1 }, [])
  useEffect(() => { api.listSchemas().then((s) => setNames(s.map((x) => x.name))).catch(() => {}) }, [])
  useEffect(() => {
    if (!refName) { setRefCols([]); return }
    api.listSchemas().then((s) => setRefCols(s.find((x) => x.name === refName)?.columns ?? [])).catch(() => setRefCols([]))
  }, [refName])
  const setEnforce = (on: boolean) => updateConfig(nodeId, { enforceSchema: on || undefined })
  const reference = (name: string) => updateConfig(nodeId, { outputSchema: name ? { ref: name } : undefined, outputSchemaSource: undefined, outputSchemaCodeHash: undefined })
  const saveAsNamed = async () => {
    const name = window.prompt('Save these columns as a named contract:')?.trim()
    if (!name) return
    try { await api.saveSchema(name, declared); setNames((n) => Array.from(new Set([...n, name]))) }
    catch (e) { setErr(e instanceof Error ? e.message : 'save failed') }
  }

  // a manual edit (no explicit src) takes ownership → 'declared'; only "Infer from sample" sets 'inferred'.
  // pin the current cell's hash alongside, so a later cell edit can flag the contract as possibly stale.
  const commit = (cols: ColumnSchema[], src: 'declared' | 'inferred' = 'declared') =>
    updateConfig(nodeId, {
      outputSchema: cols.length ? cols : undefined,
      outputSchemaSource: cols.length ? src : undefined,
      outputSchemaCodeHash: cols.length && code != null ? codeHash(code) : undefined,
    })

  const infer = async () => {
    setBusy(true); setErr(null)
    const doc = useStore.getState().doc
    const planIdentity = previewPlanIdentity(doc, nodeId)
    const parameterBindings = useStore.getState().runs[nodeId]?.parameterBindings ?? []
    const parameterIdentity = parameterBindingsIdentity(parameterBindings)
    const requestGeneration = ++inferRequestGeneration.current
    const current = () => (
      inferRequestGeneration.current === requestGeneration
      && previewPlanIdentity(useStore.getState().doc, nodeId) === planIdentity
      && parameterBindingsIdentity(useStore.getState().runs[nodeId]?.parameterBindings) === parameterIdentity
    )
    const changedMessage = 'The graph or parameter bindings changed while the sample was loading. Infer again for the current inputs.'
    try {
      let res: Awaited<ReturnType<typeof api.preview>>
      if (doc.nodes.find((candidate) => candidate.id === nodeId)?.type === 'transform') {
        try {
          res = await api.retainedEditorPreview(
            doc, nodeId, 50, 0, undefined, parameterBindings,
          )
        } catch (e) {
          if (!(e instanceof KernelError) || !e.code
              || !RETAINED_PREVIEW_FALLBACK_CODES.has(e.code)) throw e
          if (inferRequestGeneration.current !== requestGeneration) return
          if (!current()) {
            setErr(changedMessage)
            return
          }
          res = await api.preview(
            doc, nodeId, 50, 0, undefined, undefined, parameterBindings,
          )
        }
      } else {
        res = await api.preview(
          doc, nodeId, 50, 0, undefined, undefined, parameterBindings,
        )
      }
      if (inferRequestGeneration.current !== requestGeneration) return
      if (!current()) {
        setErr(changedMessage)
      } else if (res.error || res.notPreviewable) setErr(res.reason || 'could not infer — run needs a full pass')
      else if (res.columns?.length) commit(res.columns as ColumnSchema[], 'inferred')
      else setErr('no columns produced on the sample')
    } catch (e) {
      if (inferRequestGeneration.current === requestGeneration) {
        setErr(current()
          ? e instanceof Error ? e.message : 'infer failed'
          : changedMessage)
      }
    } finally {
      if (inferRequestGeneration.current === requestGeneration) setBusy(false)
    }
  }

  return (
    <Section title="Output schema (contract)" embedded={embedded}>
      {refName ? (
        <>
          <div className="text-[10.5px] leading-relaxed text-muted-foreground">
            References the named contract <span className="dp-mono text-foreground">{refName}</span> — shared across pipelines; edit it in the schema registry.
          </div>
          {refCols.map((c, i) => (
            <div key={i} className="flex items-center gap-2 text-[11px]">
              <span className="dp-mono min-w-0 flex-1 overflow-hidden text-ellipsis text-foreground">{c.name}</span>
              <span className="dp-mono w-[80px] flex-none text-muted-foreground">{c.type}</span>
            </div>
          ))}
          <div className="mt-0.5 flex flex-wrap items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => reference('')}
              className="h-auto px-2 py-1 text-[10.5px] font-medium text-muted-foreground shadow-none">Unlink</Button>
          </div>
        </>
      ) : (
        <>
          <div className="text-[10.5px] leading-relaxed text-muted-foreground">
            {declared.length
              ? (source === 'inferred' ? 'Inferred from a sample — edit to pin it as the contract.' : 'Declared — types this port and everything downstream.')
              : 'Untyped until it runs. Declare a contract, infer it, or reference a named one. Leave empty to stay dynamic.'}
          </div>
          {stale && (
            <div className="rounded-md border border-amber-300 bg-amber-50 px-2 py-1 text-[10px] leading-relaxed text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300">
              ⚠ The {node?.type === 'sql' ? 'SQL' : 'cell'} changed since this contract was pinned — it may be stale. Re-infer or edit to re-pin.
            </div>
          )}
          {declared.map((c, i) => (
            <div key={i} className="flex items-center gap-1">
              <Input value={c.name} placeholder="column"
                onChange={(e) => commit(declared.map((x, j) => (j === i ? { ...x, name: e.target.value.replace(/\s+/g, '_') } : x)))}
                className={cn(miniInputClass, 'dp-mono min-w-0 flex-1 text-[11px] md:text-[11px]')} />
              <Input value={c.type} placeholder="type"
                onChange={(e) => commit(declared.map((x, j) => (j === i ? { ...x, type: e.target.value } : x)))}
                className={cn(miniInputClass, 'dp-mono w-[80px] flex-none text-[11px] md:text-[11px]')} />
              <Button variant="ghost" size="icon" onClick={() => commit(declared.filter((_, j) => j !== i))} title="Remove column"
                className="h-5 w-5 flex-none text-muted-foreground [&_svg]:size-3"><Icon name="close" size={11} /></Button>
            </div>
          ))}
          <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
            <Button variant="outline" size="sm"
              onClick={() => commit([...declared, { name: `col${declared.length + 1}`, type: 'string', capabilities: [] }])}
              className="h-auto gap-1 self-start border-dashed px-2 py-1 text-[10.5px] font-medium text-muted-foreground shadow-none [&_svg]:size-3">
              <Icon name="plus" size={11} /> add column
            </Button>
            <Button variant="outline" size="sm" disabled={busy || !runnable} onClick={infer}
              title={runnable ? 'Run a bounded sample to resolve the output columns' : 'Wire a runnable input first'}
              className="h-auto gap-1 px-2 py-1 text-[10.5px] font-medium text-primary shadow-none [&_svg]:size-3">
              <Icon name="eye" size={11} /> {busy ? 'Inferring…' : 'Infer from sample'}
            </Button>
            {declared.length > 0 && (
              <Button variant="ghost" size="sm" onClick={saveAsNamed}
                className="h-auto px-2 py-1 text-[10.5px] font-medium text-primary shadow-none" title="Save these columns as a named, versioned workspace contract">Save as named…</Button>
            )}
            {declared.length > 0 && (
              <Button variant="ghost" size="sm" onClick={() => commit([])}
                className="h-auto px-2 py-1 text-[10.5px] font-medium text-muted-foreground shadow-none">Clear</Button>
            )}
          </div>
          {names.length > 0 && (
            <select value="" onChange={(e) => e.target.value && reference(e.target.value)}
              className={cn(miniInputClass, 'mt-1 text-[10.5px] text-muted-foreground')} title="Reference a named workspace contract">
              <option value="">Reference a named contract…</option>
              {names.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          )}
        </>
      )}
      {(declared.length > 0 || refName) && (
        <label className="mt-1 flex items-center gap-1.5 text-[10.5px] text-muted-foreground" title="Fail the run if the actual output columns drift from this contract (missing / unexpected / retyped)">
          <input type="checkbox" checked={enforce} onChange={(e) => setEnforce(e.target.checked)} /> Enforce (fail the run on drift)
        </label>
      )}
      {err && <div className="text-[10px] leading-relaxed text-amber-700 dark:text-amber-300">⚠ {err}</div>}
    </Section>
  )
}

function Action({ icon, label, onClick, disabled, danger }: { icon: IconName; label: string; onClick: () => void; disabled?: boolean; danger?: boolean }) {
  return (
    <Button
      variant="outline" size="sm"
      disabled={disabled}
      onClick={() => { if (!disabled) onClick() }}
      aria-disabled={disabled}
      className={cn(
        'h-auto gap-1.5 px-2 py-1.5 text-[11.5px] font-medium shadow-none [&_svg]:size-3',
        danger ? 'text-destructive' : 'text-muted-foreground',
        disabled && 'cursor-not-allowed opacity-50',
      )}>
      <Icon name={icon} size={12} /> {label}
    </Button>
  )
}
