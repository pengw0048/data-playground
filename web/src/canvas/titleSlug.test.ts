import { describe, expect, it } from 'vitest'
import { canvasTitleSlug } from './titleSlug'

describe('canvasTitleSlug', () => {
  it('omits empty, whitespace-only, and the ordinary initial untitled name', () => {
    expect(canvasTitleSlug('')).toBeUndefined()
    expect(canvasTitleSlug('   ')).toBeUndefined()
    expect(canvasTitleSlug(null)).toBeUndefined()
    expect(canvasTitleSlug(undefined)).toBeUndefined()
    // Ordinary first-run / blank Canvas name stays off the URL; any other title may contribute.
    expect(canvasTitleSlug('untitled')).toBeUndefined()
    expect(canvasTitleSlug('Untitled')).toBeUndefined()
    expect(canvasTitleSlug('  untitled  ')).toBeUndefined()
  })

  it('preserves useful non-ASCII titles without lossy transliteration', () => {
    expect(canvasTitleSlug('销售分析')).toBe('销售分析')
    expect(canvasTitleSlug('分析 报告')).toBe('分析-报告')
    expect(canvasTitleSlug('Pipeline 🚀 v2')).toBe('Pipeline-🚀-v2')
    // Combining acute accent on e → NFC single code point.
    expect(canvasTitleSlug('cafe\u0301')).toBe('café')
  })

  it('collapses separators, strips URL delimiters/controls, and caps at 80 code points', () => {
    expect(canvasTitleSlug('My   Analysis___Draft')).toBe('My-Analysis-Draft')
    expect(canvasTitleSlug('a/b#c?d&e=f%g')).toBe('abcdefg')
    expect(canvasTitleSlug('ok\u0000name')).toBe('okname')
    const long = '字'.repeat(100)
    expect(Array.from(canvasTitleSlug(long)!)).toHaveLength(80)
  })
})
