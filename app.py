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
                   stream_with_context)
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
    print('[LLM] openai не установлен')

# ------------------------------------------------------------
# Конфигурация
# ------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Простой загрузчик .env без внешних зависимостей (не переопределяет окружение)."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding='utf-8-sig') as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
    except OSError as exc:
        print(f'[ENV] Не удалось прочитать {path}: {exc}')


_load_dotenv(os.path.join(BASE_DIR, '.env'))

# INTRAVIA_DB позволяет тестам и стендам работать с отдельной копией базы.
DB_PATH = os.environ.get('INTRAVIA_DB', '').strip() or os.path.join(BASE_DIR, 'database.db')
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
REGULATION_MINUTES = 15
WALK_SPEED_KMH = 5.0
RECHECK_DELAY_SEC = 10

# AUTH_REQUIRED=1 включает серверную проверку сессии и ролей (рекомендуется
# для любого стенда, доступного не только с localhost).
# По умолчанию сервер закрыт сессией. Для демо без входа: AUTH_REQUIRED=0.
AUTH_REQUIRED = os.environ.get('AUTH_REQUIRED', '1').strip().lower() in ('1', 'true', 'yes', 'on')
# CORS по умолчанию выключен: дашборды и мобильные страницы работают с того же origin.
CORS_ORIGINS = [o.strip() for o in os.environ.get('CORS_ORIGINS', '').split(',') if o.strip()]

FFMPEG_PATH = r'C:\Users\deela\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe'
if os.path.exists(FFMPEG_PATH):
    ffmpeg_dir = os.path.dirname(FFMPEG_PATH)
    cp = os.environ.get('PATH', '')
    if ffmpeg_dir not in cp.split(os.pathsep):
        os.environ['PATH'] = ffmpeg_dir + os.pathsep + cp

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


# ------------------------------------------------------------
# Realtime: in-process SSE bus (gunicorn/Flask, один процесс, много потоков)
# ------------------------------------------------------------
_SSE_LOCK = threading.Lock()
_SSE_CLIENTS = set()
_SSE_QUEUE_SIZE = 200

# Последние метрики точки (acc/spd) по агенту. Хранятся в памяти, чтобы не
# менять схему database.db; нужны для /api/v1/agents и SSE.
_TRACK_META_LOCK = threading.Lock()
_TRACK_META = {}


def sse_publish(event, data):
    """Публикует событие всем подключённым SSE-клиентам."""
    payload = ('event: ' + str(event) + '\n' +
               'data: ' + json.dumps(data, ensure_ascii=False, default=str) + '\n\n')
    with _SSE_LOCK:
        dead = []
        for client_queue in _SSE_CLIENTS:
            try:
                client_queue.put_nowait(payload)
            except queue.Full:
                dead.append(client_queue)
        for client_queue in dead:
            _SSE_CLIENTS.discard(client_queue)


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


# ------------------------------------------------------------
# Журнал событий: append-only JSONL (без изменения схемы БД)
# ------------------------------------------------------------
EVENTS_PATH = (os.environ.get('INTRAVIA_EVENTS', '').strip()
               or os.path.join(BASE_DIR, 'data', 'events.jsonl'))
_EVENTS_LOCK = threading.Lock()


def append_event(event_type, **fields):
    """Дописывает событие в data/events.jsonl. Журнал только пополняется."""
    record = {'at': _utc_now_iso(), 'type': str(event_type)}
    record.update(fields)
    try:
        os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _EVENTS_LOCK:
            with open(EVENTS_PATH, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
    except OSError as exc:
        print(f'[EVENTS] {exc}')
    return record


def read_events(since=None, limit=1000):
    """Читает append-only журнал. since — ISO-время, возвращаются события после него."""
    if not os.path.isfile(EVENTS_PATH):
        return []
    events = []
    try:
        with _EVENTS_LOCK:
            with open(EVENTS_PATH, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if since and str(event.get('at') or '') <= str(since):
                        continue
                    events.append(event)
    except OSError as exc:
        print(f'[EVENTS] read: {exc}')
        return []
    if limit and limit > 0:
        events = events[-limit:]
    return events


def _append_request_created(rid, source, aircraft, parking_id, flight_number, status, stage):
    return append_event('request_created', request_id=rid, source=source,
                        aircraft=aircraft, parking_id=parking_id,
                        flight_number=flight_number, status=status,
                        routing_stage=stage)


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
_LOGIN_ATTEMPTS = {}
_LOGIN_LOCK = threading.Lock()
_LOGIN_WINDOW_SEC = 300
_LOGIN_MAX_FAILS = 10


def hash_password(value):
    return generate_password_hash(value)


def is_password_hash(value):
    return isinstance(value, str) and (value.startswith('pbkdf2:') or value.startswith('scrypt:'))


def verify_password(stored, provided):
    """Возвращает (пароль верен, нужна ли миграция из открытого текста)."""
    if not stored or provided is None:
        return False, False
    if is_password_hash(stored):
        try:
            return check_password_hash(stored, provided), False
        except ValueError:
            return False, False
    return secrets.compare_digest(str(stored), str(provided)), True


def _login_key(email):
    return f'{request.remote_addr}|{(email or "").lower()}'


def _login_blocked(key):
    now = time.time()
    with _LOGIN_LOCK:
        fails = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < _LOGIN_WINDOW_SEC]
        _LOGIN_ATTEMPTS[key] = fails
        return len(fails) >= _LOGIN_MAX_FAILS


def _login_record_fail(key):
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.setdefault(key, []).append(time.time())


def _login_clear(key):
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.pop(key, None)


# ------------------------------------------------------------
# Серверная авторизация (включается AUTH_REQUIRED=1)
# ------------------------------------------------------------
PUBLIC_API_PATHS = {
    '/api/auth/login',
    '/api/auth/logout',
    # Контракт трекинга для полевых устройств и мобильных WebView.
    '/api/v1/track',
    '/api/v1/stream',
    '/api/v1/agents',
    '/api/v1/snapshot',
    '/api/auth/session',
    # Публичная география и расчёт маршрута нужны мобильным приложениям
    # для отрисовки карты до входа и для имитатора организаторов.
    '/api/location',
    '/api/parking',
    '/api/route',
}
PUBLIC_PAGES = {'/login.html', '/m/engineer', '/m/agents'}

DASHBOARD_ROLES = {
    '/mcc/dashboard.html': {'mcc'},
    # ЦПУ объединено с MCC: очередь ЦПУ ведёт та же роль.
    '/cpu/dashboard.html': {'mcc'},
    '/dispatcher/dashboard.html': {'dispatcher'},
    '/pilot/dashboard.html': {'pilot'},
    '/engineer/dashboard.html': {'engineer'},
    '/admin/dashboard.html': {'admin'},
}

ROLE_HOME = {
    'admin': '/admin/dashboard.html',
    'mcc': '/mcc/dashboard.html',
    'dispatcher': '/dispatcher/dashboard.html',
    'pilot': '/pilot/dashboard.html',
    'engineer': '/engineer/dashboard.html',
}

# Изменять эти группы API может только соответствующая роль (admin — всегда).
WRITE_ROLE_PREFIXES = (
    ('/api/mcc/', {'mcc'}),
    ('/api/cpu/', {'mcc'}),
    ('/api/dispatcher/', {'dispatcher'}),
    ('/api/pilot/', {'pilot'}),
    ('/api/engineer/', {'engineer'}),
    ('/api/users', {'admin'}),
    ('/api/v1/import/', {'admin'}),
    ('/api/clear_all_requests', {'admin'}),
    ('/api/clear_all_pilot_requests', {'admin'}),
)


@app.before_request
def _enforce_server_auth():
    if not AUTH_REQUIRED:
        return None
    path = request.path
    if path in PUBLIC_API_PATHS or path in PUBLIC_PAGES:
        return None

    if path.startswith('/api/'):
        if not session.get('user_id'):
            append_event('role_denied', path=path, method=request.method,
                         role=None, reason='unauthorized')
            return jsonify({'error': 'Требуется авторизация'}), 401
        role = session.get('role')
        if role == 'admin':
            return None
        for prefix, roles in WRITE_ROLE_PREFIXES:
            if path == prefix or path.startswith(prefix):
                if role not in roles:
                    append_event('role_denied', path=path, method=request.method,
                                 role=role, reason='forbidden')
                    return jsonify({'error': 'Недостаточно прав'}), 403
                break
        return None

    if path in DASHBOARD_ROLES:
        if not session.get('user_id'):
            return redirect('/login.html')
        role = session.get('role')
        if role != 'admin' and role not in DASHBOARD_ROLES[path]:
            return redirect(ROLE_HOME.get(role, '/login.html'))
    return None


@app.after_request
def _security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    # strict-origin-when-cross-origin: путь не утекает, но тайловые серверы
    # (OSM) получают origin в Referer — иначе они отдают плитку «Access blocked».
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'geolocation=(self), microphone=(self), camera=()')
    # API и HTML не кэшируем: после перезапуска сервера браузер
    # не должен показывать старую карту/шаблон.
    if request.path.startswith('/api/') or response.mimetype == 'text/html':
        response.headers.setdefault('Cache-Control', 'no-store')
    return response


@app.errorhandler(404)
def _json_404(e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Endpoint not found',
                        'path': request.path}), 404
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
OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434/v1/')
OLLAMA_API_KEY = os.environ.get('OLLAMA_API_KEY', 'ollama')
LLM_MODEL = os.environ.get('LLM_MODEL', 'qwen2.5:7b')
LLM_ENABLED = os.environ.get('LLM_ENABLED', '1') == '1'
WHISPER_MODEL = os.environ.get('WHISPER_MODEL', 'small')

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
PARKING_COORDS = {}
PARKING_META = {}
MAP_OBJECTS = []

# Динамические зоны безопасности самолётов на рулении (без изменения схемы БД).
# Координаты обновляются диспетчерской картой и используются для блокировки
# перемещения инженеров и спецтранспорта через занятый самолётом коридор.
AIRCRAFT_TAXI_RESERVATIONS = {}
AIRCRAFT_TAXI_LOCK = threading.RLock()
AIRCRAFT_TAXI_SAFETY_METERS = 45.0


def _distance_meters(lat1, lng1, lat2, lng2):
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _point_segment_distance_m(lat, lng, a, b):
    # Локальная плоская проекция достаточна для коротких рулёжных сегментов.
    scale_x = 111320.0 * math.cos(math.radians(lat))
    scale_y = 110540.0
    px, py = (lng - lng) * scale_x, 0.0
    ax, ay = (float(a[1]) - lng) * scale_x, (float(a[0]) - lat) * scale_y
    bx, by = (float(b[1]) - lng) * scale_x, (float(b[0]) - lat) * scale_y
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / denom))
    return math.hypot(ax + t * dx, ay + t * dy)


def _aircraft_taxi_conflict(lat, lng):
    with AIRCRAFT_TAXI_LOCK:
        for reservation in AIRCRAFT_TAXI_RESERVATIONS.values():
            route = reservation.get('route') or []
            if len(route) == 1 and _distance_meters(lat, lng, route[0][0], route[0][1]) < AIRCRAFT_TAXI_SAFETY_METERS:
                return reservation.get('aircraft') or 'самолёт'
            for idx in range(len(route) - 1):
                if _point_segment_distance_m(lat, lng, route[idx], route[idx + 1]) < AIRCRAFT_TAXI_SAFETY_METERS:
                    return reservation.get('aircraft') or 'самолёт'
    return None

# Легаси-координаты Шереметьево удалены: география берётся только
# из config/location.json и static/geodata/vdnh.
_PARKING_FALLBACK = {}


def distance_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(d_lng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _normalize_parking_ref(raw):
    if not raw:
        return None
    s = str(raw).strip()
    if re.match(r'^\d{1,4}[A-Za-zА-Яа-я]?$', s):
        return s.upper()
    m = re.search(r'(\d{1,4}[A-Za-zА-Яа-я]?)', s)
    return m.group(1).upper() if m else None


def _color_from_props(props, default='#dc2626'):
    color = (props.get('stroke') or props.get('color')
             or props.get('fill') or props.get('fillColor')
             or default)
    color = str(color)
    if not color.startswith('#'):
        if re.match(r'^[0-9A-Fa-f]{6}$', color):
            color = '#' + color
    return color


def _float_from_props(props, key, default):
    try:
        v = props.get(key)
        if v is not None and str(v).strip() != '':
            return float(v)
    except (TypeError, ValueError):
        pass
    return default


# ------------------------------------------------------------
# Конфигурация локации (ВДНХ) — единственный источник географии
# ------------------------------------------------------------
LOCATION_CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'location.json')
_LOCATION_LOCK = threading.Lock()
_LOCATION_CACHE = None


def _read_geojson(path):
    with open(path, encoding='utf-8-sig') as fh:
        data = json.load(fh)
    if data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
        raise RuntimeError(f'[LOCATION] Некорректный GeoJSON: {path}')
    return data


def _geometry_rings(geometry):
    if not geometry:
        return []
    if geometry.get('type') == 'Polygon':
        return [geometry['coordinates'][0]]
    if geometry.get('type') == 'MultiPolygon':
        return [poly[0] for poly in geometry['coordinates']]
    return []


def _geometry_centroid(geometry):
    points = [pt for ring in _geometry_rings(geometry) for pt in ring]
    if not points:
        return None
    lon = sum(p[0] for p in points) / len(points)
    lat = sum(p[1] for p in points) / len(points)
    return {'lat': round(lat, 6), 'lon': round(lon, 6)}


def _geometry_bbox(geometry):
    points = [pt for ring in _geometry_rings(geometry) for pt in ring]
    if not points:
        return None
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return [round(min(lons), 6), round(min(lats), 6),
            round(max(lons), 6), round(max(lats), 6)]


def _road_stats(features):
    counts, speeds = {}, {}
    for feature in features:
        props = feature.get('properties') or {}
        road_type = props.get('type')
        if not road_type:
            continue
        counts[road_type] = counts.get(road_type, 0) + 1
        if props.get('spd_kmh'):
            speeds.setdefault(road_type, set()).add(float(props['spd_kmh']))
    return {'counts': counts, 'speeds': {k: sorted(v) for k, v in speeds.items()}}


def load_location_config(force=False):
    """Читает config/location.json и привязанные GeoJSON с кэшированием."""
    global _LOCATION_CACHE
    with _LOCATION_LOCK:
        if _LOCATION_CACHE is not None and not force:
            return _LOCATION_CACHE
        if not os.path.isfile(LOCATION_CONFIG_PATH):
            raise RuntimeError(f'[LOCATION] Конфиг не найден: {LOCATION_CONFIG_PATH}')
        with open(LOCATION_CONFIG_PATH, encoding='utf-8-sig') as fh:
            config = json.load(fh)

        required = ('id', 'name', 'center', 'bbox', 'files', 'road_rules',
                    'objects', 'parkings', 'regulation_minutes', 'walk_speed_kmh')
        missing = [key for key in required if key not in config]
        if missing:
            raise RuntimeError('[LOCATION] В конфиге отсутствуют поля: ' + ', '.join(missing))

        paths = {}
        for key in ('roads', 'objects', 'service_zones', 'boundary'):
            rel = (config.get('files') or {}).get(key)
            if not rel:
                raise RuntimeError(f'[LOCATION] Не указан файл {key}')
            full = rel if os.path.isabs(rel) else os.path.join(BASE_DIR, rel)
            if not os.path.isfile(full):
                raise RuntimeError(f'[LOCATION] Файл не найден: {full}')
            paths[key] = full

        roads = _read_geojson(paths['roads'])
        objects_fc = _read_geojson(paths['objects'])
        zones_fc = _read_geojson(paths['service_zones'])
        boundary_fc = _read_geojson(paths['boundary'])

        role_by_name = {o.get('name'): o.get('role') for o in config.get('objects', [])}
        objects = []
        for feature in objects_fc['features']:
            props = feature.get('properties') or {}
            name = props.get('name') or f"object-{props.get('id')}"
            objects.append({
                'id': role_by_name.get(name) or f"object-{props.get('id')}",
                'role': role_by_name.get(name, 'other'),
                'name': name,
                'centroid': _geometry_centroid(feature.get('geometry')),
                'geometry': feature.get('geometry'),
            })

        parkings = []
        for feature in zones_fc['features']:
            props = feature.get('properties') or {}
            zone_id = props.get('id')
            parkings.append({
                'id': f'SZ-{zone_id}',
                'zone_id': zone_id,
                'label': f'Стоянка SZ-{zone_id}',
                'centroid': _geometry_centroid(feature.get('geometry')),
                'geometry': feature.get('geometry'),
            })
        parkings.sort(key=lambda p: p['zone_id'] if isinstance(p['zone_id'], int) else 0)

        boundary = boundary_fc['features'][0].get('geometry') if boundary_fc['features'] else None
        result = dict(config)
        result.update({
            'objects': objects,
            'parkings': parkings,
            'zones': parkings,
            'boundary': boundary,
            'roads': roads,
            'road_stats': _road_stats(roads['features']),
            'bbox': config.get('bbox') or _geometry_bbox(boundary),
        })
        _LOCATION_CACHE = result
        print(f"[LOCATION] {result['id']}: объектов {len(objects)}, стоянок {len(parkings)}, "
              f"дорог {len(roads['features'])}")
        return result


def _location_center():
    try:
        center = load_location_config().get('center') or {}
        return float(center.get('lat', 55.83367)), float(center.get('lon', 37.62709))
    except Exception:
        return 55.83367, 37.62709


def _parking_point(parking_id):
    """Координаты стоянки: SZ-N из config/location.json либо старые id из SVO."""
    pid = str(parking_id or '').strip()
    if not pid:
        return None
    if pid in PARKING_COORDS:
        return PARKING_COORDS[pid]
    try:
        location = load_location_config()
    except Exception:
        return None
    for parking in location.get('parkings', []):
        zone_id = parking.get('zone_id')
        if pid in (str(parking.get('id')), str(zone_id), f'SZ-{zone_id}'):
            centroid = parking.get('centroid') or {}
            if centroid:
                return float(centroid['lat']), float(centroid['lon'])
    return None


def apply_location_parking():
    """Заменяет стоянки SVO на зоны обслуживания ВДНХ (SZ-1..SZ-10)."""
    global PARKING_COORDS, PARKING_META
    try:
        location = load_location_config()
    except Exception as exc:
        print(f'[LOCATION] Стоянки не загружены: {exc}')
        return 0
    PARKING_COORDS = {}
    PARKING_META = {}
    for parking in location.get('parkings', []):
        pid = parking.get('id')
        centroid = parking.get('centroid') or {}
        if not pid or not centroid:
            continue
        lat, lng = float(centroid['lat']), float(centroid['lon'])
        PARKING_COORDS[pid] = (lat, lng)
        PARKING_META[pid] = {
            'id': pid, 'ref': pid,
            'label': parking.get('label') or pid,
            'name': parking.get('label') or pid,
            'lat': lat, 'lng': lng,
            'centroid': {'lat': lat, 'lon': lng},
            'polygon': parking.get('geometry'),
            'geometry': parking.get('geometry'),
            'zone_id': parking.get('zone_id'),
        }
    print(f'[LOCATION] Стоянок ВДНХ: {len(PARKING_META)}')
    return len(PARKING_META)


def apply_location_objects():
    """Заполняет MAP_OBJECTS объектами ВДНХ (вместо легаси svo_all.geojson)."""
    global MAP_OBJECTS
    try:
        location = load_location_config()
    except Exception as exc:
        print(f'[LOCATION] Объекты не загружены: {exc}')
        return 0
    objects = []
    for obj in location.get('objects', []):
        geometry = obj.get('geometry') or {}
        gt = geometry.get('type')
        coords = geometry.get('coordinates') or []
        if gt == 'Polygon':
            rings = coords
        elif gt == 'MultiPolygon':
            rings = [ring for polygon in coords for ring in polygon]
        else:
            rings = []
        latlngs = []
        for ring in rings:
            points = []
            for point in ring:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    try:
                        points.append([float(point[1]), float(point[0])])
                    except (TypeError, ValueError):
                        continue
            if len(points) >= 3:
                latlngs.append(points)
        if not latlngs:
            continue
        objects.append({
            'name': str(obj.get('name') or obj.get('id') or 'Объект'),
            'latlngs': latlngs[0] if len(latlngs) == 1 else latlngs,
            'color': obj.get('color') or '#2563eb',
            'weight': 2,
            'opacity': 0.9,
            'fillOpacity': 0.15,
        })
    MAP_OBJECTS = objects
    print(f'[LOCATION] Объектов ВДНХ: {len(MAP_OBJECTS)}')
    return len(MAP_OBJECTS)


def prune_stale_map_positions():
    """Удаляет позиции агентов вне полигона (легаси-точки Шереметьево).

    Иначе COALESCE(map_object_positions, coords_*) показывает инженеров
    и технику на старых координатах SVO.
    """
    try:
        bbox = load_location_config().get('bbox') or []
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except Exception as exc:
        print(f'[LOCATION] Чистка позиций пропущена: {exc}')
        return 0
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""DELETE FROM map_object_positions
                       WHERE lat IS NULL OR lng IS NULL
                          OR lat < ? OR lat > ? OR lng < ? OR lng > ?""",
                    (min_lat, max_lat, min_lon, max_lon))
        removed = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if removed:
        print(f'[LOCATION] Удалено устаревших позиций вне полигона: {removed}')
    return removed


def _zone_geometry(parking_id):
    pid = str(parking_id or '').strip()
    if not pid:
        return None
    meta = PARKING_META.get(pid)
    if meta and (meta.get('polygon') or meta.get('geometry')):
        return meta.get('polygon') or meta.get('geometry')
    try:
        location = load_location_config()
    except Exception:
        return None
    for parking in location.get('parkings', []):
        zone_id = parking.get('zone_id')
        if pid in (str(parking.get('id')), str(zone_id), f'SZ-{zone_id}'):
            return parking.get('geometry')
    return None


def _point_in_ring(lat, lng, ring):
    inside = False
    x, y = float(lng), float(lat)
    count = len(ring)
    if count < 3:
        return False
    for i in range(count):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % count][0], ring[(i + 1) % count][1]
        if (y1 > y) != (y2 > y):
            intersect_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersect_x:
                inside = not inside
    return inside


def _point_in_geometry(lat, lng, geometry):
    if not geometry:
        return False
    geom_type = geometry.get('type')
    if geom_type == 'Polygon':
        return _point_in_ring(lat, lng, geometry['coordinates'][0])
    if geom_type == 'MultiPolygon':
        return any(_point_in_ring(lat, lng, poly[0])
                   for poly in geometry['coordinates'])
    return False


def _parse_timestamp(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_svo_geojson():
    """Загружает стоянки (Point) и объекты (Polygon/MultiPolygon/LineString)
    из static/svo_all.geojson."""
    global PARKING_COORDS, PARKING_META, MAP_OBJECTS

    PARKING_COORDS = {}
    PARKING_META = {}
    MAP_OBJECTS = []

    filepath = os.path.join(STATIC_DIR, 'svo_all.geojson')
    if not os.path.exists(filepath):
        print(f'[SVO] {filepath} не найден — fallback')
        for ref, (lat, lng) in _PARKING_FALLBACK.items():
            PARKING_COORDS[ref] = (lat, lng)
            PARKING_META[ref] = {'ref': ref, 'name': f'Стоянка {ref}',
                                 'lat': lat, 'lng': lng, 'terminal': ''}
        return

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f'[SVO] Ошибка чтения: {e}')
        for ref, (lat, lng) in _PARKING_FALLBACK.items():
            PARKING_COORDS[ref] = (lat, lng)
        return

    features = data.get('features', [])
    print(f'[SVO] Всего фич: {len(features)}')

    REF_KEYS = ('ref', 'REF', 'Ref', 'id', 'ID', 'Id',
                'number', 'NUMBER', 'Number', 'parking', 'PARKING',
                'parking_id', 'stand', 'STAND', 'name', 'NAME', 'Name',
                'title', 'label', 'code', 'CODE')

    points_count = 0
    polys_count = 0
    lines_count = 0
    skipped = 0

    for feature in features:
        geom = feature.get('geometry') or {}
        props = feature.get('properties') or {}
        gt = geom.get('type')

        # ─── Point → стоянка ───
        if gt == 'Point':
            coords = geom.get('coordinates') or []
            if len(coords) < 2:
                continue
            try:
                lng, lat = float(coords[0]), float(coords[1])
            except (TypeError, ValueError):
                continue

            raw_ref = None
            for k in REF_KEYS:
                v = props.get(k)
                if v is not None and str(v).strip():
                    raw_ref = str(v).strip()
                    break

            if not raw_ref:
                skipped += 1
                continue
            ref = _normalize_parking_ref(raw_ref)
            if not ref:
                skipped += 1
                continue

            PARKING_COORDS[ref] = (lat, lng)
            PARKING_META[ref] = {
                'ref': ref,
                'name': props.get('name') or props.get('NAME') or f'Стоянка {ref}',
                'terminal': props.get('terminal') or props.get('TERMINAL') or '',
                'lat': lat, 'lng': lng,
            }
            points_count += 1

        # ─── Polygon / MultiPolygon → объект ───
        elif gt in ('Polygon', 'MultiPolygon'):
            color = _color_from_props(props, '#dc2626')
            weight = _float_from_props(props, 'stroke-width', 2.0)
            opacity = _float_from_props(props, 'stroke-opacity', 0.95)
            fill_opacity = _float_from_props(props, 'fill-opacity', 0.35)
            if fill_opacity < 0.15:
                fill_opacity = 0.30

            name = (props.get('name') or props.get('NAME')
                    or props.get('description') or props.get('title')
                    or 'Объект')

            # Polygon:      [[ring], ...]              → [ring]
            # MultiPolygon: [[[ring1, ring2]], ...]    → [[ring1, ring2], ...]
            if gt == 'Polygon':
                polygons = [geom.get('coordinates') or []]
            else:
                polygons = geom.get('coordinates') or []

            for poly in polygons:
                if not poly:
                    continue
                rings_latlngs = []
                for ring in poly:
                    if not ring:
                        continue
                    ring_ll = []
                    for c in ring:
                        if isinstance(c, (list, tuple)) and len(c) >= 2:
                            try:
                                ring_ll.append([float(c[1]), float(c[0])])
                            except (TypeError, ValueError):
                                continue
                    if len(ring_ll) >= 3:
                        rings_latlngs.append(ring_ll)

                if not rings_latlngs:
                    continue

                # Leaflet Polygon: 1 кольцо → [ [lat,lng], ... ]
                #                  2+ колец → [ [ [lat,lng], ... ], ... ]
                latlngs = rings_latlngs[0] if len(rings_latlngs) == 1 else rings_latlngs

                MAP_OBJECTS.append({
                    'name': str(name),
                    'latlngs': latlngs,
                    'color': color,
                    'weight': weight,
                    'opacity': opacity,
                    'fillOpacity': fill_opacity,
                })
                polys_count += 1

        # ─── LineString → объект-линия ───
        elif gt == 'LineString':
            coords = geom.get('coordinates') or []
            if len(coords) < 2:
                continue
            color = _color_from_props(props, '#dc2626')
            weight = _float_from_props(props, 'stroke-width', 2.5)
            name = (props.get('name') or props.get('NAME')
                    or props.get('description') or props.get('title')
                    or 'Линия')
            latlngs = []
            for c in coords:
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    try:
                        latlngs.append([float(c[1]), float(c[0])])
                    except (TypeError, ValueError):
                        continue
            if len(latlngs) >= 2:
                MAP_OBJECTS.append({
                    'name': str(name),
                    'latlngs': latlngs,
                    'color': color,
                    'weight': weight,
                    'opacity': 0.95,
                    'fillOpacity': 0,
                    'isLine': True,
                })
                lines_count += 1

    print(f'[SVO] Загружено: стоянок={points_count}, '
          f'полигонов={polys_count}, линий={lines_count}')
    if points_count == 0:
        print('[SVO] ВНИМАНИЕ: точки не найдены — fallback')
        for ref, (lat, lng) in _PARKING_FALLBACK.items():
            PARKING_COORDS[ref] = (lat, lng)
            PARKING_META[ref] = {'ref': ref, 'name': f'Стоянка {ref}',
                                 'lat': lat, 'lng': lng, 'terminal': ''}
    if skipped:
        print(f'[SVO] Пропущено точек без ref: {skipped}')


# ============================================================
# Дорожный граф (ВДНХ): пешеходный, транспортный, авиационный
# ============================================================
GRAPH_PEDESTRIAN = nx.Graph()
GRAPH_VEHICLE = nx.Graph()
GRAPH_AIRCRAFT = nx.Graph()
road_graph_vehicle = GRAPH_VEHICLE  # обратная совместимость

_GRAPH_TYPE_SETS = {
    'pedestrian': {'service', 'pedestrian'},
    'vehicle': {'service'},
    'aircraft': {'taxi', 'runway'},
}


def _build_graph(features, allowed_types, rules, closed_fids, label):
    """Строит граф по рёбрам указанных типов.

    Вес ребра — время в секундах: distance_m / (speed_kmh / 3.6).
    Узлы — (lat, lng), склейка с допуском 3 м.
    """
    graph = nx.Graph()
    SNAP_M = 3.0
    TOL_LAT = SNAP_M / 111320.0
    TOL_LNG = SNAP_M / (111320.0 * math.cos(math.radians(55.8337)))
    grid, cache = {}, {}

    def snap(lat, lng):
        key = (lat, lng)
        if key in cache:
            return cache[key]
        cell = (int(math.floor(lat / TOL_LAT)), int(math.floor(lng / TOL_LNG)))
        best, best_d2 = None, None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for cand in grid.get((cell[0] + di, cell[1] + dj), []):
                    dlat = (lat - cand[0]) / TOL_LAT
                    dlng = (lng - cand[1]) / TOL_LNG
                    d2 = dlat * dlat + dlng * dlng
                    if d2 <= 1.0 and (best is None or d2 < best_d2):
                        best, best_d2 = cand, d2
        if best is None:
            best = key
            grid.setdefault(cell, []).append(best)
        cache[key] = best
        return best

    skipped_closed = 0
    for feature in features:
        geom = feature.get('geometry') or {}
        if geom.get('type') != 'LineString':
            continue
        props = feature.get('properties') or {}
        road_type = str(props.get('type') or '').strip().lower()
        if road_type not in allowed_types:
            continue
        fid = props.get('fid', props.get('id'))
        if fid in closed_fids:
            skipped_closed += 1
            continue
        coords = geom.get('coordinates') or []
        if len(coords) < 2:
            continue
        rule = rules.get(road_type) or {}
        try:
            speed = float(props.get('spd_kmh') or rule.get('default_speed_kmh') or 5.0)
        except (TypeError, ValueError):
            speed = 5.0
        speed = max(speed, 1.0)
        agents = tuple(rule.get('agents') or ())
        snapped = [snap(float(c[1]), float(c[0])) for c in coords]
        for i in range(len(snapped) - 1):
            n1, n2 = snapped[i], snapped[i + 1]
            if n1 == n2:
                continue
            distance_m = distance_km(n1[0], n1[1], n2[0], n2[1]) * 1000.0
            if distance_m <= 0:
                continue
            graph.add_edge(n1, n2,
                           weight=distance_m / (speed / 3.6),
                           distance_m=distance_m,
                           speed_kmh=speed,
                           road_type=road_type,
                           allowed_agents=agents,
                           closed=False)
    if skipped_closed:
        print(f'[GRAPH] {label}: пропущено закрытых рёбер — {skipped_closed}')
    return graph


def _graph_edge_types(graph):
    return {d.get('road_type') for _, _, d in graph.edges(data=True)}


def _self_check_graphs():
    """В пешеходном/транспортном графах не должно быть taxi/runway,
    в авиационном — service/pedestrian."""
    checks = (
        ('пешеходный', GRAPH_PEDESTRIAN, {'taxi', 'runway'}),
        ('транспортный', GRAPH_VEHICLE, {'taxi', 'runway'}),
        ('авиационный', GRAPH_AIRCRAFT, {'service', 'pedestrian'}),
    )
    ok = True
    for label, graph, forbidden in checks:
        bad = _graph_edge_types(graph) & forbidden
        if bad:
            ok = False
            print(f'[GRAPH][CHECK] {label}: запрещённые типы рёбер {sorted(bad)}')
    return ok


def load_road_graph():
    """Загружает три графа из config/location.json (roads.geojson ВДНХ)."""
    global GRAPH_PEDESTRIAN, GRAPH_VEHICLE, GRAPH_AIRCRAFT, road_graph_vehicle
    try:
        location = load_location_config()
        features = location['roads']['features']
        rules = location.get('road_rules') or {}
        closed_fids = set(location.get('closed_edges') or [])
    except Exception as e:
        print(f'[GRAPH] Конфигурация локации недоступна: {e}')
        return

    GRAPH_PEDESTRIAN = _build_graph(
        features, _GRAPH_TYPE_SETS['pedestrian'], rules, closed_fids, 'пешеходный')
    GRAPH_VEHICLE = _build_graph(
        features, _GRAPH_TYPE_SETS['vehicle'], rules, closed_fids, 'транспортный')
    GRAPH_AIRCRAFT = _build_graph(
        features, _GRAPH_TYPE_SETS['aircraft'], rules, closed_fids, 'авиационный')
    road_graph_vehicle = GRAPH_VEHICLE

    for label, graph in (('пешеходный', GRAPH_PEDESTRIAN),
                         ('транспортный', GRAPH_VEHICLE),
                         ('авиационный', GRAPH_AIRCRAFT)):
        print(f'[GRAPH] {label}: {len(graph.nodes)} узлов, '
              f'{len(graph.edges)} рёбер, типы={sorted(_graph_edge_types(graph))}')
    _self_check_graphs()


def _bridge_components(graph, components):
    components = [set(c) for c in components]
    n = len(components)
    if n < 2:
        return
    bridges = []
    for i in range(n):
        for j in range(i + 1, n):
            min_d, min_pair = float('inf'), None
            for na in components[i]:
                for nb in components[j]:
                    d = distance_km(na[0], na[1], nb[0], nb[1])
                    if d < min_d:
                        min_d, min_pair = d, (na, nb)
            if min_pair:
                bridges.append((min_d, i, j, min_pair))
    bridges.sort(key=lambda x: x[0])
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
            return True
        return False

    for d, i, j, pair in bridges:
        if union(i, j):
            graph.add_edge(pair[0], pair[1], weight=d)


def find_nearest_node(graph, lat, lng):
    if graph is None or len(graph.nodes) == 0:
        return None
    nearest, min_d = None, float('inf')
    for node in graph.nodes:
        d = distance_km(lat, lng, node[0], node[1])
        if d < min_d:
            min_d, nearest = d, node
    return nearest


def get_shortest_path(graph, start_lat, start_lng, end_lat, end_lng):
    if graph is None or len(graph.nodes) == 0:
        return None, 0
    s = find_nearest_node(graph, start_lat, start_lng)
    e = find_nearest_node(graph, end_lat, end_lng)
    if not s or not e:
        return None, 0
    try:
        path_nodes = nx.shortest_path(graph, s, e, weight='weight')
        coords = [[start_lat, start_lng]]
        for n in path_nodes:
            coords.append([n[0], n[1]])
        coords.append([end_lat, end_lng])
        total = 0.0
        for i in range(len(coords) - 1):
            total += distance_km(coords[i][0], coords[i][1],
                                 coords[i + 1][0], coords[i + 1][1])
        return coords, total
    except nx.NetworkXNoPath:
        return None, 0
    except Exception as ex:
        print(f'[GRAPH] Ошибка: {ex}')
        return None, 0


# ============================================================
# Маршрутизатор ВДНХ: привязка к рёбрам, участки, промежуточные точки
# ============================================================
class RouteError(Exception):
    pass


_EARTH_M_PER_DEG = 111320.0


def _local_xy(lat, lng, lat0, lng0):
    return ((lng - lng0) * _EARTH_M_PER_DEG * math.cos(math.radians(lat0)),
            (lat - lat0) * _EARTH_M_PER_DEG)


def _edge_projection(graph, lat, lng):
    """Проекция точки на ближайшее ребро графа (в метрах, локальная плоскость)."""
    if graph is None or not graph.edges:
        return None
    best = None
    for u, v, data in graph.edges(data=True):
        ux, uy = _local_xy(u[0], u[1], lat, lng)
        vx, vy = _local_xy(v[0], v[1], lat, lng)
        dx, dy = vx - ux, vy - uy
        length2 = dx * dx + dy * dy
        if length2 <= 1e-9:
            continue
        t = -(ux * dx + uy * dy) / length2
        t = max(0.0, min(1.0, t))
        px, py = ux + t * dx, uy + t * dy
        dist = math.hypot(px, py)
        if best is None or dist < best['distance_m']:
            edge_len = data.get('distance_m') or math.sqrt(length2)
            best = {
                'distance_m': dist,
                'u': u, 'v': v, 't': t,
                'edge_len': edge_len,
                'data': data,
                'lat': u[0] + t * (v[0] - u[0]),
                'lng': u[1] + t * (v[1] - u[1]),
            }
    return best


def _attach_projection(graph, node, projection):
    """Подключает виртуальный узел к концам спроецированного ребра."""
    data = projection['data']
    speed = float(data.get('speed_kmh') or 5.0)
    speed = max(speed, 1.0)
    for endpoint, distance_m in ((projection['u'], projection['t'] * projection['edge_len']),
                                 (projection['v'], (1 - projection['t']) * projection['edge_len'])):
        distance_m = max(distance_m, 0.01)
        graph.add_edge(node, endpoint,
                       weight=distance_m / (speed / 3.6),
                       distance_m=distance_m,
                       speed_kmh=speed,
                       road_type=data.get('road_type'),
                       allowed_agents=data.get('allowed_agents') or (),
                       closed=False)


def route_leg(graph, start, end, mode='engineer'):
    """Маршрут между двумя точками с привязкой к ближайшим рёбрам."""
    if graph is None or not graph.edges:
        raise RouteError('Дорожная сеть не загружена')
    start_lat, start_lng = float(start[0]), float(start[1])
    end_lat, end_lng = float(end[0]), float(end[1])

    sp = _edge_projection(graph, start_lat, start_lng)
    ep = _edge_projection(graph, end_lat, end_lng)
    if not sp or not ep:
        raise RouteError('Не удалось привязать точки к дорожной сети')

    warnings = []
    if sp['distance_m'] > 100:
        warnings.append(f'Старт в {int(sp["distance_m"])} м от дороги')
    if ep['distance_m'] > 100:
        warnings.append(f'Цель в {int(ep["distance_m"])} м от дороги')

    graph_copy = graph.copy()
    start_node = ('__virt__', 'start')
    end_node = ('__virt__', 'end')
    _attach_projection(graph_copy, start_node, sp)
    _attach_projection(graph_copy, end_node, ep)

    # Старт и цель на одном ребре — прямое соединение вдоль него.
    if {sp['u'], sp['v']} == {ep['u'], ep['v']} and sp['u'] != sp['v']:
        direct_m = abs(sp['t'] - ep['t']) * sp['edge_len']
        speed = float(sp['data'].get('speed_kmh') or 5.0)
        graph_copy.add_edge(start_node, end_node,
                            distance_m=max(direct_m, 0.01),
                            weight=max(direct_m, 0.01) / (max(speed, 1.0) / 3.6),
                            speed_kmh=max(speed, 1.0),
                            road_type=sp['data'].get('road_type'),
                            allowed_agents=sp['data'].get('allowed_agents') or (),
                            closed=False)

    try:
        nodes = nx.shortest_path(graph_copy, start_node, end_node, weight='weight')
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        raise RouteError('Маршрут недоступен: дороги не связаны')

    coords = []
    segments = []
    distance_m = 0.0
    time_s = 0.0
    for node in nodes:
        if node == start_node:
            coords.append([start_lat, start_lng])
        elif node == end_node:
            coords.append([end_lat, end_lng])
        else:
            coords.append([node[0], node[1]])
    for u, v in zip(nodes, nodes[1:]):
        data = graph_copy[u][v]
        segment_distance = float(data.get('distance_m') or 0.0)
        segment_time = float(data.get('weight') or 0.0)
        distance_m += segment_distance
        time_s += segment_time
        segments.append({
            'distance_m': round(segment_distance, 1),
            'time_s': round(segment_time, 1),
            'speed_kmh': float(data.get('speed_kmh') or 0.0),
            'road_type': data.get('road_type'),
        })

    return {
        'mode': mode,
        'path': coords,
        'segments': segments,
        'distance_m': round(distance_m, 1),
        'time_s': round(time_s, 1),
        'warnings': warnings,
    }


def _as_point(value):
    if value is None:
        return None
    if isinstance(value, dict):
        lat = value.get('lat', value.get('start_lat'))
        lng = value.get('lng', value.get('lon', value.get('start_lng')))
        if lat is None or lng is None:
            raise RouteError('Точка должна содержать lat и lng')
        return float(lat), float(lng)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return float(value[0]), float(value[1])
    raise RouteError('Некорректный формат точки')


def build_route(agent_type, start, waypoints=None, target=None, options=None):
    """Строит маршрут агента с промежуточными точками и задержками.

    agent_type: engineer | transport | aircraft
    Точки — (lat, lng) или {'lat','lng'}.
    """
    agent_type = str(agent_type or 'engineer').strip().lower()
    if agent_type not in ('engineer', 'transport', 'aircraft'):
        raise RouteError('agent_type должен быть engineer, transport или aircraft')
    options = options or {}
    start_pt = _as_point(start)
    target_pt = _as_point(target)
    if not start_pt or not target_pt:
        raise RouteError('Нужны start и target')

    waypoint_pts = []
    for wp in (waypoints or []):
        point = _as_point(wp)
        if point:
            waypoint_pts.append(point)

    location = load_location_config()
    delays = location.get('delays') or {}
    boarding_s = float(delays.get('boarding_s', 10))
    warehouse_s = float(delays.get('warehouse_s', 30))

    legs = []
    path = []
    warnings = []
    total_distance = 0.0
    total_time = 0.0

    def add_leg(leg, mode):
        nonlocal total_distance, total_time
        legs.append({
            'mode': mode,
            'from': leg['path'][0],
            'to': leg['path'][-1],
            'path': leg['path'],
            'distance_m': leg['distance_m'],
            'time_s': leg['time_s'],
            'speed_kmh': round(
                (leg['distance_m'] / leg['time_s'] * 3.6) if leg['time_s'] else 0.0, 1),
        })
        if not path:
            path.extend(leg['path'])
        else:
            path.extend(leg['path'][1:])
        total_distance += leg['distance_m']
        total_time += leg['time_s']
        warnings.extend(leg['warnings'])

    use_transport = bool(options.get('use_transport'))
    car = None
    if use_transport:
        car_point = _as_point({'lat': options.get('car_lat'), 'lng': options.get('car_lng')}) \
            if options.get('car_lat') is not None and options.get('car_lng') is not None else None
        if car_point:
            car = car_point

    if agent_type == 'engineer' and car:
        add_leg(route_leg(GRAPH_PEDESTRIAN, start_pt, car, mode='walk'), 'walk')
        total_time += boarding_s
        steps = [car] + waypoint_pts + [target_pt]
        for a, b in zip(steps, steps[1:]):
            add_leg(route_leg(GRAPH_VEHICLE, a, b, mode='transport'), 'transport')
        total_time += boarding_s  # высадка
    elif agent_type == 'transport':
        steps = [start_pt] + waypoint_pts + [target_pt]
        for a, b in zip(steps, steps[1:]):
            add_leg(route_leg(GRAPH_VEHICLE, a, b, mode='transport'), 'transport')
    elif agent_type == 'aircraft':
        steps = [start_pt] + waypoint_pts + [target_pt]
        for a, b in zip(steps, steps[1:]):
            add_leg(route_leg(GRAPH_AIRCRAFT, a, b, mode='aircraft'), 'aircraft')
    else:
        steps = [start_pt] + waypoint_pts + [target_pt]
        for a, b in zip(steps, steps[1:]):
            add_leg(route_leg(GRAPH_PEDESTRIAN, a, b, mode='walk'), 'walk')

    total_time += warehouse_s * len(waypoint_pts)

    return {
        'agent_type': agent_type,
        'legs': legs,
        'path': path,
        'distance_m': round(total_distance, 1),
        'distance': round(total_distance / 1000.0, 3),
        'time_s': round(total_time, 1),
        'eta_min': round(total_time / 60.0, 1),
        'waypoints': len(waypoint_pts),
        'warnings': warnings,
    }


# ============================================================
# DB
# ============================================================
def get_db():
    # mode=rw: SQLite must open an existing database; never create an empty file.
    if not os.path.isfile(DB_PATH):
        raise RuntimeError(f'[DB] База данных не найдена: {DB_PATH}. Сервер не создаёт БД автоматически.')
    uri = 'file:' + DB_PATH.replace('\\', '/') + '?mode=rw'
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    return conn


def init_db():
    """Validate the existing database only. Never create, migrate, seed or alter schema/data."""
    print(f'[DB] Подключение к существующей базе: {DB_PATH}')
    if not os.path.isfile(DB_PATH):
        raise RuntimeError(f'[DB] Файл database.db не найден: {DB_PATH}. Создание отключено.')
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing = {row['name'] for row in cur.fetchall()}
        required = {
            'users', 'engineers', 'pilots', 'transport', 'requests',
            'aircraft_registry', 'mel_catalog', 'map_object_positions'
        }
        missing = sorted(required - existing)
        if missing:
            raise RuntimeError(
                '[DB] В существующей базе отсутствуют необходимые таблицы: '
                + ', '.join(missing)
                + '. Сервер не будет создавать или изменять таблицы.'
            )
        print(f'[DB] Проверка завершена. Таблиц найдено: {len(existing)}. Схема не изменялась.')
    finally:
        conn.close()


DEVICE_ID_RE = re.compile(r'^EN-(\d{3})$')


def get_engineer_number_from_device(device_id):
    if not device_id:
        return 0
    m = DEVICE_ID_RE.match(str(device_id))
    return int(m.group(1)) if m else 0


def get_used_numbers(conn):
    cur = conn.cursor()
    cur.execute("SELECT device_id FROM engineers")
    used = set()
    for r in cur.fetchall():
        n = get_engineer_number_from_device(r['device_id'])
        if n > 0:
            used.add(n)
    return used


def next_engineer_number(conn):
    used = get_used_numbers(conn)
    return (max(used) + 1) if used else 1


def normalize_device_id(device_id, number):
    if number > 0:
        return f'EN-{number:03d}'
    return device_id or 'EN-000'


# ============================================================
# Утилиты
# ============================================================
def set_engineer_busy(conn, engineer_id, task_text):
    if not engineer_id:
        return False
    cur = conn.cursor()
    cur.execute("SELECT id FROM engineers WHERE id=?", (engineer_id,))
    if not cur.fetchone():
        return False
    cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                (task_text, engineer_id))
    return cur.rowcount > 0


def set_engineer_free(conn, engineer_id):
    if not engineer_id:
        return False
    cur = conn.cursor()
    cur.execute("SELECT id FROM engineers WHERE id=?", (engineer_id,))
    if not cur.fetchone():
        return False
    cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?",
                (engineer_id,))
    return cur.rowcount > 0


def engineer_has_active_requests(conn, engineer_id, exclude_request_id=None):
    cur = conn.cursor()
    if exclude_request_id:
        cur.execute("""SELECT COUNT(*) FROM requests
            WHERE engineer_id=? AND status NOT IN ('completed','cancelled','flight_cancelled','report_submitted','deleted')
              AND id!=?""", (engineer_id, exclude_request_id))
    else:
        cur.execute("""SELECT COUNT(*) FROM requests
            WHERE engineer_id=? AND status NOT IN ('completed','cancelled','flight_cancelled','report_submitted','deleted')""",
                    (engineer_id,))
    return cur.fetchone()[0]


def release_engineers_without_active_requests(conn, engineer_ids):
    """Освобождает исполнителей после удаления заявок, если других активных задач нет."""
    released = []
    for engineer_id in {str(x) for x in engineer_ids if x not in (None, '')}:
        if engineer_has_active_requests(conn, engineer_id) == 0:
            if set_engineer_free(conn, engineer_id):
                released.append(engineer_id)
    return released


def sync_engineer_availability(conn):
    """Сверяет занятость с активными заявками и освобождает инженеров без задач."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM engineers WHERE status='busy'")
    busy_ids = [row['id'] for row in cur.fetchall()]
    return release_engineers_without_active_requests(conn, busy_ids)


FAULT_SKILL = {
    'engine_failure': 'engine', 'engine_oil': 'engine', 'engine_overheat': 'engine',
    'engine_vibration': 'engine', 'engine_start': 'engine', 'fuel_leak': 'engine',
    'fuel_system': 'engine', 'avionics_nav': 'avionics', 'avionics_radio': 'avionics',
    'avionics_autopilot': 'avionics', 'avionics_transponder': 'avionics',
    'avionics_display': 'avionics', 'avionics_gps': 'avionics', 'hydraulic': 'hydraulics',
    'hydraulic_leak': 'hydraulics', 'hydraulic_pressure': 'hydraulics',
    'electrical': 'electrical', 'electrical_generator': 'electrical',
    'electrical_battery': 'electrical', 'electrical_short': 'electrical',
    'electrical_lighting': 'electrical', 'landing_gear': 'mechanical',
    'pneumatic': 'mechanical', 'brake_system': 'mechanical', 'flap_issue': 'mechanical',
    'aileron_issue': 'mechanical', 'rudder_issue': 'mechanical', 'wing_damage': 'mechanical',
    'fuselage_damage': 'mechanical', 'door_issue': 'mechanical',
    'air_condition': 'general', 'pressurization': 'general', 'oxygen_system': 'general',
    'fire_system': 'general', 'deicing_system': 'general', 'pilot_voice': 'general',
    'other': 'general'
}

FAULT_SYNONYMS = {
    'pressurization': ['разгормат', 'разгерм', 'герметич', 'декомпресс', 'pressur', 'утечк воздух'],
    'engine_failure': ['отказ двиг', 'двигател', 'engine', 'помпаж', 'не тянет'],
    'engine_oil': ['масл', 'oil'],
    'engine_overheat': ['перегрев', 'температур', 'overheat'],
    'engine_vibration': ['вибрац', 'тряс', 'vibrat'],
    'engine_start': ['запуск', 'start', 'не заводится'],
    'fuel_leak': ['утечк топлив', 'течь топлив', 'fuel leak'],
    'hydraulic_leak': ['утечк гидравлик', 'hydraul leak', 'гидравлик'],
    'electrical_short': ['замыкан', 'short', 'кз', 'искр'],
    'landing_gear': ['шасси', 'стойк', 'gear'],
    'brake_system': ['тормоз', 'brake'],
    'avionics_nav': ['навигац', 'курс', 'nav'],
    'avionics_radio': ['связ', 'радио', 'radio'],
    'avionics_autopilot': ['автопилот', 'autopilot'],
    'avionics_transponder': ['транспондер', 'ответчик'],
    'air_condition': ['кондиционер', 'климат', 'air cond'],
    'fire_system': ['пожар', 'fire', 'дым'],
    'door_issue': ['двер', 'door', 'люк'],
    'oxygen_system': ['кислород', 'oxygen'],
    'electrical_lighting': ['освещ', 'свет', 'фар'],
}


def normalize_fault_type(raw):
    if not raw:
        return 'other'
    raw_lower = str(raw).lower().strip()
    if raw_lower in FAULT_SKILL:
        return raw_lower
    for code, synonyms in FAULT_SYNONYMS.items():
        if any(syn in raw_lower for syn in synonyms):
            return code
    return 'other'


def get_free_engineer_for(conn, fault_type, required_skill_map,
                          target_lat=None, target_lng=None, exclude_ids=None,
                          required_skill_override=None):
    if exclude_ids is None:
        exclude_ids = set()
    exclude_ids = {str(x) for x in exclude_ids}
    required_skill = required_skill_override or required_skill_map.get(fault_type, 'general')

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
    pool = [e for e in all_free if e['specialization'] == required_skill]

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
        if travel_min > REGULATION_MINUTES:
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
                          severity='medium', exclude_ids=None):
    """Строит таблицу кандидатов с факторами, оценкой и причинами отказа.

    Учитываются: допуск по профилю дефекта, занятость, время в пути с
    пересадкой на спецтехнику, загрузка за смену и срочность заявки.
    """
    exclude_ids = {str(x) for x in (exclude_ids or set())}
    required_skill = FAULT_SKILL.get(fault_type, 'general')
    cur = conn.cursor()
    cur.execute("""SELECT id, name, device_id, specialization, status,
                          coords_lat, coords_lng
                   FROM engineers ORDER BY device_id""")
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
            'eligible': False,
            'reason': '',
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
        if engineer.get('specialization') != required_skill:
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
        if travel_min > REGULATION_MINUTES:
            item['reason'] = f'не успевает: {travel_min:.1f} мин > {REGULATION_MINUTES}'
            table.append(item)
            continue

        workload = _engineer_shift_load(conn, eid)
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
            'укладывается в 15 мин',
        ]
        item.update({
            'eligible': True,
            'travel_time': round(travel_min, 1),
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


def get_remembered_candidates(request_id):
    with _CANDIDATES_LOCK:
        return _CANDIDATES_CACHE.get(str(request_id))


def choose_transport(engineer, target, waypoints=None, conn=None):
    """Сравнивает пеший маршрут и вариант со спецтранспортом.

    Возвращает {'mode': 'walk'|'transport', 'car': dict|None,
                'route': результат build_route, 'time_s': лучшие секунды}.
    """
    def field(obj, key):
        try:
            return obj[key]
        except (KeyError, IndexError, TypeError):
            return getattr(obj, key, None)

    lat = field(engineer, 'coords_lat')
    lng = field(engineer, 'coords_lng')
    if lat is None or lng is None:
        raise RouteError('У инженера нет координат')
    start = (float(lat), float(lng))
    target_point = _as_point(target)
    waypoint_list = waypoints or []

    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, device_id, lat, lng, speed FROM transport "
                    "WHERE assigned_to IS NULL")
        cars = [dict(r) for r in cur.fetchall()]
    finally:
        if own_conn:
            conn.close()

    walk_route = build_route('engineer', start, waypoint_list, target_point)
    best = {'mode': 'walk', 'car': None, 'route': walk_route,
            'time_s': walk_route['time_s']}

    for car in cars:
        if car.get('lat') is None or car.get('lng') is None:
            continue
        try:
            car_route = build_route(
                'engineer', start, waypoint_list, target_point,
                {'use_transport': True,
                 'car_lat': car['lat'], 'car_lng': car['lng']})
        except RouteError:
            continue
        if car_route['time_s'] < best['time_s']:
            best = {'mode': 'transport', 'car': car,
                    'route': car_route, 'time_s': car_route['time_s']}
    return best


def try_reassign_queued():
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
        eng, route = get_free_engineer_for(conn, q['fault_type'] or 'engine_failure',
                                            FAULT_SKILL, target[0], target[1],
                                            exclude_ids=exclude)
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

# ============================================================
# Пересчёт ROUTE
# ============================================================
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
    if eng['coords_lat'] is None or eng['coords_lng'] is None:
        conn.close()
        return None
    e_lat = float(eng['coords_lat'])
    e_lng = float(eng['coords_lng'])

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
    cars = [c for c in cars if c.get('lat') is not None and c.get('lng') is not None]

    best_route = walk_route
    best_car = None
    for car in cars:
        try:
            car_route = build_route(
                'engineer', (e_lat, e_lng), [], target,
                {'use_transport': True,
                 'car_lat': car['lat'], 'car_lng': car['lng']})
        except RouteError:
            continue
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
                  str(req['parking_id'] or '') in PARKING_COORDS)
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


def _log_routing_event(conn, rid, stage, actor, note=''):
    cur = conn.cursor()
    cur.execute("SELECT routing_history FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        return
    try:
        history = json.loads(row['routing_history'] or '[]')
    except Exception:
        history = []
    history.append({
        'at': datetime.now(timezone.utc).isoformat(),
        'stage': stage,
        'actor': actor,
        'note': note,
    })
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
    cars = [dict(row) for row in cur.fetchall()
            if row['lat'] is not None and row['lng'] is not None]
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
                              r.routing_history, r.status, r.engineer_id,
                              r.engineer_comment, e.task_step,
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
        started = _parse_timestamp(row['inspection_assigned_at'])
        if not started:
            return
        elapsed_min = round(
            (datetime.now(timezone.utc) - started).total_seconds() / 60.0, 1)
        remaining_min = round(REGULATION_MINUTES - elapsed_min, 1)
        eta_min = _route_eta_min(row['engineer_comment'])

        if remaining_min <= 0:
            if _history_has_stage(row['routing_history'], 'breach'):
                return
            note = (f'Норматив {REGULATION_MINUTES} мин нарушен: '
                    f'{elapsed_min} мин с назначения')
            _log_routing_event(conn, request_id, 'breach', 'Система', note)
            conn.commit()
            payload = {
                'request_id': request_id, 'zone': row['parking_id'],
                'engineer': row['engineer_name'], 'breach': True,
                'elapsed_min': elapsed_min, 'remaining_min': remaining_min,
                'eta_min': eta_min, 'regulation_minutes': REGULATION_MINUTES,
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
        note = (f'До норматива {remaining_min} мин; ETA {eta_min if eta_min is not None else "—"} мин')
        if nearest:
            note += (f'; ближайший свободный {nearest["device_id"] or nearest["name"]} '
                     f'{nearest["eta_min"]} мин')
            nearest['can_make_it'] = nearest['eta_min'] <= remaining_min
            can_make_it = nearest['can_make_it']
        _log_routing_event(conn, request_id, 'warning', 'Система', note)
        conn.commit()
        payload = {
            'request_id': request_id, 'zone': row['parking_id'],
            'engineer': row['engineer_name'], 'breach': False,
            'elapsed_min': elapsed_min, 'remaining_min': remaining_min,
            'eta_min': eta_min, 'nearest_free': nearest,
            'can_make_it': can_make_it,
            'regulation_minutes': REGULATION_MINUTES, 'at': _utc_now_iso(),
        }
        append_event('warning', **payload)
        sse_publish('warning', payload)
    except Exception as exc:
        print(f'[REGULATION] {request_id}: {exc}')
    finally:
        conn.close()


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
    return render_template('dispatcher/dashboard.html')


@app.route('/engineer/dashboard.html')
def engineer_dashboard():
    return render_template('engineer/dashboard.html')


@app.route('/pilot/dashboard.html')
def pilot_dashboard():
    return render_template('pilot/dashboard.html')


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
    keys = sorted(PARKING_META.keys(), key=sort_key)
    return jsonify([PARKING_META[k] for k in keys])


@app.route('/api/map-objects', methods=['GET'])
def api_map_objects():
    return jsonify(MAP_OBJECTS)


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
    with AIRCRAFT_TAXI_LOCK:
        AIRCRAFT_TAXI_RESERVATIONS.clear()
        AIRCRAFT_TAXI_RESERVATIONS.update(updated)
    return jsonify({'success': True, 'active': len(updated)})


@app.route('/api/map/object-positions', methods=['GET'])
def api_map_object_positions():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT object_type,object_id,lat,lng,updated_at
                   FROM map_object_positions ORDER BY object_type,object_id""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/restricted-zones', methods=['GET'])
def api_restricted_zones_legacy():
    return jsonify(MAP_OBJECTS)


@app.route('/api/map-debug', methods=['GET'])
def api_map_debug():
    polys = sum(1 for o in MAP_OBJECTS if not o.get('isLine'))
    lines = sum(1 for o in MAP_OBJECTS if o.get('isLine'))
    return jsonify({
        'parking_count': len(PARKING_COORDS),
        'object_count': len(MAP_OBJECTS),
        'objects_polygons': polys,
        'objects_lines': lines,
        'sample_parking': list(PARKING_META.values())[:3],
        'sample_objects': MAP_OBJECTS[:2],
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
    engineer_id = str(data.get('engineer_id') or '')
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
        'AOG установлен инженером: полёт запрещён. Отчёт передан в ЦПУ.' if preliminary_verdict == 'AOG' else
        ('Инженер сопоставил дефект с пунктом MEL ' + str(mel_values['mcc_mel_ref']) +
         '. Условная эксплуатация не активна; ожидаются проверка ЦПУ и решение MCC.' if preliminary_verdict == 'MEL' else
         'Инженер завершил диагностику. Отчёт передан в ЦПУ; полёт запрещён до решения MCC и отдельного допуска.')
    )
    # При передаче отчёта в ЦПУ маршрут инженера удаляется; в комментарии остаётся только отчёт.
    cur.execute("""UPDATE requests SET
        status='report_submitted', routing_stage='cpu_report',
        engineer_condition=?, engineer_verdict=?, engineer_verdict_at=CURRENT_TIMESTAMP,
        engineer_report_at=CURRENT_TIMESTAMP, cpu_report_at=CURRENT_TIMESTAMP,
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
    event_text = 'Техник установил результат «' + preliminary_verdict + '». Отчёт передан в ЦПУ. Состояние: ' + condition
    if preliminary_verdict == 'MEL':
        event_text += ' · выбран пункт ' + str(mel_values['mcc_mel_ref']) + ' · Cat ' + str(mel_values['mcc_mel_category'])
    _log_routing_event(conn, rid, 'cpu_report', 'Инженер', event_text)
    conn.commit()
    conn.close()
    _recalc_route_for_request_id(rid, force=True)
    # Инженер освобождён: сразу запускаем автоподбор для ожидающих заявок.
    try:
        try_reassign_queued()
    except Exception as exc:
        print('[ENGINEER REPORT] queued auto-assign:', exc)
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'cpu_report',
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
    cur.execute("""SELECT id, aircraft, parking_id, fault_type, routing_stage
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
                                      severity=row['severity'] or 'medium')
        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        remember_candidates(rid, table, duration_ms)
        conn.commit()
        return jsonify(get_remembered_candidates(rid))
    finally:
        conn.close()


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
                   COALESCE(p.lat,e.coords_lat) AS coords_lat,
                   COALESCE(p.lng,e.coords_lng) AS coords_lng,
                   e.status,e.task,e.phone,e.task_step,e.task_started_at
                   FROM engineers e LEFT JOIN map_object_positions p
                     ON p.object_type='engineer' AND p.object_id=e.id""")
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
                   COALESCE(p.lat,e.coords_lat) AS coords_lat,
                   COALESCE(p.lng,e.coords_lng) AS coords_lng,
                   e.status,e.task,e.phone,e.task_step,e.task_started_at
                   FROM engineers e LEFT JOIN map_object_positions p
                     ON p.object_type='engineer' AND p.object_id=e.id
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
    engineer_id = str(data.get('engineer_id') or '').strip()
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
    conn.commit()
    conn.close()
    _recalc_routes_for_engineer(eng_id, force=True)
    return jsonify({'success': True})


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
    sql += " ORDER BY r.created_at DESC"

    cur.execute(sql, params)
    res = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in res:
        try:
            r['routing_history'] = json.loads(r['routing_history'] or '[]')
        except Exception:
            r['routing_history'] = []
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

    cur.execute("SELECT routing_stage FROM requests WHERE id=?", (rid,))
    current = cur.fetchone()
    if data.get('status') == 'completed' and current and current['routing_stage'] in ('technician','dispatcher','dispatcher_report','cpu_report','mcc_finalize'):
        conn.close()
        return jsonify({'success': False, 'error': 'Нельзя закрыть техническую задачу вручную. Инженер отправляет отчёт, затем он проходит диспетчера, ЦПУ и MCC.'}), 409

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

    new_status = data.get('status')
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
# DISPATCH — создание заявки
# ============================================================
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
            return jsonify({'success': True, 'request_id': rid})

        parking_id = str(data.get('parking_id') or '')
        if not parking_id:
            conn.close()
            return jsonify({'success': False, 'error': 'Не указана стоянка'}), 400
        flight_number = (data.get('flight_number') or '').strip()
        fault = normalize_fault_type(data.get('fault_type', 'other'))
        description = data.get('description', fault)
        severity = 'medium'

        rid = 'req_' + str(int(time.time() * 1000))
        cur.execute("""INSERT INTO requests (id, aircraft, flight_number, fault_type, severity,
            parking_id, description, source, status, criticality, mcc_status, routing_stage,
            pilot_name, pilot_rank)
            VALUES (?,?,?,?,?,?,?,'pilot','new',?,'PENDING','mcc',?,?)""",
                    (rid, aircraft or '—', flight_number, fault, severity, parking_id, description,
                     severity, pilot_name, pilot_rank))
        conn.commit()
        conn.close()
        _append_request_created(rid, 'pilot', aircraft, parking_id, flight_number, 'new', 'mcc')
        return jsonify({'success': True, 'request_id': rid})

    # source == 'dispatcher'
    aircraft = (data.get('aircraft') or '').strip()
    parking_id = str(data.get('parking_id') or '')
    if not aircraft or not parking_id:
        conn.close()
        return jsonify({'success': False, 'error': 'Не указан борт или стоянка'}), 400

    flight_number = (data.get('flight_number') or '').strip()
    fault = normalize_fault_type(data.get('fault_type', 'other'))
    severity = data.get('severity', 'medium')
    description = data.get('description', fault)
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
                                  severity=severity, exclude_ids=busy)
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
            pilot_name, pilot_rank)
            VALUES (?,?,?,?,?,?,?,?,'assigned',?,'PENDING','technician',?,?,?,?,?,?,?,?,?)""",
                    (rid, aircraft, flight_number, fault, severity, parking_id, description,
                     source, severity,
                     eng['id'], eng['name'],
                     0, 0, 0, 0, None, pilot_name, pilot_rank))
        cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                    ('ВС ' + aircraft, eng['id']))
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
            'candidates': table
        })

    cur.execute("""INSERT INTO requests (id, aircraft, flight_number, fault_type, severity,
        parking_id, description, source, status, criticality, mcc_status, routing_stage,
        pilot_name, pilot_rank, delay_started_at, delay_reason)
        VALUES (?,?,?,?,?,?,?,?,'queued',?,'PENDING','dispatcher',?,?,CURRENT_TIMESTAMP,'Ожидание инженера')""",
                (rid, aircraft, flight_number, fault, severity, parking_id, description,
                 source, severity, pilot_name, pilot_rank))
    conn.commit()
    conn.close()
    _append_request_created(rid, source, aircraft, parking_id, flight_number,
                            'queued', 'dispatcher')
    _schedule_recheck(rid)
    return jsonify({'success': True, 'queued': True, 'request_id': rid,
                    'message': 'Заявка в очереди — нет свободных специалистов',
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
        walk_path, _ = get_shortest_path(road_graph_vehicle,
                                          start_lat, start_lng, car_lat, car_lng)
        if walk_path is None:
            walk_path = [[start_lat, start_lng], [car_lat, car_lng]]
        car_path, car_dist = get_shortest_path(road_graph_vehicle,
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

    path, dist = get_shortest_path(road_graph_vehicle,
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
        description,engineer_id,engineer_name,status,rejected_by,created_at
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

    who_id = engineer_id or row['engineer_id']
    who_name = row['engineer_name'] or ''
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

    fault = row['fault_type'] or 'engine_failure'
    parking = str(row['parking_id'] or '')
    aircraft = row['aircraft'] or 'ВС'
    target = _parking_point(parking) or _location_center()

    cur.execute("""SELECT DISTINCT engineer_id FROM requests
        WHERE status NOT IN ('completed','cancelled','flight_cancelled','deleted')
          AND engineer_id IS NOT NULL AND id!=?""", (rid,))
    busy = {r['engineer_id'] for r in cur.fetchall()}

    new_eng, route = get_free_engineer_for(conn, fault, FAULT_SKILL,
                                           target[0], target[1],
                                           exclude_ids=rej_prev | busy)
    cur.execute("""UPDATE requests SET status='cancelled', cancel_reason=?,
        cancelled_by_name=?, cancelled_at=CURRENT_TIMESTAMP, rejected_by=?,
        routing_stage='done' WHERE id=?""", (reason, who_name, rej_str, rid))

    if new_eng:
        new_rid = 'req_' + str(int(time.time() * 1000)) + '_r'
        cur.execute("""INSERT INTO requests (id,aircraft,fault_type,severity,terminal,
            parking_id,description,source,engineer_id,engineer_name,travel_time,
            using_car,walk_to_car,car_time,best_car,status,criticality,rejected_by,
            replaces_request_id,all_rejected,mcc_status,routing_stage)
            VALUES (?,?,?,?,?,?,?,'dispatcher',?,?,?,?,?,?,?,'assigned',?,?,?,0,'APPROVED','technician')""",
                    (new_rid, aircraft, fault, row['severity'], row['terminal'], parking,
                     row['description'], new_eng['id'], new_eng['name'],
                     0, 0, 0, 0, None, row['severity'], rej_str, rid))
        cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                    ('ВС ' + aircraft, new_eng['id']))
        conn.commit()
        conn.close()
        # При переназначении после отказа/отмены новый маршрут также
        # сохраняется немедленно, пока новый инженер ещё не ответил.
        _recalc_route_for_request_id(new_rid, force=True)
        return jsonify({
            'success': True, 'request_id': rid,
            'cancelled_by_name': who_name, 'reason': reason,
            'reassigned': True, 'new_request_id': new_rid,
            'selected': {
                'request_id': new_rid,
                'engineer': {
                    'id': new_eng['id'], 'name': new_eng['name'],
                    'device_id': normalize_device_id(new_eng['device_id'],
                                                     get_engineer_number_from_device(new_eng['device_id'])),
                    'specialization': new_eng['specialization'],
                    'coords_lat': new_eng['coords_lat'],
                    'coords_lng': new_eng['coords_lng']
                },
                'travel_time': 0,
                'using_car': False,
                'awaiting_acceptance': True
            }
        })

    cur.execute("""UPDATE requests SET status='queued', all_rejected=?,
        delay_started_at=COALESCE(delay_started_at,CURRENT_TIMESTAMP),
        delay_reason=CASE WHEN delay_reason='' OR delay_reason IS NULL
                          THEN 'Ожидание инженера' ELSE delay_reason END
        WHERE id=?""", (0, rid))
    conn.commit()
    conn.close()
    _schedule_recheck(rid)
    return jsonify({
        'success': True, 'request_id': rid,
        'cancelled_by_name': who_name, 'reason': reason,
        'reassigned': False, 'queued': True
    })


@app.route('/api/requests/<rid>/reject', methods=['POST'])
def reject_request(rid):
    data = request.get_json() or {}
    rejected_by = str(data.get('engineer_id') or '')
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

    fault = row['fault_type'] or 'engine_failure'
    parking = str(row['parking_id'] or '')
    aircraft = row['aircraft'] or 'ВС'
    target = _parking_point(parking) or _location_center()
    old_eng = row['engineer_id']
    rej_prev = {x.strip() for x in str(row['rejected_by']).split(',') if x.strip()}
    if rejected_by:
        rej_prev.add(rejected_by)
    rej_str = ','.join(sorted(rej_prev))
    if old_eng:
        set_engineer_free(conn, old_eng)
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))

    cur.execute("""SELECT DISTINCT engineer_id FROM requests
        WHERE status NOT IN ('completed','cancelled','flight_cancelled','deleted')
          AND engineer_id IS NOT NULL AND id!=?""", (rid,))
    busy = {r['engineer_id'] for r in cur.fetchall()}

    new_eng, route = get_free_engineer_for(conn, fault, FAULT_SKILL,
                                           target[0], target[1],
                                           exclude_ids=rej_prev | busy)
    if new_eng:
        cur.execute("""UPDATE requests SET engineer_id=?, engineer_name=?,
            travel_time=0, using_car=0, walk_to_car=0, car_time=0, best_car=NULL,
            engineer_comment='', status='assigned', routing_stage='technician',
            inspection_assigned_at=NULL, rejected_by=?, all_rejected=0 WHERE id=?""",
                    (new_eng['id'], new_eng['name'], rej_str, rid))
        set_engineer_busy(conn, new_eng['id'], 'ВС ' + aircraft)
        conn.commit()
        conn.close()
        append_event('status_changed', request_id=rid, previous_status='assigned',
                     status='assigned', actor='engineer', engineer_id=rejected_by,
                     note=reason)
        append_event('reassigned', request_id=rid, previous_engineer=old_eng,
                     engineer_id=new_eng['id'], reason=reason)
        # После отказа назначаем следующего инженера и сразу строим его маршрут.
        _recalc_route_for_request_id(rid, force=True)
        _recalc_all_active_routes()
        return jsonify({
            'success': True, 'reassigned': True,
            'rejected_engineer_id': rejected_by,
            'selected': {
                'engineer': {
                    'id': new_eng['id'], 'name': new_eng['name'],
                    'device_id': normalize_device_id(new_eng['device_id'],
                                                     get_engineer_number_from_device(new_eng['device_id'])),
                    'specialization': new_eng['specialization'],
                    'coords_lat': new_eng['coords_lat'],
                    'coords_lng': new_eng['coords_lng']
                },
                'travel_time': 0,
                'using_car': False,
                'awaiting_acceptance': True
            }
        })

    cur.execute("""UPDATE requests SET status='queued', engineer_id=NULL,
        engineer_name=NULL, travel_time=0, using_car=0, walk_to_car=0, car_time=0,
        best_car=NULL, rejected_by=?, all_rejected=1,
        delay_started_at=COALESCE(delay_started_at,CURRENT_TIMESTAMP),
        delay_reason='Все отказались'
        WHERE id=?""", (rej_str, rid))
    conn.commit()
    conn.close()
    append_event('status_changed', request_id=rid, previous_status='assigned',
                 status='queued', actor='engineer', engineer_id=rejected_by,
                 note=reason)
    append_event('reassigned', request_id=rid, previous_engineer=old_eng,
                 engineer_id=None, reason=reason)
    _recalc_all_active_routes()
    _schedule_recheck(rid)
    return jsonify({'success': True, 'reassigned': False, 'queued': True})


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
    cur.execute("SELECT id, pilot_audio, pilot_audio_mime, pilot_audio_text FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
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
                                f'en-{number:03d}', f'eng-{number}'})
            if needle in aliases:
                return {'type': 'engineer', 'id': eid, 'label': row['name'] or device}

        cur.execute("SELECT id, device_id FROM transport")
        for row in cur.fetchall():
            tid = str(row['id'])
            device = str(row['device_id'] or '')
            aliases = {tid.lower(), device.lower(), f'v-{int(tid):02d}', f'v-{tid}'}
            if needle in aliases:
                return {'type': 'transport', 'id': tid, 'label': device or tid}

        cur.execute("SELECT id, callsign FROM aircraft")
        for row in cur.fetchall():
            callsign = str(row['callsign'] or '')
            if needle in {str(row['id']).lower(), callsign.lower()}:
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


def _remember_track_meta(kind, object_id, acc, spd, when):
    """Запоминает acc/spd последней точки агента (без изменения схемы БД)."""
    with _TRACK_META_LOCK:
        _TRACK_META[(kind, str(object_id))] = {'acc': acc, 'spd': spd, 't': when}


def _track_apply_position(kind, object_id, lat, lng, when, acc=None, spd=None):
    conn = get_db()
    arrival_event = None
    active_request_id = None
    try:
        cur = conn.cursor()
        assigned_request = None
        if kind == 'engineer':
            cur.execute("UPDATE engineers SET coords_lat=?, coords_lng=? WHERE id=?",
                        (lat, lng, object_id))
            if cur.rowcount == 0:
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
                            late = elapsed_min is not None and elapsed_min > REGULATION_MINUTES
                            eta_min = _route_eta_min(active['engineer_comment'])
                            delta_min = (round(elapsed_min - eta_min, 1)
                                         if elapsed_min is not None and eta_min is not None
                                         else None)
                            note = f'Автоматическое прибытие в зону {active["parking_id"]}'
                            if elapsed_min is not None:
                                note += (f'; {elapsed_min} мин с назначения — '
                                         + ('нарушение норматива 15 мин' if late
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
                                'regulation_minutes': REGULATION_MINUTES,
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

    _remember_track_meta(kind, object_id, acc, spd, when)
    sse_publish('position', {'type': kind, 'obj': str(object_id),
                             'lat': lat, 'lon': lng, 't': when,
                             'acc': acc, 'spd': spd})
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

    result['applied'] = _track_apply_position(known['type'], known['id'],
                                              lat, lon, when,
                                              acc=metrics['acc'],
                                              spd=metrics['spd'])
    return result


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
    client_queue = queue.Queue(maxsize=_SSE_QUEUE_SIZE)
    with _SSE_LOCK:
        _SSE_CLIENTS.add(client_queue)

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
            with _SSE_LOCK:
                _SSE_CLIENTS.discard(client_queue)

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no'})


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
                              COALESCE(p.lat, e.coords_lat) AS lat,
                              COALESCE(p.lng, e.coords_lng) AS lng
                       FROM engineers e
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


@app.route('/api/v1/agents', methods=['GET'])
def api_v1_agents():
    return jsonify(_agents_snapshot())


@app.route('/api/v1/snapshot', methods=['GET'])
def api_v1_snapshot():
    payload = _agents_snapshot()
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
                    'journal': os.path.basename(EVENTS_PATH)})


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


@app.route('/m/engineer')
def mobile_engineer_app():
    return render_template('mobile/engineer.html')


@app.route('/m/agents')
def mobile_agents_app():
    return render_template('mobile/agents.html')


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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)