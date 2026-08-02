import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  editorInputFitsPreviewCap, PreviewDetails, PreviewProvenance, PreviewSummary,
} from './PreviewPresentation'
import type { SampleResult } from '../types/api'

const preview = (overrides: Partial<SampleResult> = {}): SampleResult => ({
  columns: [], rows: [{ id: 1 }], rowCount: null, hasMore: true, truncated: true,
  completeness: 'page', notPreviewable: false, wire: 'dataset',
  sampleProvenance: {
    strategy: 'prefix', seed: null, requestedRows: 50, scannedRows: null, returnedRows: 1,
    totalRows: null, datasetIdentity: 'dataset://events', datasetRevision: 'revision-1',
    identity: 'a'.repeat(64), limitations: ['This is a prefix preview, not representative or random.'],
  },
  ...overrides,
})

describe('Preview presentation', () => {
  it('keeps an ordinary prefix preview to its visible range until details are opened', () => {
    const data = preview()
    render(<><PreviewSummary data={data} /><PreviewDetails provenance={data.sampleProvenance} /></>)

    expect(screen.getByText('rows 1–1')).toBeInTheDocument()
    expect(screen.queryByText(/Full dataset not scanned/i)).not.toBeInTheDocument()
    const details = screen.getByTestId('preview-details')
    expect(details).not.toHaveAttribute('open')
    fireEvent.click(screen.getByText('Preview details'))
    expect(screen.getByText(/Requested 50 rows.*scanned unknown.*returned 1.*total unknown/i)).toBeInTheDocument()
    expect(screen.getByTestId('preview-details')).not.toHaveTextContent('dataset://events')
    expect(screen.getByTestId('preview-details')).not.toHaveTextContent('revision-1')
  })

  it('labels a reservoir sample and keeps one actionable bounded-input warning', () => {
    const data = preview({
      limitScope: 'each-source', limitReason: 'preview-scan', rowLimit: 2_000,
      sampleProvenance: { ...preview().sampleProvenance!, strategy: 'reservoir', seed: 42, returnedRows: 50 },
    })
    render(<PreviewSummary data={data} />)

    expect(screen.getByText('Random sample · 50 rows · seed 42')).toBeInTheDocument()
    expect(screen.getByText('Preview uses up to 2,000 rows from each input; output may differ from a full run.')).toBeInTheDocument()
  })

  it('removes the input-cap warning only for a complete retained editor input within the cap', () => {
    const data = preview({
      limitScope: 'each-source', limitReason: 'preview-scan', rowLimit: 2_000,
      editorTestInput: { runId: 'run-1', nodeId: 'sample', portId: 'out', label: 'Editor sample', rows: 1_000 },
    })
    render(<PreviewSummary data={data} suppressSourceCapWarning={editorInputFitsPreviewCap(data)} />)

    expect(editorInputFitsPreviewCap(data)).toBe(true)
    expect(screen.queryByText(/Preview uses up to 2,000 rows from each input/)).not.toBeInTheDocument()
  })

  it('keeps limitations for unknown or capped retained editor inputs and for output windows', () => {
    const unknownInput = preview({
      limitScope: 'each-source', limitReason: 'preview-scan', rowLimit: 2_000,
      editorTestInput: { runId: 'run-1', nodeId: 'sample', portId: 'out', label: 'Editor sample' },
    })
    const cappedInput = {
      ...unknownInput,
      editorTestInput: { ...unknownInput.editorTestInput!, rows: 2_001 },
    }
    const outputWindow = {
      ...unknownInput,
      completeness: 'capped' as const,
      limitScope: 'result-window' as const,
      limitReason: 'interactive-row-budget' as const,
      editorTestInput: { ...unknownInput.editorTestInput!, rows: 1_000 },
    }
    const { rerender } = render(
      <PreviewSummary data={unknownInput} suppressSourceCapWarning={editorInputFitsPreviewCap(unknownInput)} />,
    )

    expect(editorInputFitsPreviewCap(unknownInput)).toBe(false)
    expect(screen.getByText('Preview uses up to 2,000 rows from each input; output may differ from a full run.')).toBeInTheDocument()

    rerender(<PreviewSummary data={cappedInput} suppressSourceCapWarning={editorInputFitsPreviewCap(cappedInput)} />)
    expect(editorInputFitsPreviewCap(cappedInput)).toBe(false)
    expect(screen.getByText('Preview uses up to 2,000 rows from each input; output may differ from a full run.')).toBeInTheDocument()

    rerender(<PreviewSummary data={outputWindow} suppressSourceCapWarning={editorInputFitsPreviewCap(outputWindow)} />)
    expect(screen.getByText('Showing up to 2,000 rows; run the node to inspect the full result.')).toBeInTheDocument()
  })

  it('can keep a plain Source to the range while retaining provenance on demand', () => {
    const data = preview({
      limitScope: 'each-source', limitReason: 'preview-scan', rowLimit: 2_000,
    })
    render(<>
      <PreviewSummary data={data} showWarning={false} />
      <PreviewDetails provenance={data.sampleProvenance} />
    </>)

    expect(screen.getByText('rows 1–1')).toBeInTheDocument()
    expect(screen.queryByText(/2,000 rows from each input/)).not.toBeInTheDocument()
    expect(screen.getByText('Preview details')).toBeInTheDocument()
  })

  it('uses catalog-specific capped copy instead of promising a node action', () => {
    const data = preview({
      completeness: 'capped', limitScope: 'result-window',
      limitReason: 'interactive-row-budget', rowLimit: 2_000,
    })
    render(<PreviewSummary data={data} surface="catalog" />)

    expect(screen.getByText('Preview is limited to 2,000 rows; the dataset may contain more.')).toBeInTheDocument()
    expect(screen.queryByText(/run the node/i)).not.toBeInTheDocument()
  })

  it('keeps the reservoir summary on provenance-only surfaces such as Stats and Run history', () => {
    const provenance = {
      ...preview().sampleProvenance!, strategy: 'reservoir' as const, seed: 7, returnedRows: 4,
    }
    render(<PreviewProvenance provenance={provenance} />)

    expect(screen.getByText('Random sample · 4 rows · seed 7')).toBeInTheDocument()
    expect(screen.getByText('Preview details')).toBeInTheDocument()
  })
})
