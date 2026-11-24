"""Стартовые позиции ВДНХ: инженеры — Терминал, машины — Стоянка спецтехники.

Идемпотентный скрипт: делает резервную копию database.db и обновляет только
координаты. Схема БД не меняется.

Запуск:  .venv/Scripts/python.exe tools/apply_vdnh_location.py
"""
import json
import os
import shutil
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get('INTRAVIA_DB', '').strip() or os.path.join(BASE_DIR, 'database.db')
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'location.json')


def object_centroid(config, role):
    for item in config.get('objects', []):
        if item.get('role') == role:
            centroid = item.get('centroid') or {}
            return float(centroid['lat']), float(centroid['lon'])
    raise SystemExit(f'В конфиге нет объекта с ролью {role}')


def main():
    if not os.path.isfile(DB_PATH):
        raise SystemExit(f'База не найдена: {DB_PATH}')
    with open(CONFIG_PATH, encoding='utf-8-sig') as fh:
        config = json.load(fh)

    terminal = object_centroid(config, 'terminal')
    transport_park = object_centroid(config, 'transport_park')

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = os.path.join(os.path.dirname(DB_PATH), f'database_backup_{stamp}.db')
    shutil.copyfile(DB_PATH, backup)

    conn = sqlite3.connect(DB_PATH)
    try:
        engineers = conn.execute(
            "UPDATE engineers SET coords_lat=?, coords_lng=?",
            (terminal[0], terminal[1])).rowcount
        cars = conn.execute(
            "UPDATE transport SET lat=?, lng=?",
            (transport_park[0], transport_park[1])).rowcount
        conn.commit()
    finally:
        conn.close()

    print(f'backup: {backup}')
    print(f'engineers -> Терминал ({terminal[0]}, {terminal[1]}): {engineers}')
    print(f'transport -> Стоянка спецтехники ({transport_park[0]}, {transport_park[1]}): {cars}')


if __name__ == '__main__':
    main()
