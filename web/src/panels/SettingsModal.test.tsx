import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

const PACK = {
  name: 'dp_x', source: 'drop-in', version: '0.1.0',
  config: [
    { key: 'url', type: 'string', label: 'URL' },
    { key: 'tok', type: 'password', secret: true, label: 'Token' },
  ],
  config_values: { url: 'existing' },   // non-secret current value (secret never sent)
  config_set: ['url'],
}
const getSettings = vi.fn()
const plugins = vi.fn()
const putSettingsBatch = vi.fn()
const listCreds = vi.fn()
const createCred = vi.fn()
const updateCred = vi.fn()
const deleteCred = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    getSettings: () => getSettings(),
    plugins: () => plugins(),
    putSettingsBatch: (...a: unknown[]) => putSettingsBatch(...a),
    listCreds: () => listCreds(),
    createCred: (...a: unknown[]) => createCred(...a),
    updateCred: (...a: unknown[]) => updateCred(...a),
    deleteCred: (...a: unknown[]) => deleteCred(...a),
    createUser: async () => ({}),
    restartKernel: async () => ({}),
  },
}))

const state = {
  kernelInfo: { runners: ['local-out-of-core'], backends: [] },
  users: [], currentUser: { id: 'u1', name: 'me', capabilities: ['global_settings'] }, authEnabled: false,
  refreshUsers: vi.fn(), pushToast: vi.fn(), doc: { id: 'canvas' },
}
vi.mock('../store/graph', () => ({ useStore: (sel: (s: unknown) => unknown) => sel(state) }))

import { SettingsModal } from './SettingsModal'

describe('SettingsModal — plugin config form', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getSettings.mockReset().mockResolvedValue({
      global: { 'plugin.dp_x.url': 'existing' }, user: {}, revision: { global: 2, user: 4 },
    })
    plugins.mockReset().mockResolvedValue([PACK])
    putSettingsBatch.mockReset().mockResolvedValue({ ok: true, revision: { global: 3, user: 5 } })
    listCreds.mockReset().mockResolvedValue([])
    createCred.mockReset().mockImplementation(async (b) => ({ id: 'new-cred', ...b }))
    updateCred.mockReset().mockImplementation(async (id, b) => ({ id, ...b }))
    deleteCred.mockReset().mockResolvedValue({ ok: true })
    state.currentUser.capabilities = ['global_settings']
  })

  it('renders declared fields, saves them as plugin.<pack>.<key>, and skips a blank secret', async () => {
    render(<SettingsModal onClose={vi.fn()} />)

    // Plugins is its own pane now (master-detail) — switch to it before editing its fields
    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))
    // the url field is pre-filled from config_values; the secret token prompts for a reference
    const url = await screen.findByDisplayValue('existing')
    const tok = screen.getByPlaceholderText(/env:VAR or file:\/path/i)

    fireEvent.change(url, { target: { value: 'new-url' } })
    fireEvent.change(tok, { target: { value: 'env:DP_X_TOK' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(putSettingsBatch).toHaveBeenCalledWith(
      { global: 2, user: 4 },
      [
        { scope: 'global', key: 'plugin.dp_x.url', value: 'new-url' },
        { scope: 'global', key: 'plugin.dp_x.tok', value: 'env:DP_X_TOK' },
      ],
    ))

    // clearing the secret must NOT write a blank (that would wipe the stored reference) — it's skipped
    putSettingsBatch.mockClear()
    fireEvent.change(tok, { target: { value: '' } })
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(putSettingsBatch).not.toHaveBeenCalled()
  })

  it('opens clean and sends only fields the user changed', async () => {
    render(<SettingsModal onClose={vi.fn()} />)
    const model = await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()

    fireEvent.change(model, { target: { value: 'openai/gpt-5' } })
    expect(await screen.findByText('1 unsaved change')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(putSettingsBatch).toHaveBeenCalledWith(
      { global: 2, user: 4 },
      [{ scope: 'global', key: 'agentModel', value: 'openai/gpt-5' }],
    ))
    expect(await screen.findByText('Saved')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('surfaces a save failure instead of a false "Saved" (UX-01)', async () => {
    putSettingsBatch.mockRejectedValueOnce(new Error('save failed'))
    render(<SettingsModal onClose={vi.fn()} />)
    const model = await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    fireEvent.change(model, { target: { value: 'edited-model' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(state.pushToast).toHaveBeenCalledWith('Settings were not saved: save failed', 'error'))
    expect(screen.getByRole('alert')).toHaveTextContent('The save was not confirmed. Settings are never partially committed; your edits remain here.')
    expect(screen.getByDisplayValue('edited-model')).toBeVisible()
    expect(screen.queryByText('Saved')).toBeNull()  // no false success
  })

  it.each([
    'HTTP 401: authentication required',
    'HTTP 403: admin only',
    'HTTP 500: database unavailable',
    'network unavailable',
  ])('blocks editing on a settings load failure and retries (%s)', async (reason) => {
    getSettings.mockRejectedValueOnce(new Error(reason))
    render(<SettingsModal onClose={vi.fn()} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Settings could not be loaded')
    expect(alert).toHaveTextContent(reason)
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.queryByPlaceholderText('anthropic/claude-opus-4-8')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Retry loading' }))
    expect(await screen.findByPlaceholderText('anthropic/claude-opus-4-8')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('treats a plugin metadata failure as a blocking load failure for admins', async () => {
    plugins.mockRejectedValueOnce(new Error('HTTP 500: plugin registry unavailable'))
    render(<SettingsModal onClose={vi.fn()} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Plugins request failed: HTTP 500: plugin registry unavailable')
    expect(screen.queryByText('No plugins loaded.')).toBeNull()
  })

  it('hides admin-only controls and saves only the user runner for a non-admin', async () => {
    state.currentUser.capabilities = []
    render(<SettingsModal onClose={vi.fn()} />)

    expect(await screen.findByText('Workspace-wide settings are managed by an administrator. You can still change your runner preference.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Agent' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Destinations' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Plugins' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Members' })).toBeNull()
    expect(screen.queryByPlaceholderText('anthropic/claude-opus-4-8')).toBeNull()
    expect(screen.queryByPlaceholderText('access key id')).toBeNull()
    expect(screen.queryByPlaceholderText('Name')).toBeNull()
    expect(plugins).not.toHaveBeenCalled()

    fireEvent.click(screen.getByLabelText('Runner'))
    fireEvent.click(await screen.findByRole('option', { name: 'local-out-of-core' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(putSettingsBatch).toHaveBeenCalledWith(
      { global: 2, user: 4 },
      [{ scope: 'user', key: 'backend', value: 'local-out-of-core' }],
    ))
  })

  it('keeps every edit when the atomic save fails and retries without claiming success', async () => {
    putSettingsBatch.mockRejectedValueOnce(new Error('HTTP 500: write failed'))
    render(<SettingsModal onClose={vi.fn()} />)
    const model = await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    const baseUrl = screen.getByPlaceholderText('http://localhost:11434 (optional)')
    fireEvent.change(model, { target: { value: 'edited-model' } })
    fireEvent.change(baseUrl, { target: { value: 'http://edited.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Settings were not saved: HTTP 500: write failed')
    expect(alert).toHaveTextContent('The save was not confirmed. Settings are never partially committed; your edits remain here.')
    expect(screen.getByDisplayValue('edited-model')).toBeVisible()
    expect(screen.getByDisplayValue('http://edited.example')).toBeVisible()
    expect(screen.queryByText('Saved')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Retry save' }))
    expect(await screen.findByText('Saved')).toBeVisible()
  })

  it('advances only the revision for the scope confirmed by each save', async () => {
    putSettingsBatch
      .mockResolvedValueOnce({ ok: true, revision: { global: 3, user: 99 } })
      .mockResolvedValueOnce({ ok: true, revision: { global: 3, user: 5 } })
    render(<SettingsModal onClose={vi.fn()} />)

    fireEvent.change(await screen.findByPlaceholderText('anthropic/claude-opus-4-8'), {
      target: { value: 'edited-model' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(putSettingsBatch).toHaveBeenNthCalledWith(
      1,
      { global: 2, user: 4 },
      [{ scope: 'global', key: 'agentModel', value: 'edited-model' }],
    ))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled())

    fireEvent.click(screen.getByRole('button', { name: 'Execution' }))
    fireEvent.click(screen.getByLabelText('Runner'))
    fireEvent.click(await screen.findByRole('option', { name: 'local-out-of-core' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(putSettingsBatch).toHaveBeenNthCalledWith(
      2,
      { global: 3, user: 4 },
      [{ scope: 'user', key: 'backend', value: 'local-out-of-core' }],
    ))
  })

  it('commits mixed global and user edits in one batch', async () => {
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.change(await screen.findByPlaceholderText('anthropic/claude-opus-4-8'), {
      target: { value: 'edited-model' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Execution' }))
    fireEvent.click(screen.getByLabelText('Runner'))
    fireEvent.click(await screen.findByRole('option', { name: 'local-out-of-core' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(putSettingsBatch).toHaveBeenCalledWith(
      { global: 2, user: 4 },
      [
        { scope: 'global', key: 'agentModel', value: 'edited-model' },
        { scope: 'user', key: 'backend', value: 'local-out-of-core' },
      ],
    ))
  })

  it('creates an object-store credential from the Credentials pane (references only)', async () => {
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Credentials' }))

    fireEvent.change(screen.getByLabelText('Credential name'), { target: { value: 'Prod S3' } })
    fireEvent.change(screen.getByLabelText('accessKeyId'), { target: { value: 'env:AWS_ACCESS_KEY_ID' } })
    fireEvent.change(screen.getByLabelText('region'), { target: { value: 'us-east-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add credential' }))

    // kind defaults to object_store; blank secretAccessKey/endpoint are omitted (never a raw/blank secret)
    await waitFor(() => expect(createCred).toHaveBeenCalledWith({
      name: 'Prod S3', kind: 'object_store',
      fields: { accessKeyId: 'env:AWS_ACCESS_KEY_ID', region: 'us-east-1' },
    }))
    expect(await screen.findByText('Prod S3')).toBeVisible()  // the created cred lands in the list
  })

  it('edits and deletes a credential', async () => {
    listCreds.mockResolvedValue([{ id: 'c1', name: 'Old', kind: 'agent', fields: { apiKey: 'env:K' } }])
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Credentials' }))

    fireEvent.click(await screen.findByRole('button', { name: 'Edit credential Old' }))
    const nameInput = screen.getByLabelText('Credential name') as HTMLInputElement
    expect(nameInput.value).toBe('Old')  // form loaded from the cred
    fireEvent.change(nameInput, { target: { value: 'New' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save credential' }))
    await waitFor(() => expect(updateCred).toHaveBeenCalledWith('c1', { name: 'New', kind: 'agent', fields: { apiKey: 'env:K' } }))

    fireEvent.click(await screen.findByRole('button', { name: 'Remove credential New' }))
    await waitFor(() => expect(deleteCred).toHaveBeenCalledWith('c1'))
  })

  it('saves the selected agent + default object-store credential references', async () => {
    getSettings.mockResolvedValue({
      global: { agentCredId: 'a1', defaultObjectStoreCredId: 'o1' },
      user: {}, revision: { global: 2, user: 4 },
    })
    listCreds.mockResolvedValue([
      { id: 'a1', name: 'Agent key', kind: 'agent', fields: {} },
      { id: 'o1', name: 'Store', kind: 'object_store', fields: {} },
    ])
    render(<SettingsModal onClose={vi.fn()} />)
    await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    fireEvent.click(screen.getByLabelText('Agent credential'))
    fireEvent.click(await screen.findByRole('option', { name: /None/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    // creds are referenced by id in settings; the raw agentApiKey/objectStore keys are gone
    await waitFor(() => expect(putSettingsBatch).toHaveBeenCalledWith(
      { global: 2, user: 4 },
      [{ scope: 'global', key: 'agentCredId', value: '' }],
    ))
  })

  it('tags an object-store destination with a credential and shows it', async () => {
    getSettings.mockResolvedValue({
      global: { destinations: [{ id: 'd1', name: 'Exports', backend: 's3', root: 's3://b/p', credId: 'c1' }] },
      user: {},
      revision: { global: 2, user: 4 },
    })
    listCreds.mockResolvedValue([{ id: 'c1', name: 'Prod S3', kind: 'object_store', fields: {} }])
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Destinations' }))

    // the destination row shows its bound credential's name
    expect(await screen.findByText('Exports')).toBeVisible()
    expect(screen.getByText('Prod S3')).toBeVisible()
    // a local add-form has no cred picker; it appears only for object-store backends
    expect(screen.queryByLabelText('Destination credential')).toBeNull()

    // switching the new-destination backend to s3 reveals the object-store credential picker
    fireEvent.click(screen.getByLabelText('Destination backend'))
    fireEvent.click(await screen.findByRole('option', { name: 's3' }))
    expect(await screen.findByLabelText('Destination credential')).toBeVisible()
    expect(screen.getByText(/Restart the Data Playground server after adding this destination/i)).toBeVisible()
    expect(screen.getByText(/restarting only the canvas kernel is not enough/i)).toBeVisible()
  })
})
