"""Настройки продакшн-сервера: флаги и доверенный прокси."""

import serve


def test_flag_parsing(monkeypatch):
    for value in ('1', 'true', 'yes', 'on', ' TRUE '):
        monkeypatch.setenv('INTRAVIA_TEST_FLAG', value)
        assert serve._flag('INTRAVIA_TEST_FLAG'), value
    for value in ('', '0', 'false', 'off', 'no'):
        monkeypatch.setenv('INTRAVIA_TEST_FLAG', value)
        assert not serve._flag('INTRAVIA_TEST_FLAG'), value


def test_waitress_kwargs_without_proxy(monkeypatch):
    monkeypatch.delenv('INTRAVIA_TRUSTED_PROXY', raising=False)
    kwargs = serve._waitress_kwargs()
    assert 'trusted_proxy' not in kwargs
    assert kwargs['threads'] >= 1


def test_waitress_kwargs_with_proxy(monkeypatch):
    monkeypatch.setenv('INTRAVIA_TRUSTED_PROXY', '127.0.0.1')
    kwargs = serve._waitress_kwargs()
    assert kwargs['trusted_proxy'] == '127.0.0.1'
    assert kwargs['clear_untrusted_proxy_headers'] is True
