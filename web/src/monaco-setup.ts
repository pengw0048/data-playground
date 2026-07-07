// Monaco, wired to run FULLY OFFLINE (the kernel serves dist/ with no CDN). Import this once at
// the top of main.tsx, before anything renders. We use the trimmed `editor.api` entrypoint and pull
// in only the SQL + Python Monarch grammars, so the bundle stays small and we need only the base
// editor worker (the json/css/html/ts language *services* are the ones with dedicated workers, and
// we don't use those languages).
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api'
import { loader } from '@monaco-editor/react'
import 'monaco-editor/esm/vs/basic-languages/sql/sql.contribution'
import 'monaco-editor/esm/vs/basic-languages/python/python.contribution'
// Vite's ?worker import yields a Worker constructor, bundled locally into dist/ (same-origin).
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'

self.MonacoEnvironment = { getWorker: () => new editorWorker() }
loader.config({ monaco }) // use the bundled instance — never fetch from a CDN

// Light theme matching the app's white surfaces.
monaco.editor.defineTheme('dp-light', {
  base: 'vs', inherit: true, rules: [],
  colors: {
    'editor.background': '#ffffff',
    'editor.foreground': '#1f2328',
    'editorLineNumber.foreground': '#b6bcc4',
    'editor.lineHighlightBackground': '#f6f8fa',
    'editorCursor.foreground': '#1f2328',
    'editor.selectionBackground': '#cfe8ff',
  },
})

// Dark theme matching the app's dark code surfaces (--code-bg / --code-text in index.css).
monaco.editor.defineTheme('dp-dark', {
  base: 'vs-dark', inherit: true, rules: [],
  colors: {
    'editor.background': '#1a1e26',
    'editor.foreground': '#d7dde5',
    'editorLineNumber.foreground': '#4b5462',
    'editor.lineHighlightBackground': '#20242c',
    'editorCursor.foreground': '#d7dde5',
    'editor.selectionBackground': '#2a3a4d',
  },
})

// Column-name autocomplete for SQL/Python cells, backed by a mutable list the UI keeps current
// (columns the user has seen in previews). Registered once — providers are global per language.
export const columnStore: { columns: string[] } = { columns: [] }
for (const lang of ['sql', 'python'] as const) {
  monaco.languages.registerCompletionItemProvider(lang, {
    provideCompletionItems(model, position) {
      const word = model.getWordUntilPosition(position)
      const range: monaco.IRange = {
        startLineNumber: position.lineNumber, endLineNumber: position.lineNumber,
        startColumn: word.startColumn, endColumn: word.endColumn,
      }
      return {
        suggestions: columnStore.columns.map((col) => ({
          label: col, kind: monaco.languages.CompletionItemKind.Field,
          insertText: col, detail: 'column', range,
        })),
      }
    },
  })
}

export { monaco }
