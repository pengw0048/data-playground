import { act, render, screen, waitFor } from '@testing-library/react'
import { ReactFlowProvider } from '@xyflow/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'

const apiMocks = vi.hoisted(() => ({ writeAdmission: vi.fn() }))

vi.mock('../../api/client', () => ({
  KernelError: class KernelError extends Error {
    status: number
    code?: string
    field?: string
    reason?: string
    constructor(status: number, message: string, code?: string, _retryable?: boolean, field?: string, reason?: string) {
      super(message); this.status = status; this.code = code; this.field = field; this.reason = reason
    }
  },
  managedDatasetNameErrorMessage: (error: any) => (
    error?.code === 'invalid_managed_dataset_name'
      && error?.field === 'filename'
      && error?.reason === 'path_syntax'
      ? 'Use one managed dataset name, without a path or URI.'
      : null
  ),
  api: new Proxy({}, { get: (_target, property) => property === 'writeAdmission'
    ? apiMocks.writeAdmission : async () => ({}) }),
}))

import './write'
import { getComponent } from '../registry'
import { useStore, writeAdmissionFingerprint } from '../../store/graph'
import { KernelError } from '../../api/client'

describe('Write card — typed local mode truth', () => {
  beforeEach(() => {
    apiMocks.writeAdmission.mockReset()
    const doc = {
      id: 'c', version: 1, name: 'write', requirements: [], edges: [],
      nodes: [{
        id: 'write', type: 'write', position: { x: 0, y: 0 },
        data: { title: 'write', status: 'draft', config: {
          filename: 'existing.lance', writeMode: 'append',
        } },
      }],
    }
    const fingerprint = writeAdmissionFingerprint(doc, 'write')
    useStore.setState({
      canvasRole: 'owner', kernelUp: true, doc,
      runs: { write: { phase: 'idle', writeAdmissionFingerprint: fingerprint, writeAdmission: {
        nodeId: 'write', managed: true, destination: '/outputs/existing.lance',
        mode: 'append', provider: 'managed-local-lance', expectedSchema: [], partitions: [],
        expectedHead: { kind: 'exact', datasetId: 'dataset-lance', revisionId: '7' },
      } } },
    } as any)
  })

  it('keeps the admitted append label and uses direct language for a provider-neutral overwrite', () => {
    const Write = getComponent('write')!
    const data = useStore.getState().doc.nodes[0].data
    render(<TooltipProvider><ReactFlowProvider><Write id="write" data={data} /></ReactFlowProvider></TooltipProvider>)

    expect(screen.getByRole('option', { name: 'append (exact head)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'replace output' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /create \/ replace/ })).not.toBeInTheDocument()
  })

  it.each(['estimating', 'confirm', 'drift', 'running'] as const)(
    'does not mint a competing admission while run intent is %s', async (phase) => {
      const Write = getComponent('write')!
      const data = useStore.getState().doc.nodes[0].data
      render(<TooltipProvider><ReactFlowProvider><Write id="write" data={data} /></ReactFlowProvider></TooltipProvider>)

      expect(apiMocks.writeAdmission).not.toHaveBeenCalled()
      await act(async () => {
        useStore.setState({
          runs: { write: {
            phase, writeAdmission: undefined, writeSubmissionId: undefined,
            writeAdmissionFingerprint: undefined,
          } },
        } as any)
        await Promise.resolve()
      })

      expect(apiMocks.writeAdmission).not.toHaveBeenCalled()
      expect(useStore.getState().runs.write.writeSubmissionId).toBeUndefined()
    },
  )

  it('shares identity-keyed requests across a raw edge replacement and undo', async () => {
    const sourceA = {
      id: 'source-a', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'source-a', status: 'draft', config: {} },
    }
    const sourceB = {
      id: 'source-b', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'source-b', status: 'draft', config: {} },
    }
    const transform = {
      id: 'transform', type: 'filter', position: { x: 0, y: 0 },
      data: { title: 'transform', status: 'draft', config: {} },
    }
    const write = useStore.getState().doc.nodes[0]
    const doc = {
      id: 'c', version: 1, name: 'write', requirements: [],
      nodes: [sourceA, sourceB, transform, write],
      edges: [
        { id: 'source-transform', source: 'source-a', target: 'transform' },
        { id: 'transform-write', source: 'transform', target: 'write' },
      ],
    }
    const admissionA = {
      nodeId: 'write', managed: true, destination: '/outputs/existing.lance',
      mode: 'append' as const, provider: 'managed-local-lance', expectedSchema: [], partitions: [],
    }
    const admissionB = { ...admissionA, expectedSchema: [{ name: 'replacement', type: 'string' }] }
    let resolveA!: (value: typeof admissionA) => void
    let resolveB!: (value: typeof admissionB) => void
    apiMocks.writeAdmission
      .mockImplementationOnce(() => new Promise<typeof admissionA>((resolve) => { resolveA = resolve }))
      .mockImplementationOnce(() => new Promise<typeof admissionB>((resolve) => { resolveB = resolve }))
    useStore.setState({ doc, runs: {}, past: [], future: [] } as any)
    useStore.getState().commit()
    const Write = getComponent('write')!

    render(<TooltipProvider><ReactFlowProvider><Write id="write" data={write.data} /></ReactFlowProvider></TooltipProvider>)
    await waitFor(() => expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(1))

    await act(async () => {
      useStore.getState().setEdges([
        { id: 'replacement-edge-id', source: 'source-b', target: 'transform' },
        { id: 'transform-write', source: 'transform', target: 'write' },
      ])
    })
    await waitFor(() => expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(2))
    await act(async () => { useStore.getState().undo() })
    expect(useStore.getState().doc.edges[0]).toMatchObject({ source: 'source-a', target: 'transform' })
    await Promise.resolve()
    expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(2)

    await act(async () => {
      resolveA(admissionA)
      resolveB(admissionB)
    })

    await waitFor(() => expect(useStore.getState().runs.write.writeAdmission).toEqual(admissionA))
  })

  it('hides a cached admission as soon as its semantic identity changes', async () => {
    const sourceA = {
      id: 'source-a', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'source-a', status: 'draft', config: {} },
    }
    const sourceB = {
      id: 'source-b', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'source-b', status: 'draft', config: {} },
    }
    const write = useStore.getState().doc.nodes[0]
    const doc = {
      id: 'c', version: 1, name: 'write', requirements: [], nodes: [sourceA, sourceB, write],
      edges: [{ id: 'source-write', source: 'source-a', target: 'write' }],
    }
    const stale = {
      nodeId: 'write', managed: true, destination: '/outputs/existing.lance',
      mode: 'append' as const, provider: 'managed-local-lance', expectedSchema: [], partitions: [],
      blocker: 'Old source is unavailable.',
    }
    const fresh = { ...stale, blocker: undefined, expectedSchema: [{ name: 'value', type: 'string' }] }
    let resolveFresh!: (value: typeof fresh) => void
    apiMocks.writeAdmission.mockImplementationOnce(
      () => new Promise<typeof fresh>((resolve) => { resolveFresh = resolve }),
    )
    useStore.setState({
      doc,
      runs: { write: {
        phase: 'idle', writeAdmission: stale,
        writeAdmissionFingerprint: writeAdmissionFingerprint(doc, 'write'),
        writeSubmissionId: 'stale-submission',
      } },
    } as any)
    const Write = getComponent('write')!
    render(<TooltipProvider><ReactFlowProvider><Write id="write" data={write.data} /></ReactFlowProvider></TooltipProvider>)
    expect(screen.getByText(/needs attention/)).toBeInTheDocument()

    await act(async () => {
      useStore.getState().setEdges([{ id: 'replacement', source: 'source-b', target: 'write' }])
    })
    await waitFor(() => expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(1))
    expect(screen.queryByText(/needs attention/)).not.toBeInTheDocument()
    expect(screen.getByText(/checking output/)).toBeInTheDocument()

    await act(async () => { resolveFresh(fresh) })

    await waitFor(() => expect(useStore.getState().runs.write.writeAdmission).toEqual(fresh))
  })

  it('re-admits after terminal cleanup without reusing the completed submission or polling', async () => {
    const doc = useStore.getState().doc
    const data = doc.nodes[0].data
    const fingerprint = writeAdmissionFingerprint(doc, 'write')
    useStore.setState({
      runs: { write: {
        phase: 'running', writeSubmissionId: 'completed-submission',
        writeAdmissionFingerprint: fingerprint,
        writeAdmission: {
          nodeId: 'write', managed: false, destination: '/outputs/existing.lance',
          mode: 'append', provider: 'duckdb', expectedSchema: [], partitions: [],
        },
      } },
    } as any)
    apiMocks.writeAdmission.mockResolvedValue({
      nodeId: 'write', managed: false, destination: '/outputs/existing.lance',
      mode: 'append', provider: 'duckdb', expectedSchema: [], partitions: [],
    })

    const Write = getComponent('write')!
    render(<TooltipProvider><ReactFlowProvider><Write id="write" data={data} /></ReactFlowProvider></TooltipProvider>)

    expect(apiMocks.writeAdmission).not.toHaveBeenCalled()
    await act(async () => {
      useStore.setState({
        runs: { write: {
          phase: 'done', writeAdmission: undefined, writeSubmissionId: undefined,
          writeAdmissionFingerprint: undefined,
          status: { outputs: [{ writeReceipt: {
            revisionId: 'committed-7', datasetId: 'dataset-1', name: 'committed output',
          } }] },
        } },
      } as any)
      await Promise.resolve()
    })

    expect(screen.getByText(/Published · committed output/)).toBeInTheDocument()
    expect(screen.queryByText(/committed-7/)).not.toBeInTheDocument()
    await waitFor(() => expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(1))
    expect(useStore.getState().runs.write.writeSubmissionId).not.toBe('completed-submission')
    await Promise.resolve()
    expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(1)
  })

  it('keeps a recovered publication visible while preparing an independent next admission', async () => {
    const doc = useStore.getState().doc
    const data = doc.nodes[0].data
    useStore.setState({
      runs: { write: {
        phase: 'idle',
        writeOutcome: {
          runId: 'published-run',
          receipt: { revisionId: 'recovered-8', datasetId: 'dataset-1', name: 'recovered output' },
          outputs: [],
        },
      } },
    } as any)
    apiMocks.writeAdmission.mockResolvedValueOnce({
      nodeId: 'write', managed: true, destination: '/outputs/existing.lance',
      mode: 'replace', provider: 'managed-local-lance', expectedSchema: [], partitions: [],
    })

    const Write = getComponent('write')!
    render(<TooltipProvider><ReactFlowProvider><Write id="write" data={data} /></ReactFlowProvider></TooltipProvider>)

    expect(screen.getByText(/Published · recovered output/)).toBeInTheDocument()
    expect(screen.queryByText(/recovered-8/)).not.toBeInTheDocument()
    await waitFor(() => expect(useStore.getState().runs.write.writeAdmission).toMatchObject({
      mode: 'replace',
    }))
    expect(screen.getByText(/Published · recovered output/)).toBeInTheDocument()
    expect(screen.queryByText(/recovered-8/)).not.toBeInTheDocument()
    expect(useStore.getState().runs.write.writeOutcome?.runId).toBe('published-run')
  })

  it('shows the typed managed-name reason beside the filename field', async () => {
    useStore.setState({
      runs: { write: { phase: 'idle' } },
      doc: {
        ...useStore.getState().doc,
        nodes: [{
          ...useStore.getState().doc.nodes[0],
          data: {
            ...useStore.getState().doc.nodes[0].data,
            config: { filename: '../escape.parquet', writeMode: 'overwrite' },
          },
        }],
      },
    } as any)
    apiMocks.writeAdmission.mockRejectedValueOnce(new KernelError(
      422,
      'server prose is not the client contract',
      'invalid_managed_dataset_name',
      false,
      'filename',
      'path_syntax',
    ))
    const Write = getComponent('write')!
    const data = useStore.getState().doc.nodes[0].data

    render(<TooltipProvider><ReactFlowProvider><Write id="write" data={data} /></ReactFlowProvider></TooltipProvider>)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Use one managed dataset name, without a path or URI.')
    expect(screen.getByPlaceholderText('output')).toHaveAttribute('aria-invalid', 'true')
  })
})
