"""Дорожные графы и маршрутизация (вынесено из app.py)."""
import math

import networkx as nx

from intravia.db import get_db
from intravia.geo import distance_km, load_location_config

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

# Пешеход всегда идёт 5 км/ч, независимо от скорости ребра дороги
# (service может быть 8/10/20 км/ч — это скорость транспорта).
WALK_SPEED_KMH = 5.0


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


def _attach_projection(graph, point_node, projection, speed_override=None):
    """Подключает точку к спроецированному ребру через узел проекции.

    В граф добавляется перпендикуляр ``точка → проекция`` и участки
    ``проекция → u/v`` вдоль ребра. Поэтому подход к графу попадает в path,
    distance_m и time_s, а не рисуется прямой от точки до конца ребра.

    Возвращает узел проекции (lat, lng).
    """
    data = projection['data']
    edge_speed = max(float(data.get('speed_kmh') or 5.0), 1.0)
    perp_speed = max(float(speed_override), 1.0) if speed_override else edge_speed
    proj_node = (float(projection['lat']), float(projection['lng']))
    perp_m = max(float(projection['distance_m'] or 0.0), 0.0)

    graph.add_edge(point_node, proj_node,
                   weight=perp_m / (perp_speed / 3.6),
                   distance_m=perp_m,
                   speed_kmh=perp_speed,
                   road_type='approach',
                   allowed_agents=(),
                   closed=False)
    for endpoint, along_m in ((projection['u'], projection['t'] * projection['edge_len']),
                              (projection['v'], (1 - projection['t']) * projection['edge_len'])):
        # Проекция совпала с концом ребра — отдельное ребро не нужно.
        if endpoint == proj_node:
            continue
        along_m = max(float(along_m), 0.0)
        graph.add_edge(proj_node, endpoint,
                       weight=along_m / (edge_speed / 3.6),
                       distance_m=along_m,
                       speed_kmh=edge_speed,
                       road_type=data.get('road_type'),
                       allowed_agents=data.get('allowed_agents') or (),
                       closed=False)
    return proj_node


def within_location_bbox(lat, lng):
    """Точка внутри bbox полигона ВДНХ (как фильтр агентов на карте)."""
    try:
        bbox = load_location_config().get('bbox') or []
        if len(bbox) != 4:
            return True
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except Exception:
        return True
    return min_lat <= lat <= max_lat and min_lon <= lng <= max_lon


def filter_transport_cars(cars, max_graph_distance_m=300.0):
    """Оставляет машины внутри полигона и рядом с транспортным графом.

    CAR-004/005/006 стоят в Шереметьево в 16-20 км от ВДНХ: их нельзя
    предлагать для заявок, иначе в маршрут попадает сегмент в десятки км.
    """
    result = []
    graph = GRAPH_VEHICLE
    for car in cars or []:
        if car.get('lat') is None or car.get('lng') is None:
            continue
        try:
            lat = float(car['lat'])
            lng = float(car['lng'])
        except (TypeError, ValueError):
            continue
        if not within_location_bbox(lat, lng):
            continue
        if graph is not None and graph.edges:
            proj = _edge_projection(graph, lat, lng)
            if proj is not None and proj['distance_m'] > max_graph_distance_m:
                continue
        result.append(car)
    return result


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

    walk_mode = mode in ('walk', 'engineer')
    walk_speed = WALK_SPEED_KMH if walk_mode else None

    graph_copy = graph.copy()
    start_node = ('__virt__', 'start')
    end_node = ('__virt__', 'end')
    start_proj = _attach_projection(graph_copy, start_node, sp,
                                    speed_override=walk_speed)
    end_proj = _attach_projection(graph_copy, end_node, ep,
                                  speed_override=walk_speed)

    # Старт и цель на одном ребре — прямое соединение вдоль него
    # (между точками проекции, а не исходными точками).
    if ({sp['u'], sp['v']} == {ep['u'], ep['v']} and sp['u'] != sp['v']
            and start_proj != end_proj):
        direct_m = abs(sp['t'] - ep['t']) * sp['edge_len']
        speed = (WALK_SPEED_KMH if walk_mode
                 else max(float(sp['data'].get('speed_kmh') or 5.0), 1.0))
        graph_copy.add_edge(start_proj, end_proj,
                            distance_m=max(direct_m, 0.0),
                            weight=max(direct_m, 0.0) / (speed / 3.6),
                            speed_kmh=speed,
                            road_type=sp['data'].get('road_type'),
                            allowed_agents=sp['data'].get('allowed_agents') or (),
                            closed=False)

    if walk_mode:
        # Пешеход всегда 5 км/ч: пересчитываем веса всего графа-копии,
        # иначе Dijkstra выберет путь по «быстрым» автодорогам (8-20 км/ч).
        for _, _, data in graph_copy.edges(data=True):
            dist = float(data.get('distance_m') or 0.0)
            data['weight'] = dist / (WALK_SPEED_KMH / 3.6)
            data['speed_kmh'] = WALK_SPEED_KMH

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
        cars = filter_transport_cars([dict(r) for r in cur.fetchall()])
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
