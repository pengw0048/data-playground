import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { previewPlanIdentity, type PreviewState } from '../store/graph'
import type { CanvasDoc } from '../types/graph'
import { EditorInputSample } from './EditorInputSample'

const doc: CanvasDoc = {
  id: 'canvas-input', version: 1, requirements: [],
  nodes: [
    { id: 'source', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'Purchases', config: { uri: 'purchases.parquet' } } },
    { id: 'python', type: 'transform', position: { x: 200, y: 0 },
      data: { title: 'Tax', config: { source: 'adhoc', mode: 'map', code: 'def fn(row): return row' } } },
  ],
  edges: [{ id: 'edge', source: 'source', sourceHandle: 'out', target: 'python', targetHandle: 'in' }],
}

function preview(): PreviewState {
  return {
    canvasId: doc.id, nodeId: 'python', requestGeneration: 1,
    planIdentity: previewPlanIdentity(doc, 'python'), parameterBindings: [],
    result: {
      rows: [], columns: [], notPreviewable: false, truncated: true, completeness: 'sample', wire: 'dataset',
      editorTestInput: { runId: 'purchase-run', nodeId: 'source', portId: 'out', label: 'Purchases', rows: 500 },
      editorInputSample: {
        format: 'rows', containerType: 'dict', columns: ['amount', 'note'], rowLimit: 5, columnCount: 2,
        rows: [{
          amount: { pythonType: 'decimal.Decimal', representation: "Decimal('12345678901234567890.123456789')", truncated: false },
          note: { pythonType: 'builtins.NoneType', representation: 'None', truncated: false },
        }],
      },
    },
  }
}

describe('Prepared editor input sample', () => {
  it('shows exact Python representations, source and honest input scope even when code failed', () => {
    const failed = preview()
    failed.result = { ...failed.result!, error: true, failureCategory: 'syntax_error', reason: 'Expected a colon' }
    render(<EditorInputSample doc={doc} nodeId="python" preview={failed} />)

    const input = screen.getByRole('region', { name: 'Prepared input sample' })
    expect(input).toHaveTextContent('First 1 input row · 500 retained rows in total')
    expect(input).toHaveTextContent('not a random or representative sample')
    expect(input).toHaveTextContent('Purchases · out · run purchase-run')
    expect(within(input).getByText("Decimal('12345678901234567890.123456789')")).toBeVisible()
    expect(input).toHaveTextContent('decimal.Decimal')
    expect(input).toHaveTextContent('builtins.NoneType')
    expect(input).toHaveTextContent("from decimal import Decimal")
  })

  it('keeps the prepared input visible while editing only this code', () => {
    const result = preview()
    const { rerender } = render(<EditorInputSample doc={doc} nodeId="python" preview={result} />)
    const edited = structuredClone(doc)
    edited.nodes[1].data.config.code = 'def fn(row): return {**row, "tax": 1}'
    rerender(<EditorInputSample doc={edited} nodeId="python" preview={result} />)
    expect(screen.getByRole('region', { name: 'Prepared input sample' })).toHaveTextContent('decimal.Decimal')
  })

  it.each(['upstream', 'port', 'format', 'bindings', 'canvas'])(
    'hides the old input when %s changes', (change) => {
      const result = preview()
      const { rerender } = render(<EditorInputSample doc={doc} nodeId="python" preview={result} />)
      const changed = structuredClone(doc)
      if (change === 'upstream') changed.nodes[0].data.config.uri = 'other.parquet'
      if (change === 'port') changed.edges[0].sourceHandle = 'other'
      if (change === 'format') Object.assign(changed.nodes[1].data.config, { mode: 'map_batches', batchFormat: 'pandas' })
      if (change === 'canvas') changed.id = 'other-canvas'
      rerender(<EditorInputSample doc={changed} nodeId="python" preview={result}
        parameterBindings={change === 'bindings' ? [{ name: 'input', value: 'other' }] : []} />)
      expect(screen.queryByRole('region', { name: 'Prepared input sample' })).not.toBeInTheDocument()
    },
  )

  it('hides the old input when an upstream parameter default changes', () => {
    const parameterized = structuredClone(doc)
    parameterized.nodes[0].data.config.uri = { parameterRef: 'input' }
    parameterized.parameters = [{ name: 'input', type: 'string', default: 'purchases.parquet' }]
    const result = { ...preview(), planIdentity: previewPlanIdentity(parameterized, 'python') }
    const { rerender } = render(<EditorInputSample doc={parameterized} nodeId="python" preview={result} />)
    expect(screen.getByRole('region', { name: 'Prepared input sample' })).toBeVisible()
    const changed = structuredClone(parameterized)
    changed.parameters![0].default = 'other.parquet'
    rerender(<EditorInputSample doc={changed} nodeId="python" preview={result} />)
    expect(screen.queryByRole('region', { name: 'Prepared input sample' })).not.toBeInTheDocument()
  })

  it('does not reuse an old sample during a new request or after retained input expires', () => {
    const result = preview()
    const { rerender } = render(<EditorInputSample doc={doc} nodeId="python" preview={result} />)
    rerender(<EditorInputSample doc={doc} nodeId="python"
      preview={{ ...result, requestGeneration: 2, loading: true, result: undefined }} />)
    expect(screen.queryByRole('region', { name: 'Prepared input sample' })).not.toBeInTheDocument()
    rerender(<EditorInputSample doc={doc} nodeId="python"
      preview={{ ...result, requestGeneration: 2, error: 'Input expired', result: undefined }} />)
    expect(screen.queryByRole('region', { name: 'Prepared input sample' })).not.toBeInTheDocument()
  })

  it('does not accept a result for another upstream port or parameter binding', () => {
    const wrongPort = preview()
    wrongPort.result!.editorTestInput!.portId = 'other'
    const { rerender } = render(<EditorInputSample doc={doc} nodeId="python" preview={wrongPort} />)
    expect(screen.queryByRole('region', { name: 'Prepared input sample' })).not.toBeInTheDocument()
    rerender(<EditorInputSample doc={doc} nodeId="python" preview={preview()}
      parameterBindings={[{ name: 'input', value: 'different' }]} />)
    expect(screen.queryByRole('region', { name: 'Prepared input sample' })).not.toBeInTheDocument()
  })

  it('describes empty inputs without inferring runtime types from schema', () => {
    const empty = preview()
    empty.result!.editorInputSample!.rows = []
    empty.result!.editorTestInput!.rows = 0
    render(<EditorInputSample doc={doc} nodeId="python" preview={empty} />)
    expect(screen.getByRole('region', { name: 'Prepared input sample' })).toHaveTextContent('No Python cell types were observed')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByText('decimal.Decimal')).not.toBeInTheDocument()
  })

  it.each(['pandas', 'arrow'] as const)('explains the actual %s value representation and display limits', (format) => {
    const result = preview()
    const data = result.result!.editorInputSample!
    data.format = format
    data.containerType = format === 'pandas' ? 'pandas.core.frame.DataFrame' : 'pyarrow.lib.Table'
    data.columnCount = 21
    data.rows[0].note.truncated = true
    render(<EditorInputSample doc={doc} nodeId="python" preview={result} />)
    const input = screen.getByRole('region', { name: 'Prepared input sample' })
    expect(input).toHaveTextContent(format === 'pandas' ? 'DataFrame.iat' : 'Arrow scalars')
    expect(input).toHaveTextContent('Showing 2 of 21 columns')
    expect(input).toHaveTextContent('Value display shortened')
  })
})
