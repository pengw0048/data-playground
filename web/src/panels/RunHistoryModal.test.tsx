import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { fmtMs, DurationTrend, PerNodeBreakdown } from './RunHistoryModal'
import type { PerNodeStat, RunRecordDto } from '../api/client'

describe('fmtMs — human-readable durations', () => {
  it('scales ms → s → m across thresholds', () => {
    expect(fmtMs(0)).toBe('0 ms')
    expect(fmtMs(950)).toBe('950 ms')
    expect(fmtMs(1500)).toBe('1.5 s')
    expect(fmtMs(42_000)).toBe('42 s')
    expect(fmtMs(125_000)).toBe('2m 5s')
  })
})

describe('DurationTrend — a native SVG bar per run', () => {
  const runs: RunRecordDto[] = [
    { id: 'r2', status: 'failed', ms: 200 },
    { id: 'r1', status: 'done', ms: 100 },
  ]
  it('renders one rect per run and reports the max duration', () => {
    const { container } = render(<DurationTrend runs={runs} />)
    expect(container.querySelectorAll('rect')).toHaveLength(2)
    expect(screen.getByText('max 200 ms')).toBeInTheDocument()
    expect(screen.getByText('Run duration · last 2')).toBeInTheDocument()
  })
})

describe('PerNodeBreakdown — per-node horizontal bars', () => {
  const nodes: PerNodeStat[] = [
    { node_id: 'src', label: 'source', status: 'done', ms: 10, rows: 5 },
    { node_id: 'wr', label: 'write', status: 'done', ms: 90, rows: 5 },
  ]
  it('lists every node with its duration', () => {
    render(<PerNodeBreakdown nodes={nodes} />)
    expect(screen.getByText('source')).toBeInTheDocument()
    expect(screen.getByText('write')).toBeInTheDocument()
    expect(screen.getByText('Time per node')).toBeInTheDocument()
    expect(screen.getByText('90 ms')).toBeInTheDocument()
  })
})
