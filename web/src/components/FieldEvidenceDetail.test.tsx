import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import type { ColumnSchema } from '../types/graph'

const mocks = vi.hoisted(() => ({ table: vi.fn(), tableByRegistration: vi.fn() }))
vi.mock('../api/client', () => ({ api: mocks }))

import { FieldEvidenceButton } from './FieldEvidenceDetail'

const CUSTOMER: ColumnSchema = {
  fieldId: 'orders.customer_id', name: 'customer_id', type: 'int64', physicalType: 'INT64',
  nullable: false, hasDefault: null, provenance: 'provider', capabilities: [],
  annotations: [{ key: 'source.note', value: 'owned by the orders provider', encoding: 'utf8', provenance: 'provider' }],
  rowReference: {
    target: { kind: 'exact', datasetId: 'customers-logical', revisionId: 'customer-r7' },
    keyFields: ['id'], semanticType: 'customer', provenance: 'provider',
  },
}

describe('FieldEvidenceButton', () => {
  beforeEach(() => { vi.clearAllMocks() })
  afterEach(() => cleanup())

  it('keeps an exact row-reference identity while opening the resolved current catalog entry', async () => {
    mocks.table.mockResolvedValue({ id: 'stale-collision', registrationId: 'stale-collision', name: 'Wrong current dataset', uri: 'mem://wrong', columns: [] })
    mocks.tableByRegistration.mockResolvedValue({ id: 'customers-registration', registrationId: 'customers registration/1', name: 'Customers (renamed)', uri: 'mem://customers', columns: [] })
    render(<FieldEvidenceButton column={CUSTOMER} />)

    fireEvent.click(screen.getByRole('button', { name: 'Inspect evidence for customer_id' }))
    expect(await screen.findByTestId('field-evidence-customer_id')).toHaveTextContent('dataset:customers-logical · revision:customer-r7')
    expect(screen.getByText('owned by the orders provider')).toBeVisible()
    expect(screen.getByText('utf8')).toBeVisible()
    await waitFor(() => expect(screen.getByText('Customers (renamed)')).toBeVisible())
    expect(screen.getByRole('link', { name: 'Open current catalog entry' })).toHaveAttribute('href', '#/workspace/dataset%3Acustomers%20registration%2F1')
    expect(mocks.tableByRegistration).toHaveBeenCalledWith('customers-logical')
    expect(mocks.table).not.toHaveBeenCalled()
  })

  it('reports an unavailable target without replacing it with a current dataset', async () => {
    mocks.tableByRegistration.mockRejectedValue({ status: 410, message: 'compacted' })
    render(<FieldEvidenceButton column={CUSTOMER} />)

    fireEvent.click(screen.getByRole('button', { name: 'Inspect evidence for customer_id' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Target catalog identity is unavailable; no current dataset was substituted.')
    expect(screen.getByTestId('field-evidence-customer_id')).toHaveTextContent('dataset:customers-logical · revision:customer-r7')
    expect(screen.queryByRole('link', { name: 'Open current catalog entry' })).toBeNull()
  })

  it('makes absent facts explicit without claiming redacted adapter values exist', async () => {
    const absent: ColumnSchema = { name: 'legacy_row_id', type: 'int', capabilities: [], provenance: 'inferred' }
    render(<FieldEvidenceButton column={absent} />)

    fireEvent.click(screen.getByRole('button', { name: 'Inspect evidence for legacy_row_id' }))
    const detail = await screen.findByTestId('field-evidence-legacy_row_id')
    expect(detail).toHaveTextContent('not supplied')
    expect(detail).toHaveTextContent('No row-reference target was supplied.')
    expect(detail).toHaveTextContent('No safe raw annotations were supplied. Values excluded by the adapter redaction contract are not exposed here.')
    expect(mocks.tableByRegistration).not.toHaveBeenCalled()
  })
})
