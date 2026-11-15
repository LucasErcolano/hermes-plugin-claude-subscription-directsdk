"""Which subscription limit a 429 hit, and per-account benches for model-scoped limits.

A Claude subscription has account-wide limits (the 5-hour session, the all-models week, the
monthly spend cap on usage credits) and model-scoped ones (a model's own weekly limit, e.g.
Fable's). An account-wide 429 blocks every model on that account; a model-scoped one blocks
only that model family. Hermes core's credential pool benches a whole credential on any 429
for this provider, so a Fable limit used to take Opus down with it on the same account.

Account-wide limits stay with core (``mark_exhausted_and_rotate``: the credential is out for
every model, which is the truth). Model-scoped limits are benched here instead, per
(account, family), and core never sees them as a credential failure: the client rotates to an
account that can still serve the family, and when none can it raises an error core treats as
"this model is rate-limited upstream, the credential is healthy" so the fallback chain moves on
(for example to Opus on the same accounts).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: single-writer best effort
    fcntl = None

ALL = 'all'
FAMILIES = ('fable', 'opus', 'sonnet', 'haiku')
# ``anthropic-ratelimit-unified-representative-claim`` values that bind the whole account.
# Named family claims are scoped; unknown claims remain account-wide rather than guessing.
ACCOUNT_WIDE_CLAIMS = frozenset({'five_hour', 'seven_day', 'seven_day_overage_included', 'seven_day_oauth_apps'})
DEFAULT_BENCH_SECONDS = 3600
AMBIENT = 'ambient'


def family(model) -> str:
    """The rate-limit family of a model id or alias: fable/opus/sonnet/haiku, else the bare id."""
    text = str(model or '').lower()
    for name in FAMILIES:
        if name in text:
            return name
    return text.split('[', 1)[0] or 'unknown'


def scope_for(claim, message, model) -> str:
    """``'all'`` for an account-wide limit, else the family the limit is scoped to.

    The upstream claim header decides when present. Without it, a message explicitly naming
    a model family scopes the limit. Unlabelled limits and unknown claim names remain
    account-wide: the selected model alone is not evidence of the server's quota category.
    """
    if isinstance(claim, str) and claim.strip():
        claim = claim.strip().lower()
        if claim in ACCOUNT_WIDE_CLAIMS or claim in ('all', 'weekly_all', 'session'):
            return ALL
        for name in FAMILIES:
            if name in claim:
                return name
        return ALL  # unknown server claim: never infer its scope from the selected model
    text = str(message or '').lower()
    for name in FAMILIES:
        if name in text:
            return name
    if 'session limit' in text or 'weekly limit' in text:
        return ALL
    return ALL


def _store() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    try:
        from .directsdk_setup import PLUGIN_ID
    except ImportError:
        from directsdk_setup import PLUGIN_ID
    root = plugin_data_dir(PLUGIN_ID)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / 'model_benches.json'


def _key(account) -> str:
    return account if isinstance(account, str) and account.startswith('claude-config-dir:') else AMBIENT


def _locked(update=None):
    path = _store()
    with open(path, 'a+', encoding='utf-8') as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            try:
                data = json.loads(fh.read() or '{}')
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            now = time.time()
            data = {acct: {fam: until for fam, until in fams.items() if isinstance(until, (int, float)) and until > now}
                    for acct, fams in data.items() if isinstance(fams, dict)}
            data = {acct: fams for acct, fams in data.items() if fams}
            if update is not None:
                update(data)
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(data, sort_keys=True))
                fh.flush()
                os.fsync(fh.fileno())
            return data
        finally:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)


def bench(account, scope, until=None):
    """Bench *account* for model family *scope* until *until* (epoch seconds)."""
    until = float(until) if isinstance(until, (int, float)) and until > time.time() else time.time() + DEFAULT_BENCH_SECONDS

    def apply(data):
        fams = data.setdefault(_key(account), {})
        fams[scope] = max(until, fams.get(scope, 0))
    _locked(apply)
    return until


def benched_until(account, model):
    """When *account* can serve *model* again, ``None`` if it can now."""
    until = _locked().get(_key(account), {}).get(family(model))
    return until if isinstance(until, (int, float)) and until > time.time() else None


def benches():
    """Active model-scoped benches: ``{account_ref|'ambient': {family: until}}``."""
    return _locked()


def clear(account=None, scope=None):
    def apply(data):
        for acct in list(data):
            if account is not None and acct != _key(account):
                continue
            if scope is None:
                data.pop(acct)
            else:
                data[acct].pop(scope, None)
    _locked(apply)
