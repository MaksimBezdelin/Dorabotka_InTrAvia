"""Критерий 18 — открытый классификатор неисправностей (data/fault_catalog.json)."""

import json

import pytest


@pytest.fixture()
def catalog_path(app_module, tmp_path, monkeypatch):
    path = tmp_path / 'fault_catalog.json'
    monkeypatch.setattr(app_module, 'FAULT_CATALOG_PATH', str(path))
    return path


def test_known_code_and_synonym_return_ata(app_module, catalog_path):
    info = app_module.classify_fault('engine_failure')
    assert info['code'] == 'engine_failure'
    assert info['ata'] == '72'
    assert info['skill'] == 'engine'
    assert info['matched'] is True

    info = app_module.classify_fault('течь гидравлики')
    assert info['code'] == 'hydraulic_leak'
    assert info['ata'] == '29'
    assert info['skill'] == 'hydraulics'
    assert info['matched'] is True


def test_unknown_text_creates_custom_entry_and_is_reused(app_module, catalog_path):
    raw = 'Странный скрип в кабине №7'
    info = app_module.classify_fault(raw)
    assert info['matched'] is False
    assert info['code'].startswith('custom_')
    assert info['ata'] == '' and info['skill'] == 'general'
    assert info['name'] == raw

    assert catalog_path.exists(), 'custom-запись должна быть сохранена в файл'
    entries = json.loads(catalog_path.read_text(encoding='utf-8'))
    saved = [e for e in entries if e['code'] == info['code']]
    assert len(saved) == 1
    assert saved[0]['custom'] is True
    assert saved[0]['name'] == raw

    again = app_module.classify_fault(raw)
    assert again['code'] == info['code']
    assert again['matched'] is True
    entries = json.loads(catalog_path.read_text(encoding='utf-8'))
    assert len([e for e in entries if e['name'] == raw]) == 1


def test_write_error_does_not_raise(app_module, tmp_path, monkeypatch):
    target = tmp_path / 'catalog_is_dir'
    target.mkdir()
    monkeypatch.setattr(app_module, 'FAULT_CATALOG_PATH', str(target))
    info = app_module.classify_fault('Неизвестный дефект без каталога')
    assert info['code'].startswith('custom_')
    assert info['matched'] is False


def test_dispatch_uses_classifier_and_returns_fault(app_module, api, catalog_path):
    status, body = api.post('/api/dispatch', {
        'aircraft': 'RA-FAULT1', 'parking_id': '101',
        'fault_type': 'engine_failure', 'description': 'Дым из двигателя',
        'severity': 'medium',
    })
    assert status == 200, body
    assert body['fault']['code'] == 'engine_failure'
    assert body['fault']['ata'] == '72'
    assert body['fault']['matched'] is True

    rid = body['request_id']
    status, rows = api.get('/api/requests')
    assert status == 200
    row = next(r for r in rows if r['id'] == rid)
    assert row['fault_type'] == 'engine_failure'
    assert row['description'] == 'Дым из двигателя'
    assert row['mcc_ata'] == '72'
    assert 'ATA 72' in (row['mcc_comment'] or '')
    assert any('ATA 72' in (e.get('note') or '')
               for e in row['routing_history'] if isinstance(e, dict))

    status, body = api.post('/api/dispatch', {
        'aircraft': 'RA-FAULT2', 'parking_id': '101',
        'fault_type': 'Скрежет в хвостовой части 999',
        'severity': 'medium',
    })
    assert status == 200, body
    assert body['fault']['matched'] is False
    assert body['fault']['code'].startswith('custom_')
    assert catalog_path.exists()
