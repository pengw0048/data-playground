import { useEffect, useRef, useState } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore, writeAdmissionRequestIdentity } from '../../store/graph'
import { Field, MiniInput, MiniSelect } from '../../ui/controls'
import { managedDatasetNameErrorMessage } from '../../api/client'

function Write({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const name = String(data.config.filename ?? data.config.name ?? '')
  const mode = (data.config.writeMode as 'append' | 'overwrite') ?? 'overwrite'
  const dest = (data.config.destName as string | undefined) ?? 'Workspace outputs'
  const prepareWrite = useStore((s) => s.prepareWrite)
  const admissionIdentity = useStore((s) => writeAdmissionRequestIdentity(s, id))
  const admission = useStore((s) => {
    const run = s.runs[id]
    if (run?.phase === 'running') return run.writeAdmission
    if (run?.phase === 'done') return run.writeOutcomeAdmission
    return run?.writeAdmissionFingerprint === writeAdmissionRequestIdentity(s, id)
      ? run.writeAdmission : undefined
  })
  const runPhase = useStore((s) => s.runs[id]?.phase)
  const inFlightAdmissionRequests = useRef(
    new Map<string, ReturnType<typeof prepareWrite>>(),
  )
  const [nameError, setNameError] = useState<string | null>(null)
  const receipt = useStore((s) => s.runs[id]?.status?.outputs
    .find((output) => output.writeReceipt)?.writeReceipt
    ?? s.runs[id]?.writeOutcome?.receipt)
  const destinationLabel = dest
  const merge = data.config.mergeColumns as { taskId?: string; rules?: unknown[] } | undefined
  const upsert = data.config.keyedUpsert as { taskId?: string; keys?: unknown[] } | undefined
  useEffect(() => {
    if (runPhase === 'estimating' || runPhase === 'confirm'
        || runPhase === 'drift' || runPhase === 'running') return
    let request = inFlightAdmissionRequests.current.get(admissionIdentity)
    if (!request) {
      let tracked!: ReturnType<typeof prepareWrite>
      tracked = prepareWrite(id).finally(() => {
        if (inFlightAdmissionRequests.current.get(admissionIdentity) === tracked) {
          inFlightAdmissionRequests.current.delete(admissionIdentity)
        }
      })
      inFlightAdmissionRequests.current.set(admissionIdentity, tracked)
      request = tracked
    }
    let active = true
    void request.then(() => {
      if (active) setNameError(null)
    }).catch((error: unknown) => {
      if (active) setNameError(managedDatasetNameErrorMessage(error))
      // Other admission failures remain in the Run panel; this inline surface owns only this field.
    })
    return () => { active = false }
  // A terminal run deliberately drops its admission/submission identity so a later managed write
  // cannot reuse a completed request. Re-run the existing preflight when that happens: config is
  // unchanged, but the card still needs a truthful current destination summary. Active run intent
  // owns admission while it estimates, waits at a gate, or executes; the card must not race it.
  }, [id, admission, admissionIdentity, runPhase, prepareWrite])
  const displayName = admission?.intent?.destination.name ?? name
  const runtimeSchema = admission?.intent?.schemaMode === 'runtime'
  const semantics = receipt
    ? `Published · ${receipt.name || displayName || 'dataset'}`
    : admission?.managed
      ? admission.blocker ? 'needs attention' : runtimeSchema
        ? 'ready to run · columns checked during run'
        : 'ready to run'
      : admission ? 'ready to run' : 'checking output…'
  const mergeSemantics = merge?.taskId ? 'column merge tracked' : merge?.rules?.length ? 'column merge configured' : null
  const upsertSemantics = upsert?.taskId ? 'keyed upsert tracked' : upsert?.keys?.length ? 'keyed upsert configured' : null
  const cardSummary = receipt
    ? semantics
    : displayName
      ? `→ ${destinationLabel} · ${mergeSemantics ?? upsertSemantics ?? semantics}`
      : 'name an output → choose a destination in the panel'
  return (
    <NodeCard id={id} data={data} metaOverride={cardSummary}>
      <div className="grid gap-2">
        <Field label="output name">
          <MiniInput value={name} placeholder="output" invalid={nameError != null}
            onChange={(v) => updateConfig(id, { filename: v })} />
          {nameError && <span role="alert" className="text-[10px] leading-snug text-destructive">{nameError}</span>}
        </Field>
        <Field label="write mode">
          <MiniSelect value={mode} onChange={(v) => updateConfig(id, { writeMode: v })} options={[
            { value: 'overwrite', label: admission?.provider === 'managed-local-file' ? 'Create or replace' : 'replace output' },
            { value: 'append', label: admission?.provider === 'managed-local-lance' ? 'append to current version' : 'append' },
          ]} />
        </Field>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'write',
    title: 'write',
    category: 'io',
    tag: 'write',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample', 'selection'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'Save data to a file or managed dataset — scans all rows',
    defaultData: () => ({ title: 'write', status: 'draft', config: { writeMode: 'overwrite', filename: 'output' }, meta: 'sink · needs full pass', needsFullPass: true }),
  },
  Write,
)
