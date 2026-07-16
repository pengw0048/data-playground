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
const SCHEMA_PACK = {
  name: 'dp_schema', source: 'drop-in', version: '0.1.0',
  config: [
    { key: 'enabled', type: 'bool', label: 'Enabled', default: true },
    { key: 'count', type: 'int', label: 'Count', default: 1 },
    { key: 'ratio', type: 'float', label: 'Ratio', default: 0.5 },
    { key: 'label', type: 'string', label: 'Label', default: 'default label' },
    { key: 'mode', type: 'select', label: 'Mode', default: 'fast', options: ['fast', 'balanced'] },
  ],
}
const SEMANTIC_CATALOG_PACK = {
  name: 'dp-semantic-catalog', source: 'drop-in', version: '0.1.0',
  config: [{ key: 'enabled', type: 'bool', label: 'Enable semantic search', default: true }],
}
const getSettings = vi.fn()
const plugins = vi.fn()
const putSettingsBatch = vi.fn()
const listCreds = vi.fn()
const createCred = vi.fn()
const updateCred = vi.fn()
const deleteCred = vi.fn()
const createUser = vi.fn()
const restartKernel = vi.fn()
vi.mock('../api/client', () => ({
  KernelError: class KernelError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    getSettings: () => getSettings(),
    plugins: () => plugins(),
    putSettingsBatch: (...a: unknown[]) => putSettingsBatch(...a),
    listCreds: () => listCreds(),
    createCred: (...a: unknown[]) => createCred(...a),
    updateCred: (...a: unknown[]) => updateCred(...a),
    deleteCred: (...a: unknown[]) => deleteCred(...a),
    createUser: (...a: unknown[]) => createUser(...a),
    restartKernel: (...a: unknown[]) => restartKernel(...a),
  },
}))

const state = {
  kernelInfo: { runners: ['local-out-of-core'], backends: [] },
  users: [], currentUser: { id: 'u1', name: 'me', capabilities: ['global_settings'] }, authEnabled: false,
  refreshUsers: vi.fn(), pushToast: vi.fn(), doc: { id: 'canvas' },
}
vi.mock('../store/graph', () => ({ useStore: (sel: (s: unknown) => unknown) => sel(state) }))

import { KernelError } from '../api/client'
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
    createUser.mockReset().mockResolvedValue({})
    restartKernel.mockReset().mockResolvedValue({ ok: true, restarted: true })
    state.kernelInfo = { runners: ['local-out-of-core'], backends: [] }
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

  it('leaves an existing stored secret untouched when a blank editor is saved with another change', async () => {
    getSettings.mockResolvedValue({
      global: { 'plugin.dp_x.url': 'existing', 'plugin.dp_x.tok': 'env:DP_X_TOKEN' },
      user: {}, revision: { global: 2, user: 4 },
    })
    render(<SettingsModal onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))
    fireEvent.change(screen.getByLabelText('Token'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('URL'), { target: { value: 'new-url' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(putSettingsBatch).toHaveBeenCalledWith(
      { global: 2, user: 4 },
      [{ scope: 'global', key: 'plugin.dp_x.url', value: 'new-url' }],
    ))
  })

  it('clears one stored plugin secret immediately while preserving unrelated staged Settings', async () => {
    let resolveClear: ((value: unknown) => void) | undefined
    getSettings.mockResolvedValue({
      global: { 'plugin.dp_x.url': 'existing', 'plugin.dp_x.tok': 'env:DP_X_TOKEN' },
      user: {}, revision: { global: 2, user: 4 },
    })
    putSettingsBatch.mockReturnValueOnce(new Promise((resolve) => { resolveClear = resolve }))
    render(<SettingsModal onClose={vi.fn()} />)

    const model = await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    fireEvent.change(model, { target: { value: 'staged-model' } })
    fireEvent.click(screen.getByRole('button', { name: 'Plugins' }))
    fireEvent.change(screen.getByLabelText('URL'), { target: { value: 'staged-url' } })
    fireEvent.click(screen.getByRole('button', { name: 'Clear…' }))

    const confirmation = screen.getByRole('heading', { name: 'Clear stored plugin secret reference?' }).closest('[role="dialog"]')
    expect(confirmation).toHaveTextContent('It does not save or discard other staged Settings.')
    expect(confirmation).not.toHaveTextContent('env:DP_X_TOKEN')
    fireEvent.click(screen.getByRole('button', { name: 'Clear stored reference' }))

    expect(putSettingsBatch).toHaveBeenCalledWith(
      { global: 2, user: 4 },
      [{ scope: 'global', key: 'plugin.dp_x.tok', value: '' }],
    )
    expect(screen.getByRole('button', { name: 'Clearing…' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Clearing…' }))
    expect(putSettingsBatch).toHaveBeenCalledOnce()

    resolveClear?.({ ok: true, revision: { global: 3, user: 4 } })
    expect(await screen.findByText(/Token now uses its environment\/default value/)).toBeVisible()
    expect(screen.getByLabelText('Token')).toHaveValue('')
    expect(screen.getByLabelText('URL')).toHaveValue('staged-url')
    fireEvent.click(screen.getByRole('button', { name: 'Agent' }))
    expect(screen.getByPlaceholderText('anthropic/claude-opus-4-8')).toHaveValue('staged-model')
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('refreshes a failed plugin secret clear and reports that the stored reference remains set', async () => {
    getSettings.mockResolvedValueOnce({
      global: { 'plugin.dp_x.url': 'existing', 'plugin.dp_x.tok': 'env:DP_X_TOKEN' },
      user: {}, revision: { global: 2, user: 4 },
    }).mockResolvedValueOnce({
      global: { 'plugin.dp_x.url': 'server-url', 'plugin.dp_x.tok': 'env:DP_X_TOKEN' },
      user: {}, revision: { global: 3, user: 4 },
    })
    putSettingsBatch.mockRejectedValueOnce(new Error('service unavailable'))
    render(<SettingsModal onClose={vi.fn()} />)

    fireEvent.change(await screen.findByPlaceholderText('anthropic/claude-opus-4-8'), { target: { value: 'staged-model' } })
    fireEvent.click(screen.getByRole('button', { name: 'Plugins' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear stored reference' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('The stored reference is still set; choose Clear again to retry.')
    expect(screen.getByLabelText('Token')).toHaveValue('env:DP_X_TOKEN')
    expect(screen.getByRole('button', { name: 'Clear…' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Agent' }))
    expect(screen.getByPlaceholderText('anthropic/claude-opus-4-8')).toHaveValue('staged-model')
  })

  it('does not overwrite a concurrently changed plugin secret reference', async () => {
    getSettings.mockResolvedValueOnce({
      global: { 'plugin.dp_x.url': 'existing', 'plugin.dp_x.tok': 'env:DP_X_TOKEN' },
      user: {}, revision: { global: 2, user: 4 },
    }).mockResolvedValueOnce({
      global: { 'plugin.dp_x.url': 'existing', 'plugin.dp_x.tok': 'env:ROTATED_TOKEN' },
      user: {}, revision: { global: 3, user: 4 },
    })
    putSettingsBatch.mockRejectedValueOnce(new KernelError(409, 'settings revision is stale'))
    render(<SettingsModal onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear stored reference' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Token changed on the server and was not cleared.')
    expect(screen.getByLabelText('Token')).toHaveValue('env:ROTATED_TOKEN')
    expect(putSettingsBatch).toHaveBeenCalledOnce()
  })

  it('shows a declared default without an override and saves typed plugin values', async () => {
    getSettings.mockResolvedValue({
      global: {
        'plugin.dp_schema.count': 1,
        'plugin.dp_schema.ratio': 0.5,
        'plugin.dp_schema.label': 'old label',
        'plugin.dp_schema.mode': 'fast',
      }, user: {}, revision: { global: 2, user: 4 },
    })
    plugins.mockResolvedValue([SCHEMA_PACK])
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))

    // No stored `enabled` value still displays the manifest's true default and does not dirty the form.
    expect(screen.getByLabelText('Enabled')).toHaveTextContent('true')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()

    fireEvent.click(screen.getByLabelText('Enabled'))
    fireEvent.click(await screen.findByRole('option', { name: 'false' }))
    fireEvent.change(screen.getByLabelText('Count'), { target: { value: '42' } })
    fireEvent.change(screen.getByLabelText('Ratio'), { target: { value: '1.25' } })
    fireEvent.change(screen.getByLabelText('Label'), { target: { value: 'new label' } })
    fireEvent.click(screen.getByLabelText('Mode'))
    fireEvent.click(await screen.findByRole('option', { name: 'balanced' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(putSettingsBatch).toHaveBeenCalledWith(
      { global: 2, user: 4 },
      [
        { scope: 'global', key: 'plugin.dp_schema.enabled', value: false },
        { scope: 'global', key: 'plugin.dp_schema.count', value: 42 },
        { scope: 'global', key: 'plugin.dp_schema.ratio', value: 1.25 },
        { scope: 'global', key: 'plugin.dp_schema.label', value: 'new label' },
        { scope: 'global', key: 'plugin.dp_schema.mode', value: 'balanced' },
      ],
    ))
  })

  it('keeps the bundled semantic-catalog enabled default effective without creating an override', async () => {
    getSettings.mockResolvedValue({ global: {}, user: {}, revision: { global: 2, user: 4 } })
    plugins.mockResolvedValue([SEMANTIC_CATALOG_PACK])
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))

    expect(screen.getByLabelText('Enable semantic search')).toHaveTextContent('true')
    expect(screen.getByText('Using environment/default.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('uses null only to remove a non-secret plugin override and falls back to the declared default', async () => {
    getSettings.mockResolvedValue({
      global: { 'plugin.dp_schema.enabled': false }, user: {}, revision: { global: 2, user: 4 },
    })
    plugins.mockResolvedValue([SCHEMA_PACK])
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))

    fireEvent.click(screen.getByRole('button', { name: 'Use environment/default' }))
    expect(screen.queryByRole('button', { name: 'Use environment/default' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(putSettingsBatch).toHaveBeenCalledWith(
      { global: 2, user: 4 },
      [{ scope: 'global', key: 'plugin.dp_schema.enabled', value: null }],
    ))
  })

  it('does not save an incomplete numeric plugin override', async () => {
    plugins.mockResolvedValue([SCHEMA_PACK])
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))
    fireEvent.change(screen.getByLabelText('Count'), { target: { value: '' } })

    expect(await screen.findByText('Enter a finite integer.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('recovers typed and non-conflicting plugin edits after repeated revision conflicts', async () => {
    getSettings.mockResolvedValueOnce({
      global: { 'plugin.dp_schema.count': 1, 'plugin.dp_schema.label': 'old label' }, user: {}, revision: { global: 2, user: 4 },
    }).mockResolvedValueOnce({
      global: { 'plugin.dp_schema.count': 9, 'plugin.dp_schema.label': 'old label' }, user: {}, revision: { global: 3, user: 4 },
    }).mockResolvedValueOnce({
      global: { 'plugin.dp_schema.count': 10, 'plugin.dp_schema.label': 'old label' }, user: {}, revision: { global: 4, user: 4 },
    })
    plugins.mockResolvedValue([SCHEMA_PACK])
    putSettingsBatch.mockRejectedValueOnce(new KernelError(409, 'settings revision is stale'))
      .mockRejectedValueOnce(new KernelError(409, 'settings revision is stale'))
      .mockResolvedValue({ ok: true, revision: { global: 5, user: 4 } })
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))
    fireEvent.change(screen.getByLabelText('Count'), { target: { value: '42' } })
    fireEvent.change(screen.getByLabelText('Label'), { target: { value: 'local label' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    const recovery = await screen.findByTestId('settings-conflict-recovery')
    expect(recovery).toHaveTextContent('global: plugin.dp_schema.count')
    expect(recovery).not.toHaveTextContent('global: plugin.dp_schema.label')
    expect(screen.getByLabelText('Count')).toHaveValue(9)
    fireEvent.change(screen.getByLabelText('Label'), { target: { value: 'post-conflict draft' } })
    const saveButton = screen.getByRole('button', { name: 'Save' })
    expect(saveButton).toBeDisabled()
    fireEvent.click(saveButton)
    expect(putSettingsBatch).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Reapply local values for review' }))
    expect(screen.getByLabelText('Count')).toHaveValue(42)
    expect(screen.getByLabelText('Label')).toHaveValue('local label')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByTestId('settings-conflict-recovery')).toHaveTextContent('global: plugin.dp_schema.count')
    expect(screen.getByLabelText('Count')).toHaveValue(10)
    fireEvent.click(screen.getByRole('button', { name: 'Reapply local values for review' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(putSettingsBatch).toHaveBeenLastCalledWith(
      { global: 4, user: 4 },
      [
        { scope: 'global', key: 'plugin.dp_schema.count', value: 42 },
        { scope: 'global', key: 'plugin.dp_schema.label', value: 'local label' },
      ],
    ))
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

  it('closes clean Settings without a discard confirmation', async () => {
    const onClose = vi.fn()
    render(<SettingsModal onClose={onClose} />)
    await screen.findByPlaceholderText('anthropic/claude-opus-4-8')

    fireEvent.keyDown(screen.getByTestId('settings-modal'), { key: 'Escape', code: 'Escape' })

    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
    expect(screen.queryByTestId('settings-discard-confirmation')).toBeNull()
  })

  it('keeps a dirty draft and returns focus to its editing control after Escape', async () => {
    const onClose = vi.fn()
    render(<SettingsModal onClose={onClose} />)
    const model = await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    model.focus()
    fireEvent.change(model, { target: { value: 'edited-model' } })

    fireEvent.keyDown(model, { key: 'Escape', code: 'Escape' })

    expect(await screen.findByTestId('settings-discard-confirmation')).toBeVisible()
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(model).toHaveFocus())
    expect(model).toHaveValue('edited-model')
  })

  it('warns for dirty close-button dismissal, then discards only on confirmation', async () => {
    const onClose = vi.fn()
    render(<SettingsModal onClose={onClose} />)
    const model = await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    fireEvent.change(model, { target: { value: 'edited-model' } })

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(await screen.findByTestId('settings-discard-confirmation')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it.each([
    ['Destinations', 'Destination name', 'draft destination'],
    ['Credentials', 'Credential name', 'draft credential'],
  ])('protects an unsaved %s draft on dismissal', async (pane, label, draft) => {
    const onClose = vi.fn()
    render(<SettingsModal onClose={onClose} />)
    fireEvent.click(await screen.findByRole('button', { name: pane }))
    const input = screen.getByLabelText(label)
    input.focus()
    fireEvent.change(input, { target: { value: draft } })

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    expect(await screen.findByTestId('settings-discard-confirmation')).toBeVisible()
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(input).toHaveFocus())
    expect(input).toHaveValue(draft)
  })

  it('protects an unsaved member draft on dismissal', async () => {
    const onClose = vi.fn()
    render(<SettingsModal onClose={onClose} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Members' }))
    const input = screen.getByPlaceholderText('Name')
    input.focus()
    fireEvent.change(input, { target: { value: 'draft member' } })

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    expect(await screen.findByTestId('settings-discard-confirmation')).toBeVisible()
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(input).toHaveFocus())
    expect(input).toHaveValue('draft member')
  })

  it('uses the native beforeunload contract only while Settings is dirty', async () => {
    render(<SettingsModal onClose={vi.fn()} />)
    const model = await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    const clean = new Event('beforeunload', { cancelable: true })
    expect(window.dispatchEvent(clean)).toBe(true)

    fireEvent.change(model, { target: { value: 'edited-model' } })
    const dirty = new Event('beforeunload', { cancelable: true })
    expect(window.dispatchEvent(dirty)).toBe(false)
    expect(dirty.defaultPrevented).toBe(true)
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

  it('keeps unrelated staged Settings while a credential action is pending', async () => {
    let resolveCreate: ((value: unknown) => void) | undefined
    createCred.mockReturnValueOnce(new Promise((resolve) => { resolveCreate = resolve }))
    render(<SettingsModal onClose={vi.fn()} />)
    const model = await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    fireEvent.change(model, { target: { value: 'staged-model' } })
    fireEvent.click(screen.getByRole('button', { name: 'Credentials' }))
    fireEvent.change(screen.getByLabelText('Credential name'), { target: { value: 'Slow credential' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add credential' }))

    expect(screen.getByRole('button', { name: 'Saving credential…' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Saving credential…' }))
    expect(createCred).toHaveBeenCalledOnce()
    resolveCreate?.({ id: 'slow', name: 'Slow credential', kind: 'object_store', fields: {} })

    expect(await screen.findByText(/applied immediately; staged Settings are unchanged/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Agent' }))
    expect(screen.getByPlaceholderText('anthropic/claude-opus-4-8')).toHaveValue('staged-model')
    expect(putSettingsBatch).not.toHaveBeenCalled()
  })

  it('shows an actionable member failure and prevents duplicate submission', async () => {
    let rejectCreate: ((reason?: unknown) => void) | undefined
    createUser.mockReturnValueOnce(new Promise((_, reject) => { rejectCreate = reject }))
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Members' }))
    fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'Taylor' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add member' }))

    expect(screen.getByRole('button', { name: 'Adding member…' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Adding member…' }))
    expect(createUser).toHaveBeenCalledOnce()
    rejectCreate?.(new Error('name is already in use'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not add Taylor: name is already in use')
    expect(screen.getByPlaceholderText('Name')).toHaveValue('Taylor')
  })

  it('blocks removing a credential that a staged reference still selects', async () => {
    listCreds.mockResolvedValue([{ id: 'c1', name: 'Store', kind: 'object_store', fields: {} }])
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Credentials' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Make default' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove credential Store' }))

    expect(deleteCred).not.toHaveBeenCalled()
    expect(await screen.findByRole('alert')).toHaveTextContent('Select a different credential (or None) and Save before removing it.')
  })

  it.each([
    ['staged destination', true],
    ['destination draft', false],
  ])('blocks removing a credential selected by a %s', async (_state, addDestination) => {
    listCreds.mockResolvedValue([{ id: 'c1', name: 'Store', kind: 'object_store', fields: {} }])
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Destinations' }))
    fireEvent.change(screen.getByLabelText('Destination name'), { target: { value: 'Exports' } })
    fireEvent.click(screen.getByLabelText('Destination backend'))
    fireEvent.click(await screen.findByRole('option', { name: 's3' }))
    fireEvent.change(screen.getByPlaceholderText('s3://bucket/prefix'), { target: { value: 's3://bucket/exports' } })
    fireEvent.click(screen.getByLabelText('Destination credential'))
    fireEvent.click(await screen.findByRole('option', { name: 'Store' }))
    if (addDestination) fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    fireEvent.click(screen.getByRole('button', { name: 'Credentials' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove credential Store' }))

    expect(deleteCred).not.toHaveBeenCalled()
    expect(await screen.findByRole('alert')).toHaveTextContent('Select a different credential (or None) and Save before removing it.')
  })

  it('reports a failed kernel restart without committing staged Settings', async () => {
    state.kernelInfo = { runners: ['kernel'], backends: [] }
    let rejectRestart: ((reason?: unknown) => void) | undefined
    restartKernel.mockReturnValueOnce(new Promise((_, reject) => { rejectRestart = reject }))
    render(<SettingsModal onClose={vi.fn()} />)
    const model = await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    fireEvent.change(model, { target: { value: 'staged-model' } })
    fireEvent.click(screen.getByRole('button', { name: 'Execution' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Restart kernel' }))

    expect(screen.getByRole('button', { name: 'Restarting…' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Restarting…' }))
    expect(restartKernel).toHaveBeenCalledOnce()
    rejectRestart?.(new Error('kernel is unavailable'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not restart kernel: kernel is unavailable')
    fireEvent.click(screen.getByRole('button', { name: 'Agent' }))
    expect(screen.getByPlaceholderText('anthropic/claude-opus-4-8')).toHaveValue('staged-model')
    expect(putSettingsBatch).not.toHaveBeenCalled()
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
