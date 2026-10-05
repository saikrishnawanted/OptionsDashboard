import pytest
from remote import RemoteAccess


def test_remote_requires_https_and_secret():
    for origin, token in [('http://13.200.243.229', 'x'*40), ('https://13.200.243.229', ''),
                          ('https://example.com/path', 'x'*40)]:
        with pytest.raises(ValueError):
            RemoteAccess(origin, token)


def test_origin_and_authenticated_proxy_are_both_required():
    access = RemoteAccess('https://13.200.243.229', 'x'*40)
    assert access.allowed_origin('https://13.200.243.229', '13.200.243.229')
    assert not access.allowed_origin('https://evil.example', '13.200.243.229')
    assert not access.allowed_origin('https://13.200.243.229', 'evil.example')
    assert access.authorized_proxy({'x-terminal-proxy-token': 'x'*40}, '127.0.0.1')
    assert not access.authorized_proxy({}, '127.0.0.1')
    assert not access.authorized_proxy({'x-terminal-proxy-token': 'x'*40}, '1.2.3.4')


def test_local_windows_behavior_unchanged():
    access = RemoteAccess()
    assert access.allowed_origin('http://127.0.0.1:8765', '127.0.0.1:8765')
    assert not access.allowed_origin('http://public.example', 'public.example')


def test_http_and_websocket_require_proxy_auth(monkeypatch, tmp_path):
    monkeypatch.setenv('TERMINAL_DATA_DIR', str(tmp_path / 'http'))
    import app
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setattr(app, 'remote_access', RemoteAccess('https://testserver', 'x'*40))
    with TestClient(app.app) as client:
        assert client.get('/api/bootstrap').status_code == 403
        headers = {'x-terminal-proxy-token': 'x'*40}
        bootstrap = client.get('/api/bootstrap', headers=headers)
        assert bootstrap.status_code == 200
        headers.update({'origin': 'https://evil.example', 'x-terminal-token': bootstrap.json()['token']})
        assert client.post('/api/demo', json={}, headers=headers).status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/ws', headers={'origin': 'https://testserver'}):
                pass


def test_linux_vault_roundtrip_and_permissions(tmp_path):
    import os
    if os.name == 'nt':
        pytest.skip('Linux filesystem permissions test')
    from vault import Vault
    vault = Vault(tmp_path / 'credentials.dpapi')
    vault.save({'consumer_key': 'unit-test-secret'})
    assert b'unit-test-secret' not in vault.path.read_bytes()
    assert vault.read()['consumer_key'] == 'unit-test-secret'
    assert vault.path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / 'vault.key').stat().st_mode & 0o777 == 0o600
    (tmp_path / 'vault.key').chmod(0o644)
    with pytest.raises(ValueError, match='chmod 600'):
        vault.read()
