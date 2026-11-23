"""Серверная авторизация: сессия, роли, заголовки и публичные маршруты.

Проверки включают AUTH_REQUIRED=1 через фикстуру auth_on: в демо-режиме
флаг выключен, но защитный слой должен работать при включении.
"""

import pytest

PROTECTED_API = [
    ('GET', '/api/users'),
    ('GET', '/api/requests'),
    ('GET', '/api/engineers'),
    ('GET', '/api/transport'),
    ('POST', '/api/mcc/finalize'),
    ('POST', '/api/dispatcher/assign'),
    ('POST', '/api/engineer/verdict'),
]

PROTECTED_PAGES = [
    '/mcc/dashboard.html',
    '/cpu/dashboard.html',
    '/pilot/dashboard.html',
    '/dispatcher/dashboard.html',
    '/engineer/dashboard.html',
    '/admin/dashboard.html',
]


@pytest.fixture()
def auth_on(app_module):
    old = app_module.AUTH_REQUIRED
    app_module.AUTH_REQUIRED = True
    yield app_module
    app_module.AUTH_REQUIRED = old


@pytest.fixture()
def login(app_module, auth_on):
    def _login(email, password):
        c = app_module.app.test_client()
        resp = c.post('/api/auth/login', json={'email': email, 'password': password})
        assert resp.status_code == 200, resp.get_json()
        return c
    return _login


@pytest.mark.parametrize('method,path', PROTECTED_API)
def test_api_requires_session(anon_client, auth_on, method, path):
    resp = anon_client.open(path, method=method, json={})
    assert resp.status_code == 401
    assert resp.get_json()['error'] == 'Требуется авторизация'


@pytest.mark.parametrize('path', PROTECTED_PAGES)
def test_dashboard_redirects_anonymous(anon_client, auth_on, path):
    resp = anon_client.get(path)
    assert resp.status_code == 302
    assert '/login.html' in resp.headers['Location']


def test_public_routes_without_session(anon_client, auth_on):
    assert anon_client.get('/login.html').status_code == 200
    assert anon_client.get('/m/engineer').status_code == 200
    assert anon_client.get('/m/agents').status_code == 200
    assert anon_client.post('/api/v1/track', json={
        'obj': 'eng_1', 'lat': 55.97, 'lon': 37.40}).status_code in (200, 400)


def test_session_after_login_and_logout(app_module, auth_on):
    c = app_module.app.test_client()
    assert c.get('/api/requests').status_code == 401

    assert c.post('/api/auth/login',
                  json={'email': 'mcc@aeroflot.ru', 'password': 'mcc123'}).status_code == 200
    assert c.get('/api/requests').status_code == 200
    assert c.get('/mcc/dashboard.html').status_code == 200

    assert c.post('/api/auth/logout').status_code == 200
    assert c.get('/api/requests').status_code == 401


def test_foreign_dashboard_redirects_home(login):
    resp = login('pilot1@aeroflot.ru', 'pilot123').get('/mcc/dashboard.html')
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/pilot/dashboard.html')


@pytest.mark.parametrize('email,password,path', [
    ('pilot1@aeroflot.ru', 'pilot123', '/api/users'),
    ('dispatcher@aeroflot.ru', 'disp123', '/api/mcc/finalize'),
    ('mcc@aeroflot.ru', 'mcc123', '/api/users'),
])
def test_role_forbidden(login, email, password, path):
    resp = login(email, password).get(path, follow_redirects=False)
    if resp.status_code == 405:
        resp = login(email, password).post(path, json={})
    assert resp.status_code == 403


def test_security_headers_present(anon_client):
    resp = anon_client.get('/login.html')
    assert resp.headers['X-Content-Type-Options'] == 'nosniff'
    assert resp.headers['X-Frame-Options'] == 'SAMEORIGIN'
    assert resp.headers['Referrer-Policy'] == 'strict-origin-when-cross-origin'


def test_session_cookie_flags(app_module):
    c = app_module.app.test_client()
    resp = c.post('/api/auth/login',
                  json={'email': 'admin@aeroflot.ru', 'password': 'admin123'})
    cookie = resp.headers.get('Set-Cookie', '')
    assert 'HttpOnly' in cookie
    assert 'SameSite=Lax' in cookie or 'SameSite=lax' in cookie


def test_tampered_cookie_rejected(anon_client, auth_on):
    anon_client.set_cookie('session',
                           'eyJ1c2VyIjp7InJvbGUiOiJhZG1pbiJ9fQ.forged.signature',
                           domain='localhost')
    assert anon_client.get('/api/requests').status_code == 401


def test_login_rate_limited(anon_client):
    email = 'bruteforce@aeroflot.ru'
    for _ in range(10):
        assert anon_client.post('/api/auth/login',
                                json={'email': email, 'password': 'nope'}).status_code == 401
    resp = anon_client.post('/api/auth/login',
                            json={'email': email, 'password': 'nope'})
    assert resp.status_code == 429
