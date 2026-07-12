import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { register, type NodeComponentProps } from '../registry'
import { roleCanEdit, useStore } from '../../store/graph'
import { color } from '../../theme/tokens'
import { cn } from '@/lib/utils'

// A canvas annotation: a resizable-ish markdown text box. It is a real node (persisted, movable,
// selectable) but carries NO ports and is stripped from the graph sent to the kernel (see toGraph),
// so it never participates in the dataflow — purely for documenting a canvas.
function Note({ id, data, selected }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const [editing, setEditing] = useState(false)
  const md = String(data.config.markdown ?? '')
  useEffect(() => { if (!canEdit) setEditing(false) }, [canEdit])

  return (
    // the note is a paper-like annotation with a FIXED cream background, so its ink is pinned dark
    // (text-[#2c2a20]) in both themes — text-foreground would flip to near-white in dark, invisible on cream
    <div
      onDoubleClick={() => { if (canEdit) setEditing(true) }}
      className={cn('min-h-24 w-[268px] overflow-hidden rounded-lg p-3 text-[12.5px] text-[#2c2a20]',
        selected ? 'shadow-md ring-2 ring-primary/20' : 'shadow-sm')}
      style={{ background: '#fffdf3', border: selected ? `1.5px solid ${color.focus}` : '1px solid #ece3c4' }}
    >
      {editing ? (
        <textarea
          className="nodrag nowheel dp-mono min-h-[120px] max-h-[400px] w-full resize-y overflow-y-auto border-none bg-transparent text-xs leading-normal text-[#2c2a20] outline-none"  /* nowheel: let the wheel scroll the textarea, not pan the canvas; ink pinned to match the cream note */
          autoFocus
          value={md}
          onChange={(e) => updateConfig(id, { markdown: e.target.value })}
          onBlur={() => setEditing(false)}
          onKeyDown={(e) => { if (e.key === 'Escape') setEditing(false) }}
          spellCheck={false}
          placeholder="# Markdown…"
        />
      ) : md.trim() ? (
        <div className="nowheel max-h-[400px] overflow-y-auto break-words leading-[1.55]">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD}>{md}</ReactMarkdown>
        </div>
      ) : (
        <div className="italic text-[#9a9482]">{canEdit ? 'Double-click to edit…' : 'Empty note'}</div>
      )}
    </div>
  )
}
// compact, self-contained markdown styling (no global CSS dependency)
const MD = {
  h1: (p: any) => <div style={{ fontSize: 15, fontWeight: 700, margin: '2px 0 6px' }} {...p} />,
  h2: (p: any) => <div style={{ fontSize: 13.5, fontWeight: 700, margin: '2px 0 5px' }} {...p} />,
  h3: (p: any) => <div style={{ fontSize: 12.5, fontWeight: 700, margin: '2px 0 4px' }} {...p} />,
  p: (p: any) => <p style={{ margin: '0 0 6px' }} {...p} />,
  ul: (p: any) => <ul style={{ margin: '0 0 6px', paddingLeft: 18 }} {...p} />,
  ol: (p: any) => <ol style={{ margin: '0 0 6px', paddingLeft: 18 }} {...p} />,
  li: (p: any) => <li style={{ margin: '1px 0' }} {...p} />,
  a: (p: any) => <a style={{ color: color.focus }} target="_blank" rel="noreferrer" {...p} />,
  code: (p: any) => <code className="dp-mono" style={{ background: '#f1efe4', padding: '1px 4px', borderRadius: 4, fontSize: 11 }} {...p} />,
  pre: (p: any) => <pre className="dp-mono" style={{ background: '#f1efe4', padding: 8, borderRadius: 6, overflowX: 'auto', fontSize: 11, margin: '0 0 6px' }} {...p} />,
  blockquote: (p: any) => <blockquote style={{ borderLeft: `3px solid #e0d7b5`, margin: '0 0 6px', padding: '2px 0 2px 8px', color: '#6b6659' }} {...p} />,
  table: (p: any) => <table style={{ borderCollapse: 'collapse', fontSize: 11, margin: '0 0 6px' }} {...p} />,
  th: (p: any) => <th style={{ border: '1px solid #e0d7b5', padding: '2px 6px', textAlign: 'left' }} {...p} />,
  td: (p: any) => <td style={{ border: '1px solid #e0d7b5', padding: '2px 6px' }} {...p} />,
}

register(
  {
    kind: 'note',
    title: 'note',
    category: 'inspect',
    tag: 'note',
    inputs: [],
    outputs: [],
    canBypass: false,
    blurb: 'markdown note (annotation)',
    defaultData: () => ({ title: 'note', status: 'draft', config: { markdown: '# Note\n\nDouble-click to edit.' }, meta: '' }),
  },
  Note,
)
