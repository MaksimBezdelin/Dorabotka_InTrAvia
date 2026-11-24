"""Смена пароля демонстрационной учётной записи InTrAvia.

Пароль хранится только как хэш werkzeug (scrypt), в вывод не попадает.

Примеры:
    .venv\\Scripts\\python.exe tools/rotate_demo_passwords.py --email admin@aeroflot.ru
    .venv\\Scripts\\python.exe tools/rotate_demo_passwords.py --email admin@aeroflot.ru --password '...'
    .venv\\Scripts\\python.exe tools/rotate_demo_passwords.py          # ничего не делает

Без аргументов скрипт не меняет базу. Если не хватает --email или --password,
недостающее значение запрашивается интерактивно (пароль — через getpass).
"""
import argparse
import getpass
import os
import sqlite3
import sys

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get('INTRAVIA_DB') or os.path.join(BASE_DIR, 'database.db')


def parse_args():
    parser = argparse.ArgumentParser(
        description='Сменить пароль пользователя InTrAvia (в users.password).')
    parser.add_argument('--email', help='email учётной записи')
    parser.add_argument('--password', help='новый пароль (лучше не передавать в командной строке)')
    parser.add_argument('--db', default=DEFAULT_DB,
                        help='путь к database.db (по умолчанию INTRAVIA_DB или database.db)')
    return parser.parse_args()


def ask_password():
    first = getpass.getpass('Новый пароль: ')
    second = getpass.getpass('Повторите пароль: ')
    if first != second:
        raise SystemExit('Пароли не совпадают; база не изменена.')
    return first


def main():
    args = parse_args()

    if not args.email and not args.password:
        print('Аргументы --email/--password не заданы. База не изменена.')
        print('Пример: tools/rotate_demo_passwords.py --email admin@aeroflot.ru')
        return 0

    email = (args.email or input('Email: ')).strip().lower()
    if not email or '@' not in email:
        raise SystemExit('Некорректный email; база не изменена.')

    password = args.password
    if not password:
        password = ask_password()
    if not password:
        raise SystemExit('Пустой пароль недопустим; база не изменена.')

    if not os.path.isfile(args.db):
        raise SystemExit('База не найдена: ' + args.db)

    conn = sqlite3.connect(args.db)
    try:
        row = conn.execute(
            'SELECT id, role FROM users WHERE email = ? COLLATE NOCASE',
            (email,),
        ).fetchone()
        if not row:
            raise SystemExit('Пользователь не найден: ' + email)

        user_id, role = row
        conn.execute(
            'UPDATE users SET password = ? WHERE id = ?',
            (generate_password_hash(password), user_id),
        )
        conn.commit()
    finally:
        conn.close()

    print('Готово: пароль обновлён для {} (роль: {}).'.format(email, role))
    print('Пароль в вывод не выводится.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
