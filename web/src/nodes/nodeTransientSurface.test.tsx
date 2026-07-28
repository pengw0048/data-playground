import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { useNodeTransientSurface } from './nodeTransientSurface'

function Surface({ id, label }: { id: string; label: string }) {
  const [open, setOpen] = useState(false)
  useNodeTransientSurface(id, open, () => setOpen(false))
  return <>
    <button onClick={() => setOpen((value) => !value)}>{label}</button>
    {open && <div>{`${label} open`}</div>}
  </>
}

describe('node transient surfaces', () => {
  it('closes the active surface when another node surface opens', () => {
    render(<><Surface id="source-picker" label="Dataset picker" /><Surface id="more-menu" label="More menu" /></>)

    fireEvent.click(screen.getByRole('button', { name: 'Dataset picker' }))
    expect(screen.getByText('Dataset picker open')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'More menu' }))

    expect(screen.queryByText('Dataset picker open')).not.toBeInTheDocument()
    expect(screen.getByText('More menu open')).toBeVisible()
  })
})
