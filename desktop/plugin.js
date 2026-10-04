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
import { useEffect, useState } from 'react'

const ID = 'quota-desk'
const ROUTE = '/quota-desk'
const POLL_MS = 120000
const UI_VERSION = '0.3.4'

// Status-bar chip visibility is a per-app preference, toggled from the page.
const CHIP_HIDDEN_KEY = 'quota-desk:chip-hidden'
const CHIP_EVENT = 'quota-desk:chip-visibility'

function chipHidden() {
  try { return localStorage.getItem(CHIP_HIDDEN_KEY) === '1' } catch { return false }
}

function setChipHidden(hidden) {
  try { localStorage.setItem(CHIP_HIDDEN_KEY, hidden ? '1' : '0') } catch { /* ignore */ }
  window.dispatchEvent(new CustomEvent(CHIP_EVENT))
}

function useChipHidden() {
  const [hidden, setHidden] = useState(chipHidden)
  useEffect(() => {
    const update = () => setHidden(chipHidden())
    window.addEventListener(CHIP_EVENT, update)
    return () => window.removeEventListener(CHIP_EVENT, update)
  }, [])
  return hidden
}

// Four heat tiers, shared by the bars and the chip parts:
// <50 default, 50-84 yellow, 85-99 orange, 100 red.
function levelFor(value) {
  if (value === null || !Number.isFinite(value)) return ''
  if (value >= 100) return 'crit'
  if (value >= 85) return 'warn'
  if (value >= 50) return 'mid'
  return ''
}

// UI-only patch releases must not trip the mismatch banner: compare major.minor.
function versionTrack(v) {
  return String(v || '').split('.').slice(0, 2).join('.')
}

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
.qd-resets{font-size:11px;color:var(--ui-accent);border:1px solid var(--ui-accent);border-radius:999px;padding:1px 7px}
.qd-src{margin-left:auto;font-size:11px;color:var(--ui-text-quaternary)}
.qd-win{margin-top:10px}
.qd-winrow{display:flex;align-items:baseline;gap:8px;font-size:11px;color:var(--ui-text-secondary)}
.qd-winlabel{min-width:120px}
.qd-winpct{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--ui-text-primary)}
.qd-bar{height:5px;border-radius:999px;background:var(--ui-stroke-secondary);margin-top:4px;overflow:hidden}
.qd-bar>i{display:block;height:100%;background:var(--ui-accent)}
.qd-bar>i.qd-fill-mid{background:var(--ui-yellow,#c08532)}
.qd-bar>i.qd-fill-warn{background:var(--ui-orange,#db704b)}
.qd-bar>i.qd-fill-crit{background:var(--ui-danger,var(--ui-red,#cf2d56))}
.qd-reset{font-size:10px;color:var(--ui-text-quaternary);margin-top:2px}
.qd-bad{margin-top:8px;font-size:12px;color:var(--ui-yellow,var(--ui-text-secondary));line-height:1.45}
.qd-err{font-size:12px;color:var(--ui-danger,var(--ui-text-secondary));margin-top:10px}
.qd-chip{display:inline-flex;gap:6px;align-items:center;font-size:11px;color:var(--ui-text-tertiary);cursor:pointer;padding:0 6px}
.qd-chip:hover{color:var(--ui-text-primary)}
.qd-chip-part-mid{color:var(--ui-yellow,#c08532)}
.qd-chip-part-warn{color:var(--ui-orange,#db704b)}
.qd-chip-part-crit{color:var(--ui-danger,var(--ui-red,#cf2d56))}
.qd-version{font-size:10px;color:var(--ui-text-quaternary);margin-top:16px}
.qd-mismatch{font-size:12px;color:var(--ui-yellow,#c08532);margin-top:10px}
.qd-refreshbtn{font-size:11px;color:var(--ui-text-tertiary);border:1px solid var(--ui-stroke-secondary);border-radius:6px;padding:2px 10px;cursor:pointer;background:transparent}
.qd-refreshbtn:hover{color:var(--ui-text-primary)}
.qd-refreshbtn:disabled{opacity:.5;cursor:default}
.qd-titlerow{display:flex;align-items:baseline;gap:10px}
.qd-credits{margin-top:10px;border-top:1px solid var(--ui-stroke-secondary);padding-top:8px}
.qd-crow{display:flex;align-items:baseline;font-size:12px;padding:2px 0}
.qd-clabel{color:var(--ui-text-secondary)}
.qd-cval{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600;color:var(--ui-text-primary)}
.qd-renews{display:flex;align-items:baseline;margin-top:6px;font-size:11px;color:var(--ui-text-tertiary)}
.qd-topup{margin-left:auto;font-size:11px;color:var(--ui-accent);cursor:pointer}
.qd-topup:hover{text-decoration:underline}
.qd-detail{font-size:10px;color:var(--ui-text-quaternary);margin-top:2px}
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

function projectionText(projection) {
  if (!projection || typeof projection.days_left !== 'number' || !Number.isFinite(projection.days_left)) return null
  const days = projection.days_left
  const time = days < 1 ? `${Math.max(0, Math.round(days * 24))}h` : `${Math.round(days * 10) / 10}d`
  const date = projection.runs_out_at && Date.parse(projection.runs_out_at)
  const when = date && Number.isFinite(date)
    ? ` (${new Date(date).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }).replace(',', '')})`
    : ''
  return `at this pace, runs out in ${time}${when}`
}

function usd(value) {
  return typeof value === 'number' && Number.isFinite(value)
    ? `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : null
}

function renewsText(raw) {
  if (!raw) return null
  const then = Date.parse(raw)
  if (Number.isNaN(then)) return `Renews ${raw}`
  const date = new Date(then).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
  const days = Math.round((then - Date.now()) / 86400000)
  if (days < 0) return `Renewal due (was ${date})`
  if (days === 0) return `Renews today · ${date}`
  if (days === 1) return `Renews tomorrow · ${date}`
  return `Renews in ${days}d · ${date}`
}

function openExternal(url) {
  const os = pluginContext && pluginContext.os
  if (typeof url === 'string' && /^https?:\/\//.test(url) && os && typeof os.openExternal === 'function') {
    os.openExternal(url)
  }
}

function CreditRow({ label, value, strong }) {
  const text = usd(value)
  if (text === null) return null
  return jsxs('div', {
    className: 'qd-crow',
    children: [
      jsx('span', { className: 'qd-clabel', children: label }),
      jsx('span', { className: 'qd-cval', children: strong ? jsx('b', { children: text }) : text })
    ]
  })
}

function CreditsPanel({ credits }) {
  const c = credits || {}
  const rows = [
    jsx(CreditRow, { label: 'Subscription credits', value: c.subscription_remaining }, 'sub'),
    jsx(CreditRow, { label: 'Top-up credits', value: c.topup_remaining }, 'topup'),
    jsx(CreditRow, { label: 'Rollover', value: c.rollover }, 'rollover'),
    jsx(CreditRow, { label: 'Total usable', value: c.total_usable, strong: true }, 'total')
  ].filter(Boolean)
  const renews = renewsText(c.renews_at)
  return jsxs('div', {
    className: 'qd-credits',
    children: [
      ...rows,
      (renews || c.topup_url)
        ? jsxs('div', {
            className: 'qd-renews',
            children: [
              jsx('span', { children: renews || '' }),
              c.topup_url
                ? jsx('span', {
                    className: 'qd-topup',
                    title: c.topup_url,
                    onClick: () => openExternal(c.topup_url),
                    children: 'Top up →'
                  })
                : null
            ]
          })
        : null
    ]
  })
}

function Window({ window }) {
  const value = pct(window)
  const projection = projectionText(window && window.projection)
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
        children: jsx('i', {
          className: levelFor(value) ? `qd-fill-${levelFor(value)}` : undefined,
          style: { width: `${value === null ? 0 : Math.max(0, Math.min(100, value))}%` }
        })
      }),
      resetText(window.reset_at)
        ? jsx('div', { className: 'qd-reset', children: resetText(window.reset_at) })
        : null,
      projection ? jsx('div', { className: 'qd-reset', children: projection }) : null,
      window.detail ? jsx('div', { className: 'qd-reset', children: window.detail }) : null
    ]
  })
}

function ProviderCard({ row }) {
  const windows = (row && row.windows) || []
  const last = row && row.last_good
  const banked = row && typeof row.banked_resets === 'number' ? row.banked_resets : null
  return jsxs('div', {
    className: 'qd-card',
    children: [
      jsxs('div', {
        className: 'qd-head',
        children: [
          jsx('span', { className: 'qd-name', children: (row && row.label) || (row && row.id) || 'Provider' }),
          row && row.plan ? jsx('span', { className: 'qd-plan', children: row.plan }) : null,
          banked !== null
            ? jsx('span', {
                className: 'qd-resets',
                title: 'Banked Codex rate-limit resets — redeem from the CLI with /usage reset once a window is exhausted',
                children: `${banked} reset${banked === 1 ? '' : 's'} banked`
              })
            : null,
          row && row.source ? jsx('span', { className: 'qd-src', children: row.source }) : null
        ]
      }),
      windows.length
        ? jsx('div', { children: windows.map((window, index) => jsx(Window, { window }, String(index))) })
        : null,
      row && row.credits ? jsx(CreditsPanel, { credits: row.credits }) : null,
      row && Array.isArray(row.details) && row.details.length
        ? jsx('div', {
            children: row.details.map((line, index) =>
              jsx('div', { className: 'qd-detail', children: line }, String(index)))
          })
        : null,
      row && row.unavailable
        ? jsx('div', { className: 'qd-bad', children: row.unavailable })
        : null,
      last && last.windows && last.windows.length
        ? jsxs('div', {
            children: [
              jsx('div', { className: 'qd-reset', children: `Last known — snapshot ${last.observed_at || 'time unknown'}` }),
              last.windows.map((window, index) =>
                jsx(Window, { window: { ...window, reset_at: null, projection: null } }, `last-${index}`))
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
  const hidden = useChipHidden()
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
          jsxs('div', {
            className: 'qd-titlerow',
            children: [
              jsx('div', { className: 'qd-title', children: 'Subscription quota' }),
              jsx('button', {
                className: 'qd-refreshbtn',
                disabled: !!(query && query.isFetching),
                title: 'Re-read every provider now (Claude is re-read from the CLI store — run Claude Code once first if its token expired)',
                onClick: () => { if (query && query.refetch) query.refetch() },
                children: query && query.isFetching ? 'Reading…' : 'Refresh'
              })
            ]
          }),
          jsx('div', {
            className: 'qd-sub',
            children: data && data.generated_at
              ? `Read ${new Date(data.generated_at).toLocaleTimeString()} · snapshot age is shown per provider`
              : 'Reading provider quota…'
          }),
          data && data.backend_version && versionTrack(data.backend_version) !== versionTrack(UI_VERSION)
            ? jsx('div', { className: 'qd-mismatch', children: 'Quota Desk UI and backend are out of sync — update/reload the plugin.' })
            : null,
          failed
            ? jsx('div', { className: 'qd-err', children: `Backend unavailable: ${String((failed && failed.message) || failed)}` })
            : null,
          !failed && !rows.length && query && query.isLoading
            ? jsx('div', { className: 'qd-sub', children: 'Reading provider quota…' })
            : null,
          rows.map((row, index) => jsx(ProviderCard, { row }, (row && row.id) || String(index))),
          !failed && !rows.length && query && !query.isLoading
            ? jsx('div', { className: 'qd-bad', children: 'No provider rows returned.' })
            : null,
          jsxs('div', {
            className: 'qd-version',
            children: [
              `UI ${UI_VERSION} · backend ${(data && data.backend_version) || 'unknown'} · `,
              jsx('span', {
                className: 'qd-topup',
                onClick: () => setChipHidden(!hidden),
                children: hidden ? 'show status-bar chip' : 'hide status-bar chip'
              })
            ]
          })
        ]
      })
    ]
  })
}

function StatusChip() {
  const query = useUsage()
  const hidden = useChipHidden()
  const rows = (query && query.data && query.data.providers) || []
  if (hidden) return null
  // Each part is colored by ITS OWN provider's state, not the desk's worst:
  // 'nous 100%' turns red without implying kimi 22% is also on fire.
  const parts = []
  for (const row of rows) {
    if (!row) continue
    const windows = Array.isArray(row.windows) ? row.windows : []
    let rowWorst = 0
    for (const window of windows) {
      const value = pct(window)
      if (value !== null && Number.isFinite(value)) rowWorst = Math.max(rowWorst, value)
    }
    const value = summarise(row)
    if (value !== null) {
      const id = String(row.id || '')
      const name = id === 'openai-codex' ? 'codex' : id.split('-')[0]
      parts.push({
        text: `${name} ${value}`,
        level: levelFor(rowWorst)
      })
    }
    if (row.id === 'openai-codex' && row.banked_resets > 0 &&
        windows.some(window => pct(window) >= 100)) {
      parts.push({ text: `${row.banked_resets} resets`, level: 'warn' })
    }
    const renewsAt = row.id === 'nous' && row.credits && row.credits.renews_at
    const then = renewsAt && Date.parse(renewsAt)
    if (Number.isFinite(then)) {
      const days = (then - Date.now()) / 86400000
      if (days < 0) parts.push({ text: 'nous renewal due', level: 'warn' })
      else if (days <= 7) parts.push({ text: `nous renews ${Math.ceil(days)}d`, level: '' })
    }
  }
  const children = []
  parts.forEach((part, index) => {
    if (index > 0) children.push(jsx('span', { children: ' · ' }, `sep-${index}`))
    children.push(jsx('span', {
      className: part.level ? `qd-chip-part-${part.level}` : undefined,
      children: part.text
    }, `part-${index}`))
  })
  return jsx('span', {
    className: 'qd-chip',
    title: 'Quota Desk — click to open',
    onClick: () => host && host.navigate && host.navigate(ROUTE),
    children: children.length ? children : 'quota'
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
