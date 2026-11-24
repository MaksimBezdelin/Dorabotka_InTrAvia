"""SSE-шина и append-only журнал событий (вынесено из app.py)."""
import json
import os
import queue
import threading
from datetime import datetime, timezone

from intravia.config import BASE_DIR

_SSE_LOCK = threading.Lock()
_SSE_CLIENTS = set()
_SSE_QUEUE_SIZE = 200

def sse_publish(event, data):
    """Публикует событие всем подключённым SSE-клиентам."""
    payload = ('event: ' + str(event) + '\n' +
               'data: ' + json.dumps(data, ensure_ascii=False, default=str) + '\n\n')
    with _SSE_LOCK:
        dead = []
        for client_queue in _SSE_CLIENTS:
            try:
                client_queue.put_nowait(payload)
            except queue.Full:
                dead.append(client_queue)
        for client_queue in dead:
            _SSE_CLIENTS.discard(client_queue)

def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

EVENTS_PATH = (os.environ.get('INTRAVIA_EVENTS', '').strip()
               or os.path.join(BASE_DIR, 'data', 'events.jsonl'))
_EVENTS_LOCK = threading.Lock()


def append_event(event_type, **fields):
    """Дописывает событие в data/events.jsonl. Журнал только пополняется."""
    record = {'at': _utc_now_iso(), 'type': str(event_type)}
    record.update(fields)
    try:
        os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _EVENTS_LOCK:
            with open(EVENTS_PATH, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
    except OSError as exc:
        print(f'[EVENTS] {exc}')
    return record


def read_events(since=None, limit=1000):
    """Читает append-only журнал. since — ISO-время, возвращаются события после него."""
    if not os.path.isfile(EVENTS_PATH):
        return []
    events = []
    try:
        with _EVENTS_LOCK:
            with open(EVENTS_PATH, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if since and str(event.get('at') or '') <= str(since):
                        continue
                    events.append(event)
    except OSError as exc:
        print(f'[EVENTS] read: {exc}')
        return []
    if limit and limit > 0:
        events = events[-limit:]
    return events


def _append_request_created(rid, source, aircraft, parking_id, flight_number, status, stage):
    return append_event('request_created', request_id=rid, source=source,
                        aircraft=aircraft, parking_id=parking_id,
                        flight_number=flight_number, status=status,
                        routing_stage=stage)
