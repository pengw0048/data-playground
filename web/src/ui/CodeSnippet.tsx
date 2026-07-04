// A tiny, dependency-free syntax highlighter for the small read-only code previews on node cards.
// (The full editor is Monaco, lazy-loaded in the code panel; this is just for the at-a-glance card
// snippet, so it must be cheap and always-on — hence a ~40-line tokenizer, not a bundled grammar.)
import type { CSSProperties } from 'react'

const SQL_KW = new Set('select from where limit join left right inner outer full cross on group by order as and or not distinct union all having with case when then else end asc desc using offset between like ilike in is null count sum avg min max cast over partition'.split(' '))
const PY_KW = new Set('def return for in if elif else import from as while with try except finally raise class lambda yield pass break continue and or not is none true false global nonlocal del assert async await print len range'.split(' '))

const COLOR: Record<string, string> = {
  kw: '#2f6fd0', str: '#0a7f4f', com: '#9aa0aa', num: '#b5691a', txt: 'inherit',
}

type Tok = { t: keyof typeof COLOR; v: string }

function tokenize(code: string, language: 'sql' | 'python'): Tok[] {
  const kws = language === 'sql' ? SQL_KW : PY_KW
  const lineComment = language === 'sql' ? '--' : '#'
  const out: Tok[] = []
  const n = code.length
  let i = 0
  while (i < n) {
    const ch = code[i]
    if (code.startsWith(lineComment, i)) {
      let j = code.indexOf('\n', i); if (j < 0) j = n
      out.push({ t: 'com', v: code.slice(i, j) }); i = j; continue
    }
    if (ch === "'" || ch === '"') {
      let j = i + 1
      while (j < n && code[j] !== ch) { if (code[j] === '\\') j++; j++ }
      j = Math.min(j + 1, n)
      out.push({ t: 'str', v: code.slice(i, j) }); i = j; continue
    }
    if (/[A-Za-z_]/.test(ch)) {
      let j = i + 1; while (j < n && /\w/.test(code[j])) j++
      const w = code.slice(i, j)
      out.push({ t: kws.has(w.toLowerCase()) ? 'kw' : 'txt', v: w }); i = j; continue
    }
    if (/[0-9]/.test(ch)) {
      let j = i + 1; while (j < n && /[\d.]/.test(code[j])) j++
      out.push({ t: 'num', v: code.slice(i, j) }); i = j; continue
    }
    out.push({ t: 'txt', v: ch }); i++
  }
  return out
}

export function CodeSnippet({ code, language, style }: {
  code: string; language: 'sql' | 'python'; style?: CSSProperties
}) {
  return (
    <span className="dp-mono" style={style}>
      {tokenize(code, language).map((tok, i) => (
        <span key={i} style={{ color: COLOR[tok.t] }}>{tok.v}</span>
      ))}
    </span>
  )
}
