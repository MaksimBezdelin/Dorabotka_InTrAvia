"""Разграничение ролей: API и дашборды доступны только своим ролям."""

import pytest

CREDENTIALS = {
    'admin': ('admin@aeroflot.ru', 'admin123'),
    'dispatcher': ('dispatcher@aeroflot.ru', 'disp123'),
    'mcc': ('mcc@aeroflot.ru', 'mcc123'),
    'pilot': ('pilot1@aeroflot.ru', 'pilot123'),
    'engineer': ('ivanov@aeroflot.ru', 'eng123'),
    'cpu': ('cpu@aeroflot.ru', 'cpu123'),
    'shift_supervisor': ('shift@aeroflot.ru', 'shift123'),
    'foreman': ('foreman@aeroflot.ru', 'foreman123'),
}

DASHBOARDS = {
    'admin': '/admin/dashboard.html',
    'dispatcher': '/dispatcher/dashboard.html',
    'mcc': '/mcc/dashboard.html',
    'pilot': '/pilot/dashboard.html',
    'engineer': '/engineer/dashboard.html',
    'cpu': '/cpu/dashboard.html',
    'shift_supervisor': '/shift/dashboard.html',
    'foreman': '/foreman/dashboard.html',
}


@pytest.fixture()
def login(app_module):
    def _login(role):
        email, password = CREDENTIALS[role]
        c = app_module.app.test_client()
        resp = c.post('/api/auth/login',
                      json={'email': email, 'password': password})
        assert resp.status_code == 200, (role, resp.get_json())
        return c
    return _login


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/mcc/finalize'),
    ('POST', '/api/chain/req_x/assign_technician'),
    ('POST', '/api/engineer/verdict'),
    ('GET', '/api/users'),
    ('GET', '/api/chain'),
    ('GET', '/api/staff'),
])
def test_pilot_cannot_use_other_roles(login, method, path):
    resp = login('pilot').open(path, method=method, json={})
    assert resp.status_code == 403, (path, resp.get_data(as_text=True)[:200])


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/mcc/finalize'),
    ('POST', '/api/chain/req_x/assign_technician'),
    ('POST', '/api/chain/req_x/assign_shift'),
    ('POST', '/api/engineer/verdict'),
    ('GET', '/api/chain'),
    ('GET', '/api/staff'),
    ('GET', '/api/users'),
])
def test_dispatcher_cannot_use_other_roles(login, method, path):
    resp = login('dispatcher').open(path, method=method, json={})
    assert resp.status_code == 403, (path, resp.get_data(as_text=True)[:200])


@pytest.mark.parametrize('method,path', [
    ('GET', '/api/users'),
    ('GET', '/api/transport'),
    ('POST', '/api/mcc/finalize'),
    ('POST', '/api/pilot/atlb/sign_crs'),
])
def test_engineer_cannot_use_other_roles(login, method, path):
    resp = login('engineer').open(path, method=method, json={})
    assert resp.status_code == 403, (path, resp.get_data(as_text=True)[:200])


@pytest.mark.parametrize('method,path', [
    ('GET', '/api/users'),
    ('GET', '/api/reports'),
    ('POST', '/api/engineer/verdict'),
])
def test_mcc_cannot_use_other_roles(login, method, path):
    resp = login('mcc').open(path, method=method, json={})
    assert resp.status_code == 403, (path, resp.get_data(as_text=True)[:200])


def test_chain_stage_roles_split(login):
    # каждый шаг цепочки — только своя роль (MCC может координировать всю цепочку)
    assert login('cpu').post('/api/chain/req_x/assign_shift', json={}).status_code != 403
    assert login('cpu').post('/api/chain/req_x/assign_foreman', json={}).status_code == 403
    assert login('shift_supervisor').post('/api/chain/req_x/assign_foreman', json={}).status_code != 403
    assert login('shift_supervisor').post('/api/chain/req_x/assign_technician', json={}).status_code == 403
    assert login('foreman').post('/api/chain/req_x/assign_technician', json={}).status_code != 403
    assert login('foreman').post('/api/chain/req_x/assign_shift', json={}).status_code == 403


def test_allowed_calls_pass_role_check(login):
    assert login('dispatcher').get('/api/requests').status_code == 200
    assert login('dispatcher').get('/api/engineers').status_code == 200
    assert login('dispatcher').get('/api/transport').status_code == 200
    assert login('engineer').get('/api/engineers').status_code == 200
    assert login('pilot').get('/api/pilot/atlb').status_code == 200
    assert login('pilot').get('/api/requests').status_code == 200
    assert login('mcc').get('/api/chain').status_code == 200
    assert login('mcc').get('/api/staff').status_code == 200
    assert login('cpu').get('/api/chain').status_code == 200


def test_pilot_updates_only_own_profile(login):
    c = login('pilot')
    assert c.put('/api/users/pilot_2', json={'name': 'Чужой'}).status_code == 403
    resp = c.put('/api/users/pilot_1', json={'name': 'Иванов И.И.'})
    assert resp.status_code == 200


def test_engineer_updates_only_own_card(login):
    c = login('engineer')
    assert c.put('/api/engineers/eng_2', json={'name': 'Другой'}).status_code == 403
    me = next(e for e in c.get('/api/engineers').get_json() if e['id'] == 'eng_1')
    resp = c.put('/api/engineers/eng_1', json={
        'name': me['name'], 'specialization': me['specialization'],
        'status': me['status'], 'phone': me['phone'],
    })
    assert resp.status_code == 200


@pytest.mark.parametrize('role', list(DASHBOARDS))
def test_own_dashboard_opens(login, role):
    assert login(role).get(DASHBOARDS[role]).status_code == 200


@pytest.mark.parametrize('role', [r for r in DASHBOARDS if r != 'admin'])
def test_foreign_dashboard_redirects_home(login, role):
    foreign = [p for r, p in DASHBOARDS.items() if r != role and r != 'admin'][0]
    resp = login(role).get(foreign)
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith(DASHBOARDS[role])


@pytest.mark.parametrize('role', list(DASHBOARDS))
def test_admin_opens_every_dashboard(login, role):
    assert login('admin').get(DASHBOARDS[role]).status_code == 200
