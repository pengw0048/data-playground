import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { useStore } from '../store/graph'
import { Icon } from '../ui/Icon'
import { cn } from '@/lib/utils'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

// Settings scoped to THIS canvas (not the app/workspace ones). Kept deliberately separate from the
// global SettingsModal — this is about the open file: its name and who can see it.
export function CanvasSettingsModal({ onClose }: { onClose: () => void }) {
  const doc = useStore((s) => s.doc)
  const renameFile = useStore((s) => s.renameFile)
  const setRequirements = useStore((s) => s.setRequirements)
  const [name, setName] = useState(doc.name ?? '')
  const [reqs, setReqs] = useState((doc.requirements ?? []).join('\n'))
  const [visibility, setVisibility] = useState<'private' | 'workspace'>('private')

  useEffect(() => {
    api.getShares(doc.id).then((s) => setVisibility(s.visibility === 'workspace' ? 'workspace' : 'private')).catch(() => {})
  }, [doc.id])

  const setVis = (v: 'private' | 'workspace') => {
    setVisibility(v)
    api.addShare(doc.id, { visibility: v }).catch(() => {})
  }

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="dp-modal-overlay gap-0 overflow-hidden p-0 w-[420px] max-w-[92vw] rounded-xl">
        <div className="flex items-center gap-2 border-b border-border py-3 pl-4 pr-12">
          <span className="flex items-center text-muted-foreground"><Icon name="grid" size={14} /></span>
          <DialogTitle className="text-sm font-semibold">Canvas settings</DialogTitle>
        </div>
        <DialogDescription className="sr-only">Settings for the current canvas: its name and visibility.</DialogDescription>

        <div className="flex flex-col gap-4 p-4">
          <div>
            <Label className="mb-1 block text-[11.5px] font-normal text-muted-foreground">Name</Label>
            <Input value={name} onChange={(e) => { setName(e.target.value); renameFile(e.target.value) }} placeholder="untitled" />
          </div>
          <div>
            <div className="mb-1.5 text-[11.5px] text-muted-foreground">Visibility</div>
            <div className="flex gap-2">
              {(['private', 'workspace'] as const).map((v) => (
                <button key={v} onClick={() => setVis(v)}
                  className={cn('flex-1 rounded-lg border px-2.5 py-2.5 text-left transition-colors',
                    visibility === v ? 'border-primary bg-primary/10' : 'border-border bg-background hover:bg-accent/50')}>
                  <div className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
                    <Icon name={v === 'private' ? 'grid' : 'link'} size={12} /> {v === 'private' ? 'Private' : 'Workspace'}
                  </div>
                  <div className="mt-0.5 text-[10.5px] font-normal text-muted-foreground">{v === 'private' ? 'Only you and people you invite' : 'Everyone in the workspace can edit'}</div>
                </button>
              ))}
            </div>
            <div className="mt-2 text-[10.5px] text-muted-foreground">Invite specific people from the <b>Share</b> button.</div>
          </div>
          <div>
            <Label className="mb-1 block text-[11.5px] font-normal text-muted-foreground">Dependencies (pip)</Label>
            <textarea
              value={reqs}
              onChange={(e) => { setReqs(e.target.value); setRequirements(e.target.value.split('\n').map((s) => s.trim()).filter(Boolean)) }}
              placeholder={'pandas\nscikit-learn==1.5'}
              spellCheck={false}
              rows={3}
              className="dp-mono w-full resize-y rounded-md border border-border bg-background px-2 py-1.5 text-[11.5px] text-foreground outline-none"
            />
            <div className="mt-1 text-[10.5px] text-muted-foreground">One pip spec per line — installed on this canvas's kernel, then importable in <code>transform</code> cells. Travels with the canvas.</div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
