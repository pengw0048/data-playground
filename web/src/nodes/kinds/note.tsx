import { useState, type CSSProperties } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { register, type NodeComponentProps } from '../registry'
import { useStore } from '../../store/graph'
import { color, radius, shadow } from '../../theme/tokens'

// A canvas annotation: a resizable-ish markdown text box. It is a real node (persisted, movable,
// selectable) but carries NO ports and is stripped from the graph sent to the kernel (see toGraph),
// so it never participates in the dataflow — purely for documenting a canvas.
function Note({ id, data, selected }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const [editing, setEditing] = useState(false)
  const md = String(data.config.markdown ?? '')

  return (
    <div
      onDoubleClick={() => setEditing(true)}
      style={{
        width: 268, minHeight: 96, background: '#fffdf3',
        border: selected ? `1.5px solid ${color.focus}` : `1px solid #ece3c4`,
        borderRadius: radius.node, boxShadow: selected ? shadow.focus : shadow.card,
        padding: 12, fontSize: 12.5, color: color.ink, overflow: 'hidden',
      }}
    >
      {editing ? (
        <textarea
          className="nodrag dp-mono"
          autoFocus
          value={md}
          onChange={(e) => updateConfig(id, { markdown: e.target.value })}
          onBlur={() => setEditing(false)}
          onKeyDown={(e) => { if (e.key === 'Escape') setEditing(false) }}
          spellCheck={false}
          placeholder="# Markdown…"
          style={{
            width: '100%', minHeight: 120, resize: 'vertical', border: 'none', outline: 'none',
            background: 'transparent', color: color.ink, fontSize: 12, lineHeight: 1.5,
          }}
        />
      ) : md.trim() ? (
        <div style={mdWrap}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD}>{md}</ReactMarkdown>
        </div>
      ) : (
        <div style={{ color: color.text3, fontStyle: 'italic' }}>Double-click to edit…</div>
      )}
    </div>
  )
}

const mdWrap: CSSProperties = { lineHeight: 1.55, wordBreak: 'break-word' }
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
  blockquote: (p: any) => <blockquote style={{ borderLeft: `3px solid #e0d7b5`, margin: '0 0 6px', padding: '2px 0 2px 8px', color: color.text2 }} {...p} />,
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
