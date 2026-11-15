"""Общая настройка тестов: отдельная временная база, тестовый клиент Flask."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(scope='session')
def app_module(tmp_path_factory):
    db_path = tmp_path_factory.mktemp('db') / 'test.db'
    os.environ['INTRAVIA_DB'] = str(db_path)

    import app as module
    module.app.config['TESTING'] = True
    module.init_db()
    return module


@pytest.fixture()
def anon_client(app_module):
    """Клиент без авторизации."""
    return app_module.app.test_client()


@pytest.fixture()
def client(app_module):
    """Клиент, авторизованный как администратор (API закрыт сессией)."""
    c = app_module.app.test_client()
    resp = c.post('/api/auth/login',
                  json={'email': 'admin@aeroflot.ru', 'password': 'admin123'})
    assert resp.status_code == 200, 'не удалось войти под admin'
    return c


@pytest.fixture()
def api(app_module, client):
    """Хелпер: POST/GET JSON через авторизованный тестовый клиент."""

    class Api:
        def __init__(self, client):
            self.client = client

        def post(self, path, payload=None):
            resp = self.client.post(path, json=payload or {})
            return resp.status_code, resp.get_json()

        def get(self, path):
            resp = self.client.get(path)
            return resp.status_code, resp.get_json()

    return Api(client)


@pytest.fixture()
def make_pilot_request(api):
    """Создаёт заявку пилота и возвращает её id."""
    counter = {'n': 0}

    def _make(aircraft='RA-TEST1', parking='101', fault='hydraulic_leak',
              description='Тестовая течь гидравлики'):
        counter['n'] += 1
        status, body = api.post('/api/dispatch', {
            'source': 'pilot',
            'pilot_name': 'Тестовый КВС',
            'pilot_rank': 'КВС',
            'aircraft': aircraft,
            'parking_id': parking,
            'fault_type': fault,
            'description': description,
        })
        assert status == 200, body
        return body['request_id']

    return _make


def chain_entry(api, rid):
    status, rows = api.get('/api/chain')
    assert status == 200
    return next((r for r in rows if r['id'] == rid), None)
