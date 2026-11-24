"""География ВДНХ: стоянки, объекты, зоны, расстояния (вынесено из app.py)."""
import json
import math
import os
import re
import threading
from datetime import datetime, timezone

from intravia.config import BASE_DIR, LOCATION_CONFIG_PATH, STATIC_DIR
from intravia.db import get_db

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
    # Идентификаторы зон организаторов: mc-01 / мс-01 -> SZ-1.
    alias = None
    match = re.match(r'^(?:mc|мс)[-_]?(\d+)$', pid, re.IGNORECASE)
    if match:
        alias = f'SZ-{int(match.group(1))}'
    try:
        location = load_location_config()
    except Exception:
        return None
    for parking in location.get('parkings', []):
        zone_id = parking.get('zone_id')
        if pid in (str(parking.get('id')), str(zone_id), f'SZ-{zone_id}') or \
                (alias and alias == f'SZ-{zone_id}'):
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
        # Базовые координаты инженеров вне полигона (легаси SVO) переносим
        # в центр ВДНХ, чтобы до прихода GPS они не висели в Шереметьево.
        center = load_location_config().get('center') or {}
        if center.get('lat') is not None and center.get('lon') is not None:
            cur.execute("""UPDATE engineers SET coords_lat=?, coords_lng=?
                           WHERE coords_lat IS NULL OR coords_lng IS NULL
                              OR coords_lat < ? OR coords_lat > ?
                              OR coords_lng < ? OR coords_lng > ?""",
                        (float(center['lat']), float(center['lon']),
                         min_lat, max_lat, min_lon, max_lon))
            moved = cur.rowcount
            if moved:
                print(f'[LOCATION] Инженеров переставлено в центр ВДНХ: {moved}')
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
    alias = None
    match = re.match(r'^(?:mc|мс)[-_]?(\d+)$', pid, re.IGNORECASE)
    if match:
        alias = f'SZ-{int(match.group(1))}'
        meta = PARKING_META.get(alias)
        if meta and (meta.get('polygon') or meta.get('geometry')):
            return meta.get('polygon') or meta.get('geometry')
    try:
        location = load_location_config()
    except Exception:
        return None
    for parking in location.get('parkings', []):
        zone_id = parking.get('zone_id')
        if pid in (str(parking.get('id')), str(zone_id), f'SZ-{zone_id}') or \
                (alias and alias == f'SZ-{zone_id}'):
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
