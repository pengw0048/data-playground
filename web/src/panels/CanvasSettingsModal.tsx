import { useEffect, useState } from 'react'
import { type CanvasVisibility } from '../api/client'
import { roleCanEdit, useStore } from '../store/graph'
import { Icon } from '../ui/Icon'
import { useCanvasSharing } from './useCanvasSharing'
import { cn } from '@/lib/utils'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { CanvasParameterDeclaration, CanvasParameterType } from '../types/graph'

const VISIBILITIES: { value: CanvasVisibility; title: string; description: string }[] = [
  { value: 'private', title: 'Private', description: 'Only the owner and invited people' },
  { value: 'workspace', title: 'Workspace', description: 'Everyone in the workspace can edit' },
  { value: 'workspace_view', title: 'Workspace view-only', description: 'Everyone in the workspace can view' },
]

// Settings scoped to THIS canvas (not the app/workspace ones). Sharing state comes from the same hook
// as ShareModal so both surfaces have identical pending, failure, and retry behavior.
export function CanvasSettingsModal({ onClose }: { onClose: () => void }) {
  const doc = useStore((s) => s.doc)
  const canvasRole = useStore((s) => s.canvasRole)
  const renameFile = useStore((s) => s.renameFile)
  const setRequirements = useStore((s) => s.setRequirements)
  const setParameters = useStore((s) => s.setParameters)
  const canEdit = roleCanEdit(canvasRole)
  const isOwner = canvasRole === 'owner'
  const sharing = useCanvasSharing(doc.id, isOwner)
  const [name, setName] = useState(doc.name ?? '')
  const [reqs, setReqs] = useState((doc.requirements ?? []).join('\n'))
  const [parameters, setParameterDrafts] = useState<CanvasParameterDeclaration[]>(doc.parameters ?? [])
  const [parameterError, setParameterError] = useState('')

  useEffect(() => {
    setName(doc.name ?? '')
    setReqs((doc.requirements ?? []).join('\n'))
    setParameterDrafts(doc.parameters ?? [])
    setParameterError('')
  }, [doc.id, doc.name, doc.requirements, doc.parameters])

  const busy = sharing.pending !== null
  const applyParameters = (next: CanvasParameterDeclaration[]) => {
    setParameterDrafts(next)
    const error = declarationListError(next)
    setParameterError(error ?? '')
    if (!error) {
      const storeError = setParameters(next)
      if (storeError) {
        setParameterDrafts(parameters)
        setParameterError(storeError)
      }
    }
  }
  const access = canvasRole === 'owner'
    ? 'Owner access'
    : canvasRole === 'editor'
      ? 'Editor access'
      : canvasRole === 'viewer'
        ? 'View-only access'
        : 'Access is unknown — editing is disabled'

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="dp-modal-overlay w-[440px] max-w-[92vw] gap-0 overflow-hidden rounded-xl p-0">
        <div className="flex items-center gap-2 border-b border-border py-3 pl-4 pr-12">
          <span className="flex items-center text-muted-foreground"><Icon name="grid" size={14} /></span>
          <DialogTitle className="text-sm font-semibold">Canvas settings</DialogTitle>
        </div>
        <DialogDescription className="sr-only">Settings for the current canvas: its name, visibility, and dependencies.</DialogDescription>

        <div className="flex flex-col gap-4 p-4">
          <div className="rounded-md bg-muted px-2.5 py-1.5 text-[10.5px] text-muted-foreground">{access}</div>
          {sharing.error && (
            <div role="alert" className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-2.5 py-2 text-[11.5px] text-destructive">
              <span className="min-w-0 flex-1">{sharing.error}</span>
              {sharing.retryable && <Button type="button" variant="outline" size="sm" onClick={sharing.retry} disabled={busy} className="h-6 px-2 text-[10.5px]">Retry</Button>}
            </div>
          )}
          {sharing.pending && sharing.pending !== 'load' && (
            <div role="status" className="text-[10.5px] text-muted-foreground">Saving sharing changes…</div>
          )}
          <div>
            <Label className="mb-1 block text-[11.5px] font-normal text-muted-foreground">Name</Label>
            <Input value={name} disabled={!canEdit} onChange={(event) => { setName(event.target.value); renameFile(event.target.value) }} placeholder="untitled" />
          </div>
          <div>
            <div className="mb-1.5 text-[11.5px] text-muted-foreground">Visibility</div>
            {sharing.visibility === null && sharing.pending === 'load' ? (
              <div className="text-[11.5px] text-muted-foreground">Loading visibility…</div>
            ) : (
              <div className="grid gap-2">
                {VISIBILITIES.map(({ value, title, description }) => (
                  <button key={value} onClick={() => sharing.setCanvasVisibility(value)} disabled={!isOwner || busy || sharing.visibility === null}
                    aria-pressed={sharing.visibility === value}
                    className={cn('rounded-lg border px-2.5 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-70',
                      sharing.visibility === value ? 'border-primary bg-primary/10' : 'border-border bg-background enabled:hover:bg-accent/50')}>
                    <div className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
                      <Icon name={value === 'private' ? 'grid' : 'link'} size={12} /> {title}
                    </div>
                    <div className="mt-0.5 text-[10.5px] font-normal text-muted-foreground">{description}</div>
                  </button>
                ))}
              </div>
            )}
            <div className="mt-2 text-[10.5px] text-muted-foreground">
              {isOwner ? <>Invite specific people from the <b>Share</b> button.</> : 'Only the canvas owner can change visibility.'}
            </div>
          </div>
          <div>
            <Label className="mb-1 block text-[11.5px] font-normal text-muted-foreground">Dependencies (pip)</Label>
            <textarea
              value={reqs}
              disabled={!canEdit}
              onChange={(event) => {
                setReqs(event.target.value)
                setRequirements(event.target.value.split('\n').map((value) => value.trim()).filter(Boolean))
              }}
              placeholder={'pandas\nscikit-learn==1.5'}
              spellCheck={false}
              rows={3}
              className="dp-mono w-full resize-y rounded-md border border-border bg-background px-2 py-1.5 text-[11.5px] text-foreground outline-none disabled:cursor-not-allowed disabled:opacity-70"
            />
            <div className="mt-1 text-[10.5px] text-muted-foreground">One pip spec per line — installed on this canvas's kernel, then importable in <code>transform</code> cells. Travels with the canvas.</div>
          </div>
          <div>
            <div className="mb-1 flex items-center gap-2">
              <Label className="text-[11.5px] font-normal text-muted-foreground">Run parameter declarations</Label>
              <Button type="button" size="sm" variant="outline" disabled={!canEdit} onClick={() => {
                let suffix = parameters.length + 1
                while (parameters.some((item) => item.name === `parameter${suffix}`)) suffix += 1
                applyParameters([...parameters, { name: `parameter${suffix}`, type: 'string', required: true }])
              }} className="ml-auto h-6 px-2 text-[10.5px]">Add parameter</Button>
            </div>
            <div className="flex max-h-[360px] flex-col gap-2 overflow-y-auto pr-1">
              {parameters.map((parameter, index) => <ParameterDeclarationEditor
                key={index} value={parameter} disabled={!canEdit}
                moveUp={() => { if (index > 0) { const next = [...parameters]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; applyParameters(next) } }}
                moveDown={() => { if (index + 1 < parameters.length) { const next = [...parameters]; [next[index], next[index + 1]] = [next[index + 1], next[index]]; applyParameters(next) } }}
                remove={() => applyParameters(parameters.filter((_item, candidate) => candidate !== index))}
                update={(nextValue) => applyParameters(parameters.map((item, candidate) => candidate === index ? nextValue : item))} />)}
              {parameters.length === 0 && <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-[10.5px] text-muted-foreground">No run parameters. Existing Canvas behavior is unchanged.</div>}
            </div>
            {parameterError
              ? <div role="alert" className="mt-1 text-[10.5px] text-destructive">{parameterError}</div>
              : <div className="mt-1 text-[10.5px] text-muted-foreground">Order is durable. Invalid edits stay local to this dialog and are not autosaved.</div>}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

const PARAMETER_TYPES: CanvasParameterType[] = ['string', 'integer', 'float', 'boolean', 'date', 'datetime', 'dataset']

function initialDefault(type: CanvasParameterType): unknown {
  if (type === 'integer' || type === 'float') return 0
  if (type === 'boolean') return false
  if (type === 'date') return '2026-01-01'
  if (type === 'datetime') return '2026-01-01T00:00:00Z'
  if (type === 'dataset') return { kind: 'latest', datasetId: '' }
  return ''
}

function isBuiltInSecretRef(value: string): boolean {
  return /^(?:env|file):/i.test(value)
}

function declarationListError(values: CanvasParameterDeclaration[]): string | null {
  const names = new Set<string>()
  for (const value of values) {
    if (!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(value.name)) return 'Names start with a letter and use only letters, numbers, _ or -.'
    if (names.has(value.name)) return `Parameter name '${value.name}' is duplicated.`
    names.add(value.name)
    if (value.required && value.default != null) return `'${value.name}' cannot be required and have a default.`
    if ((value.label?.length ?? 0) > 128 || (value.help?.length ?? 0) > 1024) return `'${value.name}' label or help text is too long.`
    const limits = value.constraints
    if ([limits?.minimum, limits?.maximum].some((item) => item != null && !Number.isFinite(item))) return `'${value.name}' numeric constraints must be finite.`
    if ([limits?.minLength, limits?.maxLength].some((item) => item != null && (!Number.isInteger(item) || item < 0 || item > 4096))) return `'${value.name}' length constraints must be whole numbers from 0 to 4096.`
    if (limits?.minimum != null && limits?.maximum != null && limits.minimum > limits.maximum) return `'${value.name}' minimum exceeds maximum.`
    if (limits?.minLength != null && limits?.maxLength != null && limits.minLength > limits.maxLength) return `'${value.name}' minLength exceeds maxLength.`
    if (value.default != null) {
      if (value.type === 'string' && (typeof value.default !== 'string' || isBuiltInSecretRef(value.default))) return `'${value.name}' default must be a public string, not a SecretRef.`
      if (value.type === 'integer' && !Number.isSafeInteger(value.default)) return `'${value.name}' default must be a safe integer.`
      if (value.type === 'float' && (typeof value.default !== 'number' || !Number.isFinite(value.default))) return `'${value.name}' default must be finite.`
      if (value.type === 'boolean' && typeof value.default !== 'boolean') return `'${value.name}' default must be boolean.`
      if (value.type === 'date' && (typeof value.default !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value.default)
          || Number.isNaN(Date.parse(`${value.default}T00:00:00Z`)) || new Date(`${value.default}T00:00:00Z`).toISOString().slice(0, 10) !== value.default)) return `'${value.name}' default must be a real YYYY-MM-DD date.`
      if (value.type === 'datetime' && (typeof value.default !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value.default) || Number.isNaN(Date.parse(value.default)))) return `'${value.name}' datetime default needs a timezone.`
      if (value.type === 'dataset') {
        const ref = value.default as { kind?: unknown; datasetId?: unknown; revisionId?: unknown }
        if (!value.default || typeof value.default !== 'object' || !['exact', 'latest'].includes(String(ref.kind))
            || typeof ref.datasetId !== 'string' || !ref.datasetId
            || isBuiltInSecretRef(ref.datasetId)
            || (ref.kind === 'exact' && (typeof ref.revisionId !== 'string' || !ref.revisionId
              || isBuiltInSecretRef(ref.revisionId)
              || Object.keys(ref).length !== 3))
            || (ref.kind === 'latest' && Object.keys(ref).length !== 2)) return `'${value.name}' dataset default is incomplete.`
      }
      if (value.type === 'string' && typeof value.default === 'string') {
        if (limits?.minLength != null && value.default.length < limits.minLength) return `'${value.name}' default is shorter than minLength.`
        if (limits?.maxLength != null && value.default.length > limits.maxLength) return `'${value.name}' default is longer than maxLength.`
      }
      if ((value.type === 'integer' || value.type === 'float') && typeof value.default === 'number') {
        if (limits?.minimum != null && value.default < limits.minimum) return `'${value.name}' default is below minimum.`
        if (limits?.maximum != null && value.default > limits.maximum) return `'${value.name}' default is above maximum.`
      }
    }
  }
  return null
}

function ParameterDeclarationEditor({ value, disabled, update, remove, moveUp, moveDown }: {
  value: CanvasParameterDeclaration; disabled: boolean
  update: (value: CanvasParameterDeclaration) => void
  remove: () => void; moveUp: () => void; moveDown: () => void
}) {
  const field = 'w-full rounded-md border border-border bg-background px-2 py-1 text-[10.5px] disabled:opacity-60'
  const setDefault = (raw: string) => {
    let parsed: unknown = raw
    if (value.type === 'integer' || value.type === 'float') parsed = raw && Number.isFinite(Number(raw)) ? Number(raw) : raw
    if (value.type === 'boolean') parsed = raw === 'true'
    update({ ...value, default: parsed })
  }
  const ref = value.type === 'dataset' && value.default && typeof value.default === 'object'
    ? value.default as { kind?: string; datasetId?: string; revisionId?: string } : null
  return <div className="rounded-md border border-border bg-muted/20 p-2">
    <div className="mb-2 flex gap-1">
      <Input aria-label="Parameter name" value={value.name} disabled={disabled} onChange={(event) => update({ ...value, name: event.target.value })} className="h-7 text-[11px]" />
      <select aria-label={`${value.name} type`} value={value.type} disabled={disabled} onChange={(event) => {
        const type = event.target.value as CanvasParameterType
        update({ name: value.name, type, required: true, label: value.label, help: value.help })
      }} className={`${field} w-[105px]`}>{PARAMETER_TYPES.map((type) => <option key={type}>{type}</option>)}</select>
      <button type="button" aria-label={`Move ${value.name} up`} disabled={disabled} onClick={moveUp}>↑</button>
      <button type="button" aria-label={`Move ${value.name} down`} disabled={disabled} onClick={moveDown}>↓</button>
      <button type="button" aria-label={`Remove ${value.name}`} disabled={disabled} onClick={remove}>×</button>
    </div>
    <div className="grid grid-cols-2 gap-1.5">
      <Input aria-label={`${value.name} label`} value={value.label ?? ''} disabled={disabled} placeholder="Label" onChange={(event) => update({ ...value, label: event.target.value || undefined })} className="h-7 text-[10.5px]" />
      <Input aria-label={`${value.name} help`} value={value.help ?? ''} disabled={disabled} placeholder="Help text" onChange={(event) => update({ ...value, help: event.target.value || undefined })} className="h-7 text-[10.5px]" />
    </div>
    <div className="mt-2 flex items-center gap-3 text-[10.5px]">
      <label><input type="checkbox" checked={value.required === true} disabled={disabled} onChange={(event) => update({ ...value, required: event.target.checked, default: undefined })} /> Required</label>
      <label><input type="checkbox" checked={value.default != null} disabled={disabled || value.required} onChange={(event) => update({ ...value, default: event.target.checked ? initialDefault(value.type) : undefined })} /> Default</label>
    </div>
    {value.default != null && !value.required && (value.type === 'dataset' ? <div className="mt-1.5 grid grid-cols-[90px_1fr] gap-1">
      <select aria-label={`${value.name} default selection`} value={ref?.kind ?? 'latest'} disabled={disabled} onChange={(event) => update({ ...value, default: { kind: event.target.value, datasetId: ref?.datasetId ?? '', ...(event.target.value === 'exact' ? { revisionId: ref?.revisionId ?? '' } : {}) } })} className={field}><option value="latest">Latest</option><option value="exact">Exact</option></select>
      <Input aria-label={`${value.name} default dataset`} value={ref?.datasetId ?? ''} disabled={disabled} placeholder="Dataset identity" onChange={(event) => update({ ...value, default: { ...ref, kind: ref?.kind ?? 'latest', datasetId: event.target.value } })} className="h-7 text-[10.5px]" />
      {ref?.kind === 'exact' && <Input aria-label={`${value.name} default revision`} value={ref.revisionId ?? ''} disabled={disabled} placeholder="Exact revision" onChange={(event) => update({ ...value, default: { ...ref, revisionId: event.target.value } })} className="col-start-2 h-7 text-[10.5px]" />}
    </div> : value.type === 'boolean' ? <select aria-label={`${value.name} default`} value={String(value.default)} disabled={disabled} onChange={(event) => setDefault(event.target.value)} className={`mt-1.5 ${field}`}><option value="true">true</option><option value="false">false</option></select>
      : <Input aria-label={`${value.name} default`} value={String(value.default)} disabled={disabled} type={value.type === 'date' ? 'date' : 'text'} placeholder={value.type === 'datetime' ? 'ISO 8601 with timezone' : 'Default'} onChange={(event) => setDefault(event.target.value)} className="mt-1.5 h-7 text-[10.5px]" />)}
    {(value.type === 'string' || value.type === 'integer' || value.type === 'float') && <div className="mt-1.5 grid grid-cols-2 gap-1">
      <Input aria-label={`${value.name} minimum constraint`} value={value.type === 'string' ? value.constraints?.minLength ?? '' : value.constraints?.minimum ?? ''} disabled={disabled} placeholder={value.type === 'string' ? 'Min length' : 'Minimum'} onChange={(event) => updateConstraint(value, update, value.type === 'string' ? 'minLength' : 'minimum', event.target.value)} className="h-7 text-[10.5px]" />
      <Input aria-label={`${value.name} maximum constraint`} value={value.type === 'string' ? value.constraints?.maxLength ?? '' : value.constraints?.maximum ?? ''} disabled={disabled} placeholder={value.type === 'string' ? 'Max length' : 'Maximum'} onChange={(event) => updateConstraint(value, update, value.type === 'string' ? 'maxLength' : 'maximum', event.target.value)} className="h-7 text-[10.5px]" />
    </div>}
  </div>
}

function updateConstraint(value: CanvasParameterDeclaration, update: (value: CanvasParameterDeclaration) => void,
                          key: 'minimum' | 'maximum' | 'minLength' | 'maxLength', raw: string) {
  const constraints = { ...(value.constraints ?? {}) }
  if (!raw) delete constraints[key]
  else constraints[key] = Number(raw)
  update({ ...value, constraints: Object.keys(constraints).length ? constraints : undefined })
}
