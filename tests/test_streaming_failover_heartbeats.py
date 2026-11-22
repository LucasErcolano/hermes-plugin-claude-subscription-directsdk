"""A heartbeat/progress chunk must not lock a streaming request to a quota-exhausted account.

These tests exercise the real RotatingStream and credential-pool 429 handling, while
stubbing only the network-facing individual attempt. No Claude login is required.
"""
import sys

import pytest

import accounts
from conftest import PLUGIN_NAME


def _pool(tmp_path):
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool

    pool = load_pool(PLUGIN_NAME)
    refs = {}
    for name in ("first", "second"):
        ref = accounts.account_ref(tmp_path / name)
        refs[name] = ref
        pool.add_entry(PooledCredential(
            provider=PLUGIN_NAME, id=name, label=name, auth_type=AUTH_TYPE_API_KEY,
            priority=0, source=SOURCE_MANUAL, access_token=ref, base_url=accounts.BASE_URL))
    return refs


def _quota_error(error_type):
    error = error_type("session limit", 429, {
        "error": {"type": "usage_limit_reached", "resets_at": 4102444800}
    })
    error.limit_scope = "all"
    return error


@pytest.mark.parametrize("early_deltas", [
    [{}],
    [{"content": ""}],
    [{}, {}, {"content": ""}],
])
def test_liveness_chunks_before_a_429_do_not_prevent_rotation(
        profile, tmp_path, monkeypatch, early_deltas):
    refs = _pool(tmp_path)
    client = profile.create_client(api_key=refs["first"])
    error_type = sys.modules[type(client).__module__].ClaudeAPIError
    attempts = []

    def fake_attempt(request, **kwargs):
        attempts.append(request.api_key)
        if request.api_key == refs["first"]:
            def limited():
                for delta in early_deltas:
                    yield client._chunk(kwargs["model"], delta)
                raise _quota_error(error_type)
            return limited()

        def successful():
            yield client._chunk(kwargs["model"], {"content": "recovered"})
        return successful()

    monkeypatch.setattr(client, "_create_one", fake_attempt)
    try:
        emitted = list(client.create(model="sonnet", stream=True))
        assert attempts == [refs["first"], refs["second"]]
        assert len(emitted) == len(early_deltas) + 1
        assert emitted[-1].choices[0].delta.content == "recovered"
        assert all(not chunk.choices[0].delta.content for chunk in emitted[:-1])
        from agent.credential_pool import load_pool
        first = next(e for e in load_pool(PLUGIN_NAME).entries() if e.id == "first")
        assert first.last_status == "exhausted"
    finally:
        client.close()


@pytest.mark.parametrize("delta,finish", [
    ({"content": "partial answer"}, None),
    ({"reasoning_content": "partial thinking"}, None),
    ({"tool_calls": [{"index": 0, "id": "toolu_test", "type": "function",
                       "function": {"name": "probe", "arguments": "{}"}}]}, None),
    ({"reasoning_details": [{"type": "native_assistant"}]}, None),
    ({}, "stop"),
])
def test_quota_error_after_real_output_never_replays_on_another_account(
        profile, tmp_path, monkeypatch, delta, finish):
    refs = _pool(tmp_path)
    client = profile.create_client(api_key=refs["first"])
    error_type = sys.modules[type(client).__module__].ClaudeAPIError
    attempts = []

    def fake_attempt(request, **kwargs):
        attempts.append(request.api_key)
        if request.api_key == refs["first"]:
            def limited():
                yield client._chunk(kwargs["model"], {})  # keepalive does not count
                yield client._chunk(kwargs["model"], delta, finish=finish)
                raise _quota_error(error_type)
            return limited()
        pytest.fail("Cannot replay partial streaming output on a different account")

    monkeypatch.setattr(client, "_create_one", fake_attempt)
    try:
        stream = client.create(model="sonnet", stream=True)
        assert not next(stream).choices[0].delta.content
        next(stream)
        with pytest.raises(error_type, match="session limit"):
            next(stream)
        assert attempts == [refs["first"]]
    finally:
        client.close()


def test_non_quota_error_is_not_rotated_even_after_keepalive(profile, tmp_path, monkeypatch):
    refs = _pool(tmp_path)
    client = profile.create_client(api_key=refs["first"])
    error_type = sys.modules[type(client).__module__].ClaudeAPIError
    attempts = []

    def fake_attempt(request, **kwargs):
        attempts.append(request.api_key)

        def broken():
            yield client._chunk(kwargs["model"], {})
            raise error_type("upstream failure", 503, {"error": {"type": "api_error"}})
        return broken()

    monkeypatch.setattr(client, "_create_one", fake_attempt)
    try:
        stream = client.create(model="sonnet", stream=True)
        next(stream)
        with pytest.raises(error_type, match="upstream failure"):
            next(stream)
        assert attempts == [refs["first"]]
    finally:
        client.close()
