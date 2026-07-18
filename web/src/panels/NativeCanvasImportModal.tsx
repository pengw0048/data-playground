import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { NativeCanvasValidation } from '../types/api'
import { useStore } from '../store/graph'
import { Icon } from '../ui/Icon'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'

const MAX_BYTES = 2 * 1024 * 1024

type ActiveRequest = { generation: number; controller: AbortController }
type ImportSession = { principalId: string | null; canvasId: string; view: string }

function newImportId(): string {
  return crypto.randomUUID()
}

// Native import is intentionally a browser file flow, not a multipart upload surface. The browser
// reads one bounded JSON document and the API receives the parsed envelope plus its original filename.
export function NativeCanvasImportModal({ onClose }: { onClose: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const mountedRef = useRef(false)
  const closedRef = useRef(false)
  const generationRef = useRef(0)
  const activeRef = useRef<ActiveRequest | null>(null)
  const initialSessionRef = useRef<ImportSession | null>(null)
  const expectedDestinationRef = useRef<string | null>(null)
  const principalId = useStore((state) => state.currentUser?.id ?? null)
  const canvasId = useStore((state) => state.doc.id)
  const view = useStore((state) => state.view)
  if (!initialSessionRef.current) initialSessionRef.current = { principalId, canvasId, view }

  const [filename, setFilename] = useState<string | null>(null)
  const [envelope, setEnvelope] = useState<Record<string, unknown> | null>(null)
  const [importId, setImportId] = useState('')
  const [validation, setValidation] = useState<NativeCanvasValidation | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [stage, setStage] = useState<'validating' | 'importing' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const sameSession = useCallback(() => {
    const original = initialSessionRef.current!
    const current = useStore.getState()
    return current.currentUser?.id === original.principalId
      && current.view === original.view
      && (current.doc.id === original.canvasId || current.doc.id === expectedDestinationRef.current)
  }, [])

  const invalidate = useCallback(() => {
    generationRef.current += 1
    activeRef.current?.controller.abort()
    activeRef.current = null
  }, [])

  const close = useCallback(() => {
    if (closedRef.current) return
    closedRef.current = true
    invalidate()
    if (mountedRef.current) {
      setBusy(false)
      setStage(null)
    }
    onClose()
  }, [invalidate, onClose])

  const start = useCallback(() => {
    invalidate()
    const active = { generation: ++generationRef.current, controller: new AbortController() }
    activeRef.current = active
    return active
  }, [invalidate])

  const isCurrent = useCallback((active: ActiveRequest) => (
    mountedRef.current
    && !closedRef.current
    && sameSession()
    && activeRef.current === active
    && generationRef.current === active.generation
    && !active.controller.signal.aborted
  ), [sameSession])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      invalidate()
    }
  }, [invalidate])

  // This dialog belongs to the Canvas and principal from which it was opened. Do not let an old
  // request surface, or open a newly imported Canvas, after a route/canvas/account transition.
  useEffect(() => {
    if (!sameSession()) close()
  }, [canvasId, close, principalId, sameSession, view])

  const choose = () => inputRef.current?.click()

  const selectFile = async (file: File | undefined) => {
    if (!file) return
    expectedDestinationRef.current = null
    const active = start()
    setFilename(null)
    setEnvelope(null)
    setImportId('')
    setValidation(null)
    setConfirmed(false)
    setError(null)
    setBusy(true)
    setStage('validating')
    try {
      if (file.size > MAX_BYTES) throw new Error('This native Canvas file is larger than 2 MiB.')
      let parsed: unknown
      try {
        parsed = JSON.parse(await file.text())
      } catch {
        throw new Error('This file is not valid JSON.')
      }
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('This file must contain one native Canvas JSON object.')
      }
      if (!isCurrent(active)) return
      // Keep this id for every retry of this exact parsed file. If the import response is lost, the
      // server can replay the original creation instead of creating another Canvas.
      const nextImportId = newImportId()
      const nextEnvelope = parsed as Record<string, unknown>
      setFilename(file.name)
      setEnvelope(nextEnvelope)
      setImportId(nextImportId)
      const checked = await api.validateNativeCanvasImport(
        { filename: file.name, importId: nextImportId, envelope: nextEnvelope },
        { signal: active.controller.signal },
      )
      if (!isCurrent(active)) return
      setValidation(checked)
    } catch (caught) {
      if (!isCurrent(active)) return
      setError(caught instanceof Error ? caught.message : 'Could not validate this native Canvas file.')
    } finally {
      if (isCurrent(active)) {
        activeRef.current = null
        setBusy(false)
        setStage(null)
      }
    }
  }

  const runImport = async () => {
    if (!filename || !envelope || !importId || !validation?.canImport
      || !validation.validationDigest
      || (validation.requiresConfirmation && !confirmed)) return
    const active = start()
    setBusy(true)
    setStage('importing')
    setError(null)
    try {
      const result = await api.importNativeCanvas(
        { filename, importId, envelope, validationDigest: validation.validationDigest, confirmWarnings: confirmed },
        { signal: active.controller.signal },
      )
      if (!isCurrent(active)) return
      await useStore.getState().refreshFiles()
      if (!isCurrent(active)) return
      // openFile has its own navigation-generation guard; this check prevents a late import response
      // from ever starting an open after the researcher has already left this Canvas. The exact
      // destination is the one intentional Canvas transition this modal permits.
      expectedDestinationRef.current = result.id
      const opened = await useStore.getState().openFile(result.id)
      const current = useStore.getState()
      if (!opened || active.controller.signal.aborted || current.currentUser?.id !== initialSessionRef.current!.principalId
        || current.view !== 'canvas' || current.doc.id !== result.id) {
        expectedDestinationRef.current = null
        if (!sameSession()) close()
        return
      }
      current.pushToast(result.replayed ? 'Opened the Canvas created by the earlier import request.' : 'Imported a new Canvas.', 'success')
      close()
    } catch (caught) {
      if (!isCurrent(active)) return
      // Deliberately retain the validated envelope and import id: retrying a lost response is safe.
      setError(caught instanceof Error ? caught.message : 'Import failed.')
    } finally {
      if (isCurrent(active)) {
        activeRef.current = null
        setBusy(false)
        setStage(null)
      }
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) close() }}>
      <DialogContent className="dp-modal-overlay gap-0 overflow-hidden p-0 w-[560px] max-w-[92vw] rounded-xl">
        <div className="flex items-center gap-2 border-b border-border py-3 pl-4 pr-12">
          <span className="flex items-center text-muted-foreground"><Icon name="import" size={14} /></span>
          <DialogTitle className="text-sm font-semibold">Import native Canvas</DialogTitle>
        </div>
        <DialogDescription className="sr-only">Validate and import a Data Playground native Canvas file as a new Canvas.</DialogDescription>
        <div className="flex flex-col gap-3 p-4 text-[12px]">
          <p className="m-0 text-muted-foreground">Imports always create a new Canvas. Data, credentials, plugins, environments, and run output are not included.</p>
          <input ref={inputRef} type="file" accept=".dp-canvas.json,application/json" aria-label="Choose native Canvas file" className="sr-only"
            onChange={(event) => { void selectFile(event.target.files?.[0]); event.currentTarget.value = '' }} />
          <div className="flex items-center justify-between rounded-md border border-border bg-muted/20 px-3 py-2">
            <span className="truncate text-muted-foreground">{filename ?? 'Choose a .dp-canvas.json file'}</span>
            <Button variant="outline" size="sm" onClick={choose}>Choose file</Button>
          </div>
          {busy && <div className="text-muted-foreground">{stage === 'importing' ? 'Importing native Canvas…' : 'Validating native Canvas…'}</div>}
          {validation && <div className="rounded-md border border-border p-3">
            <div className="font-medium">{validation.name}</div>
            <div className="mt-1 text-muted-foreground">{validation.nodeCount} nodes · {validation.edgeCount} connections · {validation.requirements.length} requirements</div>
            {validation.diagnostics.length > 0 && <ul className="mb-0 mt-2 space-y-1 pl-4">
              {validation.diagnostics.map((item, index) => <li key={`${item.code}-${index}`} className={item.severity === 'error' ? 'text-destructive' : 'text-muted-foreground'}>{item.message}</li>)}
            </ul>}
            {validation.requiresConfirmation && validation.canImport && <label className="mt-3 flex items-start gap-2 text-muted-foreground">
              <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
              <span>I understand the warnings and will relink or prepare unavailable dependencies before running.</span>
            </label>}
          </div>}
          {error && <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-destructive">{error}</div>}
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={close}>Cancel</Button>
            <Button size="sm" onClick={() => void runImport()} disabled={busy || !validation?.canImport || !validation.validationDigest || (validation.requiresConfirmation && !confirmed)}>
              <Icon name="import" size={13} /> Import as new Canvas
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
