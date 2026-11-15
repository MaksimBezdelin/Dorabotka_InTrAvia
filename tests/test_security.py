"""API и страницы дашбордов закрыты серверной сессией."""

import pytest

PROTECTED_API = [
    ('GET', '/api/chain'),
    ('GET', '/api/export/xlsx'),
    ('GET', '/api/engineers'),
    ('GET', '/api/staff?role=foreman'),
    ('POST', '/api/chain/req_x/assign_technician'),
    ('POST', '/api/engineer/verdict'),
    ('POST', '/api/mcc/finalize'),
]

PROTECTED_PAGES = [
    '/mcc/dashboard.html',
    '/cpu/dashboard.html',
    '/shift/dashboard.html',
    '/foreman/dashboard.html',
    '/pilot/dashboard.html',
    '/dispatcher/dashboard.html',
    '/engineer/dashboard.html',
    '/admin/dashboard.html',
]


@pytest.mark.parametrize('method,path', PROTECTED_API)
def test_api_requires_session(anon_client, method, path):
    resp = anon_client.open(path, method=method, json={})
    assert resp.status_code == 401
    assert resp.get_json()['error'] == 'Требуется авторизация'


@pytest.mark.parametrize('path', PROTECTED_PAGES)
def test_dashboard_redirects_anonymous(anon_client, path):
    resp = anon_client.get(path)
    assert resp.status_code == 302
    assert '/login.html' in resp.headers['Location']


def test_login_page_is_public(anon_client):
    assert anon_client.get('/login.html').status_code == 200


def test_session_after_login_and_logout(app_module):
    c = app_module.app.test_client()
    assert c.get('/api/chain').status_code == 401

    assert c.post('/api/auth/login',
                  json={'email': 'mcc@aeroflot.ru', 'password': 'mcc123'}).status_code == 200
    assert c.get('/api/chain').status_code == 200
    assert c.get('/mcc/dashboard.html').status_code == 200

    assert c.post('/api/auth/logout').status_code == 200
    assert c.get('/api/chain').status_code == 401


def test_security_headers_present(anon_client):
    resp = anon_client.get('/login.html')
    assert resp.headers['X-Content-Type-Options'] == 'nosniff'
    assert resp.headers['X-Frame-Options'] == 'DENY'
    assert resp.headers['Referrer-Policy'] == 'same-origin'


def test_session_cookie_flags(app_module):
    c = app_module.app.test_client()
    resp = c.post('/api/auth/login',
                  json={'email': 'admin@aeroflot.ru', 'password': 'admin123'})
    cookie = resp.headers.get('Set-Cookie', '')
    assert 'HttpOnly' in cookie
    assert 'SameSite=Lax' in cookie or 'SameSite=lax' in cookie


def test_tampered_cookie_rejected(anon_client):
    # подсунутая сессия без валидной подписи не даёт доступа
    anon_client.set_cookie('session',
                           'eyJ1c2VyIjp7InJvbGUiOiJhZG1pbiJ9fQ.forged.signature',
                           domain='localhost')
    assert anon_client.get('/api/chain').status_code == 401
