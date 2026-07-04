import { useEffect, useState } from 'react'
import { api, type DpUser } from '../api/client'
import { color, radius, shadow } from '../theme/tokens'

// Shown only when auth is enabled (DP_AUTH_SECRET) and there's no valid session. Pick who you are +
// the shared password → a signed session cookie. A per-user credential / SSO replaces this later.
export function Login({ onLoggedIn }: { onLoggedIn: (userId: string) => void }) {
  const [users, setUsers] = useState<DpUser[]>([])
  const [userId, setUserId] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { api.users().then((u) => { setUsers(u); setUserId(u[0]?.id ?? '') }).catch(() => {}) }, [])

  const submit = async () => {
    setBusy(true); setErr('')
    try { const r = await api.login(userId, password); onLoggedIn(r.userId) }
    catch (e) { setErr((e as Error).message || 'Login failed') }
    finally { setBusy(false) }
  }

  return (
    <div style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', background: color.canvas }}>
      <div style={{ width: 320, background: '#fff', border: `1px solid ${color.border}`, borderRadius: radius.panel, boxShadow: shadow.panel, padding: 22 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
          <span style={{ width: 22, height: 22, borderRadius: 6, background: color.ink, color: '#fff', display: 'grid', placeItems: 'center', fontSize: 13, fontWeight: 700 }}>D</span>
          <span style={{ fontSize: 14, fontWeight: 700, color: color.ink }}>Data Playground</span>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <label style={{ fontSize: 11, color: color.text2 }}>User
            <select value={userId} onChange={(e) => setUserId(e.target.value)}
              style={{ width: '100%', marginTop: 4, fontSize: 13, border: `1px solid ${color.border}`, borderRadius: 8, padding: '8px 10px', background: '#fff' }}>
              {users.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
            </select>
          </label>
          <label style={{ fontSize: 11, color: color.text2 }}>Password
            <input type="password" value={password} autoFocus onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
              style={{ width: '100%', marginTop: 4, fontSize: 13, border: `1px solid ${color.border}`, borderRadius: 8, padding: '8px 10px', outline: 'none' }} />
          </label>
          {err && <div style={{ fontSize: 12, color: color.failed }}>{err}</div>}
          <button onClick={submit} disabled={busy}
            style={{ marginTop: 4, border: 'none', borderRadius: 8, background: color.ink, color: '#fff', fontSize: 13, fontWeight: 600, padding: '9px 0', cursor: busy ? 'default' : 'pointer', opacity: busy ? 0.6 : 1 }}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </div>
      </div>
    </div>
  )
}
