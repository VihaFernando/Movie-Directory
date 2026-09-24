import { useAuth } from '@clerk/react'
import { Ban, Clock, ShieldCheck, ShieldX, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { fetchAdminAuditLog } from '../api'
import { Skeleton } from './ui/Skeleton'

const ACTION_META = {
  set_role: { icon: ShieldCheck, label: (d) => `set role to ${d?.role ?? '?'}` },
  ban: { icon: Ban, label: () => 'banned' },
  unban: { icon: ShieldX, label: () => 'unbanned' },
  delete: { icon: Trash2, label: () => 'deleted' },
}

function describe(person) {
  if (!person) return 'Unknown'
  return person.name || person.email || person.id
}

function formatWhen(unixSeconds) {
  if (!unixSeconds) return '—'
  return new Date(unixSeconds * 1000).toLocaleString()
}

function AuditRowSkeleton() {
  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <Skeleton className="h-8 w-8 shrink-0 rounded-full" />
      <div className="flex-1 space-y-1.5">
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-3 w-24" />
      </div>
    </li>
  )
}

// Recent admin actions (role changes, bans, deletes) - Clerk itself keeps no
// such history, so this is the only place that answers "who did that and
// when" (see app/users.py's admin_audit_log collection).
//
// `refreshToken` is bumped by the parent (AdminPage) after every successful
// action - without it this panel only ever fetched once on mount and never
// found out an action elsewhere on the page had happened, so a fresh ban/
// promote/delete wouldn't show up here until a manual page reload.
export function AuditLogPanel({ refreshToken }) {
  const { getToken } = useAuth()
  const [entries, setEntries] = useState(null) // null = first load
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const token = await getToken()
        const data = await fetchAdminAuditLog(token, { limit: 50 })
        if (!cancelled) setEntries(data.entries)
      } catch (err) {
        if (!cancelled) setError(err.message || 'Failed to load activity')
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [getToken, refreshToken])

  return (
    <div className="mt-8">
      <div className="mb-3 flex items-center gap-2">
        <Clock className="h-4 w-4 text-muted-foreground" />
        <h2 className="text-lg font-semibold">Recent activity</h2>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="rounded-lg border border-border">
        {entries === null ? (
          <ul className="divide-y divide-border">
            {Array.from({ length: 4 }).map((_, i) => (
              <AuditRowSkeleton key={i} />
            ))}
          </ul>
        ) : entries.length === 0 ? (
          <p className="px-4 py-8 text-center text-sm text-muted-foreground">No admin activity yet.</p>
        ) : (
          <ul className="divide-y divide-border">
            {entries.map((entry) => {
              const meta = ACTION_META[entry.action] || { icon: Clock, label: () => entry.action }
              const Icon = meta.icon
              return (
                <li key={entry._id} className="flex items-start gap-3 px-4 py-3 text-sm">
                  <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-muted">
                    <Icon className="h-4 w-4 text-muted-foreground" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate">
                      <span className="font-medium">{describe(entry.actor)}</span>{' '}
                      <span className="text-muted-foreground">{meta.label(entry.detail)}</span>{' '}
                      <span className="font-medium">{describe(entry.target)}</span>
                    </p>
                    <p className="text-xs text-muted-foreground">{formatWhen(entry.at)}</p>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
