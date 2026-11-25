"""Финальный smoke-тест InTrAvia.

Запускается против уже работающего сервера. Создаёт одну тестовую заявку
и удаляет её в конце; журнал событий при этом сохраняет факт проверки.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def request(base, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode('utf-8')
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8')
        return exc.code, json.loads(body) if body else {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:5000')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    failures = []

    def check(name, condition, extra=''):
        print(('PASS ' if condition else 'FAIL ') + name + ((' :: ' + str(extra)) if extra else ''))
        if not condition:
            failures.append(name)

    status, health = request(base, 'GET', '/api/health')
    check('health', status == 200 and health.get('success'), health)

    status, refs = request(base, 'GET', '/api/defects/ata')
    check('defect_ata_reference', status == 200 and len(refs.get('ata_chapters') or {}) > 20)

    status, classified = request(base, 'POST', '/api/defects/classify',
                                 {'text': 'Заклинило клапан ПОС'})
    check('open_defect_classifier', status == 200 and classified.get('ata') == '30', classified)

    status, track = request(base, 'POST', '/api/v1/track',
                            {'obj': 'e-999999', 'lat': 55.83, 'lon': 37.63,
                             't': '2026-12-03T09:14:20.123Z'})
    check('track_unknown_object_200', status == 200 and track.get('accepted') is True and
          track.get('applied') is False, track)

    status, invalid = request(base, 'POST', '/api/v1/track',
                              {'obj': 'e-01', 'lat': 'oops', 'lon': 37.63})
    check('track_invalid_400', status == 400, invalid)

    status, airport = request(base, 'GET', '/api/system/current-airport')
    zones = request(base, 'GET', '/api/parking')[1]
    if airport.get('airport') == 'vdnh' and zones:
        start = zones[0]
        end = zones[-1]
        status, route = request(base, 'POST', '/api/route', {
            'start_lat': start['lat'], 'start_lng': start['lng'],
            'end_lat': end['lat'], 'end_lng': end['lng'],
            'waypoints': [{'lat': (start['lat'] + end['lat']) / 2,
                           'lng': (start['lng'] + end['lng']) / 2, 'type': 'tool'}],
        })
        check('route_with_waypoint', status == 200 and route.get('waypoints') == 1 and
              len(route.get('path') or []) >= 2 and route.get('total_time_min', 0) > 0, route)

    status, created = request(base, 'POST', '/api/dispatch', {
        'aircraft': 'SMOKE-FINAL', 'parking_id': (zones[0]['ref'] if zones else '1'),
        'description': 'Заклинило клапан ПОС', 'severity': 'high', 'auto_assign': False,
    })
    rid = created.get('request_id')
    check('create_request', status == 200 and created.get('success') and rid, created)

    if rid:
        status, priorities = request(base, 'GET', '/api/dispatcher/priorities')
        item = next((x for x in priorities.get('requests', []) if x.get('request_id') == rid), None)
        ranking = (item or {}).get('ranking') or {}
        check('explainable_candidates', status == 200 and item is not None and
              ranking.get('duration_ms') is not None, ranking.get('no_candidate_reason'))
        candidates = ranking.get('candidates') or []
        check('rejected_candidates_visible', 'rejected' in ranking)
        if candidates:
            engineer_id = candidates[0]['engineer_id']
            status, assigned = request(base, 'POST', '/api/dispatcher/assign', {
                'request_id': rid, 'engineer_id': engineer_id, 'comment': 'Smoke-тест',
            })
            check('assign', status == 200 and assigned.get('success'), assigned)
            status, route = request(base, 'GET', '/api/dispatcher/requests/%s/route' % rid)
            route_data = (route or {}).get('route') or {}
            check('route_after_assign', status == 200 and len(route_data.get('path') or []) >= 2,
                  {k: route_data.get(k) for k in ('tt', 'uc', 'mode')})
            status, norms = request(base, 'GET', '/api/dispatcher/norms')
            check('norms', status == 200 and norms.get('success'), norms.get('count'))
        status, deleted = request(base, 'DELETE', '/api/requests/' + rid)
        check('cleanup', status == 200 and deleted.get('success'), deleted)

    status, event_log = request(base, 'GET', '/api/event-log?format=json')
    types = {event.get('type') for event in event_log.get('events', [])}
    check('event_log', status == 200 and 'request_created' in types and 'candidates' in types,
          sorted(types)[-12:])
    check('event_log_ms', all('.' in str(e.get('t')) and str(e.get('t')).endswith('Z')
                              for e in event_log.get('events', [])[-10:]))

    print('\nFAILURES:', failures)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
