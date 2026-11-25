"""Подключение к БД и справочник инженеров (вынесено из app.py)."""
import os
import re
import sqlite3

from intravia.config import DB_PATH

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
    """Проверяет существующую БД и идемпотентно гарантирует таблицу назначений.

    Данные не создаются и не изменяются; единственное разрешённое изменение
    схемы — CREATE TABLE IF NOT EXISTS engineer_assignment (нужна API и тестам).
    """
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
        cur.execute("""CREATE TABLE IF NOT EXISTS engineer_assignment(
                       engineer_id TEXT PRIMARY KEY,
                       at_id TEXT UNIQUE,
                       grade TEXT,
                       scope TEXT,
                       skill TEXT,
                       scope_desc TEXT,
                       is_live INTEGER DEFAULT 0,
                       updated_at TEXT)""")
        conn.commit()
        print(f'[DB] Проверка завершена. Таблиц найдено: {len(existing)}. '
              'Таблица engineer_assignment гарантирована.')
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
