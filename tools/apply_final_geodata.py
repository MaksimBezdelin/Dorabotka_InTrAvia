#!/usr/bin/env python3
"""Применение геоданных финального этапа ИТ-2026 (ВДНХ) к проекту InTrAvia.

Что делает:
  * aircrafts_positions        -> aircraft.lat/lng (+ map_object_positions 'aircraft')
  * engineers_positions        -> engineers.coords_lat/lng (+ map_object_positions 'engineer')
                                  и engineer_assignment (AT-ID, grade, scope, skill,
                                  scope_desc, is_live). AT-000N сопоставляется с инженером
                                  по номеру device_id (EN-00N) с учётом live-свапа
                                  (INTRAVIA_LIVE_ENGINEER, по умолчанию eng_3).
  * service_vehicles_positions -> transport.lat/lng (+ map_object_positions 'transport')
                                  vehicle-NN -> transport.id (алиас из tracking.py).
  * service_zones              -> static/geodata/vdnh/service_zones.geojson:
                                  сохраняются id и геометрия, добавляется name и mc_id="мс-NN".

Границы:
  * НЕ трогает requests (включая живые заявки), users, task_step/status инженеров;
  * НЕ трогает map_object_positions с object_type 'engineer_gps'/'engineer_gps_heading';
  * не изменяет таблицы FIM (fault_classification, mel_catalog, aog_catalog, ...).

Идемпотентность: повторный запуск не меняет данные (сравнение значений до записи).
Перед записью в БД делает копию database.db.bak_geodata_<timestamp>.
После записи выполняет PRAGMA integrity_check.

Использование:
  python tools/apply_final_geodata.py            # dry-run по умолчанию
  python tools/apply_final_geodata.py --dry-run  # явный dry-run
  python tools/apply_final_geodata.py --apply    # запись
"""
import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GEODATA = os.path.join(BASE_DIR, 'audit_tmp', 'geodata', 'extracted')
DEFAULT_DB = os.path.join(BASE_DIR, 'database.db')
DEFAULT_ZONES_DST = os.path.join(BASE_DIR, 'static', 'geodata', 'vdnh', 'service_zones.geojson')
LOG_DIR = os.path.join(BASE_DIR, 'audit_tmp', 'geodata')

# Границы полигона ВДНХ из config/location.json (bbox) с запасом.
LON_MIN, LON_MAX = 37.55, 37.70
LAT_MIN, LAT_MAX = 55.80, 55.86

FIM_TABLES = ('fault_classification', 'fault_classification_rules', 'mel_catalog',
              'aog_catalog', 'fault_ata_systems')
ENGINEER_GPS_TYPES = ('engineer_gps', 'engineer_gps_heading')
PROTECTED_MOP_TYPES = ENGINEER_GPS_TYPES
LIVE_REQUEST_IDS = ('req_1795559334743', 'req_1795590196679')

log = logging.getLogger('geodata')


def setup_logging(apply_mode):
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(LOG_DIR, 'apply_geodata_%s_%s.log' % (stamp, 'apply' if apply_mode else 'dryrun'))
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter('%(asctime)s %(levelname)-7s %(message)s')
    fh = logging.FileHandler(path, encoding='utf-8')
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return path


def utc_now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def load_geojson(path):
    with open(path, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    if data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
        raise ValueError('не FeatureCollection: %s' % path)
    return data


def iter_points(fc, name):
    """Отдаёт (properties, lon, lat), проверяя тип геометрии и границы."""
    for idx, feat in enumerate(fc['features']):
        geom = feat.get('geometry') or {}
        if geom.get('type') != 'Point':
            raise ValueError('%s[%d]: ожидалась Point, получено %r' % (name, idx, geom.get('type')))
        coord = geom.get('coordinates')
        if not (isinstance(coord, (list, tuple)) and len(coord) >= 2):
            raise ValueError('%s[%d]: некорректные coordinates %r' % (name, idx, coord))
        lon, lat = float(coord[0]), float(coord[1])
        if not (LON_MIN <= lon <= LON_MAX and LAT_MIN <= lat <= LAT_MAX):
            raise ValueError('%s[%d]: координаты вне полигона ВДНХ: lon=%s lat=%s' % (name, idx, lon, lat))
        yield feat.get('properties') or {}, lon, lat


def table_exists(cur, name):
    return cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def count(cur, table):
    if not table_exists(cur, table):
        return None
    return cur.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0]


def snapshot(cur):
    snap = {t: count(cur, t) for t in ('users', 'requests', 'engineers', 'aircraft',
                                       'transport') + FIM_TABLES}
    snap['engineer_gps_rows'] = cur.execute(
        "SELECT COUNT(*) FROM map_object_positions WHERE object_type IN (?,?)",
        ENGINEER_GPS_TYPES).fetchone()[0]
    snap['mop_other_rows'] = cur.execute(
        "SELECT COUNT(*) FROM map_object_positions "
        "WHERE object_type NOT IN ('engineer','transport','aircraft','engineer_gps','engineer_gps_heading')"
    ).fetchone()[0]
    snap['engineer_state'] = cur.execute(
        "SELECT id, status, task_step FROM engineers ORDER BY id").fetchall()
    snap['live_requests'] = {}
    for rid in LIVE_REQUEST_IDS:
        row = cur.execute('SELECT * FROM requests WHERE id=?', (rid,)).fetchone()
        snap['live_requests'][rid] = tuple(row) if row else None
    return snap


def diff_snapshots(before, after):
    problems = []
    for key in ('users', 'requests', 'engineers'):
        if before.get(key) != after.get(key):
            problems.append('%s: %s -> %s' % (key, before.get(key), after.get(key)))
    if before['mop_other_rows'] != after['mop_other_rows']:
        problems.append('посторонние строки map_object_positions: %s -> %s'
                        % (before['mop_other_rows'], after['mop_other_rows']))
    if before['engineer_gps_rows'] != after['engineer_gps_rows']:
        problems.append('engineer_gps rows: %s -> %s' % (before['engineer_gps_rows'],
                                                         after['engineer_gps_rows']))
    for t in FIM_TABLES:
        if before.get(t) != after.get(t):
            problems.append('FIM %s: %s -> %s' % (t, before.get(t), after.get(t)))
    if before['engineer_state'] != after['engineer_state']:
        problems.append('engineers.status/task_step изменились')
    for rid in LIVE_REQUEST_IDS:
        if before['live_requests'].get(rid) != after['live_requests'].get(rid):
            problems.append('изменена живая заявка %s' % rid)
    return problems


ASSIGNMENT_DDL = """CREATE TABLE IF NOT EXISTS engineer_assignment(
    engineer_id TEXT PRIMARY KEY,
    at_id TEXT UNIQUE,
    grade TEXT,
    scope TEXT,
    skill TEXT,
    scope_desc TEXT,
    is_live INTEGER DEFAULT 0,
    updated_at TEXT)"""


def engineer_map(cur):
    """device_id EN-00N -> engineer.id (та же логика, что в tracking.py)."""
    mapping = {}
    for eid, device in cur.execute('SELECT id, device_id FROM engineers ORDER BY id'):
        m = re.fullmatch(r'EN-(\d+)', str(device or '').upper())
        if m:
            mapping[int(m.group(1))] = str(eid)
    return mapping


def live_engineer_map(cur, live_id=None, live_at=None):
    """EN-номер подписи AT -> engineer.id с учётом live-свапа.

    Живой инженер (INTRAVIA_LIVE_ENGINEER, по умолчанию eng_3) занимает
    AT-0001, а инженер с EN-001 получает его AT-номер (AT-0003).
    Логика совпадает с intravia/tracking.py.
    """
    mapping = engineer_map(cur)
    live_id = (live_id or os.environ.get('INTRAVIA_LIVE_ENGINEER', 'eng_3') or 'eng_3').strip()
    live_at = (live_at or os.environ.get('INTRAVIA_LIVE_AT', 'AT-0001') or 'AT-0001').strip()
    live_num = int(re.sub(r'\D', '', live_id) or 0)
    label_num = int(re.sub(r'\D', '', live_at) or 1)
    swapped = dict(mapping)
    if live_num and live_num in mapping and label_num in mapping:
        swapped[label_num] = mapping[live_num]
        swapped[live_num] = mapping[label_num]
    return swapped, live_id


def plan_engineers(cur, fc, live_id=None):
    mapping, live_id = live_engineer_map(cur, live_id)
    plan, skipped = [], []
    for props, lon, lat in iter_points(fc, 'engineers_positions'):
        tabel = str(props.get('tabel_id') or '').strip()
        m = re.fullmatch(r'AT-(\d+)', tabel, re.IGNORECASE)
        if not m:
            skipped.append((tabel, 'нет AT-NNNN'))
            continue
        num = int(m.group(1))
        eid = mapping.get(num)
        if not eid:
            skipped.append((tabel, 'в БД нет инженера с device_id EN-%03d' % num))
            continue
        row = cur.execute('SELECT coords_lat, coords_lng FROM engineers WHERE id=?', (eid,)).fetchone()
        cur_lat, cur_lng = (row[0], row[1]) if row else (None, None)
        changed = (cur_lat != lat or cur_lng != lon)
        plan.append({'tabel_id': tabel, 'at_id': 'AT-%04d' % num, 'engineer_id': eid,
                     'lon': lon, 'lat': lat,
                     'old_lat': cur_lat, 'old_lng': cur_lng, 'changed': changed,
                     'grade': props.get('grade'), 'scope': props.get('scope'),
                     'skill': props.get('skill'), 'scope_desc': props.get('scope_desc'),
                     'is_live': 1 if str(eid) == live_id else 0})
    return plan, skipped


def plan_assignments(cur, fc, live_id=None):
    """Заполнение engineer_assignment из геоданных (идемпотентно)."""
    mapping, live_id = live_engineer_map(cur, live_id)
    plan, skipped = [], []
    for props, _lon, _lat in iter_points(fc, 'engineers_positions'):
        tabel = str(props.get('tabel_id') or '').strip()
        m = re.fullmatch(r'AT-(\d+)', tabel, re.IGNORECASE)
        if not m:
            skipped.append((tabel, 'нет AT-NNNN'))
            continue
        num = int(m.group(1))
        eid = mapping.get(num)
        if not eid:
            skipped.append((tabel, 'в БД нет инженера с device_id EN-%03d' % num))
            continue
        plan.append({'engineer_id': str(eid), 'at_id': 'AT-%04d' % num,
                     'grade': props.get('grade'), 'scope': props.get('scope'),
                     'skill': props.get('skill'), 'scope_desc': props.get('scope_desc'),
                     'is_live': 1 if str(eid) == live_id else 0})
    return plan, skipped


def upsert_assignment(cur, row, when):
    """Пишет строку engineer_assignment, только если значения изменились."""
    values = (row['grade'], row['scope'], row['skill'], row['scope_desc'],
              int(row['is_live']))
    existing = cur.execute(
        'SELECT grade, scope, skill, scope_desc, is_live FROM engineer_assignment '
        'WHERE engineer_id=?', (row['engineer_id'],)).fetchone()
    same = (existing is not None and tuple(existing) == values
            and cur.execute('SELECT at_id FROM engineer_assignment WHERE engineer_id=?',
                            (row['engineer_id'],)).fetchone()[0] == row['at_id'])
    if same:
        return False
    cur.execute("""INSERT INTO engineer_assignment
                   (engineer_id, at_id, grade, scope, skill, scope_desc, is_live, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(engineer_id) DO UPDATE SET
                     at_id=excluded.at_id, grade=excluded.grade, scope=excluded.scope,
                     skill=excluded.skill, scope_desc=excluded.scope_desc,
                     is_live=excluded.is_live, updated_at=excluded.updated_at""",
                (row['engineer_id'], row['at_id'], row['grade'], row['scope'],
                 row['skill'], row['scope_desc'], int(row['is_live']), when))
    return True


def plan_transport(cur, fc):
    plan, skipped = [], []
    for props, lon, lat in iter_points(fc, 'service_vehicles_positions'):
        oid = str(props.get('object_id') or '').strip()
        m = re.fullmatch(r'vehicle-(\d+)', oid, re.IGNORECASE)
        if not m:
            skipped.append((oid, 'не vehicle-NN'))
            continue
        tid = int(m.group(1))
        row = cur.execute('SELECT lat, lng FROM transport WHERE id=?', (tid,)).fetchone()
        if not row:
            skipped.append((oid, 'в БД нет transport.id=%d' % tid))
            continue
        changed = (row[0] != lat or row[1] != lon)
        plan.append({'object_id': oid, 'transport_id': tid, 'lon': lon, 'lat': lat,
                     'old_lat': row[0], 'old_lng': row[1], 'changed': changed,
                     'model': props.get('model')})
    return plan, skipped


def plan_aircraft(cur, fc):
    plan = []
    for props, lon, lat in iter_points(fc, 'aircrafts_positions'):
        oid = str(props.get('object_id') or '').strip()
        if not re.fullmatch(r'[A-Z]{2}-?\d{3,5}', oid.upper()):
            raise ValueError('aircrafts_positions: неожиданный object_id %r' % oid)
        row = cur.execute('SELECT id, callsign, lat, lng FROM aircraft '
                          'WHERE id=? OR UPPER(callsign)=?', (oid, oid.upper())).fetchone()
        if row:
            changed = (row[2] != lat or row[3] != lon)
            plan.append({'object_id': oid, 'aircraft_id': str(row[0]), 'lon': lon, 'lat': lat,
                         'old_lat': row[2], 'old_lng': row[3], 'changed': changed,
                         'insert': False, 'name': props.get('name')})
        else:
            plan.append({'object_id': oid, 'aircraft_id': oid.upper(), 'lon': lon, 'lat': lat,
                         'old_lat': None, 'old_lng': None, 'changed': True,
                         'insert': True, 'name': props.get('name')})
    return plan


def upsert_mop(cur, kind, object_id, lat, lon, when):
    """Обновляет map_object_positions, только если координаты реально изменились."""
    assert kind not in PROTECTED_MOP_TYPES, 'запрещённый object_type: %s' % kind
    row = cur.execute('SELECT lat, lng FROM map_object_positions '
                      'WHERE object_type=? AND object_id=?', (kind, str(object_id))).fetchone()
    if row and row[0] == lat and row[1] == lon:
        return False
    if row:
        cur.execute('UPDATE map_object_positions SET lat=?, lng=?, updated_at=? '
                    'WHERE object_type=? AND object_id=?',
                    (lat, lon, when, kind, str(object_id)))
    else:
        cur.execute('INSERT INTO map_object_positions (object_type, object_id, lat, lng, updated_at) '
                    'VALUES (?,?,?,?,?)', (kind, str(object_id), lat, lon, when))
    return True


def apply_db(db_path, engineers_fc, vehicles_fc, aircraft_fc, dry_run):
    when = utc_now_iso()
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    stats = {'engineering_changed': 0, 'transport_changed': 0, 'aircraft_changed': 0,
             'aircraft_inserted': 0, 'mop_written': 0, 'assignments_written': 0}
    try:
        cur = conn.cursor()
        for tbl in ('engineers', 'transport', 'aircraft', 'map_object_positions', 'requests', 'users'):
            if not table_exists(cur, tbl):
                raise RuntimeError('в БД отсутствует обязательная таблица %s' % tbl)
        for tbl in FIM_TABLES:
            if not table_exists(cur, tbl):
                log.warning('FIM-таблица %s отсутствует (пропускаю проверку)', tbl)
        cur.execute(ASSIGNMENT_DDL)

        before = snapshot(cur)
        eng_plan, eng_skip = plan_engineers(cur, engineers_fc)
        assign_plan, assign_skip = plan_assignments(cur, engineers_fc)
        tr_plan, tr_skip = plan_transport(cur, vehicles_fc)
        ac_plan = plan_aircraft(cur, aircraft_fc)

        log.info('План инженеров: %d, пропущено: %d', len(eng_plan), len(eng_skip))
        for s in eng_skip:
            log.warning('  инженер %s пропущен: %s', s[0], s[1])
        for p in eng_plan:
            log.info('  %s -> %s %s (%s, %s)', p['tabel_id'], p['engineer_id'],
                     'ИЗМЕНИТЬ' if p['changed'] else 'без изменений',
                     p['lat'], p['lon'])
        log.info('План назначений engineer_assignment: %d, пропущено: %d',
                 len(assign_plan), len(assign_skip))
        for s in assign_skip:
            log.warning('  назначение %s пропущено: %s', s[0], s[1])
        for p in assign_plan:
            log.info('  %s -> %s grade=%s scope=%s skill=%s live=%s', p['at_id'],
                     p['engineer_id'], p['grade'], p['scope'], p['skill'], p['is_live'])
        log.info('План транспорта: %d, пропущено: %d', len(tr_plan), len(tr_skip))
        for s in tr_skip:
            log.warning('  транспорт %s пропущен: %s', s[0], s[1])
        for p in tr_plan:
            log.info('  %s -> transport.id=%s %s (%s, %s) model=%r', p['object_id'],
                     p['transport_id'], 'ИЗМЕНИТЬ' if p['changed'] else 'без изменений',
                     p['lat'], p['lon'], p['model'])
        log.info('План ВС: %d', len(ac_plan))
        for p in ac_plan:
            log.info('  %s -> aircraft.id=%s %s (%s, %s)', p['object_id'], p['aircraft_id'],
                     'INSERT' if p['insert'] else ('ИЗМЕНИТЬ' if p['changed'] else 'без изменений'),
                     p['lat'], p['lon'])

        if dry_run:
            log.info('DRY-RUN: запись в БД не выполняется')
            return stats, eng_plan, tr_plan, ac_plan

        cur.execute('BEGIN IMMEDIATE')
        try:
            for p in eng_plan:
                if not p['changed']:
                    continue
                cur.execute('UPDATE engineers SET coords_lat=?, coords_lng=? WHERE id=?',
                            (p['lat'], p['lon'], p['engineer_id']))
                if cur.rowcount != 1:
                    raise RuntimeError('engineers: ожидалась 1 строка для %s' % p['engineer_id'])
                stats['engineering_changed'] += 1
            for p in eng_plan:
                if upsert_mop(cur, 'engineer', p['engineer_id'], p['lat'], p['lon'], when):
                    stats['mop_written'] += 1
            for p in assign_plan:
                if upsert_assignment(cur, p, when):
                    stats['assignments_written'] += 1

            for p in tr_plan:
                if p['changed']:
                    cur.execute('UPDATE transport SET lat=?, lng=? WHERE id=?',
                                (p['lat'], p['lon'], p['transport_id']))
                    if cur.rowcount != 1:
                        raise RuntimeError('transport: ожидалась 1 строка для id=%s' % p['transport_id'])
                    stats['transport_changed'] += 1
                if upsert_mop(cur, 'transport', str(p['transport_id']), p['lat'], p['lon'], when):
                    stats['mop_written'] += 1

            for p in ac_plan:
                if p['insert']:
                    cur.execute("INSERT INTO aircraft (id, callsign, lat, lng, status, updated_at) "
                                "VALUES (?,?,?,?,'parked',CURRENT_TIMESTAMP)",
                                (p['aircraft_id'], p['aircraft_id'], p['lat'], p['lon']))
                    stats['aircraft_inserted'] += 1
                elif p['changed']:
                    cur.execute('UPDATE aircraft SET lat=?, lng=?, updated_at=CURRENT_TIMESTAMP '
                                'WHERE id=?', (p['lat'], p['lon'], p['aircraft_id']))
                    stats['aircraft_changed'] += 1
                if upsert_mop(cur, 'aircraft', p['aircraft_id'], p['lat'], p['lon'], when):
                    stats['mop_written'] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            log.error('Транзакция откатана из-за ошибки')
            raise

        after = snapshot(cur)
        problems = diff_snapshots(before, after)
        if problems:
            log.error('Проверка инвариантов не пройдена: %s', '; '.join(problems))
            raise RuntimeError('инварианты нарушены: %s' % '; '.join(problems))
        log.info('Инварианты соблюдены: users=%s, requests=%s, engineer_gps=%s, FIM=%s',
                 after['users'], after['requests'], after['engineer_gps_rows'],
                 {t: after.get(t) for t in FIM_TABLES})
        return stats, eng_plan, tr_plan, ac_plan
    finally:
        conn.close()


def zone_mc_id(props):
    name = str(props.get('name') or '').strip()
    m = re.fullmatch(r'(?:мс|mc)[-_]?(\d+)', name, re.IGNORECASE)
    if m:
        return 'мс-%02d' % int(m.group(1))
    zid = props.get('id')
    return 'мс-%02d' % int(zid)


def render_zones(fc):
    """Собирает текст файла в исходном стиле: заголовок + один feature на строку."""
    lines = [
        '{',
        '"type": "FeatureCollection",',
        '"name": "service_zones",',
        '"crs": { "type": "name", "properties": { "name": "urn:ogc:def:crs:OGC:1.3:CRS84" } },',
        '"features": [',
    ]
    feats = []
    for feat in fc['features']:
        props = dict(feat.get('properties') or {})
        zid = props.get('id')
        if not isinstance(zid, int):
            raise ValueError('service_zones: id зоны должен быть целым, получено %r' % zid)
        out_props = {'id': zid, 'name': str(props.get('name') or 'мс-%02d' % zid),
                     'mc_id': zone_mc_id(props)}
        item = {'type': 'Feature', 'properties': out_props, 'geometry': feat['geometry']}
        feats.append(json.dumps(item, ensure_ascii=False, separators=(',', ':')))
    lines.append(',\n'.join(feats))
    lines.append(']')
    lines.append('}')
    return '\n'.join(lines) + '\n'


def apply_zones(src_path, dst_path, dry_run):
    fc = load_geojson(src_path)
    if len(fc['features']) != 10:
        raise ValueError('service_zones: ожидалось 10 зон, найдено %d' % len(fc['features']))
    ids = [f.get('properties', {}).get('id') for f in fc['features']]
    if sorted(ids) != list(range(1, 11)):
        raise ValueError('service_zones: id должны быть 1..10, получено %r' % ids)
    for feat in fc['features']:
        if (feat.get('geometry') or {}).get('type') not in ('Polygon', 'MultiPolygon'):
            raise ValueError('service_zones: ожидался Polygon/MultiPolygon')

    new_text = render_zones(fc)
    old_text = None
    if os.path.isfile(dst_path):
        with open(dst_path, 'r', encoding='utf-8') as fh:
            old_text = fh.read()
    if old_text == new_text:
        log.info('service_zones: файл уже актуален, запись не требуется (%s)', dst_path)
        return False
    log.info('service_zones: %s (%d зон, mc_id=%s)', dst_path,
             len(fc['features']), [zone_mc_id(f['properties']) for f in fc['features']])
    if dry_run:
        log.info('DRY-RUN: файл зон не перезаписывается')
        return False
    if old_text is not None:
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        bak = dst_path + '.bak_geodata_' + stamp
        shutil.copy2(dst_path, bak)
        log.info('бэкап зон: %s', bak)
    tmp = dst_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(new_text)
    os.replace(tmp, dst_path)
    log.info('service_zones записан: %s', dst_path)
    return True


def backup_db(db_path):
    # WAL может содержать ещё не сброшенные транзакции: сначала чекпойнт,
    # чтобы обычная копия файла БД была полной и согласованной.
    wal = db_path + '-wal'
    if os.path.isfile(wal) and os.path.getsize(wal) > 0:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            conn.close()
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    bak = '%s.bak_geodata_%s' % (db_path, stamp)
    shutil.copy2(db_path, bak)
    wal_size = os.path.getsize(wal) if os.path.isfile(wal) else 0
    log.info('бэкап БД: %s (wal=%d байт)', bak, wal_size)
    conn = sqlite3.connect('file:%s?mode=ro' % bak.replace('\\', '/'), uri=True)
    try:
        status = conn.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        conn.close()
    if status != 'ok':
        raise RuntimeError('integrity_check бэкапа вернул %r' % status)
    log.info('integrity_check бэкапа: ok')
    return bak


def integrity_check(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        status = conn.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        conn.close()
    return status


def main(argv=None):
    ap = argparse.ArgumentParser(description='Применение геоданных финального этапа ИТ-2026')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='только показать план (по умолчанию)')
    mode.add_argument('--apply', action='store_true', help='записать изменения')
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--geodata-dir', default=DEFAULT_GEODATA)
    ap.add_argument('--zones-dst', default=DEFAULT_ZONES_DST)
    args = ap.parse_args(argv)

    apply_mode = bool(args.apply)
    log_path = setup_logging(apply_mode)
    log.info('=' * 70)
    log.info('Режим: %s', 'APPLY' if apply_mode else 'DRY-RUN')
    log.info('БД: %s', args.db)
    log.info('Геоданные: %s', args.geodata_dir)
    log.info('Лог: %s', log_path)

    if not os.path.isfile(args.db):
        log.error('БД не найдена: %s', args.db)
        return 2

    try:
        engineers_fc = load_geojson(os.path.join(args.geodata_dir, 'engineers_positions.geojson'))
        vehicles_fc = load_geojson(os.path.join(args.geodata_dir, 'service_vehicles_positions.geojson'))
        aircraft_fc = load_geojson(os.path.join(args.geodata_dir, 'aircrafts_positions.geojson'))
        zones_src = os.path.join(args.geodata_dir, 'service_zones.geojson')
        if not os.path.isfile(zones_src):
            raise FileNotFoundError('нет service_zones.geojson в %s' % args.geodata_dir)

        if apply_mode:
            backup_db(args.db)

        stats, eng_plan, tr_plan, ac_plan = apply_db(args.db, engineers_fc, vehicles_fc,
                                                     aircraft_fc, dry_run=not apply_mode)
        zones_changed = apply_zones(zones_src, args.zones_dst, dry_run=not apply_mode)

        if apply_mode:
            status = integrity_check(args.db)
            log.info('PRAGMA integrity_check: %s', status)
            if status != 'ok':
                log.error('integrity_check не ok: %s', status)
                return 1
            log.info('Итог: инженеры изменено=%d, назначений записано=%d, транспорт=%d, '
                     'ВС обновлено=%d, ВС добавлено=%d, map_object_positions=%d, зоны=%s',
                     stats['engineering_changed'], stats['assignments_written'],
                     stats['transport_changed'], stats['aircraft_changed'],
                     stats['aircraft_inserted'], stats['mop_written'],
                     'обновлены' if zones_changed else 'без изменений')
        else:
            log.info('DRY-RUN завершён без записи')
        return 0
    except Exception as exc:
        log.exception('Ошибка применения геоданных: %s', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
