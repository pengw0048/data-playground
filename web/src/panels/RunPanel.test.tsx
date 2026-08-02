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
  hasConfiguredManagedSidecarMerge: () => false,
  roleCanEdit: (role: string) => role === 'owner' || role === 'editor',
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
      sizes: {},
      estimate: vi.fn(), requestRun: vi.fn(), run: vi.fn(), cancelRun: vi.fn(), refreshPreviewInputs: vi.fn(),
      previewBindings: {}, canvasRole: 'owner', setRunParameterBinding: mocks.setBinding,
      clearRunParameterBinding: mocks.clearBinding, submitRunParameters: mocks.submit,
      editRunParameters: mocks.edit, setJobsQuery: mocks.setJobsQuery,
    }
  })

  it('blocks invalid values, clears bindings explicitly, and keeps DatasetRef fields structural', () => {
    render(<RunPanel nodeId="target" />)
    expect(screen.getByText(/explicit timezone/i)).toBeVisible()
    expect(screen.getByText(/provide the dataset and version IDs/i)).toBeVisible()
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

  it('explains a large unknown binary pass without exposing opaque source IDs', () => {
    const datasetId = 'workspace-provider-opaque-dataset-should-not-appear'
    const revisionId = 'provider-revision-opaque-value-should-not-appear'
    mocks.state.doc.nodes = [{
      id: 'source', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'Source', status: 'ready', config: { datasetRef: { kind: 'exact', datasetId, revisionId } } },
    }, {
      id: 'target', type: 'filter', position: { x: 200, y: 0 },
      data: { title: 'Target', status: 'draft', config: {} },
    }]
    mocks.state.doc.edges = [{ id: 'source-target', source: 'source', target: 'target' }]
    mocks.state.runs.target = {
      phase: 'confirm',
      estimate: {
        rows: 2_001,
        bytes: null,
        placement: 'local',
        needsConfirm: true,
        confirmationReasons: ['unknown_byte_size'],
        breakdown: 'size unknown · 2,001 rows · confirmation required: Binary column "payload" has no fixed-width byte-size evidence; Data Playground did not scan values to guess.',
      },
    }
    render(<RunPanel nodeId="target" />)

    expect(screen.queryByText('HEADS UP')).not.toBeInTheDocument()
    expect(screen.getByText('CONFIRM RUN')).toBeVisible()
    expect(screen.getAllByText('Run 2,001 rows')).toHaveLength(2)
    expect(screen.getByText(/This full run will process 2,001 rows/)).toBeVisible()
    expect(screen.getByText(/Data size is not available, and this input is above the small-run limit/)).toBeVisible()
    expect(screen.queryByText(datasetId)).not.toBeInTheDocument()
    expect(screen.queryByText(revisionId)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run 2,001 rows' })).toBeVisible()

    expect(screen.queryByText('Diagnostics')).not.toBeInTheDocument()
    expect(screen.queryByText(/Binary column/)).not.toBeInTheDocument()
  })

  it.each([
    { rows: 200_000, bytes: 3 * 1024 ** 3, label: 'byte threshold' },
    { rows: 6_000_000, bytes: 20 * 1024 ** 2, label: 'row threshold' },
  ])('keeps both known row and byte evidence for a $label confirmation', ({ rows, bytes }) => {
    mocks.state.runs.target = {
      phase: 'confirm',
      estimate: {
        rows,
        bytes,
        placement: 'local',
        needsConfirm: true,
        confirmationReasons: [bytes === 3 * 1024 ** 3 ? 'large_bytes' : 'large_rows'],
        breakdown: 'confirmation required',
      },
    }

    render(<RunPanel nodeId="target" />)

    expect(screen.getByText(new RegExp(`This full run will process ${rows.toLocaleString()} rows`))).toBeVisible()
    expect(screen.getByText(new RegExp(bytes === 3 * 1024 ** 3 ? '3 GiB' : '20 MiB'))).toBeVisible()
    expect(screen.getByText(bytes === 3 * 1024 ** 3
      ? /estimated data volume is large/i : /row count is above the automatic-run limit/i)).toBeVisible()
    expect(screen.queryByText(/heads up/i)).not.toBeInTheDocument()
  })

  it('uses a current exact graph count for visible confirmation copy without rewriting admission evidence', () => {
    mocks.state.sizes = { target: { rows: 2_000, confidence: 'exact' } }
    mocks.state.runs.target = {
      phase: 'confirm',
      estimate: {
        rows: null,
        bytes: null,
        placement: 'local',
        needsConfirm: true,
        confirmationReasons: ['unknown_population'],
        breakdown: 'size unknown · retained input was not reopened during admission',
      },
    }

    render(<RunPanel nodeId="target" />)

    expect(screen.getAllByText('Run 2,000 rows')).toHaveLength(2)
    expect(screen.getByText(/This full run will process 2,000 rows/)).toBeVisible()
    expect(screen.queryByText('Diagnostics')).not.toBeInTheDocument()
    expect(screen.queryByText('size unknown · retained input was not reopened during admission')).not.toBeInTheDocument()
  })

  it.each(['bounded', 'unknown'])(
    'does not turn a %s graph-size hint into a visible exact row claim',
    (confidence) => {
      mocks.state.sizes = { target: { rows: 2_000, confidence } }
      mocks.state.runs.target = {
        phase: 'confirm',
        estimate: {
          rows: null,
          bytes: null,
          placement: 'local',
          needsConfirm: true,
          confirmationReasons: ['unknown_population'],
        },
      }

      render(<RunPanel nodeId="target" />)

      expect(screen.getAllByText('Run with unknown row count')).toHaveLength(2)
      expect(screen.getByText(/unknown number of rows/)).toBeVisible()
    },
  )

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

    expect(screen.getByText('COLUMN MERGE')).toBeVisible()
    expect(screen.getByLabelText('Column merge setup')).toBeVisible()
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

  it('frames an ordinary Write as a direct output task before and during execution', () => {
    mocks.state.doc.nodes = [{
      id: 'target', type: 'write', position: { x: 0, y: 0 },
      data: { title: 'Write', status: 'draft', config: { filename: 'results' } },
    }]
    mocks.state.doc.parameters = []
    const admission = {
      nodeId: 'target', mode: 'create', provider: 'managed-local-file',
      destination: '/outputs/results.parquet', managed: true, expectedSchema: [], partitions: [],
      intent: { destination: { name: 'results' } },
    }
    mocks.state.runs = { target: {
      phase: 'estimated', estimate: { rows: 2, placement: 'local', needsConfirm: false },
      writeAdmission: admission, status: { outputs: [] },
    } }
    const { rerender } = render(<RunPanel nodeId="target" />)

    expect(screen.getByRole('button', { name: 'Publish output' })).toBeVisible()
    expect(screen.getByLabelText('Write publication')).toHaveTextContent('Ready to run')

    mocks.state.runs.target = {
      phase: 'running', writeAdmission: admission,
      status: {
        runId: 'write-job', status: 'running', jobType: 'run', targetNodeId: 'target',
        rowsProcessed: 1, totalRows: 2, ms: 10, placement: 'local', perNode: [], outputs: [],
      },
    }
    rerender(<RunPanel nodeId="target" />)
    expect(screen.getByText('publishing dataset')).toBeVisible()
    expect(screen.getByLabelText('Write publication')).toHaveTextContent('Writing output…')
    expect(screen.queryByLabelText('Run outputs')).not.toBeInTheDocument()
  })

  it('shows a registered-input prerequisite instead of offering publication', () => {
    mocks.state.doc.nodes = [{
      id: 'target', type: 'write', position: { x: 0, y: 0 },
      data: { title: 'Write', status: 'draft', config: { filename: 'results' } },
    }]
    mocks.state.doc.parameters = []
    mocks.state.runs = { target: {
      phase: 'estimated', estimate: { rows: 2, placement: 'local', needsConfirm: false },
      writeAdmission: {
        nodeId: 'target', mode: 'create', provider: 'managed-local-file',
        destination: '/outputs/results.parquet', managed: true, expectedSchema: [], partitions: [],
        blocker: 'Register this local input before publishing an exact managed revision.',
        exactRunReadiness: {
          ready: false, reason: 'registration_required', sourceNodeIds: ['source'],
          message: 'Register this local input before publishing an exact managed revision.',
        },
      },
    } }

    render(<RunPanel nodeId="target" />)

    expect(screen.queryByRole('button', { name: 'Publish output' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Register inputs to publish' })).toBeDisabled()
    expect(screen.getByLabelText('Run readiness')).toHaveTextContent(
      'Fix before running: Register this local input',
    )
  })

  it('blocks a formal non-Write run until the estimate reports an exact input registration', () => {
    mocks.state.doc.nodes = [{
      id: 'target', type: 'filter', position: { x: 0, y: 0 },
      data: { title: 'Filter', status: 'draft', config: {} },
    }]
    mocks.state.doc.parameters = []
    mocks.state.runs = { target: {
      phase: 'estimated', estimate: {
        rows: 2, placement: 'local', needsConfirm: false,
        exactRunReadiness: {
          ready: false, reason: 'registration_required', sourceNodeIds: ['source'],
          message: 'Register this local input before running.',
        },
      },
    } }

    render(<RunPanel nodeId="target" />)

    expect(screen.getByRole('button', { name: 'Register inputs to run' })).toBeDisabled()
    expect(screen.getByLabelText('Run readiness')).toHaveTextContent(
      'Not ready to run: Register this local input before running.',
    )
  })

  it('shows exact schema drift before the confirmation click', () => {
    mocks.state.doc.nodes = [{
      id: 'target', type: 'write', position: { x: 0, y: 0 },
      data: { title: 'Write', status: 'draft', config: { filename: 'results' } },
    }]
    mocks.state.doc.parameters = []
    mocks.state.runs = { target: {
      phase: 'confirm', estimate: { rows: 2, placement: 'local', needsConfirm: false },
      writeAdmission: {
        nodeId: 'target', mode: 'replace', provider: 'managed-local-file',
        destination: '/outputs/results.parquet', managed: true, expectedSchema: [], partitions: [],
        intent: {
          destination: { name: 'results' },
          schemaDrift: {
            comparedHead: {
              kind: 'exact', datasetId: 'dataset-1', revisionId: 'revision-1',
            },
            compatibility: { status: 'compatible', fields: [{
              kind: 'added', status: 'compatible', newName: 'extra',
              reason: 'nullable field was added',
            }] },
            requiresConfirmation: true,
          },
        },
      },
    } }
    render(<RunPanel nodeId="target" />)

    expect(screen.getByLabelText('Schema changes')).toHaveTextContent('extra: nullable field was added')
    expect(screen.getByLabelText('Schema changes')).not.toHaveTextContent('dataset-1@revision-1')
    expect(screen.getByLabelText('Write readiness')).toHaveTextContent(
      'Review schema changes before running.')
    fireEvent.click(screen.getByRole('button', { name: 'Publish output' }))
    expect(mocks.state.run).toHaveBeenCalledWith('target', true)
  })

  it('keeps provider-neutral Write execution in the ordinary Run and output model', () => {
    mocks.state.doc.nodes = [{
      id: 'target', type: 'write', position: { x: 0, y: 0 },
      data: { title: 'Write', status: 'draft', config: { filename: 'results.parquet' } },
    }]
    mocks.state.doc.parameters = []
    const admission = {
      nodeId: 'target', mode: 'overwrite', provider: 'plugin-sink',
      destination: 's3://example/results.parquet', managed: false, expectedSchema: [], partitions: [],
    }
    mocks.state.runs = { target: {
      phase: 'estimated', estimate: { rows: 2, placement: 'ray', needsConfirm: false },
      writeAdmission: admission, status: { outputs: [] },
    } }
    const { rerender } = render(<RunPanel nodeId="target" />)

    expect(screen.getByRole('button', { name: 'Run' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Publish output' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Write publication')).toHaveTextContent('Ready to run')

    mocks.state.runs.target = {
      phase: 'running', writeAdmission: admission,
      status: {
        runId: 'provider-job', status: 'running', jobType: 'run', targetNodeId: 'target',
        rowsProcessed: 1, totalRows: 2, ms: 10, placement: 'ray', perNode: [], outputs: [],
      },
    }
    rerender(<RunPanel nodeId="target" />)
    expect(screen.getByText('running')).toBeVisible()
    expect(screen.queryByText('publishing dataset')).not.toBeInTheDocument()

    mocks.state.runs.target = {
      phase: 'done', writeOutcomeAdmission: admission,
      status: {
        runId: 'provider-job', status: 'done', jobType: 'run', targetNodeId: 'target',
        rowsProcessed: 2, totalRows: 2, ms: 10, placement: 'ray', perNode: [], outputs: [],
      },
    }
    rerender(<RunPanel nodeId="target" />)
    expect(screen.getByText('DONE')).toBeVisible()
    expect(screen.queryByText('DATASET PUBLISHED')).not.toBeInTheDocument()
  })

  it('uses the same receipt-backed publication hierarchy after an ordinary Write succeeds', () => {
    mocks.state.doc.nodes = [{
      id: 'target', type: 'write', position: { x: 0, y: 0 },
      data: { title: 'Output', status: 'draft', config: { filename: 'results.parquet' } },
    }]
    mocks.state.doc.parameters = []
    mocks.state.runs = { target: {
      phase: 'done', writeOutcomeAdmission: {
        nodeId: 'target',
        mode: 'append', provider: 'managed-local-file', destination: '/outputs/results.parquet',
        managed: true, expectedSchema: [], partitions: [],
      }, status: {
        runId: 'write-job', status: 'done', jobType: 'run', targetNodeId: 'target', rowsProcessed: 2,
        totalRows: 2, ms: 10, placement: 'local', perNode: [], outputs: [{
          nodeId: 'target', portId: 'out', wire: 'dataset', publicationKind: 'catalog', outcome: 'committed',
          uri: 'managed://dataset-1', table: 'results', version: 'revision-9', rows: 2,
          writeReceipt: { datasetId: 'dataset-1', revisionId: 'revision-9', name: 'results', rows: 2, bytes: 128,
            durable: true, head: { datasetId: 'dataset-1', revisionId: 'revision-9', committedAt: '2026-07-21T12:00:00Z', retentionOwner: 'core' },
            schema: [{ name: 'id', type: 'bigint' }], partitions: [], publication: {
              provider: 'managed-local-file', logicalUri: 'managed://dataset-1', artifactUri: 'file:///revision-9.parquet',
              publishSequence: 9, idempotencyKey: 'write-9', catalogVersion: 'catalog-9', backendVersion: '8.0.0',
            }, executionManifestSha256: 'a'.repeat(64) },
        }],
      },
    } }
    render(<RunPanel nodeId="target" />)
    const publication = screen.getByLabelText('Write publication')
    expect(publication).toHaveTextContent('Append to the selected dataset')
    expect(publication).toHaveTextContent('Published · results · 2 rows')
    expect(screen.getByLabelText('Published result')).not.toHaveTextContent('revision-9')
    expect(screen.getByText('DATASET PUBLISHED')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Open dataset' })).toBeVisible()
    expect(screen.queryByLabelText('Run outputs')).not.toBeInTheDocument()
    expect(screen.queryByText('Diagnostics')).not.toBeInTheDocument()
    expect(publication).not.toHaveTextContent('file:///revision-9.parquet')
    expect(publication).not.toHaveTextContent('catalog-9')
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

  it('keeps a failed run readable without offering a false Retry action to viewers', () => {
    mocks.state.canvasRole = 'viewer'
    mocks.state.runs.target = { phase: 'failed', error: 'Transform failed' }

    render(<RunPanel nodeId="target" />)

    expect(screen.getByText('Transform failed')).toBeVisible()
    const retry = screen.getByRole('button', { name: 'Retry' })
    expect(retry).toBeDisabled()
    expect(retry).toHaveAttribute('title', 'View-only canvas')
    fireEvent.click(retry)
    expect(mocks.state.estimate).not.toHaveBeenCalled()
    expect(mocks.state.requestRun).not.toHaveBeenCalled()
    expect(mocks.state.run).not.toHaveBeenCalled()
  })

  it('makes Retry repeat an ordinary failed run instead of stopping at a new estimate', () => {
    mocks.state.runs.target = { phase: 'failed', error: 'Transform failed' }

    render(<RunPanel nodeId="target" />)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(mocks.state.requestRun).toHaveBeenCalledWith('target')
    expect(mocks.state.estimate).not.toHaveBeenCalled()
    expect(mocks.state.run).not.toHaveBeenCalled()
  })
})
