"""Вход под всеми ролями текущей модели данных."""

import pytest

CREDENTIALS = [
    ('admin@aeroflot.ru', 'admin123', 'admin'),
    ('dispatcher@aeroflot.ru', 'disp123', 'dispatcher'),
    ('mcc@aeroflot.ru', 'mcc123', 'mcc'),
    ('pilot1@aeroflot.ru', 'pilot123', 'pilot'),
    ('ivanov@aeroflot.ru', 'eng123', 'engineer'),
    ('cpu@aeroflot.ru', 'cpu123', 'cpu'),
]


@pytest.mark.parametrize('email,password,role', CREDENTIALS)
def test_login_each_role(api, email, password, role):
    c = api.client
    resp = c.post('/api/auth/login', json={'email': email, 'password': password})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['success'] is True
    assert body['user']['role'] == role
    assert body['user']['name']


def test_login_sets_server_side_session(app_module):
    c = app_module.app.test_client()
    assert c.get('/api/auth/session').status_code == 401
    assert c.post('/api/auth/login',
                  json={'email': 'cpu@aeroflot.ru', 'password': 'cpu123'}).status_code == 200
    resp = c.get('/api/auth/session')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['authenticated'] is True
    assert data['user']['role'] == 'cpu'
    assert c.post('/api/auth/logout').status_code == 200
    assert c.get('/api/auth/session').status_code == 401


def test_login_wrong_password(api):
    status, body = api.post('/api/auth/login',
                            {'email': 'mcc@aeroflot.ru', 'password': 'wrong'})
    assert status == 401
    assert 'error' in body


def test_login_empty(api):
    status, _ = api.post('/api/auth/login', {'email': '', 'password': ''})
    assert status == 400


def test_users_directory_lists_roles(api):
    status, rows = api.get('/api/users')
    assert status == 200
    roles = {row['role'] for row in rows}
    assert {'admin', 'dispatcher', 'mcc', 'pilot', 'engineer', 'cpu'} <= roles
