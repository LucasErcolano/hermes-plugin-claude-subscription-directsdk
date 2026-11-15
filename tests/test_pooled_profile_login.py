import pytest
import directsdk_setup as setup


def test_routed_pool_login_wins_without_inherited_token(monkeypatch):
    monkeypatch.setattr(setup, '_secret_scope', lambda: (lambda name: None, True))
    monkeypatch.setattr(setup, '_setting', lambda key: None)
    env = {'CLAUDE_CODE_OAUTH_TOKEN': 'dummy-inherited', 'ANTHROPIC_CUSTOM_HEADERS': 'dummy-inherited'}
    setup.apply_profile_credential(env, pooled_dir='/synthetic/pool')
    assert env['CLAUDE_CONFIG_DIR'] == '/synthetic/pool'
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in env
    assert 'ANTHROPIC_CUSTOM_HEADERS' not in env


def test_routed_without_pool_still_refused(monkeypatch):
    monkeypatch.setattr(setup, '_secret_scope', lambda: (lambda name: None, True))
    monkeypatch.setattr(setup, '_setting', lambda key: None)
    with pytest.raises(setup.ClaudeCodeLoggedOut):
        setup.apply_profile_credential({})


def test_pool_overrides_scoped_token(monkeypatch):
    monkeypatch.setattr(setup, '_secret_scope', lambda: (lambda name: 'dummy-scoped', True))
    monkeypatch.setattr(setup, '_setting', lambda key: None)
    env = {}
    setup.apply_profile_credential(env, pooled_dir='/synthetic/pool')
    assert env['CLAUDE_CONFIG_DIR'] == '/synthetic/pool'
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in env
