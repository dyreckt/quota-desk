# Quota Desk

Read-only subscription quota for **Hermes Desktop** — a status-bar chip and a page
showing how much of each paid plan is left, when it resets, where the number came
from, and why a provider couldn't be read.

| Provider | Source | What you get |
|---|---|---|
| **Claude** (Max) | macOS Keychain `Claude Code-credentials`, or `~/.claude/.credentials.json` | Current session, current week, and any model-scoped limit (e.g. **Fable**) |
| **Kimi** (coding plan) | Hermes secret scope (`kimi-coding`) | Weekly and 5-hour session windows |
| **MiniMax** (coding plan) | Hermes secret scope (`minimax` / `MINIMAX_API_KEY`) | Weekly and 5-hour session windows |
| **Nous Portal** | Nous account API | Plan, subscription credits, top-up, rollover, renewal |
| **OpenAI Codex** | Hermes credential pool | Weekly window and banked rate-limit resets |

The status-bar chip is state-aware: it turns yellow when any provider crosses
85% used, red at 100%, and surfaces banked Codex resets and an upcoming Nous
renewal right in the chip. Each window can also show a burn-rate projection
("at this pace, runs out in 3d") once the plugin has gathered a few hours of
its own history samples.

## Read-only by construction

This plugin **never refreshes, rewrites, or logs a credential** — the single most
important property for the Claude path, because Claude's refresh token is
single-use and a second refresher can log the Claude Code CLI out.

- Claude is read from the stores the CLI owns. If the access token is expired the
  card says so, shows the last known snapshot with its timestamp, and never
  touches the login. Run Claude Code once and the CLI refreshes its own token.
- Kimi resolves through Hermes's own Bitwarden-backed secret scope rather than a
  copy of the key.
- Every row carries its **credential source** and the **age of the snapshot**.
- A provider that fails returns a row with an explicit reason instead of
  disappearing.

## Install

**One installable folder — the agent half and the desktop half ship together.**

```text
quota-desk/
├── plugin.yaml            # agent half: metadata
├── __init__.py            # standard loader surface (registers no tools/hooks)
├── dashboard/
│   ├── manifest.json      # { "name": "quota-desk", "api": "plugin_api.py" }
│   └── plugin_api.py      # backend routes → /api/plugins/quota-desk/
└── desktop/
    └── plugin.js          # desktop half: chip, sidebar row, page
```

**On the machine running the gateway** (the agent half, which serves the data):

```bash
hermes plugins install dyreckt/quota-desk
hermes plugins enable quota-desk
```

**On the machine running the Desktop app** (the desktop half, which draws the UI):

- If the app and gateway are the same machine, the Electron shell copies
  `desktop/plugin.js` into `$HERMES_HOME/desktop-plugins/quota-desk/` on the next
  **Rescan**. Toggle it on in **Capabilities → Plugins**.
- If the app is a **remote client** of the gateway, the gateway's `plugins/`
  folder is not reachable as a filesystem. Install from the app side instead:
  **Capabilities → Plugins → Install from Git**, paste this repo, tick the
  **Desktop** target, then flip the toggle. Without this the Plugins page shows
  the desktop half as *unavailable (remote backend)*.

The gateway must be restarted after install so the backend route is mounted.

## Notes and limitations

- **Claude percentages are the account's, not a person's.** On a shared Max plan
  the bar moves for every user of that login. The card names the account it read,
  so a shared cap can't be misread as your own usage.
- Per-model Claude limits (Fable, Opus, Sonnet…) are read from
  `limits[].scope.model` in the usage payload. The legacy `seven_day_opus` /
  `seven_day_sonnet` fields are `null` on current accounts, so readers built on
  those fields show nothing for model-scoped usage.
- Nous Portal: the subscription gauge can read 100% while top-up and rollover
  credits remain usable — the dollar figures in the card are the honest signal.
- The Claude reader prefers the Keychain login; `~/.claude/.credentials.json` is
  used only as a fallback (they can belong to different accounts).

## Requirements

Hermes with the native Desktop app and the web dashboard plugin routes
(`/api/plugins/<id>/`). Verified against Hermes with
`hermes plugins validate` passing 16/16, including *no core override* and
*desktop surface stays inside the plugin SDK surface*.

## License

MIT — see [LICENSE](LICENSE).
