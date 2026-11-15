"""Сквозная цепочка MCC → ЦПУ → нач. смены → бригадир → техник → MCC."""

from conftest import chain_entry


def first_staff(api, role):
    status, rows = api.get('/api/staff?role=' + role)
    assert status == 200 and rows, role
    return rows[0]['id']


def free_engineer(api):
    status, rows = api.get('/api/engineers')
    assert status == 200
    return next(e for e in rows if (e.get('status') or 'free') == 'free')


def test_full_chain_to_closed(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-CHAIN', parking='105')

    entry = chain_entry(api, rid)
    assert entry['chainStage'] == 'mcc'
    assert entry['status'] == 'open'

    assert api.post(f'/api/chain/{rid}/to_cpu', {'actor': 'Смирнов А.В.'})[0] == 200
    assert chain_entry(api, rid)['chainStage'] == 'cpu'

    supid = first_staff(api, 'shift_supervisor')
    assert api.post(f'/api/chain/{rid}/assign_shift',
                    {'staff_id': supid, 'comment': 'Смена приняла'})[0] == 200
    entry = chain_entry(api, rid)
    assert entry['chainStage'] == 'shift'
    assert entry['shiftSupervisorId'] == supid

    frid = first_staff(api, 'foreman')
    assert api.post(f'/api/chain/{rid}/assign_foreman',
                    {'staff_id': frid, 'comment': 'Бригадир назначен'})[0] == 200
    entry = chain_entry(api, rid)
    assert entry['chainStage'] == 'foreman'
    assert entry['foremanId'] == frid

    eng = free_engineer(api)
    assert api.post(f'/api/chain/{rid}/assign_technician',
                    {'engineer_id': eng['id'], 'comment': 'Техник выехал'})[0] == 200
    entry = chain_entry(api, rid)
    assert entry['chainStage'] == 'tech'
    assert entry['engineerId'] == eng['id']

    status, body = api.post('/api/engineer/verdict', {
        'request_id': rid, 'verdict': 'REPAIR',
        'comment': 'Заменён датчик', 'engineer_id': eng['id'],
    })
    assert status == 200, body
    assert chain_entry(api, rid)['chainStage'] == 'verdict'

    status, body = api.post('/api/mcc/finalize', {
        'request_id': rid, 'action': 'CLOSE',
        'comment': 'Ремонт выполнен', 'dce_name': 'Смирнов А.В.',
    })
    assert status == 200, body

    entry = chain_entry(api, rid)
    assert entry['chainStage'] == 'closed'
    assert entry['status'] == 'repaired'
    assert entry['mccDecision'] == 'CLOSE'
    assert entry['closedAt']

    stages = [h['to'] for h in entry['history']]
    assert stages == ['mcc', 'cpu', 'shift', 'foreman', 'tech', 'verdict', 'closed']


def test_verdict_frees_engineer(api, make_pilot_request, app_module):
    rid = make_pilot_request(aircraft='RA-FREE', parking='106')
    api.post(f'/api/chain/{rid}/to_cpu', {})
    api.post(f'/api/chain/{rid}/assign_shift',
             {'staff_id': first_staff(api, 'shift_supervisor'), 'comment': 'x'})
    api.post(f'/api/chain/{rid}/assign_foreman',
             {'staff_id': first_staff(api, 'foreman'), 'comment': 'x'})
    eng = free_engineer(api)
    api.post(f'/api/chain/{rid}/assign_technician',
             {'engineer_id': eng['id'], 'comment': 'x'})

    conn = app_module.get_db()
    row = conn.execute("SELECT status FROM engineers WHERE id=?", (eng['id'],)).fetchone()
    assert row['status'] == 'busy'
    conn.close()

    api.post('/api/engineer/verdict', {
        'request_id': rid, 'verdict': 'MEL', 'comment': 'Можно отложить',
        'engineer_id': eng['id'],
    })

    conn = app_module.get_db()
    row = conn.execute("SELECT status, task FROM engineers WHERE id=?", (eng['id'],)).fetchone()
    conn.close()
    assert row['status'] == 'free' and not row['task']


def test_stage_guard_rejects_wrong_step(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-GUARD')
    # нельзя назначить бригадира, минуя ЦПУ и начальника смены
    status, body = api.post(f'/api/chain/{rid}/assign_foreman',
                            {'staff_id': first_staff(api, 'foreman'), 'comment': 'x'})
    assert status == 400
    assert 'этапе' in body['error']


def test_assignment_requires_comment(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-CMT')
    api.post(f'/api/chain/{rid}/to_cpu', {})
    status, body = api.post(f'/api/chain/{rid}/assign_shift',
                            {'staff_id': first_staff(api, 'shift_supervisor'),
                             'comment': '   '})
    assert status == 400
    assert 'комментарий' in body['error'].lower()


def test_verdict_requires_valid_value(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-VRD')
    status, body = api.post('/api/engineer/verdict',
                            {'request_id': rid, 'verdict': 'MAYBE', 'comment': 'x'})
    assert status == 400


def test_mel_defer_closes_without_technician(api, make_pilot_request):
    rid = make_pilot_request(aircraft='RA-MEL')
    status, body = api.post(f'/api/chain/{rid}/defer_mel', {
        'mel_category': 'C', 'mel_ref': 'MEL-32-01',
        'comment': 'Допустимо отложить', 'actor': 'Смирнов А.В.',
    })
    assert status == 200, body
    entry = chain_entry(api, rid)
    assert entry['chainStage'] == 'closed'
    assert entry['status'] == 'deferred'
    assert entry['melCategory'] == 'C'


def test_recurring_flag_after_three_defects(api, make_pilot_request):
    for _ in range(3):
        make_pilot_request(aircraft='RA-RECUR', fault='engine_vibration')
    entries = [chain_entry(api, r['id']) for r in api.get('/api/chain')[1]
               if r['aircraft'] == 'RA-RECUR']
    assert entries and all(e['recurring'] for e in entries)
    assert max(e['occurrences'] for e in entries) >= 3
