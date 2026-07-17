import { render, screen } from '@testing-library/react'
import { ReactFlowProvider } from '@xyflow/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/client', () => ({ api: new Proxy({}, { get: () => async () => ({}) }) }))

import './write'
import { getComponent } from '../registry'
import { useStore } from '../../store/graph'

describe('Write card — typed local mode truth', () => {
  beforeEach(() => {
    const doc = {
      id: 'c', version: 1, name: 'write', requirements: [], edges: [],
      nodes: [{
        id: 'write', type: 'write', position: { x: 0, y: 0 },
        data: { title: 'write', status: 'draft', config: {
          filename: 'existing.lance', writeMode: 'append',
        } },
      }],
    }
    const fingerprint = JSON.stringify({
      ...doc,
      nodes: doc.nodes.map((node) => {
        const { status: _status, ...data } = node.data
        return { ...node, data }
      }),
    })
    useStore.setState({
      canvasRole: 'owner', doc,
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
    render(<ReactFlowProvider><Write id="write" data={data} /></ReactFlowProvider>)

    expect(screen.getByRole('option', { name: 'append (exact head)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'overwrite' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /create \/ replace/ })).not.toBeInTheDocument()
  })
})
