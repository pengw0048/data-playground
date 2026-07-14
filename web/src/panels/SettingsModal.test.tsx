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
const putSetting = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    getSettings: () => getSettings(),
    plugins: () => plugins(),
    putSetting: (...a: unknown[]) => putSetting(...a),
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
    getSettings.mockReset().mockResolvedValue({ global: {}, user: {} })
    plugins.mockReset().mockResolvedValue([PACK])
    putSetting.mockReset().mockResolvedValue({ ok: true })
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

    await waitFor(() => expect(putSetting).toHaveBeenCalledWith('global', 'plugin.dp_x.url', 'new-url'))
    expect(putSetting).toHaveBeenCalledWith('global', 'plugin.dp_x.tok', 'env:DP_X_TOK')  // namespaced per pack

    // clearing the secret must NOT write a blank (that would wipe the stored reference) — it's skipped
    putSetting.mockClear()
    fireEvent.change(tok, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(putSetting).toHaveBeenCalledWith('global', 'plugin.dp_x.url', 'new-url'))
    expect(putSetting).not.toHaveBeenCalledWith('global', 'plugin.dp_x.tok', expect.anything())
  })

  it('surfaces a save failure instead of a false "Saved" (UX-01)', async () => {
    putSetting.mockRejectedValueOnce(new Error('save failed'))  // first write rejects
    render(<SettingsModal onClose={vi.fn()} />)
    await screen.findByPlaceholderText('anthropic/claude-opus-4-8')  // wait for load (agent pane is default)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(state.pushToast).toHaveBeenCalledWith('Could not save global setting "agentModel": save failed', 'error'))
    expect(screen.getByRole('alert')).toHaveTextContent('No update was confirmed. Your edits remain here.')
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
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
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

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(putSetting).toHaveBeenCalledWith('user', 'backend', ''))
    expect(putSetting).toHaveBeenCalledTimes(1)
  })

  it('reports a partial sequential save, keeps edits, and retries without claiming success', async () => {
    putSetting.mockImplementation(async () => {
      if (putSetting.mock.calls.length === 2) throw new Error('HTTP 500: write failed')
      return { ok: true }
    })
    render(<SettingsModal onClose={vi.fn()} />)
    const model = await screen.findByPlaceholderText('anthropic/claude-opus-4-8')
    fireEvent.change(model, { target: { value: 'edited-model' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not save global setting "agentApiKey": HTTP 500: write failed')
    expect(alert).toHaveTextContent('1 of 7 updates completed before the failure. Server settings may be partially updated; your edits remain here.')
    expect(screen.getByDisplayValue('edited-model')).toBeVisible()
    expect(screen.queryByText('Saved')).toBeNull()

    putSetting.mockResolvedValue({ ok: true })
    fireEvent.click(screen.getByRole('button', { name: 'Retry save' }))
    expect(await screen.findByText('Saved')).toBeVisible()
  })
})
