import Editor from '@monaco-editor/react'
import { columnStore } from '../monaco-setup' // side-effect: wires Monaco offline (this chunk is lazy-loaded)

// Monaco-backed code cell: syntax highlighting + autocomplete for SQL / Python. This module (and
// all of Monaco) is code-split — CodePanel lazy-imports it, so the editor loads only when opened.
export function CodeEditor({ value, onChange, language, readOnly, height = 200, completions }: {
  value: string
  onChange: (v: string) => void
  language: 'sql' | 'python'
  readOnly?: boolean
  height?: number
  completions?: string[]
}) {
  columnStore.columns = completions ?? []
  return (
    <div style={{ border: '1px solid var(--viewer-border, #e3e6ea)', borderRadius: 8, overflow: 'hidden', height }}>
      <Editor
        language={language}
        theme="dp-light"
        value={value}
        onChange={(v) => onChange(v ?? '')}
        height="100%"
        options={{
          readOnly,
          minimap: { enabled: false },
          fontSize: 12,
          fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
          lineHeight: 18,
          padding: { top: 10, bottom: 10 },
          scrollBeyondLastLine: false,
          automaticLayout: true,
          renderLineHighlight: 'line',
          overviewRulerLanes: 0,
          scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
          wordWrap: 'off',
          tabSize: 2,
        }}
      />
    </div>
  )
}
