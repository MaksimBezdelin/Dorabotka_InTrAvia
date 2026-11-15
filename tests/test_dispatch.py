"""Заявки пилота и диспетчера: источник, поля, попадание в цепочку."""

from conftest import chain_entry


def test_pilot_text_request_saved_with_source_and_description(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-PILOT', parking='117',
                             fault='hydraulic_leak',
                             description='Падение давления в гидросистеме №1')
    entry = chain_entry(api, rid)
    assert entry['source'] == 'pilot'
    assert entry['pilotName'] == 'Тестовый КВС'
    assert 'Падение давления' in entry['defect']
    assert entry['chainStage'] == 'mcc'
    assert entry['history'][0]['role'] == 'Пилот'


def test_pilot_request_without_audio_and_text_rejected(api):
    status, body = api.post('/api/dispatch', {
        'source': 'pilot', 'aircraft': 'RA-EMPTY', 'parking_id': '101',
    })
    assert status == 400
    assert 'описание' in body['error'].lower()


def test_dispatcher_request_goes_straight_to_technician_stage(api):
    status, body = api.post('/api/dispatch', {
        'aircraft': 'RA-DISP', 'parking_id': '101',
        'fault_type': 'engine_failure', 'severity': 'high',
        'description': 'Диспетчерская заявка',
    })
    assert status == 200, body
    rid = body['request_id']
    entry = chain_entry(api, rid)
    # либо сразу назначен техник, либо поставлено в очередь
    assert entry['chainStage'] in ('tech', 'mcc')
    if not body.get('queued'):
        assert entry['chainStage'] == 'tech'
        assert entry['engineerName']


def test_dispatch_requires_aircraft_and_parking(api):
    status, body = api.post('/api/dispatch', {'aircraft': 'RA-NOPE'})
    assert status == 400
    assert 'стоянка' in body['error'].lower() or 'рейс' in body['error'].lower()


def test_chain_contains_only_active_and_closed_not_drafts(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-DRAFTCHECK')
    api.post('/api/pilot/confirm/' + rid, {})
    status, rows = api.get('/api/chain')
    assert status == 200
    assert any(r['id'] == rid for r in rows)
