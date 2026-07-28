import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import { roleCanEdit, useStore } from '../store/graph'
import type { DatasetRevisionPage, RelatedDatasetCandidate, RelatedDatasetPage } from '../types/api'
import { datasetRefIdentity, isParameterRef, type DatasetRef } from '../types/graph'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'

const EVIDENCE_LABEL = {
  declared_relationship: 'Declared relationship',
  typed_reference: 'Typed reference',
  schema_match: 'Inferred schema match',
} as const

const CARDINALITY_TONE: Record<string, string> = {
  '1:1': 'bg-green-100 text-green-800 dark:bg-green-500/15 dark:text-green-200',
  '1:N': 'bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-200',
  'N:1': 'bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-200',
  'N:M': 'bg-rose-100 text-rose-800 dark:bg-rose-500/15 dark:text-rose-200',
  unknown: 'bg-muted text-muted-foreground',
}

function orientedJoinFacts(candidate: RelatedDatasetCandidate, swapInputs: boolean) {
  const cardinality = swapInputs
    ? candidate.cardinality === '1:N' ? 'N:1'
      : candidate.cardinality === 'N:1' ? '1:N'
        : candidate.cardinality
    : candidate.cardinality
  return {
    leftColumns: swapInputs ? candidate.rightColumns : candidate.leftColumns,
    rightColumns: swapInputs ? candidate.leftColumns : candidate.rightColumns,
    cardinality,
  }
}

function qualifiedColumns(side: 'a' | 'b', columns: string[]) {
  return columns.map((column) => `${side}.${column}`).join(' + ')
}

function cardinalityExplanation(cardinality: RelatedDatasetCandidate['cardinality']): string {
  if (cardinality === '1:N') {
    return 'A left input (a) row can match multiple right input (b) rows, so joined rows may multiply.'
  }
  if (cardinality === 'N:1') {
    return 'A right input (b) row can match multiple left input (a) rows, so joined rows may multiply.'
  }
  if (cardinality === 'N:M') {
    return 'Rows on either input can match multiple rows on the other, so joined rows may multiply.'
  }
  if (cardinality === '1:1') return 'Each input row matches at most one row on the other input.'
  return 'Cardinality is unknown because it was not measured for this join.'
}

function joinTypeMeaning(
  how: 'inner' | 'left' | 'right' | 'outer',
  leftName: string,
  rightName: string,
) {
  if (how === 'left') return `Keeps every row from left input (a): ${leftName}.`
  if (how === 'right') return `Keeps every row from right input (b): ${rightName}.`
  if (how === 'outer') return 'Keeps rows from both inputs, including unmatched rows.'
  return 'Keeps only rows that match across left input (a) and right input (b).'
}

function revisionLabel(index: number, committedAt?: string | null) {
  return `Retained version ${index + 1}${committedAt ? ` · ${new Date(committedAt).toLocaleString()}` : ''}`
}

function friendlyRevisionError(error: string) {
  if (error.includes('related_dataset_revision_history_unavailable')) {
    return 'Version history is unavailable for this dataset.'
  }
  return 'Version history could not be loaded. You can still join the current version.'
}

function friendlyActionError(error: string) {
  const lowered = error.toLowerCase()
  if (lowered.includes('canvas')) return 'The Canvas changed while you were reviewing. Refresh it and try again.'
  if (lowered.includes('dataset') || lowered.includes('evidence') || lowered.includes('revision')) {
    return 'The selected dataset changed. Refresh the review before trying again.'
  }
  return 'The Join could not be added. Try again.'
}

function relationshipEvidence(candidate: RelatedDatasetCandidate) {
  if (candidate.cardinality === 'unknown' || candidate.reason.includes('cardinality not measurable')) {
    return 'Matching key columns.'
  }
  return candidate.reason
}

export function JoinWithRelated({ nodeId, surface = 'inspector' }: {
  nodeId: string
  surface?: 'inspector' | 'canvas'
}) {
  const doc = useStore((state) => state.doc)
  const canEdit = useStore((state) => roleCanEdit(state.canvasRole))
  const serverVersion = useStore((state) => state.serverVersion)
  const currentDraftId = useStore((state) => state.currentDraftId)
  const selectedNode = doc.nodes.find((node) => node.id === nodeId)
  const context = useMemo(() => {
    if (selectedNode?.type === 'source') {
      return { source: selectedNode, joinNodeId: undefined as string | undefined, emptyPort: undefined }
    }
    if (selectedNode?.type !== 'join') return null
    const incoming = doc.edges.filter((edge) => edge.target === nodeId)
    if (incoming.length !== 1) return null
    const occupiedPort = incoming[0].targetHandle
    if (occupiedPort !== 'a' && occupiedPort !== 'b') return null
    const source = doc.nodes.find(
      (node) => node.id === incoming[0].source && node.type === 'source',
    )
    return source ? {
      source,
      joinNodeId: nodeId,
      emptyPort: occupiedPort === 'a' ? 'b' as const : 'a' as const,
    } : null
  }, [doc.edges, doc.nodes, nodeId, selectedNode])
  const sourceIdentity = useMemo(() => {
    const config = context?.source.data.config
    if (!config) return null
    const ref = config.datasetRef as DatasetRef | { parameterRef: string } | undefined
    if (isParameterRef(ref)) return null
    if (typeof config.registrationId === 'string' && config.registrationId) {
      const exact = ref ? datasetRefIdentity(ref) : null
      return {
        kind: 'local' as const, registrationId: config.registrationId,
        revisionMode: exact ? 'exact' as const : 'current' as const,
        ...(exact ? { revisionId: exact.revisionId } : {}),
      }
    }
    if (typeof config.providerMountId === 'string' && config.providerMountId
        && typeof config.providerSourceBindingId === 'string' && config.providerSourceBindingId) {
      const exact = ref ? datasetRefIdentity(ref) : null
      return {
        kind: 'provider' as const, mountId: config.providerMountId,
        sourceBindingId: config.providerSourceBindingId,
        revisionMode: exact ? 'exact' as const : 'current' as const,
        ...(exact ? { revisionId: exact.revisionId } : {}),
      }
    }
    return null
  }, [context])
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [folder, setFolder] = useState('')
  const [page, setPage] = useState<RelatedDatasetPage | null>(null)
  const [candidate, setCandidate] = useState<RelatedDatasetCandidate | null>(null)
  const [candidateBase, setCandidateBase] = useState<RelatedDatasetCandidate | null>(null)
  const [requestedRevisionId, setRequestedRevisionId] = useState('')
  const [revisions, setRevisions] = useState<DatasetRevisionPage | null>(null)
  const [loadingRevisions, setLoadingRevisions] = useState(false)
  const [loadingMoreRevisions, setLoadingMoreRevisions] = useState(false)
  const [revising, setRevising] = useState(false)
  const [revisionError, setRevisionError] = useState('')
  const [how, setHow] = useState<'inner' | 'left' | 'right' | 'outer'>('inner')
  const [loading, setLoading] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState('')
  const request = useRef(0)
  const dialogRef = useRef<HTMLDivElement>(null)
  const openerRef = useRef<HTMLButtonElement | null>(null)
  const reviewFocusRef = useRef<HTMLButtonElement | null>(null)

  const close = () => {
    setOpen(false)
    window.requestAnimationFrame(() => openerRef.current?.focus())
  }

  const load = async (preserveSelection = false) => {
    const generation = ++request.current
    setLoading(true)
    setError('')
    try {
      const next = await api.relatedDatasets(sourceIdentity!, {
        q: q.trim() || undefined,
        folder: folder.trim() || undefined,
        limit: 12,
      })
      if (generation !== request.current) return
      setPage(next)
      if (preserveSelection && candidate) {
        const refreshed = next.candidates.find((item) => (
          (item.identity.registrationId ?? item.identity.sourceBindingId)
            === (candidate.identity.registrationId ?? candidate.identity.sourceBindingId)
        ))
        if (refreshed) {
          setCandidate(refreshed)
          setCandidateBase(refreshed)
          setRequestedRevisionId('')
        }
        else setError('The selected dataset is no longer in this bounded result. Refine or choose again.')
      } else {
        setCandidate(null)
        setCandidateBase(null)
      }
    } catch (reason) {
      if (generation === request.current) {
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    } finally {
      if (generation === request.current) setLoading(false)
    }
  }

  useEffect(() => {
    if (!open || !sourceIdentity || candidate) return
    const timer = window.setTimeout(() => { void load() }, q.trim() ? 200 : 0)
    return () => window.clearTimeout(timer)
    // Candidate review deliberately freezes the query until the user goes back.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, sourceIdentity, q, folder, candidate])

  useEffect(() => {
    if (!candidateBase) {
      setRevisions(null)
      setRevisionError('')
      setRequestedRevisionId('')
      return
    }
    let active = true
    setLoadingRevisions(true)
    setRevisionError('')
    api.relatedDatasetRevisions(candidateBase.identity, { limit: 20 }).then(
      (next) => { if (active) setRevisions(next) },
      (reason) => {
        if (active) setRevisionError(reason instanceof Error ? reason.message : String(reason))
      },
    ).finally(() => { if (active) setLoadingRevisions(false) })
    return () => { active = false }
  }, [candidateBase])

  useEffect(() => {
    if (!open) return
    const dialog = dialogRef.current
    if (!dialog) return
    const focusables = () => Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
    )).filter((element) => !element.hasAttribute('hidden'))
    const focusInitial = () => (candidate ? reviewFocusRef.current : focusables()[0])?.focus()
    const timer = window.setTimeout(focusInitial, 0)
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !confirming) {
        event.preventDefault()
        close()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusables()
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open, candidate, confirming])

  if (!context || !sourceIdentity || !canEdit) return null

  const confirm = async (expectedVersion?: number) => {
    if (!candidate || !page) return
    if (expectedVersion == null && (serverVersion == null || currentDraftId != null)) {
      setError('Wait for the current Canvas draft to finish syncing before confirming this graph edit.')
      return
    }
    setConfirming(true)
    setError('')
    try {
      const result = await api.joinWithRelated(doc.id, {
        expectedCanvasVersion: expectedVersion ?? serverVersion!,
        sourceNodeId: context.source.id,
        joinNodeId: context.joinNodeId,
        sourceIdentity: page.source,
        candidate,
        q: q.trim() || undefined,
        folder: folder.trim() || undefined,
        how,
      })
      useStore.getState().loadDoc(result.canvas)
      useStore.getState().select(result.joinNodeId)
      useStore.getState().pushToast('Added the reviewed Source and Join as one Canvas edit', 'success')
      close()
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason)
      setError(message)
    } finally {
      setConfirming(false)
    }
  }

  const selectRevision = async (revisionId: string) => {
    if (!candidateBase || !page) return
    setRequestedRevisionId(revisionId)
    if (!revisionId) {
      setCandidate(candidateBase)
      setRevisionError('')
      return
    }
    setRevising(true)
    setRevisionError('')
    try {
      const reviewed = await api.reviewRelatedDatasetRevision(page.source, candidateBase, revisionId, {
        q: q.trim() || undefined,
        folder: folder.trim() || undefined,
      })
      setCandidate(reviewed)
    } catch (reason) {
      setRevisionError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setRevising(false)
    }
  }

  const loadMoreRevisions = async () => {
    if (!candidateBase || !revisions?.nextCursor) return
    setLoadingMoreRevisions(true)
    setRevisionError('')
    try {
      const next = await api.relatedDatasetRevisions(candidateBase.identity, {
        limit: 20, cursor: revisions.nextCursor,
      })
      setRevisions((current) => {
        if (!current) return next
        const known = new Set(current.items.map((item) => item.revisionId))
        return {
          ...next,
          items: [...current.items, ...next.items.filter((item) => !known.has(item.revisionId))],
        }
      })
    } catch (reason) {
      setRevisionError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoadingMoreRevisions(false)
    }
  }

  const reapplyLatestCanvas = async () => {
    setConfirming(true)
    setError('')
    try {
      const latest = await api.getCanvas(doc.id)
      await confirm(latest.version)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
      setConfirming(false)
    }
  }

  const conflict = error && error.toLowerCase().includes('canvas')
  const datasetConflict = error && (
    error.toLowerCase().includes('dataset')
    || error.toLowerCase().includes('evidence')
    || error.toLowerCase().includes('revision')
  )
  const sourceFallback = page
    ? `${page.source.registrationId ?? page.source.sourceBindingId}@${page.source.revisionMode}`
    : sourceIdentity.kind === 'local' ? sourceIdentity.registrationId
      : `${sourceIdentity.mountId}/${sourceIdentity.sourceBindingId}`
  const candidateIdentity = candidate
    ? candidate.exactRef
      ? `${candidate.exactRef.datasetId}@${candidate.exactRef.revisionId}`
      : `${candidate.identity.registrationId ?? candidate.identity.sourceBindingId}@${candidate.identity.revisionMode}`
    : ''
  const reviewedRevisionId = candidate?.identity.revisionMode === 'exact'
    ? candidate.identity.revisionId ?? '' : ''
  const exactRevisionPending = Boolean(requestedRevisionId && requestedRevisionId !== reviewedRevisionId)
  const declared = page?.candidates.filter((item) => item.evidence !== 'schema_match') ?? []
  const inferred = page?.candidates.filter((item) => item.evidence === 'schema_match') ?? []
  const sourceOnRight = context.emptyPort === 'a'
  const oriented = candidate ? orientedJoinFacts(candidate, sourceOnRight) : null
  const sourceReview = {
    name: page?.sourceName ?? context.source.data.title,
    role: 'Selected dataset',
  }
  const relatedReview = candidate ? {
    name: candidate.name,
    role: 'Related dataset',
  } : null
  const leftReview = sourceOnRight ? relatedReview : sourceReview
  const rightReview = sourceOnRight ? sourceReview : relatedReview
  const cardinalityCopy = oriented ? cardinalityExplanation(oriented.cardinality) : ''
  const triggerLabel = surface === 'canvas'
    ? context.joinNodeId
      ? `Join with related data on ${context.emptyPort === 'a' ? 'left' : 'right'} input`
      : 'Join with related data'
    : 'Join with…'

  return (
    <>
      <Button type="button" size="sm" variant="outline"
        data-testid={`${surface === 'canvas' ? 'join-with-related-canvas' : 'join-with-related'}-${nodeId}`}
        className={cn('w-full justify-start', surface === 'canvas' && 'nodrag mt-1.5 h-7 px-2 text-[10.5px]')}
        onClick={(event) => {
          event.stopPropagation()
          openerRef.current = event.currentTarget
          setOpen(true)
          setError('')
        }}>
        {triggerLabel}
      </Button>
      {open && createPortal(<div className="dp-modal-overlay fixed inset-0 z-[80] grid place-items-center bg-black/40 p-4"
        onMouseDown={(event) => { if (event.target === event.currentTarget && !confirming) close() }}>
        <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Join with related data"
          className="flex max-h-[84vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-border bg-card shadow-2xl">
          <div className="flex items-start justify-between border-b border-border px-4 py-3">
            <div>
              <div className="text-sm font-semibold text-foreground">Join with related data</div>
              <div className="mt-0.5 text-[11px] text-muted-foreground">
                Review the relationship before adding this Join to the Canvas.
              </div>
            </div>
            <button type="button" aria-label="Cancel Join with related data" disabled={confirming}
              onClick={close} className="text-lg text-muted-foreground">×</button>
          </div>
          <div className="overflow-y-auto p-4">
            {!candidate ? <>
              {page && <div className="mb-3 text-xs text-muted-foreground">
                Join <strong className="text-foreground">{page.sourceName}</strong> with a related dataset.
              </div>}
              <div className="grid grid-cols-2 gap-2">
                <Label className="text-[10.5px]">Search
                  <Input autoFocus value={q} onChange={(event) => setQ(event.target.value)}
                    placeholder="Dataset, column, tag…" className="mt-1 h-8" />
                </Label>
                <Label className="text-[10.5px]">Folder
                  <Input value={folder} onChange={(event) => setFolder(event.target.value)}
                    placeholder="Optional folder subtree" className="mt-1 h-8" />
                </Label>
              </div>
              {error && <div role="alert" className="mt-3 rounded border border-destructive/30 p-2 text-[11px] text-destructive">
                {error} <button className="ml-1 font-semibold underline" onClick={() => void load()}>Retry</button>
              </div>}
              {loading && <div className="py-8 text-center text-xs text-muted-foreground">Finding bounded candidates…</div>}
              {!loading && page && page.candidates.length === 0 && <div data-testid="related-no-results"
                className="py-8 text-center text-xs text-muted-foreground">
                No related datasets in this search/folder scope.
              </div>}
              {!loading && declared.length > 0 && <CandidateGroup title="Declared and proven references"
                candidates={declared} swapInputs={sourceOnRight} onSelect={(item) => {
                  setCandidate(item); setCandidateBase(item)
                  setRequestedRevisionId(item.identity.revisionMode === 'exact' ? item.identity.revisionId ?? '' : '')
                }} />}
              {!loading && inferred.length > 0 && <CandidateGroup title="Inferred candidates"
                candidates={inferred} swapInputs={sourceOnRight} onSelect={(item) => {
                  setCandidate(item); setCandidateBase(item)
                  setRequestedRevisionId(item.identity.revisionMode === 'exact' ? item.identity.revisionId ?? '' : '')
                }} />}
              {!loading && page && page.excluded.length > 0 && <details className="mt-3 text-[10.5px] text-muted-foreground">
                <summary>{page.excluded.length} contradicted match{page.excluded.length === 1 ? '' : 'es'} excluded</summary>
                {page.excluded.map((item) => <div key={item.identity.registrationId ?? item.identity.sourceBindingId}
                  className="mt-1 rounded border border-border p-2"><strong>{item.name}</strong> — {item.reason}</div>)}
              </details>}
              {!loading && page?.refinementRequired && <div className="mt-3 rounded border border-amber-300/50 bg-amber-50 p-2 text-[10.5px] text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                {page.scopeNote ?? 'Results are truncated to a bounded working set. Refine search or folder to inspect omitted datasets.'}
              </div>}
            </> : <>
              <button type="button" className="mb-3 text-[11px] font-semibold text-primary"
                ref={reviewFocusRef}
                onClick={() => { setCandidate(null); setCandidateBase(null); setRequestedRevisionId(''); setError('') }}>← Back to candidates</button>
              <div className="grid gap-2 rounded-md border border-border bg-muted/20 p-3 text-[11px]">
                {leftReview && <ReviewIdentity label="Left input (a)" {...leftReview} />}
                {rightReview && <ReviewIdentity label="Right input (b)" {...rightReview} />}
                <div className="grid grid-cols-[120px_1fr] gap-2">
                  <span className="text-muted-foreground">Evidence</span>
                  <span>{EVIDENCE_LABEL[candidate.evidence]} · {candidate.reason}</span>
                  <span className="text-muted-foreground">Keys</span>
                  <span className="font-mono">
                    {qualifiedColumns('a', oriented!.leftColumns)} = {qualifiedColumns('b', oriented!.rightColumns)}
                  </span>
                  <span className="text-muted-foreground">Cardinality</span>
                  <span><span className={cn('rounded px-1.5 py-0.5 font-semibold', CARDINALITY_TONE[oriented!.cardinality])}>
                    {oriented!.cardinality}
                  </span> — {cardinalityCopy}</span>
                  <span className="text-muted-foreground">Version</span>
                  <span>
                    <select aria-label="Related dataset version"
                      value={requestedRevisionId}
                      disabled={loadingRevisions || revising || !revisions}
                      onChange={(event) => { void selectRevision(event.target.value) }}
                      className="h-7 max-w-full rounded border border-border bg-background px-2">
                      <option value="">Current version</option>
                      {revisions?.items.map((revision, index) => <option key={revision.revisionId} value={revision.revisionId}>
                        {revisionLabel(index, revision.committedAt)}
                      </option>)}
                    </select>
                    {loadingRevisions && <span className="ml-2 text-muted-foreground">Loading retained versions…</span>}
                    {revising && <span className="ml-2 text-muted-foreground">Reviewing selected version…</span>}
                    {revisions?.hasMore && <Button type="button" size="sm" variant="outline"
                      className="ml-2 h-7" disabled={loadingMoreRevisions || revising}
                      onClick={() => void loadMoreRevisions()}>
                      {loadingMoreRevisions ? 'Loading…' : 'Load more versions'}
                    </Button>}
                    {revisionError && <span role="status" className="ml-2 text-muted-foreground">
                      {friendlyRevisionError(revisionError)}
                      {requestedRevisionId && <Button type="button" size="sm" variant="outline"
                        className="ml-2 h-7" disabled={revising}
                        onClick={() => void selectRevision(requestedRevisionId)}>Retry selected version</Button>}
                    </span>}
                  </span>
                  <span className="text-muted-foreground">Join type</span>
                  <select aria-label="Join type" value={how} onChange={(event) => setHow(event.target.value as typeof how)}
                    className="h-7 rounded border border-border bg-background px-2">
                    {['inner', 'left', 'right', 'outer'].map((item) => <option key={item}>{item}</option>)}
                  </select>
                  <span className="text-muted-foreground">Join behavior</span>
                  <span data-testid="related-join-behavior">
                    {joinTypeMeaning(how, leftReview!.name, rightReview!.name)}
                  </span>
                </div>
                <details className="text-[10px] text-muted-foreground">
                  <summary>Details</summary>
                  <div className="mt-1 break-all font-mono">
                    Selected binding: {sourceFallback}<br />
                    Related binding: {candidateIdentity}
                  </div>
                  {revisionError && <div className="mt-1 break-all font-mono">Diagnostic: {revisionError}</div>}
                  {error && <div className="mt-1 break-all font-mono">Diagnostic: {error}</div>}
                </details>
              </div>
              {error && <div role="alert" className="mt-3 rounded border border-destructive/30 p-2 text-[11px] text-destructive">
                {friendlyActionError(error)}
                <div className="mt-2 flex gap-2">
                  {conflict && <Button size="sm" variant="outline" disabled={confirming}
                    onClick={() => void reapplyLatestCanvas()}>Reapply to latest Canvas</Button>}
                  {datasetConflict && <Button size="sm" variant="outline" disabled={loading || confirming}
                    onClick={() => void load(true)}>Refresh review</Button>}
                </div>
              </div>}
            </>}
          </div>
          <div className="flex justify-end gap-2 border-t border-border px-4 py-3">
            <Button type="button" variant="ghost" disabled={confirming} onClick={close}>Cancel</Button>
            {candidate && <Button type="button" data-testid="confirm-related-join" disabled={confirming || revising || exactRevisionPending}
              onClick={() => void confirm()}>
              {confirming ? 'Confirming…' : 'Confirm graph edit'}
            </Button>}
          </div>
        </div>
      </div>, document.body)}
    </>
  )
}

function CandidateGroup({ title, candidates, swapInputs, onSelect }: {
  title: string
  candidates: RelatedDatasetCandidate[]
  swapInputs: boolean
  onSelect: (candidate: RelatedDatasetCandidate) => void
}) {
  return <section className="mt-4">
    <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{title}</h3>
    <div className="grid gap-1.5">
      {candidates.map((candidate) => {
        const oriented = orientedJoinFacts(candidate, swapInputs)
        return <button type="button"
          key={candidate.identity.registrationId ?? candidate.identity.sourceBindingId}
          onClick={() => onSelect(candidate)}
          className="grid grid-cols-[1fr_auto] gap-2 rounded-md border border-border p-2.5 text-left hover:bg-accent">
          <span className="min-w-0">
            <span className="block truncate text-xs font-semibold text-foreground">{candidate.name}</span>
            <span className="block text-[10.5px] text-muted-foreground">{relationshipEvidence(candidate)}</span>
            <span className="block truncate font-mono text-[10px] text-muted-foreground">
              {qualifiedColumns('a', oriented.leftColumns)} = {qualifiedColumns('b', oriented.rightColumns)}
            </span>
            <span className="block text-[10px] text-muted-foreground">{EVIDENCE_LABEL[candidate.evidence]} · Inner join</span>
            <span className="block text-[10px] text-muted-foreground">{cardinalityExplanation(oriented.cardinality)}</span>
          </span>
          <span className={cn('self-center rounded px-1.5 py-0.5 text-[9.5px] font-semibold',
            CARDINALITY_TONE[oriented.cardinality])}>{oriented.cardinality}</span>
        </button>
      })}
    </div>
  </section>
}

function ReviewIdentity({ label, name, role }: {
  label: string
  name: string
  role: string
}) {
  return <div className="grid grid-cols-[120px_1fr] gap-2">
    <span className="text-muted-foreground">{label}</span>
    <span>
      <strong>{name}</strong>
      <span className="ml-1.5 text-[9.5px] text-muted-foreground">{role}</span>
    </span>
  </div>
}
