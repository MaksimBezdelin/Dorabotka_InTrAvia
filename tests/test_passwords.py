"""Пароли хранятся только хэшами; старые открытые пароли мигрируют при входе."""


def _scalar(app_module, sql, params=()):
    conn = app_module.get_db()
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _restore(app_module, table, user_id, value):
    conn = app_module.get_db()
    conn.execute(f"UPDATE {table} SET password=? WHERE id=?", (value, user_id))
    conn.commit()
    conn.close()


def test_seeded_passwords_are_hashed(app_module):
    conn = app_module.get_db()
    users = conn.execute("SELECT id, password FROM users").fetchall()
    staff = conn.execute("SELECT id, password FROM staff").fetchall()
    conn.close()

    assert users and staff
    for row in list(users) + list(staff):
        assert app_module.is_password_hash(row['password']), row['id']
    # демо-пароли нигде не лежат открытым текстом
    for plain in ('admin123', 'disp123', 'mcc123', 'eng123',
                  'pilot123', 'shift123', 'foreman123', 'cpu123'):
        assert _scalar(app_module, "SELECT COUNT(*) FROM users WHERE password=?",
                       (plain,)) == 0
        assert _scalar(app_module, "SELECT COUNT(*) FROM staff WHERE password=?",
                       (plain,)) == 0


def test_legacy_user_password_upgraded_on_login(app_module):
    old_hash = _scalar(app_module, "SELECT password FROM users WHERE id='disp_2'")
    _restore(app_module, 'users', 'disp_2', 'legacy123')
    try:
        c = app_module.app.test_client()
        resp = c.post('/api/auth/login',
                      json={'email': 'dispatcher2@aeroflot.ru', 'password': 'legacy123'})
        assert resp.status_code == 200, resp.get_json()

        stored = _scalar(app_module, "SELECT password FROM users WHERE id='disp_2'")
        assert stored != 'legacy123'
        assert app_module.is_password_hash(stored)
        assert app_module.verify_password(stored, 'legacy123') == (True, False)
    finally:
        _restore(app_module, 'users', 'disp_2', old_hash)


def test_legacy_staff_password_upgraded_on_login(app_module):
    old_hash = _scalar(app_module, "SELECT password FROM staff WHERE id='fr_3'")
    _restore(app_module, 'staff', 'fr_3', 'legacy456')
    try:
        c = app_module.app.test_client()
        resp = c.post('/api/auth/login',
                      json={'email': 'foreman3@aeroflot.ru', 'password': 'legacy456'})
        assert resp.status_code == 200, resp.get_json()

        stored = _scalar(app_module, "SELECT password FROM staff WHERE id='fr_3'")
        assert app_module.is_password_hash(stored)
        assert app_module.verify_password(stored, 'legacy456') == (True, False)
    finally:
        _restore(app_module, 'staff', 'fr_3', old_hash)


def test_wrong_password_still_rejected_against_hash(api):
    status, body = api.post('/api/auth/login',
                            {'email': 'dispatcher@aeroflot.ru', 'password': 'disp1234'})
    assert status == 401
    assert 'error' in body


def test_create_user_stores_hash_and_login_works(app_module, api):
    status, body = api.post('/api/users', {
        'name': 'Тестовый Оператор', 'email': 'newop@aeroflot.ru',
        'role': 'mcc', 'password': 'Secret#42',
    })
    assert status == 200, body
    uid = body['id']

    stored = _scalar(app_module, "SELECT password FROM users WHERE id=?", (uid,))
    assert stored != 'Secret#42'
    assert app_module.is_password_hash(stored)
    assert app_module.verify_password(stored, 'Secret#42') == (True, False)

    c = app_module.app.test_client()
    assert c.post('/api/auth/login', json={
        'email': 'newop@aeroflot.ru', 'password': 'Secret#42'}).status_code == 200


def test_update_user_password_stores_hash(app_module, api):
    status, body = api.post('/api/users', {
        'name': 'Сменный Инженер', 'email': 'shifteng@aeroflot.ru',
        'role': 'mcc', 'password': 'Initial1',
    })
    assert status == 200, body
    uid = body['id']

    resp = api.client.put(f'/api/users/{uid}', json={
        'name': 'Сменный Инженер', 'password': 'Changed2',
        'phone': '', 'shift': 'day', 'status': 'offline',
    })
    assert resp.status_code == 200

    stored = _scalar(app_module, "SELECT password FROM users WHERE id=?", (uid,))
    assert app_module.is_password_hash(stored)
    assert app_module.verify_password(stored, 'Changed2') == (True, False)
    assert app_module.verify_password(stored, 'Initial1') == (False, False)


def test_staff_directory_hides_passwords(api):
    status, rows = api.get('/api/staff')
    assert status == 200
    assert rows
    assert all('password' not in row for row in rows)
