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

  it('opens the resolved linked dataset without exposing internal identities', async () => {
    mocks.table.mockResolvedValue({ id: 'stale-collision', registrationId: 'stale-collision', name: 'Wrong current dataset', uri: 'mem://wrong', columns: [] })
    mocks.tableByRegistration.mockResolvedValue({ id: 'customers-registration', registrationId: 'customers registration/1', name: 'Customers (renamed)', uri: 'mem://customers', columns: [] })
    render(<FieldEvidenceButton column={CUSTOMER} />)

    fireEvent.click(screen.getByRole('button', { name: 'View details for customer_id' }))
    const detail = await screen.findByTestId('field-evidence-customer_id')
    expect(detail).not.toHaveTextContent('customers-logical')
    expect(detail).not.toHaveTextContent('customer-r7')
    expect(screen.getByText('Type')).toBeVisible()
    expect(screen.getByText('Nullable')).toBeVisible()
    expect(screen.queryByText('owned by the orders provider')).not.toBeInTheDocument()
    expect(screen.queryByText('Diagnostics')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Customers (renamed)')).toBeVisible())
    expect(screen.getByRole('link', { name: 'Open linked dataset' })).toHaveAttribute('href', '#/workspace/dataset%3Acustomers%20registration%2F1')
    expect(mocks.tableByRegistration).toHaveBeenCalledWith('customers-logical')
    expect(mocks.table).not.toHaveBeenCalled()
  })

  it('reports an unavailable target without replacing it with a current dataset', async () => {
    mocks.tableByRegistration.mockRejectedValue({ status: 410, message: 'compacted' })
    render(<FieldEvidenceButton column={CUSTOMER} />)

    fireEvent.click(screen.getByRole('button', { name: 'View details for customer_id' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The linked dataset is no longer available.')
    expect(screen.getByTestId('field-evidence-customer_id')).not.toHaveTextContent('customers-logical')
    expect(screen.queryByRole('link', { name: 'Open linked dataset' })).toBeNull()
  })

  it('omits unavailable reference and raw annotation copy from a field without evidence', async () => {
    const absent: ColumnSchema = { name: 'legacy_row_id', type: 'int', capabilities: [], provenance: 'inferred' }
    render(<FieldEvidenceButton column={absent} />)

    fireEvent.click(screen.getByRole('button', { name: 'View details for legacy_row_id' }))
    const detail = await screen.findByTestId('field-evidence-legacy_row_id')
    expect(detail).toHaveTextContent('Typeint')
    expect(detail).not.toHaveTextContent('Linked dataset')
    expect(detail).not.toHaveTextContent('Raw annotations')
    expect(detail).not.toHaveTextContent('not supplied')
    expect(mocks.tableByRegistration).not.toHaveBeenCalled()
  })
})
