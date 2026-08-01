import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
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
const browseDestination = vi.fn()
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
    browseDestination: (...a: unknown[]) => browseDestination(...a),
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
    browseDestination.mockReset().mockResolvedValue({ path: '', entries: [] })
    state.kernelInfo = { runners: ['local-out-of-core'], backends: [] }
    state.currentUser.capabilities = ['global_settings']
    state.authEnabled = false
  })

  it('opens directly to a requested settings category', async () => {
    render(<SettingsModal onClose={vi.fn()} initialCategory="destinations" />)

    expect(await screen.findByText(/Named places to save outputs/i)).toBeInTheDocument()
    const destinations = screen.getByRole('button', { name: 'Destinations' })
    expect(destinations).toHaveClass('bg-accent')
    expect(destinations).toHaveFocus()
  })

  it('renders declared fields, saves them as plugin.<pack>.<key>, and skips a blank secret', async () => {
    render(<SettingsModal onClose={vi.fn()} />)

    // Plugins is its own pane now (master-detail) — switch to it before editing its fields
    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))
    // the url field is pre-filled from config_values; the secret token prompts for a reference
    const pluginCard = screen.getByTestId('plugin-status-dp_x')
    expect(within(pluginCard).getByLabelText('URL')).toBeVisible()
    expect(screen.getAllByText('dp_x')).toHaveLength(1)
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

  it('preserves unrelated edits made while a failed plugin secret clear is pending', async () => {
    let rejectClear: ((reason: unknown) => void) | undefined
    getSettings.mockResolvedValueOnce({
      global: { agentModel: 'server-model', 'plugin.dp_x.url': 'existing', 'plugin.dp_x.tok': 'env:DP_X_TOKEN' },
      user: {}, revision: { global: 2, user: 4 },
    }).mockResolvedValueOnce({
      global: { agentModel: 'server-model', 'plugin.dp_x.url': 'server-url', 'plugin.dp_x.tok': 'env:DP_X_TOKEN' },
      user: {}, revision: { global: 3, user: 4 },
    })
    putSettingsBatch.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectClear = reject }))
    render(<SettingsModal onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear stored reference' }))
    expect(screen.getByLabelText('Token')).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Agent' }))
    fireEvent.change(screen.getByPlaceholderText('anthropic/claude-opus-4-8'), { target: { value: 'during-clear' } })

    rejectClear?.(new Error('service unavailable'))
    await waitFor(() => expect(getSettings).toHaveBeenCalledTimes(2))
    expect(screen.getByPlaceholderText('anthropic/claude-opus-4-8')).toHaveValue('during-clear')
    fireEvent.click(screen.getByRole('button', { name: 'Plugins' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The stored reference is still set; choose Clear again to retry.')
  })

  it('distinguishes an unrelated revision conflict from a changed secret reference', async () => {
    getSettings.mockResolvedValueOnce({
      global: { 'plugin.dp_x.url': 'existing', 'plugin.dp_x.tok': 'env:DP_X_TOKEN' },
      user: {}, revision: { global: 2, user: 4 },
    }).mockResolvedValueOnce({
      global: { 'plugin.dp_x.url': 'server-url', 'plugin.dp_x.tok': 'env:DP_X_TOKEN' },
      user: {}, revision: { global: 3, user: 4 },
    })
    putSettingsBatch.mockRejectedValueOnce(new KernelError(409, 'settings revision is stale'))
    render(<SettingsModal onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear stored reference' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Settings changed on the server before Token could be cleared.')
    expect(alert).not.toHaveTextContent('Token changed on the server')
    expect(screen.getByLabelText('Token')).toHaveValue('env:DP_X_TOKEN')
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
    state.authEnabled = true
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

  it('keeps unrelated Settings available when extension metadata fails', async () => {
    plugins.mockRejectedValueOnce(new Error('HTTP 500: plugin registry unavailable'))
    render(<SettingsModal onClose={vi.fn()} />)

    const model = await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    fireEvent.change(model, { target: { value: 'staged-model' } })
    fireEvent.click(screen.getByRole('button', { name: 'Plugins' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Extensions could not be loaded: HTTP 500: plugin registry unavailable')
    expect(screen.queryByText('No extensions installed.')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByTestId('plugin-status-dp_x')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Agent' }))
    expect(screen.getByPlaceholderText('anthropic/claude-opus-4-8')).toHaveValue('staged-model')
  })

  it('shows healthy, inactive, degraded, conflict, and failed plugin lifecycle states truthfully', async () => {
    plugins.mockResolvedValue([
      { name: 'healthy', package: 'dp-healthy', source: 'entry_point', version: '1.2.0', state: 'active',
        effective_capabilities: ['node:clean'], process_placement: ['execution'] },
      { name: 'idle', source: 'drop-in', state: 'inactive', effective_capabilities: [], process_placement: [] },
      { name: 'partial', source: 'module', state: 'degraded', effective_capabilities: ['node:usable'],
        process_placement: ['execution'], failure_summary: 'Runner activation failed (RuntimeError); check server logs.',
        failure_impact: 'optional-degradation' },
      { name: 'collision', source: 'drop-in', state: 'conflict', effective_capabilities: [], process_placement: [],
        failure_summary: "Node 'source' conflicts with a built-in node.", failure_impact: 'optional-degradation' },
      { name: 'broken', source: 'entry_point', state: 'failed', effective_capabilities: [], process_placement: [],
        failure_summary: 'Plugin registration failed (ValueError); check server logs.', failure_impact: 'optional-degradation' },
    ])
    render(<SettingsModal onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))
    expect(within(screen.getByTestId('plugin-status-healthy')).getByText('active')).toBeVisible()
    expect(screen.getByTestId('plugin-status-healthy')).toHaveTextContent('Ready to use in this Data Playground instance.')
    expect(screen.getByTestId('plugin-status-healthy')).toHaveTextContent('Canvas step: clean')
    expect(screen.getByTestId('plugin-status-healthy')).toHaveTextContent('Next: add its steps from a Canvas.')
    fireEvent.click(within(screen.getByTestId('plugin-status-healthy')).getByText('Installation details'))
    expect(screen.getByTestId('plugin-status-healthy')).toHaveTextContent('Features: node:clean')
    expect(screen.getByTestId('plugin-status-healthy')).toHaveTextContent('Starts with: execution')
    expect(within(screen.getByTestId('plugin-status-idle')).getByText('inactive')).toBeVisible()
    expect(screen.getByTestId('plugin-status-idle')).toHaveTextContent('Installed, but not currently available.')
    expect(within(screen.getByTestId('plugin-status-partial')).getByText('degraded')).toBeVisible()
    expect(screen.getByTestId('plugin-status-partial')).toHaveTextContent('Other parts of Data Playground still work.')
    expect(within(screen.getByTestId('plugin-status-collision')).getByText('conflict')).toBeVisible()
    expect(within(screen.getByTestId('plugin-status-broken')).getByText('failed')).toBeVisible()
  })

  it('shows the empty discovery state without implying a load failure', async () => {
    plugins.mockResolvedValue([])
    render(<SettingsModal onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Plugins' }))
    expect(screen.getByText('No extensions installed.')).toBeVisible()
  })

  it('hides admin-only controls and saves only the user runner for a non-admin', async () => {
    state.currentUser.capabilities = []
    render(<SettingsModal onClose={vi.fn()} />)

    expect(await screen.findByText('Workspace-wide settings are managed by an administrator. You can still change how your own jobs run.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Agent' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Destinations' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Plugins' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Members' })).toBeNull()
    expect(screen.queryByPlaceholderText('anthropic/claude-opus-4-8')).toBeNull()
    expect(screen.queryByPlaceholderText('access key id')).toBeNull()
    expect(screen.queryByPlaceholderText('Name')).toBeNull()
    expect(plugins).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Use Local streaming' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(putSettingsBatch).toHaveBeenCalledWith(
      { global: 2, user: 4 },
      [{ scope: 'user', key: 'backend', value: 'local-out-of-core' }],
    ))
  })

  it('uses friendly execution names and does not present host capacity as runner capacity', async () => {
    state.kernelInfo = {
      runners: ['local-out-of-core', 'local-subprocess', 'kernel', 'acme-batch'],
      backends: [
        { name: 'local-out-of-core', workers: [{ id: 'local-out-of-core:local', state: 'idle', capacity: { cpu: 8, mem: '32GiB' } }] },
        { name: 'local-subprocess', workers: [{ id: 'local-subprocess:local', state: 'idle', capacity: { cpu: 8, mem: '32GiB' } }] },
        { name: 'kernel', workers: [{ id: 'kernel:local', state: 'idle', capacity: { cpu: 8, mem: '32GiB' } }] },
        { name: 'acme-batch', workers: [
          { id: 'acme-01', state: 'busy', capacity: { cpu: 32, mem: '128GiB' } },
          { id: 'acme-02', state: 'idle', capacity: { cpu: 64, mem: '256GiB' } },
        ] },
        { name: 'acme-offline', workers: [] },
      ],
    } as typeof state.kernelInfo
    getSettings.mockResolvedValue({
      global: { backend: 'kernel' }, user: {}, revision: { global: 2, user: 4 },
    })

    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Execution' }))

    expect(screen.getByText('Choose how your jobs run')).toBeVisible()
    expect(screen.getByText('Local streaming')).toBeVisible()
    expect(screen.getByText('Isolated local process')).toBeVisible()
    expect(screen.getByText('Warm Canvas worker')).toBeVisible()
    expect(screen.getByText('Acme batch')).toBeVisible()
    expect(screen.getByText(/Runs through a provider configured for this workspace/)).toBeVisible()
    expect(screen.queryByText(/8 cpu|32GiB|128GiB|256GiB/)).toBeNull()
    expect(screen.queryByText('local-out-of-core')).toBeNull()
    expect(screen.queryByText('kernel:local')).toBeNull()
    expect(screen.queryByText('acme-01')).toBeNull()

    expect(screen.getByRole('button', { name: 'Use Automatic execution' })).toBeVisible()
  })

  it('presents inherited execution as Automatic without exposing deployment internals', async () => {
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Execution' }))
    expect(screen.getByRole('button', { name: 'Use Automatic execution' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('Leave Automatic selected unless you need a specific isolation or worker behavior.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Use Automatic execution' })).toHaveTextContent('Uses the default configured for Data Playground.')
    expect(screen.queryByText(/Uses the mode chosen for this Workspace/)).toBeNull()
    expect(screen.queryByText('Workspace default (deployment default)')).toBeNull()
  })

  it('lets a researcher choose an execution mode from its explanation card', async () => {
    state.kernelInfo = {
      runners: ['local-out-of-core', 'local-subprocess'],
      backends: [],
    } as typeof state.kernelInfo
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Execution' }))

    const isolated = screen.getByRole('button', { name: 'Use Isolated local process' })
    expect(isolated).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(isolated)
    expect(isolated).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('group', { name: 'Execution mode' })).toHaveTextContent('Isolated local process')

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(putSettingsBatch).toHaveBeenCalledWith(
      { global: 2, user: 4 },
      [{ scope: 'user', key: 'backend', value: 'local-subprocess' }],
    ))
  })

  it('does not expose Restart kernel when Settings has no explicit runner selection', async () => {
    state.kernelInfo = {
      runners: ['local-out-of-core', 'local-subprocess', 'kernel'],
      backends: [],
    } as typeof state.kernelInfo
    render(<SettingsModal onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Execution' }))
    expect(screen.queryByRole('button', { name: 'Restart kernel' })).toBeNull()
  })

  it('offers Restart kernel for an explicit user runner selection', async () => {
    state.kernelInfo = {
      runners: ['local-out-of-core', 'local-subprocess', 'kernel'],
      backends: [],
    } as typeof state.kernelInfo
    getSettings.mockResolvedValue({
      global: {}, user: { backend: 'kernel' }, revision: { global: 2, user: 4 },
    })
    render(<SettingsModal onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Execution' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Restart kernel' }))

    await waitFor(() => expect(restartKernel).toHaveBeenCalledWith('canvas'))
  })

  it('does not expose Restart kernel for an explicit non-kernel runner', async () => {
    state.kernelInfo = {
      runners: ['local-out-of-core', 'local-subprocess', 'kernel'],
      backends: [],
    } as typeof state.kernelInfo
    getSettings.mockResolvedValue({
      global: { backend: 'local-out-of-core' }, user: {}, revision: { global: 2, user: 4 },
    })
    render(<SettingsModal onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Execution' }))
    expect(screen.queryByRole('button', { name: 'Restart kernel' })).toBeNull()
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
    fireEvent.click(screen.getByRole('button', { name: 'Use Local streaming' }))
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
    fireEvent.click(screen.getByRole('button', { name: 'Use Local streaming' }))
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
    state.authEnabled = true
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Members' }))
    fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'Taylor' } })
    fireEvent.change(screen.getByLabelText('Initial password'), { target: { value: 'secret1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

    expect(screen.getByRole('button', { name: 'Adding…' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Adding…' }))
    expect(createUser).toHaveBeenCalledOnce()
    rejectCreate?.(new Error('name is already in use'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not add Taylor: name is already in use')
    expect(screen.getByPlaceholderText('Name')).toHaveValue('Taylor')
  })

  it('adds a collaboration member without asking for a password while authentication is off', async () => {
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Members' }))

    fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'Taylor' } })
    expect(screen.queryByLabelText('Initial password')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Add identity' }))
    await waitFor(() => expect(createUser).toHaveBeenCalledWith('Taylor'))
    expect(screen.getByText(/collaboration identities rather than password accounts/i)).toBeVisible()
    expect(screen.getByText(/must not be treated as authentication/i)).toBeVisible()
  })

  it('requires an initial password of at least six characters when authentication is on', async () => {
    state.authEnabled = true
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Members' }))
    fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'Taylor' } })
    fireEvent.change(screen.getByLabelText('Initial password'), { target: { value: 'short' } })

    expect(screen.getByRole('alert')).toHaveTextContent('Password must be at least 6 characters.')
    expect(screen.getByRole('button', { name: 'Create account' })).toBeDisabled()
    expect(createUser).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Initial password'), { target: { value: 'secret1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    await waitFor(() => expect(createUser).toHaveBeenCalledWith('Taylor', 'secret1'))
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
    getSettings.mockResolvedValue({
      global: { backend: 'kernel' }, user: {}, revision: { global: 2, user: 4 },
    })
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

  it('tests browsing for a saved destination without claiming write access', async () => {
    getSettings.mockResolvedValue({
      global: { destinations: [{ id: 'd1', name: 'Exports', backend: 's3', root: 's3://b/p' }] },
      user: {},
      revision: { global: 2, user: 4 },
    })
    browseDestination.mockResolvedValue({
      path: '', entries: [{ name: 'result.parquet', kind: 'file', uri: 's3://b/p/result.parquet' }],
    })
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Destinations' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Preview files in Exports' }))

    await waitFor(() => expect(browseDestination).toHaveBeenCalledWith('d1', ''))
    expect(await screen.findByText(/Preview loaded · 1 item found/)).toHaveTextContent('This checks listing only; a real write is verified when a run saves output.')
    expect(screen.getByText(/does not create a test file or prove write access/i)).toBeVisible()
    expect(screen.queryByText(/write access works/i)).toBeNull()
  })

  it('does not test a destination until its staged Settings are saved', async () => {
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Destinations' }))
    fireEvent.change(screen.getByLabelText('Destination name'), { target: { value: 'Draft' } })
    fireEvent.change(screen.getByLabelText('Destination root or prefix'), { target: { value: '/tmp/draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    expect(screen.getByText('Save to preview')).toBeVisible()
    expect(screen.queryByRole('button', { name: /Preview files in Draft/ })).toBeNull()
    expect(browseDestination).not.toHaveBeenCalled()
  })

  it('labels and validates destination roots before adding them', async () => {
    render(<SettingsModal onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Destinations' }))

    const name = screen.getByLabelText('Destination name')
    const root = screen.getByLabelText('Destination root or prefix')
    const add = screen.getByRole('button', { name: 'Add' })
    expect(add).toBeDisabled()

    fireEvent.change(name, { target: { value: 'Local exports' } })
    fireEvent.change(root, { target: { value: '/tmp/exports' } })
    expect(add).toBeEnabled()
    fireEvent.change(root, { target: { value: 's3://bucket/exports' } })
    expect(add).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a local filesystem path, not a URI.')

    fireEvent.click(screen.getByLabelText('Destination backend'))
    fireEvent.click(await screen.findByRole('option', { name: 's3' }))
    expect(add).toBeEnabled()
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.change(root, { target: { value: 'gs://bucket/exports' } })
    expect(add).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a s3:// bucket and optional prefix.')

    fireEvent.click(screen.getByLabelText('Destination backend'))
    fireEvent.click(await screen.findByRole('option', { name: 'gs' }))
    expect(add).toBeEnabled()
  })
})
