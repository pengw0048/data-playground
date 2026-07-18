import { expect, test } from '@playwright/test'

test('deep-links an exact Transform and atomically creates its target Canvas', async ({ page, request }) => {
  const createdUser = await request.post('/api/users', {
    data: { name: `Transform library ${Date.now()}` },
    headers: { 'X-DP-User': 'local' },
  })
  expect(createdUser.ok()).toBe(true)
  const userId = (await createdUser.json() as { id: string }).id
  const headers = { 'X-DP-User': userId }
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2)}`
  const title = `Robot scorer ${suffix}`
  const promoted = await request.post('/api/processors/promote', {
    headers,
    data: {
      id: `e2e.robot-scorer-${suffix}`,
      title,
      blurb: 'Scores one exact robot observation schema.',
      category: 'robotics',
      mode: 'map',
      code: "def fn(row):\n    row['score'] = 1.0\n    return row",
      inputColumns: ['observation'],
      inputSchema: [{ name: 'observation', type: 'string' }],
      outputSchema: [{ name: 'score', type: 'float64' }],
      requirements: [],
    },
  })
  expect(promoted.ok()).toBe(true)
  const transform = await promoted.json() as { id: string; version: string }
  await page.addInitScript((id) => localStorage.setItem('dp-user', id), userId)

  await page.goto(`/#/transforms/${encodeURIComponent(transform.id)}?version=${transform.version}`)
  await expect(page.getByRole('heading', { name: title })).toBeVisible()
  await expect(page.getByRole('button', { name: `Use exact ${transform.version}` })).toBeEnabled()
  expect(new URL(page.url()).hash).toContain(`version=${transform.version}`)

  await page.getByRole('button', { name: `Use exact ${transform.version}` }).click()
  await page.getByLabel('New Canvas name').fill(`Exact ${title}`)
  await page.getByRole('button', { name: 'Create and open' }).click()
  await expect(page).toHaveURL(/#\/canvas\/[^?]+\?node=[^&]+$/)
  const canvasId = decodeURIComponent(new URL(page.url()).hash.split('?')[0].replace('#/canvas/', ''))
  const node = page.locator('.react-flow__node').filter({ hasText: title })
  await expect(node).toHaveCount(1)
  await expect(node).toContainText(transform.version)

  const canvasResponse = await request.get(`/api/canvas/${encodeURIComponent(canvasId)}`, { headers })
  expect(canvasResponse.ok()).toBe(true)
  const canvas = await canvasResponse.json() as { nodes: Array<{ id: string }> }
  expect(canvas.nodes).toHaveLength(1)
  await node.getByText('Manage', { exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`#\/transforms\/${transform.id}`))
  const managed = new URLSearchParams(new URL(page.url()).hash.split('?')[1])
  expect(managed.get('version')).toBe(transform.version)
  expect(managed.get('canvas')).toBe(canvasId)
  expect(managed.get('node')).toBe(canvas.nodes[0].id)

  expect((await request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`, { headers })).ok()).toBe(true)
  expect((await request.delete(
    `/api/processors/${encodeURIComponent(transform.id)}/versions/${encodeURIComponent(transform.version)}`,
    { headers },
  )).ok()).toBe(true)
})
