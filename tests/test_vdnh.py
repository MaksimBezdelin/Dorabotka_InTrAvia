"""Полигон ВДНХ: графы, маршруты, транспорт, геозоны, ETA и журнал."""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


def connect(app_module):
    conn = sqlite3.connect(app_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def iso_minutes_ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')


def centroid(app_module, role=None, parking=None):
    location = app_module.load_location_config()
    if parking:
        for zone in location['parkings']:
            if zone['id'] == parking:
                return zone['centroid']['lat'], zone['centroid']['lon']
    for obj in location['objects']:
        if obj['role'] == role:
            return obj['centroid']['lat'], obj['centroid']['lon']
    raise AssertionError('объект не найден: %s / %s' % (role, parking))


def insert_request(app_module, rid, engineer='eng_1', parking='SZ-3',
                   minutes_ago=1, eta=4.0, status='assigned', step='accepted'):
    route = {'flat': 55.835388, 'flng': 37.632507, 'tt': eta, 'eta_min': eta,
             'uc': False, 'pid': parking, 'path': []}
    conn = connect(app_module)
    conn.execute("""INSERT INTO requests
        (id, aircraft, fault_type, parking_id, status, routing_stage,
         engineer_id, engineer_name, inspection_assigned_at, engineer_comment, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                 (rid, 'RA-TEST', 'engine', parking, status, 'technician',
                  engineer, 'Тестовый инженер', iso_minutes_ago(minutes_ago),
                  'ROUTE:' + json.dumps(route, ensure_ascii=False)))
    conn.execute("UPDATE engineers SET status='busy', task_step=?, task='RA-TEST' WHERE id=?",
                 (step, engineer))
    conn.commit()
    conn.close()


def cleanup(app_module, rid, engineer='eng_1'):
    conn = connect(app_module)
    conn.execute("DELETE FROM requests WHERE id=?", (rid,))
    conn.execute("UPDATE engineers SET status='free', task_step='idle', task=NULL WHERE id=?",
                 (engineer,))
    conn.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    conn.commit()
    conn.close()


def read_journal():
    path = os.environ['INTRAVIA_EVENTS']
    if not os.path.isfile(path):
        return []
    events = []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def test_track_single_batch_unknown_and_bad_coordinates(anon_client, app_module):
    conn = connect(app_module)
    saved = dict(conn.execute(
        "SELECT coords_lat, coords_lng FROM engineers WHERE id='eng_6'").fetchone())
    conn.close()

    resp = anon_client.post('/api/v1/track',
                            json={'obj': 'eng_6', 'lat': 55.834, 'lon': 37.633})
    assert resp.status_code == 200
    assert resp.get_json()['applied'] is True

    resp = anon_client.post('/api/v1/track', json={'points': [
        {'obj': 'eng_6', 'lat': 55.8341, 'lon': 37.6331},
        {'obj': 'unknown-agent', 'lat': 55.8342, 'lon': 37.6332},
    ]})
    assert resp.status_code == 200
    rows = resp.get_json()
    assert rows[0]['applied'] is True
    assert rows[1]['accepted'] is True and rows[1]['applied'] is False

    resp = anon_client.post('/api/v1/track', json={'obj': 'eng_6', 'lat': 'x', 'lon': 'y'})
    assert resp.status_code == 400
    assert 'lat/lon' in resp.get_json()['error']
    assert any(e['type'] == 'ingest_error' for e in read_journal())

    conn = connect(app_module)
    conn.execute("UPDATE engineers SET coords_lat=?, coords_lng=? WHERE id='eng_6'",
                 (saved['coords_lat'], saved['coords_lng']))
    conn.commit()
    conn.close()


def test_graphs_do_not_mix_road_types(app_module):
    def types(graph):
        return {data['road_type'] for _, _, data in graph.edges(data=True)}

    pedestrian = types(app_module.GRAPH_PEDESTRIAN)
    vehicle = types(app_module.GRAPH_VEHICLE)
    aircraft = types(app_module.GRAPH_AIRCRAFT)
    assert pedestrian <= {'service', 'pedestrian'}
    assert vehicle <= {'service'}
    assert aircraft <= {'taxi', 'runway'}
    assert 'runway' not in pedestrian and 'taxi' not in pedestrian
    assert 'service' not in aircraft and 'pedestrian' not in aircraft

    speeds = {data['speed_kmh'] for _, _, data in app_module.GRAPH_VEHICLE.edges(data=True)}
    assert speeds <= {5, 8, 10, 20}
    assert app_module.road_graph_vehicle is app_module.GRAPH_VEHICLE


def test_route_with_waypoints_and_off_graph_point(app_module):
    terminal = centroid(app_module, role='terminal')
    warehouse = centroid(app_module, role='warehouse1')
    park = centroid(app_module, role='transport_park')
    target = centroid(app_module, parking='SZ-3')

    route = app_module.build_route(
        'engineer', terminal, [warehouse, park], target)
    assert route['agent_type'] == 'engineer'
    assert len(route['legs']) >= 3
    assert route['distance_m'] > 0
    assert route['eta_min'] > 0
    assert len(route['path']) >= 2
    assert route['legs'][0]['mode'] == 'walk'

    # Точка вне дорожного графа привязывается к ближайшему ребру.
    far_route = app_module.build_route(
        'engineer', (55.8300, 37.6250), [], (55.8380, 37.6350))
    assert far_route['distance_m'] > 0
    assert len(far_route['path']) >= 2


def test_transport_choice_and_single_car_reservation(client, app_module):
    engineer = {'coords_lat': 55.835388, 'coords_lng': 37.632507}
    target = centroid(app_module, parking='SZ-9')
    choice = app_module.choose_transport(engineer, target)
    assert choice['mode'] in ('walk', 'transport')
    if choice['mode'] == 'transport':
        assert choice['car'] is not None
        assert choice['route']['time_s'] > 0

    conn = connect(app_module)
    car = conn.execute(
        "SELECT id FROM transport WHERE assigned_to IS NULL ORDER BY id LIMIT 1").fetchone()
    conn.close()
    assert car is not None

    rid1, rid2 = 'req_test_car_1', 'req_test_car_2'
    insert_request(app_module, rid1, engineer='eng_2', parking='SZ-3')
    insert_request(app_module, rid2, engineer='eng_3', parking='SZ-4')
    try:
        first = client.post('/api/transport/%d/assign' % car['id'],
                            json={'request_id': rid1})
        assert first.status_code == 200
        second = client.post('/api/transport/%d/assign' % car['id'],
                             json={'request_id': rid2})
        assert second.status_code == 409
        released = client.post('/api/transport/%d/release' % car['id'],
                               json={'request_id': rid1})
        assert released.status_code == 200
        conn = connect(app_module)
        row = conn.execute("SELECT assigned_to FROM transport WHERE id=?",
                           (car['id'],)).fetchone()
        conn.close()
        assert row['assigned_to'] is None
    finally:
        cleanup(app_module, rid1, engineer='eng_2')
        cleanup(app_module, rid2, engineer='eng_3')


def test_geozone_arrival_only_inside_polygon(app_module):
    rid = 'req_test_zone'
    insert_request(app_module, rid, engineer='eng_4', parking='SZ-3')
    try:
        outside = (55.8000, 37.6000)
        app_module._track_apply_position('engineer', 'eng_4', outside[0], outside[1],
                                         app_module._utc_now_iso())
        conn = connect(app_module)
        step = conn.execute("SELECT task_step FROM engineers WHERE id='eng_4'").fetchone()[0]
        conn.close()
        assert step == 'accepted'

        inside = centroid(app_module, parking='SZ-3')
        assert app_module._track_apply_position('engineer', 'eng_4', inside[0], inside[1],
                                                app_module._utc_now_iso()) is True
        conn = connect(app_module)
        step = conn.execute("SELECT task_step FROM engineers WHERE id='eng_4'").fetchone()[0]
        conn.close()
        assert step == 'arrived'
        assert any(e['type'] == 'arrival' and e.get('request_id') == rid
                   for e in read_journal())
    finally:
        cleanup(app_module, rid, engineer='eng_4')


def test_warning_and_breach_events(app_module):
    warn_rid, breach_rid = 'req_test_warn', 'req_test_breach'
    insert_request(app_module, warn_rid, engineer='eng_5', minutes_ago=11, eta=4.0)
    insert_request(app_module, breach_rid, engineer='eng_6', minutes_ago=16, eta=4.0)
    try:
        app_module._check_regulation_warning(warn_rid)
        app_module._check_regulation_warning(breach_rid)
        events = read_journal()
        warning = [e for e in events if e['type'] == 'warning'
                   and e.get('request_id') == warn_rid]
        breach = [e for e in events if e['type'] == 'breach'
                  and e.get('request_id') == breach_rid]
        assert warning and 0 < warning[-1]['remaining_min'] <= 5
        assert breach and breach[-1]['breach'] is True
        assert breach[-1]['remaining_min'] <= 0

        conn = connect(app_module)
        history = json.loads(conn.execute(
            "SELECT routing_history FROM requests WHERE id=?", (breach_rid,)
        ).fetchone()[0] or '[]')
        conn.close()
        assert any(item.get('stage') == 'breach' for item in history)
    finally:
        cleanup(app_module, warn_rid, engineer='eng_5')
        cleanup(app_module, breach_rid, engineer='eng_6')


def test_events_api_journal_and_xlsx(client):
    events_resp = client.get('/api/v1/events?limit=10')
    body = events_resp.get_json()
    assert events_resp.status_code == 200
    assert isinstance(body['events'], list)
    assert body['count'] == len(body['events'])

    export = client.get('/api/v1/events/export.xlsx')
    assert export.status_code == 200
    assert export.data[:2] == b'PK'

    report = client.get('/api/reports/export.xlsx')
    assert report.status_code == 200
    assert report.data[:2] == b'PK'

    from io import BytesIO
    from openpyxl import load_workbook
    events_wb = load_workbook(BytesIO(export.data))
    assert 'Журнал' in events_wb.sheetnames
    header = [cell.value for cell in events_wb['Журнал'][1]]
    assert header[:3] == ['Время (ISO, мс)', 'Тип', 'Заявка']

    report_wb = load_workbook(BytesIO(report.data))
    assert 'Журнал' in report_wb.sheetnames
