import { Highlight, themes } from 'prism-react-renderer'
import type { CSSProperties } from 'react'
import { useResolvedTheme } from '../theme/mode'

// Syntax-highlighted read-only code preview for node cards, via prism-react-renderer (a proper,
// lightweight, synchronous highlighter). The full editor in the code panel is Monaco.
export function CodeSnippet({ code, language, style }: {
  code: string; language: 'sql' | 'python'; style?: CSSProperties
}) {
  const dark = useResolvedTheme() === 'dark'
  return (
    <Highlight code={code} language={language} theme={dark ? themes.vsDark : themes.github}>
      {({ tokens, getLineProps, getTokenProps }) => (
        <span className="dp-mono" style={{ display: 'block', background: 'transparent', ...style }}>
          {tokens.map((line, i) => (
            <span key={i} {...getLineProps({ line })} style={{ display: 'block' }}>
              {line.map((token, key) => <span key={key} {...getTokenProps({ token })} />)}
            </span>
          ))}
        </span>
      )}
    </Highlight>
  )
}
