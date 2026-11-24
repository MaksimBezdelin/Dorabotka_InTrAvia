"""Пароли, лимит входов и серверная авторизация (вынесено из app.py)."""
import re
import secrets
import threading
import time

from flask import jsonify, redirect, request, session
from werkzeug.security import generate_password_hash, check_password_hash

from intravia.config import AUTH_REQUIRED
from intravia.events import append_event

_LOGIN_ATTEMPTS = {}
_LOGIN_LOCK = threading.Lock()
_LOGIN_WINDOW_SEC = 300
_LOGIN_MAX_FAILS = 10

def hash_password(value):
    return generate_password_hash(value)


def is_password_hash(value):
    return isinstance(value, str) and (value.startswith('pbkdf2:') or value.startswith('scrypt:'))


def verify_password(stored, provided):
    """Возвращает (пароль верен, нужна ли миграция из открытого текста)."""
    if not stored or provided is None:
        return False, False
    if is_password_hash(stored):
        try:
            return check_password_hash(stored, provided), False
        except ValueError:
            return False, False
    return secrets.compare_digest(str(stored), str(provided)), True


def _login_key(email):
    return f'{request.remote_addr}|{(email or "").lower()}'


def _login_blocked(key):
    now = time.time()
    with _LOGIN_LOCK:
        fails = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < _LOGIN_WINDOW_SEC]
        _LOGIN_ATTEMPTS[key] = fails
        return len(fails) >= _LOGIN_MAX_FAILS


def _login_record_fail(key):
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.setdefault(key, []).append(time.time())


def _login_clear(key):
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.pop(key, None)



PUBLIC_API_PATHS = {
    '/api/auth/login',
    '/api/auth/logout',
    # Контракт трекинга для полевых устройств и мобильных WebView.
    '/api/v1/track',
    '/api/v1/stream',
    '/api/v1/agents',
    '/api/v1/snapshot',
    '/api/auth/session',
    # Публичная география и расчёт маршрута нужны мобильным приложениям
    # для отрисовки карты до входа и для имитатора организаторов.
    '/api/location',
    '/api/parking',
    '/api/route',
}

# Публичные только для чтения: мобильное приложение узнаёт текущий аэропорт
# до входа, но POST остаётся закрытым матрицей ролей.
PUBLIC_API_GET_PATHS = {
    '/api/system/current-airport',
}
PUBLIC_PAGES = {'/login.html', '/m/engineer', '/m/agents'}

DASHBOARD_ROLES = {
    '/mcc/dashboard.html': {'mcc'},
    # ЦПУ объединено с MCC: очередь ЦПУ ведёт та же роль.
    '/cpu/dashboard.html': {'mcc'},
    '/dispatcher/dashboard.html': {'dispatcher'},
    '/pilot/dashboard.html': {'pilot'},
    '/engineer/dashboard.html': {'engineer'},
    '/admin/dashboard.html': {'admin'},
}

ROLE_HOME = {
    'admin': '/admin/dashboard.html',
    'mcc': '/mcc/dashboard.html',
    'dispatcher': '/dispatcher/dashboard.html',
    'pilot': '/pilot/dashboard.html',
    'engineer': '/engineer/dashboard.html',
}

# Изменять эти группы API может только соответствующая роль (admin — всегда).
WRITE_ROLE_PREFIXES = (
    ('/api/mcc/', {'mcc'}),
    ('/api/cpu/', {'mcc'}),
    ('/api/dispatcher/', {'dispatcher'}),
    ('/api/pilot/', {'pilot'}),
    ('/api/engineer/', {'engineer'}),
    ('/api/users', {'admin'}),
    ('/api/v1/import/', {'admin'}),
    ('/api/clear_all_requests', {'admin'}),
    ('/api/clear_all_pilot_requests', {'admin'}),
)

# Точечная матрица прав для изменяющих запросов: (regexp, роли[, режим]).
# Роли задаются либо множеством, либо словарём {метод: множество ролей}.
# Режим 'engineer_self' дополнительно требует, чтобы id в пути совпадал
# с id инженера из сессии. Проверяется только при AUTH_REQUIRED=1; admin
# обходит матрицу (как и WRITE_ROLE_PREFIXES).
WRITE_ENDPOINT_ROLES = (
    # Инженер меняет только свой шаг; диспетчер и MCC — любой.
    (r'/api/engineers/(?P<engineer_id>[^/]+)/step',
     {'engineer', 'dispatcher', 'mcc'}, 'engineer_self'),
    # GPS-точка мобильного устройства: инженер — только своя, диспетчер/MCC — любая.
    (r'/api/engineers/(?P<engineer_id>[^/]+)/(?:location|gps|coords)',
     {'engineer', 'dispatcher', 'mcc'}, 'engineer_self'),
    (r'/api/engineer/(?P<engineer_id>[^/]+)/location',
     {'engineer', 'dispatcher', 'mcc'}, 'engineer_self'),
    # Справочник инженеров редактирует администратор.
    (r'/api/engineers', {'admin'}),
    (r'/api/engineers/(?P<engineer_id>[^/]+)', {'admin'}),
    # Транспорт — зона диспетчера.
    (r'/api/transport(?:/.*)?', {'dispatcher'}),
    # Справочник пилотов — зона администратора.
    (r'/api/pilots(?:/.*)?', {'admin'}),
    # Заявка: редактирование — пилот/диспетчер/MCC, удаление — диспетчер.
    (r'/api/requests/(?P<rid>[^/]+)',
     {'PUT': {'pilot', 'dispatcher', 'mcc'}, 'DELETE': {'dispatcher'}}),
    (r'/api/requests/(?P<rid>[^/]+)/(?:accept|reject)', {'engineer'}),
    (r'/api/requests/(?P<rid>[^/]+)/cancel',
     {'engineer', 'dispatcher', 'mcc', 'pilot'}),
    (r'/api/requests/(?P<rid>[^/]+)/pilot-signature', {'pilot'}),
    # Статусная модель и ожидание запчастей.
    (r'/api/requests/(?P<rid>[^/]+)/start', {'engineer', 'dispatcher', 'mcc'}),
    (r'/api/requests/(?P<rid>[^/]+)/tool_status', {'engineer'}),
    (r'/api/requests/(?P<rid>[^/]+)/unassign', {'dispatcher', 'mcc'}),
    (r'/api/requests/(?P<rid>[^/]+)/awaiting_parts', {'dispatcher', 'mcc'}),
    (r'/api/requests/(?P<rid>[^/]+)/parts_received', {'dispatcher', 'mcc'}),
    # Демонстрационные настройки заявки (норматив, спецтехника, пауза).
    (r'/api/requests/(?P<rid>[^/]+)/test_options', {'dispatcher'}),
    # Расшифровка голосовой заявки: пилот — своя, диспетчер/MCC — любая.
    (r'/api/requests/(?P<rid>[^/]+)/transcribe',
     {'pilot', 'dispatcher', 'mcc'}),
    # Служебные операции по заявке/рейсу — зона диспетчера и MCC.
    (r'/api/requests/(?P<rid>[^/]+)/(?:approve_delay|cancel_flight)',
     {'dispatcher', 'mcc'}),
    (r'/api/complete/[^/]+', {'dispatcher', 'mcc'}),
    (r'/api/system/current-airport', {'dispatcher', 'mcc'}),
    (r'/api/aircraft/taxi-reservations', {'dispatcher', 'mcc'}),
    # Открытый классификатор: добавление нового типа без правки кода.
    (r'/api/faults/catalog', {'dispatcher'}),
)


def _endpoint_roles_for(rule, method):
    """Возвращает набор ролей для метода из правила WRITE_ENDPOINT_ROLES."""
    roles = rule[1]
    if isinstance(roles, dict):
        return roles.get(method, set())
    return roles


def _enforce_endpoint_roles(path, method, role):
    """Проверяет точечную матрицу. Возвращает None или (jsonify, status)."""
    for rule in WRITE_ENDPOINT_ROLES:
        match = re.fullmatch(rule[0], path)
        if not match:
            continue
        allowed = _endpoint_roles_for(rule, method)
        if role not in allowed:
            append_event('role_denied', path=path, method=method,
                         role=role, reason='endpoint_forbidden')
            return jsonify({'error': 'Недостаточно прав'}), 403
        if len(rule) > 2 and rule[2] == 'engineer_self' and role == 'engineer':
            target_id = match.groupdict().get('engineer_id')
            own_id = str(session.get('user_id') or '')
            if target_id is not None and str(target_id) != own_id:
                append_event('role_denied', path=path, method=method,
                             role=role, reason='foreign_engineer')
                return jsonify({'error': 'Можно менять только свой профиль'}), 403
        return None
    return None


def enforce_server_auth(auth_required=None):
    if auth_required is None:
        auth_required = AUTH_REQUIRED
    if not auth_required:
        return None
    path = request.path
    if path in PUBLIC_API_PATHS or path in PUBLIC_PAGES:
        return None
    if request.method == 'GET' and path in PUBLIC_API_GET_PATHS:
        return None

    if path.startswith('/api/'):
        if not session.get('user_id'):
            append_event('role_denied', path=path, method=request.method,
                         role=None, reason='unauthorized')
            return jsonify({'error': 'Требуется авторизация'}), 401
        role = session.get('role')
        if role == 'admin':
            return None
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            denied = _enforce_endpoint_roles(path, request.method, role)
            if denied is not None:
                return denied
        # ЦПУ (роль mcc) читает очереди диспетчера для своего экрана.
        if (request.method == 'GET' and role == 'mcc'
                and path in ('/api/dispatcher/queue', '/api/dispatcher/assigned',
                             '/api/dispatcher/priorities')):
            return None
        for prefix, roles in WRITE_ROLE_PREFIXES:
            if path == prefix or path.startswith(prefix):
                if role not in roles:
                    append_event('role_denied', path=path, method=request.method,
                                 role=role, reason='forbidden')
                    return jsonify({'error': 'Недостаточно прав'}), 403
                break
        return None

    if path in DASHBOARD_ROLES:
        if not session.get('user_id'):
            return redirect('/login.html')
        role = session.get('role')
        if role != 'admin' and role not in DASHBOARD_ROLES[path]:
            return redirect(ROLE_HOME.get(role, '/login.html'))
    return None

def apply_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    # strict-origin-when-cross-origin: путь не утекает, но тайловые серверы
    # (OSM) получают origin в Referer — иначе они отдают плитку «Access blocked».
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'geolocation=(self), microphone=(self), camera=()')
    # API и HTML не кэшируем: после перезапуска сервера браузер
    # не должен показывать старую карту/шаблон.
    if request.path.startswith('/api/') or response.mimetype == 'text/html':
        response.headers.setdefault('Cache-Control', 'no-store')
    return response
