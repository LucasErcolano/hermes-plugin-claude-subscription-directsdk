import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics
from test_directsdk import Contract


def records(caplog):
    return [json.loads(r.message.split('DirectSDK diagnostic ', 1)[1])
            for r in caplog.records if r.name == 'claude_directsdk.diagnostics']


def test_redaction_and_metadata(caplog, monkeypatch):
    monkeypatch.setenv('EXAMPLE_API_KEY', 'synthetic-env-secret')
    with caplog.at_level(logging.WARNING):
        diagnostics.record('request_error', 'fable',
            error=RuntimeError('failure synthetic-env-secret Bearer tok-example sk-test-123 accessToken="abc123" https://host/path?key=abc'),
            api_key='synthetic-pool-secret',
            stderr=['synthetic-pool-secret\n'],
            admission=NS(status=529, request_id='req_test', used=True, denied=1,
                         failure=None, capture=NS(complete=False)))
    out = records(caplog)[0]
    assert out['upstream_status'] == 529
    assert out['upstream_request_id'] == 'req_test'
    assert out['capture_complete'] is False
    for secret in ('synthetic-env-secret', 'synthetic-pool-secret', 'tok-example', 'sk-test-123', 'abc123', 'https://host'):
        assert secret not in caplog.text


def test_logging_failure_does_not_raise(monkeypatch):
    def broken(*a, **k):
        raise OSError('disk full')
    monkeypatch.setattr(diagnostics.LOG, 'warning', broken)
    diagnostics.record('request_error', 'fable', error=ValueError('original'))


def test_native_failure_preserved_once_sync(tmp_path, caplog):
    import pytest
    import directsdk
    helper = Contract()
    client = helper.client(tmp_path, NATIVE_ERROR='authentication_failed:synthetic login failure')
    with pytest.raises(directsdk.ClaudeCodeLoggedOut, match='synthetic login failure'):
        client.chat.completions.create(**helper.request())
    out = records(caplog)
    assert len(out) == 1
    assert out[0]['exception'] == 'ClaudeCodeLoggedOut'
    assert 'synthetic login failure' in out[0]['message']
    assert out[0]['stack']
    client.close()


def test_native_failure_preserved_stream(tmp_path, caplog):
    import pytest
    import directsdk
    helper = Contract()
    client = helper.client(tmp_path, NATIVE_ERROR='authentication_failed:synthetic stream failure')
    args = helper.request()
    args['stream'] = True
    with pytest.raises(directsdk.ClaudeCodeLoggedOut):
        list(client.chat.completions.create(**args))
    assert len(records(caplog)) == 1
    client.close()


def test_preparation_failure_preserved(tmp_path, caplog):
    import pytest
    helper = Contract()
    client = helper.client(tmp_path)
    args = helper.request()
    args['messages'] = [{'role': 'invalid', 'content': 'not logged'}]
    with pytest.raises(ValueError):
        client.chat.completions.create(**args)
    assert len(records(caplog)) == 1
    assert 'not logged' not in caplog.text
    client.close()
