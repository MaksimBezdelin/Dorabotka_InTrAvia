"""Выдача списков ограничена ролью: инженер — свои заявки, пилот — свой ATLB."""

import pytest

CREDENTIALS = {
    'dispatcher': ('dispatcher@aeroflot.ru', 'disp123'),
    'engineer': ('ivanov@aeroflot.ru', 'eng123'),
    'pilot': ('pilot1@aeroflot.ru', 'pilot123'),
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


def _insert(app_module, rid, *, engineer_id='', pilot_name='',
            source='dispatcher', status='assigned'):
    conn = app_module.get_db()
    conn.execute("""INSERT INTO requests
                    (id, aircraft, source, status, engineer_id, pilot_name,
                     chain_stage, description)
                    VALUES (?,?,?,?,?,?,'mcc','тестовая заявка')""",
                 (rid, 'RA-SCOPE', source, status, engineer_id, pilot_name))
    conn.commit()
    conn.close()


def _ids(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]
    return {r['id'] for r in resp.get_json()}


def test_engineer_sees_only_own_requests(app_module, login):
    _insert(app_module, 'req_scope_eng1', engineer_id='eng_1')
    _insert(app_module, 'req_scope_eng2', engineer_id='eng_2')

    ids = _ids(login('engineer'), '/api/requests')
    assert 'req_scope_eng1' in ids
    assert 'req_scope_eng2' not in ids


def test_dispatcher_sees_all_requests(app_module, login):
    _insert(app_module, 'req_scope_eng3', engineer_id='eng_3')
    _insert(app_module, 'req_scope_eng4', engineer_id='eng_4')

    ids = _ids(login('dispatcher'), '/api/requests')
    assert {'req_scope_eng3', 'req_scope_eng4'} <= ids


def test_pilot_atlb_shows_only_own_journal(app_module, login):
    me = 'Иванов И.И.'       # pilot1@aeroflot.ru
    other = 'Петров П.П.'
    _insert(app_module, 'req_scope_mine', pilot_name=me, source='pilot', status='new')
    _insert(app_module, 'req_scope_other', pilot_name=other, source='pilot', status='new')

    client = login('pilot')
    assert 'req_scope_mine' in _ids(client, '/api/pilot/atlb')
    assert 'req_scope_other' not in _ids(client, '/api/pilot/atlb')
    # подстановка чужого имени в ?pilot= ничего не открывает
    resp = client.get('/api/pilot/atlb', query_string={'pilot': other})
    assert 'req_scope_other' not in {r['id'] for r in resp.get_json()}
