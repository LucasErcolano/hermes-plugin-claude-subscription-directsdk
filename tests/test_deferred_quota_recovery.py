"""Bounded Claude subscription recovery: wrap around after the last account.

Mock only the native requests, not Hermes' credential-pool selection/cooldowns.
"""
import asyncio
import sys
import threading
import time

import pytest

import accounts
from conftest import PLUGIN_NAME


def _pool(tmp_path, names=("first", "second")):
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool
    pool = load_pool(PLUGIN_NAME)
    refs = {}
    for name in names:
        ref = accounts.account_ref(tmp_path / name)
        refs[name] = ref
        pool.add_entry(PooledCredential(
            provider=PLUGIN_NAME, id=name, label=name, auth_type=AUTH_TYPE_API_KEY,
            priority=0, source=SOURCE_MANUAL, access_token=ref, base_url=accounts.BASE_URL))
    return refs


def _client(profile, refs, monkeypatch, wait=2, cycles=2):
    client = profile.create_client(api_key=refs[next(iter(refs))])
    module = sys.modules[type(client).__module__]
    monkeypatch.setattr(module, "quota_retry_settings", lambda: (wait, cycles))
    return client, module.ClaudeAPIError


def _quota(error_type, reset):
    error = error_type("subscription session limit", 429,
                       {"error": {"type": "usage_limit_reached", "resets_at": reset}})
    error.limit_scope = "all"
    return error


def test_fourth_quota_rotates_to_first_reopened_account_within_same_call(
        profile, tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    refs = _pool(tmp_path, ("first", "second", "third", "fourth"))
    client, error_type = _client(profile, refs, monkeypatch)
    calls = []

    def attempt(request, **kwargs):
        name = next(n for n, ref in refs.items() if ref == request.api_key)
        calls.append(name)
        if name == "first" and len(calls) > 4:
            return "first reopened successfully"
        if name == "fourth":
            # First account's reset expired while others were in use.
            load_pool(PLUGIN_NAME).reset_status("first")
        raise _quota(error_type, time.time() + 3600)

    monkeypatch.setattr(client, "_create_one", attempt)
    try:
        assert client.create(model="sonnet") == "first reopened successfully"
        assert calls == ["first", "second", "third", "fourth", "first"]
    finally:
        client.close()


def test_wait_for_nearby_known_reset_without_holding_a_native_slot(
        profile, tmp_path, monkeypatch):
    refs = _pool(tmp_path, ("first",))
    client, error_type = _client(profile, refs, monkeypatch, wait=2)
    calls = []
    reset = time.time() + 0.22

    def attempt(request, **kwargs):
        calls.append(request.api_key)
        if len(calls) == 1:
            raise _quota(error_type, reset)
        return "resumed"

    monkeypatch.setattr(client, "_create_one", attempt)
    try:
        started = time.monotonic()
        assert client.create(model="sonnet") == "resumed"
        assert time.monotonic() - started >= 0.10
        assert calls == [refs["first"], refs["first"]]
    finally:
        client.close()


@pytest.mark.parametrize("wait,cycles", [(0, 2), (2, 0), (0, 0)])
def test_disabled_recovery_does_not_wait_or_cycle(
        profile, tmp_path, monkeypatch, wait, cycles):
    refs = _pool(tmp_path, ("first",))
    client, error_type = _client(profile, refs, monkeypatch, wait=wait, cycles=cycles)
    calls = []

    def attempt(request, **kwargs):
        calls.append(request.api_key)
        raise _quota(error_type, time.time() + 0.3)

    monkeypatch.setattr(client, "_create_one", attempt)
    try:
        started = time.monotonic()
        with pytest.raises(error_type):
            client.create(model="sonnet")
        assert time.monotonic() - started < 0.2
        assert calls == [refs["first"]]
    finally:
        client.close()


def test_distant_reset_exceeds_budget_and_fails_fast(profile, tmp_path, monkeypatch):
    refs = _pool(tmp_path, ("first",))
    client, error_type = _client(profile, refs, monkeypatch, wait=1)
    calls = []
    def attempt(request, **kwargs):
        calls.append(request.api_key)
        raise _quota(error_type, time.time() + 4000)
    monkeypatch.setattr(client, "_create_one", attempt)
    try:
        started = time.monotonic()
        with pytest.raises(error_type):
            client.create(model="sonnet")
        assert time.monotonic() - started < .4
        assert len(calls) == 1
    finally:
        client.close()


def test_recovery_is_bounded_after_repeated_immediate_reopenings(
        profile, tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    refs = _pool(tmp_path, ("first", "second"))
    client, error_type = _client(profile, refs, monkeypatch, wait=2, cycles=2)
    calls = []

    def attempt(request, **kwargs):
        name = next(n for n, ref in refs.items() if ref == request.api_key)
        calls.append(name)
        # Give another attempt immediate availability, even if its latest
        # response was a 429. Every retry cycle must still be bounded.
        if name == "second":
            load_pool(PLUGIN_NAME).reset_status("first")
        raise _quota(error_type, time.time() + 3600)

    monkeypatch.setattr(client, "_create_one", attempt)
    try:
        with pytest.raises(error_type):
            client.create(model="sonnet")
        assert calls == ["first", "second"] * 3  # initial + 2 more pool cycles
    finally:
        client.close()


@pytest.mark.parametrize("async_mode", [False, True])
def test_cancelled_wait_returns_quickly_without_retry(
        profile, tmp_path, monkeypatch, async_mode):
    refs = _pool(tmp_path, ("first",))
    client, error_type = _client(profile, refs, monkeypatch, wait=5)
    calls = []
    started_wait = threading.Event()

    def attempt(request, **kwargs):
        calls.append(request.api_key)
        raise _quota(error_type, time.time() + 3.5)

    monkeypatch.setattr(client, "_create_one", attempt)
    module = sys.modules[type(client).__module__]
    original_sleep = module.QuotaRecovery.sleep
    def observe_sleep(self, seconds):
        started_wait.set()
        return original_sleep(self, seconds)
    monkeypatch.setattr(module.QuotaRecovery, "sleep", observe_sleep)
    try:
        if async_mode:
            async def run():
                job = asyncio.create_task(client._acreate(model="sonnet"))
                assert await asyncio.to_thread(started_wait.wait, 3)
                job.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await job
            asyncio.run(run())
        else:
            result = []
            def run():
                try:
                    client.create(model="sonnet")
                except Exception as error:
                    result.append(error)
            thread = threading.Thread(target=run)
            thread.start()
            assert started_wait.wait(3)
            client.cancel()
            thread.join(2)
            assert not thread.is_alive()
            assert len(result) == 1 and "cancelled" in str(result[0])
        assert calls == [refs["first"]]
    finally:
        client.close()


def test_streaming_wait_heartbeats_then_resume_without_replay(
        profile, tmp_path, monkeypatch):
    refs = _pool(tmp_path, ("first",))
    client, error_type = _client(profile, refs, monkeypatch, wait=2)
    calls = []
    reset = time.time() + .18

    def attempt(request, **kwargs):
        calls.append(request.api_key)
        if len(calls) == 1:
            def limited():
                yield client._chunk(kwargs["model"], {})  # liveness, not output
                raise _quota(error_type, reset)
            return limited()
        def recovered():
            yield client._chunk(kwargs["model"], {"content": "done"})
        return recovered()

    monkeypatch.setattr(client, "_create_one", attempt)
    try:
        chunks = list(client.create(model="sonnet", stream=True))
        assert [c.choices[0].delta.content for c in chunks][-1] == "done"
        assert len(calls) == 2
        assert not any(c.choices[0].delta.content for c in chunks[:-1])
    finally:
        client.close()


def test_initially_exhausted_auxiliary_client_recovers_after_reset(
        profile, tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    refs = _pool(tmp_path, ("first",))
    pool = load_pool(PLUGIN_NAME)
    pool.mark_exhausted_and_rotate(
        status_code=429, api_key_hint=refs["first"],
        error_context={"reason": "usage_limit_reached", "reset_at": time.time() + .22})
    client = profile.create_client(api_key=PLUGIN_NAME)
    module = sys.modules[type(client).__module__]
    monkeypatch.setattr(module, "quota_retry_settings", lambda: (2, 2))
    calls = []
    def attempt(request, **kwargs):
        calls.append(request.api_key)
        return "recovered"
    monkeypatch.setattr(client, "_create_one", attempt)
    try:
        assert client.create(model="sonnet") == "recovered"
        assert calls == [refs["first"]]
    finally:
        client.close()


def test_generic_429_is_not_a_quota_reset_wait(
        profile, tmp_path, monkeypatch):
    refs = _pool(tmp_path, ("first",))
    client, error_type = _client(profile, refs, monkeypatch)
    calls = []
    def attempt(request, **kwargs):
        calls.append(request.api_key)
        raise error_type("requests too fast", 429, {"error": {"type": "rate_limit_error"}})
    monkeypatch.setattr(client, "_create_one", attempt)
    try:
        with pytest.raises(error_type, match="requests too fast"):
            client.create(model="sonnet")
        assert calls == [refs["first"]]
    finally:
        client.close()
