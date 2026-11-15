"""Вход под всеми ролями, включая новые звенья цепочки."""

import pytest

CREDENTIALS = [
    ('admin@aeroflot.ru', 'admin123', 'admin'),
    ('dispatcher@aeroflot.ru', 'disp123', 'dispatcher'),
    ('mcc@aeroflot.ru', 'mcc123', 'mcc'),
    ('pilot1@aeroflot.ru', 'pilot123', 'pilot'),
    ('ivanov@aeroflot.ru', 'eng123', 'engineer'),
    ('cpu@aeroflot.ru', 'cpu123', 'cpu'),
    ('shift@aeroflot.ru', 'shift123', 'shift_supervisor'),
    ('foreman@aeroflot.ru', 'foreman123', 'foreman'),
]


@pytest.mark.parametrize('email,password,role', CREDENTIALS)
def test_login_each_role(api, email, password, role):
    status, body = api.post('/api/auth/login', {'email': email, 'password': password})
    assert status == 200, body
    assert body['success'] is True
    assert body['user']['role'] == role
    assert body['user']['name']


def test_login_wrong_password(api):
    status, body = api.post('/api/auth/login',
                            {'email': 'mcc@aeroflot.ru', 'password': 'wrong'})
    assert status == 401
    assert 'error' in body


def test_login_empty(api):
    status, _ = api.post('/api/auth/login', {'email': '', 'password': ''})
    assert status == 400


def test_staff_directory_has_all_chain_roles(api):
    for role, minimum in (('cpu', 1), ('shift_supervisor', 1), ('foreman', 1)):
        status, rows = api.get('/api/staff?role=' + role)
        assert status == 200
        assert len(rows) >= minimum, role
        assert all(r['role'] == role for r in rows)
