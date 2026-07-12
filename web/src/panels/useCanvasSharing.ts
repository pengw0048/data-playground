import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api,
  type CanvasVisibility,
  type DpUser,
  type ShareInfo,
  type ShareRole,
} from '../api/client'

type PendingOperation = 'load' | 'visibility' | `add:${string}` | `role:${string}` | `remove:${string}`
type CanvasGeneration = { canvasId: string; generation: number }
type RetryOperation = { scope: CanvasGeneration; run: () => Promise<void>; requiresOwner: boolean }

function failureMessage(action: string, error: unknown): string {
  const status = typeof error === 'object' && error !== null && 'status' in error
    ? Number((error as { status?: unknown }).status)
    : null
  const detail = error instanceof Error && error.message ? error.message : 'unknown error'
  return `${action} failed${status ? ` (${status})` : ''}: ${detail}`
}

// Single source of truth for the two sharing surfaces. Mutations are deliberately pessimistic: the
// visible value changes only after the server accepts it, so a 401/403/500/offline response never
// leaves a control claiming success. Every request and retry is scoped to one canvas generation — a
// late response from canvas A can never update canvas B after the modal follows a navigation.
export function useCanvasSharing(canvasId: string, canManage: boolean) {
  const [visibility, setVisibility] = useState<CanvasVisibility | null>(null)
  const [shares, setShares] = useState<ShareInfo[]>([])
  const [pending, setPending] = useState<PendingOperation | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retryable, setRetryable] = useState(false)
  const retryRef = useRef<RetryOperation | null>(null)
  const canManageRef = useRef(canManage)
  const generationRef = useRef<CanvasGeneration>({ canvasId, generation: 0 })

  canManageRef.current = canManage
  // Advance synchronously during render, before effects run, so any A promise settling while React
  // switches the modal to B already sees an obsolete token.
  if (generationRef.current.canvasId !== canvasId) {
    generationRef.current = { canvasId, generation: generationRef.current.generation + 1 }
  }

  const currentScope = useCallback((): CanvasGeneration => ({ ...generationRef.current }), [])
  const isCurrent = useCallback((scope: CanvasGeneration): boolean => (
    generationRef.current.canvasId === scope.canvasId
      && generationRef.current.generation === scope.generation
  ), [])

  const clearFailure = useCallback((scope: CanvasGeneration): boolean => {
    if (!isCurrent(scope)) return false
    retryRef.current = null
    setRetryable(false)
    setError(null)
    return true
  }, [isCurrent])

  const begin = useCallback((scope: CanvasGeneration, operation: PendingOperation): boolean => {
    if (!clearFailure(scope)) return false
    setPending(operation)
    return true
  }, [clearFailure])

  const rememberFailure = useCallback((
    scope: CanvasGeneration,
    message: string,
    retry: () => Promise<void>,
    requiresOwner = false,
  ) => {
    if (!isCurrent(scope)) return
    retryRef.current = { scope, run: retry, requiresOwner }
    setRetryable(true)
    setError(message)
  }, [isCurrent])

  const load = useCallback(async () => {
    const scope = currentScope()
    // A stale callback retained by a caller must not issue a request for its old canvas under B's token.
    if (scope.canvasId !== canvasId || !begin(scope, 'load')) return
    try {
      const result = await api.getShares(canvasId)
      if (!isCurrent(scope)) return
      setVisibility(result.visibility)
      setShares(result.shares)
    } catch (e) {
      rememberFailure(scope, failureMessage('Loading sharing settings', e), load)
    } finally {
      if (isCurrent(scope)) setPending(null)
    }
  }, [begin, canvasId, currentScope, isCurrent, rememberFailure])

  useEffect(() => {
    const scope = currentScope()
    if (!isCurrent(scope) || scope.canvasId !== canvasId) return
    retryRef.current = null
    setRetryable(false)
    setError(null)
    setVisibility(null)
    setShares([])
    setPending(null)
    void load()
    return () => {
      // Unmount invalidates in-flight work. On A→B, render already advanced the generation, so A's
      // cleanup must not advance B a second time and invalidate B's new load.
      if (isCurrent(scope)) {
        generationRef.current = { canvasId: scope.canvasId, generation: scope.generation + 1 }
        retryRef.current = null
      }
    }
  }, [canvasId, currentScope, isCurrent, load])

  const requireOwner = useCallback((scope: CanvasGeneration, action: string): boolean => {
    if (!isCurrent(scope)) return false
    if (canManageRef.current) return true
    clearFailure(scope)
    setError(`${action} failed: only the canvas owner can change sharing.`)
    return false
  }, [clearFailure, isCurrent])

  const setCanvasVisibility = useCallback(async (next: CanvasVisibility) => {
    const scope = currentScope()
    if (scope.canvasId !== canvasId || !requireOwner(scope, 'Updating visibility')) return
    const run = async () => {
      if (!begin(scope, 'visibility')) return
      try {
        await api.addShare(canvasId, { visibility: next })
        if (isCurrent(scope)) setVisibility(next)
      } catch (e) {
        rememberFailure(scope, failureMessage('Updating visibility', e), run, true)
      } finally {
        if (isCurrent(scope)) setPending(null)
      }
    }
    await run()
  }, [begin, canvasId, currentScope, isCurrent, rememberFailure, requireOwner])

  const addCollaborator = useCallback(async (user: DpUser, role: ShareRole): Promise<boolean> => {
    const scope = currentScope()
    if (scope.canvasId !== canvasId || !requireOwner(scope, 'Adding collaborator')) return false
    let succeeded = false
    const operation: PendingOperation = `add:${user.id}`
    const run = async () => {
      if (!begin(scope, operation)) return
      try {
        await api.addShare(canvasId, { userId: user.id, role })
        if (!isCurrent(scope)) return
        setShares((current) => [...current.filter((share) => share.userId !== user.id), {
          userId: user.id,
          name: user.name,
          role,
        }])
        succeeded = true
      } catch (e) {
        rememberFailure(scope, failureMessage('Adding collaborator', e), run, true)
      } finally {
        if (isCurrent(scope)) setPending(null)
      }
    }
    await run()
    return succeeded
  }, [begin, canvasId, currentScope, isCurrent, rememberFailure, requireOwner])

  const changeCollaboratorRole = useCallback(async (userId: string, role: ShareRole) => {
    const scope = currentScope()
    if (scope.canvasId !== canvasId || !requireOwner(scope, 'Changing collaborator access')) return
    const operation: PendingOperation = `role:${userId}`
    const run = async () => {
      if (!begin(scope, operation)) return
      try {
        await api.addShare(canvasId, { userId, role })
        if (isCurrent(scope)) {
          setShares((current) => current.map((share) => share.userId === userId ? { ...share, role } : share))
        }
      } catch (e) {
        rememberFailure(scope, failureMessage('Changing collaborator access', e), run, true)
      } finally {
        if (isCurrent(scope)) setPending(null)
      }
    }
    await run()
  }, [begin, canvasId, currentScope, isCurrent, rememberFailure, requireOwner])

  const removeCollaborator = useCallback(async (userId: string) => {
    const scope = currentScope()
    if (scope.canvasId !== canvasId || !requireOwner(scope, 'Removing collaborator')) return
    const operation: PendingOperation = `remove:${userId}`
    const run = async () => {
      if (!begin(scope, operation)) return
      try {
        await api.removeShare(canvasId, userId)
        if (isCurrent(scope)) setShares((current) => current.filter((share) => share.userId !== userId))
      } catch (e) {
        rememberFailure(scope, failureMessage('Removing collaborator', e), run, true)
      } finally {
        if (isCurrent(scope)) setPending(null)
      }
    }
    await run()
  }, [begin, canvasId, currentScope, isCurrent, rememberFailure, requireOwner])

  const retry = useCallback(() => {
    const scope = currentScope()
    if (scope.canvasId !== canvasId) return // stale Retry handler retained from another canvas render
    const retryOperation = retryRef.current
    if (!retryOperation || pending) return
    if (!isCurrent(retryOperation.scope)) {
      retryRef.current = null
      setRetryable(false)
      return
    }
    if (retryOperation.requiresOwner && !canManageRef.current) {
      retryRef.current = null
      setRetryable(false)
      setError('Retry failed: only the canvas owner can change sharing.')
      return
    }
    void retryOperation.run()
  }, [canvasId, currentScope, isCurrent, pending])

  return {
    visibility,
    shares,
    pending,
    error,
    retryable,
    retry,
    setCanvasVisibility,
    addCollaborator,
    changeCollaboratorRole,
    removeCollaborator,
  }
}
