import { useState } from 'react'
import { useStore } from '../store/graph'
import type { LocalCanvasDraft } from '../store/canvasDrafts'
import { Icon } from '../ui/Icon'
import { cn } from '@/lib/utils'

function statusLabel(draft: LocalCanvasDraft): string {
  if (draft.syncState === 'syncing') return 'syncing…'
  if (draft.syncState === 'conflict') return 'conflict'
  if (draft.syncState === 'error') return 'sync blocked'
  return draft.baseCanvasId === null ? 'local only' : 'saved locally'
}

function DraftActions({ draft, close }: { draft: LocalCanvasDraft; close?: () => void }) {
  const retry = useStore((state) => state.retryLocalDraft)
  const fork = useStore((state) => state.forkLocalDraft)
  const discard = useStore((state) => state.discardLocalDraft)
  const exportDraft = useStore((state) => state.exportLocalDraft)
  const openFile = useStore((state) => state.openFile)
  const actionClass = 'rounded px-1.5 py-0.5 text-[10.5px] font-semibold hover:bg-accent disabled:opacity-50'
  return <div className="flex shrink-0 items-center gap-0.5">
    {draft.syncState === 'conflict' && draft.baseCanvasId && (
      <button aria-label={`Open server copy for ${draft.name}`} className={actionClass} onClick={() => { void openFile(draft.baseCanvasId!, { serverCopy: true }); close?.() }}>Open server</button>
    )}
    {draft.syncState === 'conflict' && (
      <button aria-label={`Keep local draft ${draft.name} as new Canvas`} className={actionClass} onClick={() => { void fork(draft.draftId); close?.() }}>Keep as new</button>
    )}
    {draft.syncState !== 'conflict' && (
      <button aria-label={`Retry local draft ${draft.name}`} className={actionClass} disabled={draft.syncState === 'syncing'} onClick={() => void retry(draft.draftId)}>Retry</button>
    )}
    <button aria-label={`Export local draft ${draft.name}`} className={actionClass} onClick={() => exportDraft(draft.draftId)}>Export</button>
    <button aria-label={`Delete local draft ${draft.name}`} title="Delete local draft" className={cn(actionClass, 'text-destructive')} onClick={() => void discard(draft.draftId)}>
      <Icon name="trash" size={11} />
    </button>
  </div>
}

export function WorkspaceLocalDrafts() {
  const drafts = useStore((state) => state.localDrafts) ?? []
  const errors = useStore((state) => state.draftStorageErrors) ?? []
  const openDraft = useStore((state) => state.openLocalDraft)
  const [expanded, setExpanded] = useState(true)
  if (drafts.length === 0 && errors.length === 0) return null
  return <section aria-label="Local Canvas drafts" className="border-b border-amber-300/50 bg-amber-50/80 text-amber-950 dark:bg-amber-950/25 dark:text-amber-100">
    <button type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)} className="flex w-full items-center gap-2 px-7 py-2 text-left text-[12px] font-semibold">
      <Icon name="note" size={13} />
      <span className="flex-1">Local Canvas drafts ({drafts.length})</span>
      <span className="text-[10.5px] font-normal opacity-80">Stored only in this browser until explicitly synced</span>
      <Icon name={expanded ? 'chevronDown' : 'chevronRight'} size={12} />
    </button>
    {expanded && <div className="grid gap-1 border-t border-amber-300/30 px-7 py-2">
      {errors.map((error, index) => <div key={`${index}-${error}`} role="alert" className="text-[11px] text-destructive">{error}</div>)}
      {drafts.map((draft) => <div key={draft.draftId} data-testid="local-draft-row" data-draft-id={draft.draftId} className="flex min-w-0 items-center gap-2 rounded-md bg-background/70 px-2 py-1.5 text-[11.5px]">
        <button className="min-w-0 flex-1 truncate text-left font-semibold hover:underline" onClick={() => openDraft(draft.draftId)}>{draft.name || 'untitled'}</button>
        <span className={cn('shrink-0 text-[10.5px]', draft.syncState === 'conflict' || draft.syncState === 'error' ? 'text-destructive' : 'text-muted-foreground')}>{statusLabel(draft)}</span>
        <DraftActions draft={draft} />
        {draft.lastError && <span className="sr-only">{draft.lastError}</span>}
      </div>)}
    </div>}
  </section>
}

export function CanvasDraftMenu({ close }: { close: () => void }) {
  const drafts = useStore((state) => state.localDrafts) ?? []
  const currentDraftId = useStore((state) => state.currentDraftId)
  const openDraft = useStore((state) => state.openLocalDraft)
  if (drafts.length === 0) return null
  return <div className="border-t border-border py-1">
    <div className="px-2.5 py-1 text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground">Local drafts</div>
    {drafts.map((draft) => <div key={draft.draftId} className={cn('flex items-center gap-1 px-2 py-1', draft.draftId === currentDraftId && 'bg-accent')}>
      <button className="min-w-0 flex-1 truncate text-left text-[12px] text-foreground" onClick={() => { openDraft(draft.draftId); close() }}>
        {draft.name || 'untitled'} <span className="text-[9.5px] text-muted-foreground">· {statusLabel(draft)}</span>
      </button>
      <DraftActions draft={draft} close={close} />
    </div>)}
  </div>
}
