import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useStore } from '../store/graph'
import type { CanvasNode, NodeStatus, NodeVersion } from '../types/graph'
import { HistoryPanel } from './HistoryPanel'

const restoreVersion = vi.fn()
const setJobsQuery = vi.fn()

function version(id: string, rows: number): NodeVersion {
  return {
    id, ts: Date.now() - rows * 1000, rows,
    label: `run · ${rows} rows`, config: { predicate: `id = ${rows}` },
  }
}

function setNode(
  status: NodeStatus,
  history: NodeVersion[] = [],
  currentOutputVersionId?: string,
) {
  const node: CanvasNode = {
    id: 'target', type: 'filter', position: { x: 0, y: 0 },
    data: {
      title: 'Target', status,
      config: status === 'latest' && history.length > 0
        ? { ...history[history.length - 1].config }
        : {},
      history,
      currentOutputVersionId,
    },
  }
  useStore.setState({
    canvasRole: 'owner',
    doc: {
      id: 'canvas-1', name: 'History test', version: 1,
      nodes: [node], edges: [], requirements: [],
    },
    restoreVersion,
    setJobsQuery,
  } as any)
}

describe('HistoryPanel output versions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setNode('draft')
  })

  it('identifies the current output without a no-op Restore or duplicate row count', () => {
    setNode('latest', [version('old', 10), version('current', 20)], 'current')

    render(<HistoryPanel nodeId="target" />)

    expect(screen.getByText('Current output')).toBeVisible()
    expect(screen.getAllByText(/20 rows/)).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: /Restore/ })).toHaveLength(1)
    expect(screen.queryByText('run · 20 rows')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Restore/ }))
    expect(restoreVersion).toHaveBeenCalledWith('target', 'old')
  })

  it('keeps the last successful output restorable when the node is now failed', () => {
    setNode('failed', [version('successful', 10)])

    render(<HistoryPanel nodeId="target" />)

    expect(screen.queryByText('Current output')).not.toBeInTheDocument()
    expect(screen.getByText('Previous output')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /Restore/ }))
    expect(restoreVersion).toHaveBeenCalledWith('target', 'successful')
  })

  it('marks a restored older configuration as current instead of assuming the newest run', () => {
    const old = version('old', 10)
    const newest = version('newest', 20)
    setNode('latest', [old, newest], 'old')
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: state.doc.nodes.map((node) => ({
          ...node, data: {
            ...node.data,
            config: { ...old.config },
            currentOutputVersionId: old.id,
          },
        })),
      },
    }))

    render(<HistoryPanel nodeId="target" />)

    expect(within(screen.getByLabelText('Current output version')).getByText(/10 rows/)).toBeVisible()
    const restorable = screen.getByLabelText('Output version')
    expect(within(restorable).getByText(/20 rows/)).toBeVisible()
    fireEvent.click(within(restorable).getByRole('button', { name: /Restore/ }))
    expect(restoreVersion).toHaveBeenCalledWith('target', 'newest')
  })

  it('uses persisted output identity when successive versions have identical configs', () => {
    const older = version('older', 10)
    const newer = { ...version('newer', 20), config: { ...older.config } }
    setNode('latest', [older, newer], 'older')

    render(<HistoryPanel nodeId="target" />)

    expect(within(screen.getByLabelText('Current output version')).getByText(/10 rows/)).toBeVisible()
    const restorable = screen.getByLabelText('Output version')
    expect(within(restorable).getByText(/20 rows/)).toBeVisible()
  })

  it.each(['stale', 'failed'] as const)(
    'does not label a retained identity Current while the node is %s',
    (status) => {
      const successful = version('successful', 10)
      setNode(status, [successful], successful.id)

      render(<HistoryPanel nodeId="target" />)

      expect(screen.queryByText('Current output')).not.toBeInTheDocument()
      expect(screen.getByText('Previous output')).toBeVisible()
    },
  )

  it('does not guess a Current version when persisted identity is absent', () => {
    const successful = version('successful', 10)
    setNode('latest', [successful])

    render(<HistoryPanel nodeId="target" />)

    expect(screen.queryByText('Current output')).not.toBeInTheDocument()
    expect(screen.getByText('Previous output')).toBeVisible()
  })

  it('sends an empty failed node to its filtered Jobs attempts', () => {
    setNode('failed')

    render(<HistoryPanel nodeId="target" />)

    expect(screen.getByText('No successful output yet.')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'View in Jobs' }))
    expect(setJobsQuery).toHaveBeenCalledWith('canvas=canvas-1&node=target&status=failed')
  })

  it('does not imply that an untouched draft has a failed run', () => {
    render(<HistoryPanel nodeId="target" />)

    expect(screen.getByText('No successful output yet.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'View in Jobs' })).not.toBeInTheDocument()
  })
})
