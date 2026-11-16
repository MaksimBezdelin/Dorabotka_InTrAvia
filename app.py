import os
import re
import math
import time
import json
import base64
import sqlite3
import tempfile
import subprocess
import threading
import networkx as nx
from datetime import datetime, timezone
from flask_cors import CORS
from flask import (Flask, request, jsonify, session,
                   render_template, redirect, url_for, Response, abort)

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
app.secret_key = 'intravia-secret-key-change-in-production'
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024
CORS(app, resources={r"/*": {"origins": "*"}})


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
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.OperationalError as e:
        print(f'[DB] PRAGMA: {e}')
    return conn


def ensure_schema(conn):
    cur = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
        name TEXT NOT NULL, role TEXT NOT NULL, avatar TEXT, phone TEXT,
        shift TEXT, status TEXT DEFAULT 'offline')""")

    cur.execute("""CREATE TABLE IF NOT EXISTS engineers (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, device_id TEXT,
        specialization TEXT DEFAULT 'general', coords_lat REAL DEFAULT 55.975,
        coords_lng REAL DEFAULT 37.410, status TEXT DEFAULT 'free',
        task TEXT, phone TEXT,
        task_step TEXT DEFAULT 'idle',
        task_started_at TIMESTAMP)""")

    cur.execute("""CREATE TABLE IF NOT EXISTS pilots (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE,
        phone TEXT, aircraft TEXT, rank TEXT DEFAULT 'pilot',
        status TEXT DEFAULT 'active')""")

    cur.execute("""CREATE TABLE IF NOT EXISTS transport (
        id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT UNIQUE NOT NULL,
        lat REAL NOT NULL, lng REAL NOT NULL, speed INTEGER DEFAULT 50,
        assigned_to TEXT)""")

    cur.execute("""CREATE TABLE IF NOT EXISTS requests (
        id TEXT PRIMARY KEY, aircraft TEXT, fault_type TEXT,
        severity TEXT DEFAULT 'medium', terminal TEXT, parking_id TEXT,
        description TEXT, source TEXT DEFAULT 'dispatcher',
        engineer_id TEXT, engineer_name TEXT, travel_time REAL DEFAULT 0,
        using_car INTEGER DEFAULT 0, walk_to_car REAL DEFAULT 0,
        car_time REAL DEFAULT 0, best_car TEXT,
        status TEXT DEFAULT 'assigned', criticality TEXT DEFAULT 'medium',
        engineer_comment TEXT, pilot_name TEXT, pilot_rank TEXT,
        pilot_audio TEXT, pilot_audio_mime TEXT,
        pilot_audio_duration REAL DEFAULT 0, pilot_audio_text TEXT,
        rejected_by TEXT DEFAULT '', cancel_reason TEXT DEFAULT '',
        cancelled_by_name TEXT DEFAULT '', cancelled_at TIMESTAMP,
        replaces_request_id TEXT DEFAULT '', delay_minutes INTEGER DEFAULT 0,
        delay_reason TEXT DEFAULT '', delay_started_at TIMESTAMP,
        delay_approved_by TEXT DEFAULT '', flight_cancelled INTEGER DEFAULT 0,
        flight_cancel_reason TEXT DEFAULT '', flight_cancelled_at TIMESTAMP,
        all_rejected INTEGER DEFAULT 0,
        mcc_status TEXT DEFAULT 'PENDING', mcc_decision TEXT DEFAULT '',
        mcc_comment TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    conn.commit()

    # ── Миграция requests ──
    cur.execute("PRAGMA table_info(requests)")
    existing = {r['name'] for r in cur.fetchall()}
    needed = {
        'pilot_name': "TEXT", 'pilot_rank': "TEXT", 'pilot_audio': "TEXT",
        'pilot_audio_mime': "TEXT", 'pilot_audio_duration': "REAL DEFAULT 0",
        'pilot_audio_text': "TEXT", 'rejected_by': "TEXT DEFAULT ''",
        'cancel_reason': "TEXT DEFAULT ''", 'cancelled_by_name': "TEXT DEFAULT ''",
        'cancelled_at': "TIMESTAMP", 'replaces_request_id': "TEXT DEFAULT ''",
        'delay_minutes': "INTEGER DEFAULT 0", 'delay_reason': "TEXT DEFAULT ''",
        'delay_started_at': "TIMESTAMP", 'delay_approved_by': "TEXT DEFAULT ''",
        'flight_cancelled': "INTEGER DEFAULT 0",
        'flight_cancel_reason': "TEXT DEFAULT ''", 'flight_cancelled_at': "TIMESTAMP",
        'all_rejected': "INTEGER DEFAULT 0",
        'mcc_status': "TEXT DEFAULT 'PENDING'", 'mcc_decision': "TEXT DEFAULT ''",
        'mcc_comment': "TEXT DEFAULT ''", 'mcc_ata': "TEXT DEFAULT ''",
        'mcc_mel_choice': "TEXT DEFAULT ''", 'mcc_mel_ref': "TEXT DEFAULT ''",
        'mcc_amm_verdict': "TEXT DEFAULT ''", 'mcc_proc_o': "INTEGER DEFAULT 0",
        'mcc_proc_m': "INTEGER DEFAULT 0", 'mcc_proc_mh': "INTEGER DEFAULT 0",
        'mcc_mel_category': "TEXT DEFAULT ''", 'mcc_due_date': "TEXT DEFAULT ''",
        'mcc_combined_check': "TEXT DEFAULT ''",
        'mcc_recurring_action': "TEXT DEFAULT ''",
        'mcc_escalation': "TEXT DEFAULT ''", 'mcc_dce_name': "TEXT DEFAULT ''",
        'mcc_kupol_ref': "TEXT DEFAULT ''", 'aog_since': "TIMESTAMP",
        'etr': "TEXT DEFAULT ''", 'reported_by': "TEXT DEFAULT ''",
        'engineer_verdict': "TEXT DEFAULT ''",
        'engineer_verdict_at': "TIMESTAMP",
        'aog_action': "TEXT DEFAULT ''",
        'inspection_assigned_at': "TIMESTAMP",
        'flight_number': "TEXT DEFAULT ''",
        'crs_signature': "TEXT",
        'crs_signed_at': "TIMESTAMP",
        'crs_signed_by': "TEXT",
        'routing_stage': "TEXT DEFAULT 'mcc'",
        'routing_history': "TEXT DEFAULT '[]'",
    }
    for col, typ in needed.items():
        if col not in existing:
            try:
                cur.execute(f"ALTER TABLE requests ADD COLUMN {col} {typ}")
                print(f"[MIGRATION] + requests.{col}")
            except sqlite3.OperationalError as e:
                print(f"[MIGRATION] skip {col}: {e}")

    # ── Миграция engineers ──
    cur.execute("PRAGMA table_info(engineers)")
    eng_cols = {r['name'] for r in cur.fetchall()}
    if 'task_step' not in eng_cols:
        try:
            cur.execute("ALTER TABLE engineers ADD COLUMN task_step TEXT DEFAULT 'idle'")
            print("[MIGRATION] + engineers.task_step")
        except sqlite3.OperationalError as e:
            print(f"[MIGRATION] skip engineers.task_step: {e}")
    if 'task_started_at' not in eng_cols:
        try:
            cur.execute("ALTER TABLE engineers ADD COLUMN task_started_at TIMESTAMP")
            print("[MIGRATION] + engineers.task_started_at")
        except sqlite3.OperationalError as e:
            print(f"[MIGRATION] skip engineers.task_started_at: {e}")

    conn.commit()


def seed_initial_data(conn):
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO users (id,email,password,name,role,avatar,status) "
                    "VALUES ('admin_1','admin@aeroflot.ru','admin123','Администратор','admin','👨‍💼','online')")

    cur.execute("SELECT COUNT(*) FROM users WHERE role='dispatcher'")
    if cur.fetchone()[0] == 0:
        cur.executemany("""INSERT INTO users (id,email,password,name,role,avatar,phone,shift,status)
                           VALUES (?,?,?,?,'dispatcher','👨‍✈️',?,?,'online')""", [
            ('disp_1', 'dispatcher@aeroflot.ru', 'disp123', 'Петров П.П.', '+7 (999) 200-10-01', 'day'),
            ('disp_2', 'dispatcher2@aeroflot.ru', 'disp123', 'Козлов К.К.', '+7 (999) 200-10-02', 'night'),
        ])

    cur.execute("SELECT COUNT(*) FROM users WHERE role='mcc'")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO users (id,email,password,name,role,avatar,status) "
                    "VALUES ('mcc_1','mcc@aeroflot.ru','mcc123','Смирнов А.В.','mcc','👨‍💻','online')")

    cur.execute("SELECT COUNT(*) FROM users WHERE role='cpu'")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO users (id,email,password,name,role,avatar,status) "
                    "VALUES ('cpu_1','cpu@aeroflot.ru','cpu123','Диспетчер ЦПУ','cpu','🎧','online')")

    cur.execute("SELECT COUNT(*) FROM engineers")
    if cur.fetchone()[0] == 0:
        cur.executemany("""INSERT INTO engineers (id,name,device_id,specialization,coords_lat,coords_lng,status,phone)
                           VALUES (?,?,?,?,?,?,'free',?)""", [
            ('eng_1', 'Иванов И.И.', 'EN-001', 'engine', 55.975, 37.410, '+7 (999) 300-10-01'),
            ('eng_5', 'Морозов М.М.', 'EN-005', 'engine', 55.974, 37.412, '+7 (999) 300-10-05'),
            ('eng_7', 'Волков В.В.', 'EN-007', 'engine', 55.973, 37.408, '+7 (999) 300-10-07'),
            ('eng_2', 'Сидоров С.С.', 'EN-002', 'avionics', 55.977, 37.412, '+7 (999) 300-10-02'),
            ('eng_8', 'Зайцев З.З.', 'EN-008', 'avionics', 55.978, 37.410, '+7 (999) 300-10-08'),
            ('eng_13', 'Гусев Г.Г.', 'EN-013', 'avionics', 55.976, 37.418, '+7 (999) 300-10-13'),
            ('eng_3', 'Кузнецов К.К.', 'EN-003', 'hydraulics', 55.973, 37.415, '+7 (999) 300-10-03'),
            ('eng_10', 'Орлов О.О.', 'EN-010', 'hydraulics', 55.977, 37.416, '+7 (999) 300-10-10'),
            ('eng_4', 'Смирнов С.С.', 'EN-004', 'electrical', 55.976, 37.408, '+7 (999) 300-10-04'),
            ('eng_9', 'Лебедев Л.Л.', 'EN-009', 'electrical', 55.974, 37.418, '+7 (999) 300-10-09'),
            ('eng_14', 'Соколов С.С.', 'EN-014', 'electrical', 55.972, 37.410, '+7 (999) 300-10-14'),
            ('eng_6', 'Новиков Н.Н.', 'EN-006', 'mechanical', 55.976, 37.415, '+7 (999) 300-10-06'),
            ('eng_11', 'Титов Т.Т.', 'EN-011', 'mechanical', 55.972, 37.413, '+7 (999) 300-10-11'),
            ('eng_15', 'Фомин Ф.Ф.', 'EN-015', 'mechanical', 55.979, 37.412, '+7 (999) 300-10-15'),
            ('eng_12', 'Белов Б.Б.', 'EN-012', 'general', 55.979, 37.414, '+7 (999) 300-10-12'),
            ('eng_16', 'Егоров Е.Е.', 'EN-016', 'general', 55.971, 37.415, '+7 (999) 300-10-16'),
        ])
        cur.executemany("""INSERT OR IGNORE INTO users (id,email,password,name,role,avatar,status)
                           VALUES (?,?,'eng123',?,'engineer','🔧','offline')""", [
            ('eng_1', 'ivanov@aeroflot.ru', 'Иванов И.И.'),
            ('eng_2', 'sidorov@aeroflot.ru', 'Сидоров С.С.'),
            ('eng_3', 'kuznetsov@aeroflot.ru', 'Кузнецов К.К.'),
            ('eng_4', 'smirnov@aeroflot.ru', 'Смирнов С.С.'),
            ('eng_5', 'morozov@aeroflot.ru', 'Морозов М.М.'),
            ('eng_6', 'novikov@aeroflot.ru', 'Новиков Н.Н.'),
            ('eng_7', 'volkov@aeroflot.ru', 'Волков В.В.'),
            ('eng_8', 'zaytsev@aeroflot.ru', 'Зайцев З.З.'),
            ('eng_9', 'lebedev@aeroflot.ru', 'Лебедев Л.Л.'),
            ('eng_10', 'orlov@aeroflot.ru', 'Орлов О.О.'),
            ('eng_11', 'titov@aeroflot.ru', 'Титов Т.Т.'),
            ('eng_12', 'belov@aeroflot.ru', 'Белов Б.Б.'),
            ('eng_13', 'gusev@aeroflot.ru', 'Гусев Г.Г.'),
            ('eng_14', 'sokolov@aeroflot.ru', 'Соколов С.С.'),
            ('eng_15', 'fomin@aeroflot.ru', 'Фомин Ф.Ф.'),
            ('eng_16', 'egorov@aeroflot.ru', 'Егоров Е.Е.'),
        ])

    cur.execute("SELECT COUNT(*) FROM pilots")
    if cur.fetchone()[0] == 0:
        cur.executemany("""INSERT INTO pilots (id,name,email,phone,aircraft,rank,status)
                           VALUES (?,?,?,?,?,?,'active')""", [
            ('pilot_1', 'Иванов И.И.', 'ivanov@aeroflot.ru', '+7 (999) 100-10-01', 'RA-73701', 'КВС'),
            ('pilot_2', 'Петров П.П.', 'petrov@aeroflot.ru', '+7 (999) 100-10-02', 'RA-73702', 'Пилот'),
            ('pilot_3', 'Сидоров С.С.', 'sidorov@aeroflot.ru', '+7 (999) 100-10-03', 'RA-73703', 'Второй пилот'),
            ('pilot_4', 'Кузнецов К.К.', 'kuznetsov@aeroflot.ru', '+7 (999) 100-10-04', 'RA-73704', 'КВС'),
            ('pilot_5', 'Смирнов С.С.', 'smirnov@aeroflot.ru', '+7 (999) 100-10-05', 'RA-73705', 'Пилот'),
        ])
        cur.executemany("""INSERT OR IGNORE INTO users (id,email,password,name,role,avatar,status)
                           VALUES (?,?,'pilot123',?,'pilot','👨‍✈️','offline')""", [
            ('pilot_1', 'pilot1@aeroflot.ru', 'Иванов И.И.'),
            ('pilot_2', 'pilot2@aeroflot.ru', 'Петров П.П.'),
            ('pilot_3', 'pilot3@aeroflot.ru', 'Сидоров С.С.'),
            ('pilot_4', 'pilot4@aeroflot.ru', 'Кузнецов К.К.'),
            ('pilot_5', 'pilot5@aeroflot.ru', 'Смирнов С.С.'),
        ])

    cur.execute("SELECT COUNT(*) FROM transport")
    if cur.fetchone()[0] == 0:
        cur.executemany("""INSERT INTO transport (device_id,lat,lng,speed)
                           VALUES (?,?,?,?)""", [
            ('CAR-001', 55.9780, 37.4150, 40),
            ('CAR-002', 55.9790, 37.4130, 60),
            ('CAR-003', 55.9760, 37.4090, 50),
            ('CAR-004', 55.9740, 37.4160, 30),
            ('CAR-005', 55.9770, 37.4180, 70),
        ])
    conn.commit()


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


def migrate_engineer_numbers():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, device_id, name FROM engineers")
    rows = cur.fetchall()
    if not rows:
        conn.close()
        return

    def sort_key(r):
        m = re.search(r'(\d+)\s*$', str(r['id'] or ''))
        return (int(m.group(1)) if m else 10 ** 9, str(r['name'] or ''))

    ordered = sorted(rows, key=sort_key)
    needs = any(get_engineer_number_from_device(r['device_id']) != idx
                for idx, r in enumerate(ordered, start=1))
    if not needs:
        conn.close()
        return
    print(f'[MIGRATION] Перенумерация {len(ordered)} инженеров')
    mapping = []
    for idx, r in enumerate(ordered, start=1):
        old_id = r['id']
        tmp_id = f'__tmp_{int(time.time() * 1000000)}_{idx}'
        tmp_dev = f'__TMP_{idx}'
        mapping.append((tmp_id, f'eng_{idx}', f'EN-{idx:03d}', old_id))
        try:
            cur.execute("UPDATE engineers SET id=?, device_id=? WHERE id=?",
                        (tmp_id, tmp_dev, old_id))
            cur.execute("UPDATE users SET id=? WHERE id=?", (tmp_id, old_id))
            cur.execute("UPDATE requests SET engineer_id=? WHERE engineer_id=?",
                        (tmp_id, old_id))
        except sqlite3.IntegrityError as e:
            print(f'[MIGRATION] tmp {old_id}: {e}')
    conn.commit()
    for tmp_id, new_id, new_dev, old_id in mapping:
        try:
            cur.execute("UPDATE engineers SET id=?, device_id=? WHERE id=?",
                        (new_id, new_dev, tmp_id))
            cur.execute("UPDATE users SET id=? WHERE id=?", (new_id, tmp_id))
            cur.execute("UPDATE requests SET engineer_id=? WHERE engineer_id=?",
                        (new_id, tmp_id))
        except sqlite3.IntegrityError as e:
            print(f'[MIGRATION] set {new_id}: {e}')
    conn.commit()
    conn.close()


def init_db():
    print(f'[DB] {DB_PATH}')
    conn = get_db()
    ensure_schema(conn)
    cur = conn.cursor()
    cur.execute("UPDATE requests SET routing_stage='mcc' WHERE routing_stage IS NULL OR routing_stage=''")
    conn.commit()
    seed_initial_data(conn)
    conn.close()
    migrate_engineer_numbers()
    print('[DB] Инициализация завершена')


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
    cur.execute("UPDATE engineers SET status='busy', task=? WHERE id=?",
                (task_text, engineer_id))
    return cur.rowcount > 0


def set_engineer_free(conn, engineer_id):
    if not engineer_id:
        return False
    cur = conn.cursor()
    cur.execute("SELECT id FROM engineers WHERE id=?", (engineer_id,))
    if not cur.fetchone():
        return False
    cur.execute("UPDATE engineers SET status='free', task=NULL WHERE id=?",
                (engineer_id,))
    return cur.rowcount > 0


def engineer_has_active_requests(conn, engineer_id, exclude_request_id=None):
    cur = conn.cursor()
    if exclude_request_id:
        cur.execute("""SELECT COUNT(*) FROM requests
            WHERE engineer_id=? AND status NOT IN ('completed','cancelled','flight_cancelled')
              AND id!=?""", (engineer_id, exclude_request_id))
    else:
        cur.execute("""SELECT COUNT(*) FROM requests
            WHERE engineer_id=? AND status NOT IN ('completed','cancelled','flight_cancelled')""",
                    (engineer_id,))
    return cur.fetchone()[0]


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
                   WHERE status NOT IN ('completed','cancelled','flight_cancelled')
                     AND engineer_id IS NOT NULL""")
    busy_with_requests = {r['engineer_id'] for r in cur.fetchall()}

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
                   WHERE status NOT IN ('completed','cancelled','flight_cancelled')
                     AND engineer_id IS NOT NULL""")
    busy = {r['engineer_id'] for r in cur.fetchall()}

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
            travel_time=?, using_car=?, walk_to_car=?, car_time=?, best_car=?,
            status='assigned', routing_stage='technician',
            delay_minutes=0, delay_reason='', delay_started_at=NULL,
            delay_approved_by='', all_rejected=0
            WHERE id=?""",
            (eng['id'], eng['name'], route['travel_time'],
             1 if route['using_car'] else 0, route['walk_to_car'],
             route['best_car_time'],
             route['best_car']['device_id'] if route['best_car'] else None, q['id']))
        if route['using_car'] and route['best_car']:
            cur.execute("UPDATE transport SET assigned_to=? WHERE id=?",
                        (q['id'], route['best_car']['id']))
        cur.execute("UPDATE engineers SET status='busy', task=? WHERE id=?",
                    ('ВС ' + (q['aircraft'] or ''), eng['id']))
        busy.add(eng['id'])
        assigned.append(q['id'])
    conn.commit()
    conn.close()

    for rid in assigned:
        _recalc_route_for_request_id(rid, force=True)

# ============================================================
# Пересчёт ROUTE
# ============================================================
def _recompute_route_for_request(request_row):
    eng_id = request_row['engineer_id']
    parking = str(request_row['parking_id'] or '')
    if not eng_id or not parking:
        return None

    target = PARKING_COORDS.get(parking, (55.975, 37.410))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, coords_lat, coords_lng FROM engineers WHERE id=?", (eng_id,))
    eng = cur.fetchone()
    if not eng:
        conn.close()
        return None

    e_lat = eng['coords_lat'] or 55.975
    e_lng = eng['coords_lng'] or 37.410

    walk_path, walk_dist = get_shortest_path(
        road_graph_vehicle, e_lat, e_lng, target[0], target[1]
    )
    if walk_path is None:
        walk_path = [[e_lat, e_lng], [target[0], target[1]]]
        walk_dist = distance_km(e_lat, e_lng, target[0], target[1])
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


def _recalc_route_for_request_id(request_id, force=False):
    if not request_id:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, engineer_id, parking_id, engineer_comment, status
                   FROM requests WHERE id=? AND status NOT IN
                   ('completed','cancelled','flight_cancelled','draft')""", (request_id,))
    req = cur.fetchone()
    if not req:
        conn.close()
        return
    old_comment = req['engineer_comment'] or ''
    if not force and old_comment.startswith('ROUTE:') and req['status'] in ('assigned', 'arriving', 'in_progress', 'working'):
        conn.close()
        return

    result = _recompute_route_for_request(req)
    if not result:
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
                   WHERE engineer_id=? AND status NOT IN
                   ('completed','cancelled','flight_cancelled','draft')""", (eng_id,))
    ids = [r['id'] for r in cur.fetchall()]
    conn.close()
    for rid in ids:
        _recalc_route_for_request_id(rid, force=force)
        
def _recalc_all_active_routes():
    """Пересчитывает ROUTE для всех активных назначенных заявок.
    Вызывается после отказа инженера — освободился транспорт/инженер,
    маршруты остальных заявок могли измениться."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id FROM requests
                   WHERE status NOT IN ('completed','cancelled','flight_cancelled','draft','queued')
                     AND engineer_id IS NOT NULL
                     AND parking_id IS NOT NULL AND parking_id != ''""")
    ids = [r['id'] for r in cur.fetchall()]
    conn.close()
    for rid in ids:
        try:
            _recalc_route_for_request_id(rid)
        except Exception as e:
            print(f'[RECALC] {rid}: {e}')


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
# MCC
# ============================================================
def _compute_recurring_map(conn):
    cur = conn.cursor()
    cur.execute("""SELECT aircraft, fault_type, COUNT(*) AS cnt, MAX(created_at) AS last_seen
                   FROM requests
                   WHERE aircraft IS NOT NULL AND aircraft!='' AND fault_type IS NOT NULL
                     AND status NOT IN ('completed','cancelled','flight_cancelled','draft')
                     AND created_at >= datetime('now','-30 days')
                   GROUP BY aircraft, fault_type""")
    out = {}
    for r in cur.fetchall():
        out[(str(r['aircraft']).strip(), str(r['fault_type']).strip())] = {
            'occurrences': r['cnt'] or 0, 'last_occurrence': r['last_seen']
        }
    return out


@app.route('/api/mcc/defects', methods=['GET'])
def get_mcc_defects():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, fault_type, description, parking_id, terminal,
        source, pilot_name, pilot_rank, pilot_audio_text, engineer_id, engineer_name,
        created_at, status, mcc_status, mcc_decision, mcc_comment, mcc_ata,
        mcc_mel_choice, mcc_mel_ref, mcc_amm_verdict, mcc_proc_o, mcc_proc_m,
        mcc_proc_mh, mcc_mel_category, mcc_due_date, mcc_combined_check,
        mcc_recurring_action, mcc_escalation, mcc_dce_name, mcc_kupol_ref,
        aog_since, etr, reported_by, severity, criticality,
        engineer_verdict, engineer_verdict_at, aog_action, inspection_assigned_at,
        flight_number, travel_time, using_car, best_car,
        crs_signature, crs_signed_at, crs_signed_by,
        routing_stage, routing_history
        FROM requests WHERE status NOT IN ('cancelled','flight_cancelled','draft')
        ORDER BY created_at DESC""")
    rows = cur.fetchall()
    rec_map = _compute_recurring_map(conn)
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
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
                   routing_stage='cpu',
                   mcc_comment=COALESCE(mcc_comment,'') || ?,
                   mcc_status='INSPECTION'
                   WHERE id=?""",
                ('\n[→ЦПУ] ' + comment, rid))
    _log_routing_event(conn, rid, 'cpu', 'MCC', comment)
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'cpu'})


@app.route('/api/mcc/assign_inspection', methods=['POST'])
def mcc_assign_inspection_legacy():
    return mcc_send_to_cpu()


@app.route('/api/engineer/verdict', methods=['POST'])
def engineer_verdict():
    data = request.get_json() or {}
    rid = data.get('request_id')
    verdict = (data.get('verdict') or '').upper()
    comment = (data.get('comment') or '').strip()
    if not rid or verdict not in ('AOG', 'REPAIR', 'MEL'):
        return jsonify({'success': False, 'error': 'Неверный вердикт'}), 400
    if not comment:
        return jsonify({'success': False, 'error': 'Комментарий обязателен'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, engineer_id, mcc_status, engineer_comment FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404

    old_comment = row['engineer_comment'] or ''
    new_comment = old_comment + '\n[Вердикт] ' + verdict + ': ' + comment
    cur.execute("""UPDATE requests SET
        engineer_verdict=?, engineer_verdict_at=CURRENT_TIMESTAMP, engineer_comment=?
        WHERE id=?""", (verdict, new_comment, rid))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'verdict': verdict})


@app.route('/api/mcc/finalize', methods=['POST'])
def mcc_finalize():
    data = request.get_json() or {}
    rid = data.get('request_id')
    action = data.get('action')
    comment = (data.get('comment') or '').strip()
    if not rid or action not in ('AOG_RECOVERY', 'MEL_DEFER', 'CLOSE'):
        return jsonify({'success': False, 'error': 'Неверное действие'}), 400
    if not comment:
        return jsonify({'success': False, 'error': 'Комментарий обязателен'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, engineer_verdict, engineer_id, mcc_status FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['mcc_status'] != 'INSPECTION':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не на инспекции'}), 400
    if not row['engineer_verdict']:
        conn.close()
        return jsonify({'success': False, 'error': 'Нет вердикта инженера'}), 400

    kupol_ref = 'KP-' + str(int(time.time()))
    dce_name = data.get('dce_name') or 'DCE'

    if action == 'AOG_RECOVERY':
        if row['engineer_verdict'] != 'AOG':
            conn.close()
            return jsonify({'success': False, 'error': 'Инженер не подтвердил AOG'}), 400
        etr = data.get('etr', '')
        aog_action = data.get('aog_action', '')
        if not etr:
            conn.close()
            return jsonify({'success': False, 'error': 'Укажите ETR'}), 400
        cur.execute("""UPDATE requests SET
            mcc_status='AOG_CONFIRMED', status='aog_waiting',
            mcc_decision='AOG_RECOVERY',
            mcc_comment=COALESCE(mcc_comment,'') || ?,
            etr=?, aog_action=?, mcc_dce_name=?, mcc_kupol_ref=?,
            aog_since=COALESCE(aog_since,CURRENT_TIMESTAMP)
            WHERE id=?""",
            ('\n[MCC] AOG Recovery: ' + comment, etr, aog_action, dce_name, kupol_ref, rid))
        _log_routing_event(conn, rid, 'aog_recovery', 'MCC', 'ETR=' + etr)

    elif action == 'MEL_DEFER':
        cat = data.get('mel_category', '')
        ref = data.get('mel_ref', '')
        due = data.get('mel_due_date', '')
        if not cat or not ref:
            conn.close()
            return jsonify({'success': False, 'error': 'Укажите MEL категорию и ссылку'}), 400
        cur.execute("""UPDATE requests SET
            mcc_status='MEL', status='deferred', mcc_decision='DEFER',
            mcc_comment=COALESCE(mcc_comment,'') || ?,
            mcc_mel_category=?, mcc_mel_ref=?, mcc_due_date=?,
            mcc_dce_name=?, mcc_kupol_ref=?
            WHERE id=?""",
            ('\n[MCC] MEL: ' + comment, cat, ref, due, dce_name, kupol_ref, rid))
        cur.execute("UPDATE requests SET routing_stage='pilot_crs' WHERE id=?", (rid,))
        _log_routing_event(conn, rid, 'pilot_crs', 'MCC', 'MEL ' + cat + ' ' + ref)
        if row['engineer_id']:
            cur.execute("UPDATE engineers SET status='free', task=NULL WHERE id=?",
                        (row['engineer_id'],))

    elif action == 'CLOSE':
        cur.execute("""UPDATE requests SET
            mcc_status='REPAIRED', status='completed', mcc_decision='CLOSE',
            mcc_comment=COALESCE(mcc_comment,'') || ?,
            mcc_dce_name=?, mcc_kupol_ref=?
            WHERE id=?""",
            ('\n[MCC] Закрыто: ' + comment, dce_name, kupol_ref, rid))
        cur.execute("UPDATE requests SET routing_stage='pilot_crs' WHERE id=?", (rid,))
        _log_routing_event(conn, rid, 'pilot_crs', 'MCC', 'Закрыто: ' + comment)
        if row['engineer_id']:
            cur.execute("UPDATE engineers SET status='free', task=NULL WHERE id=?",
                        (row['engineer_id'],))

    conn.commit()
    conn.close()
    return jsonify({
        'success': True, 'request_id': rid, 'action': action,
        'mcc_status': {'AOG_RECOVERY': 'AOG_CONFIRMED', 'MEL_DEFER': 'MEL', 'CLOSE': 'REPAIRED'}.get(action, action)
    })


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
        if clear_type == 'queue':
            cur.execute("DELETE FROM requests WHERE mcc_status='PENDING'")
            deleted = cur.rowcount
        elif clear_type == 'inspection':
            cur.execute("DELETE FROM requests WHERE mcc_status='INSPECTION'")
            deleted = cur.rowcount
        elif clear_type == 'deferred':
            cur.execute("DELETE FROM requests WHERE mcc_status='MEL'")
            deleted = cur.rowcount
        elif clear_type == 'aog':
            cur.execute("DELETE FROM requests WHERE mcc_status='AOG_CONFIRMED'")
            deleted = cur.rowcount
        elif clear_type == 'history':
            cur.execute("DELETE FROM requests WHERE mcc_status IN ('REPAIRED','RELEASED','CLOSED')")
            deleted = cur.rowcount
        elif clear_type == 'all':
            cur.execute("DELETE FROM requests WHERE status!='draft'")
            deleted = cur.rowcount
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
    cur.execute("UPDATE requests SET status='new', mcc_status='PENDING', routing_stage='mcc' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/pilot/sign_crs/<rid>', methods=['POST'])
def pilot_sign_crs(rid):
    data = request.get_json() or {}
    signature = data.get('signature')
    signed_by = (data.get('signed_by') or '').strip()
    if not signature or not signed_by:
        return jsonify({'success': False, 'error': 'Нужны signature и signed_by'}), 400
    if not signature.startswith('data:image/'):
        return jsonify({'success': False, 'error': 'Неверный формат подписи'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, mcc_status FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    if row['mcc_status'] not in ('REPAIRED', 'MEL'):
        conn.close()
        return jsonify({'success': False, 'error': 'CRS только после REPAIRED или MEL'}), 400

    cur.execute("""UPDATE requests SET
        status='completed', mcc_status='RELEASED', routing_stage='released',
        crs_signature=?, crs_signed_at=CURRENT_TIMESTAMP, crs_signed_by=?
        WHERE id=?""", (signature, signed_by, rid))
    _log_routing_event(conn, rid, 'released', 'Пилот', 'CRS подписан: ' + signed_by)
    conn.commit()
    conn.close()
    print(f'[pilot/sign_crs] {rid} подписан {signed_by}')
    return jsonify({'success': True, 'signed_at': datetime.now(timezone.utc).isoformat()})
@app.route('/api/cpu/queue', methods=['GET'])
def cpu_queue():
    """Заявки, ожидающие решения ЦПУ."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description,
                   parking_id, terminal, source, pilot_name, pilot_rank,
                   mcc_status, mcc_decision, mcc_comment, routing_history,
                   created_at, severity, criticality, engineer_comment
                   FROM requests
                   WHERE routing_stage='cpu'
                     AND status NOT IN ('cancelled','flight_cancelled','completed','draft')
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
    """ЦПУ передаёт заявку диспетчеру. Инженер назначается автоматически
    тем же алгоритмом, что при ручном создании заявки в /api/dispatch."""
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
        return jsonify({'success': False, 'error': 'Заявка не в очереди ЦПУ'}), 400

    # Логируем переход ЦПУ → Диспетчер.
    # source='pilot' → 'cpu', чтобы заявка перестала считаться «пилотской»
    # и отображалась у диспетчера в «Активных заявках».
    cur.execute("""UPDATE requests SET
                   routing_stage='dispatcher',
                   source=CASE WHEN source='pilot' THEN 'cpu' ELSE source END,
                   mcc_comment=COALESCE(mcc_comment,'') || ?
                   WHERE id=?""",
                ('\n[ЦПУ→Диспетчер] ' + comment, rid))
    _log_routing_event(conn, rid, 'dispatcher', 'ЦПУ', comment)

    # ── АВТОНАЗНАЧЕНИЕ (как в /api/dispatch) ──
    parking_id = str(row['parking_id'] or '')
    fault = row['fault_type'] or 'engine_failure'
    aircraft = row['aircraft'] or 'ВС'
    target = PARKING_COORDS.get(parking_id, (55.975, 37.410))

    cur.execute("""SELECT DISTINCT engineer_id FROM requests
        WHERE status NOT IN ('completed','cancelled','flight_cancelled')
          AND engineer_id IS NOT NULL AND id!=?""", (rid,))
    busy = {r['engineer_id'] for r in cur.fetchall()}

    eng, route = get_free_engineer_for(conn, fault, FAULT_SKILL,
                                       target[0], target[1],
                                       exclude_ids=busy)

    if eng:
        cur.execute("""UPDATE requests SET
                       engineer_id=?, engineer_name=?,
                       status='assigned', routing_stage='technician',
                       mcc_status='INSPECTION',
                       travel_time=?, using_car=?, walk_to_car=?, car_time=?, best_car=?,
                       mcc_comment=COALESCE(mcc_comment,'') || ?
                       WHERE id=?""",
                    (eng['id'], eng['name'],
                     route['travel_time'],
                     1 if route['using_car'] else 0,
                     route['walk_to_car'],
                     route['best_car_time'],
                     route['best_car']['device_id'] if route['best_car'] else None,
                     '\n[Автоназначение] ' + eng['name'], rid))

        if route['using_car'] and route['best_car']:
            cur.execute("UPDATE transport SET assigned_to=? WHERE id=?",
                        (rid, route['best_car']['id']))
        cur.execute("UPDATE engineers SET status='busy', task=? WHERE id=?",
                    ('ВС ' + aircraft, eng['id']))
        _log_routing_event(conn, rid, 'technician', 'Диспетчер (авто)',
                           f'Назначен {eng["name"]}')
        conn.commit()
        conn.close()

        try:
            _recalc_route_for_request_id(rid)
        except Exception as e:
            print(f'[CPU] recalc route: {e}')

        print(f'[CPU] auto-assigned {rid} → {eng["name"]} ({route["travel_time"]} min)')
        return jsonify({
            'success': True,
            'request_id': rid,
            'routing_stage': 'technician',
            'assigned': True,
            'engineer_id': eng['id'],
            'engineer_name': eng['name'],
            'travel_time': route['travel_time']
        })

    # Нет свободных — в очередь, try_reassign_queued подхватит
    cur.execute("""UPDATE requests SET
                   status='queued',
                   delay_started_at=COALESCE(delay_started_at, CURRENT_TIMESTAMP),
                   delay_reason=CASE WHEN delay_reason='' OR delay_reason IS NULL
                                     THEN 'Ожидание инженера' ELSE delay_reason END
                   WHERE id=?""", (rid,))
    _log_routing_event(conn, rid, 'dispatcher', 'Диспетчер (авто)',
                       'Нет свободных техников — в очереди')
    conn.commit()
    conn.close()
    _schedule_recheck(rid)
    print(f'[CPU] queued {rid} (no free engineers)')
    return jsonify({
        'success': True,
        'request_id': rid,
        'routing_stage': 'dispatcher',
        'assigned': False,
        'queued': True
    })


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
                   created_at, severity, criticality
                   FROM requests
                   WHERE routing_stage='dispatcher'
                     AND status NOT IN ('cancelled','flight_cancelled','completed','draft')
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

    cur.execute("SELECT id, name, device_id FROM engineers WHERE id=?", (eng_id,))
    eng = cur.fetchone()
    if not eng:
        conn.close()
        return jsonify({'success': False, 'error': 'Техник не найден'}), 404

    cur.execute("""UPDATE requests SET
                   engineer_id=?, engineer_name=?,
                   status='assigned', routing_stage='technician',
                   mcc_status='INSPECTION',
                   mcc_comment=COALESCE(mcc_comment,'') || ?
                   WHERE id=?""",
                (eng_id, eng['name'],
                 '\n[Диспетчер→Техник] ' + comment + ' — ' + eng['name'], rid))
    cur.execute("UPDATE engineers SET status='busy', task=? WHERE id=?",
                ('ВС ' + (req['aircraft'] or ''), eng_id))
    _log_routing_event(conn, rid, 'technician', 'Диспетчер',
                       f'Назначен {eng["name"]}: {comment}')
    conn.commit()
    conn.close()

    try:
        _recalc_route_for_request_id(rid)
    except Exception as e:
        print(f'[DISPATCHER] recalc route: {e}')

    conn2 = get_db()
    cur2 = conn2.cursor()
    cur2.execute("SELECT travel_time FROM requests WHERE id=?", (rid,))
    t = cur2.fetchone()
    travel_time = t['travel_time'] if t else 0
    conn2.close()

    return jsonify({
        'success': True, 'request_id': rid,
        'engineer_id': eng_id, 'engineer_name': eng['name'],
        'travel_time': travel_time,
    })


@app.route('/api/dispatcher/assigned', methods=['GET'])
def dispatcher_assigned():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.id, r.aircraft, r.fault_type, r.parking_id,
                   r.status, r.engineer_id, r.engineer_name, r.travel_time,
                   r.created_at, e.device_id, e.specialization
                   FROM requests r
                   LEFT JOIN engineers e ON e.id = r.engineer_id
                   WHERE r.routing_stage='technician'
                     AND r.status NOT IN ('completed','cancelled','flight_cancelled','draft')
                   ORDER BY r.created_at DESC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


# ============================================================
# Engineers
# ============================================================
@app.route('/api/engineers', methods=['GET'])
def get_engineers():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id,name,device_id,specialization,coords_lat,coords_lng,
                   status,task,phone,task_step,task_started_at FROM engineers""")
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
    cur = conn.cursor()
    cur.execute("""SELECT id,name,device_id,specialization,coords_lat,coords_lng,
                   status,task,phone,task_step,task_started_at FROM engineers WHERE id=?""", (eng_id,))
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
    return jsonify({'success': True})

@app.route('/api/engineers/<eng_id>/step', methods=['PUT'])
def update_engineer_step(eng_id):
    """Инженер сообщает свой текущий шаг: idle/pending/accepted/in_car/arrived/diagnosing/completed.
    При accepted — фиксируется task_started_at; при idle/completed — очищается."""
    data = request.get_json() or {}
    step = (data.get('step') or '').strip()
    valid = ('idle', 'pending', 'accepted', 'in_car', 'arrived', 'diagnosing', 'completed')
    if step not in valid:
        return jsonify({'success': False, 'error': 'Неверный step'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM engineers WHERE id=?", (eng_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'Не найден'}), 404

    if step == 'accepted':
        cur.execute(
            "UPDATE engineers SET task_step=?, "
            "task_started_at=COALESCE(task_started_at, CURRENT_TIMESTAMP) WHERE id=?",
            (step, eng_id))
    elif step in ('idle', 'completed'):
        cur.execute(
            "UPDATE engineers SET task_step=?, task_started_at=NULL WHERE id=?",
            (step, eng_id))
    else:
        cur.execute("UPDATE engineers SET task_step=? WHERE id=?", (step, eng_id))

    conn.commit()
    conn.close()
    return jsonify({'success': True, 'step': step})


@app.route('/api/engineers/<eng_id>', methods=['DELETE'])
def delete_engineer(eng_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM engineers WHERE id=?", (eng_id,))
    cur.execute("DELETE FROM users WHERE id=?", (eng_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/engineers/<eng_id>/coords', methods=['PUT'])
def update_engineer_coords(eng_id):
    data = request.get_json() or {}
    lat = data.get('lat')
    lng = data.get('lng')
    if lat is None or lng is None:
        return jsonify({'error': 'lat/lng обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE engineers SET coords_lat=?, coords_lng=? WHERE id=?",
                (lat, lng, eng_id))
    conn.commit()
    conn.close()
    _recalc_routes_for_engineer(eng_id)
    return jsonify({'success': True})


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
    cur.execute("SELECT id,device_id,lat,lng,speed,assigned_to FROM transport ORDER BY id")
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
    conn.commit()
    assigned_to = row['assigned_to']
    conn.close()
    if assigned_to:
        _recalc_route_for_request_id(assigned_to)
    return jsonify({'success': True})


@app.route('/api/transport/<int:tid>', methods=['DELETE'])
def delete_transport(tid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM transport WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# Requests
# ============================================================
@app.route('/api/requests', methods=['GET'])
def get_requests():
    conn = get_db()
    cur = conn.cursor()
    source = request.args.get('source')
    pilot_name = request.args.get('pilot_name')

    sql = """SELECT id, aircraft, flight_number, fault_type, severity, terminal, parking_id,
        description, source, engineer_id, engineer_name,
        travel_time, using_car, walk_to_car, car_time, best_car,
        status, criticality, engineer_comment, created_at,
        pilot_name, pilot_rank, pilot_audio_text, rejected_by,
        cancel_reason, cancelled_by_name, cancelled_at,
        replaces_request_id, delay_minutes, delay_reason, delay_started_at,
        delay_approved_by, flight_cancelled, flight_cancel_reason,
        flight_cancelled_at, all_rejected, mcc_status, mcc_decision, mcc_comment,
        engineer_verdict, engineer_verdict_at, aog_action, inspection_assigned_at,
        mcc_mel_ref, mcc_mel_category, mcc_due_date,
        crs_signature, crs_signed_at, crs_signed_by,
        routing_stage, routing_history
        FROM requests WHERE status != 'draft'"""
    params = []
    if source:
        sql += " AND source=?"
        params.append(source)
    if pilot_name:
        sql += " AND pilot_name=?"
        params.append(pilot_name)
    sql += " ORDER BY created_at DESC"

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

    cur.execute("""UPDATE requests
        SET status=COALESCE(?,status), criticality=COALESCE(?,criticality),
            engineer_comment=COALESCE(?,engineer_comment),
            aircraft=COALESCE(?,aircraft), fault_type=COALESCE(?,fault_type),
            terminal=COALESCE(?,terminal), parking_id=COALESCE(?,parking_id),
            severity=COALESCE(?,severity), flight_number=COALESCE(?,flight_number)
        WHERE id=?""",
                (data.get('status'), data.get('criticality'),
                 data.get('engineer_comment'), data.get('aircraft'),
                 data.get('fault_type'), data.get('terminal'),
                 data.get('parking_id'), data.get('severity'),
                 data.get('flight_number'), rid))

    new_status = data.get('status')
    if new_status == 'completed':
        cur.execute("UPDATE requests SET routing_stage='mcc_finalize' WHERE id=? AND routing_stage='technician'", (rid,))
        cur.execute("SELECT engineer_id FROM requests WHERE id=?", (rid,))
        r = cur.fetchone()
        if r and r['engineer_id']:
            other = engineer_has_active_requests(conn, r['engineer_id'], rid)
            if other == 0:
                cur.execute("UPDATE engineers SET status='free', task=NULL WHERE id=?",
                            (r['engineer_id'],))
        cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    elif new_status == 'cancelled':
        cur.execute("UPDATE requests SET routing_stage='done' WHERE id=?", (rid,))
        cur.execute("SELECT engineer_id FROM requests WHERE id=?", (rid,))
        r = cur.fetchone()
        if r and r['engineer_id']:
            other = engineer_has_active_requests(conn, r['engineer_id'], rid)
            if other == 0:
                cur.execute("UPDATE engineers SET status='free', task=NULL WHERE id=?",
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
    old_status = row['status']
    cur.execute("DELETE FROM requests WHERE id=?", (rid,))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    if eng_id and old_status not in ('completed', 'cancelled', 'flight_cancelled'):
        other = engineer_has_active_requests(conn, eng_id, rid)
        if other == 0:
            set_engineer_free(conn, eng_id)
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
        WHERE status NOT IN ('completed','cancelled','flight_cancelled')
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
                     route['travel_time'],
                     1 if route['using_car'] else 0,
                     route['walk_to_car'],
                     route['best_car_time'],
                     route['best_car']['device_id'] if route['best_car'] else None,
                     pilot_name, pilot_rank))

        if route['using_car'] and route['best_car']:
            cur.execute("UPDATE transport SET assigned_to=? WHERE id=?",
                        (rid, route['best_car']['id']))
        cur.execute("UPDATE engineers SET status='busy', task=? WHERE id=?",
                    ('ВС ' + aircraft, eng['id']))
        conn.commit()
        conn.close()

        _recalc_route_for_request_id(rid)

        conn2 = get_db()
        cur2 = conn2.cursor()
        cur2.execute("SELECT travel_time FROM requests WHERE id=?", (rid,))
        t = cur2.fetchone()
        final_time = t['travel_time'] if t else route['travel_time']
        conn2.close()

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
        cur.execute("UPDATE engineers SET status='free', task=NULL WHERE id=?", (old_eng,))
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
        WHERE status NOT IN ('completed','cancelled','flight_cancelled')
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
                     route['travel_time'], 1 if route['using_car'] else 0,
                     route['walk_to_car'], route['best_car_time'],
                     route['best_car']['device_id'] if route['best_car'] else None,
                     row['severity'], rej_str, rid))
        if route['using_car'] and route['best_car']:
            cur.execute("UPDATE transport SET assigned_to=? WHERE id=?",
                        (new_rid, route['best_car']['id']))
        cur.execute("UPDATE engineers SET status='busy', task=? WHERE id=?",
                    ('ВС ' + aircraft, new_eng['id']))
        conn.commit()
        conn.close()
        _recalc_route_for_request_id(new_rid)
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
                'travel_time': route['travel_time'],
                'using_car': route['using_car'],
                'walk_to_car': route['walk_to_car'],
                'car_time': route['best_car_time'],
                'best_car': route['best_car']['device_id'] if route['using_car'] else None,
                'path': route.get('path_coords')
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
        description,engineer_id,rejected_by,status FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    if row['status'] == 'completed':
        conn.close()
        return jsonify({'success': False, 'error': 'Уже выполнена'}), 400
    if not rejected_by:
        rejected_by = str(row['engineer_id'] or '')

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
        WHERE status NOT IN ('completed','cancelled','flight_cancelled')
          AND engineer_id IS NOT NULL AND id!=?""", (rid,))
    busy = {r['engineer_id'] for r in cur.fetchall()}

    new_eng, route = get_free_engineer_for(conn, fault, FAULT_SKILL,
                                           target[0], target[1],
                                           exclude_ids=rej_prev | busy)
    if new_eng:
        cur.execute("""UPDATE requests SET engineer_id=?, engineer_name=?,
            travel_time=?, using_car=?, walk_to_car=?, car_time=?, best_car=?,
            status='assigned', routing_stage='technician',
            rejected_by=?, all_rejected=0 WHERE id=?""",
                    (new_eng['id'], new_eng['name'], route['travel_time'],
                     1 if route['using_car'] else 0, route['walk_to_car'],
                     route['best_car_time'],
                     route['best_car']['device_id'] if route['best_car'] else None,
                     rej_str, rid))
        if route['using_car'] and route['best_car']:
            cur.execute("UPDATE transport SET assigned_to=? WHERE id=?",
                        (rid, route['best_car']['id']))
        set_engineer_busy(conn, new_eng['id'], 'ВС ' + aircraft)
        conn.commit()
        conn.close()
        _recalc_route_for_request_id(rid)
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
                'travel_time': route['travel_time'],
                'using_car': route['using_car'],
                'path': route.get('path_coords')
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
            cur.execute("UPDATE engineers SET status='free', task=NULL WHERE id=?", (eng,))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    cur.execute("DELETE FROM requests WHERE id=?", (rid,))
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
        AND status NOT IN ('completed','cancelled','flight_cancelled')""", (eng_id,))
    if cur.fetchall():
        conn.close()
        return jsonify({'success': False, 'reason': 'has_active_requests'})
    cur.execute("""UPDATE transport SET assigned_to=NULL
        WHERE assigned_to IN (SELECT id FROM requests WHERE engineer_id=?)""", (eng_id,))
    cur.execute("UPDATE engineers SET status='free', task=NULL WHERE id=?", (eng_id,))
    conn.commit()
    conn.close()
    try_reassign_queued()
    return jsonify({'success': True})


@app.route('/api/clear_all_requests', methods=['POST'])
def clear_all_requests():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM requests WHERE status != 'draft'")
        deleted = cur.rowcount
        cur.execute("UPDATE engineers SET status='free', task=NULL")
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
        cur.execute("DELETE FROM requests WHERE source='pilot'")
        deleted = cur.rowcount
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
# Запуск
# ============================================================
def _startup():
    init_db()
    load_svo_geojson()
    load_road_graph()


_startup()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)