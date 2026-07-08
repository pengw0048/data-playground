import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

const putSetting = vi.fn(async (..._a: unknown[]) => ({ ok: true }))
const PACK = {
  name: 'dp_x', source: 'drop-in', version: '0.1.0',
  config: [
    { key: 'url', type: 'string', label: 'URL' },
    { key: 'tok', type: 'password', secret: true, label: 'Token' },
  ],
  config_values: { url: 'existing' },   // non-secret current value (secret never sent)
  config_set: ['url'],
}
vi.mock('../api/client', () => ({
  api: {
    getSettings: async () => ({ global: {}, user: {} }),
    plugins: async () => [PACK],
    putSetting: (...a: unknown[]) => putSetting(...a),
    createUser: async () => ({}),
    restartKernel: async () => ({}),
  },
}))

const state = {
  kernelInfo: { runners: ['local-out-of-core'], backends: [] },
  users: [], currentUser: { id: 'u1', name: 'me' }, authEnabled: false,
  refreshUsers: vi.fn(), pushToast: vi.fn(), doc: { id: 'canvas' },
}
vi.mock('../store/graph', () => ({ useStore: (sel: (s: unknown) => unknown) => sel(state) }))

import { SettingsModal } from './SettingsModal'

describe('SettingsModal — plugin config form', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders declared fields, saves them as plugin.<pack>.<key>, and skips a blank secret', async () => {
    render(<SettingsModal onClose={vi.fn()} />)

    // the url field is pre-filled from config_values; the secret token is not (shows "not set")
    const url = await screen.findByDisplayValue('existing')
    const tok = screen.getByPlaceholderText(/not set/i)

    fireEvent.change(url, { target: { value: 'new-url' } })
    fireEvent.change(tok, { target: { value: 'sk-tok' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(putSetting).toHaveBeenCalledWith('global', 'plugin.dp_x.url', 'new-url'))
    expect(putSetting).toHaveBeenCalledWith('global', 'plugin.dp_x.tok', 'sk-tok')  // namespaced per pack

    // clearing the secret must NOT write a blank (that would wipe the stored secret) — it's skipped
    putSetting.mockClear()
    fireEvent.change(tok, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(putSetting).toHaveBeenCalledWith('global', 'plugin.dp_x.url', 'new-url'))
    expect(putSetting).not.toHaveBeenCalledWith('global', 'plugin.dp_x.tok', expect.anything())
  })
})
