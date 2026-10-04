/**
 * Quota Desk — read-only subscription quota for Hermes Desktop.
 *
 * Thin by design: every number comes from this plugin's own authenticated
 * backend route (ctx.rest('/usage') -> GET /api/plugins/quota-desk/usage).
 * No credential is read, cached or computed in the renderer.
 *
 * 1. Namespace import so a missing named export cannot crash load.
 * 2. No hardcoded colours — only --ui-* / var(--…) tokens.
 * 3. Nothing polls faster than 2 minutes.
 */

import * as sdk from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'quota-desk'
const ROUTE = '/quota-desk'
const POLL_MS = 120000

const host = sdk.host
const {
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  PALETTE_AREA,
  useQuery
} = sdk

let pluginContext = null

const CSS = `
.qd{height:100%;overflow:auto;padding:22px 26px;color:var(--ui-text-primary)}
.qd-inner{width:min(860px,100%);margin:0 auto}
.qd-kicker{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--ui-text-quaternary)}
.qd-title{font-size:20px;font-weight:650;margin-top:3px}
.qd-sub{font-size:12px;color:var(--ui-text-tertiary);margin-top:5px}
.qd-card{border:1px solid var(--ui-stroke-secondary);border-radius:8px;padding:12px 14px;margin-top:12px}
.qd-head{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.qd-name{font-size:13px;font-weight:620}
.qd-plan{font-size:11px;color:var(--ui-text-tertiary);border:1px solid var(--ui-stroke-secondary);border-radius:999px;padding:1px 7px}
.qd-src{margin-left:auto;font-size:11px;color:var(--ui-text-quaternary)}
.qd-win{margin-top:10px}
.qd-winrow{display:flex;align-items:baseline;gap:8px;font-size:11px;color:var(--ui-text-secondary)}
.qd-winlabel{min-width:120px}
.qd-winpct{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--ui-text-primary)}
.qd-bar{height:5px;border-radius:999px;background:var(--ui-stroke-secondary);margin-top:4px;overflow:hidden}
.qd-bar>i{display:block;height:100%;background:var(--ui-accent)}
.qd-reset{font-size:10px;color:var(--ui-text-quaternary);margin-top:2px}
.qd-bad{margin-top:8px;font-size:12px;color:var(--ui-yellow,var(--ui-text-secondary));line-height:1.45}
.qd-err{font-size:12px;color:var(--ui-danger,var(--ui-text-secondary));margin-top:10px}
.qd-chip{display:inline-flex;gap:6px;align-items:center;font-size:11px;color:var(--ui-text-tertiary);cursor:pointer;padding:0 6px}
.qd-chip:hover{color:var(--ui-text-primary)}
`

function pct(window) {
  return window && typeof window.used_percent === 'number' ? window.used_percent : null
}

function summarise(row) {
  const windows = (row && row.windows) || []
  if (!windows.length) return null
  const worst = windows.reduce((a, b) => ((pct(b) || 0) > (pct(a) || 0) ? b : a), windows[0])
  const value = pct(worst)
  return value === null ? null : `${Math.round(value)}%`
}

function resetText(iso) {
  if (!iso) return null
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return null
  const delta = then - Date.now()
  if (delta <= 0) return 'reset due'
  const mins = Math.round(delta / 60000)
  if (mins < 60) return `resets in ${mins}m`
  const hours = Math.round(mins / 60)
  if (hours < 48) return `resets in ${hours}h`
  return `resets in ${Math.round(hours / 24)}d`
}

function Window({ window }) {
  const value = pct(window)
  return jsxs('div', {
    className: 'qd-win',
    children: [
      jsxs('div', {
        className: 'qd-winrow',
        children: [
          jsx('span', { className: 'qd-winlabel', children: window.label || 'Window' }),
          jsx('span', { className: 'qd-winpct', children: value === null ? '—' : `${value}% used` })
        ]
      }),
      jsx('div', {
        className: 'qd-bar',
        children: jsx('i', { style: { width: `${value === null ? 0 : Math.max(0, Math.min(100, value))}%` } })
      }),
      resetText(window.reset_at)
        ? jsx('div', { className: 'qd-reset', children: resetText(window.reset_at) })
        : null,
      window.detail ? jsx('div', { className: 'qd-reset', children: window.detail }) : null
    ]
  })
}

function ProviderCard({ row }) {
  const windows = (row && row.windows) || []
  const last = row && row.last_good
  return jsxs('div', {
    className: 'qd-card',
    children: [
      jsxs('div', {
        className: 'qd-head',
        children: [
          jsx('span', { className: 'qd-name', children: (row && row.label) || (row && row.id) || 'Provider' }),
          row && row.plan ? jsx('span', { className: 'qd-plan', children: row.plan }) : null,
          row && row.source ? jsx('span', { className: 'qd-src', children: row.source }) : null
        ]
      }),
      windows.length
        ? jsx('div', { children: windows.map((window, index) => jsx(Window, { window }, String(index))) })
        : null,
      row && Array.isArray(row.details) && row.details.length
        ? jsx('div', { className: 'qd-reset', children: row.details.join(' · ') })
        : null,
      row && row.unavailable
        ? jsx('div', { className: 'qd-bad', children: row.unavailable })
        : null,
      last && last.windows && last.windows.length
        ? jsxs('div', {
            children: [
              jsx('div', { className: 'qd-reset', children: `Last known — snapshot ${last.observed_at || 'time unknown'}` }),
              last.windows.map((window, index) =>
                jsx(Window, { window: { ...window, reset_at: null } }, `last-${index}`))
            ]
          })
        : null
    ]
  })
}

function useUsage() {
  const ctx = pluginContext
  return useQuery({
    queryKey: [ID, 'usage'],
    enabled: Boolean(ctx),
    refetchInterval: POLL_MS,
    queryFn: () => pluginContext.rest('/usage')
  })
}

function QuotaDeskPage() {
  const query = useUsage()
  const data = query && query.data
  const rows = (data && data.providers) || []
  const failed = query && query.error
  return jsxs('div', {
    className: 'qd',
    children: [
      jsx('style', { children: CSS }),
      jsxs('div', {
        className: 'qd-inner',
        children: [
          jsx('div', { className: 'qd-kicker', children: 'Quota Desk' }),
          jsx('div', { className: 'qd-title', children: 'Subscription quota' }),
          jsx('div', {
            className: 'qd-sub',
            children: data && data.generated_at
              ? `Read ${new Date(data.generated_at).toLocaleTimeString()} · snapshot age is shown per provider`
              : 'Reading provider quota…'
          }),
          failed
            ? jsx('div', { className: 'qd-err', children: `Backend unavailable: ${String((failed && failed.message) || failed)}` })
            : null,
          !failed && !rows.length && query && query.isLoading
            ? jsx('div', { className: 'qd-sub', children: 'Reading provider quota…' })
            : null,
          rows.map((row, index) => jsx(ProviderCard, { row }, (row && row.id) || String(index))),
          !failed && !rows.length && query && !query.isLoading
            ? jsx('div', { className: 'qd-bad', children: 'No provider rows returned.' })
            : null
        ]
      })
    ]
  })
}

function StatusChip() {
  const query = useUsage()
  const rows = (query && query.data && query.data.providers) || []
  const parts = rows
    .map(row => {
      const value = summarise(row)
      if (value === null) return null
      const id = String((row && row.id) || '')
      const name = id === 'openai-codex' ? 'codex' : id.split('-')[0]
      return `${name} ${value}`
    })
    .filter(Boolean)
  const label = parts.length ? parts.join(' · ') : 'quota'
  return jsx('span', {
    className: 'qd-chip',
    title: 'Quota Desk — click to open',
    onClick: () => host && host.navigate && host.navigate(ROUTE),
    children: label
  })
}

export default {
  id: ID,
  name: 'Quota Desk',
  defaultEnabled: true,
  register(ctx) {
    pluginContext = ctx
    const contributions = [
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => jsx(QuotaDeskPage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: ROUTE, label: 'Quota Desk', codicon: 'dashboard' }
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'quota-desk.open',
          label: 'Open Quota Desk',
          keywords: ['quota', 'usage', 'limits', 'claude', 'kimi', 'codex'],
          run: () => host && host.navigate && host.navigate(ROUTE)
        }
      }
    ]
    contributions.push({
      id: 'chip',
      // String area per the bundled hermes-desktop-plugins template — the
      // STATUSBAR_AREAS constant also exists, but the string form is the one
      // the working template and the app's own watcher use.
      area: 'statusBar.right',
      order: 130,
      render: () => jsx(StatusChip, {})
    })
    ctx.registerMany(contributions)
  }
}
