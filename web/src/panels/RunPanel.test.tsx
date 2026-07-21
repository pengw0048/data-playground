import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  state: {} as any,
  setBinding: vi.fn(),
  clearBinding: vi.fn(),
  submit: vi.fn(),
  edit: vi.fn(),
  setJobsQuery: vi.fn(),
}))

vi.mock('../store/graph', () => ({
  roleCanEdit: () => true,
  targetParameterDeclarations: (doc: any) => doc.parameters ?? [],
  useStore: (selector: (state: any) => unknown) => selector(mocks.state),
}))

import { RunPanel } from './RunPanel'

describe('RunPanel typed parameter gate', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state = {
      doc: {
        id: 'canvas', version: 1, nodes: [{
          id: 'target', type: 'filter', position: { x: 0, y: 0 },
          data: { title: 'Target', status: 'draft', config: {} },
        }], edges: [], parameters: [
          { name: 'when', type: 'datetime', required: true, label: 'When' },
          { name: 'input', type: 'dataset', required: true, label: 'Input' },
        ],
      },
      runs: { target: { phase: 'parameters', parameterBindings: [
        { name: 'when', value: '2026-07-18T10:00:00' },
        { name: 'input', value: { kind: 'exact', datasetId: 'dataset-1' } },
      ] } },
      estimate: vi.fn(), run: vi.fn(), cancelRun: vi.fn(), refreshPreviewInputs: vi.fn(),
      previewBindings: {}, canvasRole: 'owner', setRunParameterBinding: mocks.setBinding,
      clearRunParameterBinding: mocks.clearBinding, submitRunParameters: mocks.submit,
      editRunParameters: mocks.edit, setJobsQuery: mocks.setJobsQuery,
    }
  })

  it('blocks invalid values, clears bindings explicitly, and keeps DatasetRef fields structural', () => {
    render(<RunPanel nodeId="target" />)
    expect(screen.getByText(/explicit timezone/i)).toBeVisible()
    expect(screen.getByText(/provide the dataset identity and revision/i)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled()

    fireEvent.change(screen.getByLabelText('When'), { target: { value: '' } })
    expect(mocks.clearBinding).toHaveBeenCalledWith('target', 'when')
    fireEvent.change(screen.getByLabelText('Input revision'), { target: { value: 'revision-1' } })
    expect(mocks.setBinding).toHaveBeenCalledWith('target', {
      name: 'input', value: { kind: 'exact', datasetId: 'dataset-1', revisionId: 'revision-1' },
    })
  })

  it('continues only after all generated controls are valid', () => {
    mocks.state.runs.target.parameterBindings = [
      { name: 'when', value: '2026-07-18T10:00:00-04:00' },
      { name: 'input', value: { kind: 'latest', datasetId: 'dataset-1' } },
    ]
    render(<RunPanel nodeId="target" />)
    const button = screen.getByRole('button', { name: 'Continue' })
    expect(button).toBeEnabled()
    fireEvent.click(button)
    expect(mocks.submit).toHaveBeenCalledWith('target')
  })

  it('shows a latest DatasetRef default until the user explicitly overrides it', () => {
    mocks.state.doc.parameters = [{
      name: 'input', type: 'dataset', label: 'Input',
      default: { kind: 'latest', datasetId: 'dataset-latest' },
    }]
    mocks.state.runs.target.parameterBindings = []
    render(<RunPanel nodeId="target" />)

    expect(screen.getByLabelText('Input selection')).toHaveValue('latest')
    expect(screen.getByLabelText('Input selection')).toBeDisabled()
    expect(screen.getByLabelText('Input dataset')).toHaveValue('dataset-latest')
    expect(screen.getByLabelText('Input dataset')).toBeDisabled()
    expect(screen.queryByLabelText('Input revision')).not.toBeInTheDocument()
    expect(screen.getByText('Using declared default.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled()

    fireEvent.click(screen.getByRole('button', { name: 'Override default' }))
    expect(mocks.setBinding).toHaveBeenCalledWith('target', {
      name: 'input', value: { kind: 'latest', datasetId: 'dataset-latest' },
    })
  })

  it('shows an exact DatasetRef default and can return an override to the default', () => {
    mocks.state.doc.parameters = [{
      name: 'input', type: 'dataset', label: 'Input',
      default: { kind: 'exact', datasetId: 'dataset-exact', revisionId: 'revision-default' },
    }]
    mocks.state.runs.target.parameterBindings = []
    const { rerender } = render(<RunPanel nodeId="target" />)

    expect(screen.getByLabelText('Input selection')).toHaveValue('exact')
    expect(screen.getByLabelText('Input selection')).toBeDisabled()
    expect(screen.getByLabelText('Input dataset')).toHaveValue('dataset-exact')
    expect(screen.getByLabelText('Input dataset')).toBeDisabled()
    expect(screen.getByLabelText('Input revision')).toHaveValue('revision-default')
    expect(screen.getByLabelText('Input revision')).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Override default' }))
    expect(mocks.setBinding).toHaveBeenCalledWith('target', {
      name: 'input', value: { kind: 'exact', datasetId: 'dataset-exact', revisionId: 'revision-default' },
    })

    mocks.state.runs.target.parameterBindings = [{
      name: 'input', value: { kind: 'exact', datasetId: 'dataset-exact', revisionId: 'revision-override' },
    }]
    rerender(<RunPanel nodeId="target" />)
    expect(screen.getByLabelText('Input revision')).toHaveValue('revision-override')
    expect(screen.getByLabelText('Input revision')).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Use default' }))
    expect(mocks.clearBinding).toHaveBeenCalledWith('target', 'input')
  })

  it('keeps a required DatasetRef without a default editable and actionable', () => {
    mocks.state.doc.parameters = [{ name: 'input', type: 'dataset', required: true, label: 'Input' }]
    mocks.state.runs.target.parameterBindings = []
    render(<RunPanel nodeId="target" />)

    expect(screen.getByLabelText('Input selection')).toBeEnabled()
    expect(screen.getByLabelText('Input dataset')).toBeEnabled()
    expect(screen.getByLabelText('Input revision')).toBeEnabled()
    expect(screen.getByRole('alert')).toHaveTextContent('no default')
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Input dataset'), { target: { value: 'dataset-1' } })
    expect(mocks.setBinding).toHaveBeenCalledWith('target', {
      name: 'input', value: { kind: 'exact', datasetId: 'dataset-1', revisionId: '' },
    })
  })

  it('distinguishes an empty string binding from use-default and only rejects built-in SecretRefs', () => {
    mocks.state.doc.parameters = [{ name: 'uri', type: 'string', required: true, label: 'URI' }]
    mocks.state.runs.target.parameterBindings = [{ name: 'uri', value: '' }]
    const { rerender } = render(<RunPanel nodeId="target" />)

    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Clear binding' })).toBeVisible()

    mocks.state.runs.target.parameterBindings = [{ name: 'uri', value: 's3://public-bucket/key' }]
    rerender(<RunPanel nodeId="target" />)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    mocks.state.runs.target.parameterBindings = [{ name: 'uri', value: 'https://example.test/data' }]
    rerender(<RunPanel nodeId="target" />)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    mocks.state.runs.target.parameterBindings = [{ name: 'uri', value: 'file:/private/token' }]
    rerender(<RunPanel nodeId="target" />)
    expect(screen.getByRole('alert')).toHaveTextContent('Secret references')
    mocks.state.runs.target.parameterBindings = [{ name: 'uri', value: 'ENV:PRIVATE_VALUE' }]
    rerender(<RunPanel nodeId="target" />)
    expect(screen.getByRole('alert')).toHaveTextContent('Secret references')
  })

  it('offers one shared Edit parameters path back to a fresh estimate', () => {
    mocks.state.runs.target = {
      phase: 'estimated', estimate: { rows: 10, placement: 'local', needsConfirm: false },
      parameterBindings: [
        { name: 'when', value: '2026-07-18T10:00:00-04:00' },
        { name: 'input', value: { kind: 'latest', datasetId: 'dataset-1' } },
      ],
    }
    render(<RunPanel nodeId="target" />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit parameters' }))
    expect(mocks.edit).toHaveBeenCalledWith('target')
  })

  it('shows configured column merges only through their certified admission control', async () => {
    mocks.state.doc.nodes = [{
      id: 'target', type: 'write', position: { x: 0, y: 0 },
      data: { title: 'Write', status: 'draft', config: { mergeColumns: {
        identityColumns: ['id'], rules: [{ source: 'score', target: 'score', mode: 'add' }],
      } } },
    }]
    mocks.state.doc.parameters = []
    mocks.state.runs = { target: { phase: 'idle' } }
    render(<RunPanel nodeId="target" />)

    expect(screen.getByText('CERTIFIED COLUMN MERGE')).toBeVisible()
    expect(screen.getByLabelText('Certified column merge')).toBeVisible()
    await waitFor(() => expect(mocks.state.estimate).not.toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: 'Run' })).not.toBeInTheDocument()
  })

  it('continues to estimate an ordinary Write with no merge rules', async () => {
    mocks.state.doc.nodes = [{
      id: 'target', type: 'write', position: { x: 0, y: 0 },
      data: { title: 'Write', status: 'draft', config: {} },
    }]
    mocks.state.doc.parameters = []
    mocks.state.runs = { target: { phase: 'idle' } }
    render(<RunPanel nodeId="target" />)
    await waitFor(() => expect(mocks.state.estimate).toHaveBeenCalledWith('target'))
  })

  it('uses the same receipt-backed publication hierarchy after an ordinary Write succeeds', () => {
    mocks.state.doc.nodes = [{
      id: 'target', type: 'write', position: { x: 0, y: 0 },
      data: { title: 'Output', status: 'draft', config: { filename: 'results.parquet' } },
    }]
    mocks.state.doc.parameters = []
    mocks.state.runs = { target: {
      phase: 'done', writeAdmission: {
        mode: 'append', provider: 'managed-local-file', destination: '/outputs/results.parquet',
        managed: true, expectedSchema: [], partitions: [],
      }, status: {
        runId: 'write-job', status: 'done', jobType: 'run', targetNodeId: 'target', rowsProcessed: 2,
        totalRows: 2, ms: 10, placement: 'local', perNode: [], outputs: [{
          nodeId: 'target', portId: 'out', outcome: 'committed', rows: 2,
          writeReceipt: { datasetId: 'dataset-1', revisionId: 'revision-9', rows: 2, bytes: 128,
            durable: true, head: { datasetId: 'dataset-1', revisionId: 'revision-9', retentionOwner: 'core' }, schema: [], partitions: [], publication: {} },
        }],
      },
    } }
    render(<RunPanel nodeId="target" />)
    const publication = screen.getByLabelText('Write publication')
    expect(publication).toHaveTextContent('Append to the selected dataset')
    expect(publication).toHaveTextContent('results.parquet published · 2 rows')
    expect(screen.getByRole('button', { name: 'Open exact revision' })).toBeVisible()
    expect(screen.queryByLabelText('Run outputs')).not.toBeInTheDocument()
  })

  it.each([
    ['queued managed Write', 'running', 'queued'],
    ['running Job', 'running', 'running'],
    ['completed Job', 'done', 'done'],
    ['failed Job', 'failed', 'failed'],
    ['cancelled Job', 'idle', 'cancelled'],
  ])('opens the exact authorized Job for a %s', (_label, phase, status) => {
    mocks.state.runs.target = {
      phase,
      status: {
        runId: `job-${phase}`, status, jobType: 'run', targetNodeId: 'target', rowsProcessed: 0,
        ms: 0, placement: 'local', perNode: [], outputs: [],
      },
    }
    render(<RunPanel nodeId="target" />)
    fireEvent.click(screen.getByRole('button', { name: 'View in Jobs' }))
    expect(mocks.setJobsQuery).toHaveBeenCalledWith(`run=job-${phase}`)
  })

  it('omits View in Jobs without a known Job identity or after an unrelated estimate failure', () => {
    mocks.state.runs.target = {
      phase: 'failed',
      status: { runId: '', status: 'failed', jobType: 'run', targetNodeId: 'target', rowsProcessed: 0, ms: 0, placement: 'local', perNode: [], outputs: [] },
    }
    const { rerender } = render(<RunPanel nodeId="target" />)
    expect(screen.queryByRole('button', { name: 'View in Jobs' })).toBeNull()

    mocks.state.runs.target = {
      phase: 'estimated',
      status: { runId: 'old-failed-job', status: 'failed', jobType: 'run', targetNodeId: 'target', rowsProcessed: 0, ms: 0, placement: 'local', perNode: [], outputs: [] },
      estimate: { rows: 1, placement: 'local', needsConfirm: false },
    }
    rerender(<RunPanel nodeId="target" />)
    expect(screen.queryByRole('button', { name: 'View in Jobs' })).toBeNull()
  })
})
