import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type CanvasCopyRequest } from '../api/client'
import { useStore } from '../store/graph'
import type { CanvasCopyValidation, WorkspaceResource } from '../types/api'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'

const ROOT_ID = 'workspace-local-root'
const identity = (resource: WorkspaceResource) => resource.id.slice(resource.id.indexOf(':') + 1)
const message = (error: unknown) => error instanceof Error ? error.message : String(error)

export type CanvasCopySource = {
  canvasId: string
  version?: number
  subjectId?: string
  name: string
}

export function CanvasCopyModal({ source, onClose, onCreated }: {
  source: CanvasCopySource
  onClose: () => void
  onCreated?: () => void
}) {
  const principalId = useStore((state) => state.currentUser?.id ?? null)
  const view = useStore((state) => state.view)
  const session = useRef({ principalId, view })
  const alive = useRef(true)
  const [copyId] = useState(() => crypto.randomUUID())
  const [name, setName] = useState(`${source.name || 'Untitled canvas'} copy`)
  const [path, setPath] = useState<WorkspaceResource[]>([])
  const [container, setContainer] = useState<WorkspaceResource | null>(null)
  const [children, setChildren] = useState<WorkspaceResource[]>([])
  const [loadingDestination, setLoadingDestination] = useState(true)
  const [busy, setBusy] = useState(false)
  const [creating, setCreating] = useState(false)
  const [validation, setValidation] = useState<CanvasCopyValidation | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => () => { alive.current = false }, [])

  const request = useMemo<CanvasCopyRequest | null>(() => {
    if (!container || container.version == null || !name.trim()) return null
    return {
      copyId,
      sourceCanvasId: source.canvasId,
      ...(source.subjectId
        ? { sourceSubjectId: source.subjectId }
        : { sourceCanvasVersion: source.version }),
      containerId: identity(container),
      expectedContainerVersion: container.version,
      name: name.trim(),
    }
  }, [container, copyId, name, source])

  const load = async (containerId: string, nextPath?: WorkspaceResource[]) => {
    setLoadingDestination(true); setError(''); setValidation(null); setConfirmed(false)
    try {
      const page = await api.workspaceBrowse(containerId, { limit: 100 })
      if (!page.container) throw new Error('Workspace destination is unavailable')
      if (!alive.current) return
      setContainer(page.container)
      setChildren(page.items.filter((item) => (
        item.kind === 'container' && item.source === 'local' && !item.detached
      )))
      setPath(nextPath ?? [page.container])
    } catch (caught) {
      if (alive.current) setError(message(caught))
    } finally {
      if (alive.current) setLoadingDestination(false)
    }
  }
  useEffect(() => { void load(ROOT_ID) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const invalidate = () => { setValidation(null); setConfirmed(false); setError('') }
  const validate = async () => {
    if (!request || busy) return
    setBusy(true); setError('')
    try { setValidation(await api.validateCanvasCopy(request)) }
    catch (caught) { setError(message(caught)) }
    finally { setBusy(false) }
  }
  const create = async () => {
    if (!request || !validation?.canImport || busy
        || (validation.requiresConfirmation && !confirmed)) return
    setBusy(true); setCreating(true); setError('')
    try {
      const result = await api.createCanvasCopy({
        ...request,
        copyIntentDigest: validation.copyIntentDigest,
        validationDigest: validation.validationDigest,
        confirmWarnings: confirmed,
      })
      const current = useStore.getState()
      if (!alive.current || current.currentUser?.id !== session.current.principalId
          || current.view !== session.current.view) return
      await current.refreshFiles()
      if (!alive.current || useStore.getState().currentUser?.id !== session.current.principalId) return
      if (await useStore.getState().openFile(result.id)) {
        useStore.getState().pushToast(
          result.replayed ? 'Opened the Canvas created by the earlier copy request.' : 'Created an independent Canvas copy.',
          'success',
        )
        onClose()
        onCreated?.()
      }
    } catch (caught) { if (alive.current) setError(message(caught)) }
    finally {
      if (alive.current) {
        setBusy(false)
        setCreating(false)
      }
    }
  }

  return <Dialog open onOpenChange={(open) => { if (!open && !creating) onClose() }}>
    <DialogContent closeDisabled={creating}
      onEscapeKeyDown={(event) => { if (creating) event.preventDefault() }}
      onPointerDownOutside={(event) => { if (creating) event.preventDefault() }}
      className="dp-modal-overlay flex max-h-[86vh] w-[560px] max-w-[calc(100vw-2rem)] flex-col gap-3 overflow-y-auto">
      <DialogTitle>{source.subjectId ? 'Clone retained run as new Canvas' : 'Save a copy'}</DialogTitle>
      <DialogDescription>
        Creates a private, editable snapshot owned by you. Shares, collaborators, credentials, history, outputs, and Inbox state are not copied.
      </DialogDescription>
      <label className="grid gap-1 text-[11px] text-muted-foreground">New Canvas name
        <input aria-label="New Canvas name" className="dp-input" value={name} disabled={busy}
          onChange={(event) => { setName(event.target.value); invalidate() }} />
      </label>
      <div className="grid gap-2">
        <span className="text-[11px] text-muted-foreground">Workspace destination</span>
        <nav aria-label="Choose copy destination" className="flex flex-wrap gap-1 text-[11px]">
          {path.map((item, index) => <button key={item.id} disabled={busy} className="text-primary underline"
            onClick={() => void load(identity(item), path.slice(0, index + 1))}>{item.name}</button>)}
        </nav>
        <div className="max-h-36 overflow-y-auto rounded-md border border-border p-1">
          {loadingDestination && !container ? <div className="p-2 text-[11px] text-muted-foreground">Loading destination…</div>
            : children.length ? children.map((child) => <button key={child.id}
              className="block w-full rounded px-2 py-1.5 text-left text-[11px] hover:bg-accent"
              disabled={busy} onClick={() => void load(identity(child), [...path, child])}>{child.name}</button>)
              : <div className="p-2 text-[11px] text-muted-foreground">No child containers.</div>}
        </div>
        {container && <span className="text-[11px]">Destination: <strong>{container.name}</strong></span>}
      </div>
      {validation && <div className="rounded-md border border-border p-3 text-[11px]">
        <div className="font-semibold">{validation.nodeCount} nodes · {validation.edgeCount} connections · {validation.requirements.length} requirements</div>
        {validation.diagnostics.length > 0 && <ul className="mb-0 mt-2 grid gap-1 pl-4">
          {validation.diagnostics.map((item, index) => <li key={`${item.code}-${index}`}
            className={item.severity === 'error' ? 'text-destructive' : 'text-muted-foreground'}>{item.message}</li>)}
        </ul>}
        {validation.requiresConfirmation && validation.canImport && <label className="mt-3 flex gap-2 text-muted-foreground">
          <input type="checkbox" checked={confirmed} disabled={busy}
            onChange={(event) => setConfirmed(event.target.checked)} />
          <span>I understand the warnings and will relink or install unavailable dependencies before running.</span>
        </label>}
      </div>}
      {creating && <div role="status" aria-live="polite" className="text-[12px] text-muted-foreground">
        Creating your Canvas… This request has been submitted and cannot be cancelled.
      </div>}
      {error && <div role="alert" className="text-[12px] text-destructive">{error}</div>}
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={onClose} disabled={creating}>Cancel</Button>
        {!validation ? <Button onClick={() => void validate()} disabled={!request || busy}>{busy ? 'Validating…' : 'Review copy'}</Button>
          : <Button onClick={() => void create()} disabled={busy || !validation.canImport || (validation.requiresConfirmation && !confirmed)}>
            {busy ? 'Creating…' : 'Create and open'}
          </Button>}
      </div>
    </DialogContent>
  </Dialog>
}
