# -*- coding: utf-8 -*-
"""Дымовой прогон полигона ВДНХ на копии database.db.

Создаёт три заявки, принимает их инженерами, двигает инженеров в геозоны
и проверяет журнал событий и выгрузки XLSX. Боевая база не изменяется.

Запуск:  .venv/Scripts/python.exe tools/smoke_vdnh.py
"""
import json
import os
import shutil
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

tmp_dir = tempfile.mkdtemp(prefix='intravia_smoke_')
db_path = os.path.join(tmp_dir, 'database.db')
source_db = os.environ.get('INTRAVIA_DB', '').strip() or os.path.join(BASE_DIR, 'database.db')
if not os.path.isfile(source_db):
    raise SystemExit('База не найдена: ' + source_db)
shutil.copyfile(source_db, db_path)
os.environ['INTRAVIA_DB'] = db_path
os.environ.setdefault('INTRAVIA_EVENTS', os.path.join(tmp_dir, 'events.jsonl'))
os.environ.setdefault('AUTH_REQUIRED', '1')

import app as A  # noqa: E402

client = A.app.test_client()


def login():
    response = client.post('/api/auth/login',
                           json={'email': 'admin@aeroflot.ru', 'password': 'admin123'})
    if response.status_code != 200:
        raise SystemExit('Не удалось войти под admin: %s' % response.get_json())
    return response.get_json()['user']


def create_and_accept(zone, fault):
    response = client.post('/api/dispatch', json={
        'source': 'dispatcher', 'aircraft': 'RA-SMOKE', 'flight_number': 'SM ' + zone,
        'fault_type': fault, 'severity': 'medium', 'parking_id': zone,
        'description': 'Дымовой прогон ' + zone,
    })
    body = response.get_json()
    if response.status_code != 200 or not body.get('success'):
        raise SystemExit('Заявка %s не создана: %s' % (zone, body))
    if body.get('queued'):
        print('  %s: очередь (нет свободного инженера)' % zone)
        return None
    rid = body['request_id']
    engineer_id = body['selected']['engineer']['id']
    accepted = client.post('/api/requests/%s/accept' % rid,
                           json={'engineer_id': engineer_id})
    if accepted.status_code != 200:
        raise SystemExit('Заявка %s не принята: %s' % (rid, accepted.get_json()))
    print('  %s: инженер %s, ETA %s мин' % (
        rid, body['selected']['engineer']['device_id'], body['selected']['travel_time']))
    return {'request_id': rid, 'engineer_id': engineer_id, 'zone': zone}


def move_to_zone(task):
    location = A.load_location_config()
    zone = next(z for z in location['parkings'] if z['id'] == task['zone'])
    lat, lng = zone['centroid']['lat'], zone['centroid']['lon']
    response = client.post('/api/v1/track',
                           json={'obj': task['engineer_id'], 'lat': lat, 'lon': lng})
    if response.status_code != 200:
        raise SystemExit('Точка не принята: %s' % response.get_json())
    return response.get_json()


def main():
    user = login()
    print('Вход:', user['role'], user['email'])
    tasks = []
    for zone, fault in (('SZ-3', 'engine_failure'), ('SZ-5', 'avionics_nav'),
                        ('SZ-8', 'hydraulic')):
        task = create_and_accept(zone, fault)
        if task:
            tasks.append(task)
    if not tasks:
        raise SystemExit('Ни одна заявка не назначена')

    print('Движение инженеров в геозоны:')
    for task in tasks:
        result = move_to_zone(task)
        print('  %s -> %s: applied=%s' % (task['engineer_id'], task['zone'],
                                          result.get('applied')))

    events_resp = client.get('/api/v1/events?limit=1000')
    events = events_resp.get_json()['events']
    types = [event['type'] for event in events]
    arrivals = [event for event in events if event['type'] == 'arrival']
    print('События:', ', '.join(sorted(set(types))))
    if not arrivals:
        raise SystemExit('В журнале нет события arrival')

    for path in ('/api/v1/events/export.xlsx', '/api/reports/export.xlsx'):
        export = client.get(path)
        if export.status_code != 200 or export.data[:2] != b'PK':
            raise SystemExit('Выгрузка %s не удалась: %s' % (path, export.status_code))
        print('Выгрузка OK:', path, len(export.data), 'байт')

    with open(os.environ['INTRAVIA_EVENTS'], encoding='utf-8') as fh:
        for line in list(fh)[-3:]:
            event = json.loads(line)
            print('  ', event.get('at'), event.get('type'), event.get('request_id'))

    print('SMOKE OK: заявок', len(tasks), ', прибытий', len(arrivals))


if __name__ == '__main__':
    main()
