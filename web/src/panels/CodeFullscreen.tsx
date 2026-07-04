import { Suspense, lazy, useEffect } from 'react'
import { useStore } from '../store/graph'
import { color } from '../theme/tokens'
import { Icon } from '../ui/Icon'

const CodeEditor = lazy(() => import('../ui/CodeEditor').then((m) => ({ default: m.CodeEditor })))

// A full-viewport Monaco editor for one node's code param — the "open fullscreen" path from the
// Inspector (and later from code-on-canvas). Edits write straight to the node's config.
export function CodeFullscreen() {
  const fs = useStore((s) => s.fullscreenCode)
  const node = useStore((s) => (fs ? s.doc.nodes.find((n) => n.id === fs.nodeId) : undefined))
  const updateConfig = useStore((s) => s.updateConfig)
  const close = useStore((s) => s.closeCodeFullscreen)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [close])
  if (!fs || !node) return null

  const language = fs.lang === 'sql' ? 'sql' : 'python'
  const value = String((node.data.config as Record<string, unknown>)[fs.param] ?? '')

  return (
    <div style={{ position: 'fixed', inset: 0, zIndex: 60, background: 'rgba(16,20,30,0.45)', display: 'flex', flexDirection: 'column', padding: 28 }}
      onClick={close}>
      <div onClick={(e) => e.stopPropagation()}
        style={{ flex: 1, minHeight: 0, background: '#fff', borderRadius: 12, overflow: 'hidden', display: 'flex', flexDirection: 'column', boxShadow: '0 20px 60px rgba(16,20,30,0.35)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px', borderBottom: `1px solid ${color.hairline}` }}>
          <Icon name="code" size={14} style={{ color: color.text3 }} />
          <span style={{ fontSize: 13, fontWeight: 600, color: color.ink }}>{node.data.title}</span>
          <span style={{ fontSize: 12.5, color: color.text3 }}>· {fs.param} · {language}</span>
          <span style={{ flex: 1 }} />
          <button onClick={close} aria-label="Close" title="Close (Esc)"
            style={{ width: 28, height: 26, display: 'grid', placeItems: 'center', border: 'none', borderRadius: 7, background: 'transparent', color: color.text2, cursor: 'pointer' }}>
            <Icon name="close" size={15} />
          </button>
        </div>
        <div style={{ flex: 1, minHeight: 0 }}>
          <Suspense fallback={<div style={{ height: '100%', display: 'grid', placeItems: 'center', color: color.text3, fontSize: 12 }}>loading editor…</div>}>
            <CodeEditor language={language} height="100%" value={value} onChange={(v) => updateConfig(fs.nodeId, { [fs.param]: v })} />
          </Suspense>
        </div>
      </div>
    </div>
  )
}
