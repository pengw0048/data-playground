import { expect, test } from '@playwright/test'
import { createHash } from 'node:crypto'

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
  const sourceCode = "def fn(row):\n    row['score'] = 1.0\n    return row"
  const promoted = await request.post('/api/processors/promote', {
    headers,
    data: {
      id: `e2e.robot-scorer-${suffix}`,
      title,
      blurb: 'Scores one exact robot observation schema.',
      category: 'robotics',
      mode: 'map',
      code: sourceCode,
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
  const implementation = page.getByRole('region', { name: 'Implementation source' })
  await expect(implementation).toContainText("row['score'] = 1.0")
  await implementation.getByText('Source integrity').click()
  await expect(implementation).toContainText(
    `SHA-256 ${createHash('sha256').update(sourceCode).digest('hex')}`,
  )
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

  const canvasUrl = page.url()
  await node.getByText('View definition', { exact: true }).click()
  await expect(page).toHaveURL(canvasUrl)
  const canvasDefinition = page.getByRole('region', { name: 'Library processor definition' })
  await expect(canvasDefinition).toContainText(sourceCode)
  await expect(canvasDefinition.getByText(`${transform.id}@${transform.version}`)).not.toBeVisible()
  await canvasDefinition.getByText('Technical details').click()
  await expect(canvasDefinition.getByText(`${transform.id}@${transform.version}`)).toBeVisible()
  await page.getByRole('button', { name: 'Close' }).click()
  await expect(page).toHaveURL(canvasUrl)

  const canvasResponse = await request.get(`/api/canvas/${encodeURIComponent(canvasId)}`, { headers })
  expect(canvasResponse.ok()).toBe(true)
  const canvas = await canvasResponse.json() as { nodes: Array<{ id: string }> }
  expect(canvas.nodes).toHaveLength(1)

  expect((await request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`, { headers })).ok()).toBe(true)
  expect((await request.delete(
    `/api/processors/${encodeURIComponent(transform.id)}/versions/${encodeURIComponent(transform.version)}`,
    { headers },
  )).ok()).toBe(true)
})
