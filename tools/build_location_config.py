"""Сборка config/location.json из geojson ВДНХ.

Запуск:  .venv/Scripts/python.exe tools/build_location_config.py
Идемпотентен: перезаписывает config/location.json текущими данными.
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEO_DIR = os.path.join(BASE_DIR, 'static', 'geodata', 'vdnh')
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'location.json')

OBJECT_ROLES = {
    'Терминал': 'terminal',
    'Ангар': 'hangar',
    'ВПП': 'runway',
    'Взлётно-посадочная полоса': 'runway',
    'Стоянка спецтехники': 'transport_park',
    'Склад №1': 'warehouse1',
    'Склад №2': 'warehouse2',
}


def load(name):
    with open(os.path.join(GEO_DIR, name), encoding='utf-8') as fh:
        return json.load(fh)


def rings(geometry):
    """Возвращает список внешних колец для Polygon/MultiPolygon."""
    if not geometry:
        return []
    if geometry['type'] == 'Polygon':
        return [geometry['coordinates'][0]]
    if geometry['type'] == 'MultiPolygon':
        return [poly[0] for poly in geometry['coordinates']]
    return []


def centroid(geometry):
    points = [pt for ring in rings(geometry) for pt in ring]
    if not points:
        return None
    lon = sum(p[0] for p in points) / len(points)
    lat = sum(p[1] for p in points) / len(points)
    return {'lat': round(lat, 6), 'lon': round(lon, 6)}


def bbox(geometry):
    points = [pt for ring in rings(geometry) for pt in ring]
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return [round(min(lons), 6), round(min(lats), 6),
            round(max(lons), 6), round(max(lats), 6)]


def main():
    roads = load('roads.geojson')
    objects = load('objects.geojson')
    zones = load('service_zones.geojson')
    boundary = load('airport_boundaries.geojson')

    road_types = {}
    speeds = {}
    for feature in roads['features']:
        props = feature['properties'] or {}
        road_type = props.get('type')
        if not road_type:
            continue
        road_types[road_type] = road_types.get(road_type, 0) + 1
        if props.get('spd_kmh'):
            speeds.setdefault(road_type, set()).add(float(props['spd_kmh']))

    obj_items = []
    for feature in objects['features']:
        props = feature['properties'] or {}
        name = props.get('name') or f"object-{props.get('id')}"
        obj_items.append({
            'id': OBJECT_ROLES.get(name, 'other'),
            'role': OBJECT_ROLES.get(name, 'other'),
            'name': name,
            'centroid': centroid(feature['geometry']),
            'geometry': feature['geometry'],
        })

    parking_items = []
    for feature in zones['features']:
        props = feature['properties'] or {}
        zone_id = props.get('id')
        parking_items.append({
            'id': f'SZ-{zone_id}',
            'zone_id': zone_id,
            'label': f'Стоянка SZ-{zone_id}',
            'centroid': centroid(feature['geometry']),
            'geometry': feature['geometry'],
        })

    boundary_geometry = boundary['features'][0]['geometry']
    boundary_bbox = bbox(boundary_geometry)
    # Центр полигона из задания ИТ-2026.
    center_lat, center_lon = 55.83367, 37.62709

    config = {
        'id': 'vdnh',
        'name': 'ВДНХ — тестовый полигон ИТ-2026',
        'center': {'lat': center_lat, 'lon': center_lon},
        'bbox': boundary_bbox,
        'files': {
            'roads': 'static/geodata/vdnh/roads.geojson',
            'objects': 'static/geodata/vdnh/objects.geojson',
            'service_zones': 'static/geodata/vdnh/service_zones.geojson',
            'boundary': 'static/geodata/vdnh/airport_boundaries.geojson',
        },
        'road_rules': {
            'service': {'agents': ['engineer', 'transport'], 'default_speed_kmh': 8},
            'pedestrian': {'agents': ['engineer'], 'default_speed_kmh': 5},
            'taxi': {'agents': ['aircraft'], 'default_speed_kmh': 10},
            'runway': {'agents': ['aircraft'], 'default_speed_kmh': 300},
        },
        'road_stats': {
            'counts': road_types,
            'speeds': {k: sorted(v) for k, v in speeds.items()},
        },
        'delays': {'boarding_s': 10, 'warehouse_s': 30},
        'objects': obj_items,
        'parkings': parking_items,
        'regulation_minutes': 15,
        'walk_speed_kmh': 5.0,
        'boundary': boundary_geometry,
    }

    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
    print(f'config written: {CONFIG_PATH}')
    print('objects:', [(o['id'], o['name']) for o in obj_items])
    print('parkings:', len(parking_items), 'roads:', road_types)


if __name__ == '__main__':
    main()
