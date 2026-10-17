"""Claude Accounts dashboard tab — backend, mounted at /api/plugins/claude-subscription-directsdk-experimental/.

Read-only view of the provider's pooled Claude logins (see ../accounts.py) plus the one write
the operator needs from a dashboard: clearing a bench early. Plan limits come from Anthropic's
``/api/oauth/usage`` with each account's own token, read from its ``CLAUDE_CONFIG_DIR`` and never
refreshed or written (Claude Code owns the grant; its refresh tokens are single-use).
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

log = logging.getLogger(__name__)
router = APIRouter()

PLUGIN_ID = "claude-subscription-directsdk-experimental"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
USAGE_TTL = 60.0
STATUS_TTL = 120.0

_usage_cache: Dict[str, tuple] = {}
_status_cache: Dict[str, tuple] = {}
_lock = threading.Lock()


_PKG = "hermes_dashboard_plugin_pkg_claude_subscription_directsdk"


def _module(name: str):
    """The plugin's own module, loaded as a submodule of the plugin root (so its relative imports
    work), under a package name separate from the provider loader's hashed one."""
    import importlib
    if _PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(_PKG, PLUGIN_ROOT / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN_ROOT)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = pkg
        try:
            spec.loader.exec_module(pkg)
        except Exception:
            sys.modules.pop(_PKG, None)
            raise
    return sys.modules[_PKG] if name == "__init__" else importlib.import_module(f"{_PKG}.{name}")


def _usage_for(config_dir: str) -> Dict[str, Any]:
    """``{windows: [...], details: [...], unavailable: str|None}`` for one login; cached briefly."""
    now = time.time()
    with _lock:
        hit = _usage_cache.get(config_dir)
        if hit and now - hit[0] < USAGE_TTL:
            return hit[1]
    result = _fetch_usage(config_dir)
    with _lock:
        _usage_cache[config_dir] = (now, result)
    return result


def _fetch_usage(config_dir: str) -> Dict[str, Any]:
    accounts = _module("accounts")
    provider = _module("__init__")
    creds = accounts.read_oauth_credentials(config_dir)
    if not creds:
        return {"windows": [], "details": [], "unavailable": "no login in this directory"}
    expires = creds.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 < time.time():
        return {"windows": [], "details": [], "unavailable": "token expired — next request through Claude Code refreshes it"}
    import httpx
    headers = {"Authorization": f"Bearer {creds['accessToken']}", "anthropic-beta": "oauth-2025-04-20",
               "Accept": "application/json", "User-Agent": "claude-code/2.1.0"}
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(provider.USAGE_URL, headers=headers)
    except httpx.HTTPError:
        return {"windows": [], "details": [], "unavailable": "could not reach the usage API"}
    if response.status_code in (401, 403):
        return {"windows": [], "details": [], "unavailable": "token rejected"}
    if not 200 <= response.status_code < 300:
        return {"windows": [], "details": [], "unavailable": f"usage API returned HTTP {response.status_code}"}
    try:
        payload = response.json()
    except ValueError:
        return {"windows": [], "details": [], "unavailable": "usage API returned non-JSON"}
    windows, details = provider.usage_windows(payload if isinstance(payload, dict) else {})
    return {
        "windows": [{"label": w.label, "used_percent": w.used_percent,
                     "reset_at": w.reset_at.timestamp() if w.reset_at else None} for w in windows],
        "details": list(details), "unavailable": None,
    }


def _login_status(config_dir: str) -> Dict[str, Any]:
    now = time.time()
    with _lock:
        hit = _status_cache.get(config_dir)
        if hit and now - hit[0] < STATUS_TTL:
            return hit[1]
    status = _module("accounts").auth_status(config_dir)
    with _lock:
        _status_cache[config_dir] = (now, status)
    return status


def _entry_row(entry, config_dir: str, current_id: Optional[str]) -> Dict[str, Any]:
    reset_at = entry.last_error_reset_at
    if isinstance(reset_at, str):
        try:
            reset_at = datetime.fromisoformat(reset_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            reset_at = None
    benched = bool(entry.last_status and entry.last_status != "ok" and (reset_at is None or reset_at > time.time()))
    login = _login_status(config_dir)
    return {
        "id": entry.id, "label": entry.label, "priority": entry.priority, "config_dir": config_dir,
        "current": entry.id == current_id, "request_count": entry.request_count,
        "status": entry.last_status or "ok", "benched": benched,
        "error_reason": entry.last_error_reason, "error_message": (entry.last_error_message or "")[:200],
        "error_reset_at": reset_at, "status_at": entry.last_status_at,
        "logged_in": login["logged_in"], "plan": login["plan"], "login_error": login.get("error") or "",
        "usage": _usage_for(config_dir) if login["logged_in"] else {"windows": [], "details": [], "unavailable": "logged out"},
    }


@router.get("/accounts")
def list_accounts() -> Dict[str, Any]:
    from agent.credential_pool import load_pool
    accounts = _module("accounts")
    pool = load_pool(PLUGIN_ID)
    current_id = getattr(pool, "_current_id", None)
    rows: List[Dict[str, Any]] = []
    for entry in pool.entries():
        config_dir = accounts.account_dir(entry.access_token)
        if config_dir:
            rows.append(_entry_row(entry, config_dir, current_id))
    default = None
    if not rows:
        from agent.anthropic_credentials import claude_code_credentials_path
        config_dir = str(Path(claude_code_credentials_path()).parent)
        login = _login_status(config_dir)
        default = {"config_dir": config_dir, "logged_in": login["logged_in"], "plan": login["plan"],
                   "usage": _usage_for(config_dir) if login["logged_in"] else {"windows": [], "details": [], "unavailable": "logged out"}}
    return {"provider": PLUGIN_ID, "accounts": rows, "default_login": default, "fetched_at": time.time(),
            "add_hint": f"hermes auth add {PLUGIN_ID} --label <name>"}


@router.post("/accounts/{credential_id}/clear-bench")
def clear_bench(credential_id: str) -> Dict[str, Any]:
    from agent.credential_pool import load_pool
    pool = load_pool(PLUGIN_ID)
    cleared = pool.reset_status(credential_id)
    if cleared is None:
        raise HTTPException(status_code=404, detail="no such pooled credential")
    return {"ok": True, "id": cleared.id, "label": cleared.label}


@router.post("/accounts/refresh")
def refresh() -> Dict[str, Any]:
    with _lock:
        _usage_cache.clear()
        _status_cache.clear()
    return {"ok": True}
