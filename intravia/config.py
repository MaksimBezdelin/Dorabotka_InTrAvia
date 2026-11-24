"""Конфигурация InTrAvia (вынесено из app.py, поведение не менялось)."""
import json
import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv(path):
    """Простой загрузчик .env без внешних зависимостей (не переопределяет окружение)."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding='utf-8-sig') as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
    except OSError as exc:
        print(f'[ENV] Не удалось прочитать {path}: {exc}')


_load_dotenv(os.path.join(BASE_DIR, '.env'))

# INTRAVIA_DB позволяет тестам и стендам работать с отдельной копией базы.
DB_PATH = os.environ.get('INTRAVIA_DB', '').strip() or os.path.join(BASE_DIR, 'database.db')
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
MOBILE_DIR = os.path.join(BASE_DIR, 'mobile', 'www')
REGULATION_MINUTES = 15
WALK_SPEED_KMH = 5.0
RECHECK_DELAY_SEC = 10
# Кэш таблицы кандидатов живёт ограниченное время и сбрасывается при действиях.
CANDIDATE_CACHE_TTL_SEC = 60

# AUTH_REQUIRED=1 включает серверную проверку сессии и ролей (рекомендуется
# для любого стенда, доступного не только с localhost).
# По умолчанию сервер закрыт сессией. Для демо без входа: AUTH_REQUIRED=0.
AUTH_REQUIRED = os.environ.get('AUTH_REQUIRED', '1').strip().lower() in ('1', 'true', 'yes', 'on')
# CORS по умолчанию выключен: дашборды и мобильные страницы работают с того же origin.
CORS_ORIGINS = [o.strip() for o in os.environ.get('CORS_ORIGINS', '').split(',') if o.strip()]

FFMPEG_PATH = r'C:\Users\deela\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe'
if os.path.exists(FFMPEG_PATH):
    ffmpeg_dir = os.path.dirname(FFMPEG_PATH)
    cp = os.environ.get('PATH', '')
    if ffmpeg_dir not in cp.split(os.pathsep):
        os.environ['PATH'] = ffmpeg_dir + os.pathsep + cp

_REQUEST_TEST_LOCK = threading.Lock()
_REQUEST_TEST_OPTIONS = {}


def _request_test_option(request_id, key, default=None):
    with _REQUEST_TEST_LOCK:
        return _REQUEST_TEST_OPTIONS.get(str(request_id), {}).get(key, default)


def _request_test_options(request_id):
    """Копия демонстрационных настроек заявки (без схемы БД)."""
    with _REQUEST_TEST_LOCK:
        return dict(_REQUEST_TEST_OPTIONS.get(str(request_id), {}))


def _set_request_test_options(request_id, options):
    """Обновляет демонстрационные настройки заявки и возвращает их копию."""
    with _REQUEST_TEST_LOCK:
        current = dict(_REQUEST_TEST_OPTIONS.get(str(request_id), {}))
        current.update(options or {})
        _REQUEST_TEST_OPTIONS[str(request_id)] = current
        return dict(current)


def _request_regulation_minutes(request_id):
    value = _request_test_option(request_id, 'regulation_minutes')
    try:
        return int(value) if value else REGULATION_MINUTES
    except (TypeError, ValueError):
        return REGULATION_MINUTES


def _request_is_paused(request_id, now=None):
    """True, если для заявки включена пауза (бессрочная или до paused_until)."""
    if not request_id:
        return False
    options = _request_test_options(request_id)
    if options.get('paused'):
        return True
    paused_until = options.get('paused_until')
    if not paused_until:
        return False
    if isinstance(paused_until, (int, float)):
        return float(paused_until) > (now if now is not None else time.time())
    try:
        from datetime import datetime, timezone
        text = str(paused_until).strip().replace('Z', '+00:00')
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        current = now if now is not None else datetime.now(timezone.utc).timestamp()
        return moment.timestamp() > current
    except (TypeError, ValueError):
        return False

AIRPORTS = {
    'svo': 'Шереметьево (SVO)',
    'vdnh': 'ВДНХ',
}
DEFAULT_AIRPORT = 'vdnh'
SYSTEM_SETTINGS_PATH = os.path.join(BASE_DIR, 'data', 'system_settings.json')


def load_system_settings():
    try:
        with open(SYSTEM_SETTINGS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_system_settings(settings):
    os.makedirs(os.path.dirname(SYSTEM_SETTINGS_PATH), exist_ok=True)
    tmp_path = SYSTEM_SETTINGS_PATH + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, SYSTEM_SETTINGS_PATH)


def current_airport_payload():
    settings = load_system_settings()
    code = settings.get('airport')
    if code not in AIRPORTS:
        code = DEFAULT_AIRPORT
    return {
        'airport': code,
        'name': AIRPORTS[code],
        'confirmed_at': settings.get('confirmed_at'),
        'confirmed_by': settings.get('confirmed_by'),
    }

OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434/v1/')
OLLAMA_API_KEY = os.environ.get('OLLAMA_API_KEY', 'ollama')
LLM_MODEL = os.environ.get('LLM_MODEL', 'qwen2.5:7b')
LLM_ENABLED = os.environ.get('LLM_ENABLED', '1') == '1'
WHISPER_MODEL = os.environ.get('WHISPER_MODEL', 'small')

LOCATION_CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'location.json')
