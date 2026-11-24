"""Управление учётными записями InTrAvia.

    python manage.py list                 — кто заведён
    python manage.py passwd <email>       — сменить пароль (спросит новый)
    python manage.py create-admin <email> — первый администратор
    python manage.py check-defaults       — кто ещё на демо-пароле

Для боевой базы запускайте с INTRAVIA_DEMO_SEED=0: демо-учётки не создаются,
первого администратора заводит create-admin.
"""

import getpass
import sys
import time

import app as intravia

DEFAULT_BY_ROLE = {
    'admin': 'admin123',
    'dispatcher': 'disp123',
    'mcc': 'mcc123',
    'engineer': 'eng123',
    'pilot': 'pilot123',
}

STAFF_DEFAULT_BY_ROLE = {
    'shift_supervisor': 'shift123',
    'foreman': 'foreman123',
    'cpu': 'cpu123',
}


def open_db():
    """База без демо-сидов: только схема и миграции колонок."""
    intravia.migrate_legacy_database()
    conn = intravia.get_db()
    intravia.ensure_schema(conn)
    return conn


def set_password(conn, email, password):
    """Меняет пароль в users или staff. Возвращает True, если запись найдена."""
    email = (email or '').strip().lower()
    password = (password or '').strip()
    if not email or not password:
        return False
    pwd_hash = intravia.hash_password(password)
    cur = conn.cursor()
    cur.execute("UPDATE users SET password=? WHERE LOWER(email)=?",
                (pwd_hash, email))
    changed = cur.rowcount
    cur.execute("UPDATE staff SET password=? WHERE LOWER(email)=?",
                (pwd_hash, email))
    changed += cur.rowcount
    conn.commit()
    return changed > 0


def create_admin(conn, email, password, name='Администратор'):
    """Создаёт администратора. Возвращает id или None, если email занят."""
    email = (email or '').strip().lower()
    password = (password or '').strip()
    name = (name or '').strip() or 'Администратор'
    if not email or not password:
        return None
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE LOWER(email)=?", (email,))
    if cur.fetchone():
        return None
    uid = 'admin_' + str(int(time.time() * 1000))
    cur.execute("""INSERT INTO users (id,email,password,name,role,avatar,status)
                   VALUES (?,?,?,?,'admin','👨‍💼','online')""",
                (uid, email, intravia.hash_password(password), name))
    conn.commit()
    return uid


def default_password_accounts(conn):
    """Записи, пароль которых всё ещё совпадает с демо-значением роли."""
    found = []
    cur = conn.cursor()
    cur.execute("SELECT id,email,name,role,password FROM users ORDER BY role,email")
    for row in cur.fetchall():
        default = DEFAULT_BY_ROLE.get(row['role'])
        if default and intravia.verify_password(row['password'], default)[0]:
            found.append({'id': row['id'], 'email': row['email'],
                          'name': row['name'], 'role': row['role'],
                          'where': 'users'})
    cur.execute("SELECT id,email,name,role,password FROM staff ORDER BY role,email")
    for row in cur.fetchall():
        default = STAFF_DEFAULT_BY_ROLE.get(row['role'])
        if default and intravia.verify_password(row['password'], default)[0]:
            found.append({'id': row['id'], 'email': row['email'],
                          'name': row['name'], 'role': row['role'],
                          'where': 'staff'})
    return found


def _ask_password():
    first = getpass.getpass('Новый пароль: ')
    if not first.strip():
        raise SystemExit('Пустой пароль не подходит')
    if first != getpass.getpass('Повторите пароль: '):
        raise SystemExit('Пароли не совпадают')
    return first


def _cmd_list(conn, args):
    cur = conn.cursor()
    cur.execute("SELECT role,email,name FROM users ORDER BY role,email")
    rows = [tuple(r) for r in cur.fetchall()]
    cur.execute("SELECT role,email,name FROM staff ORDER BY role,email")
    rows += [tuple(r) for r in cur.fetchall()]
    for role, email, name in rows:
        print(f'{role:<18} {email:<32} {name}')
    return 0


def _cmd_passwd(conn, args):
    if len(args) < 1:
        raise SystemExit('Укажите email: python manage.py passwd <email>')
    if not set_password(conn, args[0], _ask_password()):
        print(f'Учётная запись не найдена: {args[0]}')
        return 1
    print('Пароль изменён.')
    return 0


def _cmd_create_admin(conn, args):
    if len(args) < 1:
        raise SystemExit('Укажите email: python manage.py create-admin <email>')
    uid = create_admin(conn, args[0], _ask_password())
    if not uid:
        print('Не удалось создать: email уже занят или пустой пароль.')
        return 1
    print(f'Администратор создан: {args[0]} (id {uid})')
    return 0


def _cmd_check_defaults(conn, args):
    rows = default_password_accounts(conn)
    if not rows:
        print('Демо-паролей не найдено.')
        return 0
    print('ВНИМАНИЕ: эти учётные записи всё ещё на демо-пароле:')
    for row in rows:
        print(f"  {row['role']:<18} {row['email']:<32} {row['name']}")
    print('\nСменить: python manage.py passwd <email>')
    return 1


COMMANDS = {
    'list': _cmd_list,
    'passwd': _cmd_passwd,
    'create-admin': _cmd_create_admin,
    'check-defaults': _cmd_check_defaults,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    command, args = argv[0], argv[1:]
    handler = COMMANDS.get(command)
    if not handler:
        print(f'Неизвестная команда: {command}\n')
        print(__doc__)
        return 2
    conn = open_db()
    try:
        return handler(conn, args)
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
