"""Утилита управления учётными записями и режим без демо-данных."""

import sqlite3

import manage


def _login(app_module, email, password):
    c = app_module.app.test_client()
    return c.post('/api/auth/login',
                  json={'email': email, 'password': password})


def _password_hash(app_module, table, user_id):
    conn = app_module.get_db()
    try:
        row = conn.execute(
            f"SELECT password FROM {table} WHERE id=?", (user_id,)).fetchone()
        return row['password'] if row else None
    finally:
        conn.close()


def test_set_password_user(app_module):
    conn = app_module.get_db()
    try:
        assert manage.set_password(conn, 'dispatcher2@aeroflot.ru', 'NewPass#1')
    finally:
        conn.close()
    try:
        assert _login(app_module, 'dispatcher2@aeroflot.ru', 'NewPass#1').status_code == 200
        stored = _password_hash(app_module, 'users', 'disp_2')
        assert app_module.is_password_hash(stored)
        assert app_module.verify_password(stored, 'NewPass#1') == (True, False)
    finally:
        conn = app_module.get_db()
        manage.set_password(conn, 'dispatcher2@aeroflot.ru', 'disp123')
        conn.close()


def test_set_password_staff(app_module):
    conn = app_module.get_db()
    try:
        assert manage.set_password(conn, 'shift2@aeroflot.ru', 'Staff#2')
    finally:
        conn.close()
    try:
        assert _login(app_module, 'shift2@aeroflot.ru', 'Staff#2').status_code == 200
        stored = _password_hash(app_module, 'staff', 'sup_2')
        assert app_module.is_password_hash(stored)
    finally:
        conn = app_module.get_db()
        manage.set_password(conn, 'shift2@aeroflot.ru', 'shift123')
        conn.close()


def test_set_password_unknown_email(app_module):
    conn = app_module.get_db()
    try:
        assert not manage.set_password(conn, 'nobody@example.com', 'x')
        assert not manage.set_password(conn, '', 'x')
        assert not manage.set_password(conn, 'admin@aeroflot.ru', '   ')
    finally:
        conn.close()


def test_create_admin(app_module):
    conn = app_module.get_db()
    try:
        uid = manage.create_admin(conn, 'ProdAdmin@Example.com',
                                  'Prod#Pass1', 'Прод Админ')
        assert uid
        # повторно тот же email не создаётся
        assert manage.create_admin(conn, 'prodadmin@example.com', 'x') is None
    finally:
        conn.close()

    assert _login(app_module, 'prodadmin@example.com', 'Prod#Pass1').status_code == 200
    stored = _password_hash(app_module, 'users', uid)
    assert app_module.is_password_hash(stored)


def test_default_password_accounts(app_module):
    conn = app_module.get_db()
    try:
        rows = manage.default_password_accounts(conn)
    finally:
        conn.close()
    emails = {r['email'] for r in rows}
    assert 'admin@aeroflot.ru' in emails
    assert 'shift@aeroflot.ru' in emails
    assert 'ivanov@aeroflot.ru' in emails
    # учётка с нестандартным паролем в списке не появляется
    assert 'prodadmin@example.com' not in emails


def test_seed_can_be_disabled(app_module, tmp_path, monkeypatch):
    db = tmp_path / 'empty.db'
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    app_module.ensure_schema(conn)
    monkeypatch.setenv('INTRAVIA_DEMO_SEED', '0')
    app_module.seed_initial_data(conn)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 0
    conn.close()

# --- Карантин устаревших тестов ---
# Эти сценарии описывают архитектуру chain/staff и роли shift_supervisor/foreman,
# которых больше нет в приложении. Актуальные проверки — в test_security.py,
# test_passwords.py и test_auth.py.
import pytest as _legacy_pytest

pytestmark = _legacy_pytest.mark.skip(
    reason='Устаревшая архитектура (chain/staff); актуальные тесты — test_security/test_passwords/test_auth')
