import { register, type NodeComponentProps } from '../registry'
import { roleCanEdit, useStore } from '../../store/graph'
import { CodeSnippet } from '../../ui/CodeSnippet'
import { Icon } from '../../ui/Icon'
import { cn } from '@/lib/utils'

// A "code on the canvas" layer (à la Figma): a code block that lives on the canvas as an annotation.
// Syntax-highlighted read-only preview; double-click (or ⤢) opens the fullscreen Monaco editor. Like
// `note` it's a real node with NO ports and is stripped from the graph sent to the kernel (toGraph).
type Lang = 'sql' | 'python'

function CodeBlock({ id, data, selected }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const openFullscreen = useStore((s) => s.openCodeFullscreen)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const code = String(data.config.code ?? '')
  const lang: Lang = data.config.lang === 'sql' ? 'sql' : 'python'

  return (
    <div onDoubleClick={() => openFullscreen(id, 'code', lang)}
      className={cn('dp-no-select min-h-24 w-80 overflow-hidden rounded-lg border bg-card shadow-sm',
        selected ? 'border-primary ring-2 ring-primary/20' : 'border-border')}>
      <div className="flex items-center gap-1.5 border-b border-border bg-muted py-1.5 pl-2.5 pr-2 text-muted-foreground">
        <Icon name="code" size={12} />
        <button disabled={!canEdit} className="nodrag rounded bg-secondary px-[7px] py-0.5 text-[9.5px] font-semibold uppercase tracking-[0.4px] text-muted-foreground disabled:cursor-not-allowed disabled:opacity-70" onClick={(e) => { e.stopPropagation(); updateConfig(id, { lang: lang === 'python' ? 'sql' : 'python' }) }}
          title={canEdit ? 'Toggle language' : 'View-only'}>
          {lang}
        </button>
        <span className="flex-1" />
        <button className="nodrag grid h-5 w-[22px] place-items-center text-muted-foreground" onClick={(e) => { e.stopPropagation(); openFullscreen(id, 'code', lang) }}
          title={canEdit ? 'Open fullscreen editor' : 'View full code'}>
          <Icon name="external" size={13} />
        </button>
      </div>
      <div className="max-h-60 overflow-auto p-2.5">
        {code.trim()
          ? <CodeSnippet code={code} language={lang} style={{ fontSize: 11, lineHeight: 1.5 }} />
          : <span className="text-xs italic text-muted-foreground">{canEdit ? 'Double-click to write code…' : 'Empty code block'}</span>}
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
