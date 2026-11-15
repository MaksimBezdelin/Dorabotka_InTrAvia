"""ATLB пилота на сервере: список заявок и подпись CRS."""

from conftest import chain_entry


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
