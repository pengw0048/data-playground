import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { getBackendSpec } from '../generic'
import { useStore } from '../../store/graph'
import { Field, MiniInput, miniSelectClass } from '../../ui/controls'
import { useInputColumnsForPort } from '../fields'
import { parseJoinKeys, serializeJoinCondition, serializeJoinKeys, type JoinKeyPair } from '../joinKeys'
import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import type { NodeConfig } from '../../types/graph'
import { JoinWithRelated } from '../../components/JoinWithRelated'

function Join({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const doc = useStore((s) => s.doc)
  const incoming = doc.edges.filter((edge) => edge.target === id)
  const config = doc.nodes.find((node) => node.id === id)?.data.config ?? data.config
  const on = String(config.on ?? '')
  const condition = String(config.condition ?? '')
  const how = (config.how as NodeConfig['how']) ?? 'inner'
  // derive the join types from the backend NodeSpec (source of truth: inner/left/right/outer) instead of
  // a hardcoded subset, so the card can't silently drift from what the engine supports (UX-05). The
  // fallback covers the brief pre-bootstrap window before backendSpecs is populated.
  const howOptions = ((getBackendSpec('join')?.params.find((p) => p.name === 'how')?.options
    ?? ['inner', 'left', 'right', 'outer']) as NonNullable<NodeConfig['how']>[])
  const leftColumns = useInputColumnsForPort(id, 'a')
  const rightColumns = useInputColumnsForPort(id, 'b')
  const connectedInput = incoming.length === 1 ? incoming[0].targetHandle : null
  const parsed = parseJoinKeys(on, condition)
  const [advanced, setAdvanced] = useState(parsed === null)
  // A half-selected pair is an editor draft, not an executable join. Keeping it locally lets a
  // researcher choose either side first without serializing (and then immediately losing) that
  // partial value. Persist only complete pairs through the existing on/condition contract.
  const [draftPairs, setDraftPairs] = useState<JoinKeyPair[]>(parsed?.length ? parsed : [{ left: '', right: '' }])
  const committed = useRef({ on, condition })
  // External config changes (notably a Join hint) must move back into the structured builder when
  // representable, while an unrepresentable predicate stays visibly raw and untouched.
  useEffect(() => {
    const ownCommit = committed.current.on === on && committed.current.condition === condition
    if (!ownCommit) {
      // Advanced is a user-selected editing mode. A raw predicate becoming representable must not
      // steal focus and switch controls beneath the user; only an unrepresentable condition forces
      // us into the safe raw editor.
      if (parsed === null) setAdvanced(true)
      setDraftPairs(parsed?.length ? parsed : [{ left: '', right: '' }])
    }
    committed.current = { on, condition }
  }, [condition, on])
  const pairs = draftPairs
  const effectiveCondition = condition || (parsed ? serializeJoinCondition(parsed) : '')
  const commit = (next: JoinKeyPair[]) => {
    setDraftPairs(next)
    const nextConfig = serializeJoinKeys(next)
    committed.current = nextConfig
    updateConfig(id, nextConfig)
  }
  const addPair = () => commit([...pairs, { left: '', right: '' }])
  return (
    <NodeCard id={id} data={data} metaOverride={`${how}${condition ? ` · on ${condition}` : on ? ` · on ${on}` : ''}`}>
      {incoming.length === 0 && (
        <p data-testid="join-missing-datasets" className="text-[10.5px] leading-snug text-muted-foreground">
          Choose a left dataset and a right dataset before this Join can run.
        </p>
      )}
      {connectedInput === 'a' && (
        <p data-testid="join-missing-right-dataset" className="text-[10.5px] leading-snug text-muted-foreground">
          Left dataset connected. Connect a right dataset to continue.
        </p>
      )}
      {connectedInput === 'b' && (
        <p data-testid="join-missing-left-dataset" className="text-[10.5px] leading-snug text-muted-foreground">
          Right dataset connected. Connect a left dataset to continue.
        </p>
      )}
      {parsed?.length === 0 && (
        <p data-testid="join-missing-condition" className="text-[10.5px] leading-snug text-amber-700 dark:text-amber-300">
          Choose at least one left and right column.
        </p>
      )}
      <Field label="join type">
        <select aria-label="Join type" value={how} onClick={(event) => event.stopPropagation()}
          onChange={(event) => updateConfig(id, { how: event.target.value as NodeConfig['how'] })} className={cn('nodrag', miniSelectClass)}>
          {howOptions.map((option) => <option key={option} value={option}>{option}</option>)}
        </select>
      </Field>
      {advanced || parsed === null ? (
        <div className="mt-1.5 flex flex-col gap-1">
          <Field label="advanced ON condition">
            <MiniInput value={effectiveCondition} placeholder="a.user_id = b.uid" mono
              onChange={(value) => updateConfig(id, { on: '', condition: value })} />
          </Field>
          {parseJoinKeys(on, condition) !== null && <button type="button" className="nodrag self-start text-[10px] text-muted-foreground underline"
            onClick={(event) => { event.stopPropagation(); setAdvanced(false) }}>Use key builder</button>}
        </div>
      ) : (
        <div className="mt-1.5 flex flex-col gap-1.5">
          {pairs.map((pair, index) => <div key={index} className="grid grid-cols-[1fr_1fr_auto] items-end gap-1">
            <Field label="left column">
              <KeySelect ariaLabel={`Left key ${index + 1}`} value={pair.left} columns={leftColumns.map((column) => column.name)}
                onChange={(left) => commit(pairs.map((current, i) => i === index ? { ...current, left } : current))} />
            </Field>
            <Field label="right column">
              <KeySelect ariaLabel={`Right key ${index + 1}`} value={pair.right} columns={rightColumns.map((column) => column.name)}
                onChange={(right) => commit(pairs.map((current, i) => i === index ? { ...current, right } : current))} />
            </Field>
            <button type="button" className="nodrag h-7 px-1 text-xs text-muted-foreground" aria-label={`Remove key pair ${index + 1}`}
              onClick={(event) => { event.stopPropagation(); commit(pairs.length === 1 ? [{ left: '', right: '' }] : pairs.filter((_, i) => i !== index)) }}>×</button>
          </div>)}
          <div className="flex gap-2">
            <button type="button" className="nodrag text-[10px] text-muted-foreground underline" onClick={(event) => { event.stopPropagation(); addPair() }}>Add key pair</button>
            <button type="button" className="nodrag text-[10px] text-muted-foreground underline" onClick={(event) => { event.stopPropagation(); setAdvanced(true) }}>Advanced condition</button>
          </div>
        </div>
      )}
      <JoinWithRelated nodeId={id} surface="canvas" />
    </NodeCard>
  )
}

function KeySelect({ ariaLabel, value, columns, onChange }: { ariaLabel: string; value: string; columns: string[]; onChange: (value: string) => void }) {
  const available = columns.includes(value)
  return <select aria-label={ariaLabel} value={value} onClick={(event) => event.stopPropagation()} onChange={(event) => onChange(event.target.value)} className={cn('nodrag', miniSelectClass)}>
    {!value && <option value="">Choose column</option>}
    {value && !available && <option value={value}>{value} (schema unavailable)</option>}
    {columns.map((column) => <option key={column} value={column}>{column}</option>)}
  </select>
}

register(
  {
    kind: 'join',
    title: 'join',
    category: 'compute',
    tag: 'join',
    inputs: [
      { id: 'a', label: 'left', wire: 'dataset', accepts: ['dataset', 'sample'] },
      { id: 'b', label: 'right', wire: 'dataset', accepts: ['dataset', 'sample'] },
    ],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'Combine two datasets by matching rows',
    defaultData: () => ({ title: 'join', status: 'draft', config: { how: 'inner', on: '' }, meta: 'inner' }),
  },
  Join,
)
