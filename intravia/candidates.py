"""Подбор и кэш кандидатов-инженеров (вынесено из app.py)."""
import threading
from datetime import datetime, timezone

from intravia.config import (CANDIDATE_CACHE_TTL_SEC, REGULATION_MINUTES,
                             _request_regulation_minutes)
from intravia.db import (get_db, get_engineer_number_from_device,
                         normalize_device_id)
from intravia.events import _utc_now_iso
from intravia.faults import FAULT_SKILL
from intravia.geo import _location_center, _parking_point
from intravia.routing import RouteError, build_route


def _skill_set(specialization):
    """Набор допусков инженера: 'engine,mechanical' -> {'engine','mechanical'}.

    Квалификации из геоданных хранятся строкой через запятую, поэтому
    сравнение с профилем дефекта идёт по набору, а не по точному равенству.
    """
    if specialization is None:
        return set()
    if isinstance(specialization, (list, tuple, set)):
        parts = specialization
    else:
        parts = str(specialization).split(',')
    return {str(part).strip().lower() for part in parts if str(part).strip()}


def _has_skill(specialization, required_skill):
    """Есть ли у инженера допуск required_skill (trim/lower, без fallback)."""
    required = str(required_skill or '').strip().lower()
    if not required:
        return False
    return required in _skill_set(specialization)


def get_free_engineer_for(conn, fault_type, required_skill_map,
                          target_lat=None, target_lng=None, exclude_ids=None,
                          required_skill_override=None,
                          regulation_minutes=None):
    if exclude_ids is None:
        exclude_ids = set()
    exclude_ids = {str(x) for x in exclude_ids}
    required_skill = (required_skill_override
                      or required_skill_map.get(fault_type, 'general'))

    cur = conn.cursor()
    cur.execute("""SELECT id, name, device_id, specialization, coords_lat, coords_lng
                   FROM engineers WHERE status='free'""")
    all_free = [dict(r) for r in cur.fetchall()]

    cur.execute("""SELECT DISTINCT engineer_id FROM requests
                   WHERE routing_stage='technician'
                     AND status IN ('assigned','arriving','in_progress','working')
                     AND engineer_id IS NOT NULL""")
    busy_with_requests = {str(r['engineer_id']) for r in cur.fetchall()}

    all_free = [e for e in all_free
                if str(e['id']) not in exclude_ids
                and str(e['id']) not in busy_with_requests]
    pool = [e for e in all_free if _has_skill(e['specialization'], required_skill)]

    if not pool:
        return None, None
    if target_lat is None or target_lng is None:
        return pool[0], {'travel_time': 0, 'walk_time': 0, 'using_car': False,
                         'best_car': None, 'best_car_time': 0, 'walk_to_car': 0,
                         'path_coords': None}

    cur.execute("SELECT id, device_id, lat, lng, speed FROM transport WHERE assigned_to IS NULL")
    cars = [dict(r) for r in cur.fetchall()]
    cars = [c for c in cars if c.get('lat') is not None and c.get('lng') is not None]

    feasible = []
    for e in pool:
        if e['coords_lat'] is None or e['coords_lng'] is None:
            continue
        eng_point = (float(e['coords_lat']), float(e['coords_lng']))
        target_point = (float(target_lat), float(target_lng))
        try:
            walk_route = build_route('engineer', eng_point, [], target_point)
        except RouteError:
            continue

        best_route = walk_route
        best_car = None
        for car in cars:
            try:
                car_route = build_route(
                    'engineer', eng_point, [], target_point,
                    {'use_transport': True,
                     'car_lat': car['lat'], 'car_lng': car['lng']})
            except RouteError:
                continue
            if car_route['time_s'] < best_route['time_s']:
                best_route, best_car = car_route, car

        travel_min = best_route['time_s'] / 60.0
        limit_minutes = (regulation_minutes if regulation_minutes is not None
                         else REGULATION_MINUTES)
        if travel_min > limit_minutes:
            continue

        walk_legs = [l for l in best_route['legs'] if l['mode'] == 'walk']
        car_legs = [l for l in best_route['legs'] if l['mode'] == 'transport']
        walk_to_car_min = (walk_legs[0]['time_s'] / 60.0) if (best_car and walk_legs) else 0.0
        car_time_min = sum(l['time_s'] for l in car_legs) / 60.0

        feasible.append({
            'engineer': e,
            'travel_time': round(travel_min, 1),
            'walk_time': round(walk_route['time_s'] / 60.0, 1),
            'using_car': best_car is not None, 'best_car': best_car,
            'best_car_time_exact': car_time_min,
            'walk_to_car_exact': walk_to_car_min,
            'best_car_time': round(car_time_min, 1),
            'walk_to_car': round(walk_to_car_min, 1),
            'path_coords': best_route['path'],
        })

    if not feasible:
        return None, None
    feasible.sort(key=lambda x: x['travel_time'])
    return feasible[0]['engineer'], feasible[0]

# Кэш таблиц кандидатов для объяснимости назначения (критерий 17).
_CANDIDATES_LOCK = threading.Lock()
_CANDIDATES_CACHE = {}
CANDIDATE_URGENCY = {'low': 0.85, 'medium': 1.0, 'high': 1.3, 'critical': 1.5}


def _engineer_shift_load(conn, engineer_id):
    """Сколько заявок инженер уже получал за текущие сутки (загрузка смены)."""
    cur = conn.cursor()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    cur.execute("""SELECT COUNT(*) AS cnt FROM requests
                   WHERE engineer_id=?
                     AND status NOT IN ('cancelled','deleted','draft')
                     AND substr(COALESCE(created_at,''),1,10)=?""",
                (engineer_id, today))
    row = cur.fetchone()
    return int(row['cnt'] or 0) if row else 0


def build_candidate_table(conn, fault_type, target_lat, target_lng,
                          severity='medium', exclude_ids=None,
                          regulation_minutes=None):
    """Строит таблицу кандидатов с факторами, оценкой и причинами отказа.

    Учитываются: допуск по профилю дефекта, занятость, время в пути с
    пересадкой на спецтехнику, загрузка за смену и срочность заявки.
    regulation_minutes — пер-заявочный норматив (демо-настройка), по умолчанию 15.
    """
    exclude_ids = {str(x) for x in (exclude_ids or set())}
    limit_minutes = (regulation_minutes if regulation_minutes is not None
                     else REGULATION_MINUTES)
    required_skill = FAULT_SKILL.get(fault_type, 'general')
    cur = conn.cursor()
    cur.execute("""SELECT e.id, e.name, e.device_id, e.specialization, e.status,
                          e.coords_lat, e.coords_lng,
                          a.at_id, a.grade, a.scope, a.skill, a.scope_desc,
                          COALESCE(a.is_live,0) AS is_live
                   FROM engineers e
                   LEFT JOIN engineer_assignment a ON a.engineer_id=e.id
                   ORDER BY e.device_id""")
    engineers = [dict(row) for row in cur.fetchall()]

    cur.execute("""SELECT engineer_id, id FROM requests
                   WHERE routing_stage='technician'
                     AND status IN ('assigned','arriving','in_progress','working')
                     AND engineer_id IS NOT NULL""")
    active = {}
    for row in cur.fetchall():
        active.setdefault(str(row['engineer_id']), row['id'])

    cur.execute("SELECT id, device_id, lat, lng, speed FROM transport WHERE assigned_to IS NULL")
    cars = [dict(row) for row in cur.fetchall()]
    cars = [car for car in cars if car.get('lat') is not None and car.get('lng') is not None]

    urgency = CANDIDATE_URGENCY.get(str(severity or 'medium').lower(), 1.0)
    table = []
    for engineer in engineers:
        eid = str(engineer['id'])
        device = normalize_device_id(engineer.get('device_id'),
                                     get_engineer_number_from_device(engineer.get('device_id')))
        item = {
            'engineer_id': eid,
            'name': engineer.get('name') or device,
            'device_id': device,
            'specialization': engineer.get('specialization') or 'general',
            'at_id': engineer.get('at_id'),
            'grade': engineer.get('grade'),
            'scope': engineer.get('scope'),
            'skill': engineer.get('skill'),
            'scope_desc': engineer.get('scope_desc'),
            'is_live': int(engineer.get('is_live') or 0),
            'eligible': False,
            'reason': '',
            'workload': _engineer_shift_load(conn, eid),
            'eta': None,
        }
        if eid in exclude_ids:
            item['reason'] = 'уже отказался от этой заявки'
            table.append(item)
            continue
        if engineer.get('status') != 'free':
            item['reason'] = 'занят'
            table.append(item)
            continue
        if eid in active:
            item['reason'] = 'работает по заявке ' + str(active[eid])
            table.append(item)
            continue
        if not _has_skill(engineer.get('specialization'), required_skill):
            item['reason'] = 'нет допуска: требуется ' + required_skill
            table.append(item)
            continue
        if engineer.get('coords_lat') is None or engineer.get('coords_lng') is None:
            item['reason'] = 'нет координат'
            table.append(item)
            continue

        eng_point = (float(engineer['coords_lat']), float(engineer['coords_lng']))
        try:
            walk_route = build_route('engineer', eng_point, [], (target_lat, target_lng))
        except RouteError as exc:
            item['reason'] = 'маршрут не строится: ' + str(exc)
            table.append(item)
            continue

        best_route, best_car = walk_route, None
        for car in cars:
            try:
                car_route = build_route(
                    'engineer', eng_point, [], (target_lat, target_lng),
                    {'use_transport': True,
                     'car_lat': car['lat'], 'car_lng': car['lng']})
            except RouteError:
                continue
            if car_route['time_s'] < best_route['time_s']:
                best_route, best_car = car_route, car
        travel_min = best_route['time_s'] / 60.0
        if travel_min > limit_minutes:
            item['reason'] = f'не успевает: {travel_min:.1f} мин > {limit_minutes}'
            table.append(item)
            continue

        workload = item['workload']
        walk_legs = [leg for leg in best_route['legs'] if leg['mode'] == 'walk']
        car_legs = [leg for leg in best_route['legs'] if leg['mode'] == 'transport']
        walk_to_car = (walk_legs[0]['time_s'] / 60.0) if (best_car and walk_legs) else 0.0
        car_time = sum(leg['time_s'] for leg in car_legs) / 60.0
        score = round(travel_min * urgency + workload * 2.0 + (0.5 if best_car else 0.0), 2)
        reasons = [
            'допуск ' + required_skill + ' — да',
            'свободен',
            f'в пути {travel_min:.1f} мин',
            ('на спецтехнике ' + str(best_car.get('device_id')))
            if best_car else 'пешком',
            f'загрузка за смену {workload}',
            f'срочность {severity} ×{urgency}',
            f'укладывается в {limit_minutes} мин',
        ]
        item.update({
            'eligible': True,
            'travel_time': round(travel_min, 1),
            'eta': round(travel_min, 1),
            'walk_time': round(walk_route['time_s'] / 60.0, 1),
            'using_car': best_car is not None,
            'best_car': best_car.get('device_id') if best_car else None,
            'walk_to_car': round(walk_to_car, 1),
            'car_time': round(car_time, 1),
            'workload': workload,
            'score': score,
            'reason': ', '.join(reasons),
            'path': best_route.get('path'),
        })
        table.append(item)

    table.sort(key=lambda row: (not row['eligible'], row.get('score', 9999.0)))
    return table


def remember_candidates(request_id, table, duration_ms, selected_id=None):
    with _CANDIDATES_LOCK:
        _CANDIDATES_CACHE[str(request_id)] = {
            'request_id': str(request_id),
            'built_at': _utc_now_iso(),
            'duration_ms': duration_ms,
            'selected_engineer_id': selected_id,
            'candidates': table,
        }


def _cache_is_fresh(entry):
    """Запись кэша кандидатов живёт не дольше CANDIDATE_CACHE_TTL_SEC."""
    try:
        built = datetime.fromisoformat(
            str(entry.get('built_at') or '').replace('Z', '+00:00'))
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - built).total_seconds()
        return age <= CANDIDATE_CACHE_TTL_SEC
    except (TypeError, ValueError):
        return False


def get_remembered_candidates(request_id):
    with _CANDIDATES_LOCK:
        entry = _CANDIDATES_CACHE.get(str(request_id))
        if entry and not _cache_is_fresh(entry):
            _CANDIDATES_CACHE.pop(str(request_id), None)
            return None
        return entry


def forget_candidates(request_id):
    """Сбрасывает кэш кандидатов после изменения заявки/назначения."""
    with _CANDIDATES_LOCK:
        return _CANDIDATES_CACHE.pop(str(request_id), None) is not None

def try_reassign_queued():
    try:
        from intravia.requests_logic import _recalc_route_for_request_id
    except ImportError:  # этап 2 разбиения: логика ещё в app.py
        from app import _recalc_route_for_request_id
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT engineer_id FROM requests
                   WHERE routing_stage='technician'
                     AND status IN ('assigned','arriving','in_progress','working')
                     AND engineer_id IS NOT NULL""")
    busy = {str(r['engineer_id']) for r in cur.fetchall()}

    # Заявки, ожидающие решения диспетчера, автоматически не назначаются:
    # это звено цепочки, через которое заявка обязана пройти.
    cur.execute("""SELECT id, aircraft, fault_type, parking_id, rejected_by
                   FROM requests
                   WHERE status='queued'
                     AND COALESCE(routing_stage,'') != 'dispatcher'
                   ORDER BY created_at ASC""")
    queued = [dict(r) for r in cur.fetchall()]
    if not queued:
        conn.close()
        return

    assigned = []
    for q in queued:
        rejected = {x.strip() for x in str(q['rejected_by']).split(',') if x.strip()}
        exclude = rejected | busy
        target = _parking_point(q['parking_id']) or _location_center()
        eng, route = get_free_engineer_for(
            conn, q['fault_type'] or 'engine_failure',
            FAULT_SKILL, target[0], target[1],
            exclude_ids=exclude,
            regulation_minutes=_request_regulation_minutes(q['id']))
        if not eng:
            continue
        cur.execute("""UPDATE requests SET engineer_id=?, engineer_name=?,
            travel_time=0, using_car=0, walk_to_car=0, car_time=0, best_car=NULL,
            engineer_comment='', status='assigned', routing_stage='technician',
            inspection_assigned_at=NULL,
            delay_minutes=0, delay_reason='', delay_started_at=NULL,
            delay_approved_by='', all_rejected=0
            WHERE id=?""",
            (eng['id'], eng['name'], q['id']))
        cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                    ('ВС ' + (q['aircraft'] or ''), eng['id']))
        busy.add(eng['id'])
        assigned.append(q['id'])
    conn.commit()
    conn.close()

    # Для автоматически назначенных заявок сразу готовим маршрут-превью.
    for assigned_rid in assigned:
        _recalc_route_for_request_id(assigned_rid, force=True)
