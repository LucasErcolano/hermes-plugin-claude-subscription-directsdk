"""Per-scope benching: an account-wide limit benches the account, a model's own limit benches
only that model family, and core never sees a model-scoped limit as a credential failure."""
import sys

import pytest

import accounts
import limits
from conftest import PLUGIN_NAME


@pytest.mark.parametrize('claim,message,model,scope', [
    ('five_hour', '', 'claude-fable-5-2[1m]', 'all'),
    ('seven_day', '', 'claude-opus-5-5', 'all'),
    # The weekly limit of the credit-billed models: the requested model's own, not the account's.
    ('seven_day_overage_included', '', 'claude-fable-5-2[1m]', 'fable'),
    ('seven_day_opus', '', 'claude-opus-5-5[1m]', 'opus'),
    ('seven_day_omelette', '', 'claude-fable-5-2[1m]', 'all'),  # unknown claim is not inferred from model
    ('fable', '', 'claude-fable-5-2[1m]', 'fable'),
    ('all', '', 'claude-fable-5-2[1m]', 'all'),
    (None, "You've hit your session limit · resets 1pm (UTC)", 'claude-fable-5-2[1m]', 'all'),
    (None, "You've hit your weekly limit · resets Oct 23, 6pm (UTC)", 'claude-opus-5-5', 'all'),
    (None, "You've hit your Fable limit · resets Oct 23, 6pm (UTC)", 'claude-fable-5-2[1m]', 'fable'),
    (None, "This request would exceed your account's monthly spend limit.", 'claude-fable-5-2[1m]', 'all'),
])
def test_scope_for(claim, message, model, scope):
    assert limits.scope_for(claim, message, model) == scope


def test_family():
    assert limits.family('claude-fable-5-2[1m]') == limits.family('fable') == 'fable'
    assert limits.family('claude-opus-5-5') == 'opus'
    assert limits.family('some-new-model[1m]') == 'some-new-model'


def test_bench_store_scopes_by_family(profile, tmp_path):
    a = accounts.account_ref(tmp_path / 'a')
    limits.bench(a, 'fable', 4102444800)
    assert limits.benched_until(a, 'claude-fable-5-2[1m]') == 4102444800
    assert limits.benched_until(a, 'fable') == 4102444800
    assert limits.benched_until(a, 'claude-opus-5-5') is None
    assert limits.benched_until(accounts.account_ref(tmp_path / 'b'), 'fable') is None
    limits.bench(a, 'opus', 1)  # already past: a default-length bench, never a no-op
    assert limits.benched_until(a, 'claude-opus-5-5') is not None
    limits.clear(a)
    assert limits.benches() == {}


def _pool(tmp_path, names=('a', 'b')):
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool
    pool = load_pool(PLUGIN_NAME)
    for name in names:
        pool.add_entry(PooledCredential(
            provider=PLUGIN_NAME, id=name, label=name, auth_type=AUTH_TYPE_API_KEY, priority=0,
            source=SOURCE_MANUAL, access_token=accounts.account_ref(tmp_path / name), base_url=accounts.BASE_URL))
    return pool


def _limit(error_type, scope, reset=4102444800):
    exc = error_type('limit', 429, {'error': {'type': 'usage_limit_reached', 'resets_at': reset}})
    exc.limit_scope = scope
    return exc


def test_fable_limit_benches_fable_only_and_falls_back_without_benching_accounts(profile, tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    from agent.error_classifier import FailoverReason, classify_api_error
    _pool(tmp_path)
    ref = {n: accounts.account_ref(tmp_path / n) for n in 'ab'}
    client = profile.create_client(api_key=ref['a'])
    error_type = sys.modules[type(client).__module__].ClaudeAPIError
    calls = []

    def attempt(key, **kwargs):
        calls.append((key, kwargs['model']))
        if limits.family(kwargs['model']) == 'fable':
            raise _limit(error_type, 'fable')
        return 'opus answer'

    monkeypatch.setattr(client, '_create_once', attempt)
    try:
        with pytest.raises(error_type) as raised:
            client.create(model='claude-fable-5-2[1m]')
        # Both accounts tried once for Fable, then a model-scoped exhaustion.
        assert calls == [(ref['a'], 'claude-fable-5-2[1m]'), (ref['b'], 'claude-fable-5-2[1m]')]
        verdict = classify_api_error(raised.value, provider=PLUGIN_NAME, model='claude-fable-5-2[1m]')
        assert verdict.reason == FailoverReason.upstream_rate_limit and verdict.should_fallback
        # Core still has every account, for every model: nothing was benched credential-wide.
        pool = load_pool(PLUGIN_NAME)
        assert all(e.last_status in (None, 'ok') for e in pool.entries())
        assert pool.has_available(model='claude-opus-5-5[1m]')
        # A later Fable request fails fast, no account is asked again.
        with pytest.raises(error_type):
            client.create(model='fable')
        assert len(calls) == 2
        # Opus on the very same client and account still works.
        assert client.create(model='claude-opus-5-5[1m]') == 'opus answer'
        assert calls[-1] == (ref['a'], 'claude-opus-5-5[1m]')
    finally:
        client.close()


def test_account_wide_limit_benches_the_account_for_every_model(profile, tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    _pool(tmp_path)
    ref = {n: accounts.account_ref(tmp_path / n) for n in 'ab'}
    client = profile.create_client(api_key=ref['a'])
    error_type = sys.modules[type(client).__module__].ClaudeAPIError
    calls = []

    def attempt(key, **kwargs):
        calls.append(key)
        if key == ref['a']:
            raise _limit(error_type, limits.ALL)
        return 'from b'

    monkeypatch.setattr(client, '_create_once', attempt)
    try:
        assert client.create(model='claude-fable-5-2[1m]') == 'from b'
        assert calls == [ref['a'], ref['b']]
        pool = load_pool(PLUGIN_NAME)
        a = next(e for e in pool.entries() if e.id == 'a')
        assert a.last_status == 'exhausted' and a.last_error_reset_at == 4102444800
        assert limits.benches() == {}  # account-wide limits live in core's pool, not the model store
    finally:
        client.close()


def test_fable_limit_on_one_account_rotates_to_the_other(profile, tmp_path, monkeypatch):
    _pool(tmp_path)
    ref = {n: accounts.account_ref(tmp_path / n) for n in 'ab'}
    client = profile.create_client(api_key=ref['a'])
    error_type = sys.modules[type(client).__module__].ClaudeAPIError
    calls = []

    def attempt(key, **kwargs):
        calls.append(key)
        if key == ref['a']:
            raise _limit(error_type, 'fable')
        return 'fable from b'

    monkeypatch.setattr(client, '_create_once', attempt)
    try:
        assert client.create(model='claude-fable-5-2[1m]') == 'fable from b'
        assert client.create(model='claude-fable-5-2[1m]') == 'fable from b'
        assert calls == [ref['a'], ref['b'], ref['b']]  # a stays benched for Fable
    finally:
        client.close()


def test_streaming_rotates_before_output_only(profile, tmp_path, monkeypatch):
    _pool(tmp_path)
    ref = {n: accounts.account_ref(tmp_path / n) for n in 'ab'}
    client = profile.create_client(api_key=ref['a'])
    error_type = sys.modules[type(client).__module__].ClaudeAPIError

    class Fake:
        def __init__(self, key):
            self.key, self.closed = key, False

        def __iter__(self):
            return self

        def __next__(self):
            if self.key == ref['a']:
                raise _limit(error_type, 'fable')
            if getattr(self, 'done', False):
                raise StopIteration
            self.done = True
            return 'chunk-from-b'

        def close(self):
            self.closed = True

    monkeypatch.setattr(client, '_create_once', lambda key, **kw: Fake(key))
    try:
        assert list(client.create(model='claude-fable-5-2[1m]', stream=True)) == ['chunk-from-b']
    finally:
        client.close()

