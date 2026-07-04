import { register, type NodeComponentProps } from '../registry'
import { useStore } from '../../store/graph'
import { CodeSnippet } from '../../ui/CodeSnippet'
import { color, radius, shadow } from '../../theme/tokens'
import { Icon } from '../../ui/Icon'

// A "code on the canvas" layer (à la Figma): a code block that lives on the canvas as an annotation.
// Syntax-highlighted read-only preview; double-click (or ⤢) opens the fullscreen Monaco editor. Like
// `note` it's a real node with NO ports and is stripped from the graph sent to the kernel (toGraph).
type Lang = 'sql' | 'python'

function CodeBlock({ id, data, selected }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const openFullscreen = useStore((s) => s.openCodeFullscreen)
  const code = String(data.config.code ?? '')
  const lang: Lang = data.config.lang === 'sql' ? 'sql' : 'python'

  return (
    <div onDoubleClick={() => openFullscreen(id, 'code', lang)} className="dp-no-select"
      style={{ width: 320, minHeight: 96, background: '#fff', border: selected ? `1.5px solid ${color.focus}` : `1px solid ${color.border}`,
        borderRadius: radius.node, boxShadow: selected ? shadow.focus : shadow.card, overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 8px 6px 10px', borderBottom: `1px solid ${color.hairline}`, background: '#f7f8fa' }}>
        <Icon name="code" size={12} style={{ color: color.text3 }} />
        <button className="nodrag" onClick={(e) => { e.stopPropagation(); updateConfig(id, { lang: lang === 'python' ? 'sql' : 'python' }) }}
          title="Toggle language" style={{ border: 'none', background: '#eef0f3', color: color.text2, fontSize: 9.5, fontWeight: 600, letterSpacing: 0.4, textTransform: 'uppercase', padding: '2px 7px', borderRadius: radius.chip, cursor: 'pointer' }}>
          {lang}
        </button>
        <span style={{ flex: 1 }} />
        <button className="nodrag" onClick={(e) => { e.stopPropagation(); openFullscreen(id, 'code', lang) }}
          title="Open fullscreen editor" style={{ border: 'none', background: 'transparent', color: color.text3, cursor: 'pointer', display: 'grid', placeItems: 'center', width: 22, height: 20 }}>
          <Icon name="external" size={13} />
        </button>
      </div>
      <div style={{ padding: 10, maxHeight: 240, overflow: 'auto' }}>
        {code.trim()
          ? <CodeSnippet code={code} language={lang} style={{ fontSize: 11, lineHeight: 1.5 }} />
          : <span style={{ color: color.text3, fontStyle: 'italic', fontSize: 12 }}>Double-click to write code…</span>}
      </div>
    </div>
  )
}

register(
  {
    kind: 'code',
    title: 'code',
    category: 'inspect',
    tag: 'code',
    inputs: [],
    outputs: [],
    canBypass: false,
    blurb: 'code block on the canvas (annotation)',
    defaultData: () => ({ title: 'code', status: 'draft', config: { code: '# code — double-click to edit\n', lang: 'python' }, meta: '' }),
  },
  CodeBlock,
)
