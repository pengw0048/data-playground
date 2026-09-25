import { useEffect, useState } from 'react'
import {
  parameterBindingsIdentity, previewIsCurrent, previewPlanIdentity,
  type PreviewState,
} from '../store/graph'
import type { EditorInputSample as InputSample, SampleResult } from '../types/api'
import type { CanvasDoc, CanvasParameterBinding } from '../types/graph'

interface AcceptedInput {
  identity: string
  generation: number
  sample: InputSample
  input: NonNullable<SampleResult['editorTestInput']>
}

// Editing this cell changes its output, not its prepared input. Keep every other execution
// detail (including input ports, mode, batch format and parameter bindings) in the identity.
function inputIdentity(doc: CanvasDoc, nodeId: string, portId: string | undefined,
  bindings: CanvasParameterBinding[]) {
  const inputDoc: CanvasDoc = {
    ...doc,
    nodes: doc.nodes.map((node) => node.id !== nodeId ? node : {
      ...node,
      data: { ...node.data, config: { ...node.data.config, code: '' } },
    }),
  }
  return `${previewPlanIdentity(inputDoc, nodeId, portId)}:${parameterBindingsIdentity(bindings)}`
}

export function EditorInputSample({ doc, nodeId, preview, parameterBindings = [] }: {
  doc: CanvasDoc
  nodeId: string
  preview?: PreviewState
  parameterBindings?: CanvasParameterBinding[]
}) {
  const [accepted, setAccepted] = useState<AcceptedInput>()
  const identity = inputIdentity(doc, nodeId, preview?.portId, parameterBindings)
  const incoming = doc.edges.filter((edge) => edge.target === nodeId)
  const edge = incoming.length === 1 ? incoming[0] : undefined
  const input = preview?.result?.editorTestInput
  const sample = preview?.result?.editorInputSample
  const current = Boolean(
    preview && !preview.loading && !preview.error && sample && input && edge
    && input.nodeId === edge.source
    && (!edge.sourceHandle || input.portId === edge.sourceHandle)
    && previewIsCurrent(preview, doc, nodeId)
    && parameterBindingsIdentity(preview.parameterBindings)
      === parameterBindingsIdentity(parameterBindings),
  )
  useEffect(() => {
    if (current && preview && input && sample) {
      setAccepted({ identity, generation: preview.requestGeneration, input, sample })
    }
  }, [current, identity, input, preview, sample])

  const visible = current && preview && input && sample
    ? { identity, generation: preview.requestGeneration, input, sample }
    : accepted?.identity === identity
      && accepted.generation === preview?.requestGeneration
      && !preview.loading && !preview.error && preview.result?.editorInputSample
      ? accepted : undefined
  if (!visible) return null

  const { input: source, sample: data } = visible
  const count = data.rows.length
  const decimal = data.rows.some((row) => Object.values(row)
    .some((cell) => cell.pythonType === 'decimal.Decimal'))
  return (
    <section aria-label="Prepared input sample" className="border-b border-border bg-muted/20">
      <details open>
        <summary className="cursor-pointer px-3 py-2 text-xs font-semibold text-foreground">
          Input sample <span className="font-normal text-muted-foreground">· {source.label}</span>
        </summary>
        <div className="space-y-1 px-3 pb-2 text-[10.5px] leading-relaxed text-muted-foreground">
          <p>
            {count ? `First ${count.toLocaleString()} input ${count === 1 ? 'row' : 'rows'}` : 'No input rows'}
            {source.rows != null ? ` · ${source.rows.toLocaleString()} retained rows in total` : ' · total row count unknown'}.
            {count > 0 && <>
              {' '}This display is limited to {data.rowLimit} rows; the test can use more.
              {' '}It is a prefix, not a random or representative sample.
            </>}
          </p>
          <p>
            Python input: <code className="text-foreground">{data.containerType}</code>.
            {' '}Values and types below come from the input before your code runs.
            {' '}Types describe these cells; other rows may differ.
            {data.format === 'pandas' && ' Cells are read with DataFrame.iat from the actual input batch.'}
            {data.format === 'arrow' && ' Cells are Arrow scalars from the actual input Table.'}
          </p>
          {decimal && <p className="text-foreground">
            <code>decimal.Decimal</code> keeps decimal precision. For decimal arithmetic, use
            {' '}<code>Decimal('0.1')</code> after <code>from decimal import Decimal</code>.
          </p>}
          {data.columnCount > data.columns.length && <p>
            Showing {data.columns.length} of {data.columnCount} columns.
          </p>}
          {!count && <p>No Python cell types were observed because this input is empty.</p>}
          <details>
            <summary className="cursor-pointer">Input origin</summary>
            <div className="break-all font-mono">{source.label} · {source.portId} · run {source.runId}</div>
          </details>
        </div>
        {count > 0 && <div className="max-h-52 overflow-auto border-t border-border">
          <table aria-label="Python input values and types" className="w-full border-collapse text-left text-[10.5px]">
            <thead className="sticky top-0 bg-card text-muted-foreground">
              <tr><th scope="col" className="px-3 py-1.5">Row</th>
                {data.columns.map((column) => <th key={column} scope="col" className="px-3 py-1.5 font-medium">{column}</th>)}
              </tr>
            </thead>
            <tbody>{data.rows.map((row, index) => <tr key={index} className="border-t border-border/70">
              <th scope="row" className="px-3 py-2 align-top font-normal text-muted-foreground">{index + 1}</th>
              {data.columns.map((column) => {
                const cell = row[column]
                return <td key={column} className="min-w-36 max-w-72 px-3 py-2 align-top">
                  {cell ? <>
                    <div className="whitespace-pre-wrap break-all font-mono text-foreground">{cell.representation}</div>
                    <div className="mt-0.5 font-mono text-muted-foreground">{cell.pythonType}</div>
                    {cell.truncated && <div className="text-muted-foreground">Value display shortened</div>}
                  </> : <span className="text-muted-foreground">Not observed</span>}
                </td>
              })}
            </tr>)}</tbody>
          </table>
        </div>}
      </details>
    </section>
  )
}
