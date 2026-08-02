import { useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  KernelError,
  type Cred,
  type CredKind,
  type DestinationPreset,
  type SettingChange,
  type SettingsSnapshot,
} from '../api/client'
import type { PluginConfigField, PluginInfo } from '../types/api'
import { useStore } from '../store/graph'
import { Icon, type IconName } from '../ui/Icon'
import { cn } from '@/lib/utils'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

// App / workspace settings — a full-screen page with a left category nav (like Figma / most apps),
// not a cramped modal. These are GLOBAL: the LLM agent (provider-agnostic; the key lives in the
// kernel), the default execution target, and save/open destinations. The current Canvas chooses its
// own target from the editor top bar. Datasets are curated from Workspace;
// canvas-scoped settings live in the separate CanvasSettingsModal (opened from the file menu).
const CATS: { id: string; label: string; icon: IconName }[] = [
  { id: 'agent', label: 'Agent', icon: 'sparkle' },
  { id: 'execution', label: 'Compute defaults', icon: 'server' },
  { id: 'destinations', label: 'Destinations', icon: 'export' },
  { id: 'credentials', label: 'Credentials', icon: 'link' },
  { id: 'plugins', label: 'Plugins', icon: 'grid' },
  { id: 'members', label: 'Members', icon: 'users' },
]

// Sentinel for the Automatic execution card; on save it maps back to an empty user override.
const INHERIT = '__default__'
// Radix Select forbids an empty value — sentinels for "no credential" pickers (mapped to '' on save).
const NO_CRED = '__none__'
const BUILTIN_RUNNER_PRESENTATION: Record<string, { label: string; guidance: string }> = {
  'local-out-of-core': {
    label: 'Local streaming',
    guidance: 'Streams larger data through this machine without requiring it all to fit in memory.',
  },
  'local-subprocess': {
    label: 'Isolated local process',
    guidance: 'Runs each job in a separate process so a failed or cancelled job does not interrupt the app.',
  },
  kernel: {
    label: 'Canvas worker',
    guidance: 'Keeps one reusable worker for each Canvas and can continue after the app restarts.',
  },
  'local-pool': {
    label: 'Local worker pool',
    guidance: 'Uses one of the worker slots configured by the workspace operator.',
  },
  'ray-data': {
    label: 'Ray Data',
    guidance: 'Uses the configured Ray runner. The Canvas menu shows whether it is local or Ray Jobs.',
  },
}
const OBJECT_STORE_FIELDS: { key: string; placeholder: string }[] = [
  { key: 'accessKeyId', placeholder: 'env:AWS_ACCESS_KEY_ID' },
  { key: 'secretAccessKey', placeholder: 'env:AWS_SECRET_ACCESS_KEY' },
  { key: 'region', placeholder: 'region (e.g. us-east-1)' },
  { key: 'endpoint', placeholder: 'endpoint (MinIO/R2, optional)' },
]
type CredForm = { id: string | null; name: string; kind: CredKind; fields: Record<string, string> }
const emptyCredForm = (kind: CredKind): CredForm => ({ id: null, name: '', kind, fields: {} })

type SaveFailure = {
  message: string
}

type ActionNotice = { kind: 'success' | 'error'; message: string }

type PluginSecretTarget = { pack: string; field: PluginConfigField }

type ConflictRecovery = {
  submitted: SettingChange[]
  serverChanged: SettingChange[]
}

type PluginEdits = Record<string, Record<string, unknown>>
type CanonicalPluginValue = { valid: true; value: unknown } | { valid: false }

function destinationRootError(backend: string, value: string): string {
  const root = value.trim()
  if (!root) return ''
  if (backend === 'local') {
    return /^[a-z][a-z0-9+.-]*:\/\//i.test(root)
      ? 'Enter a local filesystem path, not a URI.'
      : ''
  }
  const scheme = backend === 's3' ? 's3' : backend === 'gs' ? 'gs' : ''
  if (!scheme) return 'Choose a supported destination backend.'
  return new RegExp(`^${scheme}:\\/\\/[^/\\s]+(?:\\/[^\\s]*)?$`).test(root)
    ? ''
    : `Enter a ${scheme}:// bucket and optional prefix.`
}

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)

const pluginState = (plugin: PluginInfo): NonNullable<PluginInfo['state']> =>
  plugin.state ?? (plugin.error ? 'failed' : 'active')

const pluginStateTone: Record<NonNullable<PluginInfo['state']>, string> = {
  active: 'bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-300',
  inactive: 'bg-secondary text-secondary-foreground',
  degraded: 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
  conflict: 'bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-300',
  failed: 'bg-destructive text-destructive-foreground',
}

const pluginStateCopy: Record<NonNullable<PluginInfo['state']>, string> = {
  active: '',
  inactive: 'Installed, but not currently available.',
  degraded: 'Some features are unavailable.',
  conflict: 'Could not start because it conflicts with another extension.',
  failed: 'Could not start.',
}

const CAPABILITY_KIND_LABELS: Record<string, string> = {
  adapter: 'Data connection',
  catalog: 'Catalog',
  'external-wait': 'External task provider',
  node: 'Canvas step',
  'pipeline-importer': 'Pipeline import',
  processor: 'Transform',
  runner: 'Execution mode',
  telemetry: 'Monitoring',
}

function capabilityLabel(capability: string): string {
  const [kind, ...rest] = capability.split(':')
  const detail = rest.join(':').replace(/[._/-]+/g, ' ').trim()
  const kindLabel = CAPABILITY_KIND_LABELS[kind] ?? kind.replace(/[._/-]+/g, ' ')
  if (!detail) return kindLabel.charAt(0).toUpperCase() + kindLabel.slice(1)
  return `${kindLabel}: ${detail}`
}

function pluginActionCopy(plugin: PluginInfo, state: NonNullable<PluginInfo['state']>): string {
  const configurable = (plugin.config?.length ?? 0) > 0
  if (state === 'active') {
    if (configurable) return 'Configure below. Save, then restart the affected process.'
    const kinds = new Set((plugin.effective_capabilities ?? []).map((capability) => capability.split(':')[0]))
    const actions: string[] = []
    if (kinds.has('catalog') || kinds.has('adapter')) actions.push('Browse its data connections in Workspace')
    if (kinds.has('node') || kinds.has('processor')) actions.push('Add its steps from a Canvas')
    if (kinds.has('pipeline-importer')) actions.push('Import a supported pipeline from Transforms')
    if (kinds.has('runner')) actions.push('Choose it from the compute target in a Canvas top bar')
    return actions.length > 0
      ? `${actions.join('; ')}.`
      : 'No settings.'
  }
  return configurable
    ? 'Review the setup below. Save, then restart the affected process.'
    : 'This cannot be repaired in Settings. Open Installation details, fix the server installation or configuration, then restart Data Playground.'
}

const sameJson = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right)

const hasOwn = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key)

function effectivePluginValue(field: PluginConfigField, stored: unknown): unknown {
  return stored == null || stored === '' ? (field.default ?? '') : stored
}

function canonicalPluginValue(field: PluginConfigField, value: unknown): CanonicalPluginValue {
  if (value === null) return { valid: true, value: null }
  if (field.type === 'bool') return { valid: true, value: value === true || value === 'true' }
  if (field.type === 'int' || field.type === 'float') {
    if (typeof value === 'string' && !value.trim()) return { valid: false }
    const number = typeof value === 'number' ? value : Number(value)
    const valid = field.type === 'int' ? Number.isSafeInteger(number) : Number.isFinite(number)
    return valid ? { valid: true, value: number } : { valid: false }
  }
  const text = String(value)
  return field.type !== 'select' || !field.options || field.options.includes(text)
    ? { valid: true, value: text }
    : { valid: false }
}

function hasInvalidPluginEdit(pluginEdits: PluginEdits, plugins: PluginInfo[]): boolean {
  return Object.entries(pluginEdits).some(([pack, fields]) => {
    const schema = plugins.find((plugin) => plugin.name === pack)?.config ?? []
    return Object.entries(fields).some(([key, value]) => {
      const field = schema.find((candidate) => candidate.key === key)
      return Boolean(field && !field.secret && !canonicalPluginValue(field, value).valid)
    })
  })
}

function stagedSettings(
  baseline: SettingsSnapshot | null,
  global: Record<string, unknown>,
  user: Record<string, unknown>,
  pluginEdits: PluginEdits,
  plugins: PluginInfo[],
  canGlobal: boolean,
): SettingChange[] {
  if (!baseline) return []
  const changes: SettingChange[] = []
  if (canGlobal) {
    const candidates: [string, unknown, unknown][] = [
      ['agentModel', String(global.agentModel ?? ''), String(baseline.global.agentModel ?? '')],
      ['agentBaseUrl', String(global.agentBaseUrl ?? ''), String(baseline.global.agentBaseUrl ?? '')],
      ['agentCredId', global.agentCredId === NO_CRED ? '' : String(global.agentCredId ?? ''), String(baseline.global.agentCredId ?? '')],
      ['defaultObjectStoreCredId', global.defaultObjectStoreCredId === NO_CRED ? '' : String(global.defaultObjectStoreCredId ?? ''), String(baseline.global.defaultObjectStoreCredId ?? '')],
      [
        'agentDataPolicy',
        {
          level: String(global.agentDataPolicyLevel || 'metadata-only'),
          endpointIsLocal: Boolean(global.agentDataPolicyEndpointIsLocal),
        },
        {
          level: String((baseline.global.agentDataPolicy as { level?: string } | undefined)?.level || 'metadata-only'),
          endpointIsLocal: Boolean((baseline.global.agentDataPolicy as { endpointIsLocal?: boolean } | undefined)?.endpointIsLocal),
        },
      ],
      ['destinations', Array.isArray(global.destinations) ? global.destinations : [], Array.isArray(baseline.global.destinations) ? baseline.global.destinations : []],
    ]
    for (const [key, value, original] of candidates) {
      if (!sameJson(value, original)) changes.push({ scope: 'global', key, value })
    }
    for (const [pack, fields] of Object.entries(pluginEdits)) {
      const schema = plugins.find((plugin) => plugin.name === pack)?.config ?? []
      for (const [key, value] of Object.entries(fields)) {
        const field = schema.find((candidate) => candidate.key === key)
        if (!field || (field.secret && !value)) continue
        const canonical = canonicalPluginValue(field, value)
        if (!canonical.valid) continue
        const settingKey = `plugin.${pack}.${key}`
        const stored = baseline.global[settingKey]
        if (canonical.value === null) {
          if (stored != null && stored !== '') changes.push({ scope: 'global', key: settingKey, value: null })
        } else if (!sameJson(canonical.value, effectivePluginValue(field, stored))) {
          changes.push({ scope: 'global', key: settingKey, value: canonical.value })
        }
      }
    }
  }
  const backend = user.backend === INHERIT ? '' : String(user.backend ?? '')
  if (backend !== String(baseline.user.backend ?? '')) {
    changes.push({ scope: 'user', key: 'backend', value: backend })
  }
  return changes
}

function editableGlobal(snapshot: SettingsSnapshot): Record<string, unknown> {
  const global = { ...snapshot.global }
  const policy = (global.agentDataPolicy && typeof global.agentDataPolicy === 'object')
    ? global.agentDataPolicy as { level?: string; endpointIsLocal?: boolean }
    : null
  global.agentDataPolicyLevel = policy?.level || 'metadata-only'
  global.agentDataPolicyEndpointIsLocal = Boolean(policy?.endpointIsLocal)
  return global
}

const settingLabel = (change: SettingChange) => `${change.scope}: ${change.key}`

export function SettingsModal({ onClose, initialCategory }: { onClose: () => void; initialCategory?: string }) {
  const kernelInfo = useStore((s) => s.kernelInfo)
  const users = useStore((s) => s.users)
  const currentUser = useStore((s) => s.currentUser)
  const authEnabled = useStore((s) => s.authEnabled)
  const refreshUsers = useStore((s) => s.refreshUsers)
  const pushToast = useStore((s) => s.pushToast)
  const canvasId = useStore((s) => s.doc.id)
  const [g, setG] = useState<Record<string, unknown>>({})
  const [u, setU] = useState<Record<string, unknown>>({})  // per-user settings (scope='user')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [baseline, setBaseline] = useState<SettingsSnapshot | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveFailure, setSaveFailure] = useState<SaveFailure | null>(null)
  const [conflict, setConflict] = useState<ConflictRecovery | null>(null)
  const [savedMsg, setSavedMsg] = useState('')
  const [dest, setDest] = useState<{ name: string; backend: string; root: string; credId: string }>({ name: '', backend: 'local', root: '', credId: NO_CRED })
  const [destinationTestingId, setDestinationTestingId] = useState<string | null>(null)
  const [destinationNotices, setDestinationNotices] = useState<Record<string, ActionNotice>>({})
  const [creds, setCreds] = useState<Cred[]>([])
  const [credForm, setCredForm] = useState<CredForm>(emptyCredForm('object_store'))
  const [newUser, setNewUser] = useState({ name: '', password: '' })
  const [credentialSaving, setCredentialSaving] = useState(false)
  const [credentialDeletingId, setCredentialDeletingId] = useState<string | null>(null)
  const [credentialNotice, setCredentialNotice] = useState<ActionNotice | null>(null)
  const [memberAdding, setMemberAdding] = useState(false)
  const [memberNotice, setMemberNotice] = useState<ActionNotice | null>(null)
  const [kernelRestarting, setKernelRestarting] = useState(false)
  const [kernelNotice, setKernelNotice] = useState<ActionNotice | null>(null)
  const [plugins, setPlugins] = useState<PluginInfo[]>([])
  const [pluginLoadError, setPluginLoadError] = useState('')
  const [pluginReloading, setPluginReloading] = useState(false)
  const [pcfg, setPcfg] = useState<PluginEdits>({})  // pack → edited { key: value }, null = use environment/default
  const [pluginSecretTarget, setPluginSecretTarget] = useState<PluginSecretTarget | null>(null)
  const [pluginSecretClearingKey, setPluginSecretClearingKey] = useState<string | null>(null)
  const [pluginSecretNotices, setPluginSecretNotices] = useState<Record<string, ActionNotice>>({})
  const [active, setActive] = useState(
    CATS.some((category) => category.id === initialCategory) ? initialCategory! : 'agent',
  )
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const lastEditingControl = useRef<HTMLElement | null>(null)
  // /api/me is authoritative. Missing capabilities must fail closed: open/single-user mode also
  // receives global_settings, so there is no need for a permissive fallback while identity loads.
  const canGlobal = currentUser?.capabilities?.includes('global_settings') === true
  const categories = canGlobal ? CATS : CATS.filter((c) => c.id === 'execution')

  const addUser = async () => {
    const name = newUser.name.trim()
    const password = newUser.password
    if (!name || (authEnabled && password.length < 6) || memberAdding) return
    setMemberAdding(true)
    setMemberNotice(null)
    try {
      if (authEnabled) await api.createUser(name, password)
      else await api.createUser(name)
      setNewUser({ name: '', password: '' })
      await refreshUsers()
      setMemberNotice({ kind: 'success', message: `Added ${name}. This applied immediately; staged Settings are unchanged.` })
      pushToast(`Added ${name}`, 'success')
    } catch (e) {
      const message = `Could not add ${name}: ${errorMessage(e)}`
      setMemberNotice({ kind: 'error', message })
      pushToast(message, 'error')
    } finally { setMemberAdding(false) }
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError('')
    setPluginLoadError('')
    const settings = api.getSettings().catch((error) => {
      throw new Error(`Settings request failed: ${errorMessage(error)}`)
    })
    const pluginPacks = canGlobal
      ? api.plugins()
        .then((value) => ({ value, error: '' }))
        .catch((error) => ({ value: [] as PluginInfo[], error: errorMessage(error) }))
      : Promise.resolve({ value: [] as PluginInfo[], error: '' })
    const credList = canGlobal
      ? api.listCreds().catch((error) => { throw new Error(`Credentials request failed: ${errorMessage(error)}`) })
      : Promise.resolve([] as Cred[])
    Promise.all([settings, pluginPacks, credList]).then(([nextSettings, pluginResult, nextCreds]) => {
      if (cancelled) return
      setG(editableGlobal(nextSettings))
      setU(nextSettings.user)
      setBaseline(nextSettings)
      setPlugins(pluginResult.value)
      setPluginLoadError(pluginResult.error)
      setCreds(nextCreds)
      setPcfg({})
      setLoading(false)
    }).catch((error) => {
      if (cancelled) return
      setLoadError(errorMessage(error))
      setLoading(false)
    })
    return () => { cancelled = true }
  }, [canGlobal, loadAttempt])

  useEffect(() => {
    if (!canGlobal && active !== 'execution') setActive('execution')
  }, [active, canGlobal])

  // The revisioned Settings snapshot is the save baseline; /plugins contributes field schema only.
  const rawPval = (pack: string, key: string) => {
    const edits = pcfg[pack]
    if (edits && hasOwn(edits, key)) return edits[key]
    return g[`plugin.${pack}.${key}`]
  }
  const pval = (pack: string, field: PluginConfigField) =>
    effectivePluginValue(field, rawPval(pack, field.key))
  const setPval = (pack: string, key: string, v: unknown) =>
    setPcfg((prev) => ({ ...prev, [pack]: { ...(prev[pack] ?? {}), [key]: v } }))
  const val = (k: string) => (g[k] == null ? '' : String(g[k]))
  const set = (k: string, v: string) => setG((prev) => ({ ...prev, [k]: v }))
  const dests = (Array.isArray(g.destinations) ? g.destinations : []) as DestinationPreset[]
  const savedDestinations = (Array.isArray(baseline?.global.destinations)
    ? baseline.global.destinations : []) as DestinationPreset[]
  const isSavedDestination = (destination: DestinationPreset) => savedDestinations.some(
    (saved) => saved.id === destination.id && sameJson(saved, destination),
  )
  const objectStoreCreds = creds.filter((c) => c.kind === 'object_store')
  const agentCreds = creds.filter((c) => c.kind === 'agent')
  const credName = (id?: string | null) => creds.find((c) => c.id === id)?.name
  const changes = useMemo(
    () => stagedSettings(baseline, g, u, pcfg, plugins, canGlobal),
    [baseline, canGlobal, g, pcfg, plugins, u],
  )
  const changesRef = useRef(changes)
  changesRef.current = changes
  const invalidPluginEdit = useMemo(() => hasInvalidPluginEdit(pcfg, plugins), [pcfg, plugins])
  const destRootError = destinationRootError(dest.backend, dest.root)
  const canAddDestination = Boolean(dest.name.trim() && dest.root.trim() && !destRootError)
  const destinationDraftDirty = dest.name !== '' || dest.root !== '' || dest.backend !== 'local' || dest.credId !== NO_CRED
  const originalCred = credForm.id ? creds.find((credential) => credential.id === credForm.id) : null
  const credentialDraftDirty = credForm.id
    ? Boolean(originalCred && (
      credForm.name !== originalCred.name
      || credForm.kind !== originalCred.kind
      || !sameJson(credForm.fields, originalCred.fields)
    ))
    : credForm.name !== '' || credForm.kind !== 'object_store' || Object.values(credForm.fields).some((value) => value !== '')
  const memberDraftDirty = newUser.name !== '' || newUser.password !== ''
  const dirty = changes.length > 0 || invalidPluginEdit || destinationDraftDirty || credentialDraftDirty || memberDraftDirty || Boolean(conflict)

  useEffect(() => {
    if (!dirty) return
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => window.removeEventListener('beforeunload', warnBeforeUnload)
  }, [dirty])

  const restoreEditingFocus = () => {
    const target = lastEditingControl.current
    requestAnimationFrame(() => {
      if (target?.isConnected) target.focus()
    })
  }
  const requestClose = () => {
    if (!dirty) { onClose(); return }
    if (!saving) setConfirmDiscard(true)
  }
  const keepEditing = () => setConfirmDiscard(false)
  const applySnapshot = (snapshot: SettingsSnapshot) => {
    setG(editableGlobal(snapshot))
    setU(snapshot.user)
    setBaseline(snapshot)
    setPcfg({})
  }
  const reapplyForReview = () => {
    if (!conflict) return
    const pluginEdits: PluginEdits = {}
    setG((current) => {
      const next = { ...current }
      for (const change of conflict.submitted) {
        if (change.scope !== 'global') continue
        next[change.key] = change.value
        if (change.key === 'agentDataPolicy' && change.value && typeof change.value === 'object') {
          const policy = change.value as { level?: unknown; endpointIsLocal?: unknown }
          next.agentDataPolicyLevel = String(policy.level || 'metadata-only')
          next.agentDataPolicyEndpointIsLocal = Boolean(policy.endpointIsLocal)
        }
        if (change.key.startsWith('plugin.')) {
          const plugin = plugins.find((candidate) => change.key.startsWith(`plugin.${candidate.name}.`))
          if (plugin) {
            const field = change.key.slice(`plugin.${plugin.name}.`.length)
            pluginEdits[plugin.name] = { ...(pluginEdits[plugin.name] ?? {}), [field]: change.value }
          }
        }
      }
      return next
    })
    setU((current) => {
      const next = { ...current }
      for (const change of conflict.submitted) if (change.scope === 'user') next[change.key] = change.value
      return next
    })
    setPcfg(pluginEdits)
    setConflict(null)
    setSaveFailure(null)
  }
  const save = async () => {
    if (loading || loadError || saving || invalidPluginEdit || conflict || !baseline || changes.length === 0) return
    const submitted = changes

    setSaving(true)
    setSavedMsg('')
    setSaveFailure(null)
    try {
      const result = await api.putSettingsBatch(baseline.revision, submitted)
      setBaseline((current) => {
        if (!current) return current
        const next = { global: { ...current.global }, user: { ...current.user }, revision: { ...current.revision } }
        const touched = new Set(submitted.map((change) => change.scope))
        for (const change of submitted) next[change.scope][change.key] = change.value
        if (touched.has('global')) next.revision.global = result.revision.global
        if (touched.has('user')) next.revision.user = result.revision.user
        return next
      })
      setSavedMsg('Saved'); setTimeout(() => setSavedMsg(''), 1400)
    } catch (e) {
      if (e instanceof KernelError && e.status === 409) {
        try {
          const latest = await api.getSettings()
          const serverChanged = submitted.filter((change) => !sameJson(
            baseline[change.scope][change.key], latest[change.scope][change.key],
          ))
          applySnapshot(latest)
          setConflict({ submitted, serverChanged })
          pushToast('Settings changed on the server. Review local values before saving again.', 'error')
          return
        } catch (refreshError) {
          const message = `Settings conflict could not be recovered: ${errorMessage(refreshError)}`
          setSaveFailure({ message })
          pushToast(message, 'error')
          return
        }
      }
      const message = `Settings were not saved: ${errorMessage(e)}`
      setSaveFailure({ message })
      pushToast(message, 'error')
    } finally {
      setSaving(false)
    }
  }
  const addDest = () => {
    if (!canAddDestination) return
    const name = dest.name.trim(), root = dest.root.trim()
    const id = `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${Math.abs(Math.floor(Math.random() * 1e6))}`
    const credId = dest.backend !== 'local' && dest.credId !== NO_CRED ? dest.credId : null
    setG((prev) => ({ ...prev, destinations: [...dests, { id, name, backend: dest.backend, root, credId }] }))
    setDest({ name: '', backend: 'local', root: '', credId: NO_CRED })
  }
  const testDestination = async (destination: DestinationPreset) => {
    if (destinationTestingId || !isSavedDestination(destination)) return
    setDestinationTestingId(destination.id)
    setDestinationNotices((current) => {
      const next = { ...current }
      delete next[destination.id]
      return next
    })
    try {
      const result = await api.browseDestination(destination.id, '')
      if (result.error) throw new Error(result.error)
      if (result.writable === false) throw new Error('This path cannot be used as a destination.')
      const count = result.entries.length
      const preview = result.entries.slice(0, 3).map((entry) => entry.name).join(', ')
      setDestinationNotices((current) => ({
        ...current,
        [destination.id]: {
          kind: 'success',
          message: count === 0
            ? 'No files found.'
            : `${count.toLocaleString()} item${count === 1 ? '' : 's'} · ${preview}${count > 3 ? '…' : ''}`,
        },
      }))
    } catch (error) {
      setDestinationNotices((current) => ({
        ...current,
        [destination.id]: { kind: 'error', message: `Could not browse this destination: ${errorMessage(error)}` },
      }))
    } finally {
      setDestinationTestingId(null)
    }
  }
  const setCredField = (k: string, v: string) => setCredForm((p) => ({ ...p, fields: { ...p.fields, [k]: v } }))
  const editCred = (c: Cred) => setCredForm({ id: c.id, name: c.name, kind: c.kind, fields: { ...c.fields } })
  const saveCred = async () => {
    const name = credForm.name.trim()
    if (!name || credentialSaving || credentialDeletingId) return
    // Send only non-empty reference fields; a blank field is omitted (keeps refs, never writes plaintext).
    const fields = Object.fromEntries(Object.entries(credForm.fields).filter(([, v]) => v.trim() !== ''))
    const isEdit = Boolean(credForm.id)
    setCredentialSaving(true)
    setCredentialNotice(null)
    try {
      const body = { name, kind: credForm.kind, fields }
      const saved = credForm.id ? await api.updateCred(credForm.id, body) : await api.createCred(body)
      setCreds((prev) => credForm.id ? prev.map((c) => c.id === saved.id ? saved : c) : [...prev, saved])
      setCredForm(emptyCredForm(credForm.kind))
      const message = `${isEdit ? 'Saved' : 'Added'} credential ${name}. This applied immediately; staged Settings are unchanged.`
      setCredentialNotice({ kind: 'success', message })
      pushToast(`Saved credential ${name}`, 'success')
    } catch (e) {
      const message = `Could not save credential ${name}: ${errorMessage(e)}`
      setCredentialNotice({ kind: 'error', message })
      pushToast(message, 'error')
    } finally { setCredentialSaving(false) }
  }
  const removeCred = async (c: Cred) => {
    if (credentialDeletingId || credentialSaving) return
    const destinationUsesCredential = dests.some((destination) => destination.credId === c.id)
      || (dest.backend !== 'local' && dest.credId === c.id)
    if (g.defaultObjectStoreCredId === c.id || g.agentCredId === c.id || destinationUsesCredential) {
      const message = `Credential ${c.name} is selected in Settings. Select a different credential (or None) and Save before removing it.`
      setCredentialNotice({ kind: 'error', message })
      pushToast(message, 'error')
      return
    }
    setCredentialDeletingId(c.id)
    setCredentialNotice(null)
    try {
      await api.deleteCred(c.id)
      setCreds((prev) => prev.filter((x) => x.id !== c.id))
      if (credForm.id === c.id) setCredForm(emptyCredForm(credForm.kind))
      setCredentialNotice({ kind: 'success', message: `Removed credential ${c.name}. This applied immediately; staged Settings are unchanged.` })
      pushToast(`Removed credential ${c.name}`, 'success')
    } catch (e) {
      const message = `Could not remove credential ${c.name}: ${errorMessage(e)}`
      setCredentialNotice({ kind: 'error', message })
      pushToast(message, 'error')
    } finally { setCredentialDeletingId(null) }
  }
  const restartKernel = async () => {
    if (kernelRestarting) return
    setKernelRestarting(true)
    setKernelNotice(null)
    try {
      const result = await api.restartKernel(canvasId)
      const message = result.restarted
        ? 'Kernel restart requested. This applied immediately; staged Settings are unchanged.'
        : 'No live kernel to restart. The next run starts fresh; staged Settings are unchanged.'
      setKernelNotice({ kind: 'success', message })
      pushToast(result.restarted ? 'Kernel restarting…' : 'No live kernel — a fresh one starts on the next run', 'success')
    } catch (e) {
      const message = `Could not restart kernel: ${errorMessage(e)}`
      setKernelNotice({ kind: 'error', message })
      pushToast(message, 'error')
    } finally { setKernelRestarting(false) }
  }
  const pluginSettingKey = (pack: string, field: string) => `plugin.${pack}.${field}`
  const setPluginSecretNotice = (key: string, notice: ActionNotice) =>
    setPluginSecretNotices((current) => ({ ...current, [key]: notice }))
  const applySnapshotPreservingChanges = (snapshot: SettingsSnapshot, staged: SettingChange[]) => {
    const nextGlobal = editableGlobal(snapshot)
    const nextUser = { ...snapshot.user }
    for (const change of staged) {
      if (change.scope === 'user') {
        nextUser[change.key] = change.value
        continue
      }
      // Plugin drafts already live in pcfg. Keep the refreshed value in g as their truthful baseline.
      if (change.key.startsWith('plugin.')) continue
      nextGlobal[change.key] = change.value
      if (change.key === 'agentDataPolicy' && change.value && typeof change.value === 'object') {
        const policy = change.value as { level?: unknown; endpointIsLocal?: unknown }
        nextGlobal.agentDataPolicyLevel = String(policy.level || 'metadata-only')
        nextGlobal.agentDataPolicyEndpointIsLocal = Boolean(policy.endpointIsLocal)
      }
    }
    setG(nextGlobal)
    setU(nextUser)
    setBaseline(snapshot)
  }
  const finishPluginSecretClear = (
    target: PluginSecretTarget,
    revision: SettingsSnapshot['revision'],
    message: string,
  ) => {
    const key = pluginSettingKey(target.pack, target.field.key)
    setG((current) => ({ ...current, [key]: '' }))
    setBaseline((current) => current ? {
      ...current,
      global: { ...current.global, [key]: '' },
      revision: { ...revision },
    } : current)
    setPcfg((current) => {
      const fields = current[target.pack]
      if (!fields || !hasOwn(fields, target.field.key)) return current
      const nextFields = { ...fields }
      delete nextFields[target.field.key]
      const next = { ...current }
      if (Object.keys(nextFields).length) next[target.pack] = nextFields
      else delete next[target.pack]
      return next
    })
    setPluginSecretNotice(key, { kind: 'success', message })
  }
  const clearPluginSecret = async (target: PluginSecretTarget) => {
    const key = pluginSettingKey(target.pack, target.field.key)
    if (!baseline || saving || pluginSecretClearingKey) return
    const expectedRevision = baseline.revision
    const expectedValue = baseline.global[key]
    setPluginSecretTarget(null)
    setPluginSecretClearingKey(key)
    setPluginSecretNotices((current) => {
      const next = { ...current }
      delete next[key]
      return next
    })
    try {
      const result = await api.putSettingsBatch(expectedRevision, [
        { scope: 'global', key, value: '' },
      ])
      finishPluginSecretClear(
        target,
        result.revision,
        `${target.field.label} now uses its environment/default value. This applied immediately; staged Settings are unchanged.`,
      )
      pushToast(`Cleared ${target.field.label} stored reference`, 'success')
    } catch (error) {
      try {
        const latest = await api.getSettings()
        applySnapshotPreservingChanges(latest, changesRef.current)
        const latestValue = latest.global[key]
        if (latestValue == null || latestValue === '') {
          finishPluginSecretClear(
            target,
            latest.revision,
            `${target.field.label} was already cleared. It now uses its environment/default value; staged Settings are unchanged.`,
          )
        } else {
          const conflict = error instanceof KernelError && error.status === 409
          const targetChanged = !sameJson(expectedValue, latestValue)
          const message = conflict && targetChanged
            ? `${target.field.label} changed on the server and was not cleared. Review the current state, then choose Clear again.`
            : conflict
              ? `Settings changed on the server before ${target.field.label} could be cleared. The stored reference is still set; choose Clear again to retry.`
            : `Could not clear ${target.field.label}: ${errorMessage(error)}. The stored reference is still set; choose Clear again to retry.`
          setPluginSecretNotice(key, { kind: 'error', message })
          pushToast(message, 'error')
        }
      } catch (refreshError) {
        const message = `Could not confirm whether ${target.field.label} was cleared: ${errorMessage(refreshError)}. Reload Settings before retrying.`
        setPluginSecretNotice(key, { kind: 'error', message })
        pushToast(message, 'error')
      }
    } finally {
      setPluginSecretClearingKey(null)
    }
  }
  const retryPlugins = async () => {
    if (pluginReloading) return
    setPluginReloading(true)
    try {
      setPlugins(await api.plugins())
      setPluginLoadError('')
    } catch (error) {
      setPluginLoadError(errorMessage(error))
    } finally {
      setPluginReloading(false)
    }
  }
  const pluginConfigFields = (plugin: PluginInfo) => plugin.config?.map((field) => {
    const settingKey = pluginSettingKey(plugin.name, field.key)
    const storedRef = g[settingKey]
    const isSet = storedRef != null && storedRef !== ''
    const stagedSecret = field.secret && pcfg[plugin.name] && hasOwn(pcfg[plugin.name], field.key)
      ? String(pcfg[plugin.name][field.key] ?? '').trim()
      : ''
    const clearing = pluginSecretClearingKey === settingKey
    const secretNotice = pluginSecretNotices[settingKey]
    const placeholder = field.placeholder ?? (field.secret
      ? (isSet ? String(storedRef ?? 'env:VAR or file:/path') : 'env:VAR or file:/path')
      : (field.default != null ? String(field.default) : ''))
    return (
      <Field key={field.key} label={field.label}>
        {field.type === 'select' && field.options ? (
          <Select value={String(pval(plugin.name, field))} onValueChange={(value) => setPval(plugin.name, field.key, value)}>
            <SelectTrigger aria-label={field.label}><SelectValue placeholder={placeholder} /></SelectTrigger>
            <SelectContent>{field.options.map((option) => <SelectItem key={option} value={option}>{option}</SelectItem>)}</SelectContent>
          </Select>
        ) : field.type === 'bool' ? (
          <Select value={String(pval(plugin.name, field))} onValueChange={(value) => setPval(plugin.name, field.key, value)}>
            <SelectTrigger aria-label={field.label}><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="true">true</SelectItem><SelectItem value="false">false</SelectItem></SelectContent>
          </Select>
        ) : (
          <Input
            type={field.type === 'int' || field.type === 'float' ? 'number' : 'text'}
            disabled={field.secret && clearing}
            value={field.secret
              ? String(pcfg[plugin.name]?.[field.key] ?? storedRef ?? '')
              : String(pval(plugin.name, field))}
            placeholder={placeholder}
            aria-label={field.label}
            onChange={(event) => setPval(plugin.name, field.key, event.target.value)}
          />
        )}
        {!field.secret && <div className="mt-1 flex items-center gap-2 text-[10.5px] text-muted-foreground">
          {rawPval(plugin.name, field.key) == null || rawPval(plugin.name, field.key) === ''
            ? <span>Using environment/default.</span>
            : <Button variant="link" className="h-auto p-0 text-[10.5px]" onClick={() => setPval(plugin.name, field.key, null)}>Use environment/default</Button>}
        </div>}
        {!field.secret && pcfg[plugin.name] && hasOwn(pcfg[plugin.name], field.key) && !canonicalPluginValue(field, rawPval(plugin.name, field.key)).valid && <div className="mt-1 text-[10.5px] text-destructive">Enter a finite {field.type === 'int' ? 'integer' : 'number'}.</div>}
        {field.secret && <>
          <div className="mt-1 text-[10.5px] text-muted-foreground">Secret reference only (`env:VAR` / `file:/path`). Blank on Save leaves the stored reference unchanged.</div>
          <div className="mt-1 flex items-center gap-2 text-[10.5px] text-muted-foreground">
            {isSet ? <Button
              variant="link"
              className="h-auto p-0 text-[10.5px]"
              disabled={saving || clearing || Boolean(pluginSecretClearingKey) || Boolean(stagedSecret)}
              onClick={() => setPluginSecretTarget({ pack: plugin.name, field })}
            >{clearing ? 'Clearing…' : 'Clear…'}</Button> : <span>Using environment/default.</span>}
            <span>{isSet ? 'Clearing applies immediately; it does not wait for Save.' : 'No stored reference.'}</span>
          </div>
          {stagedSecret && isSet && <div className="mt-1 text-[10.5px] text-muted-foreground">Blank the staged replacement before clearing the stored reference.</div>}
          {secretNotice && <div role={secretNotice.kind === 'error' ? 'alert' : 'status'} className={cn('mt-1 text-[10.5px]', secretNotice.kind === 'error' ? 'text-destructive' : 'text-green-600')}>
            {secretNotice.message}
          </div>}
        </>}
        {field.help && <div className="mt-1 text-[10.5px] text-muted-foreground">{field.help}</div>}
      </Field>
    )
  })
  const go = (id: string) => setActive(id)  // master-detail: the nav switches the visible pane
  const runners = kernelInfo?.runners ?? ['local-out-of-core']
  // /settings carries explicit user/workspace choices, but not the deployment's DP_EXECUTION
  // override. Do not guess the deployment default from registration order: this action is only
  // meaningful when Settings explicitly selects the kernel.
  const selectedRunner = u.backend && u.backend !== INHERIT
    ? String(u.backend)
    : g.backend ? String(g.backend) : null

  return (
    <Dialog open onOpenChange={(o) => { if (!o) requestClose() }}>
      <DialogContent data-testid="settings-modal" onFocusCapture={(event) => {
        const target = event.target
        if (target instanceof HTMLElement && target.matches('input, textarea, select, [role="combobox"]')) {
          lastEditingControl.current = target
        }
      }} onCloseAutoFocus={(event) => event.preventDefault()} className="dp-modal-overlay flex flex-col gap-0 overflow-hidden p-0 w-[94vw] max-w-[940px] h-[min(680px,90vh)]">
        {/* header */}
        <div className="flex items-center gap-2 border-b border-border py-3 pl-[18px] pr-12">
          <span className="flex items-center text-muted-foreground"><Icon name="settings" size={15} /></span>
          <DialogTitle className="text-[15px] font-bold">Settings</DialogTitle>
          <span className="flex-1" />
          <span role="status" aria-live="polite" className={cn('text-[11.5px]', dirty ? 'text-amber-700 dark:text-amber-300' : 'text-green-600')}>
            {changes.length ? `${changes.length} unsaved change${changes.length === 1 ? '' : 's'}` : dirty ? 'Unsaved draft' : savedMsg}
          </span>
          <Button size="sm" onClick={save} disabled={loading || Boolean(loadError) || saving || Boolean(pluginSecretClearingKey) || invalidPluginEdit || Boolean(conflict) || changes.length === 0}>{saving ? 'Saving…' : 'Save'}</Button>
        </div>
        <DialogDescription className="sr-only">Application and workspace settings: the agent model, default compute target, and output destinations.</DialogDescription>

        {conflict && (
          <div data-testid="settings-conflict-recovery" role="alert" className="flex items-center gap-3 border-b border-amber-500/30 bg-amber-500/5 px-[18px] py-2 text-[11.5px] text-foreground">
            <div className="min-w-0 flex-1">
              <div className="font-medium">Settings changed on the server.</div>
              <div className="mt-0.5 text-[10.5px] text-muted-foreground">
                {conflict.serverChanged.length
                  ? `Server changed: ${conflict.serverChanged.map(settingLabel).join(', ')}.`
                  : 'The server did not change the settings you touched.'}
                {' '}Local values are not saved until you reapply and Save again.
              </div>
            </div>
            <Button variant="outline" size="sm" onClick={() => { setConflict(null); setSaveFailure(null) }}>Discard local values</Button>
            <Button size="sm" onClick={reapplyForReview}>Reapply local values for review</Button>
          </div>
        )}

        {saveFailure && !conflict && (
          <div role="alert" className="flex items-center gap-3 border-b border-destructive/30 bg-destructive/5 px-[18px] py-2 text-[11.5px] text-destructive">
            <div className="min-w-0 flex-1">
              <div>{saveFailure.message}</div>
              <div className="mt-0.5 text-[10.5px]">The save was not confirmed. Settings are never partially committed; your edits remain here.</div>
            </div>
            <Button variant="outline" size="sm" onClick={save} disabled={saving}>Retry save</Button>
          </div>
        )}

        <div className="flex min-h-0 flex-1">
          {/* left category nav */}
          <nav className="flex w-[190px] shrink-0 flex-col gap-0.5 border-r border-border p-3">
            {categories.map((c) => (
              <button key={c.id} autoFocus={active === c.id} onClick={() => go(c.id)}
                className={cn('flex items-center gap-[9px] rounded-md px-2.5 py-2 text-left text-[12.5px] font-medium transition-colors',
                  active === c.id ? 'bg-accent text-foreground' : 'text-muted-foreground hover:bg-accent/50')}>
                <Icon name={c.icon} size={14} /> {c.label}
              </button>
            ))}
          </nav>

          {/* content — only the active pane renders (master-detail); the nav switches panes */}
          <div className="min-w-0 flex-1 overflow-y-auto px-[22px] py-[18px]">
            {loading ? <div className="text-[12.5px] text-muted-foreground">loading…</div> : loadError ? (
              <div role="alert" className="mx-auto flex h-full max-w-[440px] flex-col items-center justify-center text-center">
                <div className="text-[13px] font-semibold text-foreground">Settings could not be loaded</div>
                <div className="mt-1.5 text-[11.5px] leading-relaxed text-destructive">{loadError}</div>
                <div className="mt-2 text-[10.5px] leading-relaxed text-muted-foreground">The editor is blocked so unavailable data is never replaced with empty defaults.</div>
                <Button variant="outline" size="sm" className="mt-4" onClick={() => setLoadAttempt((n) => n + 1)}>Retry loading</Button>
              </div>
            ) : (
              <div className="flex flex-col gap-[26px]">
                {canGlobal && active === 'agent' && <Section id="agent" title="Agent">
                  <Field label="Model"><Input value={val('agentModel')} placeholder="anthropic/claude-opus-4-8" onChange={(e) => set('agentModel', e.target.value)} /></Field>
                  <div className="-mt-1 mb-2 text-[10.5px] text-muted-foreground">e.g. anthropic/claude-opus-4-8 · openai/gpt-5 · google/gemini-2.5-pro · ollama/llama3.3</div>
                  <Field label="API key credential">
                    <Select value={g.agentCredId ? String(g.agentCredId) : NO_CRED} onValueChange={(v) => set('agentCredId', v === NO_CRED ? '' : v)}>
                      <SelectTrigger aria-label="Agent credential"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value={NO_CRED}>None (use the provider&apos;s env var)</SelectItem>
                        {agentCreds.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </Field>
                  <div className="-mt-1 mb-2 text-[10.5px] text-muted-foreground">Choose a credential from Credentials. Only an environment variable or file reference is stored.</div>
                  <Field label="Base URL"><Input value={val('agentBaseUrl')} placeholder="http://localhost:11434 (optional)" onChange={(e) => set('agentBaseUrl', e.target.value)} /></Field>
                  <Field label="Data policy">
                    <Select
                      value={String(g.agentDataPolicyLevel || 'metadata-only')}
                      onValueChange={(v) => setG((prev) => ({ ...prev, agentDataPolicyLevel: v }))}
                    >
                      <SelectTrigger aria-label="Data policy"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="metadata-only">Metadata only</SelectItem>
                        <SelectItem value="sample-values">Include preview values (up to 8 rows)</SelectItem>
                      </SelectContent>
                    </Select>
                  </Field>
                  <div className="-mt-1 mb-2 text-[10.5px] text-muted-foreground">
                    Metadata only sends column names and types. Include preview values only when this model endpoint may receive sample data.
                  </div>
                  <label className="mb-2 flex items-start gap-2 text-[11.5px] text-foreground">
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={Boolean(g.agentDataPolicyEndpointIsLocal)}
                      onChange={(e) => setG((prev) => ({ ...prev, agentDataPolicyEndpointIsLocal: e.target.checked }))}
                    />
                    <span>
                        Treat this Base URL as local or self-hosted
                      <span className="mt-0.5 block text-[10.5px] text-muted-foreground">
                        When set, sample values may reach that endpoint without the sample-values opt-in.
                        Allows preview values to reach this endpoint without changing the data policy above.
                      </span>
                    </span>
                  </label>
                </Section>}

                {active === 'execution' && <Section id="execution" title="Compute defaults">
                  {!canGlobal && <div className="mb-3 rounded-md border border-border bg-muted/40 p-2.5 text-[10.5px] text-muted-foreground">Workspace-wide defaults are managed by an administrator. Choose a target for the current Canvas from its top bar.</div>}
                  {selectedRunner === 'kernel' && (
                    <div className="mt-2 flex items-center gap-2">
                      <Button variant="outline" size="sm" onClick={restartKernel} disabled={kernelRestarting}>{kernelRestarting ? 'Restarting…' : 'Restart kernel'}</Button>
                      <span className="text-[10.5px] text-muted-foreground">Applies immediately; it does not save staged Settings. Clears this canvas's warm kernel; the next run starts fresh.</span>
                    </div>
                  )}
                  {kernelNotice && <div role={kernelNotice.kind === 'error' ? 'alert' : 'status'} className={cn('mt-2 text-[10.5px]', kernelNotice.kind === 'error' ? 'text-destructive' : 'text-green-600')}>
                    {kernelNotice.message}
                  </div>}

                  <div className="mb-1.5 text-[11.5px] font-semibold text-foreground">Default compute target</div>
                  <p className="mb-2 text-[10.5px] leading-relaxed text-muted-foreground">
                    Change a specific Canvas from its top bar.
                  </p>
                  <div role="group" aria-label="Compute target" className="flex flex-col gap-1.5">
                    <button
                      type="button"
                      aria-label="Use Automatic execution"
                      aria-pressed={!u.backend || u.backend === INHERIT}
                      onClick={() => setU((current) => ({ ...current, backend: INHERIT }))}
                      className={cn(
                        'rounded-md border px-2.5 py-2 text-left transition-colors hover:bg-accent/50',
                        !u.backend || u.backend === INHERIT ? 'border-foreground bg-accent/40' : 'border-border',
                      )}
                    >
                      <div className="flex items-baseline gap-1.5 text-xs font-semibold text-foreground">
                        <Icon name="sparkle" size={12} /> Automatic
                        {!u.backend || u.backend === INHERIT
                          ? <Badge variant="secondary" className="ml-auto rounded px-1.5 py-0 text-[10px] font-normal">Recommended</Badge>
                          : <span className="ml-auto text-[10.5px] font-medium text-muted-foreground">Use</span>}
                      </div>
                      <div className="mt-1 text-[10.5px] leading-snug text-muted-foreground">Uses the workspace default.</div>
                    </button>
                    {runners.map((runner) => (
                      <button
                        key={runner}
                        type="button"
                        aria-label={`Use ${runnerLabel(runner)}`}
                        aria-pressed={String(u.backend ?? '') === runner}
                        onClick={() => setU((current) => ({ ...current, backend: runner }))}
                        className={cn(
                          'rounded-md border px-2.5 py-2 text-left transition-colors hover:bg-accent/50',
                          String(u.backend ?? '') === runner ? 'border-foreground bg-accent/40' : 'border-border',
                        )}
                      >
                        <div className="flex items-baseline gap-1.5 text-xs font-semibold text-foreground">
                          <Icon name="server" size={12} /> {runnerLabel(runner)}
                          {String(u.backend ?? '') === runner
                            ? <Badge variant="secondary" className="ml-auto rounded px-1.5 py-0 text-[10px] font-normal">Selected</Badge>
                            : <span className="ml-auto text-[10.5px] font-medium text-muted-foreground">Use</span>}
                        </div>
                        <div className="mt-1 text-[10.5px] leading-snug text-muted-foreground">{runnerGuidance(runner)}</div>
                      </button>
                    ))}
                    {runners.length === 0 && <div className="text-[11.5px] text-muted-foreground">No compute targets are available.</div>}
                  </div>
                </Section>}

                {canGlobal && active === 'destinations' && <Section id="destinations" title="Destinations">
                  <p className="mb-2 text-[11.5px] leading-relaxed text-muted-foreground">Save locations for Canvas outputs.</p>
                  <div className="mb-2 flex flex-col gap-1">
                    {dests.map((d, i) => (
                      <div key={d.id} className="rounded-md border border-border px-2.5 py-2">
                        <div className="flex items-center gap-2 text-xs text-foreground">
                          <span className="flex items-center text-muted-foreground"><Icon name="export" size={12} /></span>
                          <span className="font-semibold">{d.name}</span>
                          <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">{d.backend}</Badge>
                          {d.credId && <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">{credName(d.credId) ?? 'credential'}</Badge>}
                          <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[11px] text-muted-foreground">{d.root}</span>
                          {isSavedDestination(d) ? <Button variant="outline" size="sm" aria-label={`Preview files in ${d.name}`} disabled={Boolean(destinationTestingId)} onClick={() => void testDestination(d)}>
                            {destinationTestingId === d.id ? 'Loading preview…' : 'Preview files'}
                          </Button> : <span className="text-[10px] text-muted-foreground">Save to preview</span>}
                          <button onClick={() => setG((prev) => ({ ...prev, destinations: dests.filter((_, j) => j !== i) }))}
                            aria-label={`Remove destination ${d.name}`}
                            className="grid place-items-center text-muted-foreground transition-colors hover:text-foreground"><Icon name="close" size={12} /></button>
                        </div>
                        {destinationNotices[d.id] && <div role={destinationNotices[d.id].kind === 'error' ? 'alert' : 'status'} className={cn('mt-1.5 text-[10.5px]', destinationNotices[d.id].kind === 'error' ? 'text-destructive' : 'text-green-600')}>
                          {destinationNotices[d.id].message}
                        </div>}
                      </div>
                    ))}
                    {dests.length === 0 && <div className="text-[11.5px] text-muted-foreground">No custom destinations.</div>}
                  </div>
                  <div className="mb-1.5 text-[11.5px] font-semibold text-foreground">Add destination</div>
                  <div className="flex gap-1.5">
                    <Input value={dest.name} onChange={(e) => setDest({ ...dest, name: e.target.value })} placeholder="e.g. S3 exports" className="w-[120px] shrink-0" aria-label="Destination name" />
                    <Select value={dest.backend} onValueChange={(v) => setDest({ ...dest, backend: v, credId: v === 'local' ? NO_CRED : dest.credId })}>
                      <SelectTrigger className="w-[84px] shrink-0" aria-label="Destination backend"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="local">local</SelectItem>
                        <SelectItem value="s3">s3</SelectItem>
                        <SelectItem value="gs">gs</SelectItem>
                      </SelectContent>
                    </Select>
                    <Input value={dest.root} onChange={(e) => setDest({ ...dest, root: e.target.value })} onKeyDown={(e) => { if (e.key === 'Enter' && canAddDestination) addDest() }}
                      aria-label="Destination root or prefix" aria-invalid={Boolean(destRootError)} aria-describedby={destRootError ? 'destination-root-error' : undefined}
                      placeholder={dest.backend === 'local' ? '/path/to/dir' : `${dest.backend}://bucket/prefix`}
                      className="min-w-0 flex-1" />
                    <Button onClick={addDest} disabled={!canAddDestination} className="shrink-0">Add</Button>
                  </div>
                  {destRootError && <div id="destination-root-error" role="alert" className="mt-1.5 text-[10.5px] text-destructive">{destRootError}</div>}
                  {dest.backend !== 'local' && (
                    <div className="mt-1.5">
                      <Select value={dest.credId} onValueChange={(v) => setDest({ ...dest, credId: v })}>
                        <SelectTrigger className="w-full" aria-label="Destination credential"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value={NO_CRED}>Default credential</SelectItem>
                          {objectStoreCreds.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}
                        </SelectContent>
                      </Select>
                      <div className="mt-1 text-[10.5px] text-muted-foreground">The object-store credential used to browse and write here. Manage credentials in the Credentials pane.</div>
                      <div className="mt-1 text-[10.5px] text-amber-700 dark:text-amber-300">In an authenticated workspace that started with no object store, external file access is fixed when the Data Playground server starts. Restart the Data Playground server after adding this destination; restarting only the canvas kernel is not enough.</div>
                    </div>
                  )}
                </Section>}

                {canGlobal && active === 'credentials' && <Section id="credentials" title="Credentials">
                  <p className="mb-2 text-[11.5px] leading-relaxed text-muted-foreground">
                    Named credentials a destination or the agent references. Fields store references (`env:VAR` / `file:/path`), never the secret bytes.
                  </p>
                  <div className="mb-2 text-[10.5px] text-muted-foreground">Credential changes apply immediately; they do not wait for Save or change other staged Settings.</div>
                  <div className="mb-3 flex flex-col gap-1">
                    {creds.map((c) => (
                      <div key={c.id} className="flex items-center gap-2 text-xs text-foreground">
                        <span className="flex items-center text-muted-foreground"><Icon name="link" size={12} /></span>
                        <span className="font-semibold">{c.name}</span>
                        <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">{c.kind === 'object_store' ? 'object store' : 'agent'}</Badge>
                        {c.kind === 'object_store' && g.defaultObjectStoreCredId === c.id && <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">default</Badge>}
                        <span className="flex-1" />
                        {c.kind === 'object_store' && g.defaultObjectStoreCredId !== c.id && (
                          <button onClick={() => setG((prev) => ({ ...prev, defaultObjectStoreCredId: c.id }))} disabled={credentialSaving || Boolean(credentialDeletingId)}
                            className="text-[10.5px] text-muted-foreground transition-colors hover:text-foreground">Make default</button>
                        )}
                        <button onClick={() => editCred(c)} disabled={credentialSaving || Boolean(credentialDeletingId)} aria-label={`Edit credential ${c.name}`}
                          className="grid place-items-center text-muted-foreground transition-colors hover:text-foreground"><Icon name="rename" size={12} /></button>
                        <button onClick={() => removeCred(c)} disabled={credentialSaving || Boolean(credentialDeletingId)} aria-label={`Remove credential ${c.name}`}
                          className="grid place-items-center text-muted-foreground transition-colors hover:text-foreground"><Icon name="close" size={12} /></button>
                      </div>
                    ))}
                    {creds.length === 0 && <div className="text-[11.5px] text-muted-foreground">No credentials yet.</div>}
                  </div>

                  <div className="rounded-md border border-border p-3">
                    <div className="mb-2 text-[12px] font-semibold text-foreground">{credForm.id ? 'Edit credential' : 'New credential'}</div>
                    <div className="mb-2 flex gap-1.5">
                      <Input value={credForm.name} onChange={(e) => setCredForm((p) => ({ ...p, name: e.target.value }))} placeholder="Name" className="min-w-0 flex-1" aria-label="Credential name" />
                      <Select value={credForm.kind} onValueChange={(v) => setCredForm({ id: credForm.id, name: credForm.name, kind: v as CredKind, fields: {} })}>
                        <SelectTrigger className="w-[130px] shrink-0" aria-label="Credential kind"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="object_store">object store</SelectItem>
                          <SelectItem value="agent">agent</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    {credForm.kind === 'object_store' ? (
                      <div className="grid grid-cols-2 gap-1.5">
                        {OBJECT_STORE_FIELDS.map((f) => (
                          <Input key={f.key} value={credForm.fields[f.key] ?? ''} placeholder={f.placeholder} aria-label={f.key}
                            onChange={(e) => setCredField(f.key, e.target.value)} />
                        ))}
                      </div>
                    ) : (
                      <Input value={credForm.fields.apiKey ?? ''} placeholder="env:ANTHROPIC_API_KEY or file:/run/secrets/agent_key" aria-label="apiKey"
                        onChange={(e) => setCredField('apiKey', e.target.value)} />
                    )}
                    <div className="mt-1.5 text-[10.5px] text-muted-foreground">References only (`env:VAR` / `file:/path`). A blank field is left unchanged; leave all blank to use the environment.</div>
                    <div className="mt-2 flex gap-1.5">
                      <Button onClick={saveCred} disabled={!credForm.name.trim() || credentialSaving || Boolean(credentialDeletingId)} className="shrink-0">{credentialSaving ? 'Saving credential…' : credForm.id ? 'Save credential' : 'Add credential'}</Button>
                      {credForm.id && <Button variant="outline" onClick={() => setCredForm(emptyCredForm(credForm.kind))} disabled={credentialSaving} className="shrink-0">Cancel</Button>}
                    </div>
                    {credentialNotice && <div role={credentialNotice.kind === 'error' ? 'alert' : 'status'} className={cn('mt-2 text-[10.5px]', credentialNotice.kind === 'error' ? 'text-destructive' : 'text-green-600')}>
                      {credentialNotice.message}
                    </div>}
                  </div>
                </Section>}

                {canGlobal && active === 'plugins' && <Section id="plugins" title="Plugins">
                  {pluginLoadError && <div role="alert" className="mb-2.5 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-[11.5px] text-destructive">
                    Extensions could not be loaded: {pluginLoadError}{' '}
                    <button type="button" className="font-semibold underline disabled:opacity-50" disabled={pluginReloading} onClick={() => void retryPlugins()}>{pluginReloading ? 'Retrying…' : 'Retry'}</button>
                  </div>}
                  <div className="mb-2.5 flex flex-col gap-2">
                    {plugins.map((p, index) => {
                      const state = pluginState(p)
                      const failure = p.failure_summary ?? p.error
                      const capabilities = p.effective_capabilities ?? []
                      return (
                      <div key={`${p.source}:${p.name}:${index}`} data-testid={`plugin-status-${p.name}`} className="rounded-md border border-border p-2.5 text-xs text-foreground">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="flex items-center text-muted-foreground"><Icon name={state === 'active' ? 'check' : state === 'inactive' ? 'minus' : 'close'} size={12} /></span>
                          <span className="font-semibold">{p.name}</span>
                          <Badge variant="outline" className={cn('rounded border-0 px-1.5 py-0 text-[10px] font-medium', pluginStateTone[state])}>{state}</Badge>
                        </div>
                        {pluginStateCopy[state] && <div className="mt-1.5 text-[10.5px] text-muted-foreground">
                          {pluginStateCopy[state]}
                        </div>}
                        {capabilities.length > 0 && <div className="mt-2">
                          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Available features</div>
                          <div className="flex flex-wrap gap-1">
                            {capabilities.map((capability) => <Badge key={capability} variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">{capabilityLabel(capability)}</Badge>)}
                          </div>
                        </div>}
                        {failure && <div className={cn('mt-1.5 text-[10.5px]', state === 'degraded' ? 'text-amber-700 dark:text-amber-300' : 'text-destructive')}>
                          {failure} {p.failure_impact === 'optional-degradation' && 'Other parts of Data Playground still work.'}
                        </div>}
                        <div className="mt-2 text-[10.5px] font-medium text-foreground">{pluginActionCopy(p, state)}</div>
                        {(p.config?.length ?? 0) > 0 && <div className="mt-3 border-t border-border pt-3">
                          <div className="mb-2 flex items-center gap-1.5 text-[11.5px] font-semibold text-foreground"><Icon name="settings" size={12} /> Setup</div>
                          {pluginConfigFields(p)}
                        </div>}
                        <details className="mt-2 border-t border-border pt-2 text-[10px] text-muted-foreground">
                          <summary className="w-fit cursor-pointer select-none font-medium hover:text-foreground">Installation details</summary>
                          <div className="mt-2 grid gap-1">
                            <div>Package: {p.package || p.name}{p.version ? ` · ${p.version}` : ''}</div>
                            <div>Source: {p.source}</div>
                            <div>Starts with: {(p.process_placement?.length ?? 0) > 0 ? p.process_placement!.join(', ') : 'no active process'}</div>
                            <div>Features: {(p.effective_capabilities?.length ?? 0) > 0 ? p.effective_capabilities!.join(', ') : 'none'}</div>
                            {p.required && <div>Required when Data Playground starts.</div>}
                          </div>
                        </details>
                      </div>
                      )
                    })}
                    {!pluginLoadError && plugins.length === 0 && <div className="rounded-md border border-dashed border-border p-3 text-[11.5px] text-muted-foreground">No extensions installed.</div>}
                  </div>
                </Section>}

                {canGlobal && active === 'members' && <Section id="members" title="Members">
                  <p className="mb-2 text-[11.5px] leading-relaxed text-muted-foreground">
                    Canvas collaborators.
                    {authEnabled
                      ? ' Creating a member also creates their sign-in account.'
                      : ' Sign-in is off, so adding a name does not grant access.'}
                  </p>
                  {authEnabled && <div className="mb-2 rounded-md border border-border bg-muted/40 p-2.5 text-[10.5px] leading-relaxed text-muted-foreground">
                    Set an initial password. The member can change it after signing in.
                  </div>}
                  <div className="mb-2.5 flex flex-col gap-1">
                    {users.map((usr) => (
                      <div key={usr.id} className="flex items-center gap-2 text-xs text-foreground">
                        <span className="grid h-[22px] w-[22px] place-items-center rounded-full bg-muted text-[10px] font-bold text-muted-foreground">{usr.name.slice(0, 1).toUpperCase()}</span>
                        <span className="flex-1">{usr.name}</span>
                        {usr.id === currentUser?.id && <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">you</Badge>}
                      </div>
                    ))}
                  </div>
                  <div className="flex gap-1.5">
                    <Input value={newUser.name} onChange={(e) => setNewUser({ ...newUser, name: e.target.value })}
                      onKeyDown={(e) => { if (e.key === 'Enter' && !authEnabled) void addUser() }}
                      aria-label="Member name" placeholder="Name" className="w-[150px] shrink-0" />
                    {authEnabled && <Input type="password" value={newUser.password} onChange={(e) => setNewUser({ ...newUser, password: e.target.value })}
                        onKeyDown={(e) => { if (e.key === 'Enter') void addUser() }}
                        aria-label="Initial password" placeholder="Initial password (at least 6 characters)" className="min-w-0 flex-1" />}
                    <Button onClick={() => void addUser()} disabled={!newUser.name.trim() || (authEnabled && newUser.password.length < 6) || memberAdding} className="shrink-0">{memberAdding ? 'Adding…' : authEnabled ? 'Create account' : 'Add member'}</Button>
                  </div>
                  {authEnabled && newUser.password.length > 0 && newUser.password.length < 6 && <div role="alert" className="mt-1.5 text-[10.5px] text-destructive">Password must be at least 6 characters.</div>}
                  {memberNotice && <div role={memberNotice.kind === 'error' ? 'alert' : 'status'} className={cn('mt-2 text-[10.5px]', memberNotice.kind === 'error' ? 'text-destructive' : 'text-green-600')}>
                    {memberNotice.message}
                  </div>}
                </Section>}
              </div>
            )}
          </div>
        </div>
      </DialogContent>
      <Dialog open={confirmDiscard} onOpenChange={(open) => { if (!open) keepEditing() }}>
        <DialogContent data-testid="settings-discard-confirmation" onCloseAutoFocus={(event) => {
          event.preventDefault()
          restoreEditingFocus()
        }} className="max-w-[390px]">
          <DialogTitle>Discard unsaved Settings changes?</DialogTitle>
          <DialogDescription>Your edits have not been saved. Keep editing to review them, or discard them and close Settings.</DialogDescription>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={keepEditing}>Keep editing</Button>
            <Button variant="destructive" onClick={onClose}>Discard</Button>
          </div>
        </DialogContent>
      </Dialog>
      <Dialog open={Boolean(pluginSecretTarget)} onOpenChange={(open) => { if (!open) setPluginSecretTarget(null) }}>
        <DialogContent className="max-w-[410px]">
          <DialogTitle>Clear stored plugin secret reference?</DialogTitle>
          <DialogDescription>
            {pluginSecretTarget && <>This immediately removes only the stored <strong>{pluginSecretTarget.field.label}</strong> reference for <strong>{pluginSecretTarget.pack}</strong>. It does not save or discard other staged Settings. The field will use its environment/default value.</>}
          </DialogDescription>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setPluginSecretTarget(null)}>Cancel</Button>
            <Button variant="destructive" onClick={() => { if (pluginSecretTarget) void clearPluginSecret(pluginSecretTarget) }}>Clear stored reference</Button>
          </div>
        </DialogContent>
      </Dialog>
    </Dialog>
  )
}

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <div id={`set-${id}`} className="scroll-mt-2">
      <div className="mb-3 text-[13px] font-bold text-foreground">{title}</div>
      {children}
    </div>
  )
}

function runnerLabel(name: string): string {
  const builtin = BUILTIN_RUNNER_PRESENTATION[name]
  if (builtin) return builtin.label
  const words = name.replaceAll('_', ' ').replaceAll('-', ' ').trim()
  return words ? words[0].toUpperCase() + words.slice(1) : 'Provider execution'
}

function runnerGuidance(name: string): string {
  return BUILTIN_RUNNER_PRESENTATION[name]?.guidance
    ?? 'Runs through a provider configured for this workspace.'
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-2.5">
      <Label className="mb-1 block text-[11.5px] font-normal text-muted-foreground">{label}</Label>
      {children}
    </div>
  )
}
