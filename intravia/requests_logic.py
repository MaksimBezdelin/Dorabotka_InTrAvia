"""Логика заявок: пересчёт маршрутов, события, нормативы (вынесено из app.py)."""
import json
import threading
import time
from datetime import datetime, timezone

import intravia.geo as _geo

from intravia.config import _request_regulation_minutes, _request_test_option
from intravia.db import get_db
from intravia.events import _utc_now_iso, append_event, sse_publish
from intravia.geo import _parse_timestamp, _parking_point
from intravia.routing import (RouteError, build_route, filter_transport_cars)

# Свежесть полевого GPS: старая точка engineer_gps не должна перекрывать
# актуальные координаты инженера (AT-0001/eng_3).
GPS_MAX_AGE_S = 60.0


def _gps_is_fresh(updated_at, now=None):
    parsed = _parse_timestamp(updated_at)
    if parsed is None:
        return False
    current = now or datetime.now(timezone.utc)
    return (current - parsed).total_seconds() <= GPS_MAX_AGE_S


def _regulation_started(row):
    """Момент старта норматива: фиксация заявки (created_at).

    Для старых/импортированных строк, где inspection_assigned_at оказался
    раньше created_at (рассинхрон часов), берём более раннее время, чтобы
    норматив не «обнулялся». В штатном потоке created_at всегда раньше.
    """
    created = _parse_timestamp(row['created_at'])
    assigned = _parse_timestamp(row['inspection_assigned_at'])
    if created and assigned:
        return min(created, assigned)
    return created or assigned


def _recompute_route_for_request(request_row):
    eng_id = request_row['engineer_id']
    parking = str(request_row['parking_id'] or '')
    if not eng_id or not parking:
        return None

    target = _parking_point(parking)
    if target is None:
        return None

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, coords_lat, coords_lng FROM engineers WHERE id=?", (eng_id,))
    eng = cur.fetchone()
    if not eng:
        conn.close()
        return None

    # Маршрут нельзя строить от условной точки: обе координаты должны
    # существовать в БД и соответствовать реальной метке инженера.
    e_lat = float(eng['coords_lat']) if eng['coords_lat'] is not None else None
    e_lng = float(eng['coords_lng']) if eng['coords_lng'] is not None else None
    # Полевой GPS мобильного устройства важнее координат симуляции:
    # если он есть, маршрут и ETA считаются от фактической точки.
    cur.execute("""SELECT lat, lng, updated_at FROM map_object_positions
                   WHERE object_type='engineer_gps' AND object_id=?""",
                (str(eng_id),))
    gps = cur.fetchone()
    if gps and gps['lat'] is not None and gps['lng'] is not None:
        if _gps_is_fresh(gps['updated_at']):
            e_lat = float(gps['lat'])
            e_lng = float(gps['lng'])
        else:
            print(f'[ROUTE] {request_row["id"]}: engineer_gps устарел '
                  f'({gps["updated_at"]}), беру coords инженера {eng_id}')
    if e_lat is None or e_lng is None:
        conn.close()
        return None

    try:
        walk_route = build_route('engineer', (e_lat, e_lng), [], target)
    except RouteError as exc:
        print(f'[ROUTE] {request_row["id"]}: {exc}')
        conn.close()
        return None

    cur.execute("SELECT id, device_id, lat, lng, speed FROM transport WHERE assigned_to=?",
                (request_row['id'],))
    assigned_car = cur.fetchone()

    if assigned_car:
        cars = [dict(assigned_car)]
    else:
        cur.execute("SELECT id, device_id, lat, lng, speed FROM transport WHERE assigned_to IS NULL")
        cars = [dict(r) for r in cur.fetchall()]
    # Только машины внутри полигона ВДНХ и рядом с графом:
    # CAR-004/005/006 из Шереметьево не участвуют в выборе.
    cars = filter_transport_cars(cars)

    best_route = walk_route
    best_car = None
    # Демонстрационная настройка заявки: использовать спецтехнику безусловно,
    # даже если пешком получилось бы быстрее.
    force_transport = bool(_request_test_option(request_row['id'], 'force_transport'))
    for car in cars:
        try:
            car_route = build_route(
                'engineer', (e_lat, e_lng), [], target,
                {'use_transport': True,
                 'car_lat': car['lat'], 'car_lng': car['lng']})
        except RouteError:
            continue
        if force_transport:
            best_route, best_car = car_route, car
            break
        if car_route['time_s'] < best_route['time_s']:
            best_route, best_car = car_route, car

    conn.close()

    walk_legs = [l for l in best_route['legs'] if l['mode'] == 'walk']
    car_legs = [l for l in best_route['legs'] if l['mode'] == 'transport']
    best_time = best_route['time_s'] / 60.0
    best_car_time = sum(l['time_s'] for l in car_legs) / 60.0
    best_walk_to_car = (walk_legs[0]['time_s'] / 60.0) if (best_car and walk_legs) else 0.0
    best_path = best_route['path']

    rounded = [[round(p[0], 6), round(p[1], 6)] for p in best_path]

    route_json = {
        'flat': round(e_lat, 6), 'flng': round(e_lng, 6),
        'tlat': round(target[0], 6), 'tlng': round(target[1], 6),
        'tt': round(best_time, 1),
        'uc': best_car is not None,
        'wc': round(best_walk_to_car, 1),
        'ct': round(best_car_time, 1),
        'wc_exact': round(best_walk_to_car, 3),
        'ct_exact': round(best_car_time, 3),
        'bclat': round(best_car['lat'], 6) if best_car else None,
        'bclng': round(best_car['lng'], 6) if best_car else None,
        'bc': best_car['device_id'] if best_car else None,
        'pid': parking,
        'path': rounded,
        'legs': best_route['legs'],
        'distance_m': best_route['distance_m'],
        'time_exact_s': round(best_route['time_s'], 1),
        'eta_min': round(best_route['time_s'] / 60.0, 1),
        'warnings': best_route['warnings'],
    }
    fields = {
        'travel_time': round(best_time, 1),
        'using_car': 1 if best_car else 0,
        'walk_to_car': round(best_walk_to_car, 1),
        'car_time': round(best_car_time, 1),
        'best_car': best_car['device_id'] if best_car else None,
    }
    return json.dumps(route_json, ensure_ascii=False), fields


def _strip_route_payload(comment):
    """Удаляет только служебный ROUTE JSON, сохраняя текст отчёта инженера."""
    comment = comment or ''
    if not comment.startswith('ROUTE:'):
        return comment
    rest = comment[6:]
    depth = 0
    end = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(rest):
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return rest.strip()
    return rest[end:].strip()


def _clear_route_for_request(cur, request_id, comment):
    """Стирает маршрут у заявки, которая больше не находится в активной работе."""
    cleaned = _strip_route_payload(comment)
    cur.execute("""UPDATE requests SET engineer_comment=?, using_car=0,
                   walk_to_car=0, car_time=0, best_car=NULL WHERE id=?""",
                (cleaned, request_id))


def _cleanup_inactive_route_payloads(cur):
    """Удаляет ROUTE JSON у всех заявок, которые не принадлежат работающим инженерам."""
    cur.execute("""SELECT r.id, r.engineer_comment FROM requests r
                   LEFT JOIN engineers e ON e.id=r.engineer_id
                   WHERE r.engineer_comment LIKE 'ROUTE:%'
                     AND NOT (
                       r.routing_stage='technician'
                       AND r.status NOT IN ('completed','cancelled','flight_cancelled','draft','report_submitted','deleted')
                       AND e.status='busy'
                       AND e.task_step IN ('pending','accepted','in_car','arrived','diagnosing')
                       AND e.coords_lat IS NOT NULL AND e.coords_lng IS NOT NULL
                       AND r.parking_id IS NOT NULL AND r.parking_id != ''
                     )""")
    stale = cur.fetchall()
    for row in stale:
        _clear_route_for_request(cur, row['id'], row['engineer_comment'])
    return len(stale)


def _recalc_route_for_request_id(request_id, force=False):
    if not request_id:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.id, r.engineer_id, r.parking_id, r.engineer_comment, r.status,
                          r.routing_stage, e.task_step AS engineer_task_step, e.status AS engineer_status,
                          e.coords_lat AS engineer_lat, e.coords_lng AS engineer_lng
                   FROM requests r LEFT JOIN engineers e ON e.id=r.engineer_id
                   WHERE r.id=?""", (request_id,))
    req = cur.fetchone()
    if not req:
        conn.close()
        return
    # Маршрут-превью создаётся сразу после назначения (pending).
    # До принятия он только отображается и не запускает движение/таймер.
    # После принятия тот же маршрут становится активным маршрутом поездки.
    route_terminal_statuses = ('completed', 'cancelled', 'flight_cancelled', 'draft', 'report_submitted')
    route_work_steps = ('pending', 'accepted', 'in_car', 'arrived', 'diagnosing')
    is_working = (req['status'] not in route_terminal_statuses and
                  req['routing_stage'] == 'technician' and
                  req['engineer_id'] is not None and req['engineer_status'] == 'busy' and
                  req['engineer_task_step'] in route_work_steps and
                  req['engineer_lat'] is not None and req['engineer_lng'] is not None and
                  str(req['parking_id'] or '') in _geo.PARKING_COORDS)
    if not is_working:
        _clear_route_for_request(cur, request_id, req['engineer_comment'])
        conn.commit()
        conn.close()
        return
    old_comment = req['engineer_comment'] or ''
    if not force and old_comment.startswith('ROUTE:') and req['status'] in ('assigned', 'arriving', 'in_progress', 'working'):
        conn.close()
        return

    route_started = time.perf_counter()
    result = _recompute_route_for_request(req)
    route_ms = round((time.perf_counter() - route_started) * 1000.0, 2)
    if not result:
        _clear_route_for_request(cur, request_id, req['engineer_comment'])
        conn.commit()
        conn.close()
        return
    new_route, fields = result

    old_comment = req['engineer_comment'] or ''
    old_eta = _route_eta_min(old_comment)
    fresh_payload = _route_payload('ROUTE:' + new_route) or {}
    new_eta = fresh_payload.get('eta_min')
    try:
        new_eta = round(float(new_eta), 1) if new_eta is not None else None
    except (TypeError, ValueError):
        new_eta = None
    if old_eta is None:
        append_event('route_built', request_id=request_id,
                     engineer_id=req['engineer_id'], eta_min=new_eta,
                     distance_m=fresh_payload.get('distance_m'),
                     duration_ms=route_ms)
    elif new_eta is not None and abs(new_eta - old_eta) >= 0.5:
        append_event('route_recalculated', request_id=request_id,
                     engineer_id=req['engineer_id'], previous_eta_min=old_eta,
                     eta_min=new_eta, duration_ms=route_ms)
        append_event('eta_updated', request_id=request_id,
                     previous_eta_min=old_eta, eta_min=new_eta,
                     delta_min=round(new_eta - old_eta, 1))
    # Пересчёт маршрута не должен стирать факт прибытия и сравнение с ETA.
    previous = _route_payload(old_comment)
    if previous and previous.get('arrival'):
        try:
            fresh = json.loads(new_route)
            fresh['arrival'] = previous['arrival']
            new_route = json.dumps(fresh, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    report_part = ''
    if old_comment.startswith('ROUTE:'):
        rest = old_comment[6:]
        depth, end = 0, -1
        for i, c in enumerate(rest):
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            report_part = rest[end:].strip()

    new_comment = 'ROUTE:' + new_route
    if report_part:
        new_comment += '\n' + report_part

    cur.execute("""UPDATE requests SET
                   engineer_comment=?, travel_time=?, using_car=?,
                   walk_to_car=?, car_time=?, best_car=?
                   WHERE id=?""",
                (new_comment, fields['travel_time'], fields['using_car'],
                 fields['walk_to_car'], fields['car_time'], fields['best_car'],
                 request_id))
    conn.commit()
    conn.close()
    print(f'[SYNC] ROUTE recalculated for {request_id}')


def _recalc_routes_for_engineer(eng_id, force=False):
    if not eng_id:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id FROM requests
                   WHERE engineer_id=? AND routing_stage='technician' AND status NOT IN
                   ('completed','cancelled','flight_cancelled','draft','report_submitted','deleted')""", (eng_id,))
    ids = [r['id'] for r in cur.fetchall()]
    conn.close()
    for rid in ids:
        _recalc_route_for_request_id(rid, force=force)
        
def _recalc_all_active_routes():
    """Пересчитывает маршруты только инженеров с принятым активным заданием.
    У остальных заявок удаляет сохранённый ROUTE JSON, не затрагивая отчёт."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.id FROM requests r
                   JOIN engineers e ON e.id=r.engineer_id
                   WHERE r.status NOT IN ('completed','cancelled','flight_cancelled','draft','report_submitted','deleted')
                     AND r.routing_stage='technician'
                     AND e.task_step IN ('pending','accepted','in_car','arrived','diagnosing')
                     AND e.status='busy'
                     AND e.coords_lat IS NOT NULL AND e.coords_lng IS NOT NULL
                     AND r.engineer_id IS NOT NULL
                     AND r.parking_id IS NOT NULL AND r.parking_id != ''""")
    active_ids = [r['id'] for r in cur.fetchall()]
    _cleanup_inactive_route_payloads(cur)
    conn.commit()
    conn.close()
    for rid in active_ids:
        try:
            _recalc_route_for_request_id(rid, force=True)
        except Exception as e:
            print(f'[RECALC] {rid}: {e}')


def _archive_requests_before_delete(cur, where_sql, params=(), reason='Удалена'):
    """Сохраняет отметку об удалении в routing_history существующей строки requests.
    Физически запись не удаляется: это и есть долговременный архив без новых таблиц.
    """
    cur.execute('SELECT id, routing_history FROM requests WHERE ' + where_sql, params)
    rows = cur.fetchall()
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        try:
            history = json.loads(row['routing_history'] or '[]')
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
        history.append({'at': now, 'stage': 'deleted', 'actor': 'Система', 'note': reason})
        cur.execute("UPDATE requests SET routing_history=?, status='deleted', routing_stage='done' WHERE id=?",
                    (json.dumps(history, ensure_ascii=False), row['id']))
    return len(rows)


def _log_routing_event(conn, rid, stage, actor, note='', **extra):
    cur = conn.cursor()
    cur.execute("SELECT routing_history FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        return
    try:
        history = json.loads(row['routing_history'] or '[]')
    except Exception:
        history = []
    event = {
        'at': datetime.now(timezone.utc).isoformat(),
        'stage': stage,
        'actor': actor,
        'note': note,
    }
    event.update(extra)
    history.append(event)
    cur.execute("UPDATE requests SET routing_history=? WHERE id=?",
                (json.dumps(history, ensure_ascii=False), rid))


def _history_has_stage(history_json, stage):
    try:
        history = json.loads(history_json or '[]')
    except (TypeError, ValueError):
        return False
    return any(isinstance(item, dict) and item.get('stage') == stage for item in history)


def _route_payload(comment):
    """Разбирает служебный ROUTE JSON из комментария заявки."""
    comment = comment or ''
    if not comment.startswith('ROUTE:'):
        return None
    rest = comment[6:]
    depth = 0
    for i, ch in enumerate(rest):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(rest[:i + 1])
                    return payload if isinstance(payload, dict) else None
                except (TypeError, ValueError):
                    return None
    return None


def _route_eta_min(comment):
    """ETA (минуты) из сохранённого ROUTE JSON, иначе None."""
    payload = _route_payload(comment)
    if not payload:
        return None
    eta = payload.get('eta_min', payload.get('tt'))
    try:
        return round(float(eta), 1) if eta is not None else None
    except (TypeError, ValueError):
        return None


def _nearest_free_engineer(conn, target_lat, target_lng):
    """Ближайший свободный инженер до цели: время пешком или на спецтранспорте."""
    cur = conn.cursor()
    cur.execute("""SELECT id, name, device_id, coords_lat, coords_lng
                   FROM engineers WHERE status='free'""")
    engineers = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT id, device_id, lat, lng FROM transport WHERE assigned_to IS NULL")
    cars = filter_transport_cars([dict(row) for row in cur.fetchall()])
    best = None
    for engineer in engineers:
        if engineer['coords_lat'] is None or engineer['coords_lng'] is None:
            continue
        start = (float(engineer['coords_lat']), float(engineer['coords_lng']))
        target = (float(target_lat), float(target_lng))
        try:
            route = build_route('engineer', start, [], target)
        except RouteError:
            continue
        for car in cars:
            try:
                car_route = build_route('engineer', start, [], target,
                                        {'use_transport': True,
                                         'car_lat': car['lat'], 'car_lng': car['lng']})
            except RouteError:
                continue
            if car_route['time_s'] < route['time_s']:
                route = car_route
        eta_min = round(route['time_s'] / 60.0, 1)
        if best is None or eta_min < best['eta_min']:
            best = {'id': engineer['id'], 'name': engineer['name'],
                    'device_id': engineer['device_id'], 'eta_min': eta_min}
    return best


def _check_regulation_warning(request_id):
    """Публикует warning за 5 минут до норматива и breach после его истечения."""
    if not request_id:
        return
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT r.id, r.parking_id, r.inspection_assigned_at,
                              r.created_at, r.routing_history, r.status,
                              r.engineer_id, r.engineer_comment, e.task_step,
                              e.name AS engineer_name
                       FROM requests r LEFT JOIN engineers e ON e.id=r.engineer_id
                       WHERE r.id=?""", (request_id,))
        row = cur.fetchone()
        if not row:
            return
        if row['task_step'] in ('arrived', 'diagnosing'):
            return
        if row['status'] in ('completed', 'cancelled', 'flight_cancelled',
                             'draft', 'report_submitted', 'deleted'):
            return
        started = _regulation_started(row)
        if not started:
            return
        elapsed_min = round(
            (datetime.now(timezone.utc) - started).total_seconds() / 60.0, 1)
        regulation = _request_regulation_minutes(request_id)
        remaining_min = round(regulation - elapsed_min, 1)
        eta_min = _route_eta_min(row['engineer_comment'])

        if remaining_min <= 0:
            if _history_has_stage(row['routing_history'], 'breach'):
                return
            note = (f'Норматив {regulation} мин нарушен: '
                    f'{elapsed_min} мин с фиксации заявки; '
                    f'назначенный инженер {row["engineer_name"] or "—"} '
                    f'(ETA {eta_min if eta_min is not None else "—"} мин)')
            _log_routing_event(conn, request_id, 'breach', 'Система', note)
            conn.commit()
            payload = {
                'request_id': request_id, 'zone': row['parking_id'],
                'engineer': row['engineer_name'], 'breach': True,
                'elapsed_min': elapsed_min, 'remaining_min': remaining_min,
                'eta_min': eta_min, 'assigned_eta_min': eta_min,
                'regulation_minutes': regulation,
                'message': ('Норматив нарушен: назначенный инженер не прибыл '
                            'в срок. Требуется решение диспетчера/MCC.'),
                'at': _utc_now_iso(),
            }
            append_event('breach', **payload)
            sse_publish('warning', payload)
            return

        if remaining_min > 5:
            return
        if _history_has_stage(row['routing_history'], 'warning'):
            return
        nearest = None
        target = _parking_point(row['parking_id'])
        if target:
            nearest = _nearest_free_engineer(conn, target[0], target[1])
        can_make_it = bool(eta_min is not None and eta_min <= remaining_min)
        note = (f'До норматива {remaining_min} мин с фиксации заявки; '
                f'ETA назначенного инженера {eta_min if eta_min is not None else "—"} мин')
        if nearest:
            note += (f'; ближайший свободный {nearest["device_id"] or nearest["name"]} '
                     f'{nearest["eta_min"]} мин')
            nearest['can_make_it'] = nearest['eta_min'] <= remaining_min
            can_make_it = nearest['can_make_it']
            note += (' — успевает' if nearest['can_make_it']
                     else ' — не успевает, рассмотреть переназначение')
        _log_routing_event(conn, request_id, 'warning', 'Система', note)
        conn.commit()
        payload = {
            'request_id': request_id, 'zone': row['parking_id'],
            'engineer': row['engineer_name'], 'breach': False,
            'elapsed_min': elapsed_min, 'remaining_min': remaining_min,
            'eta_min': eta_min, 'assigned_eta_min': eta_min,
            'nearest_free': nearest,
            'can_make_it': can_make_it,
            'regulation_minutes': regulation,
            'message': ('До истечения норматива осталось '
                        f'{remaining_min} мин. Назначенный инженер '
                        f'{row["engineer_name"] or "—"}: ETA '
                        f'{eta_min if eta_min is not None else "—"} мин.'),
            'at': _utc_now_iso(),
        }
        append_event('warning', **payload)
        sse_publish('warning', payload)
    except Exception as exc:
        print(f'[REGULATION] {request_id}: {exc}')
    finally:
        conn.close()


# Throttled-тикер нормативов: проверяет активные заявки без привязки к GPS.
# Вызывается из before_request не чаще раза в 10 секунд. Это тестобезопасный
# вариант: daemon-поток стартовал бы при импорте и влиял на pytest.
_REG_TICK_LOCK = threading.Lock()
_REG_TICK_STATE = {'last': 0.0}
REG_TICK_INTERVAL_SEC = 10.0


def maybe_run_regulation_tick(force=False, now=None):
    """Один проход по активным заявкам с ограничением частоты."""
    current = time.time() if now is None else float(now)
    with _REG_TICK_LOCK:
        if not force and current - _REG_TICK_STATE['last'] < REG_TICK_INTERVAL_SEC:
            return 0
        _REG_TICK_STATE['last'] = current

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT r.id FROM requests r
                       LEFT JOIN engineers e ON e.id=r.engineer_id
                       WHERE r.status NOT IN ('completed','cancelled',
                                              'flight_cancelled','draft',
                                              'report_submitted','deleted')
                         AND r.routing_stage='technician'
                         AND r.engineer_id IS NOT NULL
                         AND COALESCE(e.task_step,'') NOT IN ('arrived','diagnosing')""")
        request_ids = [row['id'] for row in cur.fetchall()]
    except Exception as exc:
        print(f'[REGULATION] tick query: {exc}')
        return 0
    finally:
        conn.close()

    checked = 0
    for rid in request_ids:
        try:
            _check_regulation_warning(rid)
            checked += 1
        except Exception as exc:
            print(f'[REGULATION] tick {rid}: {exc}')
    return checked
