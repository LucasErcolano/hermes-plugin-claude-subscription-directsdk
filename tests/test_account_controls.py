"""Operator controls: benches per limit category (seen, set and cleared by hand), the extra-usage
policy, token renewal through Claude Code, and signing an account in again in place."""
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import accounts
import limits
from conftest import PLUGIN_NAME

FAR = 4102444800
# The shape `/api/oauth/usage` returned for a Team account past its Fable week with usage credits on.
USAGE = {
    'extra_usage': {'is_enabled': True, 'monthly_limit': 255000, 'used_credits': 251034.0, 'currency': 'USD', 'decimal_places': 2},
    'limits': [
        {'kind': 'session', 'percent': 0, 'resets_at': None, 'scope': None},
        {'kind': 'weekly_all', 'percent': 51, 'resets_at': '2099-11-21T08:59:59+00:00', 'scope': None},
        {'kind': 'weekly_scoped', 'percent': 100, 'resets_at': '2099-11-21T09:00:00+00:00',
         'scope': {'model': {'id': None, 'display_name': 'Fable'}, 'surface': None}},
    ],
}


def _creds(path, expires_in=3600, **extra):
    path.mkdir(parents=True, exist_ok=True)
    oauth = {'accessToken': 'access', 'refreshToken': 'refresh', 'expiresAt': int((time.time() + expires_in) * 1000),
             'subscriptionType': 'max', **extra}
    (path / '.credentials.json').write_text(json.dumps({'claudeAiOauth': oauth}), encoding='utf-8')
    return path


def _pool(tmp_path, names=('a', 'b')):
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool
    pool = load_pool(PLUGIN_NAME)
    for name in names:
        pool.add_entry(PooledCredential(
            provider=PLUGIN_NAME, id=name, label=name, auth_type=AUTH_TYPE_API_KEY, priority=0,
            source=SOURCE_MANUAL, access_token=accounts.account_ref(_creds(tmp_path / name)), base_url=accounts.BASE_URL))
    return pool


@pytest.fixture
def api(monkeypatch):
    pytest.importorskip('fastapi')
    spec = importlib.util.spec_from_file_location('plugin_api_controls', Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provider = SimpleNamespace(usage_windows=lambda payload: ([], []))
    monkeypatch.setattr(module, '_module', lambda name: {'accounts': accounts, 'limits': limits, '__init__': provider}[name])
    return module


def _usage_api(monkeypatch, payload=USAGE, status=200):
    import httpx
    calls = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            calls.append(url)
            return httpx.Response(status, json=payload)
    monkeypatch.setattr(httpx, 'Client', FakeClient)
    return calls


# --- the bench store ----------------------------------------------------------------------------------

def test_a_bench_on_all_covers_every_model_and_a_family_bench_only_its_own(tmp_path):
    a = accounts.account_ref(tmp_path / 'a')
    limits.bench(a, limits.ALL, FAR, reason=limits.REASON_MANUAL)
    assert limits.benched_until(a, 'claude-fable-5-2[1m]') == limits.benched_until(a, 'claude-opus-5-5') == FAR
    limits.clear(a, limits.ALL)
    limits.bench(a, 'fable', FAR)
    assert limits.benched_until(a, 'fable') == FAR and limits.benched_until(a, 'opus') is None


def test_reasons_are_kept_beside_the_store_whose_shape_older_versions_rewrite(tmp_path):
    a = accounts.account_ref(tmp_path / 'a')
    limits.bench(a, 'fable', FAR, reason=limits.REASON_MANUAL)
    limits.bench(a, 'opus', FAR)
    assert json.loads(limits._store().read_text()) == {a: {'fable': FAR, 'opus': FAR}}
    assert limits.bench_reason(a, 'fable') == 'manual' and limits.bench_reason(a, 'opus') == 'limit'
    limits.clear(a, reason=limits.REASON_MANUAL)
    assert limits.benches() == {a: {'opus': FAR}}


def test_spent_limits_reads_the_usage_api():
    from datetime import datetime, timezone
    assert limits.spent_limits(USAGE) == [('fable', datetime(2099, 11, 21, 9, tzinfo=timezone.utc).timestamp())]
    session = {'limits': [{'kind': 'session', 'percent': 100, 'resets_at': None}, {'kind': 'weekly_all', 'percent': 99}]}
    assert limits.spent_limits(session) == [('all', None)]
    assert limits.spent_limits({}) == limits.spent_limits({'limits': 'nope'}) == []


def test_overage_headers():
    prefix = limits.HEADER_PREFIX
    assert not limits.overage_in_use({prefix + 'status': 'allowed'})
    assert limits.overage_in_use({prefix + 'overage-in-use': 'true'})
    assert limits.overage_in_use({prefix + 'representative-claim': 'overage'})
    scoped = {prefix + '5h-status': 'allowed', prefix + '7d-status': 'allowed', prefix + '7d_oi-status': 'rejected', prefix + '7d_oi-reset': str(FAR)}
    assert limits.overage_scope(scoped, 'claude-fable-5-2[1m]') == ('fable', FAR)
    assert limits.overage_scope({prefix + '7d-status': 'rejected', prefix + '7d-reset': str(FAR)}, 'claude-fable-5-2[1m]') == ('all', FAR)
    assert limits.overage_scope({prefix + '5h-utilization': '1.02', prefix + '5h-reset': '5'}, 'opus') == ('all', 5.0)


def test_extra_usage_policy_benches_only_when_blocked_and_unblocking_lifts_only_its_own(tmp_path):
    a = accounts.account_ref(tmp_path / 'a')
    assert limits.extra_usage_allowed(a)
    assert limits.apply_extra_usage_policy(a, [('fable', FAR)]) == [] and limits.benches() == {}
    limits.set_extra_usage_allowed(a, False)
    limits.bench(a, 'opus', FAR, reason=limits.REASON_MANUAL)
    assert limits.apply_extra_usage_policy(a, [('fable', FAR)]) == ['fable']
    assert limits.bench_reason(a, 'fable') == 'extra_usage'
    limits.set_extra_usage_allowed(a, True)
    assert limits.benches() == {a: {'opus': FAR}}


def test_an_answer_paid_from_credits_benches_the_account_when_extra_usage_is_blocked(profile, tmp_path):
    a = accounts.account_ref(tmp_path / 'a')
    client = profile.create_client(api_key=a)
    headers = {limits.HEADER_PREFIX + 'overage-in-use': 'true', limits.HEADER_PREFIX + '7d_oi-reset': str(FAR)}
    admission = SimpleNamespace(ratelimit=headers, request_id='req_1')
    client._note_extra_usage(a, 'claude-fable-5-2[1m]', admission)
    assert limits.benches() == {}  # allowed: nothing changes
    limits.set_extra_usage_allowed(a, False)
    client._note_extra_usage(a, 'claude-fable-5-2[1m]', SimpleNamespace(ratelimit={}, request_id='req_2'))
    assert limits.benches() == {}  # inside the plan: nothing to bench
    client._note_extra_usage(a, 'claude-fable-5-2[1m]', admission)
    assert limits.benches() == {a: {'fable': FAR}}
    assert limits.benched_until(a, 'claude-opus-5-5') is None


# --- the dashboard API --------------------------------------------------------------------------------

def test_dashboard_lists_benches_per_category_and_benches_and_clears_by_hand(api, tmp_path, monkeypatch):
    _pool(tmp_path)
    _usage_api(monkeypatch, {'limits': []})
    a = accounts.account_ref(tmp_path / 'a')
    limits.bench(a, 'fable', FAR)
    assert api.bench('a', {'scope': 'opus', 'hours': 5})['until'] == pytest.approx(time.time() + 5 * 3600, abs=30)
    assert api.bench('b', {'scope': 'all'})['until'] is None
    rows = {row['id']: row for row in api.list_accounts()['accounts']}
    assert [(b['scope'], b['reason'], b['source']) for b in rows['a']['benches']] == [
        ('fable', 'limit', 'plugin'), ('opus', 'manual', 'plugin')]
    assert [(b['scope'], b['until'], b['reason']) for b in rows['b']['benches']] == [('all', None, 'manual')]
    assert limits.benched_until(accounts.account_ref(tmp_path / 'b'), 'claude-haiku-5-5') is not None
    api.clear_bench('a', {'scope': 'fable'})
    assert limits.benches()[a] == {'opus': pytest.approx(time.time() + 5 * 3600, abs=30)}
    api.clear_bench('b', None)
    assert accounts.account_ref(tmp_path / 'b') not in limits.benches()
    with pytest.raises(api.HTTPException):
        api.bench('a', {'scope': 'gpt'})
    with pytest.raises(api.HTTPException):
        api.bench('nobody', {'scope': 'all'})


def test_dashboard_shows_and_clears_cores_account_wide_bench(api, tmp_path, monkeypatch):
    pool = _pool(tmp_path)
    _usage_api(monkeypatch, {'limits': []})
    pool.mark_exhausted_and_rotate(status_code=429, api_key_hint=accounts.account_ref(tmp_path / 'a'),
                                   error_context={'reason': 'usage_limit_reached', 'reset_at': FAR})
    row = next(r for r in api.list_accounts()['accounts'] if r['id'] == 'a')
    assert [(b['scope'], b['source']) for b in row['benches']] == [('all', 'core')]
    api.clear_bench('a', {'scope': 'all', 'source': 'core'})
    assert next(r for r in api.list_accounts()['accounts'] if r['id'] == 'a')['benches'] == []


def test_blocking_extra_usage_benches_at_the_spent_limit_now_and_allowing_lifts_it(api, tmp_path, monkeypatch):
    _pool(tmp_path)
    _usage_api(monkeypatch)
    a = accounts.account_ref(tmp_path / 'a')
    row = next(r for r in api.list_accounts()['accounts'] if r['id'] == 'a')
    assert row['extra_usage_allowed'] is True and row['benches'] == []
    assert row['usage']['extra_usage'] == {'enabled': True, 'disabled_reason': '', 'used': 2510.34, 'limit': 2550.0, 'currency': 'USD'}
    assert api.extra_usage('a', {'allowed': False})['checked'] is True
    row = next(r for r in api.list_accounts()['accounts'] if r['id'] == 'a')
    assert row['extra_usage_allowed'] is False
    assert [(b['scope'], b['reason']) for b in row['benches']] == [('fable', 'extra_usage')]
    assert limits.benched_until(a, 'claude-opus-5-5') is None
    api.extra_usage('a', {'allowed': True})
    assert next(r for r in api.list_accounts()['accounts'] if r['id'] == 'a')['benches'] == []
    with pytest.raises(api.HTTPException):
        api.extra_usage('a', {'allowed': 'no'})


def test_an_expired_token_is_renewed_instead_of_reported(api, tmp_path, monkeypatch):
    _pool(tmp_path, names=('a',))
    _creds(tmp_path / 'a', expires_in=-3600)
    calls = _usage_api(monkeypatch, {'limits': []})
    renewed = []

    def renew(config_dir, **_):
        renewed.append(config_dir)
        _creds(Path(config_dir))
        return {'ok': True, 'error': ''}
    monkeypatch.setattr(accounts, 'refresh_token', renew)
    usage = api.list_accounts()['accounts'][0]['usage']
    assert usage['unavailable'] is None and renewed == [str(tmp_path / 'a')] and len(calls) == 1


def test_a_failed_renewal_asks_for_sign_in_and_is_not_retried_every_poll(api, tmp_path, monkeypatch):
    _pool(tmp_path, names=('a',))
    _creds(tmp_path / 'a', expires_in=-3600)
    calls = _usage_api(monkeypatch, {'limits': []})
    attempts = []
    monkeypatch.setattr(accounts, 'refresh_token', lambda config_dir, **_: attempts.append(1) or {'ok': False, 'error': 'sign-in expired — sign in again'})
    for _ in range(3):
        api._usage_cache.clear()
        usage = api.list_accounts()['accounts'][0]['usage']
    assert usage['needs_login'] is True and 'sign in again' in usage['unavailable']
    assert len(attempts) == 1 and calls == []
    assert api.refresh_token('a')['ok'] is False and len(attempts) == 2  # asked for by hand: tried again


def test_signing_in_again_replaces_the_login_in_place(api, tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    monkeypatch.setattr(accounts, 'accounts_root', lambda: tmp_path)
    _pool(tmp_path, names=('a',))
    old = _creds(tmp_path / 'a', accessToken='old')
    (old / 'stale-session').write_text('x')
    limits.bench(accounts.account_ref(old), 'fable', FAR)
    login = api._Login.__new__(api._Login)
    login.replace, login.config_dir = ('a', str(old)), _creds(tmp_path / 'fresh', accessToken='new')
    assert login._swap_in() == 'a'
    assert accounts.read_oauth_credentials(old)['accessToken'] == 'new'
    assert not (tmp_path / 'fresh').exists() and not (old / 'stale-session').exists()
    assert [p.name for p in tmp_path.iterdir() if '.replaced-' in p.name] == []
    entry = load_pool(PLUGIN_NAME).entries()[0]
    assert (entry.id, entry.access_token) == ('a', accounts.account_ref(old))
    assert limits.benched_until(accounts.account_ref(old), 'fable') == FAR  # same account, same limits


# --- token renewal through Claude Code ----------------------------------------------------------------

NATIVE = r'''
import json, os, sys, time
cfg = os.path.join(os.environ['CLAUDE_CONFIG_DIR'], '.credentials.json')
open(os.path.join(os.environ['CLAUDE_CONFIG_DIR'], 'seen-base-url'), 'w').write(os.environ.get('ANTHROPIC_BASE_URL', ''))
if RENEW:
    data = json.load(open(cfg))
    data['claudeAiOauth']['expiresAt'] = int((time.time() + 3600) * 1000)
    json.dump(data, open(cfg, 'w'))
sys.exit(1)
'''


@pytest.mark.parametrize('renews', [True, False])
def test_refresh_token_runs_claude_code_against_a_local_stub(tmp_path, renews):
    config = _creds(tmp_path / 'login', expires_in=-3600)
    native = tmp_path / 'native.py'
    native.write_text(f'RENEW = {renews}\n' + NATIVE, encoding='utf-8')
    result = accounts.refresh_token(str(config), command=[sys.executable, str(native)], timeout=30)
    assert result['ok'] is renews
    assert (result['error'] == '') is renews
    # Claude Code was pointed at this process's loopback relay, never at Anthropic.
    assert (config / 'seen-base-url').read_text().startswith('http://127.0.0.1:')
    assert accounts.token_expired(str(config)) is (not renews)
