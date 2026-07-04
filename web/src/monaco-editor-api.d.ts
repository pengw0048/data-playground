// The trimmed Monaco entrypoint has no bundled .d.ts; it exposes the same API surface as the
// package root, so re-export those types.
declare module 'monaco-editor/esm/vs/editor/editor.api' {
  export * from 'monaco-editor'
}
