import { useAuth } from '@clerk/react'
import { ShieldAlert, ShieldCheck, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  banAdminUser,
  deleteAdminUser,
  fetchAdminUsers,
  setAdminUserRole,
  unbanAdminUser,
} from '../api'
import { AuditLogPanel } from '../components/AuditLogPanel'
import { Button } from '../components/ui/Button'
import { Skeleton } from '../components/ui/Skeleton'
import { useDebounced } from '../hooks/useDebounced'
import { cn } from '../lib/utils'

function formatDate(ms) {
  if (!ms) return '—'
  return new Date(ms).toLocaleString()
}

function UserRowSkeleton() {
  return (
    <tr className="border-t border-border">
      <td className="px-4 py-3">
        <div className="flex items-center gap-3">
          <Skeleton className="h-8 w-8 shrink-0 rounded-full" />
          <div className="space-y-1.5">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-3 w-40" />
          </div>
        </div>
      </td>
      <td className="px-4 py-3"><Skeleton className="h-5 w-14 rounded-full" /></td>
      <td className="px-4 py-3"><Skeleton className="h-4 w-12" /></td>
      <td className="px-4 py-3"><Skeleton className="h-3 w-28" /></td>
      <td className="px-4 py-3"><Skeleton className="h-3 w-28" /></td>
      <td className="px-4 py-3">
        <div className="flex items-center justify-end gap-2">
          <Skeleton className="h-9 w-24 rounded-md" />
          <Skeleton className="h-9 w-16 rounded-md" />
          <Skeleton className="h-9 w-9 rounded-md" />
        </div>
      </td>
    </tr>
  )
}

// User management: lists every Clerk account (Clerk is the source of truth,
// not a local table - see app/admin_routes.py) with role/ban controls.
// Only reachable when the signed-in user is an admin - App.jsx gates the
// route itself, but every action below is re-checked server-side anyway.
const SKELETON_ROWS = 5

export function AdminPage() {
  const { getToken } = useAuth()
  const [users, setUsers] = useState(null) // null = never loaded yet (first-load skeleton)
  const [total, setTotal] = useState(0)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)
  const [queryInput, setQueryInput] = useState('')
  const query = useDebounced(queryInput, 300)
  const [busyId, setBusyId] = useState(null)
  const [confirmDeleteId, setConfirmDeleteId] = useState(null)
  // Bumped after every successful action - AuditLogPanel refetches whenever
  // this changes, so a fresh ban/promote/delete shows up there immediately
  // instead of only after a manual page reload.
  const [activityRefreshToken, setActivityRefreshToken] = useState(0)
  // Guards against an in-flight search response landing after a newer one
  // was already fired (e.g. typing quickly) - without this, a slow response
  // for an earlier query could overwrite the results of a later one.
  const requestIdRef = useRef(0)

  const load = useCallback(async () => {
    const requestId = ++requestIdRef.current
    setRefreshing(true)
    setError(null)
    try {
      const token = await getToken()
      const data = await fetchAdminUsers(token, { query, limit: 100 })
      if (requestId !== requestIdRef.current) return
      setUsers(data.users)
      setTotal(data.total)
    } catch (err) {
      if (requestId !== requestIdRef.current) return
      setError(err.message || 'Failed to load users')
    } finally {
      if (requestId === requestIdRef.current) setRefreshing(false)
    }
  }, [getToken, query])

  useEffect(() => {
    load()
  }, [load])

  const isFirstLoad = users === null

  const withBusy = async (userId, action) => {
    setBusyId(userId)
    setError(null)
    try {
      const token = await getToken()
      const updated = await action(token)
      setUsers((prev) => (prev || []).map((u) => (u.id === userId ? { ...u, ...updated } : u)))
      setActivityRefreshToken((n) => n + 1)
    } catch (err) {
      setError(err.message || 'Action failed')
    } finally {
      setBusyId(null)
    }
  }

  const toggleRole = (user) =>
    withBusy(user.id, (token) => setAdminUserRole(token, user.id, user.role === 'admin' ? 'user' : 'admin'))

  const toggleBan = (user) =>
    withBusy(user.id, (token) => (user.banned ? unbanAdminUser(token, user.id) : banAdminUser(token, user.id)))

  const handleDelete = async (user) => {
    if (confirmDeleteId !== user.id) {
      setConfirmDeleteId(user.id)
      return
    }
    setBusyId(user.id)
    setError(null)
    try {
      const token = await getToken()
      await deleteAdminUser(token, user.id)
      setUsers((prev) => (prev || []).filter((u) => u.id !== user.id))
      setTotal((t) => t - 1)
      setActivityRefreshToken((n) => n + 1)
    } catch (err) {
      setError(err.message || 'Delete failed')
    } finally {
      setBusyId(null)
      setConfirmDeleteId(null)
    }
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">User Management</h1>
          <p className="text-sm text-muted-foreground">{total} registered {total === 1 ? 'user' : 'users'}</p>
        </div>
        <input
          value={queryInput}
          onChange={(e) => setQueryInput(e.target.value)}
          placeholder="Search by name or email..."
          className="h-9 w-64 rounded-md border border-border bg-card px-3 text-sm outline-none focus:ring-2 focus:ring-ring"
        />
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-left text-sm">
          <thead className="bg-card text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-4 py-3">User</th>
              <th className="px-4 py-3">Role</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Joined</th>
              <th className="px-4 py-3">Last active</th>
              <th className="px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className={cn('transition-opacity', refreshing && !isFirstLoad && 'opacity-50')}>
            {isFirstLoad ? (
              Array.from({ length: SKELETON_ROWS }).map((_, i) => <UserRowSkeleton key={i} />)
            ) : users.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">
                  No users found.
                </td>
              </tr>
            ) : (
              users.map((user) => {
                const busy = busyId === user.id
                const name = [user.first_name, user.last_name].filter(Boolean).join(' ') || '—'
                return (
                  <tr key={user.id} className="border-t border-border">
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-3">
                        {user.image_url ? (
                          <img src={user.image_url} alt="" className="h-8 w-8 rounded-full object-cover" />
                        ) : (
                          <div className="h-8 w-8 rounded-full bg-muted" />
                        )}
                        <div>
                          <div className="font-medium">{name}</div>
                          <div className="text-xs text-muted-foreground">{user.email || '—'}</div>
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={cn(
                          'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium',
                          user.role === 'admin'
                            ? 'bg-primary/15 text-primary'
                            : 'bg-muted text-muted-foreground',
                        )}
                      >
                        {user.role === 'admin' && <ShieldCheck className="h-3 w-3" />}
                        {user.role}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {user.banned ? (
                        <span className="inline-flex items-center gap-1 rounded-full bg-destructive/15 px-2 py-0.5 text-xs font-medium text-destructive">
                          <ShieldAlert className="h-3 w-3" />
                          Banned
                        </span>
                      ) : (
                        <span className="text-xs text-muted-foreground">Active</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-muted-foreground">{formatDate(user.created_at)}</td>
                    <td className="px-4 py-3 text-xs text-muted-foreground">{formatDate(user.last_active_at)}</td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => toggleRole(user)}
                        >
                          {user.role === 'admin' ? 'Demote' : 'Make admin'}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => toggleBan(user)}
                        >
                          {user.banned ? 'Unban' : 'Ban'}
                        </Button>
                        <Button
                          size="sm"
                          variant={confirmDeleteId === user.id ? 'default' : 'outline'}
                          className={confirmDeleteId === user.id ? 'bg-destructive text-destructive-foreground hover:bg-destructive/90' : ''}
                          disabled={busy}
                          onClick={() => handleDelete(user)}
                          onBlur={() => setConfirmDeleteId(null)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          {confirmDeleteId === user.id ? 'Confirm' : ''}
                        </Button>
                      </div>
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      <AuditLogPanel refreshToken={activityRefreshToken} />
    </div>
  )
}
