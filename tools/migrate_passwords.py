"""Миграция открытых паролей в таблице users на хэши werkzeug.

Запуск:  .venv/Scripts/python.exe tools/migrate_passwords.py [путь к базе]
По умолчанию используется database.db в корне проекта.
Скрипт идемпотентен: уже захэшированные пароли не трогает.
"""
import os
import sqlite3
import sys

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_hash(value):
    return isinstance(value, str) and (
        value.startswith('pbkdf2:') or value.startswith('scrypt:'))


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, 'database.db')
    if not os.path.isfile(db_path):
        raise SystemExit(f'База не найдена: {db_path}')

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id, password FROM users").fetchall()
        migrated = 0
        for user_id, password in rows:
            if not password or is_hash(password):
                continue
            conn.execute("UPDATE users SET password=? WHERE id=?",
                         (generate_password_hash(password), user_id))
            migrated += 1
        conn.commit()
    finally:
        conn.close()
    print(f'Готово: захэшировано паролей — {migrated}, всего пользователей — {len(rows)}')


if __name__ == '__main__':
    main()
