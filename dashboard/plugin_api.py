"""Claude Accounts dashboard tab — backend, mounted at /api/plugins/claude-subscription-directsdk-experimental/.

The provider's pooled Claude logins (see ../accounts.py) and the controls an operator needs:
which limit categories each account is benched for, benching or clearing one by hand, whether
an account may spend usage credits ("extra usage"), and signing an account in again in place.

Plan limits come from Anthropic's ``/api/oauth/usage`` with each account's own token, read from
its ``CLAUDE_CONFIG_DIR``. This module never refreshes or writes a token itself (Claude Code
owns the grant; its refresh tokens are single-use): an expired one is renewed by Claude Code,
see ``accounts.refresh_token``. Login state is read from the credential file: this page polls,
and a polled ``claude auth status`` can exit mid-refresh and sign the account out.
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
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

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


REFRESH_RETRY = 600.0  # after a failed token renewal, leave the account alone this long
_refresh_failed: Dict[str, tuple] = {}
_refresh_locks: Dict[str, threading.Lock] = {}
_NO_USAGE = {"windows": [], "details": [], "extra_usage": None, "needs_login": False}


def _unavailable(reason: str, needs_login: bool = False) -> Dict[str, Any]:
    return dict(_NO_USAGE, unavailable=reason, needs_login=needs_login)


def _usage_for(config_dir: str) -> Dict[str, Any]:
    """``{windows, details, extra_usage, unavailable, needs_login}`` for one login; cached briefly."""
    now = time.time()
    with _lock:
        hit = _usage_cache.get(config_dir)
        if hit and now - hit[0] < USAGE_TTL:
            return hit[1]
    result = _fetch_usage(config_dir)
    with _lock:
        _usage_cache[config_dir] = (now, result)
    return result


def _renew_token(config_dir: str, force: bool = False) -> Dict[str, Any]:
    """Renew an expired access token through Claude Code; one renewal per login at a time, and a
    login whose renewal failed is not retried on every poll."""
    accounts = _module("accounts")
    with _lock:
        failed = _refresh_failed.get(config_dir)
        lock = _refresh_locks.setdefault(config_dir, threading.Lock())
    if failed and not force and time.time() - failed[0] < REFRESH_RETRY:
        return {"ok": False, "error": failed[1]}
    with lock:
        if not accounts.token_expired(config_dir):
            return {"ok": True, "error": ""}  # renewed while this call waited
        result = accounts.refresh_token(config_dir)
    with _lock:
        if result["ok"]:
            _refresh_failed.pop(config_dir, None)
            _status_cache.pop(config_dir, None)
        else:
            _refresh_failed[config_dir] = (time.time(), result["error"])
    return result


def _money(minor, places) -> Optional[float]:
    if isinstance(minor, bool) or not isinstance(minor, (int, float)):
        return None
    return minor / (10 ** (places if isinstance(places, int) and 0 <= places <= 6 else 2))


def _extra_usage(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What the account itself is set to do past a plan limit, and how much it has spent."""
    extra = payload.get("extra_usage")
    if not isinstance(extra, dict):
        return None
    places = extra.get("decimal_places")
    return {"enabled": extra.get("is_enabled") is True, "disabled_reason": extra.get("disabled_reason") or "",
            "used": _money(extra.get("used_credits"), places), "limit": _money(extra.get("monthly_limit"), places),
            "currency": str(extra.get("currency") or "USD")}


def _fetch_usage(config_dir: str) -> Dict[str, Any]:
    accounts = _module("accounts")
    provider = _module("__init__")
    limits = _module("limits")
    creds = accounts.read_oauth_credentials(config_dir)
    if not creds:
        return _unavailable("no login in this directory", needs_login=True)
    if accounts.token_expired(config_dir):
        renewed = _renew_token(config_dir)
        if not renewed["ok"]:
            return _unavailable(renewed["error"], needs_login="sign in again" in renewed["error"])
        creds = accounts.read_oauth_credentials(config_dir)
        if not creds:
            return _unavailable("no login in this directory", needs_login=True)
    import httpx
    headers = {"Authorization": f"Bearer {creds['accessToken']}", "anthropic-beta": "oauth-2025-04-20",
               "Accept": "application/json", "User-Agent": "claude-code/2.1.0"}
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(USAGE_URL, headers=headers)
    except httpx.HTTPError:
        return _unavailable("could not reach the usage API")
    if response.status_code in (401, 403):
        return _unavailable("Anthropic rejected this login's token — sign in again", needs_login=True)
    if not 200 <= response.status_code < 300:
        return _unavailable(f"usage API returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return _unavailable("usage API returned non-JSON")
    payload = payload if isinstance(payload, dict) else {}
    windows, details = provider.usage_windows(payload)
    extra = _extra_usage(payload)
    if extra and extra["enabled"]:
        # Past a plan limit this account pays from usage credits. With extra usage turned off
        # here, bench it at each spent limit instead of letting the next request be charged.
        limits.apply_extra_usage_policy(accounts.account_ref(config_dir), limits.spent_limits(payload))
    return {
        "windows": [{"label": w.label, "used_percent": w.used_percent,
                     "reset_at": w.reset_at.timestamp() if w.reset_at else None} for w in windows],
        "details": list(details), "extra_usage": extra, "unavailable": None, "needs_login": False,
    }


def _login_status(config_dir: str) -> Dict[str, Any]:
    now = time.time()
    with _lock:
        hit = _status_cache.get(config_dir)
        if hit and now - hit[0] < STATUS_TTL:
            return hit[1]
    # File read only. Never `claude auth status` here: that short-lived process can rotate the
    # single-use refresh token and exit before saving it, logging the account out (CLI <= 2.1.300).
    status = _module("accounts").login_state(config_dir)
    with _lock:
        _status_cache[config_dir] = (now, status)
    return status


def _benches(entry, config_dir: str) -> List[Dict[str, Any]]:
    """Every bench on one account: core's account-wide cooldown, then the plugin's own per
    category (a model family, or ``all`` when set by hand or by the extra-usage policy)."""
    limits = _module("limits")
    ref = _module("accounts").account_ref(config_dir)
    now = time.time()
    rows: List[Dict[str, Any]] = []
    reset_at = entry.last_error_reset_at
    if isinstance(reset_at, str):
        try:
            reset_at = datetime.fromisoformat(reset_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            reset_at = None
    if entry.last_status and entry.last_status != "ok" and (reset_at is None or reset_at > now):
        rows.append({"scope": limits.ALL, "until": reset_at, "reason": limits.REASON_LIMIT, "source": "core",
                     "detail": entry.last_error_reason or entry.last_status,
                     "message": (entry.last_error_message or "")[:200]})
    for scope, until in sorted(limits.benches().get(ref, {}).items(), key=lambda kv: (kv[0] != limits.ALL, kv[0])):
        open_ended = until - now > limits.UNTIL_CLEARED_SECONDS / 2
        rows.append({"scope": scope, "until": None if open_ended else until, "reason": limits.bench_reason(ref, scope),
                     "source": "plugin", "detail": "", "message": ""})
    return rows


def _entry_row(entry, config_dir: str, current_id: Optional[str]) -> Dict[str, Any]:
    limits = _module("limits")
    login = _login_status(config_dir)
    usage = _usage_for(config_dir) if login["logged_in"] else _unavailable("signed out — sign in again", needs_login=True)
    benches = _benches(entry, config_dir)  # after the usage read: that can bench for the extra-usage policy
    return {
        "id": entry.id, "label": entry.label, "priority": entry.priority, "config_dir": config_dir,
        "current": entry.id == current_id, "request_count": entry.request_count,
        "status": entry.last_status or "ok", "benches": benches, "benched": bool(benches),
        "status_at": entry.last_status_at,
        "logged_in": login["logged_in"], "plan": login["plan"], "login_error": login.get("error") or "",
        "extra_usage_allowed": limits.extra_usage_allowed(_module("accounts").account_ref(config_dir)),
        "usage": usage,
    }


def _entry(credential_id: str):
    """``(pool, entry, config_dir)`` of a pooled login, or 404."""
    from agent.credential_pool import load_pool
    pool = load_pool(PLUGIN_ID)
    for entry in pool.entries():
        config_dir = _module("accounts").account_dir(entry.access_token)
        if entry.id == credential_id and config_dir:
            return pool, entry, config_dir
    raise HTTPException(status_code=404, detail="no such pooled credential")


def _forget(config_dir: Optional[str] = None):
    with _lock:
        if config_dir is None:
            _usage_cache.clear()
            _status_cache.clear()
        else:
            _usage_cache.pop(config_dir, None)
            _status_cache.pop(config_dir, None)


@router.get("/accounts")
def list_accounts() -> Dict[str, Any]:
    from agent.credential_pool import load_pool
    accounts = _module("accounts")
    limits = _module("limits")
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
                   "usage": _usage_for(config_dir) if login["logged_in"] else _unavailable("signed out")}
    return {"provider": PLUGIN_ID, "accounts": rows, "default_login": default, "fetched_at": time.time(),
            "scopes": [limits.ALL, *limits.FAMILIES],
            "add_hint": f"hermes auth add {PLUGIN_ID} --label <name>"}


@router.post("/accounts/{credential_id}/bench")
def bench(credential_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Bench one account by hand for a category (``all`` or a model family), for *hours* or,
    with no hours, until it is cleared."""
    limits = _module("limits")
    _pool, entry, config_dir = _entry(credential_id)
    scope = str(body.get("scope") or "").strip().lower()
    if scope not in (limits.ALL, *limits.FAMILIES):
        raise HTTPException(status_code=400, detail="scope must be one of: " + ", ".join((limits.ALL, *limits.FAMILIES)))
    hours = body.get("hours")
    if hours in (None, "", 0):
        seconds = limits.UNTIL_CLEARED_SECONDS
    elif isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= 24 * 366:
        raise HTTPException(status_code=400, detail="hours must be a positive number, or omitted to bench until cleared")
    else:
        seconds = hours * 3600
    ref = _module("accounts").account_ref(config_dir)
    limits.clear(ref, scope)  # a manual bench replaces the one there, it may be shorter
    until = limits.bench(ref, scope, time.time() + seconds, reason=limits.REASON_MANUAL)
    return {"ok": True, "id": entry.id, "scope": scope, "until": None if hours in (None, "", 0) else until}


@router.post("/accounts/{credential_id}/clear-bench")
def clear_bench(credential_id: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Clear one category's bench, or with no scope every bench on the account."""
    limits = _module("limits")
    pool, entry, config_dir = _entry(credential_id)
    scope = str((body or {}).get("scope") or "").strip().lower() or None
    source = str((body or {}).get("source") or "").strip().lower() or None
    ref = _module("accounts").account_ref(config_dir)
    if source != "core":
        limits.clear(ref, scope)
    if source != "plugin" and scope in (None, limits.ALL):
        pool.reset_status(credential_id)
    return {"ok": True, "id": entry.id, "label": entry.label}


@router.post("/accounts/{credential_id}/extra-usage")
def extra_usage(credential_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Allow or forbid paying for this account's requests from usage credits once a plan limit
    is reached. Forbidding benches the account now at every limit it is already past."""
    limits = _module("limits")
    _pool, entry, config_dir = _entry(credential_id)
    allowed = body.get("allowed")
    if not isinstance(allowed, bool):
        raise HTTPException(status_code=400, detail="allowed must be true or false")
    limits.set_extra_usage_allowed(_module("accounts").account_ref(config_dir), allowed)
    _forget(config_dir)
    usage = _usage_for(config_dir)  # re-reads the limits and, when forbidden, benches at the spent ones
    return {"ok": True, "id": entry.id, "allowed": allowed, "checked": usage.get("unavailable") is None}


@router.post("/accounts/{credential_id}/refresh-token")
def refresh_token(credential_id: str) -> Dict[str, Any]:
    """Renew the login's access token now (Claude Code does it; no plan usage)."""
    _pool, entry, config_dir = _entry(credential_id)
    result = _renew_token(config_dir, force=True) if _module("accounts").token_expired(config_dir) else {"ok": True, "error": ""}
    _forget(config_dir)
    return {"ok": result["ok"], "id": entry.id, "error": result["error"]}


@router.post("/accounts/refresh")
def refresh() -> Dict[str, Any]:
    with _lock:
        _refresh_failed.clear()
    _forget()
    return {"ok": True}


@router.delete("/accounts/{credential_id}")
def remove_account(credential_id: str) -> Dict[str, Any]:
    _pool, entry, config_dir = _entry(credential_id)
    removed = _module("accounts").remove_account(credential_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="no such pooled credential")
    _module("limits").clear(_module("accounts").account_ref(config_dir))
    _forget()
    return {"ok": True, "id": removed.id, "label": removed.label}


# --- Add an account from the page: the official `claude auth login` on a pty -------------------
#
# Same shape as the vault form: the browser posts the pasted OAuth code straight to this process,
# which types it into the CLI's own prompt. The code never reaches chat, logs or the model; the
# OAuth grant lands in the new login's CLAUDE_CONFIG_DIR exactly as a terminal login would.

import os
import pty
import re
import select
import subprocess
import uuid

LOGIN_TTL = 600.0
_logins: Dict[str, "_Login"] = {}
_URL_RE = re.compile(r"https://claude\.com/\S+")
_ANSI_RE = re.compile(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;]*[A-Za-z]")


class _Login:
    def __init__(self, label: str, replace: Optional[tuple] = None):
        """A new login, or with *replace* ``(credential_id, config_dir)`` a pooled account signing
        in again. The sign-in always runs in a fresh directory, so a cancelled or failed attempt
        leaves the account's existing login exactly as it was."""
        accounts = _module("accounts")
        setup = _module("directsdk_setup")
        self.id = uuid.uuid4().hex[:12]
        self.label = label
        self.replace = replace
        self.created = time.time()
        self.config_dir = accounts.new_account_dir()
        self.url: Optional[str] = None
        self.state = "starting"  # starting | awaiting_code | verifying | done | failed
        self.error = ""
        self.output = ""
        self.entry_id: Optional[str] = None
        env = setup._child_env(dict(os.environ))
        env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        env["TERM"] = "dumb"
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        command = setup._resolve(None, env)
        if command is None:
            raise HTTPException(status_code=503, detail="Claude Code CLI is not installed on the dashboard host")
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(command + ["auth", "login", "--claudeai"], stdin=slave, stdout=slave, stderr=slave,
                                     env=env, close_fds=True)
        os.close(slave)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        accounts = _module("accounts")
        while True:
            r, _, _ = select.select([self.master], [], [], 0.5)
            if r:
                try:
                    chunk = os.read(self.master, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                text = _ANSI_RE.sub("", chunk.decode("utf-8", "replace"))
                self.output = (self.output + text)[-4000:]
                if self.url is None:
                    m = _URL_RE.search(self.output.replace("\r", "").replace("\n", ""))
                    if m:
                        self.url = m.group(0).rstrip(".,")
                if self.state == "starting" and "Paste code here" in self.output:
                    self.state = "awaiting_code"
                if self.state == "verifying" and "Invalid code" in self.output.split("Paste code here")[-1]:
                    self.state = "awaiting_code"
                    self.error = "Invalid code — copy the full code from the Claude page and try again."
                    self.output = ""
            if self.proc.poll() is not None:
                break
        rc = self.proc.returncode
        status = accounts.auth_status(self.config_dir)
        if status["logged_in"] and self.replace:
            try:
                self.entry_id = self._swap_in()
                self.state = "done"
            except Exception as exc:
                log.warning("re-authentication could not replace the login: %s", type(exc).__name__)
                self.state = "failed"
                self.error = "Signed in, but the new login could not replace the old one; the account is unchanged."
                accounts._remove_dir(self.config_dir)
            _forget()
        elif status["logged_in"]:
            entry = accounts.register_dir(self.config_dir, self.label or f"claude-{status['plan'] or 'subscription'}")
            self.entry_id = entry.id
            self.state = "done"
            _forget()
        else:
            self.state = "failed"
            self.error = self.error or f"`claude auth login` exited with {rc} without a login"
            accounts._remove_dir(self.config_dir)
        try:
            os.close(self.master)
        except OSError:
            pass

    def _swap_in(self) -> str:
        """Put the fresh login where the account's old one was: same pool row, same directory
        path (so its benches, priority and label stay), new credentials."""
        accounts = _module("accounts")
        credential_id, old = self.replace
        old_path, new_path = Path(old), Path(self.config_dir)
        if old_path.resolve().is_relative_to(accounts.accounts_root().resolve()):
            aside = old_path.with_name(old_path.name + ".replaced-" + uuid.uuid4().hex[:6])
            os.rename(old_path, aside)
            try:
                os.rename(new_path, old_path)
            except OSError:
                os.rename(aside, old_path)
                raise
            accounts._remove_dir(aside)
        else:
            # A directory the operator adopted: keep it theirs, take only the login files.
            import shutil
            for name in (".credentials.json", ".claude.json"):
                if (new_path / name).exists():
                    shutil.copy2(new_path / name, old_path / (name + ".new"))
                    os.replace(old_path / (name + ".new"), old_path / name)
            accounts._remove_dir(new_path)
        self.config_dir = old_path  # the login is in place from here on: nothing below may undo it
        with _lock:
            _refresh_failed.pop(str(old_path), None)
        try:
            from agent.credential_pool import load_pool
            pool = load_pool(PLUGIN_ID)
            entry = next((e for e in pool.entries() if e.id == credential_id), None)
            if entry is not None and entry.last_status and entry.last_status != "exhausted":
                pool.reset_status(credential_id)  # an auth failure is over; a plan-limit bench still stands
        except Exception as exc:
            log.warning("re-authenticated, but the pool status was not reset: %s", type(exc).__name__)
        return credential_id

    def submit(self, code: str):
        if self.state != "awaiting_code":
            raise HTTPException(status_code=409, detail=f"login is {self.state}")
        self.state = "verifying"
        self.error = ""
        os.write(self.master, (code.strip() + "\r").encode())

    def cancel(self):
        if self.proc.poll() is None:
            self.proc.kill()

    def view(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "state": self.state, "url": self.url, "error": self.error,
                "entry_id": self.entry_id, "reauth": bool(self.replace), "expires_in": max(0, int(self.created + LOGIN_TTL - time.time()))}


def _reap():
    now = time.time()
    for key, login in list(_logins.items()):
        if now - login.created > LOGIN_TTL or login.state in ("done", "failed") and now - login.created > 120:
            login.cancel()
            _logins.pop(key, None)


def _login(login_id: str) -> "_Login":
    _reap()
    login = _logins.get(login_id)
    if login is None:
        raise HTTPException(status_code=404, detail="login session expired or unknown")
    return login


@router.post("/logins")
def start_login(body: Dict[str, Any]) -> Dict[str, Any]:
    _reap()
    label = str(body.get("label") or "").strip()[:60]
    replace = None
    credential_id = str(body.get("credential_id") or "").strip()
    if credential_id:  # sign an existing account in again, in place
        _pool, entry, config_dir = _entry(credential_id)
        label, replace = entry.label, (entry.id, config_dir)
    login = _Login(label, replace)
    _logins[login.id] = login
    deadline = time.time() + 15
    while login.state == "starting" and login.proc.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    return login.view()


@router.get("/logins/{login_id}")
def login_status(login_id: str) -> Dict[str, Any]:
    return _login(login_id).view()


@router.post("/logins/{login_id}/code")
def login_code(login_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    code = str(body.get("code") or "").strip()
    if not code or len(code) > 600:
        raise HTTPException(status_code=400, detail="paste the code from the Claude page")
    login = _login(login_id)
    login.submit(code)
    deadline = time.time() + 25
    while login.state == "verifying" and time.time() < deadline:
        time.sleep(0.25)
    return login.view()


@router.delete("/logins/{login_id}")
def login_cancel(login_id: str) -> Dict[str, Any]:
    login = _logins.pop(login_id, None)
    if login:
        login.cancel()
    return {"ok": True}
