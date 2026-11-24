# -*- coding: utf-8 -*-
"""Словарь FIM 007-00: классификация MEL/AOG и каталог неисправностей."""


def _classify(client, text, ata=None):
    params = {'text': text}
    if ata:
        params['ata'] = ata
    resp = client.get('/api/faults/classify', query_string=params)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def test_engine_failure_is_aog(client):
    result = _classify(client, 'отказ двигателя')
    assert result['decision'] == 'AOG'
    assert result['mel_category'] == ''
    assert result['needs_review'] is True


def test_sensor_fault_is_mel(client):
    result = _classify(client, 'отказ датчика температуры')
    assert result['decision'] == 'MEL'
    assert result['mel_category'] in ('A', 'B', 'C', 'D')
    assert result['interval_days'] > 0


def test_hydraulic_single_system_is_mel_category_a(client):
    result = _classify(client, 'отказ гидросистемы')
    assert result['ata'] == '29'
    assert result['decision'] == 'MEL'
    assert result['mel_category'] == 'A'


def test_depressurization_and_gear_are_aog(client):
    assert _classify(client, 'разгерметизация кабины')['decision'] == 'AOG'
    assert _classify(client, 'не выпускается шасси')['decision'] == 'AOG'
    assert _classify(client, 'пожар двигателя')['decision'] == 'AOG'


def test_mel_and_aog_catalog_tables_populated(app_module):
    conn = app_module.get_db()
    try:
        mel = conn.execute(
            "SELECT COUNT(*) FROM mel_catalog WHERE source_document='FIM 007-00'").fetchone()[0]
        aog = conn.execute('SELECT COUNT(*) FROM aog_catalog').fetchone()[0]
    finally:
        conn.close()
    assert mel > 3000
    assert aog > 100


def test_catalog_filters_and_counts(client):
    resp = client.get('/api/faults/catalog',
                      query_string={'ata': '24', 'decision': 'AOG', 'limit': 5})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['total'] > 3000
    assert body['counts']['AOG'] > 0 and body['counts']['MEL'] > 0
    assert body['items']
    assert all(item['ata'] == '24' and item['decision'] == 'AOG'
               for item in body['items'])
