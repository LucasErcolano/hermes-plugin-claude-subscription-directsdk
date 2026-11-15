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
ACCOUNT_WIDE_CLAIMS = frozenset({'five_hour', 'seven_day', 'seven_day_oauth_apps'})
# The weekly limit of the models whose use past it is paid from usage credits (Fable today).
# The claim names no model: it is the requested model's own limit ("You're out of usage
# credits. Switch to another model"), so every other model on the account still works.
REQUESTED_MODEL_CLAIMS = frozenset({'seven_day_overage_included'})
DEFAULT_BENCH_SECONDS = 3600
UNTIL_CLEARED_SECONDS = 10 * 365 * 86400  # a manual bench with no end date
AMBIENT = 'ambient'
# Why a bench exists, for the dashboard: an upstream limit, the operator, or the extra-usage policy.
REASON_LIMIT, REASON_MANUAL, REASON_EXTRA_USAGE = 'limit', 'manual', 'extra_usage'
HEADER_PREFIX = 'anthropic-ratelimit-unified-'


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
        if claim in REQUESTED_MODEL_CLAIMS:
            return family(model)
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


def bench(account, scope, until=None, reason=REASON_LIMIT):
    """Bench *account* for *scope* (a model family, or ``'all'``) until *until* (epoch seconds)."""
    until = float(until) if isinstance(until, (int, float)) and until > time.time() else time.time() + DEFAULT_BENCH_SECONDS

    def apply(data):
        fams = data.setdefault(_key(account), {})
        fams[scope] = max(until, fams.get(scope, 0))
    _locked(apply)
    _set_reason(account, scope, reason)
    return until


def benched_until(account, model):
    """When *account* can serve *model* again, ``None`` if it can now. A bench on ``'all'``
    (set by the operator or the extra-usage policy) covers every model."""
    fams = _locked().get(_key(account), {})
    now = time.time()
    hits = [until for until in (fams.get(family(model)), fams.get(ALL)) if isinstance(until, (int, float)) and until > now]
    return max(hits) if hits else None


def benches():
    """Active benches kept here: ``{account_ref|'ambient': {family|'all': until}}``."""
    return _locked()


def clear(account=None, scope=None, reason=None):
    """Drop benches: all of them, one account's, one scope's, or only those made for *reason*."""
    reasons = settings().get('bench_reasons', {}) if reason is not None else {}

    def apply(data):
        for acct in list(data):
            if account is not None and acct != _key(account):
                continue
            for fam in list(data[acct]):
                if scope is not None and fam != scope:
                    continue
                if reason is not None and reasons.get(acct, {}).get(fam, REASON_LIMIT) != reason:
                    continue
                data[acct].pop(fam)
            if not data[acct]:
                data.pop(acct)
    _locked(apply)


# --- Operator settings and bench reasons -----------------------------------------------------------
#
# Kept apart from the bench store, whose shape ({account: {scope: until}}) older plugin versions
# read and rewrite: anything else stored there would be pruned by a gateway still running them.

def _settings_path() -> Path:
    return _store().with_name('settings.json')


def settings(update=None):
    """The plugin's settings file; *update* mutates it under the lock."""
    path = _settings_path()
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


def _set_reason(account, scope, reason):
    active = benches()

    def apply(data):
        reasons = data.setdefault('bench_reasons', {})
        reasons.setdefault(_key(account), {})[scope] = reason
        for acct in list(reasons):  # forget reasons of benches that have ended
            reasons[acct] = {fam: why for fam, why in reasons[acct].items() if fam in active.get(acct, {})}
            if not reasons[acct]:
                reasons.pop(acct)
    settings(apply)


def bench_reason(account, scope) -> str:
    return settings().get('bench_reasons', {}).get(_key(account), {}).get(scope, REASON_LIMIT)


def extra_usage_allowed(account) -> bool:
    """Whether requests on *account* may be paid from usage credits ("extra usage") once a plan
    limit is reached. Default yes: that is what the account itself is set to do upstream."""
    return settings().get('extra_usage_allowed', {}).get(_key(account), True) is not False


def set_extra_usage_allowed(account, allowed):
    def apply(data):
        data.setdefault('extra_usage_allowed', {})[_key(account)] = bool(allowed)
    settings(apply)
    if allowed:
        clear(account, reason=REASON_EXTRA_USAGE)


# --- Extra usage: which limits an account is past ---------------------------------------------------

def _epoch(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            pass
        try:
            from datetime import datetime, timezone
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    return None


def spent_limits(payload):
    """``[(scope, reset_epoch|None)]`` for every plan limit ``/api/oauth/usage`` reports as used
    up: the session and all-models week are account-wide, a model's own week is that family.
    A request inside one of these is refused, or paid from usage credits when those are on."""
    found = []
    for entry in payload.get('limits') if isinstance(payload.get('limits'), list) else []:
        if not isinstance(entry, dict):
            continue
        percent = entry.get('percent')
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or percent < 100:
            continue
        scope = entry.get('scope') if isinstance(entry.get('scope'), dict) else {}
        model = scope.get('model') if isinstance(scope.get('model'), dict) else {}
        name = model.get('display_name') or model.get('id')
        if entry.get('kind') in ('session', 'weekly_all'):
            found.append((ALL, _epoch(entry.get('resets_at'))))
        elif isinstance(name, str) and family(name) in FAMILIES:
            found.append((family(name), _epoch(entry.get('resets_at'))))
    return found


def overage_in_use(headers) -> bool:
    """Whether upstream's rate-limit headers say this response was paid from usage credits."""
    if not isinstance(headers, dict):
        return False
    return (str(headers.get(HEADER_PREFIX + 'overage-in-use') or '').lower() == 'true'
            or str(headers.get(HEADER_PREFIX + 'representative-claim') or '').lower() == 'overage')


def overage_scope(headers, model):
    """``(scope, reset_epoch|None)`` of the plan limit an overage-paid response was past: an
    exhausted session or all-models week is account-wide, otherwise the requested model's own."""
    def window(short):
        status = str(headers.get(f'{HEADER_PREFIX}{short}-status') or '').lower()
        used = _epoch(headers.get(f'{HEADER_PREFIX}{short}-utilization'))
        return status == 'rejected' or (used is not None and used >= 1), _epoch(headers.get(f'{HEADER_PREFIX}{short}-reset'))
    for short in ('5h', '7d'):
        spent, reset = window(short)
        if spent:
            return ALL, reset
    _spent, reset = window('7d_oi')
    return family(model), reset or _epoch(headers.get(HEADER_PREFIX + 'reset'))


def apply_extra_usage_policy(account, spent):
    """Bench *account* at each spent limit when the operator has turned extra usage off for it.
    Returns the scopes benched."""
    if extra_usage_allowed(account):
        return []
    for scope, until in spent:
        bench(account, scope, until, reason=REASON_EXTRA_USAGE)
    return [scope for scope, _until in spent]
