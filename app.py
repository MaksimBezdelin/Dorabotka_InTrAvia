import os
import re
import math
import time
import json
import base64
import queue
import secrets
import sqlite3
import tempfile
import hashlib
import subprocess
import threading
import networkx as nx
from datetime import datetime, timezone, timedelta
from flask_cors import CORS
from flask import (Flask, request, jsonify, session,
                   render_template, redirect, url_for, Response, abort,
                   send_from_directory, stream_with_context)
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
    print('[LLM] openai не установлен')
# ------------------------------------------------------------
# Вынесенные модули (реэкспорт имён для обратной совместимости app.py)
# ------------------------------------------------------------
from intravia.config import *  # noqa: F401,F403
from intravia.events import *  # noqa: F401,F403
from intravia.authz import *  # noqa: F401,F403
from intravia.db import *  # noqa: F401,F403
from intravia.events import (_append_request_created, _utc_now_iso)
from intravia.authz import (_login_blocked, _login_clear, _login_key,
                            _login_record_fail)
from intravia.config import (_request_regulation_minutes, _request_test_options,
                             _set_request_test_options, _request_is_paused)
from intravia.authz import (apply_security_headers as _authz_apply_security_headers,
                            enforce_server_auth as _authz_enforce_server_auth)
import intravia.events as _events
from intravia.geo import *  # noqa: F401,F403
from intravia.routing import *  # noqa: F401,F403
from intravia.faults import *  # noqa: F401,F403
from intravia.candidates import *  # noqa: F401,F403
from intravia.geo import (_aircraft_taxi_conflict, _location_center,
                          _parking_point, _parse_timestamp,
                          _point_in_geometry, _zone_geometry)
import intravia.geo as geo
import intravia.routing as _routing
import intravia.faults as _faults
from intravia.faults import classify_fault as _faults_classify_fault

# Мутабельные гео-глобалы живут только в intravia.geo: не держим в app.py
# устаревшие копии (apply_location_parking пересоздаёт их при старте).
for _stale in ('PARKING_COORDS', 'PARKING_META', 'MAP_OBJECTS',
               'AIRCRAFT_TAXI_RESERVATIONS', 'AIRCRAFT_TAXI_LOCK'):
    globals().pop(_stale, None)
del _stale


def classify_fault(raw):
    # Тесты и внешний код настраивают путь каталога через app.FAULT_CATALOG_PATH.
    _faults.FAULT_CATALOG_PATH = FAULT_CATALOG_PATH
    return _faults_classify_fault(raw)
from intravia.requests_logic import *  # noqa: F401,F403
from intravia.tracking import *  # noqa: F401,F403
from intravia.requests_logic import (
    _archive_requests_before_delete, _check_regulation_warning,
    _cleanup_inactive_route_payloads, _history_has_stage,
    _log_routing_event, _nearest_free_engineer,
    _recalc_all_active_routes, _recalc_route_for_request_id,
    _recalc_routes_for_engineer, _route_eta_min, _route_payload)
from intravia.tracking import (_agents_snapshot, _track_apply_position,
                               _track_process_point)

# ------------------------------------------------------------
# Конфигурация
# ------------------------------------------------------------

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)

# Без заданного SECRET_KEY подпись сессий была бы предсказуемой, поэтому
# генерируем случайный ключ на процесс и явно предупреждаем администратора.
_env_secret = os.environ.get('SECRET_KEY', '').strip()
if _env_secret:
    app.secret_key = _env_secret
else:
    app.secret_key = secrets.token_hex(32)
    print('[SECURITY] SECRET_KEY не задан — сгенерирован временный ключ. '
          'Сессии сбросятся при перезапуске.')

app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', '').strip() == '1'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)

if CORS_ORIGINS:
    CORS(app, resources={r"/api/*": {"origins": CORS_ORIGINS}}, supports_credentials=True)
else:
    print('[SECURITY] CORS выключен (same-origin). Разрешить домены: CORS_ORIGINS=https://a,https://b')

# Capacitor-обёртка мобильного приложения работает с origin https://localhost
# или capacitor://localhost. Разрешаем ей API и SSE-поток, не расширяя
# домен имитатора организаторов за пределы /api/v1/track.
CAPACITOR_ORIGINS = ['https://localhost', 'capacitor://localhost']
CORS(app, resources={
    r"/api/*": {"origins": CAPACITOR_ORIGINS},
    r"/api/v1/stream": {"origins": CAPACITOR_ORIGINS},
}, supports_credentials=True)

# Имитатор организаторов может отправлять точки напрямую (без CORS-прокси).
# Ручка /api/v1/track публичная по контракту, credentials ей не нужны.
CORS(app, resources={r"/api/v1/track": {
    "origins": ["https://itsim.geolaba.net"]}}, supports_credentials=False)


# ------------------------------------------------------------
# Realtime: in-process SSE bus (gunicorn/Flask, один процесс, много потоков)
# ------------------------------------------------------------

# Последние метрики точки (acc/spd) по агенту. Хранятся в памяти, чтобы не
# менять схему database.db; нужны для /api/v1/agents и SSE.

# Тестовые настройки заявки для демонстрации (не меняют схему БД):
# сокращение норматива, сдвиг таймера, принудительный спецтранспорт.






# ------------------------------------------------------------
# Журнал событий: append-only JSONL (без изменения схемы БД)
# ------------------------------------------------------------


@app.after_request
def _publish_mutation(response):
    """Любая изменяющая API-операция будит веб-клиенты, чтобы они перечитали состояние."""
    try:
        if request.method in ('POST', 'PUT', 'DELETE') and request.path.startswith('/api/'):
            if response.status_code < 500 and request.path not in ('/api/auth/login',):
                sse_publish('sync', {'path': request.path,
                                     'status': response.status_code,
                                     'at': _utc_now_iso()})
    except Exception as exc:
        print(f'[SSE] publish error: {exc}')
    return response


# ------------------------------------------------------------
# Пароли и ограничение попыток входа
# ------------------------------------------------------------
# ------------------------------------------------------------
# Серверная авторизация — обёртки, логика в intravia.authz
# ------------------------------------------------------------
@app.before_request
def _enforce_server_auth():
    return _authz_enforce_server_auth(AUTH_REQUIRED)


@app.before_request
def _regulation_progress_tick():
    """Throttled-тикер нормативов: работает без GPS. Не чаще раза в 10 секунд.

    Выбран вместо daemon-потока: поток стартовал бы при импорте и мешал pytest,
    а throttled before_request безопасен в тестах (проверки идемпотентны и
    пишут событие только один раз на заявку).
    """
    try:
        from intravia.requests_logic import maybe_run_regulation_tick
        maybe_run_regulation_tick()
    except Exception as exc:
        print(f'[REGULATION] tick: {exc}')


@app.after_request
def _security_headers(response):
    return _authz_apply_security_headers(response)


@app.after_request
def _access_debug_log(response):
    """Временный лог входов/мобильных запросов для диагностики телефона."""
    if os.environ.get('INTRAVIA_ACCESS_LOG', '').strip().lower() not in ('1', 'true', 'yes', 'on'):
        return response
    try:
        p = request.path or ''
        if p.startswith('/mobile') or p.startswith('/api/auth') or p.startswith('/api/v1/track'):
            ua = (request.headers.get('User-Agent') or '').replace('\n', ' ')[:200]
            line = (f"{datetime.now().strftime('%H:%M:%S')} {request.method} {p} "
                    f"{response.status_code} ip={request.remote_addr} ua={ua}\n")
            with open(os.path.join(BASE_DIR, 'logs', 'access_mobile.log'), 'a', encoding='utf-8') as fh:
                fh.write(line)
    except Exception:
        pass
    return response

# ------------------------------------------------------------
# Текущий аэропорт — системная настройка, общая для всех ролей
# ------------------------------------------------------------


@app.errorhandler(404)
def _json_404(e):
    if request.path.startswith('/api/'):
        return jsonify({
            'success': False,
            'error': 'Endpoint not found',
            'path': request.path,
            'method': request.method,
            'hint': 'Проверьте, что на сервере установлен актуальный app.py и Flask перезапущен.'
        }), 404
    return e


@app.errorhandler(405)
def _json_405(e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Method not allowed'}), 405
    return e


@app.errorhandler(500)
def _json_500(e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Internal server error'}), 500
    return e


# ------------------------------------------------------------
# LLM: Ollama
# ------------------------------------------------------------

_llm = None
if OpenAI is not None and LLM_ENABLED:
    try:
        _llm = OpenAI(api_key=OLLAMA_API_KEY, base_url=OLLAMA_BASE_URL)
        print(f'[LLM] Ollama: model={LLM_MODEL}, base={OLLAMA_BASE_URL}')
    except Exception as e:
        print(f'[LLM] init error: {e}')


# ------------------------------------------------------------
# Стоянки и объекты — из static/svo_all.geojson
# ------------------------------------------------------------










# ============================================================
# DB
# ============================================================




@app.route('/api/faults/classify', methods=['GET'])
def api_faults_classify():
    text = (request.args.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'Параметр text обязателен'}), 400
    conn = get_db()
    try:
        result = classify_mel_aog(text, request.args.get('ata'), conn)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    finally:
        conn.close()
    return jsonify(result)


@app.route('/api/faults/catalog', methods=['POST'])
def api_faults_catalog_add():
    """Добавляет новый тип неисправности в открытый каталог (без правки кода)."""
    data = request.get_json(silent=True) or {}
    code = str(data.get('code') or '').strip()
    name = str(data.get('name') or '').strip()
    if not code or not name:
        return jsonify({'success': False, 'error': 'code и name обязательны'}), 400
    synonyms = data.get('synonyms') or []
    if not isinstance(synonyms, list):
        synonyms = [str(synonyms)]
    entry = _faults._normalize_catalog_entry({
        'code': code, 'name': name,
        'ata': data.get('ata') or '',
        'skill': data.get('skill') or 'general',
        'synonyms': synonyms, 'custom': True,
    })
    if not entry:
        return jsonify({'success': False, 'error': 'Некорректная запись каталога'}), 400
    # Синхронизируем путь каталога с обёрткой classify_fault (тесты/стенды).
    _faults.FAULT_CATALOG_PATH = FAULT_CATALOG_PATH
    try:
        with _faults._FAULT_CATALOG_LOCK:
            catalog = _faults._load_fault_catalog()
            for idx, existing in enumerate(catalog):
                if existing.get('code') == entry['code']:
                    catalog[idx] = entry
                    break
            else:
                catalog.append(entry)
            _faults._write_fault_catalog(catalog)
    except Exception as exc:
        return jsonify({'success': False,
                        'error': f'Не удалось сохранить каталог: {exc}'}), 500
    append_event('fault_catalog_added', actor=_request_actor(),
                 code=entry['code'], name=entry['name'],
                 ata=entry['ata'], skill=entry['skill'])
    return jsonify({'success': True, 'item': entry})


@app.route('/api/faults/catalog', methods=['GET'])
def api_faults_catalog():
    ata = (request.args.get('ata') or '').strip()
    decision = (request.args.get('decision') or '').upper().strip()
    query = (request.args.get('q') or '').strip()
    try:
        limit = max(1, min(int(request.args.get('limit', 50)), 500))
    except (TypeError, ValueError):
        limit = 50
    where = []
    params = []
    if ata:
        where.append('ata=?')
        params.append(ata)
    if decision in ('AOG', 'MEL'):
        where.append('decision=?')
        params.append(decision)
    if query:
        where.append('lower(title) LIKE ?')
        params.append('%' + query.lower() + '%')
    sql = ('SELECT fim_code, ata, title, decision, mel_category, interval_text, '
           'conditions, reason, matched_rule, confidence, needs_review '
           'FROM fault_classification')
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY ata, fim_code LIMIT ?'
    params.append(limit)
    conn = get_db()
    try:
        rows = [dict(row) for row in conn.execute(sql, params)]
        total = conn.execute('SELECT COUNT(*) FROM fault_classification').fetchone()[0]
        counts = {row['decision']: row['n'] for row in conn.execute(
            'SELECT decision, COUNT(*) AS n FROM fault_classification GROUP BY decision')}
    except sqlite3.Error:
        conn.close()
        return jsonify({'error': 'Словарь не собран. Запустите tools/build_fim_mel_aog.py'}), 503
    conn.close()
    return jsonify({'total': total, 'counts': counts, 'items': rows})









# ============================================================
# Пересчёт ROUTE
# ============================================================


# ============================================================
# Страницы
# ============================================================
@app.route('/')
def index():
    return redirect(url_for('login_page'))


@app.route('/login.html')
def login_page():
    return render_template('login.html')


@app.route('/admin/dashboard.html')
def admin_dashboard():
    return render_template('admin/dashboard.html')


@app.route('/dispatcher/dashboard.html')
def dispatcher_dashboard():
    return render_template('dispatcher/dashboard.html', initial_airport=current_airport_payload()['airport'])


@app.route('/engineer/dashboard.html')
def engineer_dashboard():
    return render_template('engineer/dashboard.html', initial_airport=current_airport_payload()['airport'])


@app.route('/engineer/map.html')
def engineer_live_map():
    # Компьютерная страница только отображает GPS-метку с телефона.
    # Получение геолокации и отправка координат здесь намеренно отсутствуют.
    return render_template('engineer/map.html', initial_airport=current_airport_payload()['airport'])


@app.route('/pilot/dashboard.html')
def pilot_dashboard():
    return render_template('pilot/dashboard.html')


# ============================================================
# Мобильное приложение, раздаваемое с сервера
# ============================================================
@app.route('/mobile')
@app.route('/mobile/')
def mobile_entry():
    return redirect('/mobile/login.html')


@app.route('/mobile/<path:filename>')
def mobile_files(filename):
    return send_from_directory(MOBILE_DIR, filename)


# Совместимость с мобильным клиентом, который открывает <base>/m/engineer
@app.route('/m/engineer')
@app.route('/m/engineer/')
def mobile_engineer_entry():
    return redirect('/m/engineer/login.html')


@app.route('/m/engineer/<path:filename>')
def mobile_engineer_files(filename):
    return send_from_directory(MOBILE_DIR, filename)


@app.route('/mcc/dashboard.html')
def mcc_dashboard():
    return render_template('mcc/dashboard.html')


@app.route('/cpu/dashboard.html')
def cpu_dashboard():
    return render_template('cpu/dashboard.html')


# ============================================================
# Стоянки и объекты — API
# ============================================================
@app.route('/api/location', methods=['GET'])
def api_location():
    """География полигона: конфиг, объекты, стоянки, зоны, граница и дороги."""
    try:
        return jsonify(load_location_config(force=request.args.get('refresh') == '1'))
    except Exception as exc:
        print(f'[LOCATION] {exc}')
        return jsonify({'error': 'Конфигурация локации недоступна'}), 500


@app.route('/api/parking', methods=['GET'])
def api_parking():
    def sort_key(k):
        m = re.match(r'^(?:SZ-)?(\d+)', str(k))
        return (int(m.group(1)) if m else 9999, str(k))
    keys = sorted(geo.PARKING_META.keys(), key=sort_key)
    return jsonify([geo.PARKING_META[k] for k in keys])


@app.route('/api/map-objects', methods=['GET'])
def api_map_objects():
    return jsonify(geo.MAP_OBJECTS)


@app.route('/api/aircraft/taxi-reservations', methods=['POST'])
def api_aircraft_taxi_reservations():
    """Обновляет временные зоны занятости рулёжных маршрутов самолётами."""
    data = request.get_json(silent=True) or {}
    items = data.get('aircraft') or []
    if not isinstance(items, list) or len(items) > 100:
        return jsonify({'success': False, 'error': 'Некорректный список самолётов'}), 400
    now = time.time()
    updated = {}
    for item in items:
        if not isinstance(item, dict) or not item.get('id'):
            continue
        route = item.get('route') or []
        clean_route = []
        if isinstance(route, list):
            for point in route[:5000]:
                try:
                    lat, lng = float(point[0]), float(point[1])
                    if -90 <= lat <= 90 and -180 <= lng <= 180:
                        clean_route.append([lat, lng])
                except (TypeError, ValueError, IndexError):
                    continue
        if clean_route:
            updated[str(item['id'])] = {
                'aircraft': str(item.get('aircraft') or 'самолёт')[:80],
                'route': clean_route,
                'updated_at': now,
            }
    with geo.AIRCRAFT_TAXI_LOCK:
        geo.AIRCRAFT_TAXI_RESERVATIONS.clear()
        geo.AIRCRAFT_TAXI_RESERVATIONS.update(updated)
    return jsonify({'success': True, 'active': len(updated)})


@app.route('/api/map/object-positions', methods=['GET'])
def api_map_object_positions():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT object_type,object_id,lat,lng,updated_at
                   FROM map_object_positions
                   WHERE object_type != 'engineer_gps_heading'
                   ORDER BY object_type,object_id""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/restricted-zones', methods=['GET'])
def api_restricted_zones_legacy():
    return jsonify(geo.MAP_OBJECTS)


@app.route('/api/map-debug', methods=['GET'])
def api_map_debug():
    polys = sum(1 for o in geo.MAP_OBJECTS if not o.get('isLine'))
    lines = sum(1 for o in geo.MAP_OBJECTS if o.get('isLine'))
    return jsonify({
        'parking_count': len(geo.PARKING_COORDS),
        'object_count': len(geo.MAP_OBJECTS),
        'objects_polygons': polys,
        'objects_lines': lines,
        'sample_parking': list(geo.PARKING_META.values())[:3],
        'sample_objects': geo.MAP_OBJECTS[:2],
    })


# ============================================================
# Авторизация
# ============================================================
@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = (data.get('password') or '').strip()
    if not email or not password:
        return jsonify({'error': 'Заполните все поля'}), 400

    attempt_key = _login_key(email)
    if _login_blocked(attempt_key):
        return jsonify({'error': 'Слишком много попыток входа. Повторите через 5 минут'}), 429

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,email,name,role,avatar,password FROM users WHERE email=?",
                (email,))
    u = cur.fetchone()
    ok, needs_upgrade = (False, False)
    if u:
        ok, needs_upgrade = verify_password(u['password'], password)
    if not u or not ok:
        _login_record_fail(attempt_key)
        conn.close()
        return jsonify({'error': 'Неверный email или пароль'}), 401

    _login_clear(attempt_key)
    if needs_upgrade:
        # Прозрачная миграция: старый открытый пароль заменяется хэшем.
        cur.execute("UPDATE users SET password=? WHERE id=?",
                    (hash_password(password), u['id']))
    cur.execute("UPDATE users SET status='online' WHERE id=?", (u['id'],))
    conn.commit()
    extra = {}
    if u['role'] == 'pilot':
        cur.execute("SELECT aircraft, rank FROM pilots WHERE id=?", (u['id'],))
        p = cur.fetchone()
        if p:
            extra['aircraft'] = p['aircraft'] or ''
            extra['rank'] = p['rank'] or 'pilot'
    conn.close()

    session.permanent = True
    session['user_id'] = u['id']
    session['role'] = u['role']
    session['name'] = u['name']

    res = {'id': u['id'], 'email': u['email'], 'name': u['name'],
           'role': u['role'], 'avatar': u['avatar'] or '👤'}
    res.update(extra)
    return jsonify({'success': True, 'user': res})


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    conn = get_db()
    user_id = session.get('user_id')
    if user_id:
        try:
            conn.execute("UPDATE users SET status='offline' WHERE id=?", (user_id,))
            conn.commit()
        finally:
            conn.close()
    else:
        conn.close()
    session.clear()
    return jsonify({'success': True})


@app.route('/api/auth/session', methods=['GET'])
def auth_session():
    """Текущая серверная сессия — для проверки состояния авторизации."""
    if not session.get('user_id'):
        return jsonify({'authenticated': False, 'auth_required': AUTH_REQUIRED}), 401
    return jsonify({'authenticated': True,
                    'user': {'id': session.get('user_id'),
                             'role': session.get('role'),
                             'name': session.get('name')}})


# ============================================================
# Текущий аэропорт
# ============================================================
@app.route('/api/system/current-airport', methods=['GET'])
def get_current_airport():
    return jsonify(current_airport_payload())


@app.route('/api/system/current-airport', methods=['POST'])
def set_current_airport():
    data = request.get_json(silent=True) or {}
    code = str(data.get('airport') or '').strip().lower()
    if code not in AIRPORTS:
        return jsonify({'success': False, 'error': 'Неизвестный аэропорт'}), 400

    settings = load_system_settings()
    settings['airport'] = code
    settings['confirmed_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    confirmed_by = str(data.get('confirmed_by') or '').strip()
    if confirmed_by:
        settings['confirmed_by'] = confirmed_by[:120]
    save_system_settings(settings)

    payload = current_airport_payload()
    payload['success'] = True
    return jsonify(payload)


# ============================================================
# Users
# ============================================================
@app.route('/api/users', methods=['GET'])
def get_users():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,email,name,role,avatar,phone,shift,status FROM users ORDER BY role,name")
    res = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(res)


@app.route('/api/users', methods=['POST'])
def create_user():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    role = data.get('role', 'dispatcher')
    if not name or not email:
        return jsonify({'error': 'Имя и email обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email=?", (email,))
    if cur.fetchone():
        conn.close()
        return jsonify({'error': 'Email уже существует'}), 400
    uid = role + '_' + str(int(time.time() * 1000))
    avatar = {'dispatcher': '👨‍✈️', 'mcc': '👨‍💻'}.get(role, '👤')
    default_pwd = {'dispatcher': 'disp123', 'mcc': 'mcc123'}.get(role, 'user123')
    password = data.get('password') or default_pwd
    cur.execute("""INSERT INTO users (id,email,password,name,role,avatar,phone,shift,status)
                   VALUES (?,?,?,?,?,?,?,?,'offline')""",
                (uid, email, hash_password(password), name, role, avatar,
                 data.get('phone', ''), data.get('shift', 'day')))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'id': uid})


@app.route('/api/users/<user_id>', methods=['PUT'])
def update_user(user_id):
    data = request.get_json() or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, role FROM users WHERE id=?", (user_id,))
    u = cur.fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'Не найден'}), 404
    if data.get('password'):
        new_password = data.get('password')
        if not is_password_hash(new_password):
            new_password = hash_password(new_password)
        cur.execute("UPDATE users SET password=? WHERE id=?", (new_password, user_id))
    cur.execute("""UPDATE users SET name=?, phone=?, shift=?, status=? WHERE id=?""",
                (data.get('name'), data.get('phone', ''), data.get('shift', 'day'),
                 data.get('status', 'offline'), user_id))
    if data.get('name'):
        if u['role'] == 'pilot':
            cur.execute("UPDATE pilots SET name=? WHERE id=?", (data['name'], user_id))
        elif u['role'] == 'engineer':
            cur.execute("UPDATE engineers SET name=? WHERE id=?", (data['name'], user_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/users/<user_id>', methods=['DELETE'])
def delete_user(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# Operator reference-data REST integration (no Kupol integration)
# ============================================================
def _mel_import_api_authorized():
    expected = os.environ.get('INTRAVIA_IMPORT_API_KEY', '').strip()
    supplied = (request.headers.get('X-API-Key') or '').strip()
    return bool(expected) and supplied and supplied == expected


def _stable_mel_catalog_id(item):
    raw = '|'.join(str(item.get(k) or '').strip() for k in
                   ('aircraft_type', 'configuration', 'ata', 'item_ref', 'revision'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


@app.route('/api/v1/import/reference-data', methods=['POST'])
def import_reference_data_v1():
    """Import aircraft profiles and approved operator MEL catalogue records via REST."""
    if not os.environ.get('INTRAVIA_IMPORT_API_KEY', '').strip():
        return jsonify({'success': False, 'error': 'REST import is disabled: configure INTRAVIA_IMPORT_API_KEY on the server'}), 503
    if not _mel_import_api_authorized():
        return jsonify({'success': False, 'error': 'Invalid or missing X-API-Key'}), 401

    data = request.get_json(silent=True) or {}
    aircraft_rows = data.get('aircraft') or data.get('aircraft_registry') or []
    mel_rows = data.get('mel_items') or data.get('mel_catalog') or []
    if not isinstance(aircraft_rows, list) or not isinstance(mel_rows, list) or not (aircraft_rows or mel_rows):
        return jsonify({'success': False, 'error': 'Expected aircraft[] and/or mel_items[] arrays'}), 400
    if len(aircraft_rows) > 5000 or len(mel_rows) > 10000:
        return jsonify({'success': False, 'error': 'Import batch exceeds limits'}), 413

    conn = get_db()
    cur = conn.cursor()
    imported_aircraft = 0
    imported_mel = 0
    try:
        for a in aircraft_rows:
            if not isinstance(a, dict):
                raise ValueError('Each aircraft record must be an object')
            registration = str(a.get('registration') or '').strip().upper()
            aircraft_type = str(a.get('aircraft_type') or '').strip()
            configuration = str(a.get('configuration') or '').strip()
            if not registration or not aircraft_type:
                raise ValueError('Aircraft record requires registration and aircraft_type')
            cur.execute("""INSERT INTO aircraft_registry(registration, aircraft_type, configuration, updated_at)
                VALUES(?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(registration) DO UPDATE SET aircraft_type=excluded.aircraft_type,
                    configuration=excluded.configuration, updated_at=CURRENT_TIMESTAMP""",
                (registration, aircraft_type, configuration))
            imported_aircraft += 1

        allowed_modes = ('CALENDAR_DAYS', 'FLIGHT_HOURS', 'FLIGHT_CYCLES', 'CONDITION')
        allowed_start_rules = ('DEFECT_REPORTED_AT', 'ENGINEER_ASSESSMENT_AT')
        for item in mel_rows:
            if not isinstance(item, dict):
                raise ValueError('Each mel_items record must be an object')
            aircraft_type = str(item.get('aircraft_type') or '').strip()
            configuration = str(item.get('configuration') or '').strip()
            ata = str(item.get('ata') or '').strip()
            item_ref = str(item.get('item_ref') or '').strip()
            revision = str(item.get('revision') or '').strip()
            category = str(item.get('category') or '').strip().upper()
            description = str(item.get('description') or '').strip()
            interval = str(item.get('rectification_interval') or '').strip()
            mode = str(item.get('deadline_mode') or '').strip().upper()
            restrictions = str(item.get('restrictions') or '').strip()
            if not all((aircraft_type, ata, item_ref, revision, description, interval, restrictions)):
                raise ValueError('MEL item requires aircraft_type, ATA, item_ref, revision, description, rectification_interval and restrictions')
            o_required = bool(item.get('o_required'))
            m_required = bool(item.get('m_required'))
            o_procedure = str(item.get('o_procedure') or '').strip()
            m_procedure = str(item.get('m_procedure') or '').strip()
            if o_required and not o_procedure:
                raise ValueError('MEL item with o_required=true must include the complete o_procedure text')
            if m_required and not m_procedure:
                raise ValueError('MEL item with m_required=true must include the complete m_procedure text')
            if category not in ('A', 'B', 'C', 'D'):
                raise ValueError('MEL category must be A, B, C or D')
            if mode not in allowed_modes:
                raise ValueError('deadline_mode is required and must be CALENDAR_DAYS, FLIGHT_HOURS, FLIGHT_CYCLES or CONDITION')
            value = str(item.get('deadline_value') or '').strip()
            start_rule = str(item.get('deadline_start_rule') or '').strip().upper()
            condition = str(item.get('deadline_condition') or '').strip()
            if mode == 'CALENDAR_DAYS':
                try:
                    days = int(value)
                except (TypeError, ValueError):
                    raise ValueError('CALENDAR_DAYS requires a positive integer deadline_value')
                if days < 1 or start_rule not in allowed_start_rules:
                    raise ValueError('CALENDAR_DAYS requires deadline_start_rule DEFECT_REPORTED_AT or ENGINEER_ASSESSMENT_AT')
            elif mode in ('FLIGHT_HOURS', 'FLIGHT_CYCLES'):
                try:
                    if float(value) <= 0: raise ValueError()
                except (TypeError, ValueError):
                    raise ValueError(mode + ' requires positive deadline_value')
            elif not condition:
                raise ValueError('CONDITION requires deadline_condition')
            row_id = str(item.get('id') or _stable_mel_catalog_id({
                'aircraft_type': aircraft_type, 'configuration': configuration,
                'ata': ata, 'item_ref': item_ref, 'revision': revision
            })).strip()
            cur.execute("""INSERT INTO mel_catalog(
                id,aircraft_type,configuration,ata,item_ref,revision,category,description,
                rectification_interval,deadline_mode,deadline_value,deadline_start_rule,
                deadline_condition,restrictions,o_required,o_procedure,m_required,m_procedure,
                applicability_codes,source_document,active,imported_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(aircraft_type,configuration,ata,item_ref,revision) DO UPDATE SET
                    id=excluded.id, category=excluded.category, description=excluded.description,
                    rectification_interval=excluded.rectification_interval,
                    deadline_mode=excluded.deadline_mode, deadline_value=excluded.deadline_value,
                    deadline_start_rule=excluded.deadline_start_rule,
                    deadline_condition=excluded.deadline_condition, restrictions=excluded.restrictions,
                    o_required=excluded.o_required, o_procedure=excluded.o_procedure,
                    m_required=excluded.m_required, m_procedure=excluded.m_procedure,
                    applicability_codes=excluded.applicability_codes,
                    source_document=excluded.source_document, active=excluded.active,
                    imported_at=CURRENT_TIMESTAMP""", (
                row_id, aircraft_type, configuration, ata, item_ref, revision, category,
                description, interval, mode, value, start_rule, condition, restrictions,
                1 if o_required else 0, o_procedure,
                1 if m_required else 0, m_procedure,
                str(item.get('applicability_codes') or '').strip(),
                str(item.get('source_document') or '').strip(),
                1 if item.get('active', True) else 0))
            imported_mel += 1
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': str(exc)}), 400
    conn.close()
    return jsonify({'success': True, 'imported_aircraft': imported_aircraft,
                    'imported_mel_items': imported_mel})


@app.route('/api/mel/catalog', methods=['GET'])
def get_engineer_mel_catalog():
    request_id = str(request.args.get('request_id') or '').strip()
    ata = str(request.args.get('ata') or '').strip()
    search = str(request.args.get('q') or '').strip()
    if not request_id:
        return jsonify({'success': False, 'error': 'request_id is required'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.aircraft, ar.aircraft_type, ar.configuration
        FROM requests r LEFT JOIN aircraft_registry ar ON ar.registration=UPPER(r.aircraft)
        WHERE r.id=?""", (request_id,))
    profile = cur.fetchone()
    if not profile:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    aircraft_type = str(profile['aircraft_type'] or '').strip()
    configuration = str(profile['configuration'] or '').strip()
    if not aircraft_type:
        conn.close()
        return jsonify({'success': False, 'error': 'Для борта не загружен профиль типа/конфигурации. Импортируйте aircraft[] через REST API; выбор MEL пока заблокирован.'}), 409
    sql = """SELECT id, aircraft_type, configuration, ata, item_ref, revision, category,
        description, rectification_interval, deadline_mode, deadline_value, deadline_start_rule,
        deadline_condition, restrictions, o_required, o_procedure, m_required, m_procedure,
        applicability_codes, source_document
        FROM mel_catalog WHERE active=1 AND aircraft_type=?
          AND (configuration='' OR configuration=?)
          AND (o_required=0 OR TRIM(COALESCE(o_procedure,''))<>'')
          AND (m_required=0 OR TRIM(COALESCE(m_procedure,''))<>'')"""
    params = [aircraft_type, configuration]
    if ata:
        sql += ' AND ata=?'; params.append(ata)
    if search:
        sql += ' AND (item_ref LIKE ? OR description LIKE ? OR applicability_codes LIKE ?)'
        q = '%' + search + '%'; params.extend([q, q, q])
    sql += ' ORDER BY ata, item_ref LIMIT 500'
    cur.execute(sql, params)
    rows = [dict(x) for x in cur.fetchall()]
    conn.close()
    return jsonify({'success': True, 'aircraft': profile['aircraft'],
                    'aircraft_type': aircraft_type, 'configuration': configuration,
                    'items': rows})


# ============================================================
# MCC
# ============================================================
def _compute_recurring_map(conn):
    cur = conn.cursor()
    cur.execute("""SELECT aircraft, fault_type, COUNT(*) AS cnt, MAX(created_at) AS last_seen
                   FROM requests
                   WHERE aircraft IS NOT NULL AND aircraft!='' AND fault_type IS NOT NULL
                     AND status NOT IN ('completed','cancelled','flight_cancelled','draft','deleted')
                     AND created_at >= datetime('now','-30 days')
                   GROUP BY aircraft, fault_type""")
    out = {}
    for r in cur.fetchall():
        out[(str(r['aircraft']).strip(), str(r['fault_type']).strip())] = {
            'occurrences': r['cnt'] or 0, 'last_occurrence': r['last_seen']
        }
    return out


def sync_expired_mel(conn):
    """Move MEL cases past their operator-entered calendar deadline to DO_NOT_FLY.

    This covers both an activated MEL and one still awaiting final activation. No category-based
    default interval is calculated: due dates are stored exactly as entered from the approved MEL.
    Category A may be condition-based and have no date, in which case the condition needs manual control.
    """
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, mcc_mel_ref, mcc_due_date FROM requests
        WHERE mcc_status IN ('MEL_ACTIVE','MEL_READY','MEL') AND COALESCE(TRIM(mcc_due_date),'')!=''
          AND date('now') > date(mcc_due_date)""")
    expired = [dict(r) for r in cur.fetchall()]
    for row in expired:
        cur.execute("""UPDATE requests SET mcc_status='MEL_EXPIRED', status='mel_expired',
            routing_stage='pilot_notice', flight_status='DO_NOT_FLY',
            flight_status_reason=?, pilot_notice_at=CURRENT_TIMESTAMP
            WHERE id=? AND mcc_status IN ('MEL_ACTIVE','MEL_READY','MEL')""", (
                'Срок MEL ' + str(row.get('mcc_mel_ref') or '') + ' истёк. Полёты запрещены до устранения дефекта или допустимого оформления продления.',
                row['id']))
        if cur.rowcount:
            _log_routing_event(conn, row['id'], 'pilot_notice', 'Система',
                'Срок MEL истёк (' + str(row.get('mcc_due_date') or '') + '); установлен DO_NOT_FLY')
    if expired:
        conn.commit()
    return len(expired)


@app.route('/api/mcc/ata', methods=['POST'])
def mcc_set_ata():
    data = request.get_json() or {}
    rid = data.get('request_id')
    ata = str(data.get('ata') or '').strip()
    # Разрешаем только реальные главы ATA, которые представлены в интерфейсе MCC.
    valid_ata = {
        '00','01','02','03','04','05','06','07','08','09','10','11','12','20','21','22','23','24','25','26',
        '27','28','29','30','31','32','33','34','35','36','37','38','41','42','44','45','46','47','48','49','50',
        '51','52','53','54','55','56','57','60','61','62','63','64','65','66','67','70',
        '71','72','73','74','75','76','77','78','79','80','81','82','83','84'
    }
    if not rid:
        return jsonify({'success': False, 'error': 'request_id обязателен'}), 400
    if ata not in valid_ata:
        return jsonify({'success': False, 'error': 'Укажите действующую главу ATA из списка'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM requests WHERE id=?', (rid,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    cur.execute('UPDATE requests SET mcc_ata=? WHERE id=?', (ata, rid))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'ata': ata})


@app.route('/api/mcc/defects', methods=['GET'])
def get_mcc_defects():
    conn = get_db()
    sync_expired_mel(conn)
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, fault_type, description, parking_id, terminal,
        source, pilot_name, pilot_rank, pilot_audio_text, engineer_id, engineer_name,
        created_at, status, mcc_status, mcc_decision, mcc_comment, engineer_comment, mcc_ata,
        mcc_mel_choice, mcc_mel_ref, mcc_amm_verdict, mcc_proc_o, mcc_proc_m,
        mcc_proc_mh, mcc_mel_category, mcc_due_date, mcc_combined_check,
        mcc_mel_revision, mcc_mel_description, mcc_mel_restrictions, mcc_due_condition,
        mcc_proc_o_text, mcc_proc_m_text, cpu_mel_o_completed, cpu_mel_m_completed,
        mel_catalog_id, mel_assessed_by, mel_assessment_comment, mel_rectification_interval, mcc_mel_return_comment,
        mel_m_procedure_completed, mel_m_procedure_comment,
        mel_initial_engineer_report, mel_initial_engineer_report_at, mel_initial_assessed_by,
        mel_deadline_mode, mel_deadline_value, mel_deadline_start_rule,
        cpu_mel_applicability_confirmed, cpu_mel_o_comment, cpu_mel_m_comment,
        mcc_mel_reviewed_by, mcc_mel_reviewed_at, mcc_mel_review_comment,
        mcc_mel_combined_checked, mcc_mel_crew_briefed, mcc_mel_restrictions_verified, mcc_mel_repair_plan,
        cpu_mel_check_comment, cpu_mel_checked_at, cpu_mel_checked_by,
        cpu_mel_engineer_required, cpu_mel_m_comment,
        mcc_recurring_action, mcc_escalation, mcc_dce_name, mcc_kupol_ref,
        aog_since, etr, reported_by, severity, criticality,
        engineer_verdict, engineer_verdict_at, aog_action, inspection_assigned_at,
        flight_number, travel_time, using_car, best_car,
        crs_signature, crs_signed_at, crs_signed_by,
        routing_stage, routing_history, flight_status, flight_status_reason,
        engineer_condition, engineer_report_at, dispatcher_report_at, cpu_report_at, pilot_notice_at
        FROM requests WHERE status NOT IN ('cancelled','flight_cancelled','draft','deleted')
        ORDER BY created_at DESC""")
    rows = cur.fetchall()
    rec_map = _compute_recurring_map(conn)
    conn.close()
    out = []
    for r in rows:
        d = dict(r)

        # ── Подпись источника: пилот/голос → «КВС», остальное → «Инженер» ──
        src = (d.get('source') or '').strip().lower()
        if src in ('pilot', 'voice'):
            d['source_label'] = 'КВС'
        else:
            d['source_label'] = 'Инженер'

        d['reported_by'] = d.get('pilot_name') or d.get('engineer_name') or 'Pilot'
        d['mel_category'] = d.get('mcc_mel_category') or ''
        d['mel_ref'] = d.get('mcc_mel_ref') or ''
        d['mel_due_date'] = d.get('mcc_due_date') or ''
        d['engineer_verdict'] = d.get('engineer_verdict') or ''
        d['aog_action'] = d.get('aog_action') or ''
        d['crs_signed'] = bool(d.get('crs_signed_at'))
        try:
            d['routing_history'] = json.loads(d.get('routing_history') or '[]')
        except Exception:
            d['routing_history'] = []
        key = (str(d.get('aircraft') or '').strip(), str(d.get('fault_type') or '').strip())
        rec = rec_map.get(key, {'occurrences': 1, 'last_occurrence': d.get('created_at')})
        d['occurrences'] = rec['occurrences']
        d['recurring'] = rec['occurrences'] >= 3
        out.append(d)
    return jsonify(out)


@app.route('/api/mcc/start_mel_review', methods=['POST'])
def mcc_start_mel_review():
    # Deprecated by the engineer-led MEL workflow. Keep the route for old clients,
    # but refuse to let MCC manually set the item/category/deadline.
    return jsonify({'success': False,
                    'error': 'Пункт MEL, категорию и интервал определяет инженер по справочнику утверждённого MEL. MCC выполняет проверку и контроль.'}), 410


@app.route('/api/mcc/send_to_cpu', methods=['POST'])
def mcc_send_to_cpu():
    data = request.get_json() or {}
    rid = data.get('request_id')
    comment = (data.get('comment') or '').strip()

    if not rid:
        return jsonify({'success': False, 'error': 'request_id обязателен'}), 400
    if not comment:
        return jsonify({'success': False, 'error': 'Комментарий обязателен'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, aircraft, routing_stage, mcc_status FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    expired_mel_recovery = (row['routing_stage'] == 'pilot_notice' and row['mcc_status'] == 'MEL_EXPIRED')
    if row['routing_stage'] not in ('mcc', None, '') and not expired_mel_recovery:
        conn.close()
        return jsonify({'success': False,
                        'error': 'Заявка уже передана (stage=%s)' % row['routing_stage']}), 400

    event_note = ('Срок MEL истёк. MCC направил заявку в обычную ветку AOG/ремонта: ' if expired_mel_recovery else '') + comment
    log_prefix = '\n[→ЦПУ · MEL EXPIRED → AOG/TECH] ' if expired_mel_recovery else '\n[→ЦПУ · AOG/TECH] '
    cur.execute("""UPDATE requests SET
                   routing_stage='cpu', mcc_decision='AOG_DIAGNOSIS', mcc_mel_choice='',
                   mcc_comment=COALESCE(mcc_comment,'') || ?,
                   mcc_status='INSPECTION',
                   flight_status='DO_NOT_FLY',
                   flight_status_reason='Передано на техническую диагностику по ветке AOG/ремонт. До отдельного допуска MCC полёт запрещён.'
                   WHERE id=?""",
                (log_prefix + comment, rid))
    _log_routing_event(conn, rid, 'cpu', 'MCC', event_note)
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'cpu',
                    'expired_mel_recovery': expired_mel_recovery})


@app.route('/api/mcc/assign_inspection', methods=['POST'])
def mcc_assign_inspection_legacy():
    return mcc_send_to_cpu()


@app.route('/api/mcc/send_to_dispatcher', methods=['POST'])
def mcc_send_to_dispatcher_direct():
    """Дополнительная опция: MCC напрямую передаёт заявку диспетчеру, минуя ЦПУ.

    Основная цепочка MCC → ЦПУ → диспетчер не удаляется; это отдельный маршрут
    для случаев, когда координация ЦПУ не требуется.
    """
    data = request.get_json() or {}
    rid = data.get('request_id')
    comment = (data.get('comment') or '').strip()
    if not rid:
        return jsonify({'success': False, 'error': 'request_id обязателен'}), 400
    if not comment:
        return jsonify({'success': False, 'error': 'Комментарий обязателен'}), 400

    actor = _request_actor()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, aircraft, routing_stage FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] not in ('mcc', None, ''):
        conn.close()
        return jsonify({'success': False,
                        'error': 'Заявка уже передана (stage=%s)' % row['routing_stage']}), 400

    cur.execute("""UPDATE requests SET
                   routing_stage='dispatcher', status='queued',
                   mcc_decision='AOG_DIAGNOSIS', mcc_status='INSPECTION',
                   mcc_comment=COALESCE(mcc_comment,'') || ?,
                   flight_status='DO_NOT_FLY',
                   flight_status_reason='MCC передал заявку диспетчеру напрямую (без ЦПУ).'
                   WHERE id=?""",
                ('\n[MCC→Диспетчер · direct] ' + comment, rid))
    _log_routing_event(conn, rid, 'dispatcher', actor,
                       'MCC передал заявку диспетчеру напрямую: ' + comment,
                       channel='mcc_to_dispatcher')
    conn.commit()
    conn.close()
    append_event('status_changed', request_id=rid, status='queued',
                 routing_stage='dispatcher', actor=actor,
                 channel='mcc_to_dispatcher', note=comment)
    return jsonify({'success': True, 'request_id': rid,
                    'routing_stage': 'dispatcher', 'status': 'queued',
                    'actor': actor})


@app.route('/api/engineer/reports', methods=['GET'])
def engineer_past_reports():
    """История отчётов конкретного инженера; удалённые заявки в неё не попадают."""
    engineer_id = str(request.args.get('engineer_id') or '').strip()
    if not engineer_id:
        return jsonify({'success': False, 'error': 'Не указан engineer_id'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, parking_id, severity,
                   engineer_verdict, engineer_condition, engineer_comment,
                   engineer_report_at, created_at, status, mcc_status, mcc_decision,
                   routing_stage, mcc_mel_ref, mcc_ata, mcc_mel_category,
                   mcc_mel_revision, mel_rectification_interval
                   FROM requests
                   WHERE engineer_id=? AND engineer_report_at IS NOT NULL
                     AND TRIM(COALESCE(engineer_comment,'')) != ''
                   ORDER BY engineer_report_at DESC, created_at DESC""", (engineer_id,))
    reports = [dict(row) for row in cur.fetchall()]
    conn.close()
    return jsonify({'success': True, 'reports': reports})


@app.route('/api/engineer/submit_report', methods=['POST'])
def engineer_submit_report():
    """Инженер завершает диагностику и сразу передаёт отчёт в ЦПУ, не выпуская ВС."""
    data = request.get_json() or {}
    rid = data.get('request_id')
    body_engineer_id = str(data.get('engineer_id') or '')
    # Критерий 14: для инженера исполнитель берётся из сессии.
    if AUTH_REQUIRED and session.get('role') == 'engineer':
        session_engineer_id = str(session.get('user_id') or '')
        if body_engineer_id and body_engineer_id != session_engineer_id:
            append_event('role_denied', path=request.path, method=request.method,
                         role='engineer', reason='body_engineer_mismatch')
            return jsonify({'success': False,
                            'error': 'Инженер может отправить отчёт только по своей заявке'}), 403
        engineer_id = session_engineer_id
    else:
        engineer_id = body_engineer_id
    condition = (data.get('condition') or '').strip()
    category = (data.get('category') or '').strip()
    findings = (data.get('findings') or '').strip()
    report_text = (data.get('report_text') or '').strip()
    mel_item_ref = str(data.get('mel_item_ref') or '').strip()[:80]
    mel_ata = str(data.get('mel_ata') or '').strip()[:20]
    mel_category = str(data.get('mel_category') or '').strip()[:20]
    mel_revision = str(data.get('mel_revision') or '').strip()[:80]
    mel_interval_limit = str(data.get('mel_interval_limit') or '').strip()[:500]
    mel_catalog_id = ''
    mel_assessment_comment = ''
    mel_m_procedure_completed = bool(data.get('mel_m_procedure_completed'))
    mel_m_procedure_comment = str(data.get('mel_m_procedure_comment') or '').strip()
    try:
        duration = int(data.get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0
    if not rid or condition not in ('aog', 'repaired', 'mel_deferred'):
        return jsonify({'success': False, 'error': 'Выберите один из трёх результатов диагностики: AOG, MEL или «Устранено»'}), 400
    if condition == 'mel_deferred' and not mel_item_ref:
        return jsonify({'success': False, 'error': 'Укажите номер пункта MEL, например 27-2-1'}), 400
    if condition == 'mel_deferred' and not (mel_ata and mel_category and mel_revision and mel_interval_limit):
        return jsonify({'success': False, 'error': 'Заполните ATA, категорию, редакцию MEL и интервал / лимит'}), 400
    if not findings or not report_text:
        return jsonify({'success': False, 'error': 'Отчёт и описание обнаруженного обязательны'}), 400
    if duration < 1 or duration > 480:
        return jsonify({'success': False, 'error': 'Время диагностики должно быть от 1 до 480 минут'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, created_at, engineer_id, engineer_name, routing_stage, status, engineer_comment,
        mcc_mel_choice, mel_catalog_id, mcc_proc_m, mcc_proc_o, mcc_proc_m_text, mcc_proc_o_text, cpu_mel_applicability_confirmed,
        cpu_mel_o_completed, cpu_mel_o_comment, mel_rectification_interval FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'technician' or row['status'] in ('completed','cancelled','flight_cancelled','deleted'):
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка уже не находится у инженера'}), 409
    if row['status'] != 'in_progress':
        conn.close()
        return jsonify({'success': False,
                        'error': 'Отчёт можно отправить только после начала работ '
                                 '(статус in_progress)'}), 409
    if engineer_id and str(row['engineer_id'] or '') != engineer_id:
        conn.close()
        return jsonify({'success': False, 'error': 'Эта заявка назначена другому инженеру'}), 403

    # Инженер вручную указывает номер пункта MEL; операторский справочник не блокирует ввод.
    mel_values = {
        'mcc_mel_choice': '', 'mcc_ata': '', 'mcc_mel_ref': '', 'mcc_mel_category': '',
        'mcc_due_date': None, 'mcc_due_condition': '', 'mcc_mel_revision': '',
        'mcc_mel_description': '', 'mcc_mel_restrictions': '', 'mcc_proc_o': 0,
        'mcc_proc_m': 0, 'mcc_proc_o_text': '', 'mcc_proc_m_text': '',
        'mel_catalog_id': '', 'mel_assessed_by': '', 'mel_assessment_comment': '',
        'mel_rectification_interval': '', 'mel_deadline_mode': '', 'mel_deadline_value': '',
        'mel_deadline_start_rule': ''
    }
    preliminary_verdict = {'aog': 'AOG', 'mel_deferred': 'MEL', 'repaired': 'REPAIR'}[condition]
    is_mel_m_followup = bool(row['mcc_mel_choice'] == 'MEL' and row['mcc_proc_m'])
    if preliminary_verdict == 'MEL':
        if is_mel_m_followup and str(row['mcc_mel_ref'] or '').strip().casefold() != mel_item_ref.casefold():
            conn.close()
            return jsonify({'success': False, 'error': 'Для отдельной M-задачи нельзя менять исходный номер пункта MEL.'}), 409
        mel_m_maintenance_task = is_mel_m_followup
        if mel_m_maintenance_task and (not mel_m_procedure_completed or len(mel_m_procedure_comment) < 5):
            conn.close()
            return jsonify({'success': False, 'error': 'Для этой задачи обязательна M-процедура. Подтвердите выполнение и опишите работы.'}), 400
        if not mel_m_maintenance_task:
            mel_m_procedure_completed = False
            mel_m_procedure_comment = ''
        mel_values.update({
            'mcc_mel_choice': 'MEL', 'mcc_mel_ref': mel_item_ref,
            'mcc_ata': mel_ata, 'mcc_mel_category': mel_category,
            'mcc_mel_revision': mel_revision,
            'mel_rectification_interval': (str(row['mel_rectification_interval'] or '').strip()[:500] if mel_m_maintenance_task else mel_interval_limit),
            'mcc_proc_m': 1 if mel_m_maintenance_task else 0,
            'mcc_proc_m_text': str(row['mcc_proc_m_text'] or '') if mel_m_maintenance_task else '',
            'mcc_proc_o': int(row['mcc_proc_o'] or 0) if mel_m_maintenance_task else 0,
            'mcc_proc_o_text': str(row['mcc_proc_o_text'] or '') if mel_m_maintenance_task else '',
            'mel_assessed_by': row['engineer_name'] or engineer_id,
            'mel_m_procedure_completed': 1 if mel_m_procedure_completed else 0,
            'mel_m_procedure_comment': mel_m_procedure_comment
        })
    if preliminary_verdict != 'MEL':
        mel_m_procedure_completed = False
        mel_m_procedure_comment = ''

    preserve_cpu_mel_o = bool(preliminary_verdict == 'MEL' and row['mcc_mel_choice'] == 'MEL' and
        str(row['mcc_mel_ref'] or '').strip().casefold() == mel_item_ref.casefold() and row['cpu_mel_applicability_confirmed'])

    flight_reason = (
        'AOG установлен инженером: полёт запрещён. Отчёт передан диспетчеру.' if preliminary_verdict == 'AOG' else
        ('Инженер сопоставил дефект с пунктом MEL ' + str(mel_values['mcc_mel_ref']) +
         '. Условная эксплуатация не активна; ожидаются проверка диспетчером, ЦПУ и решение MCC.' if preliminary_verdict == 'MEL' else
         'Инженер завершил диагностику. Отчёт передан диспетчеру; полёт запрещён до решения MCC и отдельного допуска.')
    )
    # Отчёт сначала проходит диспетчера, затем ЦПУ и MCC: звено не пропускается.
    cur.execute("""UPDATE requests SET
        status='report_submitted', routing_stage='dispatcher_report',
        engineer_condition=?, engineer_verdict=?, engineer_verdict_at=CURRENT_TIMESTAMP,
        engineer_report_at=CURRENT_TIMESTAMP, cpu_report_at=NULL,
        engineer_comment=?,
        mcc_mel_choice=?, mcc_ata=?, mcc_mel_ref=?, mcc_mel_category=?, mcc_due_date=?,
        mcc_due_condition=?, mcc_mel_revision=?, mcc_mel_description=?, mcc_mel_restrictions=?,
        mcc_proc_o=?, mcc_proc_m=?, mcc_proc_o_text=?, mcc_proc_m_text=?,
        mel_catalog_id=?, mel_assessed_by=?, mel_assessment_comment=?, mel_rectification_interval=?,
        mel_m_procedure_completed=?, mel_m_procedure_comment=?,
        mel_deadline_mode=?, mel_deadline_value=?, mel_deadline_start_rule=?,
        mcc_status=CASE WHEN ?='MEL' THEN 'MEL_PENDING_CPU' ELSE 'INSPECTION' END,
        cpu_mel_o_completed=CASE WHEN ?=1 THEN cpu_mel_o_completed ELSE 0 END,
        cpu_mel_m_completed=0,
        cpu_mel_applicability_confirmed=CASE WHEN ?=1 THEN cpu_mel_applicability_confirmed ELSE 0 END,
        cpu_mel_checked_at=NULL, cpu_mel_checked_by='', cpu_mel_check_comment='',
        cpu_mel_o_comment=CASE WHEN ?=1 THEN cpu_mel_o_comment ELSE '' END, cpu_mel_m_comment='',
        flight_status='DO_NOT_FLY', flight_status_reason=?,
        aog_since=CASE WHEN ?='AOG' THEN COALESCE(aog_since,CURRENT_TIMESTAMP) ELSE aog_since END
        WHERE id=?""", (
        condition, preliminary_verdict, report_text,
        mel_values['mcc_mel_choice'], mel_values['mcc_ata'], mel_values['mcc_mel_ref'],
        mel_values['mcc_mel_category'], mel_values['mcc_due_date'], mel_values['mcc_due_condition'],
        mel_values['mcc_mel_revision'], mel_values['mcc_mel_description'], mel_values['mcc_mel_restrictions'],
        mel_values['mcc_proc_o'], mel_values['mcc_proc_m'], mel_values['mcc_proc_o_text'], mel_values['mcc_proc_m_text'],
        mel_values['mel_catalog_id'], mel_values['mel_assessed_by'], mel_values['mel_assessment_comment'],
        mel_values['mel_rectification_interval'], 1 if mel_m_procedure_completed else 0, mel_m_procedure_comment,
        mel_values['mel_deadline_mode'], mel_values['mel_deadline_value'],
        mel_values['mel_deadline_start_rule'], preliminary_verdict,
        1 if preserve_cpu_mel_o else 0, 1 if preserve_cpu_mel_o else 0,
        1 if preserve_cpu_mel_o else 0, flight_reason, preliminary_verdict, rid))
    if row['engineer_id']:
        release_engineers_without_active_requests(conn, [row['engineer_id']])
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    event_text = 'Техник установил результат «' + preliminary_verdict + '». Отчёт передан диспетчеру. Состояние: ' + condition
    if preliminary_verdict == 'MEL':
        event_text += ' · выбран пункт ' + str(mel_values['mcc_mel_ref']) + ' · Cat ' + str(mel_values['mcc_mel_category'])
    _log_routing_event(conn, rid, 'dispatcher_report', 'Инженер', event_text)
    conn.commit()
    conn.close()
    _recalc_route_for_request_id(rid, force=True)
    # Инженер освобождён: сразу запускаем автоподбор для ожидающих заявок.
    try:
        try_reassign_queued()
    except Exception as exc:
        print('[ENGINEER REPORT] queued auto-assign:', exc)
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'dispatcher_report',
                    'status': 'report_submitted', 'engineer_verdict': preliminary_verdict,
        'flight_status': 'DO_NOT_FLY', 'flight_status_reason': flight_reason})


@app.route('/api/engineer/verdict', methods=['POST'])
def engineer_verdict():
    """Deprecated: only the technician's submitted diagnosis report may set the verdict."""
    return jsonify({
        'success': False,
        'error': 'Технический вердикт устанавливает только техник при отправке отчёта: MEL, AOG или «Устранено».',
    }), 410


@app.route('/api/mcc/finalize', methods=['POST'])
def mcc_finalize():
    data = request.get_json() or {}
    rid = data.get('request_id')
    action = data.get('action')
    comment = (data.get('comment') or '').strip()
    if not rid or action not in ('CONFIRM_VERDICT', 'AOG_RECOVERY', 'MEL_DEFER', 'MEL_RETURN_CPU', 'CLOSE'):
        return jsonify({'success': False, 'error': 'Неверное действие'}), 400
    if not comment and action != 'CONFIRM_VERDICT':
        return jsonify({'success': False, 'error': 'Комментарий обязателен'}), 400
    if action == 'CONFIRM_VERDICT' and not comment:
        comment = 'Технический вердикт инженера подтверждён MCC.'

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, engineer_verdict, engineer_id, mcc_status, routing_stage, engineer_condition, engineer_report_at, engineer_comment, status, mcc_mel_choice, mcc_proc_o, mcc_proc_m, cpu_mel_o_completed, cpu_mel_m_completed, cpu_mel_checked_at, mcc_mel_ref, mcc_mel_category, mcc_due_date, mcc_due_condition FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    is_mel_workflow = row['mcc_mel_choice'] == 'MEL'
    requested_action = action
    if action == 'CONFIRM_VERDICT':
        if is_mel_workflow and row['engineer_verdict'] == 'MEL':
            action = 'MEL_DEFER'
        elif row['engineer_verdict'] == 'AOG' and row['engineer_condition'] == 'aog':
            action = 'AOG_RECOVERY'
        else:
            conn.close()
            return jsonify({'success': False, 'error': 'Подтверждение доступно только для вердикта MEL или AOG'}), 409
    if is_mel_workflow:
        if row['engineer_verdict'] != 'MEL' or row['engineer_condition'] != 'mel_deferred':
            conn.close()
            return jsonify({'success': False, 'error': 'Ветка MEL должна начинаться с заключения техника MEL и выбранного пункта утверждённого справочника'}), 409
        if row['mcc_status'] not in ('MEL_CPU_CHECKED', 'MEL_REVIEW') or row['routing_stage'] != 'mcc_mel_finalize' or not row['cpu_mel_checked_at']:
            conn.close()
            return jsonify({'success': False, 'error': 'Проверка применимости и процедур MEL в ЦПУ ещё не завершена'}), 409
        if action == 'MEL_DEFER':
            if row['engineer_verdict'] == 'AOG':
                conn.close()
                return jsonify({'success': False, 'error': 'Инженер установил AOG. MEL не может заменить AOG-ограничение'}), 409
            if row['mcc_proc_o'] and not row['cpu_mel_o_completed']:
                conn.close()
                return jsonify({'success': False, 'error': 'O-процедура MEL не подтверждена ЦПУ'}), 409
            if row['mcc_proc_m'] and not row['cpu_mel_m_completed']:
                conn.close()
                return jsonify({'success': False, 'error': 'M-процедура MEL не подтверждена после работы инженера и проверки ЦПУ'}), 409
            if not bool(data.get('mel_combined_checked')):
                conn.close()
                return jsonify({'success': False, 'error': 'MCC должно проверить совокупное влияние всех активных MEL/CDL'}), 400
            if not bool(data.get('mel_restrictions_verified')):
                conn.close()
                return jsonify({'success': False, 'error': 'MCC должно подтвердить ограничения и их применимость к конфигурации ВС'}), 400
            if not bool(data.get('mel_crew_briefed')):
                conn.close()
                return jsonify({'success': False, 'error': 'Зафиксируйте доведение MEL-ограничений до экипажа / ответственного канала'}), 400
            if len(str(data.get('mel_repair_plan') or '').strip()) < 5:
                conn.close()
                return jsonify({'success': False, 'error': 'Укажите план и ответственного за устранение дефекта'}), 400
        elif action == 'CLOSE':
            conn.close()
            return jsonify({'success': False, 'error': 'Для MEL выберите MEL_DEFER после выполнения процедур либо AOG_RECOVERY при AOG'}), 409
    else:
        if row['mcc_status'] != 'INSPECTION' or row['routing_stage'] != 'mcc_finalize':
            conn.close()
            return jsonify({'success': False, 'error': 'Заявка ещё не прошла обратную цепочку диспетчер → ЦПУ → MCC'}), 409
        if (not row['engineer_report_at'] or not row['engineer_verdict'] or
                row['engineer_condition'] not in ('aog', 'repaired') or
                row['status'] != 'report_submitted' or 'ОТЧЁТ О ДИАГНОСТИКЕ' not in (row['engineer_comment'] or '')):
            conn.close()
            return jsonify({'success': False, 'error': 'MCC не может финализировать заявку до получения полного отчёта инженера'}), 409

    # No Kupol integration is performed. Legacy field is intentionally left empty.
    kupol_ref = ''
    dce_name = data.get('dce_name') or 'DCE'
    mcc_reviewer = str(data.get('reviewer') or dce_name).strip()

    if action == 'MEL_RETURN_CPU':
        if not is_mel_workflow or row['routing_stage'] != 'mcc_mel_finalize' or row['mcc_status'] not in ('MEL_CPU_CHECKED', 'MEL_REVIEW'):
            conn.close()
            return jsonify({'success': False, 'error': 'Вернуть в ЦПУ можно только MEL-отчёт, ожидающий решения MCC'}), 409
        return_comment = 'не удалось принять решение'
        cur.execute("""UPDATE requests SET
            mcc_status='MEL_CPU_RECHECK', mcc_decision='MEL_RETURNED_TO_CPU',
            routing_stage='cpu_report', status='report_submitted',
            mcc_mel_return_comment=?, mcc_comment=COALESCE(mcc_comment,'') || ?,
            flight_status='DO_NOT_FLY',
            flight_status_reason='MCC вернул MEL-отчёт в ЦПУ на перепроверку. Полёт запрещён.'
            WHERE id=?""", (return_comment,
                '\n[MCC→ЦПУ · MEL перепроверка] ' + return_comment, rid))
        _log_routing_event(conn, rid, 'cpu_report', mcc_reviewer,
            'MEL возвращён в ЦПУ на перепроверку: ' + return_comment)
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'request_id': rid, 'action': action,
                        'mcc_status': 'MEL_CPU_RECHECK', 'routing_stage': 'cpu_report',
                        'comment': return_comment, 'flight_status': 'DO_NOT_FLY'})

    if action == 'AOG_RECOVERY':
        if row['engineer_verdict'] != 'AOG' or row['engineer_condition'] != 'aog':
            conn.close()
            return jsonify({'success': False, 'error': 'AOG Recovery доступен только если техник установил результат AOG'}), 400
        etr = (data.get('etr') or '').strip()
        aog_action = (data.get('aog_action') or '').strip()
        cur.execute("""UPDATE requests SET
            mcc_status='AOG_CONFIRMED', status='aog_waiting',
            mcc_decision='AOG_RECOVERY', mcc_mel_choice='',
            mcc_comment=COALESCE(mcc_comment,'') || ?,
            etr=?, aog_action=?, mcc_dce_name=?, mcc_kupol_ref=?,
            aog_since=COALESCE(aog_since,CURRENT_TIMESTAMP),
            routing_stage='pilot_notice', flight_status='DO_NOT_FLY',
            flight_status_reason='AOG: полёт запрещён до устранения дефекта и нового решения MCC',
            pilot_notice_at=CURRENT_TIMESTAMP
            WHERE id=?""",
            ('\n[MCC] AOG Recovery: ' + comment, etr, aog_action, dce_name, kupol_ref, rid))
        _log_routing_event(conn, rid, 'pilot_notice', 'MCC', 'Вердикт AOG подтверждён MCC. ETR=' + (etr or 'не указан; будет заполнен при планировании'))

    elif action == 'MEL_DEFER':
        if is_mel_workflow:
            # Initial MEL details and time limits were entered from the operator-approved MEL.
            # CPU has confirmed required O/M procedures; MCC makes the final operational decision.
            if row['engineer_verdict'] == 'AOG':
                conn.close()
                return jsonify({'success': False, 'error': 'AOG от инженера: требуется ветка AOG Recovery, а не MEL'}), 409
            cur.execute("""UPDATE requests SET
                mcc_status='MEL_READY', status='deferred', mcc_decision='MEL_DEFER',
                mcc_comment=COALESCE(mcc_comment,'') || ?,
                mcc_mel_reviewed_by=?, mcc_mel_reviewed_at=CURRENT_TIMESTAMP, mcc_mel_review_comment=?,
                mcc_mel_combined_checked=1, mcc_mel_crew_briefed=1, mcc_mel_restrictions_verified=1,
                mcc_mel_repair_plan=?, mcc_dce_name=?, mcc_kupol_ref='', routing_stage='pilot_notice',
                flight_status='DO_NOT_FLY',
                flight_status_reason='Пункт MEL проверен ЦПУ и MCC. Условная эксплуатация ещё не активирована: требуется отдельное решение MCC.',
                pilot_notice_at=CURRENT_TIMESTAMP
                WHERE id=?""", ('\n[MCC] MEL принято к оформлению: ' + comment, mcc_reviewer, comment,
                    str(data.get('mel_repair_plan') or '').strip(), dce_name, rid))
            _log_routing_event(conn, rid, 'pilot_notice', 'MCC',
                'Проверка MEL завершена. Ожидается отдельное включение условной эксплуатации по пункту ' + str(row['mcc_mel_ref'] or ''))
        else:
            conn.close()
            return jsonify({'success': False, 'error': 'MEL нельзя создать решением MCC. Техник должен установить результат MEL и выбрать пункт из утверждённого справочника.'}), 409

    else:  # CLOSE: только подтверждённое устранение / неподтверждённый дефект
        if row['engineer_condition'] != 'repaired' or row['engineer_verdict'] != 'REPAIR':
            conn.close()
            return jsonify({'success': False, 'error': 'Закрытие доступно только если техник указал результат «Устранено»'}), 400
        cur.execute("""UPDATE requests SET
            mcc_status='REPAIRED', status='completed', mcc_decision='CLOSE',
            mcc_comment=COALESCE(mcc_comment,'') || ?,
            mcc_dce_name=?, mcc_kupol_ref=?, routing_stage='pilot_notice',
            flight_status='DO_NOT_FLY',
            flight_status_reason='Работы отмечены как выполненные. Ожидается отдельное оформление допуска MCC',
            pilot_notice_at=CURRENT_TIMESTAMP
            WHERE id=?""",
            ('\n[MCC] Заключение о ремонте: ' + comment, dce_name, kupol_ref, rid))
        _log_routing_event(conn, rid, 'pilot_notice', 'MCC', 'Пилоту передано уведомление: полёт пока запрещён до оформления допуска')

    if row['engineer_id']:
        cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle' WHERE id=?", (row['engineer_id'],))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({
        'success': True, 'request_id': rid, 'action': requested_action,
        'mcc_status': {'AOG_RECOVERY': 'AOG_CONFIRMED', 'MEL_DEFER': 'MEL_READY', 'CLOSE': 'REPAIRED'}.get(action, action),
        'routing_stage': 'pilot_notice', 'flight_status': 'DO_NOT_FLY'
    })


@app.route('/api/mcc/aog_recovered', methods=['POST'])
def mcc_aog_recovered():
    """MCC records completion of AOG recovery; this does NOT authorize a flight."""
    data = request.get_json() or {}
    rid = data.get('request_id')
    comment = (data.get('comment') or '').strip()
    authorized_by = (data.get('authorized_by') or '').strip()
    if not rid or not comment or not authorized_by:
        return jsonify({'success': False, 'error': 'Укажите заявку, сотрудника MCC и описание выполненного восстановления'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, mcc_status, routing_stage FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['mcc_status'] != 'AOG_CONFIRMED' or row['routing_stage'] != 'pilot_notice':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не находится в активном AOG Recovery'}), 409
    cur.execute("""UPDATE requests SET mcc_status='REPAIRED', status='completed',
                   mcc_decision='AOG_RECOVERED', mcc_comment=COALESCE(mcc_comment,'') || ?,
                   routing_stage='pilot_notice', flight_status='DO_NOT_FLY',
                   flight_status_reason='AOG Recovery отмечен MCC как завершённый. Полёт запрещён до отдельного оформления допуска MCC',
                   pilot_notice_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                ('\n[MCC AOG RECOVERED] ' + comment + ' (' + authorized_by + ')', rid))
    _log_routing_event(conn, rid, 'pilot_notice', 'MCC', 'AOG Recovery завершён; пилоту оставлен запрет на вылет до отдельного допуска. ' + comment)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'pilot_notice', 'flight_status': 'DO_NOT_FLY', 'mcc_status': 'REPAIRED'})


@app.route('/api/mcc/aog_update', methods=['POST'])
def mcc_aog_update():
    data = request.get_json() or {}
    rid = data.get('request_id')
    etr = data.get('etr', '')
    aog_action = data.get('aog_action', '')
    if not rid:
        return jsonify({'success': False, 'error': 'Не указан request_id'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""UPDATE requests SET etr=?, aog_action=?
        WHERE id=? AND mcc_status='AOG_CONFIRMED'""", (etr, aog_action, rid))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid})


@app.route('/api/mcc/clear/<clear_type>', methods=['POST'])
def mcc_clear(clear_type):
    valid = ('queue', 'inspection', 'deferred', 'recurring', 'aog', 'history', 'all')
    if clear_type not in valid:
        return jsonify({'success': False, 'error': f'Unknown: {clear_type}'}), 400
    conn = get_db()
    cur = conn.cursor()
    try:
        deleted = 0
        delete_where = {
            'queue': "mcc_status='PENDING' AND status!='deleted'",
            'inspection': "mcc_status='INSPECTION' AND status!='deleted'",
            'deferred': "mcc_status IN ('MEL','MEL_REVIEW','MEL_READY','MEL_ACTIVE','MEL_EXPIRED') AND status!='deleted'",
            'aog': "mcc_status='AOG_CONFIRMED' AND status!='deleted'",
            'history': "mcc_status IN ('REPAIRED','RELEASED','CLOSED') AND status!='deleted'",
            'all': "status NOT IN ('draft','deleted')",
        }[clear_type]
        cur.execute(f"SELECT DISTINCT engineer_id FROM requests WHERE {delete_where} AND engineer_id IS NOT NULL")
        affected_engineers = [r['engineer_id'] for r in cur.fetchall()]
        deleted = _archive_requests_before_delete(cur, delete_where, reason='Очищено через MCC: ' + clear_type)
        release_engineers_without_active_requests(conn, affected_engineers)
        cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to IN (SELECT id FROM requests WHERE status='deleted')")
        conn.commit()
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        conn.rollback()
        print(f'[API] {request.path}: {e}')
        return jsonify({'success': False, 'error': 'Внутренняя ошибка сервера'}), 500
    finally:
        conn.close()


@app.route('/api/pilot/confirm/<rid>', methods=['POST'])
def confirm_pilot_request(rid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT aircraft, parking_id FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if not str(row['parking_id'] or '').strip():
        # Заявка создаётся на борт: стоянку определяем по координатам ВС.
        parking_id = _resolve_aircraft_parking(cur, row['aircraft'])
        if parking_id:
            cur.execute("UPDATE requests SET parking_id=? WHERE id=?", (parking_id, rid))
    cur.execute("UPDATE requests SET status='new', mcc_status='PENDING', routing_stage='mcc', flight_status='DO_NOT_FLY', flight_status_reason='Заявка о техническом дефекте зарегистрирована; полёт запрещён до решения MCC' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/pilot/sign_crs/<rid>', methods=['POST'])
@app.route('/api/requests/<rid>/pilot-signature', methods=['POST'])
def pilot_sign_crs(rid):
    """Сохраняет подпись пилота об ознакомлении с уже полученным вердиктом.

    Второй URL /api/requests/<id>/pilot-signature используется как основной:
    некоторые reverse-proxy/правила доступа блокируют старый URL /api/pilot/....
    Никакого отдельного имени/подтверждения от пилота не требуется.
    """
    data = request.get_json(silent=True) or {}
    signature = str(data.get('signature') or '').strip()
    signed_by = str(data.get('signed_by') or '').strip()

    if not signature.startswith('data:image/') or ',' not in signature:
        return jsonify({'success': False, 'error': 'Нарисуйте подпись в поле и повторите отправку'}), 400
    if len(signature) > 2_500_000:
        return jsonify({'success': False, 'error': 'Изображение подписи слишком большое'}), 413

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, pilot_name, engineer_verdict, engineer_condition,
            engineer_report_at, mcc_decision, routing_stage, crs_signed_at
            FROM requests WHERE id=?""", (rid,))
        row = cur.fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404

        # Разрешаем подпись, когда в заявке уже есть переданный результат.
        # Не привязываем сохранение к одному конкретному mcc_status: для
        # MEL/AOG/ремонта используются разные статусы.
        has_verdict = bool(row['engineer_verdict'] or row['engineer_condition'] or
                           row['engineer_report_at'] or row['mcc_decision'])
        if not has_verdict:
            return jsonify({'success': False, 'error': 'Сначала дождитесь итогового вердикта'}), 409
        if row['crs_signed_at']:
            return jsonify({'success': True, 'already_signed': True,
                            'message': 'Подпись уже сохранена'}), 200

        if not signed_by:
            signed_by = str(row['pilot_name'] or '').strip() or 'Пилот'

        cur.execute("""UPDATE requests
            SET crs_signature=?, crs_signed_at=CURRENT_TIMESTAMP, crs_signed_by=?
            WHERE id=? AND (crs_signed_at IS NULL OR crs_signed_at='')""",
            (signature, signed_by, rid))
        if cur.rowcount != 1:
            # Идемпотентность: повторный клик не превращается в ошибку.
            cur.execute("SELECT crs_signed_at, crs_signed_by FROM requests WHERE id=?", (rid,))
            saved = cur.fetchone()
            if saved and saved['crs_signed_at']:
                conn.commit()
                return jsonify({'success': True, 'already_signed': True,
                                'message': 'Подпись уже сохранена'}), 200
            return jsonify({'success': False, 'error': 'Не удалось сохранить подпись. Повторите попытку'}), 409

        try:
            _log_routing_event(conn, rid, row['routing_stage'] or 'pilot_notice', 'Пилот',
                'Пилот подписал подтверждение ознакомления с техническим вердиктом; допуск ВС не изменён')
        except Exception as log_error:
            # Ошибка журнала не должна отменять сохранение самой подписи.
            print(f'[PILOT SIGN] routing log warning for {rid}: {log_error}')
        conn.commit()
        return jsonify({'success': True, 'message': 'Подпись сохранена',
                        'signed_by': signed_by, 'flight_status_unchanged': True}), 200
    except Exception as exc:
        conn.rollback()
        print(f'[PILOT SIGN] save error for {rid}: {exc}')
        return jsonify({'success': False, 'error': 'Ошибка сохранения подписи. Проверьте журнал сервера'}), 500
    finally:
        conn.close()


@app.route('/api/mcc/authorize_release', methods=['POST'])
def mcc_authorize_release():
    """Отдельное действие MCC для снятия программного ограничения после проверки документов."""
    data = request.get_json() or {}
    rid = data.get('request_id')
    authorized_by = (data.get('authorized_by') or '').strip()
    comment = (data.get('comment') or '').strip()
    if not rid or not authorized_by or not comment:
        return jsonify({'success': False, 'error': 'Укажите заявку, ФИО уполномоченного сотрудника MCC и основание допуска'}), 400
    conn = get_db()
    cur = conn.cursor()
    sync_expired_mel(conn)
    cur.execute("SELECT id, mcc_status, routing_stage, flight_status, mcc_mel_ref, mcc_due_date, mcc_due_condition, mcc_mel_restrictions, mcc_proc_o, mcc_proc_m, cpu_mel_o_completed, cpu_mel_m_completed, mcc_mel_reviewed_by, mcc_mel_reviewed_at, mcc_mel_combined_checked, mcc_mel_crew_briefed, mcc_mel_restrictions_verified, mcc_mel_repair_plan FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'pilot_notice' or row['flight_status'] != 'DO_NOT_FLY' or row['mcc_status'] not in ('REPAIRED', 'MEL_READY', 'MEL'):
        conn.close()
        return jsonify({'success': False, 'error': 'Для этой заявки нет основания оформить допуск. AOG или незавершённый MEL остаётся ограничением.'}), 409

    if row['mcc_status'] in ('MEL_READY', 'MEL'):
        if row['mcc_due_date']:
            try:
                if datetime.strptime(str(row['mcc_due_date'])[:10], '%Y-%m-%d').date() < datetime.utcnow().date():
                    conn.close()
                    return jsonify({'success': False, 'error': 'Срок MEL истёк. Условный допуск запрещён до устранения дефекта или отдельного допустимого продления.'}), 409
            except ValueError:
                conn.close()
                return jsonify({'success': False, 'error': 'Некорректный срок MEL. Требуется проверка MCC.'}), 409
        if row['mcc_status'] == 'MEL_READY' and ((row['mcc_proc_o'] and not row['cpu_mel_o_completed']) or (row['mcc_proc_m'] and not row['cpu_mel_m_completed'])):
            conn.close()
            return jsonify({'success': False, 'error': 'Не все процедуры MEL подтверждены ЦПУ.'}), 409
        if row['mcc_status'] == 'MEL_READY':
            if (not row['mcc_mel_reviewed_by'] or not row['mcc_mel_reviewed_at'] or
                    not row['mcc_mel_combined_checked'] or not row['mcc_mel_crew_briefed'] or
                    not row['mcc_mel_restrictions_verified'] or not row['mcc_mel_repair_plan']):
                conn.close()
                return jsonify({'success': False, 'error': 'Не завершён контроль MCC: совокупное влияние, ограничения, информирование экипажа и план устранения.'}), 409
        flight_reason = 'MEL ' + str(row['mcc_mel_ref'] or '') + ' активен. Эксплуатация возможна только при соблюдении утверждённых ограничений: ' + str(row['mcc_mel_restrictions'] or '')
        cur.execute("""UPDATE requests SET flight_status='MEL_ACTIVE', flight_status_reason=?,
            mcc_status='MEL_ACTIVE', routing_stage='mel_active', status='deferred'
            WHERE id=?""", (flight_reason, rid))
        _log_routing_event(conn, rid, 'mel_active', 'MCC', 'Условная эксплуатация по MEL активирована уполномоченным сотрудником ' + authorized_by + '. ' + comment)
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'request_id': rid, 'flight_status': 'MEL_ACTIVE', 'routing_stage': 'mel_active', 'mcc_status': 'MEL_ACTIVE'})

    cur.execute("""UPDATE requests SET flight_status='RELEASED', flight_status_reason=?,
                   mcc_status='RELEASED', routing_stage='released', status='completed'
                   WHERE id=?""", ('Допуск MCC: ' + comment + ' (' + authorized_by + ')', rid))
    _log_routing_event(conn, rid, 'released', 'MCC', 'Программное ограничение снято. Уполномоченный: ' + authorized_by + '. ' + comment)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'flight_status': 'RELEASED', 'routing_stage': 'released'})

@app.route('/api/cpu/mel_queue', methods=['GET'])
def cpu_mel_queue():
    conn = get_db()
    sync_expired_mel(conn)
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description, parking_id,
        mcc_ata, mcc_mel_ref, mcc_mel_category, mcc_due_date, mcc_due_condition,
        mcc_mel_revision, mcc_mel_description, mcc_mel_restrictions,
        mcc_proc_o, mcc_proc_m, mcc_proc_o_text, mcc_proc_m_text,
        mcc_comment, cpu_mel_o_completed, cpu_mel_m_completed,
        cpu_mel_check_comment, created_at, routing_stage, mcc_status
        FROM requests WHERE routing_stage='cpu_mel_review' AND mcc_status='MEL_REVIEW'
          AND status NOT IN ('cancelled','flight_cancelled','completed','draft','deleted')
        ORDER BY created_at ASC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/cpu/mel_return_to_mcc', methods=['POST'])
def cpu_mel_return_to_mcc():
    """Return a MEL item to MCC when CPU cannot confirm applicability/procedures."""
    data = request.get_json() or {}
    rid = data.get('request_id')
    reason = str(data.get('reason') or '').strip()
    reviewer = str(data.get('reviewer') or '').strip() or 'ЦПУ'
    if not rid or not reason:
        return jsonify({'success': False, 'error': 'Укажите заявку и причину возврата MEL в MCC'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, mcc_status, routing_stage, mcc_mel_ref FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['mcc_status'] != 'MEL_REVIEW' or row['routing_stage'] != 'cpu_mel_review':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка больше не ожидает первичной проверки MEL в ЦПУ'}), 409
    cur.execute("""UPDATE requests SET mcc_status='PENDING', mcc_decision='MEL_RETURNED',
        mcc_mel_choice='', status='open', routing_stage='mcc',
        mcc_comment=COALESCE(mcc_comment,'') || ?,
        flight_status='DO_NOT_FLY',
        flight_status_reason='ЦПУ вернуло MEL в MCC: требуется повторная оценка применимости. Полёт запрещён.'
        WHERE id=?""", ('\n[ЦПУ→MCC: возврат MEL] ' + reviewer + ': ' + reason, rid))
    _log_routing_event(conn, rid, 'mcc', reviewer,
        'MEL ' + str(row['mcc_mel_ref'] or '') + ' возвращён на повторную оценку: ' + reason)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'mcc', 'mcc_status': 'PENDING'})


@app.route('/api/cpu/mel_review', methods=['POST'])
def cpu_mel_review():
    data = request.get_json() or {}
    rid = data.get('request_id')
    o_completed = bool(data.get('o_completed'))
    comment = str(data.get('comment') or '').strip()
    reviewer = str(data.get('reviewer') or '').strip() or 'ЦПУ'
    if not rid or not comment:
        return jsonify({'success': False, 'error': 'Укажите заявку и комментарий проверки ЦПУ'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, mcc_proc_o, mcc_proc_m, routing_stage,
        mcc_status, mcc_mel_ref FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'cpu_mel_review' or row['mcc_status'] != 'MEL_REVIEW':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не находится в очереди MEL ЦПУ'}), 409
    if row['mcc_proc_o'] and not o_completed:
        conn.close()
        return jsonify({'success': False, 'error': 'Подтвердите выполнение O-процедуры или верните заявку на уточнение MCC'}), 400

    if row['mcc_proc_m']:
        cur.execute("""UPDATE requests SET cpu_mel_o_completed=?, cpu_mel_engineer_required=1,
            cpu_mel_check_comment=?, cpu_mel_checked_by=?, cpu_mel_checked_at=CURRENT_TIMESTAMP,
            status='queued', routing_stage='dispatcher',
            mcc_comment=COALESCE(mcc_comment,'') || ?
            WHERE id=?""", (1 if (o_completed or not row['mcc_proc_o']) else 0,
                comment, reviewer, '\n[ЦПУ MEL] O-процедура проверена. Требуется инженер для M-процедуры: ' + comment, rid))
        _log_routing_event(conn, rid, 'dispatcher', 'ЦПУ',
            'MEL ' + str(row['mcc_mel_ref'] or '') + ': O-процедура проверена; направлено диспетчеру для выполнения M-процедуры инженером.')
        conn.commit()
        conn.close()
        try:
            try_reassign_queued()
        except Exception as exc:
            print('[CPU MEL] auto assign:', exc)
        return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'dispatcher',
                        'engineer_required': True, 'message': 'Заявка передана диспетчеру для выполнения M-процедуры'})

    cur.execute("""UPDATE requests SET cpu_mel_o_completed=?, cpu_mel_m_completed=1,
        cpu_mel_engineer_required=0, cpu_mel_check_comment=?, cpu_mel_checked_by=?,
        cpu_mel_checked_at=CURRENT_TIMESTAMP, routing_stage='mcc_mel_finalize',
        status='mel_review', mcc_comment=COALESCE(mcc_comment,'') || ?
        WHERE id=?""", (1 if (o_completed or not row['mcc_proc_o']) else 0,
            comment, reviewer, '\n[ЦПУ MEL проверено] ' + comment, rid))
    _log_routing_event(conn, rid, 'mcc_mel_finalize', 'ЦПУ',
        'Проверка MEL завершена; передано MCC для итогового решения.')
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'mcc_mel_finalize',
                    'engineer_required': False})


@app.route('/api/cpu/queue', methods=['GET'])
def cpu_queue():
    """Заявки, ожидающие решения ЦПУ."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description,
                   parking_id, terminal, source, pilot_name, pilot_rank,
                   mcc_status, mcc_decision, mcc_comment, routing_history,
                   created_at, severity, criticality, engineer_comment, flight_status, flight_status_reason
                   FROM requests
                   WHERE routing_stage='cpu'
                     AND status NOT IN ('cancelled','flight_cancelled','completed','draft','deleted')
                   ORDER BY created_at ASC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        try:
            r['routing_history'] = json.loads(r['routing_history'] or '[]')
        except Exception:
            r['routing_history'] = []
    return jsonify(rows)


@app.route('/api/cpu/send_to_dispatcher', methods=['POST'])
def cpu_send_to_dispatcher():
    """ЦПУ передаёт заявку диспетчеру; инженер автоматически подбирается
    по специализации и минимальному времени прибытия. Заявка сразу появляется
    в активных заявках диспетчера. Если подходящих свободных инженеров нет,
    она остаётся там в статусе ожидания и назначается автоматически позже."""
    data = request.get_json() or {}
    rid = data.get('request_id')
    comment = (data.get('comment') or '').strip()
    if not rid:
        return jsonify({'success': False, 'error': 'request_id обязателен'}), 400
    if not comment:
        return jsonify({'success': False, 'error': 'Комментарий обязателен'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, parking_id, fault_type,
                          routing_stage, source
                   FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'cpu':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не находится в очереди ЦПУ'}), 409

    aircraft = row['aircraft'] or 'ВС'
    parking_id = str(row['parking_id'] or '')
    fault = row['fault_type'] or 'other'
    target = _parking_point(parking_id) or _location_center()

    # Передаём заявку в диспетчерскую зону, сохраняя исходный source='pilot'.
    # Это необходимо, чтобы пилот продолжал видеть именно свою заявку.
    cur.execute("""UPDATE requests SET routing_stage='dispatcher',
                   mcc_comment=COALESCE(mcc_comment,'') || ?
                   WHERE id=?""", ('\n[ЦПУ→Диспетчер] ' + comment, rid))
    _log_routing_event(conn, rid, 'dispatcher', 'ЦПУ', comment)

    # Штатная цепочка: MCC/ЦПУ передаёт заявку диспетчеру, инженера
    # назначает только диспетчер. Автоназначение на этом шаге запрещено.
    cur.execute("""UPDATE requests SET status='queued',
                   routing_stage='dispatcher',
                   delay_started_at=COALESCE(delay_started_at,CURRENT_TIMESTAMP),
                   delay_reason='Ожидает назначения диспетчером'
                   WHERE id=?""", (rid,))
    _log_routing_event(conn, rid, 'dispatcher', 'ЦПУ',
                       'Передано диспетчеру; требуется назначение инженера')
    conn.commit()
    conn.close()
    return jsonify({
        'success': True, 'request_id': rid, 'routing_stage': 'dispatcher',
        'assigned': False, 'queued': True,
        'message': 'Заявка передана диспетчеру и ожидает назначения инженера'
    })


@app.route('/api/dispatcher/report_queue', methods=['GET'])
def dispatcher_report_queue():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description, parking_id,
                   engineer_id, engineer_name, engineer_condition, engineer_verdict,
                   engineer_comment, engineer_report_at, created_at, status, criticality,
                   flight_status, flight_status_reason, routing_history
                   FROM requests WHERE routing_stage='dispatcher_report'
                     AND engineer_report_at IS NOT NULL
                     AND engineer_verdict IN ('AOG','REPAIR')
                     AND engineer_condition IN ('aog','repaired')
                     AND status='report_submitted'
                   ORDER BY engineer_report_at ASC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        try: r['routing_history'] = json.loads(r.get('routing_history') or '[]')
        except Exception: r['routing_history'] = []
    return jsonify(rows)


@app.route('/api/dispatcher/forward_report_to_cpu', methods=['POST'])
def dispatcher_forward_report_to_cpu():
    data = request.get_json() or {}
    rid = data.get('request_id')
    comment = (data.get('comment') or '').strip()
    if not rid or not comment:
        return jsonify({'success': False, 'error': 'request_id и комментарий обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, routing_stage, engineer_report_at, engineer_verdict, engineer_condition, status FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'dispatcher_report':
        conn.close()
        return jsonify({'success': False, 'error': 'Отчёт не ожидает обработки диспетчером'}), 409
    if (not row['engineer_report_at'] or not row['engineer_verdict'] or
            row['engineer_condition'] not in ('aog', 'repaired') or
            row['status'] != 'report_submitted'):
        conn.close()
        return jsonify({'success': False, 'error': 'Нельзя передать заявку дальше: инженер ещё не отправил полный отчёт'}), 409
    cur.execute("""UPDATE requests SET routing_stage='cpu_report', dispatcher_report_at=CURRENT_TIMESTAMP,
                   mcc_comment=COALESCE(mcc_comment,'') || ? WHERE id=?""",
                ('\n[Диспетчер→ЦПУ] ' + comment, rid))
    _log_routing_event(conn, rid, 'cpu_report', 'Диспетчер', comment)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'cpu_report'})


@app.route('/api/cpu/report_queue', methods=['GET'])
def cpu_report_queue():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description, parking_id,
                   engineer_id, engineer_name, engineer_condition, engineer_verdict,
                   engineer_comment, engineer_report_at, dispatcher_report_at, cpu_report_at, created_at,
                   status, criticality, flight_status, flight_status_reason, routing_history,
                   mcc_mel_choice, mcc_ata, mcc_mel_ref, mcc_mel_category, mcc_due_date, mcc_due_condition,
                   mcc_mel_revision, mcc_mel_description, mcc_mel_restrictions,
                   mcc_proc_o, mcc_proc_m, mcc_proc_o_text, mcc_proc_m_text,
                   mel_catalog_id, mel_assessed_by, mel_assessment_comment, mel_rectification_interval,
                   mel_m_procedure_completed, mel_m_procedure_comment,
                   mel_initial_engineer_report, mel_initial_engineer_report_at, mel_initial_assessed_by,
                   mel_deadline_mode, mel_deadline_value, mel_deadline_start_rule,
                   mcc_proc_m_text, cpu_mel_o_completed, cpu_mel_m_completed,
                   cpu_mel_engineer_required, cpu_mel_applicability_confirmed,
                   cpu_mel_o_comment, cpu_mel_m_comment, cpu_mel_checked_by, cpu_mel_checked_at,
                   mcc_mel_return_comment
                   FROM requests WHERE routing_stage='cpu_report'
                     AND engineer_report_at IS NOT NULL
                     AND engineer_verdict IN ('AOG','REPAIR','MEL')
                     AND engineer_condition IN ('aog','repaired','mel_deferred')
                     AND status='report_submitted'
                   ORDER BY COALESCE(cpu_report_at, engineer_report_at) ASC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        try: r['routing_history'] = json.loads(r.get('routing_history') or '[]')
        except Exception: r['routing_history'] = []
    return jsonify(rows)


@app.route('/api/cpu/prepare_m_procedure', methods=['POST'])
def cpu_prepare_m_procedure():
    """CPU validates a technician-selected MEL item and routes required M-work to an engineer."""
    data = request.get_json() or {}
    rid = str(data.get('request_id') or '').strip()
    comment = str(data.get('comment') or '').strip()
    reviewer = str(data.get('reviewer') or '').strip() or 'ЦПУ'
    applicable = bool(data.get('mel_applicability_confirmed'))
    o_completed = bool(data.get('o_completed'))
    o_comment = str(data.get('o_comment') or '').strip()
    if not rid or len(comment) < 5:
        return jsonify({'success': False, 'error': 'Укажите заявку и комментарий проверки ЦПУ'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, routing_stage, status, engineer_verdict, engineer_condition,
        mcc_mel_choice, mel_catalog_id, mcc_mel_ref, mcc_proc_o, mcc_proc_m,
        mel_m_procedure_completed, engineer_comment, engineer_report_at, engineer_name
        FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'cpu_report' or row['status'] != 'report_submitted' or row['engineer_verdict'] != 'MEL' or row['engineer_condition'] != 'mel_deferred' or row['mcc_mel_choice'] != 'MEL':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не является техническим заключением MEL в очереди ЦПУ'}), 409
    if not row['mel_catalog_id'] or not row['mcc_mel_ref']:
        conn.close()
        return jsonify({'success': False, 'error': 'Нет выбранного пункта из утверждённого MEL'}), 409
    if not row['mcc_proc_m']:
        conn.close()
        return jsonify({'success': False, 'error': 'Для этого пункта M-процедура не требуется. Используйте «Передать MCC»'}), 409
    if row['mel_m_procedure_completed']:
        conn.close()
        return jsonify({'success': False, 'error': 'M-процедура уже отмечена техником как выполненная. Передайте отчёт на финальную проверку ЦПУ.'}), 409
    if not applicable:
        conn.close()
        return jsonify({'success': False, 'error': 'Подтвердите применимость MEL к типу и конфигурации борта'}), 400
    if row['mcc_proc_o'] and (not o_completed or len(o_comment) < 5):
        conn.close()
        return jsonify({'success': False, 'error': 'Сначала подтвердите O-процедуру и укажите результат её проверки'}), 400
    initial_engineer_report = str(row['engineer_comment'] or '')
    if initial_engineer_report.startswith('ROUTE:'):
        route_payload = initial_engineer_report[6:]
        try:
            _route_data, _route_end = json.JSONDecoder().raw_decode(route_payload)
            initial_engineer_report = route_payload[_route_end:].lstrip()
        except Exception:
            pass

    cur.execute("""UPDATE requests SET status='queued', routing_stage='dispatcher',
        mcc_status='MEL_MAINTENANCE_REQUIRED', cpu_mel_applicability_confirmed=1,
        cpu_mel_o_completed=?, cpu_mel_m_completed=0, cpu_mel_engineer_required=1,
        cpu_mel_checked_by=?, cpu_mel_checked_at=NULL, cpu_mel_check_comment=?,
        cpu_mel_o_comment=?, cpu_mel_m_comment='',
        mel_initial_engineer_report=CASE WHEN COALESCE(TRIM(mel_initial_engineer_report),'')='' THEN ? ELSE mel_initial_engineer_report END,
        mel_initial_engineer_report_at=CASE WHEN mel_initial_engineer_report_at IS NULL THEN ? ELSE mel_initial_engineer_report_at END,
        mel_initial_assessed_by=CASE WHEN COALESCE(TRIM(mel_initial_assessed_by),'')='' THEN ? ELSE mel_initial_assessed_by END,
        engineer_id=NULL, engineer_name=NULL, inspection_assigned_at=NULL, rejected_by='',
        mcc_comment=COALESCE(mcc_comment,'') || ?
        WHERE id=?""", (
        1 if (o_completed or not row['mcc_proc_o']) else 0, reviewer, comment, o_comment,
        initial_engineer_report, row['engineer_report_at'], row['engineer_name'] or '',
        '\n[ЦПУ→Диспетчер MEL M-процедура] ' + comment +
        ((' · O-процедура: ' + o_comment) if o_comment else ''), rid))
    _log_routing_event(conn, rid, 'dispatcher', reviewer,
        'Применимость MEL проверена; требуется назначить инженера для M-процедуры по пункту ' + str(row['mcc_mel_ref']))
    conn.commit()
    conn.close()
    try:
        try_reassign_queued()
    except Exception as exc:
        print('[CPU MEL M PROCEDURE] auto-assign:', exc)
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'dispatcher',
                    'mcc_status': 'MEL_MAINTENANCE_REQUIRED', 'engineer_required': True})


@app.route('/api/cpu/forward_report_to_mcc', methods=['POST'])
def cpu_forward_report_to_mcc():
    data = request.get_json() or {}
    rid = data.get('request_id')
    comment = (data.get('comment') or '').strip()
    if not rid or not comment:
        return jsonify({'success': False, 'error': 'request_id и комментарий обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, routing_stage, mcc_status, mcc_mel_choice, mcc_proc_o, mcc_proc_m,
        engineer_report_at, engineer_verdict, engineer_condition, status,
        mel_catalog_id, mcc_mel_ref, mcc_mel_category,
        mel_m_procedure_completed, mel_m_procedure_comment
        FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'cpu_report':
        conn.close()
        return jsonify({'success': False, 'error': 'Отчёт не ожидает обработки ЦПУ'}), 409
    if (not row['engineer_report_at'] or not row['engineer_verdict'] or
            row['engineer_condition'] not in ('aog','repaired','mel_deferred') or
            row['status'] != 'report_submitted'):
        conn.close()
        return jsonify({'success': False, 'error': 'Нельзя передать заявку в MCC: отчёт инженера ещё не отправлен'}), 409

    reviewer = str(data.get('reviewer') or '').strip() or 'ЦПУ'
    if row['engineer_verdict'] == 'MEL':
        applicable = bool(data.get('mel_applicability_confirmed'))
        o_completed = bool(data.get('mel_o_completed'))
        m_completed = bool(data.get('mel_m_completed'))
        o_comment = str(data.get('mel_o_comment') or '').strip()
        m_comment = str(data.get('mel_m_comment') or '').strip()
        if not row['mcc_mel_ref']:
            conn.close()
            return jsonify({'success': False, 'error': 'В отчёте MEL не указан номер пункта'}), 409
        if row['mcc_proc_m'] and (not row['mel_m_procedure_completed'] or len(str(row['mel_m_procedure_comment'] or '').strip()) < 5):
            conn.close()
            return jsonify({'success': False, 'error': 'В отчёте техника нет подтверждения выполнения обязательной M-процедуры'}), 409
        if not applicable:
            conn.close()
            return jsonify({'success': False, 'error': 'ЦПУ должно подтвердить применимость пункта к борту и конфигурации'}), 400
        if row['mcc_proc_o'] and (not o_completed or not o_comment):
            conn.close()
            return jsonify({'success': False, 'error': 'Подтвердите O-процедуру и добавьте комментарий ЦПУ'}), 400
        if row['mcc_proc_m'] and (not m_completed or not m_comment):
            conn.close()
            return jsonify({'success': False, 'error': 'Подтвердите M-процедуру по отчёту инженера и добавьте комментарий ЦПУ'}), 400
        cur.execute("""UPDATE requests SET routing_stage='mcc_mel_finalize', cpu_report_at=CURRENT_TIMESTAMP,
            mcc_status='MEL_CPU_CHECKED', cpu_mel_applicability_confirmed=1,
            cpu_mel_o_completed=?, cpu_mel_m_completed=?, cpu_mel_checked_by=?,
            cpu_mel_checked_at=CURRENT_TIMESTAMP, cpu_mel_check_comment=?, mcc_mel_return_comment='',
            cpu_mel_o_comment=?, cpu_mel_m_comment=?,
            mcc_comment=COALESCE(mcc_comment,'') || ?
            WHERE id=?""", (
            1 if (o_completed or not row['mcc_proc_o']) else 0,
            1 if (m_completed or not row['mcc_proc_m']) else 0,
            reviewer, comment, o_comment, m_comment,
            '\n[ЦПУ→MCC MEL] ' + comment +
              ((' · O-процедура: ' + o_comment) if o_comment else '') +
              ((' · M-процедура: ' + m_comment) if m_comment else ''), rid))
        _log_routing_event(conn, rid, 'mcc_mel_finalize', 'ЦПУ',
            'Применимость MEL и необходимые процедуры проверены; передано MCC на контроль и решение.')
        next_stage = 'mcc_mel_finalize'
    else:
        cur.execute("""UPDATE requests SET routing_stage='mcc_finalize', cpu_report_at=CURRENT_TIMESTAMP,
                       mcc_comment=COALESCE(mcc_comment,'') || ?, mcc_status='INSPECTION'
                       WHERE id=?""", ('\n[ЦПУ→MCC] ' + comment, rid))
        _log_routing_event(conn, rid, 'mcc_finalize', 'ЦПУ', comment)
        next_stage = 'mcc_finalize'
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': next_stage,
                    'mcc_status': 'MEL_CPU_CHECKED' if next_stage == 'mcc_mel_finalize' else 'INSPECTION'})


@app.route('/api/mcc/mel_return_to_engineer', methods=['POST'])
def mcc_mel_return_to_engineer():
    data = request.get_json() or {}
    rid = str(data.get('request_id') or '').strip()
    reason = str(data.get('reason') or '').strip()
    if not rid or not reason:
        return jsonify({'success': False, 'error': 'Укажите заявку и причину возврата инженеру'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, mcc_status, routing_stage, engineer_id FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['mcc_status'] != 'MEL_CPU_CHECKED' or row['routing_stage'] != 'mcc_mel_finalize':
        conn.close()
        return jsonify({'success': False, 'error': 'Возврат возможен только после проверки MEL в ЦПУ и до решения MCC'}), 409
    if row['engineer_id']:
        cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?", (row['engineer_id'],))
    cur.execute("""UPDATE requests SET mcc_status='MEL_RETURNED', mcc_decision='MEL_RETURNED',
        mcc_comment=COALESCE(mcc_comment,'') || ?, status='queued', routing_stage='dispatcher',
        engineer_id=NULL, engineer_name=NULL, inspection_assigned_at=NULL,
        engineer_verdict='', engineer_verdict_at=NULL, engineer_condition='', engineer_report_at=NULL,
        mel_catalog_id='', mel_assessed_by='', mel_assessment_comment='',
        mel_m_procedure_completed=0, mel_m_procedure_comment='',
        mel_initial_engineer_report='', mel_initial_engineer_report_at=NULL, mel_initial_assessed_by='',
        mcc_mel_choice='', mcc_ata='', mcc_mel_ref='', mcc_mel_category='', mcc_due_date=NULL,
        mcc_due_condition='', mcc_mel_revision='', mcc_mel_description='', mcc_mel_restrictions='',
        mcc_proc_o=0, mcc_proc_m=0, mcc_proc_o_text='', mcc_proc_m_text='',
        cpu_mel_applicability_confirmed=0, cpu_mel_o_completed=0, cpu_mel_m_completed=0,
        cpu_mel_checked_at=NULL, cpu_mel_checked_by='', cpu_mel_check_comment='',
        flight_status='DO_NOT_FLY',
        flight_status_reason='MEL возвращён инженеру на повторную оценку. Условная эксплуатация не активна.'
        WHERE id=?""", ('\n[MCC→Инженер: возврат MEL] ' + reason, rid))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    _log_routing_event(conn, rid, 'dispatcher', 'MCC', 'MEL возвращён на повторную техническую оценку: ' + reason)
    conn.commit()
    conn.close()
    try:
        try_reassign_queued()
    except Exception as exc:
        print('[MEL RETURN] auto assign:', exc)
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'dispatcher'})


@app.route('/api/mcc/mel_mark_expired', methods=['POST'])
def mcc_mark_mel_expired():
    data = request.get_json() or {}
    rid = str(data.get('request_id') or '').strip()
    reason = str(data.get('reason') or '').strip()
    actor = str(data.get('actor') or '').strip()
    if not rid or not reason or not actor:
        return jsonify({'success': False, 'error': 'Укажите заявку, сотрудника MCC и основание фиксации достижения MEL-лимита'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, mcc_status, mel_deadline_mode, mcc_mel_ref FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['mcc_status'] != 'MEL_ACTIVE' or row['mel_deadline_mode'] == 'CALENDAR_DAYS':
        conn.close()
        return jsonify({'success': False, 'error': 'Ручная фиксация доступна только для активных MEL с некалендарным лимитом'}), 409
    cur.execute("""UPDATE requests SET mcc_status='MEL_EXPIRED', status='mel_expired',
        routing_stage='pilot_notice', flight_status='DO_NOT_FLY',
        flight_status_reason=?, pilot_notice_at=CURRENT_TIMESTAMP,
        mcc_comment=COALESCE(mcc_comment,'') || ? WHERE id=?""",
        ('Достигнут / превышен лимит MEL ' + str(row['mcc_mel_ref'] or '') + '. Полёт запрещён.',
         '\n[MCC: MEL limit reached] ' + actor + ': ' + reason, rid))
    _log_routing_event(conn, rid, 'pilot_notice', actor, 'Достигнут лимит MEL; установлен DO_NOT_FLY. ' + reason)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'mcc_status': 'MEL_EXPIRED', 'flight_status': 'DO_NOT_FLY'})


# ============================================================
# Dispatcher — ручное назначение (fallback)
# ============================================================
@app.route('/api/dispatcher/queue', methods=['GET'])
def dispatcher_queue():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description,
                   parking_id, terminal, source, pilot_name, pilot_rank,
                   mcc_status, mcc_decision, mcc_comment, routing_history,
                   created_at, severity, criticality, flight_status, flight_status_reason
                   FROM requests
                   WHERE routing_stage='dispatcher'
                     AND status NOT IN ('cancelled','flight_cancelled','completed','draft','deleted')
                   ORDER BY created_at ASC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        try:
            r['routing_history'] = json.loads(r['routing_history'] or '[]')
        except Exception:
            r['routing_history'] = []
    return jsonify(rows)


@app.route('/api/dispatcher/assign', methods=['POST'])
def dispatcher_assign():
    data = request.get_json() or {}
    rid = data.get('request_id')
    eng_id = data.get('engineer_id')
    comment = (data.get('comment') or '').strip()
    if not rid or not eng_id or not comment:
        return jsonify({'success': False, 'error': 'Заполните все поля'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, parking_id, fault_type, routing_stage,
                          engineer_id
                   FROM requests WHERE id=?""", (rid,))
    req = cur.fetchone()
    if not req:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if req['routing_stage'] != 'dispatcher':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не у диспетчера'}), 400

    cur.execute("SELECT id, name, device_id, status FROM engineers WHERE id=?", (eng_id,))
    eng = cur.fetchone()
    if not eng:
        conn.close()
        return jsonify({'success': False, 'error': 'Техник не найден'}), 404
    if (eng['status'] or 'free') != 'free':
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер уже занят или недоступен'}), 409
    cur.execute("SELECT id FROM requests WHERE engineer_id=? AND status NOT IN ('completed','cancelled','flight_cancelled','deleted') AND routing_stage='technician'", (eng_id,))
    if cur.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'У инженера уже есть активная заявка'}), 409

    cur.execute("""UPDATE requests SET
                   engineer_id=?, engineer_name=?, travel_time=0, using_car=0,
                   walk_to_car=0, car_time=0, best_car=NULL, engineer_comment='',
                   status='assigned', routing_stage='technician',
                   mcc_status=CASE WHEN mcc_mel_choice='MEL' THEN 'MEL_REVIEW' ELSE 'INSPECTION' END,
                   inspection_assigned_at=NULL,
                   mcc_comment=COALESCE(mcc_comment,'') || ?
                   WHERE id=?""",
                (eng_id, eng['name'],
                 '\n[Диспетчер→Техник] ' + comment + ' — ' + eng['name'], rid))
    cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                ('ВС ' + (req['aircraft'] or ''), eng_id))
    _log_routing_event(conn, rid, 'technician', 'Диспетчер',
                       f'Назначен {eng["name"]}: {comment}')
    conn.commit()
    conn.close()
    # Маршрут-превью появляется сразу после назначения, до ответа инженера.
    _recalc_route_for_request_id(rid, force=True)
    forget_candidates(rid)
    append_event('assigned', request_id=rid, engineer_id=eng_id,
                 engineer=eng['device_id'],
                 previous_engineer=req['engineer_id'],
                 actor=_request_actor(),
                 reassigned=bool(req['engineer_id']))

    return jsonify({
        'success': True, 'request_id': rid,
        'engineer_id': eng_id, 'engineer_name': eng['name'],
        'travel_time': 0, 'awaiting_acceptance': True,
    })


@app.route('/api/dispatcher/candidates/<rid>', methods=['GET'])
def dispatcher_candidates(rid):
    """Таблица кандидатов для объяснимости назначения (критерий 17)."""
    cached = get_remembered_candidates(rid)
    if cached:
        return jsonify(cached)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, fault_type, severity, parking_id
                       FROM requests WHERE id=?""", (rid,))
        row = cur.fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
        target = _parking_point(row['parking_id']) or _location_center()
        started = time.perf_counter()
        table = build_candidate_table(conn, row['fault_type'] or 'other',
                                      target[0], target[1],
                                      severity=row['severity'] or 'medium',
                                      regulation_minutes=_request_regulation_minutes(rid))
        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        remember_candidates(rid, table, duration_ms)
        conn.commit()
        return jsonify(get_remembered_candidates(rid))
    finally:
        conn.close()


@app.route('/api/dispatcher/priorities', methods=['GET'])
def dispatcher_priorities():
    """Приоритеты диспетчера на нашем build_candidate_table (не копия main).

    awaiting_parts исключён; critical идёт первым; время построения — duration_ms.
    """
    started = time.perf_counter()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, aircraft, fault_type, severity, criticality,
                              parking_id, created_at
                       FROM requests
                       WHERE routing_stage='dispatcher'
                         AND status NOT IN ('cancelled','flight_cancelled',
                                            'completed','draft','deleted',
                                            'awaiting_parts')
                       ORDER BY CASE lower(COALESCE(criticality,''))
                                  WHEN 'critical' THEN 0 ELSE 1 END,
                                created_at ASC
                       LIMIT 30""")
        rows = [dict(row) for row in cur.fetchall()]
        items = []
        for row in rows:
            item_started = time.perf_counter()
            target = _parking_point(row['parking_id']) or _location_center()
            table = build_candidate_table(
                conn, row['fault_type'] or 'other', target[0], target[1],
                severity=row['severity'] or 'medium',
                regulation_minutes=_request_regulation_minutes(row['id']))
            eligible = [c for c in table if c.get('eligible')]
            items.append({
                'request_id': row['id'],
                'aircraft': row['aircraft'],
                'fault_type': row['fault_type'],
                'severity': row['severity'],
                'criticality': row['criticality'],
                'parking_id': row['parking_id'],
                'created_at': row['created_at'],
                'candidates_total': len(table),
                'candidates_eligible': len(eligible),
                'best_candidate': eligible[0] if eligible else None,
                'duration_ms': round((time.perf_counter() - item_started) * 1000.0, 2),
            })
    finally:
        conn.close()
    return jsonify({
        'success': True,
        'count': len(items),
        'duration_ms': round((time.perf_counter() - started) * 1000.0, 2),
        'requests': items,
    })


@app.route('/api/dispatcher/assigned', methods=['GET'])
def dispatcher_assigned():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.id, r.aircraft, r.fault_type, r.parking_id,
                   r.status, r.engineer_id, r.engineer_name, r.travel_time,
                   r.inspection_assigned_at, r.created_at, e.device_id, e.specialization
                   FROM requests r
                   LEFT JOIN engineers e ON e.id = r.engineer_id
                   WHERE r.routing_stage='technician'
                     AND r.status NOT IN ('completed','cancelled','flight_cancelled','draft','deleted')
                   ORDER BY r.created_at DESC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


# ============================================================
# Engineers
# ============================================================
@app.route('/api/dispatcher/requests/<rid>/route', methods=['GET'])
def dispatcher_request_route(rid):
    """Return the route preview immediately after assignment. It is visible
    while the engineer decides; actual travel starts only after acceptance.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.id, r.engineer_id, r.parking_id, r.engineer_comment,
                          r.status, r.routing_stage, e.task_step AS engineer_task_step,
                          e.status AS engineer_status, e.coords_lat AS engineer_lat,
                          e.coords_lng AS engineer_lng
                   FROM requests r LEFT JOIN engineers e ON e.id=r.engineer_id
                   WHERE r.id=?""", (rid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'technician':
        return jsonify({'success': False, 'error': 'Заявка уже вышла из этапа инженера'}), 409
    if (row['status'] in ('completed', 'cancelled', 'flight_cancelled', 'draft', 'report_submitted') or
            row['routing_stage'] != 'technician' or row['engineer_status'] != 'busy' or
            row['engineer_lat'] is None or row['engineer_lng'] is None or
            row['engineer_task_step'] not in ('pending', 'accepted', 'in_car', 'arrived', 'diagnosing')):
        return jsonify({'success': False, 'error': 'Маршрут доступен только назначенному инженеру с активной заявкой',
                        'awaiting_acceptance': False}), 409

    # Rebuild on demand if route data is missing or malformed.
    comment = row['engineer_comment'] or ''
    if not comment.startswith('ROUTE:'):
        _recalc_route_for_request_id(rid, force=True)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT engineer_comment, travel_time FROM requests WHERE id=?", (rid,))
        fresh = cur.fetchone()
        conn.close()
        comment = (fresh['engineer_comment'] or '') if fresh else ''

    if not comment.startswith('ROUTE:'):
        return jsonify({'success': False, 'error': 'Не удалось рассчитать маршрут'}), 422
    raw = comment[6:]
    depth = 0
    end = -1
    for i, ch in enumerate(raw):
        if ch == '{': depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return jsonify({'success': False, 'error': 'Данные маршрута повреждены'}), 422
    try:
        route = json.loads(raw[:end])
    except Exception:
        return jsonify({'success': False, 'error': 'Не удалось прочитать данные маршрута'}), 422
    return jsonify({'success': True, 'request_id': rid, 'engineer_id': row['engineer_id'],
                    'route': route})


@app.route('/api/engineers', methods=['GET'])
def get_engineers():
    conn = get_db()
    sync_engineer_availability(conn)
    conn.commit()
    cur = conn.cursor()
    cur.execute("""SELECT e.id,e.name,e.device_id,e.specialization,
                   COALESCE(gps.lat,p.lat,e.coords_lat) AS coords_lat,
                   COALESCE(gps.lng,p.lng,e.coords_lng) AS coords_lng,
                   gps.lat AS gps_lat,gps.lng AS gps_lng,
                   gps_heading.lat AS gps_heading,gps.updated_at AS gps_updated_at,
                   e.status,e.task,e.phone,e.task_step,e.task_started_at,
                   a.at_id,a.grade,a.scope,a.skill,a.scope_desc,
                   COALESCE(a.is_live,0) AS is_live
                   FROM engineers e
                   LEFT JOIN engineer_assignment a ON a.engineer_id=e.id
                   LEFT JOIN map_object_positions p
                     ON p.object_type='engineer' AND p.object_id=e.id
                   LEFT JOIN map_object_positions gps
                     ON gps.object_type='engineer_gps' AND gps.object_id=e.id
                   LEFT JOIN map_object_positions gps_heading
                     ON gps_heading.object_type='engineer_gps_heading' AND gps_heading.object_id=e.id""")
    rows = cur.fetchall()
    conn.close()
    res = []
    for r in rows:
        d = dict(r)
        num = get_engineer_number_from_device(d.get('device_id'))
        d['number'] = num
        d['device_id'] = normalize_device_id(d.get('device_id'), num)
        # на случай, если колонка ещё NULL (старые записи)
        if not d.get('task_step'):
            d['task_step'] = 'idle'
        res.append(d)
    res.sort(key=lambda x: x.get('number') or 0)
    return jsonify(res)


@app.route('/api/engineers/me', methods=['GET'])
def get_my_engineer():
    eid = request.args.get('id')
    if not eid:
        return jsonify({'error': 'Не указан id'}), 400
    return get_engineer(eid)


@app.route('/api/engineers/<eng_id>', methods=['GET'])
def get_engineer(eng_id):
    conn = get_db()
    sync_engineer_availability(conn)
    conn.commit()
    cur = conn.cursor()
    cur.execute("""SELECT e.id,e.name,e.device_id,e.specialization,
                   COALESCE(gps.lat,p.lat,e.coords_lat) AS coords_lat,
                   COALESCE(gps.lng,p.lng,e.coords_lng) AS coords_lng,
                   gps.lat AS gps_lat,gps.lng AS gps_lng,
                   gps_heading.lat AS gps_heading,gps.updated_at AS gps_updated_at,
                   e.status,e.task,e.phone,e.task_step,e.task_started_at,
                   a.at_id,a.grade,a.scope,a.skill,a.scope_desc,
                   COALESCE(a.is_live,0) AS is_live
                   FROM engineers e
                   LEFT JOIN engineer_assignment a ON a.engineer_id=e.id
                   LEFT JOIN map_object_positions p
                     ON p.object_type='engineer' AND p.object_id=e.id
                   LEFT JOIN map_object_positions gps
                     ON gps.object_type='engineer_gps' AND gps.object_id=e.id
                   LEFT JOIN map_object_positions gps_heading
                     ON gps_heading.object_type='engineer_gps_heading' AND gps_heading.object_id=e.id
                   WHERE e.id=?""", (eng_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Не найден'}), 404
    d = dict(row)
    num = get_engineer_number_from_device(d.get('device_id'))
    d['number'] = num
    d['device_id'] = normalize_device_id(d.get('device_id'), num)
    if not d.get('task_step'):
        d['task_step'] = 'idle'
    return jsonify(d)


@app.route('/api/engineers', methods=['POST'])
def create_engineer():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    if not name or not email:
        return jsonify({'error': 'Имя и email обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email=?", (email,))
    if cur.fetchone():
        conn.close()
        return jsonify({'error': 'Email уже существует'}), 400
    num = next_engineer_number(conn)
    eid = f'eng_{num}'
    dev = f'EN-{num:03d}'
    lat, lng = _location_center()
    cur.execute("""INSERT INTO engineers (id,name,device_id,specialization,coords_lat,coords_lng,status,phone,task_step)
                   VALUES (?,?,?,?,?,?,?,?,'idle')""",
                (eid, name, dev, data.get('specialization', 'general'),
                 lat, lng, data.get('status', 'free'), data.get('phone', '')))
    cur.execute("""INSERT INTO users (id,email,password,name,role,avatar,status)
                   VALUES (?,?,?,?,'engineer','🔧','offline')""",
                (eid, email, hash_password('eng123'), name))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'id': eid, 'device_id': dev, 'number': num})


@app.route('/api/engineers/<eng_id>', methods=['PUT'])
def update_engineer(eng_id):
    data = request.get_json() or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM engineers WHERE id=?", (eng_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'error': 'Не найден'}), 404
    cur.execute("""UPDATE engineers SET name=?, specialization=?, status=?, task=?, phone=?
                   WHERE id=?""",
                (data.get('name'), data.get('specialization', 'general'),
                 data.get('status', 'free'), data.get('task'),
                 data.get('phone', ''), eng_id))
    if data.get('name'):
        cur.execute("UPDATE users SET name=? WHERE id=?", (data['name'], eng_id))
    conn.commit()
    conn.close()
    _recalc_routes_for_engineer(eng_id, force=True)
    return jsonify({'success': True})

@app.route('/api/requests/<rid>/accept', methods=['POST'])
def accept_request_assignment(rid):
    """Подтверждение назначения инженером. Только после него запускается маршрут и ETA."""
    data = request.get_json() or {}
    body_engineer_id = str(data.get('engineer_id') or '').strip()
    # Критерий 14: инженер действует только от имени сессии, тело лишь сверяется.
    if AUTH_REQUIRED and session.get('role') == 'engineer':
        session_engineer_id = str(session.get('user_id') or '')
        if body_engineer_id and body_engineer_id != session_engineer_id:
            append_event('role_denied', path=request.path, method=request.method,
                         role='engineer', reason='body_engineer_mismatch')
            return jsonify({'success': False,
                            'error': 'Инженер может принять только своё назначение'}), 403
        engineer_id = session_engineer_id
    else:
        engineer_id = body_engineer_id
    if not engineer_id:
        return jsonify({'success': False, 'error': 'Не указан инженер'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, engineer_id, engineer_name, aircraft, parking_id,
                          routing_stage, status
                   FROM requests WHERE id=?""", (rid,))
    req = cur.fetchone()
    if not req:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if str(req['engineer_id'] or '') != engineer_id:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка назначена другому инженеру'}), 403
    if req['routing_stage'] != 'technician':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка больше не находится у инженера'}), 409

    cur.execute("SELECT id, status, task, task_step FROM engineers WHERE id=?", (engineer_id,))
    engineer = cur.fetchone()
    if not engineer:
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер не найден'}), 404

    if req['status'] in ('arriving', 'in_progress', 'working') and engineer['task_step'] in ('accepted', 'in_car', 'arrived', 'diagnosing'):
        conn.close()
        return jsonify({'success': True, 'already_accepted': True, 'request_id': rid})
    if req['status'] != 'assigned' or engineer['task_step'] not in ('pending', 'idle', '', None):
        conn.close()
        return jsonify({'success': False, 'error': 'Назначение уже обработано или недоступно'}), 409

    cur.execute("""UPDATE engineers SET status='busy', task_step='accepted',
                   task_started_at=CURRENT_TIMESTAMP, task=COALESCE(task, ?)
                   WHERE id=?""", ('ВС ' + (req['aircraft'] or ''), engineer_id))
    cur.execute("""UPDATE requests SET status='arriving', inspection_assigned_at=CURRENT_TIMESTAMP,
                   travel_time=0, using_car=0, walk_to_car=0, car_time=0, best_car=NULL
                   WHERE id=?""", (rid,))
    _log_routing_event(conn, rid, 'technician', req['engineer_name'] or engineer_id,
                       'Инженер принял назначение; маршрут запущен')
    conn.commit()
    conn.close()
    append_event('status_changed', request_id=rid, previous_status=req['status'],
                 status='arriving', actor='engineer', engineer_id=engineer_id)

    # On acceptance, refresh the preview as the active travel route.
    _recalc_route_for_request_id(rid, force=True)
    forget_candidates(rid)

    # Reserve the chosen special vehicle after the route is calculated. In a race,
    # recalculate once more so the route does not point to an already occupied car.
    for attempt in range(2):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT best_car FROM requests WHERE id=?", (rid,))
        found = cur.fetchone()
        best_car = found['best_car'] if found else None
        if not best_car:
            conn.close()
            break
        cur.execute("UPDATE transport SET assigned_to=? WHERE device_id=? AND assigned_to IS NULL",
                    (rid, best_car))
        reserved = cur.rowcount > 0
        conn.commit()
        conn.close()
        if reserved:
            break
        _recalc_route_for_request_id(rid, force=True)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT travel_time, using_car, walk_to_car, car_time, best_car, engineer_comment, inspection_assigned_at FROM requests WHERE id=?", (rid,))
    result = cur.fetchone()
    conn.close()
    return jsonify({
        'success': True, 'request_id': rid, 'route_started': True,
        'travel_time': result['travel_time'] if result else 0,
        'using_car': bool(result['using_car']) if result else False,
        'best_car': result['best_car'] if result else None,
        'inspection_assigned_at': result['inspection_assigned_at'] if result else None,
    })


@app.route('/api/engineers/<eng_id>/step', methods=['PUT'])
def update_engineer_step(eng_id):
    """Инженер сообщает свой текущий шаг: idle/pending/accepted/in_car/arrived/diagnosing/completed.
    При accepted — фиксируется task_started_at; при idle/completed — очищается."""
    data = request.get_json() or {}
    step = (data.get('step') or '').strip()
    valid = ('idle', 'pending', 'accepted', 'in_car', 'arrived', 'diagnosing', 'completed')
    if step not in valid:
        return jsonify({'success': False, 'error': 'Неверный step'}), 400
    if step == 'accepted':
        return jsonify({'success': False, 'error': 'Примите заявку через кнопку «Принять» в карточке задания'}), 409

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, task_step FROM engineers WHERE id=?", (eng_id,))
    engineer = cur.fetchone()
    if not engineer:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найден'}), 404
    # Нельзя перескочить кнопки «Принять / Отклонить» прямым вызовом шага.
    if step in ('in_car', 'arrived', 'diagnosing') and engineer['task_step'] in ('pending', 'idle', None, ''):
        conn.close()
        return jsonify({'success': False, 'error': 'Сначала примите назначение кнопкой «Принять»'}), 409

    if step in ('idle', 'completed'):
        cur.execute(
            "UPDATE engineers SET task_step=?, task_started_at=NULL WHERE id=?",
            (step, eng_id))
    else:
        cur.execute("UPDATE engineers SET task_step=? WHERE id=?", (step, eng_id))

    conn.commit()
    if step in ('in_car', 'arrived', 'diagnosing', 'completed', 'idle'):
        cur.execute("""SELECT id FROM requests
                       WHERE engineer_id=? AND routing_stage='technician'
                         AND status NOT IN ('completed','cancelled','flight_cancelled',
                                            'draft','report_submitted','deleted')
                       ORDER BY created_at DESC LIMIT 1""", (eng_id,))
        linked = cur.fetchone()
        append_event('status_changed', request_id=(linked['id'] if linked else None),
                     status=step, actor='engineer', engineer_id=eng_id)
    conn.close()
    _recalc_routes_for_engineer(eng_id, force=True)
    return jsonify({'success': True, 'step': step})


@app.route('/api/engineers/<eng_id>', methods=['DELETE'])
def delete_engineer(eng_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM engineers WHERE id=?", (eng_id,))
    cur.execute("DELETE FROM users WHERE id=?", (eng_id,))
    cur.execute("DELETE FROM map_object_positions WHERE object_type IN ('engineer_gps','engineer_gps_heading') AND object_id=?", (str(eng_id),))
    conn.commit()
    conn.close()
    _recalc_routes_for_engineer(eng_id, force=True)
    return jsonify({'success': True})


@app.route('/api/engineers/<eng_id>/location', methods=['GET', 'PUT'])
@app.route('/api/engineers/<eng_id>/gps', methods=['GET', 'PUT'])
@app.route('/api/engineer/<eng_id>/location', methods=['GET', 'PUT'])
def engineer_gps_location(eng_id):
    """Отдельные реальные GPS-координаты мобильного устройства инженера.

    Хранятся отдельно от coords, которые используются симуляцией маршрутов
    и диспетчерской картой. Компьютерный экран читает только этот endpoint.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,name FROM engineers WHERE id=?", (eng_id,))
    engineer = cur.fetchone()
    if not engineer:
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер не найден'}), 404

    if request.method == 'GET':
        cur.execute("""SELECT lat,lng,updated_at
                       FROM map_object_positions
                       WHERE object_type='engineer_gps' AND object_id=?""", (str(eng_id),))
        row = cur.fetchone()
        cur.execute("""SELECT lat,lng FROM map_object_positions
                       WHERE object_type='engineer_gps_heading' AND object_id=?""", (str(eng_id),))
        heading_row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({'success': True, 'available': False, 'engineer_id': str(eng_id),
                            'name': engineer['name'], 'location': None})
        return jsonify({
            'success': True,
            'available': True,
            'engineer_id': str(eng_id),
            'name': engineer['name'],
            'location': {
                'lat': row['lat'],
                'lng': row['lng'],
                'heading': heading_row['lat'] if heading_row else None,
                'accuracy': heading_row['lng'] if heading_row else None,
                'updated_at': row['updated_at'],
            }
        })

    data = request.get_json(silent=True) or {}
    lat = data.get('lat')
    lng = data.get('lng')
    accuracy = data.get('accuracy')
    heading = data.get('heading')
    if lat is None or lng is None:
        conn.close()
        return jsonify({'success': False, 'error': 'lat/lng обязательны'}), 400
    try:
        lat = float(lat)
        lng = float(lng)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError('Координаты вне допустимого диапазона')
        if accuracy is not None:
            accuracy = float(accuracy)
            if not (0 <= accuracy <= 100000):
                raise ValueError('Некорректная точность GPS')
        if heading is not None:
            heading = float(heading) % 360.0
            if not (0 <= heading < 360):
                raise ValueError('Некорректный курс GPS')
    except (TypeError, ValueError):
        conn.close()
        return jsonify({'success': False, 'error': 'Некорректные координаты GPS'}), 400

    # Важно: GPS — фактическое положение, его нельзя блокировать правилами
    # симуляции движения самолётов или перезаписывать координатами маршрута.
    cur.execute("""INSERT INTO map_object_positions (object_type,object_id,lat,lng,updated_at)
                   VALUES ('engineer_gps',?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(object_type,object_id) DO UPDATE SET
                     lat=excluded.lat,lng=excluded.lng,updated_at=CURRENT_TIMESTAMP""",
                (str(eng_id), lat, lng))
    if heading is not None:
        # Keep compass metadata in a separate row in the existing table;
        # no database migration or schema alteration is required.
        cur.execute("""INSERT INTO map_object_positions (object_type,object_id,lat,lng,updated_at)
                       VALUES ('engineer_gps_heading',?,?,?,CURRENT_TIMESTAMP)
                       ON CONFLICT(object_type,object_id) DO UPDATE SET
                         lat=excluded.lat,lng=excluded.lng,updated_at=CURRENT_TIMESTAMP""",
                    (str(eng_id), heading, accuracy if accuracy is not None else 0.0))
    conn.commit()
    conn.close()
    # Единый конвейер: фактическая позиция телефона сразу применяется как
    # позиция инженера — маршрут/ETA/автоприбытие/SSE работают так же,
    # как для POST /api/v1/track (контракт организаторов).
    applied = _track_apply_position('engineer', str(eng_id), lat, lng,
                                    _utc_now_iso(), acc=accuracy, heading=heading,
                                    update_coords=False, recalc=True)
    response = {'success': True, 'engineer_id': str(eng_id), 'lat': lat,
                'lng': lng, 'accuracy': accuracy, 'heading': heading,
                'applied': bool(applied)}
    if not applied:
        from intravia.tracking import track_pause_reason
        pause_reason = track_pause_reason('engineer', str(eng_id))
        if pause_reason:
            response['reason'] = pause_reason
    return jsonify(response)


@app.route('/api/engineers/<eng_id>/coords', methods=['PUT'])
def update_engineer_coords(eng_id):
    data = request.get_json() or {}
    lat = data.get('lat')
    lng = data.get('lng')
    if lat is None or lng is None:
        return jsonify({'error': 'lat/lng обязательны'}), 400
    try:
        lat, lng = float(lat), float(lng)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError('Координаты вне допустимого диапазона')
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Некорректные координаты'}), 400
    conflicting_aircraft = _aircraft_taxi_conflict(lat, lng)
    if conflicting_aircraft:
        return jsonify({'success': False, 'error': 'Перемещение запрещено: рулит самолёт ' + conflicting_aircraft, 'safety_block': True}), 409
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE engineers SET coords_lat=?, coords_lng=? WHERE id=?",
                (lat, lng, eng_id))
    if cur.rowcount == 0:
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер не найден'}), 404
    cur.execute("""INSERT INTO map_object_positions (object_type,object_id,lat,lng,updated_at)
                   VALUES ('engineer',?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(object_type,object_id) DO UPDATE SET
                     lat=excluded.lat,lng=excluded.lng,updated_at=CURRENT_TIMESTAMP""",
                (str(eng_id), lat, lng))
    conn.commit()
    conn.close()
    _recalc_routes_for_engineer(eng_id, force=True)
    return jsonify({'success': True, 'object_type': 'engineer', 'id': str(eng_id),
                    'lat': lat, 'lng': lng})


# ============================================================
# Pilots
# ============================================================
@app.route('/api/pilots', methods=['GET'])
def get_pilots():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,name,email,phone,aircraft,rank,status FROM pilots ORDER BY name")
    res = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(res)


@app.route('/api/pilots', methods=['POST'])
def create_pilot():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    if not name or not email:
        return jsonify({'error': 'Имя и email обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM pilots WHERE email=?", (email,))
    if cur.fetchone():
        conn.close()
        return jsonify({'error': 'Email уже существует'}), 400
    pid = 'pilot_' + str(int(time.time() * 1000))
    cur.execute("""INSERT INTO pilots (id,name,email,phone,aircraft,rank,status)
                   VALUES (?,?,?,?,?,?,?)""",
                (pid, name, email, data.get('phone', ''), data.get('aircraft', ''),
                 data.get('rank', 'pilot'), data.get('status', 'active')))
    cur.execute("""INSERT INTO users (id,email,password,name,role,avatar,status)
                   VALUES (?,?,?,?,'pilot','👨‍✈️','offline')""",
                (pid, email, hash_password('pilot123'), name))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'id': pid})


@app.route('/api/pilots/<pid>', methods=['PUT'])
def update_pilot(pid):
    data = request.get_json() or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM pilots WHERE id=?", (pid,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'error': 'Не найден'}), 404
    cur.execute("""UPDATE pilots SET name=?, phone=?, aircraft=?, rank=?, status=? WHERE id=?""",
                (data.get('name'), data.get('phone', ''), data.get('aircraft', ''),
                 data.get('rank', 'pilot'), data.get('status', 'active'), pid))
    if data.get('name'):
        cur.execute("UPDATE users SET name=? WHERE id=?", (data['name'], pid))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/pilots/<pid>', methods=['DELETE'])
def delete_pilot(pid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM pilots WHERE id=?", (pid,))
    cur.execute("DELETE FROM users WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# Transport
# ============================================================
@app.route('/api/transport', methods=['GET'])
def get_transport():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT t.id,t.device_id,COALESCE(p.lat,t.lat) AS lat,
                   COALESCE(p.lng,t.lng) AS lng,t.speed,t.assigned_to
                   FROM transport t LEFT JOIN map_object_positions p
                     ON p.object_type='transport' AND p.object_id=CAST(t.id AS TEXT)
                   ORDER BY t.id""")
    res = [dict(r) for r in cur.fetchall()]
    for car in res:
        car['status'] = 'busy' if car.get('assigned_to') else 'free'
    conn.close()
    return jsonify(res)


@app.route('/api/transport', methods=['POST'])
def create_transport():
    data = request.get_json() or {}
    dev = (data.get('device_id') or '').strip()
    lat = data.get('lat')
    lng = data.get('lng')
    if not dev or lat is None or lng is None:
        return jsonify({'error': 'device_id, lat, lng обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM transport WHERE device_id=?", (dev,))
    if cur.fetchone():
        conn.close()
        return jsonify({'error': 'device_id уже существует'}), 400
    cur.execute("""INSERT INTO transport (device_id,lat,lng,speed,assigned_to)
                   VALUES (?,?,?,?,?)""",
                (dev, lat, lng, data.get('speed', 50), data.get('assigned_to')))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return jsonify({'success': True, 'id': tid})


@app.route('/api/transport/<int:tid>', methods=['PUT'])
def update_transport(tid):
    data = request.get_json() or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM transport WHERE id=?", (tid,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'error': 'Не найден'}), 404
    cur.execute("""UPDATE transport SET device_id=?, lat=?, lng=?, speed=?, assigned_to=?
                   WHERE id=?""",
                (data.get('device_id'), data.get('lat'), data.get('lng'),
                 data.get('speed', 50), data.get('assigned_to'), tid))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/transport/<int:tid>/assign', methods=['POST'])
def assign_transport(tid):
    """Резервирует машину за заявкой. При гонке вторая заявка получает 409."""
    data = request.get_json() or {}
    request_id = str(data.get('request_id') or '').strip()
    if not request_id:
        return jsonify({'success': False, 'error': 'request_id обязателен'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM requests WHERE id=?", (request_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    cur.execute("""UPDATE transport SET assigned_to=?
                   WHERE id=? AND (assigned_to IS NULL OR assigned_to=?)""",
                (request_id, tid, request_id))
    if cur.rowcount == 0:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': 'Машина уже назначена другой заявке'}), 409
    conn.commit()
    conn.close()
    _recalc_route_for_request_id(request_id, force=True)
    return jsonify({'success': True, 'transport_id': tid, 'request_id': request_id})


@app.route('/api/transport/<int:tid>/release', methods=['POST'])
def release_transport(tid):
    data = request.get_json() or {}
    request_id = str(data.get('request_id') or '').strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT assigned_to FROM transport WHERE id=?", (tid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Машина не найдена'}), 404
    affected = row['assigned_to']
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    target_request = request_id or affected
    if target_request:
        _recalc_route_for_request_id(target_request, force=True)
    return jsonify({'success': True, 'transport_id': tid,
                    'released_from': affected})


@app.route('/api/transport/<int:tid>/coords', methods=['PUT'])
def update_transport_coords(tid):
    data = request.get_json() or {}
    lat = data.get('lat')
    lng = data.get('lng')
    if lat is None or lng is None:
        return jsonify({'error': 'lat/lng обязательны'}), 400
    try:
        lat, lng = float(lat), float(lng)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError('Координаты вне допустимого диапазона')
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Некорректные координаты'}), 400
    conflicting_aircraft = _aircraft_taxi_conflict(lat, lng)
    if conflicting_aircraft:
        return jsonify({'success': False, 'error': 'Перемещение запрещено: рулит самолёт ' + conflicting_aircraft, 'safety_block': True}), 409
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, assigned_to FROM transport WHERE id=?", (tid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Не найден'}), 404
    if 'speed' in data and data['speed'] is not None:
        cur.execute("UPDATE transport SET lat=?, lng=?, speed=? WHERE id=?",
                    (lat, lng, data['speed'], tid))
    else:
        cur.execute("UPDATE transport SET lat=?, lng=? WHERE id=?", (lat, lng, tid))
    cur.execute("""INSERT INTO map_object_positions (object_type,object_id,lat,lng,updated_at)
                   VALUES ('transport',?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(object_type,object_id) DO UPDATE SET
                     lat=excluded.lat,lng=excluded.lng,updated_at=CURRENT_TIMESTAMP""",
                (str(tid), lat, lng))
    conn.commit()
    assigned_to = row['assigned_to']
    conn.close()
    if assigned_to:
        _recalc_route_for_request_id(assigned_to, force=True)
    return jsonify({'success': True, 'object_type': 'transport', 'id': str(tid),
                    'lat': lat, 'lng': lng})


@app.route('/api/transport/<int:tid>', methods=['DELETE'])
def delete_transport(tid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM transport WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# Excel export for operational / BI reporting
# ============================================================
@app.route('/api/reports/export.xlsx', methods=['GET'])
def export_operational_reports_xlsx():
    try:
        from io import BytesIO
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify({'success': False, 'error': 'Для экспорта Excel установите пакет openpyxl'}), 503

    conn = get_db()
    sync_expired_mel(conn)
    cur = conn.cursor()
    cur.execute("""SELECT r.*, ar.aircraft_type, ar.configuration AS aircraft_configuration
        FROM requests r LEFT JOIN aircraft_registry ar ON ar.registration=UPPER(r.aircraft)
        ORDER BY r.created_at DESC""")
    rows = [dict(x) for x in cur.fetchall()]
    archived = []
    audit_rows = []
    for row in rows:
        is_deleted = row.get('status') == 'deleted'
        row['record_storage'] = 'Архив удалённых' if is_deleted else 'В базе данных'
        row['archive_reason'] = ''
        row['archived_at'] = ''
        try:
            history = json.loads(row.get('routing_history') or '[]')
        except Exception:
            history = []
        for event in history if isinstance(history, list) else []:
            if isinstance(event, dict):
                audit_rows.append({
                    'request_id': row.get('id', ''), 'occurred_at': event.get('at') or event.get('timestamp') or '',
                    'action': event.get('action') or 'Событие маршрута', 'actor': event.get('actor') or '',
                    'stage': event.get('stage') or '', 'details': event.get('note') or event.get('details') or ''
                })
        if is_deleted:
            deleted_events = [e for e in history if isinstance(e, dict) and e.get('stage') == 'deleted'] if isinstance(history, list) else []
            if deleted_events:
                row['archive_reason'] = deleted_events[-1].get('note') or ''
                row['archived_at'] = deleted_events[-1].get('at') or ''
            archived.append(row)
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = 'Заявки'
    columns = [
        ('ID', 'id'), ('Борт', 'aircraft'), ('Тип ВС', 'aircraft_type'),
        ('Конфигурация', 'aircraft_configuration'), ('Рейс', 'flight_number'),
        ('Неисправность', 'fault_type'), ('Описание', 'description'), ('Статус', 'mcc_status'),
        ('Этап', 'routing_stage'), ('Статус полёта', 'flight_status'),
        ('Инженер', 'engineer_name'), ('Вердикт инженера', 'engineer_verdict'),
        ('Состояние ВС', 'engineer_condition'), ('Отчёт инженера', 'engineer_comment'),
        ('Дата отчёта инженера', 'engineer_report_at'), ('Пункт MEL', 'mcc_mel_ref'),
        ('ATA', 'mcc_ata'), ('Категория MEL', 'mcc_mel_category'),
        ('Интервал MEL', 'mel_rectification_interval'), ('Первичный отчёт техника', 'mel_initial_engineer_report'),
        ('Первичный отчёт — дата', 'mel_initial_engineer_report_at'), ('Первичную оценку выполнил', 'mel_initial_assessed_by'),
        ('M-процедура выполнена техником', 'mel_m_procedure_completed'),
        ('Отчёт техника по M-процедуре', 'mel_m_procedure_comment'), ('Тип контроля срока', 'mel_deadline_mode'),
        ('Значение лимита', 'mel_deadline_value'), ('Срок до', 'mcc_due_date'),
        ('Условие срока', 'mcc_due_condition'), ('Ограничения', 'mcc_mel_restrictions'),
        ('O-процедура нужна', 'mcc_proc_o'), ('O-процедура проверена', 'cpu_mel_o_completed'),
        ('M-процедура нужна', 'mcc_proc_m'), ('M-процедура проверена', 'cpu_mel_m_completed'),
        ('Применимость ЦПУ', 'cpu_mel_applicability_confirmed'),
        ('Проверил ЦПУ', 'cpu_mel_checked_by'), ('Комментарий ЦПУ', 'cpu_mel_check_comment'),
        ('Проверил MCC', 'mcc_mel_reviewed_by'), ('Дата проверки MCC', 'mcc_mel_reviewed_at'),
        ('Комментарий MCC', 'mcc_mel_review_comment'), ('Совокупное влияние проверено', 'mcc_mel_combined_checked'),
        ('Экипаж проинформирован', 'mcc_mel_crew_briefed'), ('Ограничения проверены', 'mcc_mel_restrictions_verified'),
        ('План устранения', 'mcc_mel_repair_plan'), ('Причина статуса полёта', 'flight_status_reason'),
        ('Создано', 'created_at'), ('Хранение записи', 'record_storage')]
    ws.append([c[0] for c in columns])
    for r in rows:
        ws.append([r.get(c[1]) if r.get(c[1]) is not None else '' for c in columns])

    mel_ws = wb.create_sheet('MEL')
    mel_cols = [
        ('Заявка', 'id'), ('Борт', 'aircraft'), ('Тип ВС', 'aircraft_type'),
        ('Конфигурация', 'aircraft_configuration'), ('Дефект', 'fault_type'),
        ('Пункт', 'mcc_mel_ref'), ('ATA', 'mcc_ata'), ('Категория', 'mcc_mel_category'),
        ('Редакция', 'mcc_mel_revision'), ('Описание пункта', 'mcc_mel_description'),
        ('Интервал устранения', 'mel_rectification_interval'), ('Тип лимита', 'mel_deadline_mode'),
        ('Значение', 'mel_deadline_value'), ('Правило начала отсчёта', 'mel_deadline_start_rule'),
        ('Дата устранения', 'mcc_due_date'), ('Условие', 'mcc_due_condition'),
        ('Ограничения', 'mcc_mel_restrictions'), ('O procedure', 'mcc_proc_o_text'),
        ('M procedure', 'mcc_proc_m_text'), ('Инженер', 'mel_assessed_by'),
        ('Обоснование инженера', 'mel_assessment_comment'), ('M-процедура выполнена техником', 'mel_m_procedure_completed'),
        ('Отчёт техника по M-процедуре', 'mel_m_procedure_comment'), ('Подтверждение ЦПУ', 'cpu_mel_applicability_confirmed'),
        ('O проверена', 'cpu_mel_o_completed'), ('M проверена', 'cpu_mel_m_completed'),
        ('Проверка MCC', 'mcc_mel_reviewed_by'), ('Проверено совокупное влияние', 'mcc_mel_combined_checked'),
        ('Экипаж проинформирован', 'mcc_mel_crew_briefed'), ('План устранения', 'mcc_mel_repair_plan'), ('Статус MEL', 'mcc_status'),
        ('Статус ВС', 'flight_status')]
    mel_ws.append([c[0] for c in mel_cols])
    for r in rows:
        if r.get('mcc_mel_choice') == 'MEL' or r.get('engineer_verdict') == 'MEL':
            mel_ws.append([r.get(c[1]) if r.get(c[1]) is not None else '' for c in mel_cols])

    log_ws = wb.create_sheet('Журнал действий')
    log_ws.append(['Заявка', 'Борт', 'Время', 'Этап', 'Исполнитель', 'Действие'])
    for r in rows:
        try:
            history = json.loads(r.get('routing_history') or '[]')
        except Exception:
            history = []
        for event in history:
            if isinstance(event, dict):
                log_ws.append([r.get('id',''), r.get('aircraft',''), event.get('timestamp') or event.get('at') or '',
                               event.get('stage') or '', event.get('actor') or '', event.get('note') or event.get('action') or ''])
    for event in audit_rows:
        log_ws.append([event.get('request_id',''), '', event.get('occurred_at',''),
                       event.get('stage',''), event.get('actor',''),
                       (event.get('action','') + (': ' + event.get('details','') if event.get('details') else ''))])

    reports_ws = wb.create_sheet('Отчёты инженеров')
    report_cols = [('Заявка','id'),('Борт','aircraft'),('Рейс','flight_number'),('Неисправность','fault_type'),
                   ('Инженер','engineer_name'),('Вердикт','engineer_verdict'),('Состояние','engineer_condition'),
                   ('Отчёт','engineer_comment'),('Дата отчёта','engineer_report_at'),('MEL','mcc_mel_ref'),
                   ('ATA','mcc_ata'),('Категория MEL','mcc_mel_category'),('Редакция MEL','mcc_mel_revision'),
                   ('Интервал MEL','mel_rectification_interval'),('Хранение записи','record_storage')]
    reports_ws.append([x[0] for x in report_cols])
    for r in rows:
        if r.get('engineer_report_at') or r.get('engineer_comment') or r.get('mel_initial_engineer_report'):
            reports_ws.append([r.get(k) if r.get(k) is not None else '' for _, k in report_cols])

    archive_ws = wb.create_sheet('Архив удалённых')
    archive_ws.append(['Заявка','Борт','Дата создания','Дата архивации','Причина','Инженер','Вердикт','Отчёт инженера'])
    for r in archived:
        archive_ws.append([r.get('id',''), r.get('aircraft',''), r.get('created_at',''), r.get('archived_at',''),
                           r.get('archive_reason',''), r.get('engineer_name',''), r.get('engineer_verdict',''), r.get('engineer_comment','')])

    events_ws = wb.create_sheet('Журнал')
    _fill_events_sheet(events_ws, read_events(limit=0))

    for sheet in wb.worksheets:
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='17365D')
            cell.alignment = Alignment(vertical='center', wrap_text=True)
        sheet.row_dimensions[1].height = 28
        for col_idx in range(1, sheet.max_column + 1):
            max_len = 12
            for row_idx in range(1, min(sheet.max_row, 250) + 1):
                val = sheet.cell(row=row_idx, column=col_idx).value
                if val is not None:
                    max_len = max(max_len, min(len(str(val)), 55))
            sheet.column_dimensions[get_column_letter(col_idx)].width = max(12, min(max_len + 2, 45))
        for row_cells in sheet.iter_rows(min_row=2):
            for cell in row_cells:
                cell.alignment = Alignment(vertical='top', wrap_text=True)

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    filename = 'InTrAvia_operational_report_' + datetime.utcnow().strftime('%Y%m%d_%H%M%S') + '.xlsx'
    return Response(output.getvalue(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="' + filename + '"'})


# ============================================================
# Requests
# ============================================================
@app.route('/api/requests', methods=['GET'])
def get_requests():
    conn = get_db()
    sync_expired_mel(conn)
    cur = conn.cursor()
    _cleanup_inactive_route_payloads(cur)
    conn.commit()
    source = request.args.get('source')
    pilot_name = request.args.get('pilot_name')

    sql = """SELECT r.id, r.aircraft, r.flight_number, r.fault_type, r.severity, r.terminal, r.parking_id,
        r.description, r.source, r.engineer_id, r.engineer_name, e.task_step AS engineer_task_step, e.status AS engineer_status,
        ar.aircraft_type, ar.configuration AS aircraft_configuration,
        r.travel_time, r.using_car, r.walk_to_car, r.car_time, r.best_car,
        r.status, r.criticality, r.engineer_comment, r.created_at,
        r.pilot_name, r.pilot_rank, r.pilot_audio_text, r.rejected_by,
        r.cancel_reason, r.cancelled_by_name, r.cancelled_at,
        r.replaces_request_id, r.delay_minutes, r.delay_reason, r.delay_started_at,
        r.delay_approved_by, r.flight_cancelled, r.flight_cancel_reason,
        r.flight_cancelled_at, r.all_rejected, r.mcc_status, r.mcc_decision, r.mcc_comment,
        r.engineer_verdict, r.engineer_verdict_at, r.aog_action, r.inspection_assigned_at,
        r.mcc_mel_choice, r.mcc_proc_o, r.mcc_proc_m, r.mcc_proc_o_text, r.mcc_proc_m_text,
        r.mcc_ata,
        r.mcc_mel_ref, r.mcc_mel_category, r.mcc_due_date, r.mcc_due_condition,
        r.mcc_mel_revision, r.mcc_mel_description, r.mcc_mel_restrictions,
        r.mel_catalog_id, r.mel_assessed_by, r.mel_assessment_comment, r.mel_rectification_interval,
        r.mel_m_procedure_completed, r.mel_m_procedure_comment,
        r.mel_initial_engineer_report, r.mel_initial_engineer_report_at, r.mel_initial_assessed_by,
        r.mel_deadline_mode, r.mel_deadline_value, r.mel_deadline_start_rule,
        r.cpu_mel_applicability_confirmed, r.cpu_mel_checked_by, r.cpu_mel_checked_at,
        r.mcc_mel_reviewed_by, r.mcc_mel_reviewed_at, r.mcc_mel_review_comment,
        r.crs_signature, r.crs_signed_at, r.crs_signed_by,
        r.routing_stage, r.routing_history, r.flight_status, r.flight_status_reason,
        r.engineer_condition, r.engineer_report_at, r.dispatcher_report_at, r.cpu_report_at, r.pilot_notice_at
        FROM requests r LEFT JOIN engineers e ON e.id=r.engineer_id
        LEFT JOIN aircraft_registry ar ON ar.registration=UPPER(r.aircraft)
        WHERE r.status NOT IN ('draft','deleted')"""
    params = []
    if source:
        sql += " AND source=?"
        params.append(source)
    if pilot_name:
        sql += " AND pilot_name=?"
        params.append(pilot_name)

    # Критерий 14: список заявок ограничен ролью. Пилот видит только свои
    # заявки (source='pilot' и его имя), инженер — только назначенные ему.
    if AUTH_REQUIRED:
        role = session.get('role')
        if role == 'engineer':
            sql += " AND r.engineer_id=?"
            params.append(str(session.get('user_id') or ''))
        elif role == 'pilot':
            sql += " AND r.source='pilot' AND r.pilot_name=?"
            params.append(str(session.get('name') or ''))

    sql += " ORDER BY r.created_at DESC"

    cur.execute(sql, params)
    res = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in res:
        try:
            r['routing_history'] = json.loads(r['routing_history'] or '[]')
        except Exception:
            r['routing_history'] = []
        # Последний отчёт об инструменте из журнала маршрута (без изменения схемы).
        r['tool_status'] = None
        for event in reversed(r['routing_history']):
            if isinstance(event, dict) and event.get('stage') == 'tool_status':
                r['tool_status'] = (event.get('tool_status')
                                    or event.get('status'))
                break
    return jsonify(res)


@app.route('/api/requests/<rid>', methods=['PUT'])
def update_request(rid):
    data = request.get_json() or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM requests WHERE id=?", (rid,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'error': 'Не найдена'}), 404

    cur.execute("SELECT status, routing_stage FROM requests WHERE id=?", (rid,))
    current = cur.fetchone()
    new_status = data.get('status')
    if new_status is not None:
        # Статус меняется только по разрешённым переходам. Ручное закрытие
        # технической задачи и произвольные скачки запрещены.
        allowed_transitions = {
            ('assigned', 'arriving'),
            ('arriving', 'in_progress'),
            ('in_progress', 'report_submitted'),
        }
        current_status = str((current['status'] if current else '') or '')
        if str(new_status) != current_status and \
                (current_status, str(new_status)) not in allowed_transitions:
            conn.close()
            return jsonify({
                'success': False,
                'error': 'Недопустимый переход статуса: '
                         f'{current_status or "—"} → {new_status}'
            }), 400

    def _optional_float(key):
        if key not in data or data.get(key) is None or data.get(key) == '':
            return None
        try:
            value = float(data.get(key))
            return value if value >= 0 else None
        except (TypeError, ValueError):
            return None

    travel_time = _optional_float('travel_time')
    walk_to_car = _optional_float('walk_to_car')
    car_time = _optional_float('car_time')
    using_car = None
    if 'using_car' in data:
        using_car = 1 if bool(data.get('using_car')) else 0

    cur.execute("""UPDATE requests
        SET status=COALESCE(?,status), criticality=COALESCE(?,criticality),
            engineer_comment=COALESCE(?,engineer_comment),
            aircraft=COALESCE(?,aircraft), fault_type=COALESCE(?,fault_type),
            terminal=COALESCE(?,terminal), parking_id=COALESCE(?,parking_id),
            severity=COALESCE(?,severity), flight_number=COALESCE(?,flight_number),
            travel_time=COALESCE(?,travel_time), using_car=COALESCE(?,using_car),
            walk_to_car=COALESCE(?,walk_to_car), car_time=COALESCE(?,car_time),
            best_car=CASE WHEN ?=1 THEN ? ELSE best_car END
        WHERE id=?""",
                (data.get('status'), data.get('criticality'),
                 data.get('engineer_comment'), data.get('aircraft'),
                 data.get('fault_type'), data.get('terminal'),
                 data.get('parking_id'), data.get('severity'),
                 data.get('flight_number'), travel_time, using_car,
                 walk_to_car, car_time, 1 if 'best_car' in data else 0,
                 data.get('best_car') or None, rid))

    if new_status == 'completed':
        cur.execute("SELECT engineer_id FROM requests WHERE id=?", (rid,))
        r = cur.fetchone()
        if r and r['engineer_id']:
            other = engineer_has_active_requests(conn, r['engineer_id'], rid)
            if other == 0:
                cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?",
                            (r['engineer_id'],))
        cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    elif new_status == 'cancelled':
        cur.execute("UPDATE requests SET routing_stage='done' WHERE id=?", (rid,))
        cur.execute("SELECT engineer_id FROM requests WHERE id=?", (rid,))
        r = cur.fetchone()
        if r and r['engineer_id']:
            other = engineer_has_active_requests(conn, r['engineer_id'], rid)
            if other == 0:
                cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?",
                            (r['engineer_id'],))
        cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))

    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/requests/<rid>', methods=['DELETE'])
def delete_request(rid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, engineer_id, status FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Не найдена'}), 404
    eng_id = row['engineer_id']
    _archive_requests_before_delete(cur, 'id=?', (rid,), reason='Удалена диспетчером')
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    # Освобождаем исполнителя независимо от прежнего статуса удалённой заявки.
    # Если у него осталась другая активная заявка, статус busy сохраняется.
    release_engineers_without_active_requests(conn, [eng_id])
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# Статусная модель заявки и ожидание запчастей
# ============================================================
def _engineer_self_error(engineer_id):
    """Для инженера разрешает действие только от собственного имени."""
    if not AUTH_REQUIRED:
        return None
    role = session.get('role')
    if role == 'admin':
        return None
    if role != 'engineer' or str(session.get('user_id') or '') != str(engineer_id):
        append_event('role_denied', path=request.path, method=request.method,
                     role=role, reason='foreign_engineer')
        return jsonify({'success': False,
                        'error': 'Инженер может действовать только от своего имени'}), 403
    return None


def _request_actor():
    if not AUTH_REQUIRED:
        return 'Система'
    return session.get('name') or session.get('user_id') or 'Система'


@app.route('/api/requests/<rid>/start', methods=['POST'])
def start_request(rid):
    """Инженер начинает работу: assigned/arriving → in_progress (идемпотентно)."""
    data = request.get_json() or {}
    engineer_id = str(data.get('engineer_id') or '').strip()
    if not engineer_id:
        return jsonify({'success': False, 'error': 'Не указан инженер'}), 400
    denied = _engineer_self_error(engineer_id)
    if denied is not None:
        return denied

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, engineer_id, engineer_name, status, routing_stage
                   FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404

    is_admin = AUTH_REQUIRED and session.get('role') == 'admin'
    if row['engineer_id'] and str(row['engineer_id']) != engineer_id and not is_admin:
        conn.close()
        return jsonify({'success': False,
                        'error': 'Заявка назначена другому инженеру'}), 403

    if row['status'] == 'in_progress':
        # Если работа уже начата, но task_step не был переведён из pending
        # (старый клиент/прямой вызов /start), исправляем это: иначе
        # автоприбытие по геозоне не сработает.
        if row['engineer_id']:
            cur.execute("""UPDATE engineers SET task_step=CASE
                           WHEN COALESCE(task_step,'') IN ('','pending','idle') THEN 'accepted'
                           ELSE task_step END WHERE id=?""", (row['engineer_id'],))
            conn.commit()
        conn.close()
        return jsonify({'success': True, 'status': 'in_progress',
                        'already_started': True})
    if row['status'] not in ('assigned', 'arriving'):
        conn.close()
        return jsonify({'success': False,
                        'error': 'Начать работу можно только по назначенной заявке'}), 409

    actor = row['engineer_name'] or engineer_id
    cur.execute("""UPDATE requests
                   SET status='in_progress', routing_stage='technician'
                   WHERE id=?""", (rid,))
    # Начало работ подразумевает, что инженер принял назначение. Иначе
    # task_step остаётся pending и автоприбытие по геозоне не срабатывает.
    if row['engineer_id']:
        cur.execute("""UPDATE engineers SET task_step=CASE
                       WHEN COALESCE(task_step,'') IN ('','pending','idle') THEN 'accepted'
                       ELSE task_step END WHERE id=?""", (row['engineer_id'],))
    _log_routing_event(conn, rid, 'in_progress', actor,
                       'Инженер приступил к работе')
    conn.commit()
    conn.close()
    append_event('status_changed', request_id=rid, previous_status=row['status'],
                 status='in_progress', actor='engineer', engineer_id=engineer_id)
    return jsonify({'success': True, 'status': 'in_progress'})


@app.route('/api/requests/<rid>/tool_status', methods=['POST'])
def set_tool_status(rid):
    """Инженер отмечает наличие инструмента: available/missing/warehouse."""
    data = request.get_json() or {}
    engineer_id = str(data.get('engineer_id') or '').strip()
    tool_status = str(data.get('status') or '').strip().lower()
    comment = str(data.get('comment') or '').strip()
    if not engineer_id:
        return jsonify({'success': False, 'error': 'Не указан инженер'}), 400
    if tool_status not in ('available', 'missing', 'warehouse'):
        return jsonify({'success': False,
                        'error': 'status: available, missing или warehouse'}), 400
    denied = _engineer_self_error(engineer_id)
    if denied is not None:
        return denied

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, engineer_id, engineer_name FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    is_admin = AUTH_REQUIRED and session.get('role') == 'admin'
    if row['engineer_id'] and str(row['engineer_id']) != engineer_id and not is_admin:
        conn.close()
        return jsonify({'success': False,
                        'error': 'Заявка назначена другому инженеру'}), 403

    actor = row['engineer_name'] or engineer_id
    _log_routing_event(conn, rid, 'tool_status', actor, comment,
                       tool_status=tool_status)
    conn.commit()
    conn.close()
    append_event('tool_status', request_id=rid, engineer_id=engineer_id,
                 tool_status=tool_status, comment=comment)
    return jsonify({'success': True, 'tool_status': tool_status})


@app.route('/api/requests/<rid>/unassign', methods=['POST'])
def unassign_request(rid):
    """Снимает инженера с заявки и возвращает её в очередь диспетчера."""
    data = request.get_json() or {}
    reason = str(data.get('reason') or '').strip()
    confirmed_by = str(data.get('confirmed_by') or '').strip()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, status, engineer_id, engineer_name
                   FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404

    if row['status'] in ('in_progress', 'working', 'arriving') and not confirmed_by:
        conn.close()
        return jsonify({'success': False,
                        'error': 'Для отмены назначения требуется подтверждение '
                                 'старшего смены'}), 400

    old_engineer = row['engineer_id']
    if old_engineer:
        set_engineer_free(conn, old_engineer)
    cur.execute("""UPDATE requests
                   SET engineer_id=NULL, engineer_name=NULL, engineer_comment=NULL,
                       status='queued', routing_stage='dispatcher'
                   WHERE id=?""", (rid,))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    note = reason or 'Инженер снят с заявки'
    if confirmed_by:
        note += f'; подтвердил: {confirmed_by}'
    _log_routing_event(conn, rid, 'dispatcher', _request_actor(), note,
                       previous_engineer=old_engineer, confirmed_by=confirmed_by)
    conn.commit()
    conn.close()
    append_event('unassigned', request_id=rid, previous_engineer=old_engineer,
                 reason=reason, confirmed_by=confirmed_by,
                 actor=_request_actor())
    forget_candidates(rid)
    return jsonify({'success': True, 'status': 'queued'})


@app.route('/api/requests/<rid>/awaiting_parts', methods=['POST'])
def awaiting_parts(rid):
    """Переводит заявку в ожидание запчастей и освобождает инженера."""
    data = request.get_json() or {}
    reason = str(data.get('reason') or '').strip()
    by = str(data.get('by') or '').strip() or _request_actor()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, status, engineer_id FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    # Критерий 15: в ожидание запчастей уходит только активная работа.
    if row['status'] not in ('assigned', 'arriving', 'in_progress', 'working'):
        conn.close()
        return jsonify({'success': False,
                        'error': 'Перевести в ожидание запчастей можно только '
                                 'активную заявку (assigned/arriving/in_progress)'}), 409

    old_engineer = row['engineer_id']
    if old_engineer:
        set_engineer_free(conn, old_engineer)
    cur.execute("""UPDATE requests
                   SET engineer_id=NULL, engineer_name=NULL, engineer_comment=NULL,
                       status='awaiting_parts', routing_stage='dispatcher'
                   WHERE id=?""", (rid,))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    note = reason or 'Ожидание запчастей'
    _log_routing_event(conn, rid, 'awaiting_parts', by, note,
                       previous_engineer=old_engineer)
    conn.commit()
    conn.close()
    append_event('awaiting_parts', request_id=rid, reason=reason, by=by,
                 previous_engineer=old_engineer)
    forget_candidates(rid)
    return jsonify({'success': True, 'status': 'awaiting_parts'})


@app.route('/api/requests/<rid>/test_options', methods=['POST'])
def request_test_options(rid):
    """Демонстрационные настройки заявки: норматив, спецтехника, пауза GPS.

    Хранятся в памяти процесса (без изменения схемы БД) и пишутся в журнал.
    """
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        exists = conn.execute("SELECT id FROM requests WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    if not exists:
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404

    updates = {}
    if 'regulation_minutes' in data and data.get('regulation_minutes') not in (None, ''):
        try:
            minutes = int(data.get('regulation_minutes'))
        except (TypeError, ValueError):
            return jsonify({'success': False,
                            'error': 'regulation_minutes должен быть числом 1..60'}), 400
        if not (1 <= minutes <= 60):
            return jsonify({'success': False,
                            'error': 'regulation_minutes должен быть в диапазоне 1..60'}), 400
        updates['regulation_minutes'] = minutes
    if 'force_transport' in data:
        updates['force_transport'] = bool(data.get('force_transport'))
    if 'paused' in data:
        updates['paused'] = bool(data.get('paused'))
    if 'paused_until' in data:
        until = data.get('paused_until')
        if until in (None, ''):
            updates['paused_until'] = None
        elif isinstance(until, (int, float)) or (
                isinstance(until, str) and until.strip().replace('.', '', 1).isdigit()):
            updates['paused_until'] = float(until)
        else:
            updates['paused_until'] = str(until).strip()

    options = _set_request_test_options(rid, updates)
    actor = _request_actor()
    append_event('test_options', request_id=rid, actor=actor,
                 options=options, applied=updates)
    forget_candidates(rid)
    return jsonify({'success': True, 'request_id': rid,
                    'options': options, **options})


@app.route('/api/requests/<rid>/parts_received', methods=['POST'])
def parts_received(rid):
    """Запчасти получены: заявка возвращается в очередь диспетчера."""
    data = request.get_json() or {}
    by = str(data.get('by') or '').strip() or _request_actor()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, status FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['status'] != 'awaiting_parts':
        conn.close()
        return jsonify({'success': False,
                        'error': 'Отметить получение запчастей можно только '
                                 'для заявки в статусе awaiting_parts'}), 409

    cur.execute("""UPDATE requests
                   SET status='queued', routing_stage='dispatcher',
                       delay_reason='Запчасть получена'
                   WHERE id=?""", (rid,))
    _log_routing_event(conn, rid, 'parts_received', by, 'Запчасть получена')
    conn.commit()
    conn.close()
    append_event('parts_received', request_id=rid, previous_status=row['status'],
                 by=by)
    forget_candidates(rid)
    return jsonify({'success': True, 'status': 'queued'})


# ============================================================
# DISPATCH — создание заявки
# ============================================================
def _resolve_aircraft_parking(cur, aircraft):
    """Определяет стоянку борта по его координатам.

    Порядок: зона, в полигон которой попадает борт; ближайший центр
    стоянки; последняя стоянка из заявок этого борта. Пустая строка —
    определить не удалось.
    """
    key = str(aircraft or '').strip()
    if not key:
        return ''

    cur.execute("""SELECT id, callsign, lat, lng FROM aircraft
                   WHERE UPPER(id)=UPPER(?) OR UPPER(callsign)=UPPER(?)
                   LIMIT 1""", (key, key))
    row = cur.fetchone()

    lat = lng = None
    if row:
        try:
            lat, lng = float(row['lat']), float(row['lng'])
        except (TypeError, ValueError):
            lat = lng = None

    if lat is not None and lng is not None:
        for pid in geo.PARKING_META:
            geometry = geo._zone_geometry(pid)
            if geometry and geo._point_in_geometry(lat, lng, geometry):
                return pid
        best = None
        for pid in geo.PARKING_META:
            point = geo._parking_point(pid)
            if not point:
                continue
            plat, plng = point
            try:
                dist = geo.distance_km(lat, lng, float(plat), float(plng))
            except (TypeError, ValueError):
                continue
            if best is None or dist < best[0]:
                best = (dist, pid)
        if best:
            return best[1]

    cur.execute("""SELECT parking_id FROM requests
                   WHERE UPPER(aircraft)=UPPER(?) AND parking_id IS NOT NULL
                     AND parking_id!=''
                   ORDER BY created_at DESC, id DESC LIMIT 1""", (key,))
    previous = cur.fetchone()
    if previous and previous['parking_id']:
        return str(previous['parking_id'])
    return ''


@app.route('/api/dispatch', methods=['POST'])
def dispatch():
    data = request.get_json() or {}
    source = data.get('source', 'dispatcher')
    if source not in ('pilot', 'dispatcher'):
        return jsonify({'success': False, 'error': 'Неизвестный источник заявки'}), 400
    if AUTH_REQUIRED:
        role = session.get('role')
        if role != 'admin':
            expected = 'pilot' if source == 'pilot' else 'dispatcher'
            if role != expected:
                append_event('role_denied', path=request.path, method=request.method,
                             role=role, reason='dispatch source mismatch')
                return jsonify({'error': 'Недостаточно прав для этого источника заявки'}), 403
    conn = get_db()
    cur = conn.cursor()

    if source == 'pilot':
        pilot_name = (data.get('pilot_name') or '').strip() or 'Пилот'
        pilot_rank = (data.get('pilot_rank') or '').strip() or 'КВС'
        aircraft = (data.get('aircraft') or '').strip() or None
        pilot_audio = data.get('pilot_audio') or ''

        if pilot_audio:
            rid = 'req_' + str(int(time.time() * 1000))
            cur.execute("""INSERT INTO requests (id, aircraft, fault_type, severity, terminal,
                parking_id, description, source, engineer_id, engineer_name, travel_time,
                status, criticality, pilot_name, pilot_rank, pilot_audio,
                pilot_audio_mime, pilot_audio_duration, mcc_status, routing_stage)
                VALUES (?,?,?,'medium',NULL,NULL,?,'pilot',NULL,NULL,0,'draft','medium',?,?,?,?,?,'DRAFT','mcc')""",
                        (rid, aircraft, 'other', 'Голосовая заявка от командира ВС',
                         pilot_name, pilot_rank, pilot_audio,
                         data.get('pilot_audio_mime') or 'audio/webm',
                         data.get('pilot_audio_duration') or 0))
            conn.commit()
            conn.close()
            _append_request_created(rid, 'pilot', aircraft, None, None, 'draft', 'mcc')
            return jsonify({'success': True, 'request_id': rid,
                            'fault': {'code': 'other', 'ata': '', 'skill': 'general',
                                      'matched': False}})

        parking_id = str(data.get('parking_id') or '')
        if not parking_id:
            parking_id = _resolve_aircraft_parking(cur, aircraft)
        if not parking_id:
            conn.close()
            return jsonify({'success': False, 'error': 'Не удалось определить стоянку борта'}), 400
        # Заявка создаётся на борт: номер рейса больше не требуется.
        flight_number = ''
        raw_fault = data.get('fault_type') or data.get('description') or 'other'
        fault_info = classify_fault(raw_fault)
        fault = fault_info['code']
        description = data.get('description') or str(raw_fault)
        ata_note = ('ATA ' + fault_info['ata']) if fault_info['ata'] else ''
        severity = 'medium'

        rid = 'req_' + str(int(time.time() * 1000))
        cur.execute("""INSERT INTO requests (id, aircraft, flight_number, fault_type, severity,
            parking_id, description, source, status, criticality, mcc_status, routing_stage,
            pilot_name, pilot_rank, mcc_comment, mcc_ata)
            VALUES (?,?,?,?,?,?,?,'pilot','new',?,'PENDING','mcc',?,?,?,?)""",
                    (rid, aircraft or '—', flight_number, fault, severity, parking_id, description,
                     severity, pilot_name, pilot_rank, ata_note, fault_info['ata']))
        if ata_note:
            _log_routing_event(conn, rid, 'mcc', pilot_name, ata_note)
        conn.commit()
        conn.close()
        _append_request_created(rid, 'pilot', aircraft, parking_id, flight_number, 'new', 'mcc')
        return jsonify({'success': True, 'request_id': rid,
                        'fault': {'code': fault_info['code'], 'ata': fault_info['ata'],
                                  'skill': fault_info['skill'],
                                  'matched': fault_info['matched']}})

    # source == 'dispatcher'
    aircraft = (data.get('aircraft') or '').strip()
    parking_id = str(data.get('parking_id') or '')
    if not aircraft or not parking_id:
        conn.close()
        return jsonify({'success': False, 'error': 'Не указан борт или стоянка'}), 400

    flight_number = (data.get('flight_number') or '').strip()
    raw_fault = data.get('fault_type') or data.get('description') or 'other'
    fault_info = classify_fault(raw_fault)
    fault = fault_info['code']
    severity = data.get('severity', 'medium')
    description = data.get('description') or str(raw_fault)
    ata_note = ('ATA ' + fault_info['ata']) if fault_info['ata'] else ''
    pilot_name = (data.get('pilot_name') or '').strip() or None
    pilot_rank = (data.get('pilot_rank') or '').strip() or None

    rid = 'req_' + str(int(time.time() * 1000))
    target = _parking_point(parking_id) or _location_center()

    cur.execute("""SELECT DISTINCT engineer_id FROM requests
        WHERE status NOT IN ('completed','cancelled','flight_cancelled','deleted')
          AND engineer_id IS NOT NULL""")
    busy = {r['engineer_id'] for r in cur.fetchall()}

    candidates_started = time.perf_counter()
    table = build_candidate_table(conn, fault, target[0], target[1],
                                  severity=severity, exclude_ids=busy,
                                  regulation_minutes=_request_regulation_minutes(rid))
    candidates_ms = round((time.perf_counter() - candidates_started) * 1000.0, 2)
    selected = next((row for row in table if row.get('eligible')), None)
    remember_candidates(rid, table, candidates_ms,
                        selected['engineer_id'] if selected else None)
    append_event('candidates_built', request_id=rid, duration_ms=candidates_ms,
                 selected_engineer=(selected['engineer_id'] if selected else None),
                 candidate_count=len(table))

    if selected:
        cur.execute("""SELECT id, name, device_id, specialization, coords_lat, coords_lng
                       FROM engineers WHERE id=?""", (selected['engineer_id'],))
        row = cur.fetchone()
        eng = dict(row) if row else None
    else:
        eng = None

    if eng:
        cur.execute("""INSERT INTO requests (id, aircraft, flight_number, fault_type, severity,
            parking_id, description, source, status, criticality, mcc_status, routing_stage,
            engineer_id, engineer_name, travel_time, using_car, walk_to_car, car_time, best_car,
            pilot_name, pilot_rank, mcc_comment, mcc_ata)
            VALUES (?,?,?,?,?,?,?,?,'assigned',?,'PENDING','technician',?,?,?,?,?,?,?,?,?,?,?)""",
                    (rid, aircraft, flight_number, fault, severity, parking_id, description,
                     source, severity,
                     eng['id'], eng['name'],
                     0, 0, 0, 0, None, pilot_name, pilot_rank,
                     ata_note, fault_info['ata']))
        cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                    ('ВС ' + aircraft, eng['id']))
        if ata_note:
            _log_routing_event(conn, rid, 'technician', eng['name'], ata_note)
        conn.commit()
        conn.close()
        _append_request_created(rid, source, aircraft, parking_id, flight_number,
                                'assigned', 'technician')
        append_event('assigned', request_id=rid, engineer_id=eng['id'],
                     engineer=eng['device_id'], duration_ms=candidates_ms,
                     score=selected.get('score'), reason=selected.get('reason'))
        # Маршрут-превью должен появиться сразу после назначения инженера.
        # Не ждём ни подтверждения диспетчера, ни ответа самого инженера.
        _recalc_route_for_request_id(rid, force=True)
        route_conn = get_db()
        route_cur = route_conn.cursor()
        route_cur.execute("SELECT travel_time FROM requests WHERE id=?", (rid,))
        saved_route = route_cur.fetchone()
        route_conn.close()
        final_time = float(saved_route['travel_time'] or 0) if saved_route else 0

        return jsonify({
            'success': True, 'request_id': rid,
            'selected': {
                'request_id': rid,
                'engineer': {
                    'id': eng['id'], 'name': eng['name'],
                    'device_id': normalize_device_id(eng['device_id'],
                                                     get_engineer_number_from_device(eng['device_id'])),
                    'specialization': eng['specialization'],
                    'coords_lat': eng['coords_lat'],
                    'coords_lng': eng['coords_lng']
                },
                'travel_time': final_time,
                'using_car': selected.get('using_car', False),
                'walk_time': selected.get('walk_time', 0),
                'walk_to_car': selected.get('walk_to_car', 0),
                'car_time': selected.get('car_time', 0),
                'best_car': selected.get('best_car'),
                'path': selected.get('path'),
                'score': selected.get('score'),
                'reason': selected.get('reason')
            },
            'fault': {'code': fault_info['code'], 'ata': fault_info['ata'],
                      'skill': fault_info['skill'], 'matched': fault_info['matched']},
            'candidates': table
        })

    cur.execute("""INSERT INTO requests (id, aircraft, flight_number, fault_type, severity,
        parking_id, description, source, status, criticality, mcc_status, routing_stage,
        pilot_name, pilot_rank, delay_started_at, delay_reason, mcc_comment, mcc_ata)
        VALUES (?,?,?,?,?,?,?,?,'queued',?,'PENDING','dispatcher',?,?,CURRENT_TIMESTAMP,'Ожидание инженера',?,?)""",
                (rid, aircraft, flight_number, fault, severity, parking_id, description,
                 source, severity, pilot_name, pilot_rank, ata_note, fault_info['ata']))
    if ata_note:
        _log_routing_event(conn, rid, 'dispatcher', 'Диспетчер', ata_note)
    conn.commit()
    conn.close()
    _append_request_created(rid, source, aircraft, parking_id, flight_number,
                            'queued', 'dispatcher')
    _schedule_recheck(rid)
    return jsonify({'success': True, 'queued': True, 'request_id': rid,
                    'message': 'Заявка в очереди — нет свободных специалистов',
                    'fault': {'code': fault_info['code'], 'ata': fault_info['ata'],
                              'skill': fault_info['skill'], 'matched': fault_info['matched']},
                    'candidates': table})


# ============================================================
# /api/route
# ============================================================
@app.route('/api/route', methods=['POST'])
def compute_route():
    data = request.get_json() or {}

    # Новый формат: agent_type + waypoints + target.
    if data.get('agent_type'):
        options = {
            'use_transport': bool(data.get('use_transport')),
            'car_lat': data.get('car_lat'),
            'car_lng': data.get('car_lng'),
        }
        try:
            result = build_route(
                data.get('agent_type'),
                data.get('start') or {'lat': data.get('start_lat'), 'lng': data.get('start_lng')},
                data.get('waypoints') or [],
                data.get('target') or {'lat': data.get('end_lat'), 'lng': data.get('end_lng')},
                options,
            )
        except RouteError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 422
        return jsonify({'success': True, **result})

    try:
        start_lat = float(data.get('start_lat'))
        start_lng = float(data.get('start_lng'))
        end_lat = float(data.get('end_lat'))
        end_lng = float(data.get('end_lng'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'start/end обязательны'}), 400

    car_lat = data.get('car_lat')
    car_lng = data.get('car_lng')

    if car_lat is not None and car_lng is not None:
        try:
            car_lat = float(car_lat)
            car_lng = float(car_lng)
        except (TypeError, ValueError):
            car_lat = car_lng = None

    if car_lat is not None and car_lng is not None:
        walk_path, _ = get_shortest_path(_routing.road_graph_vehicle,
                                          start_lat, start_lng, car_lat, car_lng)
        if walk_path is None:
            walk_path = [[start_lat, start_lng], [car_lat, car_lng]]
        car_path, car_dist = get_shortest_path(_routing.road_graph_vehicle,
                                               car_lat, car_lng, end_lat, end_lng)
        if car_path is None:
            car_path = [[car_lat, car_lng], [end_lat, end_lng]]
        combined = walk_path + car_path[1:]
        total = 0.0
        for i in range(len(combined) - 1):
            total += distance_km(combined[i][0], combined[i][1],
                                 combined[i + 1][0], combined[i + 1][1])
        return jsonify({'success': True, 'path': combined,
                        'distance': round(total, 3), 'fallback': False})

    path, dist = get_shortest_path(_routing.road_graph_vehicle,
                                    start_lat, start_lng, end_lat, end_lng)
    if path is None:
        return jsonify({'success': True, 'path': None, 'distance': 0, 'fallback': True})
    return jsonify({'success': True, 'path': path,
                    'distance': round(dist, 3), 'fallback': False})


# ============================================================
# Отмена / Отказ / Задержки
# ============================================================
@app.route('/api/requests/<rid>/cancel', methods=['POST'])
def cancel_request(rid):
    data = request.get_json() or {}
    engineer_id = str(data.get('engineer_id') or '').strip()
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({'success': False, 'error': 'Укажите причину'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id,aircraft,fault_type,severity,terminal,parking_id,
        description,engineer_id,engineer_name,status,rejected_by,created_at,
        source,pilot_name
        FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    if row['status'] == 'completed':
        conn.close()
        return jsonify({'success': False, 'error': 'Уже выполнена'}), 400
    if row['status'] == 'cancelled':
        conn.close()
        return jsonify({'success': False, 'error': 'Уже отменена'}), 400

    # Критерий 14: пилот и инженер отменяют только свои заявки.
    if AUTH_REQUIRED:
        role = session.get('role')
        if role == 'engineer':
            own = str(session.get('user_id') or '')
            if row['engineer_id'] and str(row['engineer_id']) != own:
                append_event('role_denied', path=request.path, method=request.method,
                             role=role, reason='foreign_engineer')
                conn.close()
                return jsonify({'success': False,
                                'error': 'Инженер может отменить только свою заявку'}), 403
        elif role == 'pilot':
            if (row['source'] == 'pilot' and row['pilot_name']
                    and str(row['pilot_name']) != str(session.get('name') or '')):
                append_event('role_denied', path=request.path, method=request.method,
                             role=role, reason='foreign_pilot')
                conn.close()
                return jsonify({'success': False,
                                'error': 'Пилот может отменить только свою заявку'}), 403

    confirmed_by = str(data.get('confirmed_by') or '').strip()
    # Отмена уже начатой работы требует подтверждения старшего смены.
    if row['status'] in ('in_progress', 'working') and not confirmed_by:
        conn.close()
        return jsonify({'success': False,
                        'error': 'Для отмены заявки в работе требуется '
                                 'подтверждение диспетчера/MCC (confirmed_by)'}), 400

    who_id = engineer_id or row['engineer_id']
    who_name = row['engineer_name'] or ''
    if AUTH_REQUIRED and session.get('role') == 'pilot':
        who_name = session.get('name') or who_name
    if who_id:
        cur.execute("SELECT name FROM engineers WHERE id=?", (who_id,))
        e = cur.fetchone()
        if e and e['name']:
            who_name = e['name']
    old_eng = row['engineer_id']
    if old_eng:
        cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?", (old_eng,))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))

    rej_prev = {x.strip() for x in str(row['rejected_by']).split(',') if x.strip()}
    if who_id:
        rej_prev.add(str(who_id))
    rej_str = ','.join(sorted(rej_prev))

    cur.execute("""UPDATE requests SET status='cancelled', cancel_reason=?,
        cancelled_by_name=?, cancelled_at=CURRENT_TIMESTAMP, rejected_by=?,
        routing_stage='done', all_rejected=0 WHERE id=?""",
                (reason, who_name, rej_str, rid))
    note = reason + (f'; подтвердил: {confirmed_by}' if confirmed_by else '')
    _log_routing_event(conn, rid, 'cancelled', _request_actor(), note,
                       confirmed_by=confirmed_by)
    conn.commit()
    conn.close()
    append_event('status_changed', request_id=rid, previous_status=row['status'],
                 status='cancelled', actor=_request_actor(),
                 reason=reason, confirmed_by=confirmed_by)
    forget_candidates(rid)
    # Переназначение — отдельное действие диспетчера: автоматически никого
    # не назначаем, заявка просто закрыта.
    return jsonify({
        'success': True, 'request_id': rid,
        'cancelled_by_name': who_name, 'reason': reason,
        'reassigned': False, 'cancelled': True
    })


@app.route('/api/requests/<rid>/reject', methods=['POST'])
def reject_request(rid):
    data = request.get_json() or {}
    body_engineer_id = str(data.get('engineer_id') or '')
    if AUTH_REQUIRED and session.get('role') == 'engineer':
        session_engineer_id = str(session.get('user_id') or '')
        if body_engineer_id and body_engineer_id != session_engineer_id:
            append_event('role_denied', path=request.path, method=request.method,
                         role='engineer', reason='body_engineer_mismatch')
            return jsonify({'success': False,
                            'error': 'Инженер может отклонить только своё назначение'}), 403
        rejected_by = session_engineer_id
    else:
        rejected_by = body_engineer_id
    reason = (data.get('reason') or '').strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id,aircraft,fault_type,severity,terminal,parking_id,
        description,engineer_id,rejected_by,status,routing_stage FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    if row['status'] == 'completed':
        conn.close()
        return jsonify({'success': False, 'error': 'Уже выполнена'}), 400
    if row['status'] != 'assigned' or row['routing_stage'] != 'technician':
        conn.close()
        return jsonify({'success': False, 'error': 'Отклонить можно только новое назначение до принятия'}), 409
    if not reason:
        conn.close()
        return jsonify({'success': False, 'error': 'Укажите причину отказа'}), 400
    if not rejected_by:
        rejected_by = str(row['engineer_id'] or '')
    if row['engineer_id'] and rejected_by != str(row['engineer_id']):
        conn.close()
        return jsonify({'success': False, 'error': 'Назначение принадлежит другому инженеру'}), 403
    if row['engineer_id']:
        cur.execute("SELECT task_step FROM engineers WHERE id=?", (row['engineer_id'],))
        current_engineer = cur.fetchone()
        if current_engineer and current_engineer['task_step'] not in ('pending', 'idle', '', None):
            conn.close()
            return jsonify({'success': False, 'error': 'Назначение уже принято; отклонить его нельзя'}), 409

    old_eng = row['engineer_id']
    rej_prev = {x.strip() for x in str(row['rejected_by']).split(',') if x.strip()}
    if rejected_by:
        rej_prev.add(rejected_by)
    rej_str = ','.join(sorted(rej_prev))
    if old_eng:
        set_engineer_free(conn, old_eng)
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))

    # Переназначение — отдельное действие диспетчера. Отказ не назначает
    # следующего инженера автоматически, заявка возвращается в очередь.
    cur.execute("""UPDATE requests SET status='queued', engineer_id=NULL,
        engineer_name=NULL, engineer_comment=NULL, travel_time=0, using_car=0,
        walk_to_car=0, car_time=0, best_car=NULL, rejected_by=?,
        all_rejected=0, routing_stage='dispatcher',
        delay_started_at=COALESCE(delay_started_at,CURRENT_TIMESTAMP),
        delay_reason='Ожидание решения диспетчера после отказа'
        WHERE id=?""", (rej_str, rid))
    _log_routing_event(conn, rid, 'dispatcher', _request_actor(),
                       f'Инженер {rejected_by} отказался: {reason}')
    conn.commit()
    conn.close()
    append_event('status_changed', request_id=rid, previous_status='assigned',
                 status='queued', actor='engineer', engineer_id=rejected_by,
                 note=reason)
    append_event('rejected', request_id=rid, previous_engineer=old_eng,
                 engineer_id=rejected_by, reason=reason,
                 reassigned=False)
    forget_candidates(rid)
    return jsonify({'success': True, 'reassigned': False, 'queued': True,
                    'status': 'queued'})


def _schedule_recheck(rid):
    def worker():
        time.sleep(RECHECK_DELAY_SEC)
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT status, flight_cancelled FROM requests WHERE id=?", (rid,))
            r = cur.fetchone()
            conn.close()
            if r and r['status'] == 'queued' and not r['flight_cancelled']:
                try_reassign_queued()
        except Exception as e:
            print(f'[RECHECK] {e}')
    threading.Thread(target=worker, daemon=True).start()


@app.route('/api/requests/<rid>/approve_delay', methods=['POST'])
def approve_delay(rid):
    data = request.get_json() or {}
    minutes = int(data.get('minutes') or 0)
    reason = (data.get('reason') or '').strip()
    dispatcher = (data.get('dispatcher_name') or 'Диспетчер').strip()
    if minutes <= 0 or minutes > 720:
        return jsonify({'success': False, 'error': 'Некорректная длительность'}), 400
    if not reason:
        return jsonify({'success': False, 'error': 'Укажите причину'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, flight_cancelled FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    if row['flight_cancelled']:
        conn.close()
        return jsonify({'success': False, 'error': 'Рейс уже отменён'}), 400
    cur.execute("""UPDATE requests SET delay_minutes=?, delay_reason=?,
        delay_approved_by=?, delay_started_at=COALESCE(delay_started_at,CURRENT_TIMESTAMP)
        WHERE id=?""", (minutes, reason, dispatcher, rid))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid,
                    'delay_minutes': minutes, 'delay_reason': reason,
                    'delay_approved_by': dispatcher})


@app.route('/api/requests/<rid>/cancel_flight', methods=['POST'])
def cancel_flight(rid):
    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()
    dispatcher = (data.get('dispatcher_name') or 'Диспетчер').strip()
    if not reason:
        return jsonify({'success': False, 'error': 'Укажите причину'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,aircraft,engineer_id,flight_cancelled,status FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    if row['flight_cancelled']:
        conn.close()
        return jsonify({'success': False, 'error': 'Рейс уже отменён'}), 400
    eng = row['engineer_id']
    if eng:
        other = engineer_has_active_requests(conn, eng, rid)
        if other == 0:
            cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?", (eng,))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    _archive_requests_before_delete(cur, 'id=?', (rid,), reason='Удалена при отмене рейса')
    cur.execute("UPDATE requests SET flight_cancelled=1, flight_cancel_reason=?, flight_cancelled_at=CURRENT_TIMESTAMP WHERE id=?", (reason, rid))
    conn.commit()
    conn.close()
    try_reassign_queued()
    return jsonify({'success': True, 'request_id': rid,
                    'flight_cancelled': True, 'deleted': True,
                    'reason': reason, 'cancelled_by': dispatcher})


# ============================================================
# Complete + Clear
# ============================================================
@app.route('/api/complete/<eng_id>', methods=['POST'])
def complete_engineer(eng_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id FROM requests WHERE engineer_id=?
        AND status NOT IN ('completed','cancelled','flight_cancelled','deleted')""", (eng_id,))
    if cur.fetchall():
        conn.close()
        return jsonify({'success': False, 'reason': 'has_active_requests'})
    cur.execute("""UPDATE transport SET assigned_to=NULL
        WHERE assigned_to IN (SELECT id FROM requests WHERE engineer_id=?)""", (eng_id,))
    cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?", (eng_id,))
    conn.commit()
    conn.close()
    try_reassign_queued()
    return jsonify({'success': True})


@app.route('/api/clear_all_requests', methods=['POST'])
def clear_all_requests():
    conn = get_db()
    cur = conn.cursor()
    try:
        deleted = _archive_requests_before_delete(cur, "status NOT IN ('draft','deleted')", reason='Массовая очистка заявок')
        cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL")
        cur.execute("UPDATE transport SET assigned_to=NULL")
        conn.commit()
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        conn.rollback()
        print(f'[API] {request.path}: {e}')
        return jsonify({'success': False, 'error': 'Внутренняя ошибка сервера'}), 500
    finally:
        conn.close()


@app.route('/api/clear_all_pilot_requests', methods=['POST'])
def clear_all_pilot_requests():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT DISTINCT engineer_id FROM requests WHERE source='pilot' AND engineer_id IS NOT NULL")
        affected_engineers = [r['engineer_id'] for r in cur.fetchall()]
        deleted = _archive_requests_before_delete(cur, "source='pilot' AND status!='deleted'", reason='Очищены заявки пилотов')
        release_engineers_without_active_requests(conn, affected_engineers)
        cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to IN (SELECT id FROM requests WHERE status='deleted')")
        conn.commit()
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        conn.rollback()
        print(f'[API] {request.path}: {e}')
        return jsonify({'success': False, 'error': 'Внутренняя ошибка сервера'}), 500
    finally:
        conn.close()


# ============================================================
# Audio + Transcribe
# ============================================================
@app.route('/api/requests/<rid>/audio', methods=['GET'])
def get_request_audio(rid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT pilot_audio, pilot_audio_mime FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    conn.close()
    if not row or not row['pilot_audio']:
        return jsonify({'error': 'Аудио не найдено'}), 404
    raw = row['pilot_audio']
    mime = row['pilot_audio_mime'] or 'audio/webm'
    b64 = raw.split(',', 1)[1] if raw.startswith('data:') else raw
    try:
        audio_bytes = base64.b64decode(b64)
    except Exception:
        return jsonify({'error': 'Ошибка декодирования'}), 500
    return Response(audio_bytes, mimetype=mime)


_whisper_model_cache = None
_whisper_lock = threading.Lock()


def _get_whisper_model():
    global _whisper_model_cache
    if _whisper_model_cache is not None:
        return _whisper_model_cache
    with _whisper_lock:
        if _whisper_model_cache is not None:
            return _whisper_model_cache
        try:
            import whisper
            _whisper_model_cache = whisper.load_model(WHISPER_MODEL)
            print(f'[WHISPER] {WHISPER_MODEL} загружена')
            return _whisper_model_cache
        except Exception as e:
            print(f'[WHISPER] недоступен: {e}')
            return None


def _parse_pilot_text(text):
    if not text:
        return {}
    out = {}
    m = re.search(r'\b(RA[-\s]?\d{5})\b', text, re.IGNORECASE)
    if m:
        out['aircraft'] = m.group(1).replace(' ', '-').upper()
    for code, syns in FAULT_SYNONYMS.items():
        if any(s in text.lower() for s in syns):
            out['fault_type'] = code
            break
    m = re.search(r'\b(\d{3}[A-Za-zА-Яа-я]?)\b', text)
    if m:
        pid = m.group(1).upper()
        if pid.isdigit() or (len(pid) == 4 and pid[:3].isdigit()):
            out['parking_id'] = pid
    for t in ('B', 'C', 'D', 'E', 'F'):
        if re.search(rf'\b{t}\b', text):
            out['terminal'] = t
            break
    return out


@app.route('/api/requests/<rid>/transcribe', methods=['POST'])
def transcribe_request_audio(rid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, pilot_audio, pilot_audio_mime, pilot_audio_text, "
                "source, pilot_name FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    # Пилот расшифровывает только свою голосовую заявку.
    if (AUTH_REQUIRED and session.get('role') == 'pilot'
            and row['source'] == 'pilot' and row['pilot_name']
            and str(row['pilot_name']) != str(session.get('name') or '')):
        append_event('role_denied', path=request.path, method=request.method,
                     role='pilot', reason='foreign_pilot')
        conn.close()
        return jsonify({'success': False,
                        'error': 'Пилот может расшифровать только свою заявку'}), 403
    if row['pilot_audio_text']:
        conn.close()
        return jsonify({'success': True, 'text': row['pilot_audio_text'],
                        'parsed': _parse_pilot_text(row['pilot_audio_text'])})
    if not row['pilot_audio']:
        conn.close()
        return jsonify({'success': False, 'error': 'no_audio'}), 400

    model = _get_whisper_model()
    if model is None:
        conn.close()
        return jsonify({'success': False, 'error': 'whisper_unavailable'}), 200

    raw = row['pilot_audio']
    b64 = raw.split(',', 1)[1] if raw.startswith('data:') else raw
    try:
        audio_bytes = base64.b64decode(b64)
    except Exception:
        conn.close()
        return jsonify({'success': False, 'error': 'decode_error'}), 500

    mime = row['pilot_audio_mime'] or 'audio/webm'
    ext = '.webm' if 'webm' in mime else ('.mp4' if 'mp4' in mime else '.wav')

    tmp = None
    wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(audio_bytes)
            tmp = f.name
        wav = tmp + '.wav'
        cmd = ['ffmpeg', '-y', '-i', tmp, '-ar', '16000', '-ac', '1',
               '-c:a', 'pcm_s16le', wav]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        except Exception as e:
            print(f'[FFMPEG] {e}')
        audio_for_whisper = wav if os.path.exists(wav) else tmp
        result = model.transcribe(audio_for_whisper, language='ru', fp16=False)
        text = (result.get('text') or '').strip()
        if not text:
            conn.close()
            return jsonify({'success': False, 'error': 'empty'}), 200
        parsed = _parse_pilot_text(text)
        cur.execute("UPDATE requests SET pilot_audio_text=? WHERE id=?", (text, rid))
        if parsed.get('aircraft'):
            cur.execute("UPDATE requests SET aircraft=? WHERE id=?", (parsed['aircraft'], rid))
        if parsed.get('fault_type'):
            cur.execute("UPDATE requests SET fault_type=? WHERE id=?", (parsed['fault_type'], rid))
        if parsed.get('terminal'):
            cur.execute("UPDATE requests SET terminal=? WHERE id=?", (parsed['terminal'], rid))
        if parsed.get('parking_id'):
            cur.execute("UPDATE requests SET parking_id=? WHERE id=?", (parsed['parking_id'], rid))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'text': text, 'parsed': parsed})
    except Exception as e:
        print(f'[TRANSCRIBE] {e}')
        try: conn.close()
        except: pass
        return jsonify({'success': False, 'error': 'transcribe_error'}), 500
    finally:
        for p in (tmp, wav):
            if p and os.path.exists(p):
                try: os.remove(p)
                except: pass


# ============================================================
# Мобильные WebView-приложения и контракт приёма геопозиции
# ============================================================










@app.route('/api/v1/track', methods=['POST'])
def api_v1_track():
    """Единая ручка приёма геопозиции: одна точка или пакет точек."""
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({'accepted': False, 'applied': False, 'obj': None,
                        't': None, 'error': 'Ожидается JSON'}), 400

    if isinstance(data, dict) and isinstance(data.get('points'), list):
        points = data['points']
        if len(points) > 5000:
            return jsonify({'error': 'Слишком большой пакет (максимум 5000 точек)'}), 413
        return jsonify([_track_process_point(point) for point in points])

    if not isinstance(data, dict):
        return jsonify({'accepted': False, 'applied': False, 'obj': None,
                        't': None, 'error': 'Ожидается объект точки или {"points": [...]}'}), 400

    result = _track_process_point(data)
    if not result['accepted']:
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/v1/stream')
def api_v1_stream():
    """Server-Sent Events: все изменения состояния в реальном времени."""
    client_queue = queue.Queue(maxsize=_events._SSE_QUEUE_SIZE)
    with _events._SSE_LOCK:
        _events._SSE_CLIENTS.add(client_queue)

    def generate():
        try:
            yield 'retry: 2000\n\n'
            yield ('event: hello\ndata: ' +
                   json.dumps({'server_time': _utc_now_iso()}, ensure_ascii=False) + '\n\n')
            while True:
                try:
                    payload = client_queue.get(timeout=15)
                    yield payload
                except queue.Empty:
                    yield ': ping\n\n'
        finally:
            with _events._SSE_LOCK:
                _events._SSE_CLIENTS.discard(client_queue)

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no'})






def _agents_with_engineer_assignment():
    """Снапшот агентов + поля задания AT-000N из engineer_assignment (LEFT JOIN)."""
    payload = _agents_snapshot()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT e.id AS engineer_id, a.at_id, a.grade, a.scope,
                              a.skill, a.scope_desc, COALESCE(a.is_live,0) AS is_live
                       FROM engineers e
                       LEFT JOIN engineer_assignment a ON a.engineer_id=e.id""")
        assignments = {str(row['engineer_id']): dict(row) for row in cur.fetchall()}
    finally:
        conn.close()
    for item in payload.get('engineers', []):
        row = assignments.get(str(item.get('id'))) or {}
        for key in ('at_id', 'grade', 'scope', 'skill', 'scope_desc'):
            item[key] = row.get(key)
        item['is_live'] = int(row.get('is_live') or 0)
    return payload


@app.route('/api/v1/agents', methods=['GET'])
def api_v1_agents():
    return jsonify(_agents_with_engineer_assignment())


@app.route('/api/v1/snapshot', methods=['GET'])
def api_v1_snapshot():
    payload = _agents_with_engineer_assignment()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, aircraft, flight_number, parking_id, fault_type,
                              status, routing_stage, engineer_id, engineer_name,
                              travel_time, using_car, best_car, created_at
                       FROM requests
                       WHERE status NOT IN ('draft','deleted','completed','cancelled','flight_cancelled')
                       ORDER BY created_at DESC""")
        payload['requests'] = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
    return jsonify(payload)


@app.route('/api/v1/events', methods=['GET'])
def api_v1_events():
    """Журнал событий (append-only). since — ISO-время, limit — последние N."""
    if AUTH_REQUIRED and session.get('role') not in ('admin', 'dispatcher', 'mcc'):
        return jsonify({'error': 'Недостаточно прав'}), 403
    since = (request.args.get('since') or '').strip() or None
    try:
        limit = int(request.args.get('limit', 500))
    except (TypeError, ValueError):
        limit = 500
    limit = max(1, min(limit, 5000))
    events = read_events(since=since, limit=limit)
    return jsonify({'events': events, 'count': len(events), 'since': since,
                    'journal': os.path.basename(_events.EVENTS_PATH)})


def _fill_events_sheet(ws, events):
    ws.append(['Время (ISO, мс)', 'Тип', 'Заявка', 'Участники', 'Параметры'])
    for event in events:
        participants = ', '.join(str(value) for value in (
            event.get('engineer'), event.get('actor'), event.get('engineer_id'),
            event.get('selected_engineer'), event.get('previous_engineer')) if value)
        params = {key: value for key, value in event.items()
                  if key not in ('at', 'type', 'request_id', 'engineer', 'actor')}
        ws.append([event.get('at', ''), event.get('type', ''),
                   event.get('request_id') or '', participants,
                   json.dumps(params, ensure_ascii=False, default=str)])
    return ws


def _events_workbook(events):
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = 'Журнал'
    _fill_events_sheet(ws, events)
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='17365D')
        cell.alignment = Alignment(vertical='center', wrap_text=True)
    for col_idx in range(1, ws.max_column + 1):
        max_len = 12
        for row_idx in range(1, min(ws.max_row, 400) + 1):
            value = ws.cell(row=row_idx, column=col_idx).value
            if value is not None:
                max_len = max(max_len, min(len(str(value)), 90))
        ws.column_dimensions[get_column_letter(col_idx)].width = max(12, min(max_len + 2, 90))
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


@app.route('/api/v1/events/export.xlsx', methods=['GET'])
def api_v1_events_export():
    if AUTH_REQUIRED and session.get('role') not in ('admin', 'dispatcher', 'mcc'):
        return jsonify({'error': 'Недостаточно прав'}), 403
    try:
        payload = _events_workbook(read_events(limit=0))
    except ImportError:
        return jsonify({'success': False, 'error': 'Установите openpyxl'}), 503
    filename = 'InTrAvia_events_' + datetime.utcnow().strftime('%Y%m%d_%H%M%S') + '.xlsx'
    return Response(payload,
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="' + filename + '"'})


@app.route('/api/v1/events/export.csv', methods=['GET'])
def api_v1_events_export_csv():
    """CSV-экспорт журнала событий (вдобавок к XLSX)."""
    if AUTH_REQUIRED and session.get('role') not in ('admin', 'dispatcher', 'mcc'):
        return jsonify({'error': 'Недостаточно прав'}), 403
    import csv
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output, delimiter=';', lineterminator='\r\n')
    writer.writerow(['Время (ISO, мс)', 'Тип', 'Заявка', 'Участники', 'Параметры'])
    for event in read_events(limit=0):
        participants = ', '.join(str(value) for value in (
            event.get('engineer'), event.get('actor'), event.get('engineer_id'),
            event.get('selected_engineer'), event.get('previous_engineer')) if value)
        params = {key: value for key, value in event.items()
                  if key not in ('at', 'type', 'request_id', 'engineer', 'actor')}
        writer.writerow([event.get('at', ''), event.get('type', ''),
                         event.get('request_id') or '', participants,
                         json.dumps(params, ensure_ascii=False, default=str)])
    payload = '﻿' + output.getvalue()
    filename = 'InTrAvia_events_' + datetime.utcnow().strftime('%Y%m%d_%H%M%S') + '.csv'
    return Response(payload, mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="' + filename + '"'})


@app.route('/m/agents')
def mobile_agents_app():
    return render_template('mobile/agents.html')


# --- Обратная совместимость: графы живут в intravia.routing ---
def _sync_graph_globals():
    global GRAPH_PEDESTRIAN, GRAPH_VEHICLE, GRAPH_AIRCRAFT, road_graph_vehicle
    GRAPH_PEDESTRIAN = _routing.GRAPH_PEDESTRIAN
    GRAPH_VEHICLE = _routing.GRAPH_VEHICLE
    GRAPH_AIRCRAFT = _routing.GRAPH_AIRCRAFT
    road_graph_vehicle = _routing.road_graph_vehicle


def load_road_graph():
    _routing.load_road_graph()
    _sync_graph_globals()


# ============================================================
# Запуск
# ============================================================
def _startup():
    init_db()
    load_road_graph()
    try:
        load_location_config()
        apply_location_parking()
        apply_location_objects()
        prune_stale_map_positions()
    except Exception as e:
        print(f'[STARTUP LOCATION] {e}')
    try:
        _recalc_all_active_routes()
    except Exception as e:
        print(f'[STARTUP ROUTE CLEANUP] {e}')


_startup()

_sync_graph_globals()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)