import { expect, test, type Page } from '@playwright/test'

async function replacePython(page: Page, code: string): Promise<void> {
  const input = page.locator('.monaco-editor').first().getByRole('textbox', { name: 'Editor content' })
  await input.focus()
  const mac = await page.evaluate(() => navigator.userAgent.includes('Macintosh'))
  await input.press(mac ? 'Meta+a' : 'Control+a')
  await page.keyboard.insertText(code)
}

test('stops a Python preview and tests corrected code without losing the editor input', async ({ page }, testInfo) => {
  test.setTimeout(60_000)
  const canvasId = `preview-stop-${Date.now()}`
  const initialCode = "def fn(row):\n    return {'answer': row['value'] * 2}"
  const loopingCode = 'def fn(row):\n    while True:\n        pass'
  const correctedCode = "def fn(row):\n    return {'answer': row['value'] * 3}"
  const exampleRows = '[{"value":7}]'
  const graph = {
    id: canvasId, name: 'Stop and recover Python preview', version: 1, requirements: [],
    nodes: [{
      id: 'python', type: 'transform', position: { x: 260, y: 180 },
      data: { title: 'Python preview', status: 'draft', config: {
        source: 'adhoc', mode: 'map', code: initialCode, onError: 'raise',
      } },
    }],
    edges: [],
  }
  const created = await page.request.post('/api/canvas', { data: graph })
  expect(created.ok(), await created.text()).toBeTruthy()
  try {
    await page.goto(`/#/canvas/${encodeURIComponent(canvasId)}?node=python`)
    await page.getByRole('button', { name: 'Edit code', exact: true }).last().click()
    await page.getByRole('button', { name: 'Example rows', exact: true }).click()
    const fixture = page.getByRole('textbox', { name: 'Example rows JSON' })
    await fixture.fill(exampleRows)
    const testCode = page.getByRole('button', { name: 'Test code', exact: true }).last()
    await testCode.click()
    await expect(page.getByRole('status', { name: 'Test result: 1 output row from Example rows' })).toBeVisible()
    await expect(page.getByText('14', { exact: true }).first()).toBeVisible()

    await replacePython(page, loopingCode)
    const runningRequest = page.waitForRequest((request) => request.url().endsWith('/api/run/editor-preview/examples')
      && request.postDataJSON()?.graph?.nodes?.some((node: { data?: { config?: { code?: string } } }) => node.data?.config?.code?.includes('while True:')))
    await testCode.click()
    await runningRequest
    await page.getByRole('button', { name: 'Stop preview', exact: true }).click()
    await expect(page.getByText('Preview stopped', { exact: true })).toBeVisible()
    await expect(testCode).toBeEnabled()
    await expect(fixture).toHaveValue(exampleRows)
    await expect(page.locator('.monaco-editor').first()).toContainText('while True')
    await page.getByText('Previous successful preview', { exact: true }).click()
    await expect(page.getByText('They are not a result of the current attempt.', { exact: false })).toBeVisible()
    await expect(page.getByText('14', { exact: true }).first()).toBeVisible()
    await testInfo.attach('stopped-python-preview', {
      body: await page.screenshot(), contentType: 'image/png',
    })

    await replacePython(page, correctedCode)
    await testCode.click()
    await expect(page.getByRole('status', { name: 'Test result: 1 output row from Example rows' })).toBeVisible()
    await expect(page.getByText('21', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('Preview stopped', { exact: true })).toHaveCount(0)
    await expect(fixture).toHaveValue(exampleRows)
    await expect.poll(async () => {
      const saved = await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}`)).json()
      return saved.nodes.find((node: { id: string }) => node.id === 'python').data.config.code
        .split('\n').map((line: string) => line.trim()).join('\n')
    }).toBe(correctedCode.split('\n').map((line) => line.trim()).join('\n'))
    expect(await (await page.request.get(`/api/canvas/${encodeURIComponent(canvasId)}/runs`)).json()).toEqual([])
    await testInfo.attach('corrected-python-preview', {
      body: await page.screenshot(), contentType: 'image/png',
    })

    await page.reload()
    await page.getByRole('button', { name: 'Edit code', exact: true }).last().click()
    await expect(page.locator('.monaco-editor').first()).toContainText("row['value'] * 3")
    await expect(page.locator('.react-flow__node')).toHaveCount(1)
  } finally {
    if (!page.isClosed()) await page.goto('about:blank')
    expect((await page.request.delete(`/api/canvas/${encodeURIComponent(canvasId)}`)).ok()).toBeTruthy()
  }
})
