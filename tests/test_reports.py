"""Отчёты диспетчера хранятся на сервере, а не в браузере."""


def test_create_and_list_report(api):
    status, body = api.post('/api/reports', {
        'text': 'Отчёт по заявке RA-REP',
        'request_id': 'req_rep_1',
        'aircraft': 'RA-REP',
        'engineer_name': 'Иванов И.И.',
    })
    assert status == 200 and body['success']
    rep_id = body['id']

    status, rows = api.get('/api/reports')
    assert status == 200
    assert any(r['id'] == rep_id for r in rows)
    assert len(rows) >= 1


def test_empty_report_rejected(api):
    status, body = api.post('/api/reports', {'text': '   '})
    assert status == 400


def test_deleted_report_hidden_but_listed(api):
    _, body = api.post('/api/reports', {'text': 'Удаляемый отчёт', 'aircraft': 'RA-DEL'})
    rep_id = body['id']

    assert api.client.delete('/api/reports/' + rep_id).status_code == 200

    _, visible = api.get('/api/reports')
    assert not any(r['id'] == rep_id for r in visible)

    _, all_rows = api.get('/api/reports?deleted=1')
    assert any(r['id'] == rep_id for r in all_rows)


def test_reports_require_auth(anon_client):
    assert anon_client.get('/api/reports').status_code == 401
