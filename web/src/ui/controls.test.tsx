import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { MiniSelect, Segmented } from './controls'

const options = [
  { value: 'adhoc', label: 'Ad-hoc' },
  { value: 'library', label: 'Library' },
] as const

describe('Segmented', () => {
  it.each(options)('does not report the active $label value and stops propagation', ({ value, label }) => {
    const onChange = vi.fn()
    const onParentClick = vi.fn()

    render(
      <div onClick={onParentClick}>
        <Segmented options={[...options]} value={value} onChange={onChange} />
      </div>,
    )

    fireEvent.click(screen.getByRole('button', { name: label }))

    expect(onChange).not.toHaveBeenCalled()
    expect(onParentClick).not.toHaveBeenCalled()
  })

  it('reports an inactive value and stops propagation', () => {
    const onChange = vi.fn()
    const onParentClick = vi.fn()

    render(
      <div onClick={onParentClick}>
        <Segmented options={[...options]} value="adhoc" onChange={onChange} />
      </div>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Library' }))

    expect(onChange).toHaveBeenCalledOnce()
    expect(onChange).toHaveBeenCalledWith('library')
    expect(onParentClick).not.toHaveBeenCalled()
  })
})

describe('MiniSelect', () => {
  it('shows a configured value no option carries instead of the first option', () => {
    render(<MiniSelect value="arrow" options={[{ value: 'rows', label: 'rows' }]} onChange={vi.fn()} />)
    expect(screen.getByRole('combobox')).toHaveValue('arrow')
  })

  it('leaves an unset value on the first option', () => {
    render(<MiniSelect value="" options={[{ value: 'all', label: 'all' }, { value: 'distinct', label: 'distinct' }]} onChange={vi.fn()} />)
    expect(screen.getByRole('combobox')).toHaveValue('all')
  })
})
