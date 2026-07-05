import { useEffect, useState } from 'react'
import { api, type DpUser } from '../api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

// Shown only when auth is enabled (DP_AUTH_SECRET) and there's no valid session. Pick who you are +
// your own password → a signed session cookie (verified against your per-user credential).
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
    <div className="absolute inset-0 grid place-items-center bg-background">
      <div className="w-80 rounded-lg border border-border bg-card p-6 shadow-lg">
        <div className="mb-4 flex items-center gap-2">
          <span className="grid h-[22px] w-[22px] place-items-center rounded-md bg-primary text-[13px] font-bold text-primary-foreground">D</span>
          <span className="text-sm font-bold text-foreground">Data Playground</span>
        </div>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="login-user" className="text-xs font-normal text-muted-foreground">User</Label>
            <Select value={userId} onValueChange={setUserId}>
              <SelectTrigger id="login-user">
                <SelectValue placeholder="User" />
              </SelectTrigger>
              <SelectContent>
                {users.map((u) => <SelectItem key={u.id} value={u.id}>{u.name}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="login-password" className="text-xs font-normal text-muted-foreground">Password</Label>
            <Input id="login-password" type="password" value={password} autoFocus
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') submit() }} />
          </div>
          {err && <div className="text-xs text-destructive">{err}</div>}
          <Button onClick={submit} disabled={busy} className="mt-1 w-full">
            {busy ? 'Signing in…' : 'Sign in'}
          </Button>
        </div>
      </div>
    </div>
  )
}
