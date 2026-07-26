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
import { useStore } from '../../store/graph'
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
    const { version: _version, ...executionDoc } = doc
    const fingerprint = JSON.stringify({
      ...executionDoc,
      nodes: doc.nodes.map((node) => {
        const { status: _status, ...data } = node.data
        return { ...node, data }
      }),
      parameterBindings: [],
    })
    useStore.setState({
      canvasRole: 'owner', kernelUp: true, doc,
      runs: { write: { phase: 'idle', writeAdmissionFingerprint: fingerprint, writeAdmission: {
        nodeId: 'write', managed: true, destination: '/outputs/existing.lance',
        mode: 'append', provider: 'managed-local-lance', expectedSchema: [], partitions: [],
        expectedHead: { kind: 'exact', datasetId: 'dataset-lance', revisionId: '7' },
      } } },
    } as any)
  })

  it('labels only the admitted append as exact-head and keeps Lance overwrite provider-neutral', () => {
    const Write = getComponent('write')!
    const data = useStore.getState().doc.nodes[0].data
    render(<TooltipProvider><ReactFlowProvider><Write id="write" data={data} /></ReactFlowProvider></TooltipProvider>)

    expect(screen.getByRole('option', { name: 'append (exact head)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'overwrite' })).toBeInTheDocument()
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

  it('re-admits after terminal cleanup without reusing the completed submission or polling', async () => {
    const doc = useStore.getState().doc
    const data = doc.nodes[0].data
    const { version: _version, ...executionDoc } = doc
    const fingerprint = JSON.stringify({
      ...executionDoc,
      nodes: doc.nodes.map((node) => {
        const { status: _status, ...nodeData } = node.data
        return { ...node, data: nodeData }
      }),
      parameterBindings: [],
    })
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
          status: { outputs: [{ writeReceipt: { revisionId: 'committed-7', datasetId: 'dataset-1' } }] },
        } },
      } as any)
      await Promise.resolve()
    })

    expect(screen.getByText(/revision committed-7/)).toBeInTheDocument()
    await waitFor(() => expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(1))
    expect(useStore.getState().runs.write.writeSubmissionId).not.toBe('completed-submission')
    await Promise.resolve()
    expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(1)
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
