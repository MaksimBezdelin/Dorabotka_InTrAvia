"""ATLB пилота на сервере: список заявок и подпись CRS."""

from conftest import chain_entry


def _move_to_verdict(api, rid, verdict='AOG'):
    api.post(f'/api/chain/{rid}/to_cpu', {})
    staff = api.get('/api/staff')[1]
    sup = next(r['id'] for r in staff if r['role'] == 'shift_supervisor')
    foreman = next(r['id'] for r in staff if r['role'] == 'foreman')
    api.post(f'/api/chain/{rid}/assign_shift', {'staff_id': sup, 'comment': 'x'})
    api.post(f'/api/chain/{rid}/assign_foreman', {'staff_id': foreman, 'comment': 'x'})
    engineers = api.get('/api/engineers')[1]
    eng = next(e for e in engineers if (e.get('status') or 'free') == 'free')
    api.post(f'/api/chain/{rid}/assign_technician',
             {'engineer_id': eng['id'], 'comment': 'x'})
    status, body = api.post('/api/engineer/verdict', {
        'request_id': rid, 'verdict': verdict, 'comment': 'осмотр',
        'engineer_id': eng['id'],
    })
    assert status == 200, body


def _atlb_entry(api, rid):
    rows = api.get('/api/pilot/atlb?pilot=Тестовый КВС')[1]
    return next(r for r in rows if r['id'] == rid)


def test_atlb_shows_engineer_aog_verdict(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-AOG-V')
    _move_to_verdict(api, rid, 'AOG')
    entry = _atlb_entry(api, rid)
    assert entry['status'] == 'aog_verdict'
    assert entry['verdict'] == 'AOG'


def test_atlb_shows_confirmed_aog(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-AOG-C')
    _move_to_verdict(api, rid, 'AOG')
    status, body = api.post('/api/mcc/finalize', {
        'request_id': rid, 'action': 'AOG_RECOVERY',
        'comment': 'Лететь нельзя', 'etr': '6 часов',
    })
    assert status == 200, body
    entry = _atlb_entry(api, rid)
    assert entry['status'] == 'aog'


def test_aog_recovered_and_closed_is_not_stuck_as_aog(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-AOG-FIX')
    _move_to_verdict(api, rid, 'AOG')
    status, body = api.post('/api/mcc/finalize', {
        'request_id': rid, 'action': 'AOG_RECOVERY',
        'comment': 'AOG', 'etr': '6 часов',
    })
    assert status == 200, body
    assert _atlb_entry(api, rid)['status'] == 'aog'

    status, body = api.post('/api/mcc/finalize', {
        'request_id': rid, 'action': 'CLOSE', 'comment': 'Восстановлено',
    })
    assert status == 200, body
    assert _atlb_entry(api, rid)['status'] == 'repaired'


def test_atlb_shows_repair_and_mel_verdicts(api, make_pilot_request):
    rep = make_pilot_request(aircraft='RA-REP-V')
    _move_to_verdict(api, rep, 'REPAIR')
    assert _atlb_entry(api, rep)['status'] == 'repair_verdict'

    mel = make_pilot_request(aircraft='RA-MEL-V')
    _move_to_verdict(api, mel, 'MEL')
    assert _atlb_entry(api, mel)['status'] == 'mel_verdict'


def test_pilot_atlb_lists_only_own_requests(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-ATLB')
    status, rows = api.get('/api/pilot/atlb?pilot=Тестовый КВС')
    assert status == 200
    assert any(r['id'] == rid for r in rows)
    assert all(r['source'] == 'pilot' for r in rows)


def test_pilot_atlb_status_follows_chain(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-ATLB2')
    entry = next(r for r in api.get('/api/pilot/atlb?pilot=Тестовый КВС')[1]
                 if r['id'] == rid)
    assert entry['status'] == 'open'
    assert entry['crs']['signed'] is False


def test_crs_signing_requires_closed_request(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-CRS-EARLY')
    status, body = api.post('/api/pilot/atlb/sign_crs',
                            {'request_id': rid, 'pilot_name': 'Тестовый КВС',
                             'signature': 'data:image/png;base64,AAAA'})
    assert status == 400
    assert 'МЦЦ' in body['error']


def test_crs_signing_on_closed_request(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-CRS-OK')
    api.post(f'/api/chain/{rid}/defer_mel', {
        'mel_category': 'D', 'mel_ref': 'MEL-71-02',
        'comment': 'Можно отложить', 'actor': 'Смирнов А.В.',
    })
    status, body = api.post('/api/pilot/atlb/sign_crs', {
        'request_id': rid, 'pilot_name': 'Тестовый КВС',
        'signature': 'data:image/png;base64,AAAA',
    })
    assert status == 200, body

    entry = next(r for r in api.get('/api/pilot/atlb?pilot=Тестовый КВС')[1]
                 if r['id'] == rid)
    assert entry['status'] == 'released'
    assert entry['crs']['signed'] is True
    assert entry['crs']['signedBy'] == 'Тестовый КВС'

    # повторная подпись запрещена
    status, body = api.post('/api/pilot/atlb/sign_crs', {
        'request_id': rid, 'pilot_name': 'Тестовый КВС', 'signature': 'x',
    })
    assert status == 400
    assert 'уже подписан' in body['error']
