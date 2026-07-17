import { expect, test } from '@playwright/test'

test('the installed descriptor fixture survives registration, editing, reload, and preview', async ({ page, request }) => {
  const descriptorsResponse = await request.get('/api/nodes')
  expect(descriptorsResponse.ok()).toBe(true)
  const descriptors = await descriptorsResponse.json() as Array<Record<string, any>>
  const contract = descriptors.find((item) => item.kind === 'descriptor_contract')
  const unavailable = descriptors.find((item) => item.kind === 'descriptor_contract_unavailable')
  expect(contract).toMatchObject({
    inputs: [{ id: 'items', multi: true }],
    params: [
      { name: 'columns', type: 'columns' },
      { name: 'count', type: 'int' },
      { name: 'ratio', type: 'float' },
    ],
    previewable: true,
    requires: { cpu: 1 },
  })
  expect(unavailable).toMatchObject({
    previewable: false,
    requires: { gpu: 1, labels: { engine: 'descriptor-contract' } },
  })

  const catalogResponse = await request.get('/api/catalog/tables?q=events')
  expect(catalogResponse.ok()).toBe(true)
  const catalog = await catalogResponse.json() as { items: Array<{ name: string; uri: string }> }
  const events = catalog.items.find((item) => item.name === 'events')
  expect(events).toBeTruthy()

  const canvasId = `plugin-contract-${Date.now()}`
  const node = (id: string, type: string, config: Record<string, unknown>, x: number, y: number) => ({
    id, type, position: { x, y }, data: { title: id, status: 'draft', config },
  })
  const edge = (source: string, target: string, targetHandle: string) => ({
    id: `${source}-${target}`, source, target, sourceHandle: null, targetHandle, data: { wire: 'dataset' },
  })
  const graph = {
    id: canvasId, name: 'installed descriptor browser contract', version: 1,
    nodes: [
      node('first', 'source', { uri: events!.uri }, 0, 0),
      node('second', 'source', { uri: events!.uri }, 0, 300),
      node('contract', 'descriptor_contract', {
        columns: ['event', 'amount'], count: 7, ratio: 1.25,
      }, 350, 100),
      node('unavailable', 'descriptor_contract_unavailable', {}, 700, 100),
    ],
    edges: [
      edge('first', 'contract', 'items'),
      edge('second', 'contract', 'items'),
      edge('contract', 'unavailable', 'in'),
    ],
  }
  const saved = await request.put(`/api/canvas/${canvasId}`, { data: graph })
  expect(saved.ok()).toBe(true)

  await page.goto(`/#/canvas/${canvasId}`)
  await expect(page.locator('.react-flow__node')).toHaveCount(4)
  await expect(page.locator('.react-flow__edge')).toHaveCount(3)

  const contractNode = page.locator('.react-flow__node').filter({ hasText: 'contract' }).filter({
    has: page.getByText('Selected columns', { exact: true }),
  })
  await expect(contractNode).toBeVisible()
  await contractNode.click()
  const [count, ratio] = await contractNode.getByRole('textbox').all()
  await count.fill('12abc')
  await expect(contractNode.getByRole('alert')).toContainText('complete safe integer')
  await expect(contractNode.getByRole('button', { name: /complete safe integer/i }).first())
    .toHaveAttribute('aria-disabled', 'true')
  await expect(page.getByTestId('autosave')).toHaveText(/saved/i)
  await count.fill('9')
  await count.blur()
  await ratio.fill('Infinity')
  await expect(contractNode.getByRole('alert')).toContainText('finite number')
  await ratio.fill('2.5')
  await ratio.blur()
  await contractNode.getByRole('button', { name: 'Move amount up' }).click()
  await expect.poll(async () => {
    const restored = await request.get(`/api/canvas/${canvasId}`)
    if (!restored.ok()) return null
    const body = await restored.json() as typeof graph
    return body.nodes.find((item) => item.id === 'contract')?.data.config
  }).toEqual({ columns: ['amount', 'event'], count: 9, ratio: 2.5 })

  await page.reload()
  const restoredContract = page.locator('.react-flow__node').filter({ hasText: 'contract' }).filter({
    has: page.getByText('Selected columns', { exact: true }),
  })
  await expect(restoredContract.getByLabel('Column 1')).toHaveValue('amount')
  await expect(restoredContract.getByLabel('Column 2')).toHaveValue('event')
  const [restoredCount, restoredRatio] = await restoredContract.getByRole('textbox').all()
  await expect(restoredCount).toHaveValue('9')
  await expect(restoredRatio).toHaveValue('2.5')

  await restoredContract.click()
  await restoredContract.getByRole('button', { name: 'View data' }).click()
  await expect(page.getByText('configured_count', { exact: true })).toBeVisible()

  const unavailableNode = page.locator('.react-flow__node').filter({ hasText: 'unavailable' })
  await unavailableNode.click()
  await unavailableNode.getByRole('button', { name: 'View data' }).click()
  await expect(page.getByText('Not sample-previewable', { exact: true })).toBeVisible()
})
