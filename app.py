import os
import re
import math
import time
import json
import base64
import queue
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

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
    print('[LLM] openai не установлен')

# ------------------------------------------------------------
# Конфигурация
# ------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database.db')
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
REGULATION_MINUTES = 15
WALK_SPEED_KMH = 5.0
RECHECK_DELAY_SEC = 10

FFMPEG_PATH = r'C:\Users\deela\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe'
if os.path.exists(FFMPEG_PATH):
    ffmpeg_dir = os.path.dirname(FFMPEG_PATH)
    cp = os.environ.get('PATH', '')
    if ffmpeg_dir not in cp.split(os.pathsep):
        os.environ['PATH'] = ffmpeg_dir + os.pathsep + cp

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.secret_key = os.environ.get('SECRET_KEY', '').strip() or 'intravia-secret-key-change-in-production'
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024
CORS(app, resources={r"/*": {"origins": "*"}})


# ------------------------------------------------------------
# Realtime: in-process SSE bus (gunicorn/Flask, один процесс, много потоков)
# ------------------------------------------------------------
_SSE_LOCK = threading.Lock()
_SSE_CLIENTS = set()
_SSE_QUEUE_SIZE = 200


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

_PARKING_FALLBACK = {
    '101': (55.9807, 37.4171), '102': (55.9806, 37.4171),
    '103': (55.9801, 37.4173), '104': (55.9800, 37.4174),
    '105': (55.9808, 37.4163), '106': (55.9807, 37.4164),
    '107': (55.9804, 37.4172), '108': (55.9803, 37.4173),
    '109': (55.9801, 37.4166), '110': (55.9800, 37.4167),
    '111': (55.9797, 37.4175), '112': (55.9796, 37.4176),
    '113': (55.9794, 37.4170), '114': (55.9793, 37.4170),
    '115': (55.9790, 37.4179), '116': (55.9789, 37.4179),
    '117': (55.9785, 37.4183), '118': (55.9782, 37.4176),
    '119': (55.9783, 37.4172), '120': (55.9784, 37.4172),
    '121': (55.9785, 37.4171), '122': (55.9783, 37.4181),
    '124': (55.9799, 37.4095), '125': (55.9797, 37.4097),
    '126': (55.9796, 37.4113), '127': (55.9796, 37.4115),
    '128': (55.9792, 37.4100), '129': (55.9790, 37.4102),
    '130': (55.9796, 37.4111), '131': (55.9791, 37.4111),
    '132': (55.9790, 37.4112), '134': (55.9789, 37.4115),
    '135': (55.9788, 37.4116), '136': (55.9784, 37.4105),
    '137': (55.9782, 37.4106), '138': (55.9781, 37.4118),
    '139': (55.9780, 37.4120), '140': (55.9775, 37.4125),
    '141': (55.9771, 37.4119), '142': (55.9771, 37.4110),
    '143': (55.9772, 37.4104), '144': (55.9774, 37.4103),
    '145': (55.9774, 37.4121), '146': (55.9772, 37.4121),
    '11': (55.9624, 37.4013), '12': (55.9624, 37.4004),
    '13': (55.9625, 37.4017), '14': (55.9627, 37.4027),
    '15': (55.9627, 37.4030), '16': (55.9630, 37.4043),
    '17': (55.9632, 37.4047), '19': (55.9643, 37.4047),
    '20': (55.9645, 37.4046), '21': (55.9650, 37.4043),
    '22': (55.9652, 37.4042), '23': (55.9655, 37.4043),
    '24': (55.9656, 37.4046), '25': (55.9656, 37.4050),
    '26': (55.9650, 37.4055), '27': (55.9648, 37.4056),
    '28': (55.9642, 37.4058), '29': (55.9640, 37.4060),
    '30': (55.9635, 37.4074), '31': (55.9636, 37.4087),
    '32': (55.9636, 37.4092), '33': (55.9637, 37.4107),
    '34': (55.9638, 37.4108), '35': (55.9639, 37.4118),
    '36': (55.9644, 37.4128), '37': (55.9644, 37.4128),
    '38': (55.9648, 37.4130), '39': (55.9652, 37.4132),
    '40': (55.9656, 37.4134), '41': (55.9659, 37.4135),
    '46': (55.9664, 37.4134), '47': (55.9667, 37.4135),
    '48': (55.9669, 37.4137), '49': (55.9669, 37.4138),
    '50': (55.9670, 37.4146), '51': (55.9671, 37.4155),
    '52': (55.9672, 37.4163), '53': (55.9674, 37.4171),
    '54': (55.9674, 37.4172), '55': (55.9671, 37.4180),
    '57': (55.9669, 37.4181), '58': (55.9668, 37.4181),
    '59': (55.9664, 37.4179), '60': (55.9661, 37.4175),
}


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
# Дорожный граф
# ============================================================
road_graph_vehicle = nx.Graph()


def load_road_graph():
    global road_graph_vehicle
    road_graph_vehicle = nx.Graph()
    filepath = os.path.join(STATIC_DIR, 'roads.geojson')
    if not os.path.exists(filepath):
        print(f'[GRAPH] {filepath} не найден')
        return
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f'[GRAPH] Ошибка: {e}')
        return

    segments = []
    for feature in data.get('features', []):
        geom = feature.get('geometry', {})
        if geom.get('type') != 'LineString':
            continue
        props = feature.get('properties') or {}
        if str(props.get('category') or '').strip().lower() not in ('vehicle', ''):
            continue
        coords = geom['coordinates']
        if len(coords) < 2:
            continue
        segments.append([(float(c[1]), float(c[0])) for c in coords])

    if not segments:
        print('[GRAPH] Нет LineString')
        return

    SNAP_M = 3.0
    TOL_LAT = SNAP_M / 111320.0
    TOL_LNG = SNAP_M / (111320.0 * math.cos(math.radians(55.98)))
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

    for seg in segments:
        snapped = [snap(lat, lng) for lat, lng in seg]
        for i in range(len(snapped) - 1):
            n1, n2 = snapped[i], snapped[i + 1]
            if n1 == n2:
                continue
            w = distance_km(n1[0], n1[1], n2[0], n2[1])
            road_graph_vehicle.add_edge(n1, n2, weight=w)

    print(f'[GRAPH] {len(road_graph_vehicle.nodes)} узлов, '
          f'{len(road_graph_vehicle.edges)} рёбер')

    comps = list(nx.connected_components(road_graph_vehicle))
    if len(comps) > 1:
        _bridge_components(road_graph_vehicle, comps)


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

    feasible = []
    for e in pool:
        e_lat = e['coords_lat'] or 55.975
        e_lng = e['coords_lng'] or 37.410
        walk_dist = distance_km(e_lat, e_lng, target_lat, target_lng)
        walk_time = walk_dist / WALK_SPEED_KMH * 60
        walk_path = [[e_lat, e_lng], [target_lat, target_lng]]
        best_car = None
        best_time = walk_time
        best_car_time = 0
        best_walk_to_car = 0
        best_combined_path = walk_path

        for car in cars:
            seg1_dist = distance_km(e_lat, e_lng, car['lat'], car['lng'])
            wtc = seg1_dist / WALK_SPEED_KMH * 60
            seg1_path = [[e_lat, e_lng], [car['lat'], car['lng']]]
            seg2_path, seg2_dist = get_shortest_path(
                road_graph_vehicle, car['lat'], car['lng'], target_lat, target_lng
            )
            if seg2_dist == 0:
                seg2_dist = distance_km(car['lat'], car['lng'], target_lat, target_lng)
                seg2_path = [[car['lat'], car['lng']], [target_lat, target_lng]]
            sp = car['speed'] or 30
            dt = seg2_dist / sp * 60
            t = wtc + dt
            if t < best_time:
                best_time = t
                best_car = car
                best_car_time = dt
                best_walk_to_car = wtc
                best_combined_path = seg1_path + seg2_path[1:]

        if best_time > REGULATION_MINUTES:
            continue

        feasible.append({
            'engineer': e,
            'travel_time': round(best_time, 1),
            'walk_time': round(walk_time, 1),
            'using_car': best_car is not None, 'best_car': best_car,
            'best_car_time_exact': best_car_time,
            'walk_to_car_exact': best_walk_to_car,
            'best_car_time': round(best_car_time, 1),
            'walk_to_car': round(best_walk_to_car, 1),
            'path_coords': best_combined_path,
        })

    if not feasible:
        return None, None
    feasible.sort(key=lambda x: x['travel_time'])
    return feasible[0]['engineer'], feasible[0]


def try_reassign_queued():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT engineer_id FROM requests
                   WHERE routing_stage='technician'
                     AND status IN ('assigned','arriving','in_progress','working')
                     AND engineer_id IS NOT NULL""")
    busy = {str(r['engineer_id']) for r in cur.fetchall()}

    cur.execute("""SELECT id, aircraft, fault_type, parking_id, rejected_by
                   FROM requests WHERE status='queued' ORDER BY created_at ASC""")
    queued = [dict(r) for r in cur.fetchall()]
    if not queued:
        conn.close()
        return

    assigned = []
    for q in queued:
        rejected = {x.strip() for x in str(q['rejected_by']).split(',') if x.strip()}
        exclude = rejected | busy
        target = PARKING_COORDS.get(str(q['parking_id'] or ''), (55.975, 37.410))
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

    target = PARKING_COORDS.get(parking)
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

    walk_path, walk_dist = get_shortest_path(
        road_graph_vehicle, e_lat, e_lng, target[0], target[1]
    )
    if walk_path is None:
        walk_path = [[e_lat, e_lng], [target[0], target[1]]]
        walk_dist = distance_km(e_lat, e_lng, target[0], target[1])
    else:
        # Гарантируем, что линия начинается ровно в координате метки инженера,
        # а не в ближайшей вершине дорожного графа.
        walk_path = list(walk_path)
        if not walk_path:
            walk_path = [[e_lat, e_lng], [target[0], target[1]]]
        else:
            walk_path[0] = [e_lat, e_lng]
            if len(walk_path) == 1:
                walk_path.append([target[0], target[1]])
            else:
                walk_path[-1] = [target[0], target[1]]
    walk_time = walk_dist / WALK_SPEED_KMH * 60

    cur.execute("SELECT id, device_id, lat, lng, speed FROM transport WHERE assigned_to=?",
                (request_row['id'],))
    assigned_car = cur.fetchone()

    best_time = walk_time
    best_car = None
    best_car_time = 0
    best_walk_to_car = 0
    best_path = walk_path

    if assigned_car:
        seg1_dist = distance_km(e_lat, e_lng, assigned_car['lat'], assigned_car['lng'])
        wtc = seg1_dist / WALK_SPEED_KMH * 60
        car_path, car_dist = get_shortest_path(
            road_graph_vehicle, assigned_car['lat'], assigned_car['lng'],
            target[0], target[1]
        )
        if car_path is None:
            car_path = [[assigned_car['lat'], assigned_car['lng']], [target[0], target[1]]]
            car_dist = distance_km(assigned_car['lat'], assigned_car['lng'], target[0], target[1])
        else:
            car_path = list(car_path)
            if len(car_path) < 2:
                car_path = [[assigned_car['lat'], assigned_car['lng']], [target[0], target[1]]]
            else:
                car_path[0] = [assigned_car['lat'], assigned_car['lng']]
                car_path[-1] = [target[0], target[1]]
        sp = assigned_car['speed'] or 30
        dt = car_dist / sp * 60
        best_time = wtc + dt
        best_car = assigned_car
        best_car_time = dt
        best_walk_to_car = wtc
        best_path = [[e_lat, e_lng]] + car_path
    else:
        cur.execute("SELECT id, device_id, lat, lng, speed FROM transport WHERE assigned_to IS NULL")
        for car in cur.fetchall():
            seg1_dist = distance_km(e_lat, e_lng, car['lat'], car['lng'])
            wtc = seg1_dist / WALK_SPEED_KMH * 60
            car_path, car_dist = get_shortest_path(
                road_graph_vehicle, car['lat'], car['lng'], target[0], target[1]
            )
            if car_path is None:
                car_path = [[car['lat'], car['lng']], [target[0], target[1]]]
                car_dist = distance_km(car['lat'], car['lng'], target[0], target[1])
            else:
                car_path = list(car_path)
                if len(car_path) < 2:
                    car_path = [[car['lat'], car['lng']], [target[0], target[1]]]
                else:
                    car_path[0] = [car['lat'], car['lng']]
                    car_path[-1] = [target[0], target[1]]
            sp = car['speed'] or 30
            dt = car_dist / sp * 60
            total = wtc + dt
            if total < best_time:
                best_time = total
                best_car = car
                best_car_time = dt
                best_walk_to_car = wtc
                best_path = [[e_lat, e_lng]] + car_path

    conn.close()

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

    result = _recompute_route_for_request(req)
    if not result:
        _clear_route_for_request(cur, request_id, req['engineer_comment'])
        conn.commit()
        conn.close()
        return
    new_route, fields = result

    old_comment = req['engineer_comment'] or ''
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
@app.route('/api/parking', methods=['GET'])
def api_parking():
    def sort_key(k):
        m = re.match(r'^(\d+)', str(k))
        return (int(m.group(1)) if m else 9999, str(k))
    result = [PARKING_META[k] for k in sorted(PARKING_META.keys(), key=sort_key)]
    return jsonify(result)


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
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,email,name,role,avatar FROM users WHERE email=? AND password=?",
                (email, password))
    u = cur.fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'Неверный email или пароль'}), 401
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
    res = {'id': u['id'], 'email': u['email'], 'name': u['name'],
           'role': u['role'], 'avatar': u['avatar'] or '👤'}
    res.update(extra)
    return jsonify({'success': True, 'user': res})


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
    avatar = {'dispatcher': '👨‍✈️', 'mcc': '👨‍💻', 'cpu': '🎧'}.get(role, '👤')
    default_pwd = {'dispatcher': 'disp123', 'mcc': 'mcc123', 'cpu': 'cpu123'}.get(role, 'user123')
    password = data.get('password') or default_pwd
    cur.execute("""INSERT INTO users (id,email,password,name,role,avatar,phone,shift,status)
                   VALUES (?,?,?,?,?,?,?,?,'offline')""",
                (uid, email, password, name, role, avatar,
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
        cur.execute("UPDATE users SET password=? WHERE id=?", (data.get('password'), user_id))
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
        return jsonify({'success': False, 'error': str(e)}), 500
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
    target = PARKING_COORDS.get(parking_id, (55.975, 37.410))

    # Передаём заявку в диспетчерскую зону, сохраняя исходный source='pilot'.
    # Это необходимо, чтобы пилот продолжал видеть именно свою заявку.
    cur.execute("""UPDATE requests SET routing_stage='dispatcher',
                   mcc_comment=COALESCE(mcc_comment,'') || ?
                   WHERE id=?""", ('\n[ЦПУ→Диспетчер] ' + comment, rid))
    _log_routing_event(conn, rid, 'dispatcher', 'ЦПУ', comment)

    # Автоподбор инженера по профилю дефекта и кратчайшему ETA.
    cur.execute("""SELECT DISTINCT engineer_id FROM requests
                   WHERE routing_stage='technician'
                     AND status IN ('assigned','arriving','in_progress','working')
                     AND engineer_id IS NOT NULL AND id!=?""", (rid,))
    busy = {str(r['engineer_id']) for r in cur.fetchall()}
    eng, route = get_free_engineer_for(
        conn, fault, FAULT_SKILL, target[0], target[1], exclude_ids=busy
    )

    if eng:
        cur.execute("""UPDATE requests SET
                       engineer_id=?, engineer_name=?, status='assigned',
                       routing_stage='technician', mcc_status=CASE WHEN mcc_mel_choice='MEL' THEN 'MEL_REVIEW' ELSE 'INSPECTION' END,
                       travel_time=0, using_car=0, walk_to_car=0, car_time=0, best_car=NULL,
                       engineer_comment='', inspection_assigned_at=NULL,
                       delay_minutes=0, delay_reason='', delay_started_at=NULL,
                       mcc_comment=COALESCE(mcc_comment,'') || ?
                       WHERE id=?""",
                    (eng['id'], eng['name'],
                     '\n[Автоназначение] ' + eng['name'], rid))
        cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                    ('ВС ' + aircraft, eng['id']))
        _log_routing_event(conn, rid, 'technician', 'Диспетчер (авто)',
                           'Автоматически назначен ' + eng['name'])
        conn.commit()
        conn.close()
        # Маршрут-превью сразу доступен диспетчеру; выезд начнётся после принятия.
        _recalc_route_for_request_id(rid, force=True)
        return jsonify({
            'success': True, 'request_id': rid, 'routing_stage': 'technician',
            'assigned': True, 'queued': False,
            'engineer_id': eng['id'], 'engineer_name': eng['name'],
            'travel_time': 0, 'awaiting_acceptance': True
        })

    # Нет свободного специалиста нужного профиля: заявка уже в активном списке
    # диспетчера и будет автоматически подобрана при освобождении инженера.
    cur.execute("""UPDATE requests SET status='queued',
                   delay_started_at=COALESCE(delay_started_at,CURRENT_TIMESTAMP),
                   delay_reason=CASE WHEN delay_reason IS NULL OR delay_reason=''
                                     THEN 'Автоматический подбор инженера' ELSE delay_reason END
                   WHERE id=?""", (rid,))
    _log_routing_event(conn, rid, 'dispatcher', 'Диспетчер (авто)',
                       'Нет свободного инженера подходящей специализации; включён автоподбор')
    conn.commit()
    conn.close()
    _schedule_recheck(rid)
    return jsonify({
        'success': True, 'request_id': rid, 'routing_stage': 'dispatcher',
        'assigned': False, 'queued': True,
        'message': 'Заявка добавлена в активные заявки; инженер будет назначен автоматически'
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
    cur.execute("SELECT COUNT(*) FROM engineers")
    total = cur.fetchone()[0]
    lat = 55.975 + (total % 6) * 0.0008
    lng = 37.410 + (total % 6) * 0.0008
    cur.execute("""INSERT INTO engineers (id,name,device_id,specialization,coords_lat,coords_lng,status,phone,task_step)
                   VALUES (?,?,?,?,?,?,?,?,'idle')""",
                (eid, name, dev, data.get('specialization', 'general'),
                 lat, lng, data.get('status', 'free'), data.get('phone', '')))
    cur.execute("""INSERT INTO users (id,email,password,name,role,avatar,status)
                   VALUES (?,?,'eng123',?,'engineer','🔧','offline')""",
                (eid, email, name))
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
                   VALUES (?,?,'pilot123',?,'pilot','👨‍✈️','offline')""",
                (pid, email, name))
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
    target = PARKING_COORDS.get(parking_id, (55.975, 37.410))

    cur.execute("""SELECT DISTINCT engineer_id FROM requests
        WHERE status NOT IN ('completed','cancelled','flight_cancelled','deleted')
          AND engineer_id IS NOT NULL""")
    busy = {r['engineer_id'] for r in cur.fetchall()}

    eng, route = get_free_engineer_for(conn, fault, FAULT_SKILL,
                                       target[0], target[1],
                                       exclude_ids=busy)

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
        # Маршрут-превью должен появиться сразу после назначения инженера.
        # Не ждём ни подтверждения диспетчера, ни ответа самого инженера.
        _recalc_route_for_request_id(rid, force=True)
        route_conn = get_db()
        route_cur = route_conn.cursor()
        route_cur.execute("SELECT travel_time, using_car, walk_to_car, car_time, best_car FROM requests WHERE id=?", (rid,))
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
                'using_car': route['using_car'],
                'walk_time': route['walk_time'],
                'walk_to_car': route['walk_to_car'],
                'car_time': route['best_car_time'],
                'best_car': route['best_car']['device_id'] if route['using_car'] else None,
                'path': route.get('path_coords')
            }
        })

    cur.execute("""INSERT INTO requests (id, aircraft, flight_number, fault_type, severity,
        parking_id, description, source, status, criticality, mcc_status, routing_stage,
        pilot_name, pilot_rank, delay_started_at, delay_reason)
        VALUES (?,?,?,?,?,?,?,?,'queued',?,'PENDING','dispatcher',?,?,CURRENT_TIMESTAMP,'Ожидание инженера')""",
                (rid, aircraft, flight_number, fault, severity, parking_id, description,
                 source, severity, pilot_name, pilot_rank))
    conn.commit()
    conn.close()
    _schedule_recheck(rid)
    return jsonify({'success': True, 'queued': True, 'request_id': rid,
                    'message': 'Заявка в очереди — нет свободных специалистов'})


# ============================================================
# /api/route
# ============================================================
@app.route('/api/route', methods=['POST'])
def compute_route():
    data = request.get_json() or {}
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
    target = PARKING_COORDS.get(parking, (55.975, 37.410))

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
    target = PARKING_COORDS.get(parking, (55.975, 37.410))
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
        return jsonify({'success': False, 'error': str(e)}), 500
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
        return jsonify({'success': False, 'error': str(e)}), 500
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


def _track_apply_position(kind, object_id, lat, lng, when):
    conn = get_db()
    try:
        cur = conn.cursor()
        assigned_request = None
        if kind == 'engineer':
            cur.execute("UPDATE engineers SET coords_lat=?, coords_lng=? WHERE id=?",
                        (lat, lng, object_id))
            if cur.rowcount == 0:
                return False
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
    elif kind == 'transport' and assigned_request:
        try:
            _recalc_route_for_request_id(assigned_request, force=True)
        except Exception as exc:
            print(f'[TRACK] route recalc request: {exc}')

    sse_publish('position', {'type': kind, 'obj': str(object_id),
                             'lat': lat, 'lon': lng, 't': when})
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
                                              lat, lon, when)
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
            engineers.append(item)

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
            transport.append(item)

        cur.execute("SELECT id, callsign, lat, lng, status, heading, speed "
                    "FROM aircraft ORDER BY callsign")
        aircraft = []
        for row in cur.fetchall():
            item = dict(row)
            item['obj'] = item.get('callsign') or str(item['id'])
            item['kind'] = 'aircraft'
            item['label'] = item.get('callsign') or str(item['id'])
            aircraft.append(item)

        cur.execute("""SELECT object_id, lat, lng, updated_at
                       FROM map_object_positions WHERE object_type='aircraft'""")
        known_aircraft = {str(a.get('obj')) for a in aircraft}
        for row in cur.fetchall():
            if str(row['object_id']) not in known_aircraft:
                aircraft.append({'id': row['object_id'], 'obj': row['object_id'],
                                 'kind': 'aircraft', 'label': row['object_id'],
                                 'callsign': row['object_id'],
                                 'lat': row['lat'], 'lng': row['lng'],
                                 'status': 'tracked', 'updated_at': row['updated_at']})

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
    load_svo_geojson()
    load_road_graph()
    try:
        _recalc_all_active_routes()
    except Exception as e:
        print(f'[STARTUP ROUTE CLEANUP] {e}')


_startup()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)