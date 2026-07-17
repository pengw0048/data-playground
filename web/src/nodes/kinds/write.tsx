import { useEffect } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput, MiniSelect } from '../../ui/controls'

function Write({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const name = String(data.config.filename ?? data.config.name ?? '')
  const mode = (data.config.writeMode as 'append' | 'overwrite') ?? 'overwrite'
  const dest = (data.config.destName as string | undefined) ?? 'Workspace outputs'
  const prepareWrite = useStore((s) => s.prepareWrite)
  const admission = useStore((s) => s.runs[id]?.writeAdmission)
  const receipt = useStore((s) => s.runs[id]?.status?.outputs
    .find((output) => output.writeReceipt)?.writeReceipt)
  useEffect(() => {
    void prepareWrite(id).catch(() => { /* the Run panel surfaces actionable admission failures */ })
  }, [id, data.config, prepareWrite])
  const semantics = receipt
    ? `revision ${receipt.revisionId}`
    : admission?.managed
      ? admission.blocker ? `blocked · ${admission.blocker}` : `${admission.mode} · ${admission.expectedSchema.length} cols`
      : admission ? `${admission.mode} · ${admission.provider}` : 'checking destination…'
  return (
    <NodeCard id={id} data={data} metaOverride={name ? `→ ${dest} · ${semantics}` : 'name an output → (destination in the panel)'}>
      <div className="flex gap-2">
        <Field label="file name" style={{ flex: 1.6 }}>
          <MiniInput value={name} placeholder="output.parquet" onChange={(v) => updateConfig(id, { filename: v })} />
        </Field>
        <Field label="mode" style={{ flex: 1 }}>
          <MiniSelect value={mode} onChange={(v) => updateConfig(id, { writeMode: v })} options={[
            { value: 'overwrite', label: admission?.provider === 'managed-local-file' ? 'create / replace (auto)' : 'overwrite' },
            { value: 'append', label: admission?.provider === 'managed-local-lance' ? 'append (exact head)' : 'append' },
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
    blurb: 'materialize / commit to a registered dataset',
    defaultData: () => ({ title: 'write', status: 'draft', config: { writeMode: 'overwrite', filename: 'output.parquet' }, meta: 'sink · needs full pass', needsFullPass: true }),
  },
  Write,
)
