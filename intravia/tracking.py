"""Приём геопозиции и снапшоты агентов (вынесено из app.py)."""
import json
import math
import os
import re
import threading
from datetime import datetime, timezone

from intravia.config import BASE_DIR, _request_regulation_minutes
from intravia.db import get_db, get_engineer_number_from_device
from intravia.events import _utc_now_iso, append_event, sse_publish
from intravia.geo import _parse_timestamp, _point_in_geometry, _zone_geometry
from intravia.requests_logic import (_check_regulation_warning,
                                     _log_routing_event,
                                     _recalc_route_for_request_id,
                                     _recalc_routes_for_engineer,
                                     _route_eta_min)

_TRACK_META_LOCK = threading.Lock()
_TRACK_META = {}

def _log_ingest_error(raw_point, error):
    """Журнал ошибок приёма геоданных (append-only JSONL, без изменения схемы БД)."""
    try:
        path = os.path.join(BASE_DIR, 'data', 'ingest_errors.log')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = json.dumps(raw_point, ensure_ascii=False, default=str)[:1000]
        with open(path, 'a', encoding='utf-8') as log_file:
            log_file.write(json.dumps({'at': _utc_now_iso(), 'error': error,
                                       'point': payload}, ensure_ascii=False) + '\n')
    except Exception as exc:
        print(f'[TRACK] log error: {exc}')
    append_event('ingest_error', error=error,
                 obj=(raw_point.get('obj') if isinstance(raw_point, dict) else None),
                 point=json.dumps(raw_point, ensure_ascii=False, default=str)[:500])


def _track_known_object(raw_obj):
    """Сопоставляет obj из контракта с агентом системы.

    Поддерживаются канонические id (`eng_1`, `CAR-001`, `SU-1001`), номера
    из контракта (`e-01`, `v-01`) и регистрация ВС (`RA-73142`).
    """
    raw = str(raw_obj or '').strip()
    if not raw:
        return None
    needle = raw.lower()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, device_id FROM engineers")
        for row in cur.fetchall():
            eid = str(row['id'])
            device = str(row['device_id'] or '')
            number = get_engineer_number_from_device(device)
            aliases = {eid.lower(), device.lower()}
            if number:
                aliases.update({f'e-{number:02d}', f'e-{number}',
                                f'en-{number:03d}', f'eng-{number}',
                                f'at-{number:02d}', f'at-{number:03d}',
                                f'at-{number:04d}', f'at-{number}'})
            if needle in aliases:
                return {'type': 'engineer', 'id': eid, 'label': row['name'] or device}

        cur.execute("SELECT id, device_id FROM transport")
        for row in cur.fetchall():
            tid = str(row['id'])
            device = str(row['device_id'] or '')
            aliases = {tid.lower(), device.lower(), f'v-{int(tid):02d}', f'v-{tid}',
                       f'vehicle-{int(tid):02d}', f'vehicle-{int(tid):03d}',
                       f'vehicle-{tid}'}
            if needle in aliases:
                return {'type': 'transport', 'id': tid, 'label': device or tid}

        cur.execute("SELECT id, callsign FROM aircraft")
        for row in cur.fetchall():
            callsign = str(row['callsign'] or '')
            aliases = {str(row['id']).lower(), callsign.lower(),
                       f'aircraft-{str(row["id"]).lower()}'}
            try:
                aliases.add(f'ac-{int(row["id"]):02d}')
            except (TypeError, ValueError):
                pass
            if needle in aliases:
                return {'type': 'aircraft', 'id': callsign or str(row['id']),
                        'label': callsign or str(row['id'])}

        cur.execute("SELECT registration FROM aircraft_registry "
                    "WHERE UPPER(registration)=?", (raw.upper(),))
        row = cur.fetchone()
        if row:
            return {'type': 'aircraft', 'id': str(row['registration']).upper(),
                    'label': str(row['registration']).upper()}

        cur.execute("SELECT DISTINCT aircraft FROM requests "
                    "WHERE UPPER(aircraft)=? LIMIT 1", (raw.upper(),))
        row = cur.fetchone()
        if row:
            return {'type': 'aircraft', 'id': str(row['aircraft']).upper(),
                    'label': str(row['aircraft']).upper()}

        # Внешний имитатор может прислать борт по регистрации (RA-73101),
        # которого ещё нет в БД. Принимаем такую регистрацию как id борта;
        # сама запись создаётся в _track_apply_position при первой точке.
        reg = raw.upper()
        if re.fullmatch(r'[A-Z]{2}-?\d{3,5}', reg):
            return {'type': 'aircraft', 'id': reg, 'label': reg}
    finally:
        conn.close()
    return None

def _stamp_route_arrival(cur, request_id, comment, at, elapsed_min, eta_min, delta_min):
    """Дописывает факт прибытия в сохранённый ROUTE JSON (без изменения схемы)."""
    comment = comment or ''
    if not comment.startswith('ROUTE:'):
        return comment
    rest = comment[6:]
    depth = 0
    end = -1
    for i, ch in enumerate(rest):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end <= 0:
        return comment
    try:
        payload = json.loads(rest[:end])
    except (TypeError, ValueError):
        return comment
    payload['arrival'] = {'at': at, 'elapsed_min': elapsed_min,
                          'eta_min': eta_min, 'delta_min': delta_min}
    return 'ROUTE:' + json.dumps(payload, ensure_ascii=False) + rest[end:]

def _remember_track_meta(kind, object_id, acc, spd, when, heading=None):
    """Запоминает acc/spd/heading последней точки агента (без изменения схемы БД)."""
    with _TRACK_META_LOCK:
        _TRACK_META[(kind, str(object_id))] = {'acc': acc, 'spd': spd,
                                               'heading': heading, 't': when}

def _pause_reason_for_cursor(cur, kind, object_id):
    """Причина паузы приёма точки: демо-настройка paused/paused_until заявки."""
    from intravia.config import _request_is_paused
    request_id = None
    if kind == 'engineer':
        cur.execute("""SELECT id FROM requests
                       WHERE engineer_id=? AND routing_stage='technician'
                         AND status NOT IN ('completed','cancelled',
                                            'flight_cancelled','draft',
                                            'report_submitted','deleted')
                       ORDER BY created_at DESC LIMIT 1""", (object_id,))
        row = cur.fetchone()
        request_id = row['id'] if row else None
    elif kind == 'transport':
        try:
            cur.execute("SELECT assigned_to FROM transport WHERE id=?",
                        (int(object_id),))
        except (TypeError, ValueError):
            return None
        row = cur.fetchone()
        request_id = row['assigned_to'] if row else None
    if request_id and _request_is_paused(request_id):
        return 'paused'
    return None


def track_pause_reason(kind, object_id):
    """Публичная проверка паузы для GPS-эндпоинта (applied:false, reason)."""
    conn = get_db()
    try:
        return _pause_reason_for_cursor(conn.cursor(), kind, object_id)
    finally:
        conn.close()


def _track_apply_position(kind, object_id, lat, lng, when, acc=None, spd=None,
                          heading=None, update_coords=True, recalc=True):
    conn = get_db()
    arrival_event = None
    active_request_id = None
    try:
        cur = conn.cursor()
        assigned_request = None
        # Пауза демонстрационной заявки: точку не применяем вообще (ни GPS,
        # ни транспорт), чтобы проверить сценарий «данные не поступают».
        if _pause_reason_for_cursor(cur, kind, object_id):
            return False
        if kind == 'engineer':
            if update_coords:
                cur.execute("UPDATE engineers SET coords_lat=?, coords_lng=? WHERE id=?",
                            (lat, lng, object_id))
                if cur.rowcount == 0:
                    return False
            else:
                cur.execute("SELECT id FROM engineers WHERE id=?", (object_id,))
                if not cur.fetchone():
                    return False

            # Автоматическое прибытие: точка внутри полигона зоны обслуживания.
            cur.execute("""SELECT r.id, r.parking_id, r.inspection_assigned_at,
                                  r.engineer_comment, e.task_step
                           FROM requests r JOIN engineers e ON e.id=r.engineer_id
                           WHERE r.engineer_id=? AND r.routing_stage='technician'
                             AND r.status NOT IN ('completed','cancelled',
                                                  'flight_cancelled','draft',
                                                  'report_submitted','deleted')
                           ORDER BY r.created_at DESC LIMIT 1""", (object_id,))
            active = cur.fetchone()
            if active:
                active_request_id = active['id']
            if active and active['parking_id']:
                geometry = _zone_geometry(active['parking_id'])
                if geometry and _point_in_geometry(lat, lng, geometry):
                    if active['task_step'] not in ('arrived', 'diagnosing'):
                        cur.execute("""UPDATE engineers SET task_step='arrived'
                                       WHERE id=? AND task_step IN ('accepted','in_car')""",
                                    (object_id,))
                        if cur.rowcount > 0:
                            started = _parse_timestamp(active['inspection_assigned_at'])
                            elapsed_min = None
                            if started:
                                elapsed_min = round(
                                    (datetime.now(timezone.utc) - started).total_seconds() / 60.0, 1)
                            regulation = _request_regulation_minutes(active['id'])
                            late = elapsed_min is not None and elapsed_min > regulation
                            eta_min = _route_eta_min(active['engineer_comment'])
                            delta_min = (round(elapsed_min - eta_min, 1)
                                         if elapsed_min is not None and eta_min is not None
                                         else None)
                            note = f'Автоматическое прибытие в зону {active["parking_id"]}'
                            if elapsed_min is not None:
                                note += (f'; {elapsed_min} мин с назначения — '
                                         + (f'нарушение норматива {regulation} мин' if late
                                            else 'в пределах норматива'))
                            if eta_min is not None and delta_min is not None:
                                note += f'; ETA {eta_min} мин, отклонение {delta_min:+} мин'
                            _log_routing_event(conn, active['id'], 'technician', 'GPS', note)
                            cur.execute("UPDATE requests SET engineer_comment=? WHERE id=?",
                                        (_stamp_route_arrival(cur, active['id'],
                                                               active['engineer_comment'],
                                                               _utc_now_iso(), elapsed_min,
                                                               eta_min, delta_min),
                                         active['id']))
                            arrival_event = {
                                'request_id': active['id'],
                                'zone': active['parking_id'],
                                'lat': lat, 'lon': lng,
                                'elapsed_min': elapsed_min,
                                'eta_min': eta_min,
                                'delta_min': delta_min,
                                'regulation_minutes': regulation,
                                'late': bool(late),
                                'at': _utc_now_iso(),
                            }
        elif kind == 'transport':
            cur.execute("SELECT assigned_to FROM transport WHERE id=?", (int(object_id),))
            row = cur.fetchone()
            assigned_request = row['assigned_to'] if row else None
            cur.execute("UPDATE transport SET lat=?, lng=? WHERE id=?",
                        (lat, lng, int(object_id)))
            if cur.rowcount == 0:
                return False
        else:  # aircraft
            cur.execute("SELECT id FROM aircraft WHERE id=? OR UPPER(callsign)=?",
                        (object_id, object_id.upper()))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE aircraft SET lat=?, lng=?, updated_at=CURRENT_TIMESTAMP "
                            "WHERE id=?", (lat, lng, row['id']))
                object_id = str(row['id'])
            elif re.fullmatch(r'[A-Z]{2}-?\d{3,5}', str(object_id).upper()):
                # Борт от внешнего имитатора по регистрации: регистрируем на лету.
                reg = str(object_id).upper()
                cur.execute("""INSERT INTO aircraft
                               (id, callsign, lat, lng, status, updated_at)
                               VALUES (?, ?, ?, ?, 'parked', CURRENT_TIMESTAMP)""",
                            (reg, reg, lat, lng))
                object_id = reg

        if update_coords:
            cur.execute("""INSERT INTO map_object_positions
                           (object_type, object_id, lat, lng, updated_at)
                           VALUES (?,?,?,?,?)
                           ON CONFLICT(object_type, object_id) DO UPDATE SET
                             lat=excluded.lat, lng=excluded.lng, updated_at=excluded.updated_at""",
                        (kind, str(object_id), lat, lng, when))
        conn.commit()
    finally:
        conn.close()

    if kind == 'engineer':
        if recalc:
            try:
                _recalc_routes_for_engineer(object_id, force=True)
            except Exception as exc:
                print(f'[TRACK] route recalc engineer: {exc}')
        if active_request_id and not arrival_event:
            _check_regulation_warning(active_request_id)
    elif kind == 'transport' and assigned_request:
        try:
            _recalc_route_for_request_id(assigned_request, force=True)
        except Exception as exc:
            print(f'[TRACK] route recalc request: {exc}')

    _remember_track_meta(kind, object_id, acc, spd, when, heading)
    sse_publish('position', {'type': kind, 'obj': str(object_id),
                             'lat': lat, 'lon': lng, 't': when,
                             'acc': acc, 'spd': spd, 'heading': heading})
    if arrival_event:
        append_event('arrival', **arrival_event)
        sse_publish('arrival', arrival_event)
    return True

def _track_process_point(point):
    result = {'accepted': False, 'applied': False, 'obj': None, 't': None}
    if not isinstance(point, dict):
        _log_ingest_error(point, 'point is not an object')
        result['error'] = 'Точка должна быть JSON-объектом'
        return result

    obj = point.get('obj')
    result['obj'] = obj
    lat_raw = point.get('lat')
    lon_raw = point.get('lon', point.get('lng'))
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError('out of range')
    except (TypeError, ValueError):
        _log_ingest_error(point, 'lat/lon missing or not numeric')
        result['error'] = 'lat/lon обязательны и должны быть числами'
        return result

    metrics = {}
    for field in ('acc', 'spd'):
        raw = point.get(field)
        if raw is None or raw == '':
            metrics[field] = None
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = None
        if value is None or not math.isfinite(value) or value < 0:
            _log_ingest_error(point, 'invalid ' + field)
            result['error'] = f'{field} должен быть неотрицательным числом'
            return result
        metrics[field] = value

    when = point.get('t')
    if not isinstance(when, str) or not when.strip():
        when = _utc_now_iso()
    result['accepted'] = True
    result['t'] = when

    known = _track_known_object(obj)
    if not known:
        _log_ingest_error(point, 'unknown object')
        return result

    paused = track_pause_reason(known['type'], known['id'])
    if paused:
        result['applied'] = False
        result['reason'] = paused
        return result

    result['applied'] = _track_apply_position(known['type'], known['id'],
                                              lat, lon, when,
                                              acc=metrics['acc'],
                                              spd=metrics['spd'])
    return result

def _attach_track_meta(item, kind):
    """Добавляет к агенту последние acc/spd из принятых точек."""
    with _TRACK_META_LOCK:
        meta = _TRACK_META.get((kind, str(item.get('obj')))) \
            or _TRACK_META.get((kind, str(item.get('id'))))
    item['acc'] = meta.get('acc') if meta else None
    item['spd'] = meta.get('spd') if meta else None
    if meta and meta.get('t'):
        item['t'] = meta['t']
    return item

def _agents_snapshot():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT e.id, e.name, e.device_id, e.specialization,
                              e.status, e.task_step, e.task,
                              COALESCE(g.lat, p.lat, e.coords_lat) AS lat,
                              COALESCE(g.lng, p.lng, e.coords_lng) AS lng,
                              g.updated_at AS gps_updated_at
                       FROM engineers e
                       LEFT JOIN map_object_positions g
                         ON g.object_type='engineer_gps' AND g.object_id=e.id
                       LEFT JOIN map_object_positions p
                         ON p.object_type='engineer' AND p.object_id=e.id
                       ORDER BY e.device_id""")
        engineers = []
        for row in cur.fetchall():
            item = dict(row)
            item['obj'] = str(item['id'])
            item['kind'] = 'engineer'
            item['label'] = item.get('device_id') or item.get('name') or item['id']
            item['number'] = get_engineer_number_from_device(item.get('device_id'))
            engineers.append(_attach_track_meta(item, 'engineer'))

        cur.execute("""SELECT t.id, t.device_id, t.assigned_to,
                              COALESCE(p.lat, t.lat) AS lat,
                              COALESCE(p.lng, t.lng) AS lng
                       FROM transport t
                       LEFT JOIN map_object_positions p
                         ON p.object_type='transport' AND p.object_id=CAST(t.id AS TEXT)
                       ORDER BY t.id""")
        transport = []
        for row in cur.fetchall():
            item = dict(row)
            item['obj'] = str(item['id'])
            item['kind'] = 'transport'
            item['label'] = item.get('device_id') or ('CAR-' + str(item['id']))
            transport.append(_attach_track_meta(item, 'transport'))

        cur.execute("SELECT id, callsign, lat, lng, status, heading, speed "
                    "FROM aircraft ORDER BY callsign")
        aircraft = []
        for row in cur.fetchall():
            item = dict(row)
            item['obj'] = item.get('callsign') or str(item['id'])
            item['kind'] = 'aircraft'
            item['label'] = item.get('callsign') or str(item['id'])
            aircraft.append(_attach_track_meta(item, 'aircraft'))

        cur.execute("""SELECT object_id, lat, lng, updated_at
                       FROM map_object_positions WHERE object_type='aircraft'""")
        known_aircraft = {str(a.get('obj')) for a in aircraft}
        for row in cur.fetchall():
            if str(row['object_id']) not in known_aircraft:
                aircraft.append(_attach_track_meta({
                    'id': row['object_id'], 'obj': row['object_id'],
                    'kind': 'aircraft', 'label': row['object_id'],
                    'callsign': row['object_id'],
                    'lat': row['lat'], 'lng': row['lng'],
                    'status': 'tracked', 'updated_at': row['updated_at']
                }, 'aircraft'))

        return {'server_time': _utc_now_iso(), 'engineers': engineers,
                'transport': transport, 'aircraft': aircraft}
    finally:
        conn.close()
