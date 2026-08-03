import Editor, { type OnMount } from '@monaco-editor/react'
import { useEffect, useRef } from 'react'
import { columnStore } from '../monaco-setup' // side-effect: wires Monaco offline (this chunk is lazy-loaded)
import { useResolvedTheme } from '../theme/mode'

type MonacoEditor = Parameters<OnMount>[0]

function focusEditorLine(editor: MonacoEditor, lineNumber: number) {
  editor.setPosition({ lineNumber, column: 1 })
  editor.revealLineInCenter(lineNumber)
  editor.getDomNode()?.setAttribute('data-cursor-line-number', String(lineNumber))
  editor.focus()
}

// Monaco-backed code cell: syntax highlighting + autocomplete for SQL / Python. This module (and
// all of Monaco) is code-split — CodePanel lazy-imports it, so the editor loads only when opened.
export function CodeEditor({ value, onChange, language, readOnly, height = 200, completions, errorLine }: {
  value: string
  onChange: (v: string) => void
  language: 'sql' | 'python'
  readOnly?: boolean
  height?: number | string // px number, or a CSS length like "100%" to fill a flex container
  completions?: string[]
  errorLine?: number
}) {
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null)
  const cursorListenerRef = useRef<{ dispose: () => void } | null>(null)
  const contentListenerRef = useRef<{ dispose: () => void } | null>(null)
  const errorFocusFrameRef = useRef<number | null>(null)
  const errorLineRef = useRef(errorLine)
  errorLineRef.current = errorLine
  const deferErrorFocus = (editor: MonacoEditor, lineNumber: number) => {
    if (errorFocusFrameRef.current !== null) cancelAnimationFrame(errorFocusFrameRef.current)
    errorFocusFrameRef.current = requestAnimationFrame(() => {
      errorFocusFrameRef.current = null
      if (editorRef.current === editor) focusEditorLine(editor, lineNumber)
    })
  }
  useEffect(() => () => {
    cursorListenerRef.current?.dispose()
    contentListenerRef.current?.dispose()
    if (errorFocusFrameRef.current !== null) cancelAnimationFrame(errorFocusFrameRef.current)
    editorRef.current = null
  }, [])
  useEffect(() => {
    if (!errorLine || !editorRef.current) return
    focusEditorLine(editorRef.current, errorLine)
    // Monaco restores model/view state asynchronously during a busy first layout. Re-apply the
    // server-selected syntax line after that frame so the visible error and editable cursor agree.
    deferErrorFocus(editorRef.current, errorLine)
  }, [errorLine])
  const onMount: OnMount = (editor) => {
    editorRef.current = editor
    cursorListenerRef.current?.dispose()
    contentListenerRef.current?.dispose()
    const editorNode = editor.getDomNode()
    const reflectCursorLine = (lineNumber: number) => {
      editorNode?.setAttribute('data-cursor-line-number', String(lineNumber))
    }
    const position = editor.getPosition()
    if (position) reflectCursorLine(position.lineNumber)
    cursorListenerRef.current = editor.onDidChangeCursorPosition((event) => {
      reflectCursorLine(event.position.lineNumber)
    })
    // @monaco-editor/react can install the controlled value after onMount. Monaco resets the
    // selection to line 1 as part of that model update, so an onMount-only focus is racy on a busy
    // first load. Re-apply a still-current server syntax location after model content settles.
    contentListenerRef.current = editor.onDidChangeModelContent(() => {
      const currentErrorLine = errorLineRef.current
      if (currentErrorLine) deferErrorFocus(editor, currentErrorLine)
    })
    if (errorLine) {
      focusEditorLine(editor, errorLine)
      deferErrorFocus(editor, errorLine)
    }
  }
  columnStore.columns = completions ?? []
  const dark = useResolvedTheme() === 'dark'  // @monaco-editor/react re-applies `theme` reactively
  // nokey keeps React Flow's window-level key handlers (Space pans the canvas) out of the editor.
  return (
    <div className="nokey" style={{ border: '1px solid hsl(var(--border))', borderRadius: 8, overflow: 'hidden', height }}>
      <Editor
        language={language}
        theme={dark ? 'dp-dark' : 'dp-light'}
        value={value}
        onChange={(v) => onChange(v ?? '')}
        onMount={onMount}
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
