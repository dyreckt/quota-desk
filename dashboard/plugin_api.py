"""Quota Desk backend — read-only subscription quota for Hermes Desktop.

Lives inside the Hermes web server, mounted at /api/plugins/quota-desk/. The
dashboard binds this profile's secret scope to every plugin-route request
(hermes_cli/web_server_dashboard.py::_plugin_route_secret_scope), which is why
provider keys can be resolved here the same way Hermes resolves them.

Hard constraints — do not relax these:

* Claude is read READ-ONLY. Two sources only: the macOS Keychain item
  "Claude Code-credentials" and ~/.claude/.credentials.json. This module never
  refreshes, rewrites, or logs a credential. Hermes's own borrower is disabled
  (auth.adopt_external_logins: false); a second refresher would spend the
  single-use Claude Code refresh token and log the CLI out.
* Every row carries its credential `source`, the `observed_at` time of that
  snapshot, and an explicit `unavailable` reason. A missing or expired login is
  visible, never a silently dropped card.
* `observed_at` is the age of the SNAPSHOT, not a claim about quota freshness.
* Responses carry derived numbers, sanitized source labels and timestamps only
  — never a credential, and never a raw resolver error string.

Provider table:

* Claude — Claude Code OAuth usage (read-only)
* Kimi — coding-plan quota via Hermes secret scope
* MiniMax — token-plan quota via Hermes secret scope
* Nous Portal — subscription credits via core's account read
* OpenAI Codex — core's read-only usage helper
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter

from hermes_constants import get_hermes_home

router = APIRouter()

BACKEND_VERSION = "0.3.0"

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"

KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT_S = 5.0
HTTP_TIMEOUT_S = 12.0

# Claude Code access tokens are ~8h. Treat anything inside this window as stale
# so a card never implies live quota from a token that is about to die.
TOKEN_SKEW_MS = 60_000


# ── small helpers ────────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _window(label: str, used: Optional[float], reset_raw: Any = None,
            detail: Optional[str] = None) -> dict:
    pct = None if used is None else max(0.0, min(100.0, float(used)))
    return {
        "label": label,
        "used_percent": None if pct is None else round(pct, 1),
        "remaining_percent": None if pct is None else round(100.0 - pct, 1),
        "reset_at": _parse_dt(reset_raw),
        "detail": detail,
    }


def _parse_dt(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raw = float(value)
        if raw > 1e11:  # milliseconds
            raw /= 1000.0
        if raw <= 0:
            return None
        return _iso(raw)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _cache_dir() -> Path:
    return Path(get_hermes_home()) / "cache" / "quota-desk"


def _snapshot_path() -> Path:
    return _cache_dir() / "last-good.json"


def _history_path() -> Path:
    return _cache_dir() / "history.jsonl"


_HISTORY_LAST_WRITE = 0.0


def _record_history(rows: list[dict]) -> None:
    """Sample available windows at most once per half hour, without blocking the desk."""
    global _HISTORY_LAST_WRITE
    try:
        now = _now()
        if now - _HISTORY_LAST_WRITE < 1800:
            return
        path = _history_path()
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        if lines:
            try:
                previous = datetime.fromisoformat(json.loads(lines[-1])["at"]).timestamp()
                if now - previous < 1800:
                    _HISTORY_LAST_WRITE = previous
                    return
            except (ValueError, KeyError, TypeError):
                pass
        at = _iso(now)
        entries = []
        for row in rows:
            for window in row.get("windows") or []:
                used = _num(window.get("used_percent"))
                if used is not None:
                    entries.append(json.dumps({"at": at, "p": row["id"],
                                               "w": window["label"], "u": used}))
        if not entries:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(lines) + len(entries) > 4000:
            path.write_text("\n".join((lines + entries)[-2000:]) + "\n", encoding="utf-8")
        else:
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(entries) + "\n")
        _HISTORY_LAST_WRITE = now
    except Exception:
        return


def _projection(provider_id: str, label: str, current_used: float,
                reset_at: Optional[str]) -> Optional[dict]:
    """Project exhaustion from this cycle's samples, unless reset arrives first."""
    try:
        points = []
        for line in _history_path().read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if item.get("p") != provider_id or item.get("w") != label:
                    continue
                used = _num(item.get("u"))
                if used is not None:
                    points.append((datetime.fromisoformat(item["at"]).timestamp(), used))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        cycle = []
        for point in reversed(points):
            if point[1] > current_used + 1:
                break
            cycle.append(point)
        if len(cycle) < 2:
            return None
        first, last = cycle[-1], cycle[0]
        span_days = (last[0] - first[0]) / 86400.0
        if span_days < 1 / 24:
            return None
        rate_per_day = (last[1] - first[1]) / span_days
        if rate_per_day <= 0.01:
            return None
        now = _now()
        days_left = (100.0 - current_used) / rate_per_day
        runs_out = now + days_left * 86400.0
        if reset_at and datetime.fromisoformat(reset_at.replace("Z", "+00:00")).timestamp() < runs_out:
            return None
        return {"days_left": round(days_left, 1), "runs_out_at": _iso(runs_out),
                "confidence": "medium" if len(cycle) >= 3 and span_days >= 0.5 else "low"}
    except Exception:
        return None


def _read_last_good() -> dict:
    try:
        payload = json.loads(_snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_last_good(provider: str, row: dict) -> None:
    """Remember the last SUCCESSFUL read so an unreadable provider can still show
    its last known windows — labelled with their own observation time."""
    try:
        store = _read_last_good()
        store[provider] = {
            "plan": row.get("plan"),
            "windows": row.get("windows") or [],
            "observed_at": row.get("observed_at"),
            "source": row.get("source"),
        }
        path = _snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return


def _row(provider: str, label: str, *, plan: Optional[str] = None,
         windows: Optional[list] = None, source: Optional[str] = None,
         unavailable: Optional[str] = None) -> dict:
    row = {
        "id": provider,
        "label": label,
        "plan": plan,
        "windows": windows or [],
        "source": source,
        "observed_at": _iso(_now()) if (windows or []) else None,
        "unavailable": unavailable,
        "last_good": None,
    }
    if windows:
        _write_last_good(provider, row)
    elif unavailable:
        last = _read_last_good().get(provider)
        if isinstance(last, dict) and last.get("windows"):
            row["last_good"] = last
    return row


# ── Claude (read-only; never refreshes) ──────────────────────────────────────

def _claude_from_keychain() -> Optional[dict]:
    """Read the Keychain item Claude Code owns. Bounded so a permission dialog
    can never hang the route."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("keychain read timed out")
    except OSError:
        raise RuntimeError("keychain tool unavailable")
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _claude_from_file() -> Optional[dict]:
    try:
        payload = json.loads(
            (Path.home() / ".claude" / ".credentials.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _claude_record(payload: Optional[dict]) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    record = payload.get("claudeAiOauth")
    if not isinstance(record, dict):
        record = payload if payload.get("accessToken") else None
    return record if isinstance(record, dict) and record.get("accessToken") else None


def _claude_valid(record: dict) -> bool:
    expires_at = _num(record.get("expiresAt"))
    if expires_at is None:
        return bool(record.get("accessToken"))
    return (time.time() * 1000.0) < (expires_at - TOKEN_SKEW_MS)


def _claude_token() -> tuple[Optional[str], Optional[str], Optional[str], Optional[dict]]:
    """(token, source_label, unavailable_reason, record). Never refreshes."""
    candidates: list[tuple[str, Optional[dict]]] = []
    keychain = None
    try:
        keychain = _claude_record(_claude_from_keychain())
        if keychain:
            candidates.append(("Keychain (Claude Code)", keychain))
    except RuntimeError as exc:
        keychain_error = str(exc)
    else:
        keychain_error = None

    record = _claude_record(_claude_from_file())
    if record:
        candidates.append(("~/.claude/.credentials.json", record))

    if not candidates:
        if keychain_error:
            return None, None, f"Claude Code credential read failed: {keychain_error}", None
        return None, None, ("No Claude Code login found on this host "
                            "(Keychain item or ~/.claude/.credentials.json)"), None

    live = [(label, rec) for label, rec in candidates if _claude_valid(rec)]
    if live:
        if len(live) == 1:
            label, rec = live[0]
        else:
            # Both stores carry a usable token (Claude Code 2.1.x refreshes one
            # source and not the other) — take the later expiry.
            label, rec = max(live, key=lambda item: _num(item[1].get("expiresAt")) or 0)
        return str(rec.get("accessToken") or "").strip(), label, None, rec

    expiries = [_parse_dt(rec.get("expiresAt")) for _, rec in candidates]
    latest = next((e for e in expiries if e), None)
    when = f"expired {latest}" if latest else "expired"
    return None, None, (
        f"Claude Code access token is {when} — the login is fine and the CLI "
        "refreshes it on its next run; this reader never refreshes it"
    ), candidates[0][1]


def _claude_plan(record: dict, payload: dict) -> Optional[str]:
    tier = str(record.get("rateLimitTier") or "").strip().lower()
    if "20x" in tier:
        return "Max 20x"
    if "5x" in tier:
        return "Max 5x"
    if "max" in tier:
        return "Max"
    if "pro" in tier:
        return "Pro"
    if str(record.get("subscriptionType") or "").strip().lower() == "max":
        return "Max"
    if payload.get("seven_day_opus"):
        return "Max"
    return None


_CLAUDE_ACCOUNT_CACHE: dict[str, Optional[str]] = {}


def _claude_account(token: str) -> Optional[str]:
    """Which account this login belongs to. A Max plan can be shared, so the
    percentages are the ACCOUNT's usage — without this label the card implies
    they are yours alone. Cached per token; never logs the token."""
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    if key in _CLAUDE_ACCOUNT_CACHE:
        return _CLAUDE_ACCOUNT_CACHE[key]
    label: Optional[str] = None
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            response = client.get(
                CLAUDE_OAUTH_PROFILE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "claude-code/2.1.0",
                },
            )
        if response.status_code == 200:
            payload = response.json() or {}
            account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
            org = payload.get("organization") if isinstance(payload.get("organization"), dict) else {}
            email = str(account.get("email") or "").strip()
            kind = str(org.get("organization_type") or "").strip()
            if email:
                label = f"{email}" + (f" · {kind}" if kind else "")
    except httpx.HTTPError:
        label = None
    _CLAUDE_ACCOUNT_CACHE[key] = label
    return label


def fetch_claude() -> dict:
    token, source, reason, record = _claude_token()
    # The rate-limit tier rides on the credential record, so the plan label is
    # known even when the usage call cannot be made.
    plan = _claude_plan(record or {}, {})
    if not token:
        row = _row("claude", "Claude", plan=plan, unavailable=reason)
        return row

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            response = client.get(
                CLAUDE_USAGE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "claude-code/2.1.0",
                },
            )
        if response.status_code == 401:
            return _row("claude", "Claude", plan=plan, source=source,
                        unavailable="Claude usage API rejected the token (401) — "
                                    "open Claude Code once to refresh its login")
        if response.status_code == 429:
            return _row("claude", "Claude", plan=plan, source=source,
                        unavailable="Claude usage API rate-limited (429)")
        response.raise_for_status()
        payload = response.json() or {}
    except httpx.HTTPError:
        return _row("claude", "Claude", plan=plan, source=source,
                    unavailable="Claude usage API unreachable")
    if not isinstance(payload, dict):
        return _row("claude", "Claude", plan=plan, source=source,
                    unavailable="Claude usage API returned an unexpected body")

    windows: list[dict] = []
    for key, label in (("five_hour", "Current session"),
                       ("seven_day", "Current week"),
                       ("seven_day_opus", "Opus week"),
                       ("seven_day_sonnet", "Sonnet week")):
        window = payload.get(key) if isinstance(payload.get(key), dict) else {}
        used = _num(window.get("utilization"))
        if used is None:
            continue
        windows.append(_window(label, used, window.get("resets_at")))

    # Model-scoped limits (e.g. Fable). Current accounts return per-model
    # windows here as `limits[].scope.model.display_name`; the legacy
    # `seven_day_opus`/`seven_day_sonnet` fields are null on those accounts.
    seen_models: set[str] = set()
    for item in payload.get("limits") if isinstance(payload.get("limits"), list) else []:
        if not isinstance(item, dict):
            continue
        scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
        model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        name = str(model.get("display_name") or "").strip()
        used = _num(item.get("percent"))
        if not name or used is None or name.lower() in seen_models:
            continue
        seen_models.add(name.lower())
        kind = str(item.get("kind") or "").strip()
        label = f"{name} (weekly)" if "weekly" in kind else name
        windows.append(_window(label, used, item.get("resets_at")))

    details: list[str] = []
    extra = payload.get("extra_usage") if isinstance(payload.get("extra_usage"), dict) else {}
    if extra.get("is_enabled"):
        used_credits = _num(extra.get("used_credits"))
        limit = _num(extra.get("monthly_limit"))
        if used_credits is not None and limit is not None:
            currency = str(extra.get("currency") or "USD")
            details.append(f"Extra usage {used_credits:.2f} / {limit:.2f} {currency}")

    if not windows and not details:
        return _row("claude", "Claude", plan=plan, source=source,
                    unavailable="Claude usage API returned no windows for this account")

    # Added last so the "no windows" branch above stays reachable.
    account = _claude_account(token)
    if account:
        details.append(f"Account: {account}")
        details.append("Max limits are per account, not per person")

    row = _row("claude", "Claude", plan=plan or _claude_plan(record or {}, payload),
               windows=windows, source=source)
    row["details"] = details
    return row


# ── Kimi (Hermes-resolved key; Bitwarden-backed) ─────────────────────────────

def _kimi_key() -> tuple[Optional[str], Optional[str]]:
    """(api_key, source_label). Resolved the way Hermes resolves it, so the
    Bitwarden-injected secret scope is the source — never a hand-copied .env."""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="kimi-coding") or {}
        key = str(runtime.get("api_key") or "").strip()
        if key:
            return key, "Hermes secret scope (kimi-coding)"
    except Exception:
        pass
    try:
        from agent.secret_scope import get_secret

        for name in ("KIMI_API_KEY", "KIMI_CODING_API_KEY"):
            key = (get_secret(name) or "").strip()
            if key:
                return key, f"Hermes secret scope ({name})"
    except Exception:
        pass
    return None, None


def fetch_kimi() -> dict:
    key, source = _kimi_key()
    if not key:
        return _row("kimi", "Kimi", unavailable=(
            "No Kimi coding-plan key resolved in this profile's secret scope"))

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            response = client.get(
                KIMI_USAGE_URL,
                headers={"Authorization": f"Bearer {key}",
                         "Accept": "application/json",
                         "User-Agent": "hermes-quota-desk"},
            )
        if response.status_code in (401, 403):
            return _row("kimi", "Kimi", source=source,
                        unavailable="Kimi usage API rejected the key")
        response.raise_for_status()
        payload = response.json() or {}
    except httpx.HTTPError:
        return _row("kimi", "Kimi", source=source,
                    unavailable="Kimi usage API unreachable")
    if not isinstance(payload, dict):
        return _row("kimi", "Kimi", source=source,
                    unavailable="Kimi usage API returned an unexpected body")

    windows: list[dict] = []

    # Weekly: `usages.limit_7d.used_ratio` (0-1) is authoritative when present.
    usages = payload.get("usages") if isinstance(payload.get("usages"), dict) else {}
    weekly = usages.get("limit_7d") if isinstance(usages.get("limit_7d"), dict) else {}
    ratio = _num(weekly.get("used_ratio"))
    if ratio is not None:
        windows.append(_window("Weekly", ratio * 100.0, weekly.get("reset_time")))
    else:
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        limit, remaining = _num(usage.get("limit")), _num(usage.get("remaining"))
        if limit and limit > 0 and remaining is not None:
            windows.append(_window("Weekly",
                                   max(0.0, limit - remaining) / limit * 100.0,
                                   usage.get("resetTime")))

    # 5-hour: `limits[]` carries the rolling window.
    limits = payload.get("limits") if isinstance(payload.get("limits"), list) else []
    for item in limits:
        if not isinstance(item, dict):
            continue
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        limit, remaining = _num(detail.get("limit")), _num(detail.get("remaining"))
        if not limit or limit <= 0 or remaining is None:
            continue
        label = "Session (5h)"
        window = item.get("window") if isinstance(item.get("window"), dict) else {}
        duration = _num(window.get("duration"))
        unit = str(window.get("timeUnit") or "").upper()
        if duration and "MINUTE" in unit and duration != 300:
            label = f"Session ({int(duration / 60)}h)" if duration % 60 == 0 else f"Session ({int(duration)}m)"
        windows.append(_window(label,
                               max(0.0, limit - remaining) / limit * 100.0,
                               detail.get("resetTime") or detail.get("resetAt")))
        break

    if not windows:
        return _row("kimi", "Kimi", source=source,
                    unavailable="Kimi usage API returned no quota windows")

    plan = None
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    membership = user.get("membership") if isinstance(user.get("membership"), dict) else {}
    level = str(membership.get("level") or "").strip()
    if level:
        plan = level.replace("LEVEL_", "").replace("_", " ").title()
    return _row("kimi", "Kimi", plan=plan, windows=windows, source=source)


# ── MiniMax (Hermes-resolved key; Bitwarden-backed) ──────────────────────────

def _minimax_key() -> tuple[Optional[str], Optional[str]]:
    """(api_key, source_label). Resolved the way Hermes resolves it, so the
    Bitwarden-injected secret scope is the source — never a hand-copied .env."""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="minimax") or {}
        key = str(runtime.get("api_key") or "").strip()
        if key:
            return key, "Hermes secret scope (minimax)"
    except Exception:
        pass
    try:
        from agent.secret_scope import get_secret

        key = (get_secret("MINIMAX_API_KEY") or "").strip()
        if key:
            return key, "Hermes secret scope (MINIMAX_API_KEY)"
    except Exception:
        pass
    return None, None


def fetch_minimax() -> dict:
    key, source = _minimax_key()
    if not key:
        return _row("minimax", "MiniMax", unavailable=(
            "No MiniMax key resolved in this profile's secret scope"))

    try:
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
                   "User-Agent": "quota-desk/0.3.0"}
        with httpx.Client(timeout=5.0) as client:
            response = client.get("https://www.minimax.io/v1/token_plan/remains",
                                  headers=headers)
            if response.status_code == 404:
                response = client.get("https://api.minimax.io/v1/token_plan/remains",
                                      headers=headers)
        if response.status_code != 200:
            return _row("minimax", "MiniMax", source=source,
                        unavailable=f"MiniMax HTTP {response.status_code}")
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return _row("minimax", "MiniMax", source=source,
                    unavailable="MiniMax usage API unreachable or returned invalid JSON")
    if not isinstance(payload, dict):
        return _row("minimax", "MiniMax", source=source,
                    unavailable="MiniMax usage response missing quota fields")
    base = payload.get("base_resp") if isinstance(payload.get("base_resp"), dict) else {}
    if base.get("status_code") != 0:
        return _row("minimax", "MiniMax", source=source,
                    unavailable=str(base.get("status_msg") or "MiniMax usage API error"))
    remains = payload.get("model_remains")
    model = remains[0] if isinstance(remains, list) and remains and isinstance(remains[0], dict) else {}
    windows: list[dict] = []
    for label, prefix, reset_key in (("Session (5h)", "current_interval", "end_time"),
                                     ("Weekly", "current_weekly", "weekly_end_time")):
        remaining = _num(model.get(f"{prefix}_remaining_percent"))
        if remaining is not None:
            used = 100.0 - remaining
        else:
            total = _num(model.get(f"{prefix}_total_count"))
            count = _num(model.get(f"{prefix}_usage_count"))
            used = count / total * 100.0 if total is not None and total > 0 and count is not None else None
        if used is None:
            continue
        reset = _num(model.get(reset_key))
        if reset is not None and reset > 1e12:
            reset /= 1000.0
        windows.append(_window(label, used, _iso(reset) if reset is not None else None))
    if not windows:
        return _row("minimax", "MiniMax", source=source,
                    unavailable="MiniMax usage response missing quota fields")
    return _row("minimax", "MiniMax", windows=windows, source=source)

# ── Nous Portal (core's own portal-account read) ─────────────────────────────

_NOUS_CACHE: dict[str, Any] = {"at": 0.0, "row": None}
NOUS_CACHE_TTL_S = 60.0


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _nous_credits_block(info: Any) -> dict:
    """Structured credit figures for the desk's Nous panel: dollar amounts as
    numbers (not baked into text lines) plus the renewal date and the portal
    top-up link, so the UI can lay them out properly."""
    credits: dict[str, Any] = {}
    sub = getattr(info, "subscription", None)
    if sub is not None:
        for attr, key in (("credits_remaining", "subscription_remaining"),
                          ("monthly_credits", "subscription_cap"),
                          ("rollover_credits", "rollover")):
            value = _finite(getattr(sub, attr, None))
            if value is not None:
                # Floating dust can read as a tiny negative ("$-0.00 left").
                credits[key] = max(0.0, value) if key != "subscription_cap" else value
        period_end = str(getattr(sub, "current_period_end", "") or "").strip()
        if period_end:
            credits["renews_at"] = period_end
    access = getattr(info, "paid_service_access_info", None)
    if access is not None:
        for attr, key in (("purchased_credits_remaining", "topup_remaining"),
                          ("total_usable_credits", "total_usable")):
            value = _finite(getattr(access, attr, None))
            if value is not None:
                credits[key] = value
    try:
        from hermes_cli.nous_account import nous_portal_topup_url

        url = str(nous_portal_topup_url(info) or "").strip()
        if url.startswith("http"):
            credits["topup_url"] = url
    except Exception:
        pass
    return credits


def fetch_nous() -> Optional[dict]:
    """Nous Portal entitlement via core's own read path. `force_fresh=True` so the
    numbers are live, with a short in-process cache so a 2-minute desk poll does
    not hammer the portal."""
    now = _now()
    cached = _NOUS_CACHE.get("row")
    if cached is not None and (now - float(_NOUS_CACHE.get("at") or 0.0)) < NOUS_CACHE_TTL_S:
        return cached  # type: ignore[return-value]

    try:
        from agent.account_usage import build_nous_credits_snapshot
        from hermes_cli.nous_account import get_nous_portal_account_info

        info = get_nous_portal_account_info(force_fresh=True)
        snapshot = build_nous_credits_snapshot(info)
    except Exception:
        return _row("nous", "Nous Portal",
                    unavailable="Nous Portal account lookup failed")

    if snapshot is None:
        row = _row("nous", "Nous Portal",
                   unavailable="Not signed in to Nous Portal on this host")
    else:
        windows = []
        for window in getattr(snapshot, "windows", ()) or ():
            used = _num(getattr(window, "used_percent", None))
            windows.append(_window(str(getattr(window, "label", "") or "Credits"),
                                   used, getattr(window, "reset_at", None),
                                   detail=getattr(window, "detail", None)))
        row = _row("nous", "Nous Portal",
                   plan=getattr(snapshot, "plan", None),
                   windows=windows,
                   source="Nous Portal account API")

        credits = _nous_credits_block(info)
        if credits:
            row["credits"] = credits
            # Regenerate the Subscription gauge's "$X of $Y left" from the
            # clamped figures so it never reads "$-0.00".
            remaining = credits.get("subscription_remaining")
            cap = credits.get("subscription_cap")
            if remaining is not None and cap:
                for window in row["windows"]:
                    if window["label"] == "Subscription":
                        window["detail"] = f"${remaining:,.2f} of ${cap:,.2f} left"

        # Lines the structured credits block now renders properly stay out of
        # the flat detail list; anything else (e.g. the depleted warning) stays.
        handled = ("subscription credits:", "top-up credits:", "total usable:",
                   "rollover:", "renews:", "top up:")
        row["details"] = [
            str(item) for item in (getattr(snapshot, "details", ()) or ())
            if str(item).strip()
            and str(item).strip() != "(or run /topup)"
            and not str(item).strip().lower().startswith(handled)
        ]
        if not windows and not row["details"] and not credits:
            row["unavailable"] = "Nous Portal returned no entitlement details"

    _NOUS_CACHE.update({"at": now, "row": row})
    return row


# ── Codex (read through Hermes's own read-only usage helper) ─────────────────

def fetch_codex() -> Optional[dict]:
    try:
        from agent.account_usage import fetch_account_usage

        snapshot = fetch_account_usage("openai-codex")
    except Exception:
        return None
    if snapshot is None or not getattr(snapshot, "available", False):
        return None
    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        used = _num(getattr(window, "used_percent", None))
        if used is None:
            continue
        windows.append(_window(str(getattr(window, "label", "") or "Window"),
                               used, getattr(window, "reset_at", None)))
    if not windows:
        return None
    row = _row("openai-codex", "OpenAI Codex",
               plan=getattr(snapshot, "plan", None), windows=windows,
               source="Hermes credential pool")

    # Banked rate-limit resets ride on the raw usage payload as
    # rate_limit_reset_credits.available_count. Surfaced as a first-class
    # field so the desk can badge it instead of burying it in a text line.
    raw = getattr(snapshot, "raw", None)
    if isinstance(raw, dict):
        credits = raw.get("rate_limit_reset_credits") if isinstance(
            raw.get("rate_limit_reset_credits"), dict) else {}
        count = _num(credits.get("available_count"))
        if count is not None:
            row["banked_resets"] = int(count)

    details = []
    for item in getattr(snapshot, "details", ()) or ():
        text = str(item).strip()
        # The banked count is a badge now, and the CLI hint belongs to the
        # terminal; everything else (e.g. credits balance) stays.
        if not text or "reset" in text.lower() and "/usage reset" in text:
            continue
        details.append(text)
    if details:
        row["details"] = details
    return row


# ── route ────────────────────────────────────────────────────────────────────

@router.get("/usage")
async def usage() -> dict:
    """One row per provider. Never raises: a provider that cannot be read
    returns a row with `unavailable` set."""
    rows: list[dict] = []
    for fetcher in (fetch_claude, fetch_kimi, fetch_minimax, fetch_nous, fetch_codex):
        try:
            row = fetcher()
        except Exception as exc:  # noqa: BLE001 - a broken reader must not 500 the desk
            row = _row("unknown", "Unavailable",
                       unavailable=f"reader failed ({type(exc).__name__})")
        if row:
            rows.append(row)
    _record_history(rows)
    for row in rows:
        for window in row.get("windows") or []:
            used = _num(window.get("used_percent"))
            if used is not None and used < 100:
                projection = _projection(row["id"], window["label"], used,
                                         window.get("reset_at"))
                if projection is not None:
                    window["projection"] = projection
    return {"backend_version": BACKEND_VERSION,
            "generated_at": _iso(_now()), "providers": rows}
