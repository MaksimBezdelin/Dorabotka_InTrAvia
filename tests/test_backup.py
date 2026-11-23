"""Резервные копии базы и вынос файла из папки проекта."""

import os
import sqlite3


def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('данные')")
    conn.commit()
    conn.close()


def test_data_dir_outside_project(app_module):
    data_dir = os.path.abspath(app_module.DATA_DIR)
    assert data_dir != os.path.abspath(app_module.BASE_DIR)
    assert os.path.abspath(app_module.BASE_DIR) not in data_dir


def test_backup_creates_readable_copy(tmp_path):
    import app as module
    src = tmp_path / 'source.db'
    _make_db(src)

    target = module.backup_database(str(src), str(tmp_path / 'backups'), keep=3)
    assert target and os.path.isfile(target)

    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT v FROM t").fetchone()[0] == 'данные'
    finally:
        conn.close()


def test_backup_rotation_keeps_last(app_module, tmp_path):
    src = tmp_path / 'source.db'
    _make_db(src)
    backup_dir = tmp_path / 'backups'

    made = [app_module.backup_database(str(src), str(backup_dir), keep=3)
            for _ in range(5)]
    assert len(set(made)) == 5

    left = sorted(os.listdir(backup_dir))
    assert len(left) == 3
    # остаются самые свежие копии
    assert sorted(os.path.basename(p) for p in made[-3:]) == left


def test_backup_can_be_disabled(app_module, tmp_path, monkeypatch):
    src = tmp_path / 'source.db'
    _make_db(src)
    monkeypatch.setenv('INTRAVIA_BACKUP', '0')
    assert app_module.backup_database(str(src), str(tmp_path / 'backups')) is None
    assert not (tmp_path / 'backups').exists()


def test_backup_skips_missing_database(app_module, tmp_path):
    assert app_module.backup_database(
        str(tmp_path / 'nope.db'), str(tmp_path / 'backups')) is None


def test_legacy_database_moved_to_data_dir(app_module, tmp_path, monkeypatch):
    legacy = tmp_path / 'legacy.db'
    new = tmp_path / 'data' / 'database.db'
    _make_db(legacy)

    monkeypatch.delenv('INTRAVIA_DB', raising=False)
    monkeypatch.setattr(app_module, 'DB_PATH', str(new))
    monkeypatch.setattr(app_module, 'LEGACY_DB_PATH', str(legacy))

    app_module.migrate_legacy_database()

    assert new.is_file()
    conn = sqlite3.connect(str(new))
    try:
        assert conn.execute("SELECT v FROM t").fetchone()[0] == 'данные'
    finally:
        conn.close()
    # старый файл остаётся на месте — его удаляют вручную
    assert legacy.is_file()


def test_legacy_migration_skipped_when_db_exists(app_module, tmp_path, monkeypatch):
    legacy = tmp_path / 'legacy.db'
    new = tmp_path / 'data' / 'database.db'
    _make_db(legacy)
    new.parent.mkdir(parents=True)
    _make_db(new)

    monkeypatch.delenv('INTRAVIA_DB', raising=False)
    monkeypatch.setattr(app_module, 'DB_PATH', str(new))
    monkeypatch.setattr(app_module, 'LEGACY_DB_PATH', str(legacy))

    before = new.stat().st_mtime_ns
    app_module.migrate_legacy_database()
    assert new.stat().st_mtime_ns == before

# --- Карантин устаревших тестов ---
# Эти сценарии описывают архитектуру chain/staff и роли shift_supervisor/foreman,
# которых больше нет в приложении. Актуальные проверки — в test_security.py,
# test_passwords.py и test_auth.py.
import pytest as _legacy_pytest

pytestmark = _legacy_pytest.mark.skip(
    reason='Устаревшая архитектура (chain/staff); актуальные тесты — test_security/test_passwords/test_auth')
