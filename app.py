import os
import re
import math
import time
import json
import csv
import io
import base64
import sqlite3
import tempfile
import hashlib
import uuid
import subprocess
import threading
import networkx as nx
from datetime import datetime, timezone, timedelta
from flask_cors import CORS
from flask import (Flask, request, jsonify, session,
                   render_template, redirect, url_for, Response, abort,
                   send_from_directory)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
    print('[LLM] openai не установлен')

# ------------------------------------------------------------
# Конфигурация
# ------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database.db')
MOBILE_SESSIONS_DB = os.path.join(BASE_DIR, 'mobile_sessions.db')
MOBILE_SESSION_TTL_SECONDS = 300
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
MOBILE_DIR = os.path.join(BASE_DIR, 'mobile', 'www')
WALK_SPEED_KMH = 5.0
RECHECK_DELAY_SEC = 10

FFMPEG_PATH = r'C:\Users\deela\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe'
if os.path.exists(FFMPEG_PATH):
    ffmpeg_dir = os.path.dirname(FFMPEG_PATH)
    cp = os.environ.get('PATH', '')
    if ffmpeg_dir not in cp.split(os.pathsep):
        os.environ['PATH'] = ffmpeg_dir + os.pathsep + cp

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.secret_key = 'intravia-secret-key-change-in-production'
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024
CORS(app, resources={r"/*": {"origins": "*"}})

# ------------------------------------------------------------
# Текущий аэропорт — системная настройка, общая для всех ролей
# ------------------------------------------------------------
AIRPORTS = {
    'svo': 'Шереметьево (SVO)',
    'vdnh': 'ВДНХ',
}
DEFAULT_AIRPORT = 'svo'
AIRPORT_CENTERS = {
    'svo': (55.975, 37.410),
    'vdnh': (55.8317, 37.6300),
}
SYSTEM_SETTINGS_PATH = os.path.join(BASE_DIR, 'data', 'system_settings.json')
AIRPORT_ASSIGNMENTS_PATH = os.path.join(BASE_DIR, 'data', 'airport_assignments.json')
EVENT_LOG_PATH = os.path.join(BASE_DIR, 'data', 'event_log.jsonl')
DEFECT_CATALOG_PATH = os.path.join(BASE_DIR, 'data', 'defect_catalog.json')
ENGINEER_TOOLS_PATH = os.path.join(BASE_DIR, 'data', 'engineer_tools.json')
TRACK_OBJECTS_PATH = os.path.join(BASE_DIR, 'data', 'track_objects.json')

# Финальный этап: неизменяемый журнал событий и служебное состояние.
_EVENT_LOG_LOCK = threading.Lock()
_TOOL_STATUS_LOCK = threading.Lock()
_ROUTE_RECALC_STATE = {}
_RISK_WARNING_STATE = {}
_RISK_WARNINGS = {}
_AIRCRAFT_ARRIVAL_STATE = {}
_DATA_WARNING_STATE = {}
_CANDIDATE_LOG_STATE = {}
TRACK_BOARD_DELAY_MIN = 10.0 / 60.0   # 10 секунд на посадку в спецтранспорт
TOOL_PICKUP_DELAY_MIN = 30.0 / 60.0   # 30 секунд на получение инструмента
NORM_LIMIT_MINUTES = 15.0


def _iso_ms(value=None):
    """ISO-8601 UTC с точностью до миллисекунд, как требует положение."""
    if isinstance(value, datetime):
        dt = value
    elif value:
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            dt = datetime.now(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _log_event(event_type, actor='', request_id='', details=None, at=None):
    """Append-only журнал: одна строка JSONL на событие, без изменения старых строк."""
    entry = {
        't': _iso_ms(at),
        'type': str(event_type or 'event'),
        'actor': str(actor or ''),
        'request_id': str(request_id or ''),
        'details': details if isinstance(details, dict) else {'value': details},
    }
    try:
        os.makedirs(os.path.dirname(EVENT_LOG_PATH), exist_ok=True)
        with _EVENT_LOG_LOCK:
            with open(EVENT_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception as exc:
        print(f'[EVENT LOG] {exc}')
    return entry


def load_engineer_tools():
    try:
        with open(ENGINEER_TOOLS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_engineer_tools(data):
    os.makedirs(os.path.dirname(ENGINEER_TOOLS_PATH), exist_ok=True)
    tmp = ENGINEER_TOOLS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ENGINEER_TOOLS_PATH)


def set_engineer_tool_status(engineer_id, status, comment=''):
    status = str(status or '').strip().lower()
    if status not in ('on_hand', 'at_site', 'warehouse', 'none', 'unknown'):
        raise ValueError('Недопустимый статус инструмента')
    with _TOOL_STATUS_LOCK:
        data = load_engineer_tools()
        data[str(engineer_id)] = {
            'status': status,
            'comment': str(comment or '')[:300],
            'updated_at': _iso_ms(),
        }
        save_engineer_tools(data)
    _log_event('tool_status', 'Инженер', '', {
        'engineer_id': str(engineer_id), 'status': status, 'comment': str(comment or '')[:300],
    })
    return data[str(engineer_id)]


def get_engineer_tool_status(engineer_id):
    item = load_engineer_tools().get(str(engineer_id)) or {}
    return str(item.get('status') or 'unknown'), item


def _request_needs_tools(request_row):
    """Tool pickup is required unless the engineer already has the tool at site."""
    def row_get(key):
        try:
            return request_row[key]
        except (KeyError, IndexError, TypeError):
            return ''
    fault = str(row_get('fault_type') or '').lower()
    text = (str(row_get('description') or '') + ' ' + str(row_get('comment') or '')).lower()
    if fault in ('avionics_nav', 'avionics_radio', 'avionics_autopilot', 'avionics_display', 'avionics_gps',
                 'avionics_transponder', 'electrical', 'electrical_generator', 'electrical_battery',
                 'electrical_short', 'electrical_lighting', 'pilot_voice'):
        return False
    tool_words = ('инструмент', 'tool', 'ключ', 'набор', 'склад', 'замен', 'ремонт', 'разбор', 'диагност')
    return any(word in text for word in tool_words) or fault not in ('other', '')


def _norm_tool_delay_min(engineer_id, request_row):
    if not _request_needs_tools(request_row):
        return 0.0
    status, _item = get_engineer_tool_status(engineer_id)
    if status in ('on_hand', 'at_site', 'none'):
        return 0.0
    return TOOL_PICKUP_DELAY_MIN


ATA_CHAPTERS = {
    '00': 'Общие положения', '05': 'Периодические проверки', '10': 'Кабина и её оборудование',
    '11': 'Плакаты и маркировка', '12': 'Электропитание', '20': 'Стандартная практика',
    '21': 'Кондиционирование и вентиляция', '22': 'Автопилот', '23': 'Связь',
    '24': 'Электросистема', '25': 'Оборудование салона', '26': 'Пожарная система',
    '27': 'Системы управления полётом', '28': 'Топливная система', '29': 'Гидравлическая система',
    '30': 'Противообледенительная система', '31': 'Индикация и регистрация',
    '32': 'Шасси', '33': 'Освещение', '34': 'Навигация', '35': 'Кислородное оборудование',
    '36': 'Пневматическая система', '38': 'Водоснабжение и отходы', '41': 'Колесо водяного балласта',
    '42': 'Интегрированная модульная авионика', '44': 'Бортовые развлекательные системы',
    '45': 'Централизованная диагностика', '46': 'Информационные системы', '47': 'Инертные газы',
    '49': 'Бортовые вспомогательные силовые установки', '51': 'Стандартная практика и конструкции',
    '52': 'Двери', '53': 'Фюзеляж', '54': 'Гондолы и пилоны', '55': 'Стабилизаторы',
    '56': 'Окна', '57': 'Крыло', '60': 'Стандартная практика (винтокрылые)',
    '61': 'Несущий винт', '62': 'Ротор', '63': 'Трансмиссия', '64': 'Хвостовой винт',
    '65': 'Приводы', '66': 'Складывающиеся лопасти', '67': 'Регулирование ротора',
    '70': 'Стандартная практика (двигатель)', '71': 'Силовая установка',
    '72': 'Двигатель', '73': 'Топливная система двигателя', '74': 'Зажигание',
    '75': 'Воздушный запуск', '76': 'Управление двигателем', '77': 'Индикация работы двигателя',
    '78': 'Выхлоп', '79': 'Масляная система', '80': 'Запуск двигателя',
    '81': 'Турбина', '82': 'Водяной впрыск', '83': 'Реверс тяги', '84': 'Топливная форсунка',
}

ATA_SKILL = [
    (('21', '35', '36', '38'), 'general'),
    (('22', '23', '31', '34', '42', '45', '46'), 'avionics'),
    (('24', '33'), 'electrical'),
    (('27', '29', '32', '52', '55', '57'), 'mechanical'),
    (('28', '70', '71', '72', '73', '74', '75', '76', '77', '78', '79', '80', '81', '82', '83', '84'), 'engine'),
    (('25', '26', '30', '49', '51', '53', '54', '56', '60', '61', '62', '63', '64', '65', '66', '67'), 'mechanical'),
]

FAULT_ATA = {
    'engine_failure': '72', 'engine_oil': '79', 'engine_overheat': '79', 'engine_vibration': '72',
    'engine_start': '80', 'fuel_leak': '28', 'fuel_system': '28', 'avionics_nav': '34',
    'avionics_radio': '23', 'avionics_autopilot': '22', 'avionics_transponder': '23',
    'avionics_display': '31', 'avionics_gps': '34', 'hydraulic': '29', 'hydraulic_leak': '29',
    'hydraulic_pressure': '29', 'electrical': '24', 'electrical_generator': '24',
    'electrical_battery': '24', 'electrical_short': '24', 'electrical_lighting': '33',
    'landing_gear': '32', 'pneumatic': '36', 'brake_system': '32', 'flap_issue': '27',
    'aileron_issue': '27', 'rudder_issue': '27', 'wing_damage': '57', 'fuselage_damage': '53',
    'door_issue': '52', 'air_condition': '21', 'pressurization': '21', 'oxygen_system': '35',
    'fire_system': '26', 'deicing_system': '30', 'pilot_voice': '00', 'other': '00',
}

DEFECT_CATEGORIES = [
    {'code': 'engine', 'name': 'Двигатель', 'skill': 'engine'},
    {'code': 'avionics', 'name': 'Авионика и связь', 'skill': 'avionics'},
    {'code': 'hydraulics', 'name': 'Гидравлика', 'skill': 'hydraulics'},
    {'code': 'electrical', 'name': 'Электрика', 'skill': 'electrical'},
    {'code': 'mechanical', 'name': 'Механика и планер', 'skill': 'mechanical'},
    {'code': 'general', 'name': 'Общесистемное оборудование', 'skill': 'general'},
    {'code': 'other', 'name': 'Прочее / свободный текст', 'skill': 'general'},
]

DEFAULT_DEFECTS = [
    {'text': 'Заклинило клапан ПОС', 'synonyms': ['клапан пос', 'заклинило клапан', 'пос', 'обледенение'],
     'ata': '30', 'category': 'mechanical', 'skill': 'mechanical'},
    {'text': 'Течь гидравлической жидкости', 'synonyms': ['течь гидравлики', 'утечка гидравлики', 'гидравлика'],
     'ata': '29', 'category': 'hydraulics', 'skill': 'hydraulics'},
    {'text': 'Отказ генератора', 'synonyms': ['генератор', 'нет генерации', 'электропитание'],
     'ata': '24', 'category': 'electrical', 'skill': 'electrical'},
    {'text': 'Неисправность автопилота', 'synonyms': ['автопилот', 'отключение автопилота'],
     'ata': '22', 'category': 'avionics', 'skill': 'avionics'},
    {'text': 'Повреждение шасси', 'synonyms': ['шасси', 'стойка шасси', 'колесо'],
     'ata': '32', 'category': 'mechanical', 'skill': 'mechanical'},
    {'text': 'Разгерметизация кабины', 'synonyms': ['разгерметизация', 'падение давления', 'герметичность'],
     'ata': '21', 'category': 'general', 'skill': 'general'},
    {'text': 'Отказ двигателя', 'synonyms': ['двигатель', 'помпаж', 'самовыключение'],
     'ata': '72', 'category': 'engine', 'skill': 'engine'},
    {'text': 'Утечка топлива', 'synonyms': ['топливо', 'течь топлива', 'fuel leak'],
     'ata': '28', 'category': 'engine', 'skill': 'engine'},
    {'text': 'Неисправность двери', 'synonyms': ['дверь', 'люк', 'трап'],
     'ata': '52', 'category': 'mechanical', 'skill': 'mechanical'},
    {'text': 'Отказ навигационного оборудования', 'synonyms': ['навигация', 'gps', 'курс'],
     'ata': '34', 'category': 'avionics', 'skill': 'avionics'},
    {'text': 'Не работает освещение', 'synonyms': ['освещение', 'фара', 'свет'],
     'ata': '33', 'category': 'electrical', 'skill': 'electrical'},
    {'text': 'Неисправность кондиционирования', 'synonyms': ['кондиционер', 'климат', 'вентиляция'],
     'ata': '21', 'category': 'general', 'skill': 'general'},
]


def _ata_skill(ata):
    ata = str(ata or '').strip()
    for codes, skill in ATA_SKILL:
        if ata in codes:
            return skill
    return 'general'


def _normalize_defect_text(text):
    text = str(text or '').strip().lower().replace('ё', 'е')
    text = re.sub(r'[^a-zа-я0-9]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _default_defect_catalog():
    now = _iso_ms()
    defects = []
    for item in DEFAULT_DEFECTS:
        text = item['text']
        defects.append({
            'id': 'def_' + hashlib.sha1(_normalize_defect_text(text).encode('utf-8')).hexdigest()[:10],
            'text': text,
            'normalized': _normalize_defect_text(text),
            'synonyms': item.get('synonyms') or [],
            'ata': item['ata'],
            'category': item['category'],
            'skill': item['skill'],
            'source': 'seed',
            'created_at': now,
        })
    return {
        'version': 1,
        'categories': DEFECT_CATEGORIES,
        'ata_chapters': ATA_CHAPTERS,
        'defects': defects,
    }


def load_defect_catalog():
    try:
        with open(DEFECT_CATALOG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('defects'), list):
            data.setdefault('categories', DEFECT_CATEGORIES)
            data.setdefault('ata_chapters', ATA_CHAPTERS)
            return data
    except (OSError, ValueError):
        pass
    data = _default_defect_catalog()
    try:
        os.makedirs(os.path.dirname(DEFECT_CATALOG_PATH), exist_ok=True)
        with open(DEFECT_CATALOG_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f'[DEFECT CATALOG] {exc}')
    return data


def save_defect_catalog(data):
    os.makedirs(os.path.dirname(DEFECT_CATALOG_PATH), exist_ok=True)
    tmp = DEFECT_CATALOG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DEFECT_CATALOG_PATH)


def search_defect_catalog(query='', ata='', category='', limit=20):
    catalog = load_defect_catalog()
    q = _normalize_defect_text(query)
    q_tokens = {t for t in q.split() if len(t) >= 3}
    ata = str(ata or '').strip()
    category = str(category or '').strip()
    scored = []
    for item in catalog.get('defects') or []:
        if not isinstance(item, dict):
            continue
        if ata and str(item.get('ata') or '') != ata:
            continue
        if category and str(item.get('category') or '') != category:
            continue
        score = 0
        text_norm = item.get('normalized') or _normalize_defect_text(item.get('text'))
        if q:
            if q == text_norm:
                score = 100
            elif q in text_norm or text_norm in q:
                score = 70
            else:
                for syn in item.get('synonyms') or []:
                    syn_norm = _normalize_defect_text(syn)
                    if syn_norm and (syn_norm in q or q in syn_norm):
                        score = max(score, 60)
                        break
                if score == 0 and q_tokens:
                    item_tokens = set(text_norm.split()) | {
                        t for syn in (item.get('synonyms') or []) for t in _normalize_defect_text(syn).split()
                    }
                    overlap = len(q_tokens & item_tokens)
                    if overlap:
                        score = 30 + overlap
        else:
            score = 1
        if score:
            scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], str(x[1].get('text') or '').lower()))
    return [dict(item, score=score) for score, item in scored[:max(1, int(limit))]]


def classify_defect(text, ata='', category=''):
    """Open classifier: free text + ATA + category, no code changes for new defects."""
    catalog = load_defect_catalog()
    raw_text = str(text or '').strip()
    norm = _normalize_defect_text(raw_text)
    ata = str(ata or '').strip()
    category = str(category or '').strip()
    categories = {c.get('code'): c for c in (catalog.get('categories') or []) if isinstance(c, dict)}

    matched = None
    if norm or ata:
        candidates = search_defect_catalog(norm, ata, category, limit=1)
        if candidates and int(candidates[0].get('score') or 0) >= 30:
            matched = candidates[0]

    if matched:
        resolved_ata = str(matched.get('ata') or ata or '00')
        resolved_category = str(matched.get('category') or category or 'other')
        skill = str(matched.get('skill') or _ata_skill(resolved_ata))
        return {
            'text': raw_text or matched.get('text'),
            'matched_text': matched.get('text'),
            'defect_id': matched.get('id'),
            'ata': resolved_ata,
            'ata_name': ATA_CHAPTERS.get(resolved_ata, ''),
            'category': resolved_category,
            'category_name': (categories.get(resolved_category) or {}).get('name', resolved_category),
            'skill': skill,
            'method': 'catalog',
            'confidence': min(1.0, round(int(matched.get('score') or 0) / 100.0, 2)),
        }

    resolved_ata = ata if ata in ATA_CHAPTERS else ''
    if not resolved_ata:
        for keyword, chapter in (
            ('пос', '30'), ('обледен', '30'), ('противообледен', '30'),
            ('гидравл', '29'), ('шасси', '32'), ('тормоз', '32'), ('колес', '32'),
            ('двигател', '72'), ('масл', '79'), ('топлив', '28'), ('генератор', '24'),
            ('электр', '24'), ('освещ', '33'), ('свет', '33'), ('автопилот', '22'),
            ('навигац', '34'), ('gps', '34'), ('связ', '23'), ('радио', '23'),
            ('двер', '52'), ('люк', '52'), ('фюзеляж', '53'), ('крыл', '57'),
            ('кондиционер', '21'), ('герметич', '21'), ('кислород', '35'), ('пожар', '26'),
        ):
            if keyword in norm:
                resolved_ata = chapter
                break
    resolved_ata = resolved_ata or '00'
    resolved_category = category if category in categories else None
    if not resolved_category:
        skill_guess = _ata_skill(resolved_ata)
        resolved_category = next((c.get('code') for c in categories.values()
                                  if c.get('skill') == skill_guess), 'other')
    skill = (categories.get(resolved_category) or {}).get('skill') or _ata_skill(resolved_ata)
    return {
        'text': raw_text,
        'matched_text': None,
        'defect_id': None,
        'ata': resolved_ata,
        'ata_name': ATA_CHAPTERS.get(resolved_ata, ''),
        'category': resolved_category,
        'category_name': (categories.get(resolved_category) or {}).get('name', resolved_category),
        'skill': skill,
        'method': 'keywords' if resolved_ata != '00' else 'free_text',
        'confidence': 0.4 if resolved_ata != '00' else 0.2,
    }


def add_defect_to_catalog(text, ata='', category='', skill='', synonyms=None):
    text = str(text or '').strip()
    if len(text) < 2:
        raise ValueError('Опишите неисправность')
    classification = classify_defect(text, ata, category)
    norm = _normalize_defect_text(text)
    catalog = load_defect_catalog()
    for item in catalog.get('defects') or []:
        if _normalize_defect_text(item.get('text')) == norm:
            item.update({
                'ata': classification['ata'], 'category': classification['category'],
                'skill': skill or classification['skill'],
                'synonyms': sorted(set([str(s) for s in (item.get('synonyms') or [])] +
                                       [str(s) for s in (synonyms or [])])),
            })
            save_defect_catalog(catalog)
            return item, False
    item = {
        'id': 'def_' + hashlib.sha1((norm + '|' + classification['ata']).encode('utf-8')).hexdigest()[:10],
        'text': text,
        'normalized': norm,
        'synonyms': [str(s) for s in (synonyms or []) if str(s).strip()],
        'ata': classification['ata'],
        'category': classification['category'],
        'skill': skill or classification['skill'],
        'source': 'user',
        'created_at': _iso_ms(),
    }
    catalog.setdefault('defects', []).append(item)
    save_defect_catalog(catalog)
    _log_event('defect_added', 'Пользователь', '', {
        'defect_id': item['id'], 'text': text, 'ata': item['ata'], 'category': item['category'],
    })
    return item, True


def _request_classification(request_row):
    def row_get(key):
        try:
            return request_row[key]
        except (KeyError, IndexError, TypeError):
            return ''
    description = str(row_get('description') or '')
    fault = str(row_get('fault_type') or 'other')
    ata = str(row_get('mcc_ata') or '')
    if not description and not ata:
        description = fault
    result = classify_defect(description or fault, ata=ata)
    if result['method'] == 'free_text':
        guessed_ata = FAULT_ATA.get(fault, '00')
        if guessed_ata != '00':
            result = classify_defect(description or fault, ata=guessed_ata)
    result['fault_type'] = fault
    return result


def _read_event_log():
    events = []
    try:
        with open(EVENT_LOG_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        events.append(item)
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    events.sort(key=lambda item: str(item.get('t') or ''))
    return events


def load_airport_assignments():
    try:
        with open(AIRPORT_ASSIGNMENTS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {'engineers': {}, 'transport': {}}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'engineers': {}, 'transport': {}}


def save_airport_assignments(data):
    os.makedirs(os.path.dirname(AIRPORT_ASSIGNMENTS_PATH), exist_ok=True)
    tmp = AIRPORT_ASSIGNMENTS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, AIRPORT_ASSIGNMENTS_PATH)


def assigned_airport(kind, object_id, assignments=None):
    assignments = assignments if assignments is not None else load_airport_assignments()
    code = (assignments.get(kind, {}).get(str(object_id)) or DEFAULT_AIRPORT).lower()
    return code if code in AIRPORTS else DEFAULT_AIRPORT


def set_assigned_airport(kind, object_id, code):
    code = str(code or DEFAULT_AIRPORT).lower()
    if code not in AIRPORTS:
        raise ValueError('Неизвестный аэропорт')
    assignments = load_airport_assignments()
    assignments.setdefault(kind, {})[str(object_id)] = code
    save_airport_assignments(assignments)
    return code


def object_belongs_to_airport(kind, object_id, airport=None, assignments=None):
    airport = airport or current_airport_payload()['airport']
    return assigned_airport(kind, object_id, assignments) == airport


def filter_objects_by_airport(kind, rows, airport=None, assignments=None):
    airport = airport or current_airport_payload()['airport']
    assignments = assignments if assignments is not None else load_airport_assignments()
    result = []
    for row in rows:
        item = dict(row)
        item['airport'] = assigned_airport(kind, item['id'], assignments)
        if item['airport'] == airport:
            result.append(item)
    return result


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


@app.errorhandler(404)
def _json_404(e):
    if request.path.startswith('/api/'):
        return jsonify({
            'success': False,
            'error': 'Endpoint not found',
            'path': request.path,
            'method': request.method,
            'hint': 'Проверьте, что на сервере установлен актуальный app.py и Flask перезапущен.'
        }), 404
    return e


@app.errorhandler(405)
def _json_405(e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Method not allowed'}), 405
    return e


@app.errorhandler(500)
def _json_500(e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Internal server error'}), 500
    return e


# ------------------------------------------------------------
# LLM: Ollama
# ------------------------------------------------------------
OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434/v1/')
OLLAMA_API_KEY = os.environ.get('OLLAMA_API_KEY', 'ollama')
LLM_MODEL = os.environ.get('LLM_MODEL', 'qwen2.5:7b')
LLM_ENABLED = os.environ.get('LLM_ENABLED', '1') == '1'
WHISPER_MODEL = os.environ.get('WHISPER_MODEL', 'small')

_llm = None
if OpenAI is not None and LLM_ENABLED:
    try:
        _llm = OpenAI(api_key=OLLAMA_API_KEY, base_url=OLLAMA_BASE_URL)
        print(f'[LLM] Ollama: model={LLM_MODEL}, base={OLLAMA_BASE_URL}')
    except Exception as e:
        print(f'[LLM] init error: {e}')


# ------------------------------------------------------------
# Стоянки и объекты — из static/svo_all.geojson
# ------------------------------------------------------------
PARKING_COORDS = {}
PARKING_META = {}
MAP_OBJECTS = []


def _vdnh_service_zone_center(zone_id):
    """Return the official stand center for a VDNH service zone."""
    try:
        with open(os.path.join(STATIC_DIR, 'vdnh', 'stands.json'), 'r', encoding='utf-8') as f:
            stands = json.load(f)
        for stand in stands if isinstance(stands, list) else []:
            if str(stand.get('zone_num')) == str(zone_id):
                return float(stand['lat']), float(stand['lon'])
    except (OSError, ValueError, TypeError, KeyError):
        pass
    path = os.path.join(STATIC_DIR, 'vdnh', 'service_zones.geojson')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            features = json.load(f).get('features', [])
        for feature in features:
            props = feature.get('properties') or {}
            if str(props.get('id')) != str(zone_id):
                continue
            geom = feature.get('geometry') or {}
            coords = geom.get('coordinates') or []
            polygons = coords if geom.get('type') == 'MultiPolygon' else [coords]
            points = []
            for polygon in polygons:
                if not polygon:
                    continue
                # Use only the outer ring; holes must not shift the zone center.
                for point in (polygon[0] or []):
                    if isinstance(point, (list, tuple)) and len(point) >= 2:
                        points.append((float(point[1]), float(point[0])))
            if points:
                return (sum(x[0] for x in points) / len(points),
                        sum(x[1] for x in points) / len(points))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'[VDNH] service zone {zone_id} read error: {exc}')
    return None


def _request_target_coords(parking_id, source=None):
    # На ВДНХ parking_id — это идентификатор сервисной зоны (1..10),
    # а не стоянка из набора Шереметьево. Иначе маршрут уходил в SVO.
    if current_airport_payload().get('airport') == 'vdnh':
        zone = _vdnh_service_zone_center(parking_id)
        if zone is not None:
            return zone
    return PARKING_COORDS.get(str(parking_id or ''))

# Динамические зоны безопасности самолётов на рулении (без изменения схемы БД).
# Координаты обновляются диспетчерской картой и используются для блокировки
# перемещения инженеров и спецтранспорта через занятый самолётом коридор.
AIRCRAFT_TAXI_RESERVATIONS = {}
AIRCRAFT_TAXI_LOCK = threading.RLock()
AIRCRAFT_TAXI_SAFETY_METERS = 45.0


def _distance_meters(lat1, lng1, lat2, lng2):
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _point_segment_distance_m(lat, lng, a, b):
    # Локальная плоская проекция достаточна для коротких рулёжных сегментов.
    scale_x = 111320.0 * math.cos(math.radians(lat))
    scale_y = 110540.0
    px, py = (lng - lng) * scale_x, 0.0
    ax, ay = (float(a[1]) - lng) * scale_x, (float(a[0]) - lat) * scale_y
    bx, by = (float(b[1]) - lng) * scale_x, (float(b[0]) - lat) * scale_y
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / denom))
    return math.hypot(ax + t * dx, ay + t * dy)


def _aircraft_taxi_conflict(lat, lng):
    with AIRCRAFT_TAXI_LOCK:
        for reservation in AIRCRAFT_TAXI_RESERVATIONS.values():
            route = reservation.get('route') or []
            if len(route) == 1 and _distance_meters(lat, lng, route[0][0], route[0][1]) < AIRCRAFT_TAXI_SAFETY_METERS:
                return reservation.get('aircraft') or 'самолёт'
            for idx in range(len(route) - 1):
                if _point_segment_distance_m(lat, lng, route[idx], route[idx + 1]) < AIRCRAFT_TAXI_SAFETY_METERS:
                    return reservation.get('aircraft') or 'самолёт'
    return None

_PARKING_FALLBACK = {
    '101': (55.9807, 37.4171), '102': (55.9806, 37.4171),
    '103': (55.9801, 37.4173), '104': (55.9800, 37.4174),
    '105': (55.9808, 37.4163), '106': (55.9807, 37.4164),
    '107': (55.9804, 37.4172), '108': (55.9803, 37.4173),
    '109': (55.9801, 37.4166), '110': (55.9800, 37.4167),
    '111': (55.9797, 37.4175), '112': (55.9796, 37.4176),
    '113': (55.9794, 37.4170), '114': (55.9793, 37.4170),
    '115': (55.9790, 37.4179), '116': (55.9789, 37.4179),
    '117': (55.9785, 37.4183), '118': (55.9782, 37.4176),
    '119': (55.9783, 37.4172), '120': (55.9784, 37.4172),
    '121': (55.9785, 37.4171), '122': (55.9783, 37.4181),
    '124': (55.9799, 37.4095), '125': (55.9797, 37.4097),
    '126': (55.9796, 37.4113), '127': (55.9796, 37.4115),
    '128': (55.9792, 37.4100), '129': (55.9790, 37.4102),
    '130': (55.9796, 37.4111), '131': (55.9791, 37.4111),
    '132': (55.9790, 37.4112), '134': (55.9789, 37.4115),
    '135': (55.9788, 37.4116), '136': (55.9784, 37.4105),
    '137': (55.9782, 37.4106), '138': (55.9781, 37.4118),
    '139': (55.9780, 37.4120), '140': (55.9775, 37.4125),
    '141': (55.9771, 37.4119), '142': (55.9771, 37.4110),
    '143': (55.9772, 37.4104), '144': (55.9774, 37.4103),
    '145': (55.9774, 37.4121), '146': (55.9772, 37.4121),
    '11': (55.9624, 37.4013), '12': (55.9624, 37.4004),
    '13': (55.9625, 37.4017), '14': (55.9627, 37.4027),
    '15': (55.9627, 37.4030), '16': (55.9630, 37.4043),
    '17': (55.9632, 37.4047), '19': (55.9643, 37.4047),
    '20': (55.9645, 37.4046), '21': (55.9650, 37.4043),
    '22': (55.9652, 37.4042), '23': (55.9655, 37.4043),
    '24': (55.9656, 37.4046), '25': (55.9656, 37.4050),
    '26': (55.9650, 37.4055), '27': (55.9648, 37.4056),
    '28': (55.9642, 37.4058), '29': (55.9640, 37.4060),
    '30': (55.9635, 37.4074), '31': (55.9636, 37.4087),
    '32': (55.9636, 37.4092), '33': (55.9637, 37.4107),
    '34': (55.9638, 37.4108), '35': (55.9639, 37.4118),
    '36': (55.9644, 37.4128), '37': (55.9644, 37.4128),
    '38': (55.9648, 37.4130), '39': (55.9652, 37.4132),
    '40': (55.9656, 37.4134), '41': (55.9659, 37.4135),
    '46': (55.9664, 37.4134), '47': (55.9667, 37.4135),
    '48': (55.9669, 37.4137), '49': (55.9669, 37.4138),
    '50': (55.9670, 37.4146), '51': (55.9671, 37.4155),
    '52': (55.9672, 37.4163), '53': (55.9674, 37.4171),
    '54': (55.9674, 37.4172), '55': (55.9671, 37.4180),
    '57': (55.9669, 37.4181), '58': (55.9668, 37.4181),
    '59': (55.9664, 37.4179), '60': (55.9661, 37.4175),
}


def distance_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(d_lng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _normalize_parking_ref(raw):
    if not raw:
        return None
    s = str(raw).strip()
    if re.match(r'^\d{1,4}[A-Za-zА-Яа-я]?$', s):
        return s.upper()
    m = re.search(r'(\d{1,4}[A-Za-zА-Яа-я]?)', s)
    return m.group(1).upper() if m else None


def _color_from_props(props, default='#dc2626'):
    color = (props.get('stroke') or props.get('color')
             or props.get('fill') or props.get('fillColor')
             or default)
    color = str(color)
    if not color.startswith('#'):
        if re.match(r'^[0-9A-Fa-f]{6}$', color):
            color = '#' + color
    return color


def _float_from_props(props, key, default):
    try:
        v = props.get(key)
        if v is not None and str(v).strip() != '':
            return float(v)
    except (TypeError, ValueError):
        pass
    return default


def load_svo_geojson():
    """Загружает стоянки (Point) и объекты (Polygon/MultiPolygon/LineString)
    из static/svo_all.geojson."""
    global PARKING_COORDS, PARKING_META, MAP_OBJECTS

    PARKING_COORDS = {}
    PARKING_META = {}
    MAP_OBJECTS = []

    filepath = os.path.join(STATIC_DIR, 'svo_all.geojson')
    if not os.path.exists(filepath):
        print(f'[SVO] {filepath} не найден — fallback')
        for ref, (lat, lng) in _PARKING_FALLBACK.items():
            PARKING_COORDS[ref] = (lat, lng)
            PARKING_META[ref] = {'ref': ref, 'name': f'Стоянка {ref}',
                                 'lat': lat, 'lng': lng, 'terminal': ''}
        return

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f'[SVO] Ошибка чтения: {e}')
        for ref, (lat, lng) in _PARKING_FALLBACK.items():
            PARKING_COORDS[ref] = (lat, lng)
        return

    features = data.get('features', [])
    print(f'[SVO] Всего фич: {len(features)}')

    REF_KEYS = ('ref', 'REF', 'Ref', 'id', 'ID', 'Id',
                'number', 'NUMBER', 'Number', 'parking', 'PARKING',
                'parking_id', 'stand', 'STAND', 'name', 'NAME', 'Name',
                'title', 'label', 'code', 'CODE')

    points_count = 0
    polys_count = 0
    lines_count = 0
    skipped = 0

    for feature in features:
        geom = feature.get('geometry') or {}
        props = feature.get('properties') or {}
        gt = geom.get('type')

        # ─── Point → стоянка ───
        if gt == 'Point':
            coords = geom.get('coordinates') or []
            if len(coords) < 2:
                continue
            try:
                lng, lat = float(coords[0]), float(coords[1])
            except (TypeError, ValueError):
                continue

            raw_ref = None
            for k in REF_KEYS:
                v = props.get(k)
                if v is not None and str(v).strip():
                    raw_ref = str(v).strip()
                    break

            if not raw_ref:
                skipped += 1
                continue
            ref = _normalize_parking_ref(raw_ref)
            if not ref:
                skipped += 1
                continue

            PARKING_COORDS[ref] = (lat, lng)
            PARKING_META[ref] = {
                'ref': ref,
                'name': props.get('name') or props.get('NAME') or f'Стоянка {ref}',
                'terminal': props.get('terminal') or props.get('TERMINAL') or '',
                'lat': lat, 'lng': lng,
            }
            points_count += 1

        # ─── Polygon / MultiPolygon → объект ───
        elif gt in ('Polygon', 'MultiPolygon'):
            color = _color_from_props(props, '#dc2626')
            weight = _float_from_props(props, 'stroke-width', 2.0)
            opacity = _float_from_props(props, 'stroke-opacity', 0.95)
            fill_opacity = _float_from_props(props, 'fill-opacity', 0.35)
            if fill_opacity < 0.15:
                fill_opacity = 0.30

            name = (props.get('name') or props.get('NAME')
                    or props.get('description') or props.get('title')
                    or 'Объект')

            # Polygon:      [[ring], ...]              → [ring]
            # MultiPolygon: [[[ring1, ring2]], ...]    → [[ring1, ring2], ...]
            if gt == 'Polygon':
                polygons = [geom.get('coordinates') or []]
            else:
                polygons = geom.get('coordinates') or []

            for poly in polygons:
                if not poly:
                    continue
                rings_latlngs = []
                for ring in poly:
                    if not ring:
                        continue
                    ring_ll = []
                    for c in ring:
                        if isinstance(c, (list, tuple)) and len(c) >= 2:
                            try:
                                ring_ll.append([float(c[1]), float(c[0])])
                            except (TypeError, ValueError):
                                continue
                    if len(ring_ll) >= 3:
                        rings_latlngs.append(ring_ll)

                if not rings_latlngs:
                    continue

                # Leaflet Polygon: 1 кольцо → [ [lat,lng], ... ]
                #                  2+ колец → [ [ [lat,lng], ... ], ... ]
                latlngs = rings_latlngs[0] if len(rings_latlngs) == 1 else rings_latlngs

                MAP_OBJECTS.append({
                    'name': str(name),
                    'latlngs': latlngs,
                    'color': color,
                    'weight': weight,
                    'opacity': opacity,
                    'fillOpacity': fill_opacity,
                })
                polys_count += 1

        # ─── LineString → объект-линия ───
        elif gt == 'LineString':
            coords = geom.get('coordinates') or []
            if len(coords) < 2:
                continue
            color = _color_from_props(props, '#dc2626')
            weight = _float_from_props(props, 'stroke-width', 2.5)
            name = (props.get('name') or props.get('NAME')
                    or props.get('description') or props.get('title')
                    or 'Линия')
            latlngs = []
            for c in coords:
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    try:
                        latlngs.append([float(c[1]), float(c[0])])
                    except (TypeError, ValueError):
                        continue
            if len(latlngs) >= 2:
                MAP_OBJECTS.append({
                    'name': str(name),
                    'latlngs': latlngs,
                    'color': color,
                    'weight': weight,
                    'opacity': 0.95,
                    'fillOpacity': 0,
                    'isLine': True,
                })
                lines_count += 1

    print(f'[SVO] Загружено: стоянок={points_count}, '
          f'полигонов={polys_count}, линий={lines_count}')
    if points_count == 0:
        print('[SVO] ВНИМАНИЕ: точки не найдены — fallback')
        for ref, (lat, lng) in _PARKING_FALLBACK.items():
            PARKING_COORDS[ref] = (lat, lng)
            PARKING_META[ref] = {'ref': ref, 'name': f'Стоянка {ref}',
                                 'lat': lat, 'lng': lng, 'terminal': ''}
    if skipped:
        print(f'[SVO] Пропущено точек без ref: {skipped}')


# ============================================================
# Дорожный граф
# ============================================================
road_graph_vehicle = nx.Graph()
vdnh_main_graph = nx.Graph()
vdnh_road_graph = nx.Graph()
vdnh_transport_graph = nx.Graph()
osm_road_graphs = {'svo': nx.Graph(), 'vdnh': nx.Graph()}


def _load_osm_geojson_graph(filepath, default_speed=8.0):
    """Load a hidden routing graph from OSM-derived GeoJSON. No road layer is added to the map."""
    graph = nx.Graph()
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return graph
    speed_defaults = {
        'motorway': 60, 'trunk': 45, 'primary': 35, 'secondary': 30,
        'tertiary': 25, 'unclassified': 20, 'residential': 15,
        'service': 10, 'living_street': 8, 'pedestrian': 5, 'footway': 5,
        'path': 5, 'track': 8
    }
    # Main-road preference is a small route-cost bias; ETA still uses real speed limits.
    preference = {
        'motorway': 0.82, 'trunk': 0.86, 'primary': 0.90, 'secondary': 0.94,
        'tertiary': 0.97, 'unclassified': 1.0, 'residential': 1.02,
        'service': 1.08, 'living_street': 1.10, 'pedestrian': 1.12,
        'footway': 1.12, 'path': 1.15, 'track': 1.08
    }
    for feature in data.get('features', []):
        geom = feature.get('geometry') or {}
        props = feature.get('properties') or {}
        if geom.get('type') not in ('LineString', 'MultiLineString'):
            continue
        raw_highway = props.get('highway')
        raw_type = str(props.get('type') or '').lower()
        category = str(props.get('category') or '').lower()
        # В локальном наборе Шереметьево дороги размечены category=vehicle,
        # а у ВДНХ часть проездов обозначена type=taxi. Это дорожные рёбра,
        # в отличие от runway/taxiway для воздушных судов.
        if not raw_highway and category == 'vehicle':
            raw_type = 'service'
        if not raw_highway and raw_type == 'taxi':
            raw_type = 'service'
        if not raw_highway and raw_type and raw_type not in ('service', 'pedestrian'):
            continue  # Не прокладываем путь по ВПП и прочим недорожным объектам.
        highway = raw_highway or raw_type or 'service'
        if isinstance(highway, list): highway = highway[0] if highway else 'service'
        highway = str(highway).lower()
        lines = geom.get('coordinates') or []
        if geom.get('type') == 'LineString': lines = [lines]
        raw_speed = props.get('spd_kmh') or props.get('maxspeed_kmh') or props.get('maxspeed')
        try:
            if isinstance(raw_speed, str):
                import re
                match = re.search(r'\d+(?:[.,]\d+)?', raw_speed)
                raw_speed = match.group(0).replace(',', '.') if match else None
            speed = float(raw_speed or speed_defaults.get(highway, default_speed))
        except (TypeError, ValueError):
            speed = speed_defaults.get(highway, default_speed)
        speed = max(3.0, min(speed, 80.0))
        for line in lines:
            for a, b in zip(line, line[1:]):
                try:
                    n1, n2 = (float(a[1]), float(a[0])), (float(b[1]), float(b[0]))
                except (TypeError, ValueError, IndexError):
                    continue
                dist = distance_km(n1[0], n1[1], n2[0], n2[1])
                if dist <= 0: continue
                minutes = dist / speed * 60.0
                cost = minutes * preference.get(highway, 1.05)
                if graph.has_edge(n1, n2) and graph[n1][n2]['route_cost'] <= cost:
                    continue
                graph.add_edge(n1, n2, weight=cost, route_cost=cost,
                               time_min=minutes, distance_km=dist,
                               speed_kmh=speed, highway=highway)
    return graph


def load_osm_road_graphs():
    """Prefer OSM GeoJSON exports when supplied; otherwise use bundled airport road data."""
    global osm_road_graphs
    base = STATIC_DIR
    sources = {
        'svo': [os.path.join(base, 'osm', 'svo_roads.geojson'), os.path.join(base, 'svo_osm_roads.geojson')],
        'vdnh': [os.path.join(base, 'osm', 'vdnh_roads.geojson'), os.path.join(base, 'vdnh', 'osm_roads.geojson')],
    }
    fallback = {'svo': os.path.join(base, 'roads.geojson'), 'vdnh': os.path.join(base, 'vdnh', 'roads.geojson')}
    built = {}
    for airport, candidates in sources.items():
        source = next((x for x in candidates if os.path.exists(x)), fallback[airport])
        built[airport] = _load_osm_geojson_graph(source, 8.0)
        print(f'[ROUTING GRAPH] {airport}: {built[airport].number_of_nodes()} узлов, источник={os.path.relpath(source, base)}')
    osm_road_graphs = built


def get_airport_road_path(airport, start_lat, start_lng, end_lat, end_lng):
    airport_key = str(airport or '').lower()
    if airport_key == 'vdnh':
        # Для ВДНХ главный граф — оранжевые taxi-дороги; OSM-граф не используем.
        return get_vdnh_main_path(start_lat, start_lng, end_lat, end_lng)
    graph = osm_road_graphs.get(airport_key)
    if graph is not None and graph.number_of_edges():
        start = find_nearest_node(graph, start_lat, start_lng)
        end = find_nearest_node(graph, end_lat, end_lng)
        if start is not None and end is not None:
            try:
                nodes = nx.shortest_path(graph, start, end, weight='route_cost')
                path = [[start_lat, start_lng]] + [[n[0], n[1]] for n in nodes] + [[end_lat, end_lng]]
                distance = minutes = 0.0
                for a, b in zip(path, path[1:]):
                    d = distance_km(a[0], a[1], b[0], b[1]); distance += d
                    edge = graph.get_edge_data(tuple(a), tuple(b)) if tuple(a) in graph and tuple(b) in graph else None
                    minutes += float(edge.get('time_min')) if edge else d / WALK_SPEED_KMH * 60
                return path, distance, minutes
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                pass
    # Если связного дорожного графа нет, маршрут недоступен.
    return None, 0.0, 0.0


def _engineer_graph_add_edges(graph, features, allowed_types):
    """Build a 5 km/h engineer graph; route cost is distance, not road speed."""
    for feature in features:
        geom = feature.get('geometry') or {}
        props = feature.get('properties') or {}
        road_type = str(props.get('type') or '').lower()
        if road_type not in allowed_types or geom.get('type') != 'LineString':
            continue
        coords = geom.get('coordinates') or []
        for a, b in zip(coords, coords[1:]):
            try:
                n1 = (float(a[1]), float(a[0]))
                n2 = (float(b[1]), float(b[0]))
            except (TypeError, ValueError, IndexError):
                continue
            dist = distance_km(n1[0], n1[1], n2[0], n2[1])
            if dist <= 0:
                continue
            if graph.has_edge(n1, n2) and graph[n1][n2].get('distance_km', float('inf')) <= dist:
                continue
            graph.add_edge(n1, n2, weight=dist, distance_km=dist,
                           time_min=dist / WALK_SPEED_KMH * 60.0,
                           speed_kmh=WALK_SPEED_KMH, road_type=road_type)


def _bridge_engineer_components(graph):
    components = [set(c) for c in nx.connected_components(graph)]
    if len(components) <= 1:
        return
    bridges = []
    for i in range(len(components)):
        for j in range(i + 1, len(components)):
            best_d, best_pair = float('inf'), None
            for a in components[i]:
                for b in components[j]:
                    d = distance_km(a[0], a[1], b[0], b[1]) * 1000.0
                    if d < best_d:
                        best_d, best_pair = d, (a, b)
            if best_pair and best_d <= 12.0:
                bridges.append((best_d, best_pair))
    for metres, (a, b) in sorted(bridges, key=lambda x: x[0]):
        if graph.has_edge(a, b):
            continue
        dist_km = metres / 1000.0
        graph.add_edge(a, b, weight=dist_km, distance_km=dist_km,
                       time_min=dist_km / WALK_SPEED_KMH * 60.0,
                       speed_kmh=WALK_SPEED_KMH, highway='connector')


def _vdnh_road_allows(feature, agent):
    """Read road access from the GeoJSON commentary.
    Old VDNKh data explicitly says:
      service     — «Спецтранспорт и инженер пешком»
      pedestrian  — «Инженер пешком»
      taxi/runway — «Воздушное судно»
    If commentary is absent, fall back to the type field.
    """
    props = feature.get('properties') or {}
    road_type = str(props.get('type') or '').lower()
    comment = str(props.get('commentary') or '').lower()
    if agent == 'engineer':
        if 'инженер' in comment:
            return True
        if 'воздушное судно' in comment and 'инженер' not in comment:
            return False
        return road_type in ('service', 'pedestrian')
    if agent == 'transport':
        if 'спецтранспорт' in comment:
            return True
        if 'инженер' in comment and 'спецтранспорт' not in comment:
            return False
        return road_type == 'service'
    return False


def _load_vdnh_official_graphs():
    """Build engineer/transport graphs from the official simulator network
    (nodes.json + edges.json). Access is taken from each edge's access/access_mode,
    exactly as the organizer simulator does.
    """
    nodes_path = os.path.join(STATIC_DIR, 'vdnh', 'nodes.json')
    edges_path = os.path.join(STATIC_DIR, 'vdnh', 'edges.json')
    try:
        with open(nodes_path, 'r', encoding='utf-8') as f:
            nodes_raw = json.load(f)
        with open(edges_path, 'r', encoding='utf-8') as f:
            edges_raw = json.load(f)
    except (OSError, ValueError):
        return None
    nodes = {}
    for node in nodes_raw if isinstance(nodes_raw, list) else []:
        try:
            nodes[str(node.get('node_id'))] = (float(node['lat']), float(node['lon']))
        except (KeyError, TypeError, ValueError):
            continue
    if not nodes or not edges_raw:
        return None
    engineer = nx.Graph()
    pedestrian = nx.Graph()
    transport = nx.Graph()

    def add_polyline(graph, coords, edge_distance_km, speed_kmh, road_type, network, edge_id):
        time_min = edge_distance_km / max(speed_kmh, 1.0) * 60.0
        for a, b in zip(coords, coords[1:]):
            seg = distance_km(a[0], a[1], b[0], b[1])
            if seg <= 0:
                continue
            seg_time = seg / max(speed_kmh, 1.0) * 60.0
            if graph.has_edge(a, b) and graph[a][b].get('distance_km', 1e9) <= seg:
                continue
            graph.add_edge(a, b, weight=seg_time if graph is transport else seg,
                           distance_km=seg, time_min=seg_time, speed_kmh=speed_kmh,
                           road_type=road_type, network=network, edge_id=edge_id)
        return time_min

    for edge in edges_raw if isinstance(edges_raw, list) else []:
        network = str(edge.get('network') or '').lower()
        if network == 'runway':
            continue
        access = {str(x).lower() for x in (edge.get('access') or [])}
        mode = str(edge.get('access_mode') or '').lower()
        engineer_ok = mode == 'foot' or 'pedestrian' in access
        transport_ok = mode == 'car'
        geom = edge.get('geometry') or {}
        raw_coords = geom.get('coordinates') or []
        coords = []
        for point in raw_coords:
            try:
                coords.append((float(point[1]), float(point[0])))
            except (TypeError, ValueError, IndexError):
                continue
        if len(coords) < 2:
            continue
        # Snap endpoints to the shared official node coordinates so edges meet.
        from_node = nodes.get(str(edge.get('from_node')))
        to_node = nodes.get(str(edge.get('to_node')))
        if from_node:
            coords[0] = from_node
        if to_node:
            coords[-1] = to_node
        try:
            distance = float(edge.get('length_m') or 0) / 1000.0
        except (TypeError, ValueError):
            distance = 0.0
        if distance <= 0:
            distance = sum(distance_km(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:]))
        try:
            speed = float(edge.get('spd_kmh') or (5.0 if mode == 'foot' else 10.0))
        except (TypeError, ValueError):
            speed = 10.0
        edge_id = edge.get('edge_id') or ''
        if engineer_ok:
            add_polyline(engineer, coords, distance, WALK_SPEED_KMH, network, network, edge_id)
            if network == 'pedestrian':
                add_polyline(pedestrian, coords, distance, WALK_SPEED_KMH, network, network, edge_id)
        if transport_ok:
            add_polyline(transport, coords, distance, speed, network, network, edge_id)
    if engineer.number_of_edges() == 0:
        return None
    return engineer, pedestrian, transport


def load_vdnh_road_graph():
    """Build VDNKh graphs from explicit road access comments.
    Engineer speed is exactly WALK_SPEED_KMH (5 km/h) on every edge.
    The transport graph uses service-road speed attributes (5/8/10/20 km/h).
    """
    global vdnh_main_graph, vdnh_road_graph, vdnh_transport_graph
    official = _load_vdnh_official_graphs()
    if official:
        vdnh_main_graph, vdnh_road_graph, vdnh_transport_graph = official
        print(f'[VDNH OFFICIAL GRAPH] engineer {vdnh_main_graph.number_of_nodes()} узлов / '
              f'{vdnh_main_graph.number_of_edges()} рёбер; transport '
              f'{vdnh_transport_graph.number_of_nodes()} / {vdnh_transport_graph.number_of_edges()}')
        return
    main = nx.Graph()
    fallback = nx.Graph()
    transport = nx.Graph()
    filepath = os.path.join(STATIC_DIR, 'vdnh', 'roads.geojson')
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            features = (json.load(f).get('features') or [])
    except (OSError, ValueError) as exc:
        print(f'[VDNH GRAPH] Не удалось загрузить roads.geojson: {exc}')
        vdnh_main_graph = main
        vdnh_road_graph = fallback
        vdnh_transport_graph = transport
        return

    engineer_features = [f for f in features if _vdnh_road_allows(f, 'engineer')]
    pedestrian_features = [f for f in engineer_features
                           if str((f.get('properties') or {}).get('type') or '').lower() == 'pedestrian']
    _engineer_graph_add_edges(main, engineer_features, {'taxi', 'service', 'pedestrian'})
    _engineer_graph_add_edges(fallback, pedestrian_features, {'pedestrian'})
    _vdnh_transport_graph_add_edges(transport, features)
    _bridge_engineer_components(main)
    _bridge_engineer_components(fallback)
    _bridge_transport_components(transport)
    vdnh_main_graph = main
    vdnh_road_graph = fallback
    vdnh_transport_graph = transport
    print(f'[VDNH MAIN GRAPH] {len(main.nodes)} узлов, {len(main.edges)} рёбер '
          f'(по комментариям roads.geojson: инженер, {WALK_SPEED_KMH:g} км/ч)')
    print(f'[VDNH FALLBACK GRAPH] {len(fallback.nodes)} узлов, {len(fallback.edges)} рёбер')
    print(f'[VDNH TRANSPORT GRAPH] {len(transport.nodes)} узлов, {len(transport.edges)} рёбер '
          f'(service 5/8/10/20 км/ч)')


def _vdnh_transport_graph_add_edges(graph, features):
    """Service-road graph for special transport. Edge weight is real travel time."""
    for feature in features:
        geom = feature.get('geometry') or {}
        props = feature.get('properties') or {}
        road_type = str(props.get('type') or '').lower()
        if road_type != 'service' or geom.get('type') != 'LineString':
            continue
        if not _vdnh_road_allows(feature, 'transport'):
            continue
        try:
            speed = float(props.get('spd_kmh') or 10.0)
        except (TypeError, ValueError):
            speed = 10.0
        if speed <= 0:
            speed = 10.0
        coords = geom.get('coordinates') or []
        for a, b in zip(coords, coords[1:]):
            try:
                n1 = (float(a[1]), float(a[0]))
                n2 = (float(b[1]), float(b[0]))
            except (TypeError, ValueError, IndexError):
                continue
            dist = distance_km(n1[0], n1[1], n2[0], n2[1])
            if dist <= 0:
                continue
            time_min = dist / speed * 60.0
            if graph.has_edge(n1, n2) and graph[n1][n2].get('time_min', float('inf')) <= time_min:
                continue
            graph.add_edge(n1, n2, weight=time_min, time_min=time_min,
                           distance_km=dist, speed_kmh=speed, road_type=road_type)


def _bridge_transport_components(graph):
    """Connect isolated service-road fragments with short connectors at 20 km/h."""
    components = [set(c) for c in nx.connected_components(graph)]
    if len(components) <= 1:
        return
    bridges = []
    for i in range(len(components)):
        for j in range(i + 1, len(components)):
            best_d, best_pair = float('inf'), None
            for a in components[i]:
                for b in components[j]:
                    d = distance_km(a[0], a[1], b[0], b[1]) * 1000.0
                    if d < best_d:
                        best_d, best_pair = d, (a, b)
            if best_pair and best_d <= 12.0:
                bridges.append((best_d, best_pair))
    for metres, (a, b) in sorted(bridges, key=lambda x: x[0]):
        if graph.has_edge(a, b):
            continue
        dist_km = metres / 1000.0
        time_min = dist_km / 20.0 * 60.0
        graph.add_edge(a, b, weight=time_min, time_min=time_min,
                       distance_km=dist_km, speed_kmh=20.0, highway='connector')


def _project_point_on_graph(graph, lat, lng):
    """Closest point on any graph edge, not just the closest node.
    This makes the engineer first reach the nearest road from roads.geojson
    and only then follow the road network.
    """
    if graph is None or graph.number_of_edges() == 0:
        return None
    cos_lat = math.cos(math.radians(lat))
    scale_x = 111320.0 * cos_lat
    scale_y = 110540.0
    best = None
    for u, v, edge_data in graph.edges(data=True):
        ax = (u[1] - lng) * scale_x
        ay = (u[0] - lat) * scale_y
        bx = (v[1] - lng) * scale_x
        by = (v[0] - lat) * scale_y
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        if denom <= 1e-9:
            t = 0.0
        else:
            t = max(0.0, min(1.0, -(ax * dx + ay * dy) / denom))
        px = ax + t * dx
        py = ay + t * dy
        dist_m = math.hypot(px, py)
        if best is not None and dist_m >= best['distance_m']:
            continue
        if t <= 1e-7:
            point = (u[0], u[1])
        elif t >= 1.0 - 1e-7:
            point = (v[0], v[1])
        else:
            point = (lat + py / scale_y, lng + px / scale_x)
        best = {
            'edge': (u, v), 't': t, 'point': point,
            'distance_m': dist_m, 'edge_data': edge_data,
        }
    return best


def _dedupe_route_points(points):
    result = []
    for point in points:
        point = [float(point[0]), float(point[1])]
        if result and distance_km(result[-1][0], result[-1][1], point[0], point[1]) < 0.0005:
            continue
        result.append(point)
    return result


def _route_over_projected_graph(graph, start_lat, start_lng, end_lat, end_lng,
                                weight='distance_km', choose_time=False):
    """Route via the nearest road edges. Only two short access connectors
    (engineer→road and road→parking) may be off-road; everything between
    the projections follows graph edges from roads.geojson.
    """
    if graph is None or graph.number_of_nodes() == 0:
        return None
    start_proj = _project_point_on_graph(graph, start_lat, start_lng)
    end_proj = _project_point_on_graph(graph, end_lat, end_lng)
    if not start_proj or not end_proj:
        return None

    best = None
    for start_node in start_proj['edge']:
        for end_node in end_proj['edge']:
            try:
                nodes = nx.shortest_path(graph, start_node, end_node, weight=weight)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                continue
            coords = _dedupe_route_points(
                [[start_lat, start_lng], list(start_proj['point'])] +
                [[n[0], n[1]] for n in nodes] +
                [list(end_proj['point']), [end_lat, end_lng]]
            )
            distance = 0.0
            for a, b in zip(coords, coords[1:]):
                distance += distance_km(a[0], a[1], b[0], b[1])

            start_speed = float(start_proj['edge_data'].get('speed_kmh') or WALK_SPEED_KMH)
            end_speed = float(end_proj['edge_data'].get('speed_kmh') or WALK_SPEED_KMH)
            total_min = (distance_km(start_lat, start_lng,
                                     start_proj['point'][0], start_proj['point'][1])
                         / WALK_SPEED_KMH * 60.0)
            total_min += (distance_km(start_proj['point'][0], start_proj['point'][1],
                                      start_node[0], start_node[1])
                          / max(start_speed, 1.0) * 60.0)
            for a, b in zip(nodes, nodes[1:]):
                edge = graph.get_edge_data(a, b) or {}
                seg_km = float(edge.get('distance_km') or
                               distance_km(a[0], a[1], b[0], b[1]))
                speed = float(edge.get('speed_kmh') or WALK_SPEED_KMH)
                total_min += float(edge.get('time_min') or (seg_km / max(speed, 1.0) * 60.0))
            total_min += (distance_km(end_node[0], end_node[1],
                                      end_proj['point'][0], end_proj['point'][1])
                          / max(end_speed, 1.0) * 60.0)
            total_min += (distance_km(end_proj['point'][0], end_proj['point'][1],
                                      end_lat, end_lng)
                          / WALK_SPEED_KMH * 60.0)
            key = total_min if choose_time else distance
            if best is None or key < best[0]:
                best = (key, coords, distance, total_min,
                        start_proj, end_proj, nodes)

    if not best:
        return None
    _key, coords, distance, total_min, start_proj, end_proj, nodes = best

    legs = []
    if distance_km(start_lat, start_lng, start_proj['point'][0], start_proj['point'][1]) > 0.001:
        acc_km = distance_km(start_lat, start_lng, start_proj['point'][0], start_proj['point'][1])
        legs.append({'from': [start_lat, start_lng], 'to': list(start_proj['point']),
                     'distance_km': round(acc_km, 4), 'speed_kmh': WALK_SPEED_KMH,
                     'time_min': round(acc_km / WALK_SPEED_KMH * 60.0, 3),
                     'mode': 'walk_access'})
    for a, b in zip(nodes, nodes[1:]):
        edge = graph.get_edge_data(a, b) or {}
        seg_km = float(edge.get('distance_km') or distance_km(a[0], a[1], b[0], b[1]))
        speed = float(edge.get('speed_kmh') or WALK_SPEED_KMH)
        legs.append({'from': [a[0], a[1]], 'to': [b[0], b[1]],
                     'distance_km': round(seg_km, 4), 'speed_kmh': speed,
                     'time_min': round(float(edge.get('time_min') or
                                             seg_km / max(speed, 1.0) * 60.0), 3),
                     'mode': 'connector' if edge.get('highway') == 'connector' else 'road'})
    if distance_km(end_proj['point'][0], end_proj['point'][1], end_lat, end_lng) > 0.001:
        egr_km = distance_km(end_proj['point'][0], end_proj['point'][1], end_lat, end_lng)
        legs.append({'from': list(end_proj['point']), 'to': [end_lat, end_lng],
                     'distance_km': round(egr_km, 4), 'speed_kmh': WALK_SPEED_KMH,
                     'time_min': round(egr_km / WALK_SPEED_KMH * 60.0, 3),
                     'mode': 'walk_egress'})
    return coords, distance, total_min, legs


def _route_on_engineer_graph(graph, start_lat, start_lng, end_lat, end_lng):
    result = _route_over_projected_graph(graph, start_lat, start_lng, end_lat, end_lng,
                                         weight='distance_km', choose_time=False)
    if not result:
        return None
    coords, distance, _total_min, _legs = result
    return coords, distance, distance / WALK_SPEED_KMH * 60.0


def get_vdnh_main_path(start_lat, start_lng, end_lat, end_lng):
    """Service-road graph first; pedestrian fallback. Taxiways/runway excluded."""
    result = _route_on_engineer_graph(vdnh_main_graph, start_lat, start_lng, end_lat, end_lng)
    if result:
        return result
    result = _route_on_engineer_graph(vdnh_road_graph, start_lat, start_lng, end_lat, end_lng)
    if result:
        print('[VDNH ROUTE] Сервисный граф недоступен, использован пешеходный запасной')
        return result
    print('[VDNH ROUTE] Связный граф roads.geojson не найден; прямая линия запрещена')
    return None


def _route_on_transport_graph(graph, start_lat, start_lng, end_lat, end_lng):
    """Shortest travel-time path over service roads with per-edge speed limits.
    The vehicle first reaches the nearest service-road edge; only the short
    access/egress connectors are made at walking speed.
    """
    return _route_over_projected_graph(graph, start_lat, start_lng, end_lat, end_lng,
                                       weight='time_min', choose_time=True)


def get_airport_transport_path(airport, start_lat, start_lng, end_lat, end_lng):
    """Special-transport path with speed-limit-aware ETA."""
    airport_key = str(airport or '').lower()
    if airport_key == 'vdnh':
        result = _route_on_transport_graph(vdnh_transport_graph,
                                           start_lat, start_lng, end_lat, end_lng)
        if result:
            return result
        # A service-road fragment may be disconnected. Keep the trip possible and
        # explicitly mark the fallback in route data.
        print('[VDNH TRANSPORT] Связный сервисный граф не найден; прямая линия запрещена')
        return None
    graph = osm_road_graphs.get(airport_key)
    if graph is not None and graph.number_of_edges():
        start = find_nearest_node(graph, start_lat, start_lng)
        end = find_nearest_node(graph, end_lat, end_lng)
        if start is not None and end is not None:
            try:
                nodes = nx.shortest_path(graph, start, end, weight='route_cost')
                path = [[start_lat, start_lng]] + [[n[0], n[1]] for n in nodes] + [[end_lat, end_lng]]
                distance = minutes = 0.0
                for a, b in zip(path, path[1:]):
                    d = distance_km(a[0], a[1], b[0], b[1])
                    distance += d
                    edge = graph.get_edge_data(tuple(a), tuple(b)) if tuple(a) in graph and tuple(b) in graph else None
                    minutes += float(edge.get('time_min')) if edge else d / 10.0 * 60
                return path, distance, minutes, []
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                pass
    return None


def load_road_graph():
    global road_graph_vehicle
    road_graph_vehicle = nx.Graph()
    filepath = os.path.join(STATIC_DIR, 'roads.geojson')
    if not os.path.exists(filepath):
        print(f'[GRAPH] {filepath} не найден')
        return
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f'[GRAPH] Ошибка: {e}')
        return

    segments = []
    for feature in data.get('features', []):
        geom = feature.get('geometry', {})
        if geom.get('type') != 'LineString':
            continue
        props = feature.get('properties') or {}
        if str(props.get('category') or '').strip().lower() not in ('vehicle', ''):
            continue
        coords = geom['coordinates']
        if len(coords) < 2:
            continue
        segments.append([(float(c[1]), float(c[0])) for c in coords])

    if not segments:
        print('[GRAPH] Нет LineString')
        return

    SNAP_M = 3.0
    TOL_LAT = SNAP_M / 111320.0
    TOL_LNG = SNAP_M / (111320.0 * math.cos(math.radians(55.98)))
    grid, cache = {}, {}

    def snap(lat, lng):
        key = (lat, lng)
        if key in cache:
            return cache[key]
        cell = (int(math.floor(lat / TOL_LAT)), int(math.floor(lng / TOL_LNG)))
        best, best_d2 = None, None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for cand in grid.get((cell[0] + di, cell[1] + dj), []):
                    dlat = (lat - cand[0]) / TOL_LAT
                    dlng = (lng - cand[1]) / TOL_LNG
                    d2 = dlat * dlat + dlng * dlng
                    if d2 <= 1.0 and (best is None or d2 < best_d2):
                        best, best_d2 = cand, d2
        if best is None:
            best = key
            grid.setdefault(cell, []).append(best)
        cache[key] = best
        return best

    for seg in segments:
        snapped = [snap(lat, lng) for lat, lng in seg]
        for i in range(len(snapped) - 1):
            n1, n2 = snapped[i], snapped[i + 1]
            if n1 == n2:
                continue
            w = distance_km(n1[0], n1[1], n2[0], n2[1])
            road_graph_vehicle.add_edge(n1, n2, weight=w)

    print(f'[GRAPH] {len(road_graph_vehicle.nodes)} узлов, '
          f'{len(road_graph_vehicle.edges)} рёбер')

    comps = list(nx.connected_components(road_graph_vehicle))
    if len(comps) > 1:
        _bridge_components(road_graph_vehicle, comps)


def _bridge_components(graph, components):
    components = [set(c) for c in components]
    n = len(components)
    if n < 2:
        return
    bridges = []
    for i in range(n):
        for j in range(i + 1, n):
            min_d, min_pair = float('inf'), None
            for na in components[i]:
                for nb in components[j]:
                    d = distance_km(na[0], na[1], nb[0], nb[1])
                    if d < min_d:
                        min_d, min_pair = d, (na, nb)
            if min_pair:
                bridges.append((min_d, i, j, min_pair))
    bridges.sort(key=lambda x: x[0])
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
            return True
        return False

    for d, i, j, pair in bridges:
        if union(i, j):
            graph.add_edge(pair[0], pair[1], weight=d)


def find_nearest_node(graph, lat, lng):
    if graph is None or len(graph.nodes) == 0:
        return None
    nearest, min_d = None, float('inf')
    for node in graph.nodes:
        d = distance_km(lat, lng, node[0], node[1])
        if d < min_d:
            min_d, nearest = d, node
    return nearest


def get_shortest_path(graph, start_lat, start_lng, end_lat, end_lng):
    if graph is None or len(graph.nodes) == 0:
        return None, 0
    s = find_nearest_node(graph, start_lat, start_lng)
    e = find_nearest_node(graph, end_lat, end_lng)
    if not s or not e:
        return None, 0
    try:
        path_nodes = nx.shortest_path(graph, s, e, weight='weight')
        coords = [[start_lat, start_lng]]
        for n in path_nodes:
            coords.append([n[0], n[1]])
        coords.append([end_lat, end_lng])
        total = 0.0
        for i in range(len(coords) - 1):
            total += distance_km(coords[i][0], coords[i][1],
                                 coords[i + 1][0], coords[i + 1][1])
        return coords, total
    except nx.NetworkXNoPath:
        return None, 0
    except Exception as ex:
        print(f'[GRAPH] Ошибка: {ex}')
        return None, 0


# ============================================================
# DB
# ============================================================
def get_db():
    # mode=rw: SQLite must open an existing database; never create an empty file.
    if not os.path.isfile(DB_PATH):
        raise RuntimeError(f'[DB] База данных не найдена: {DB_PATH}. Сервер не создаёт БД автоматически.')
    uri = 'file:' + DB_PATH.replace('\\', '/') + '?mode=rw'
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    return conn


def init_db():
    """Validate the existing database only. Never create, migrate, seed or alter schema/data."""
    print(f'[DB] Подключение к существующей базе: {DB_PATH}')
    if not os.path.isfile(DB_PATH):
        raise RuntimeError(f'[DB] Файл database.db не найден: {DB_PATH}. Создание отключено.')
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing = {row['name'] for row in cur.fetchall()}
        required = {
            'users', 'engineers', 'pilots', 'transport', 'requests',
            'aircraft_registry', 'mel_catalog', 'map_object_positions'
        }
        missing = sorted(required - existing)
        if missing:
            raise RuntimeError(
                '[DB] В существующей базе отсутствуют необходимые таблицы: '
                + ', '.join(missing)
                + '. Сервер не будет создавать или изменять таблицы.'
            )
        print(f'[DB] Проверка завершена. Таблиц найдено: {len(existing)}. Схема не изменялась.')
    finally:
        conn.close()


DEVICE_ID_RE = re.compile(r'^EN-(\d{3})$')


def get_engineer_number_from_device(device_id):
    if not device_id:
        return 0
    m = DEVICE_ID_RE.match(str(device_id))
    return int(m.group(1)) if m else 0


def get_used_numbers(conn):
    cur = conn.cursor()
    cur.execute("SELECT device_id FROM engineers")
    used = set()
    for r in cur.fetchall():
        n = get_engineer_number_from_device(r['device_id'])
        if n > 0:
            used.add(n)
    return used


def next_engineer_number(conn):
    used = get_used_numbers(conn)
    return (max(used) + 1) if used else 1


def normalize_device_id(device_id, number):
    if number > 0:
        return f'EN-{number:03d}'
    return device_id or 'EN-000'


# ============================================================
# Утилиты
# ============================================================
def set_engineer_busy(conn, engineer_id, task_text):
    if not engineer_id:
        return False
    cur = conn.cursor()
    cur.execute("SELECT id FROM engineers WHERE id=?", (engineer_id,))
    if not cur.fetchone():
        return False
    cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                (task_text, engineer_id))
    return cur.rowcount > 0


def set_engineer_free(conn, engineer_id):
    if not engineer_id:
        return False
    cur = conn.cursor()
    cur.execute("SELECT id FROM engineers WHERE id=?", (engineer_id,))
    if not cur.fetchone():
        return False
    cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?",
                (engineer_id,))
    return cur.rowcount > 0


def engineer_has_active_requests(conn, engineer_id, exclude_request_id=None):
    cur = conn.cursor()
    if exclude_request_id:
        cur.execute("""SELECT COUNT(*) FROM requests
            WHERE engineer_id=? AND status NOT IN ('completed','cancelled','flight_cancelled','report_submitted','deleted')
              AND id!=?""", (engineer_id, exclude_request_id))
    else:
        cur.execute("""SELECT COUNT(*) FROM requests
            WHERE engineer_id=? AND status NOT IN ('completed','cancelled','flight_cancelled','report_submitted','deleted')""",
                    (engineer_id,))
    return cur.fetchone()[0]


def release_engineers_without_active_requests(conn, engineer_ids):
    """Освобождает исполнителей после удаления заявок, если других активных задач нет."""
    released = []
    for engineer_id in {str(x) for x in engineer_ids if x not in (None, '')}:
        if engineer_has_active_requests(conn, engineer_id) == 0:
            if set_engineer_free(conn, engineer_id):
                released.append(engineer_id)
    return released


def sync_engineer_availability(conn):
    """Сверяет занятость с активными заявками и освобождает инженеров без задач."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM engineers WHERE status='busy'")
    busy_ids = [row['id'] for row in cur.fetchall()]
    return release_engineers_without_active_requests(conn, busy_ids)


FAULT_SKILL = {
    'engine_failure': 'engine', 'engine_oil': 'engine', 'engine_overheat': 'engine',
    'engine_vibration': 'engine', 'engine_start': 'engine', 'fuel_leak': 'engine',
    'fuel_system': 'engine', 'avionics_nav': 'avionics', 'avionics_radio': 'avionics',
    'avionics_autopilot': 'avionics', 'avionics_transponder': 'avionics',
    'avionics_display': 'avionics', 'avionics_gps': 'avionics', 'hydraulic': 'hydraulics',
    'hydraulic_leak': 'hydraulics', 'hydraulic_pressure': 'hydraulics',
    'electrical': 'electrical', 'electrical_generator': 'electrical',
    'electrical_battery': 'electrical', 'electrical_short': 'electrical',
    'electrical_lighting': 'electrical', 'landing_gear': 'mechanical',
    'pneumatic': 'mechanical', 'brake_system': 'mechanical', 'flap_issue': 'mechanical',
    'aileron_issue': 'mechanical', 'rudder_issue': 'mechanical', 'wing_damage': 'mechanical',
    'fuselage_damage': 'mechanical', 'door_issue': 'mechanical',
    'air_condition': 'general', 'pressurization': 'general', 'oxygen_system': 'general',
    'fire_system': 'general', 'deicing_system': 'general', 'pilot_voice': 'general',
    'other': 'general'
}

FAULT_SYNONYMS = {
    'pressurization': ['разгормат', 'разгерм', 'герметич', 'декомпресс', 'pressur', 'утечк воздух'],
    'engine_failure': ['отказ двиг', 'двигател', 'engine', 'помпаж', 'не тянет'],
    'engine_oil': ['масл', 'oil'],
    'engine_overheat': ['перегрев', 'температур', 'overheat'],
    'engine_vibration': ['вибрац', 'тряс', 'vibrat'],
    'engine_start': ['запуск', 'start', 'не заводится'],
    'fuel_leak': ['утечк топлив', 'течь топлив', 'fuel leak'],
    'hydraulic_leak': ['утечк гидравлик', 'hydraul leak', 'гидравлик'],
    'electrical_short': ['замыкан', 'short', 'кз', 'искр'],
    'landing_gear': ['шасси', 'стойк', 'gear'],
    'brake_system': ['тормоз', 'brake'],
    'avionics_nav': ['навигац', 'курс', 'nav'],
    'avionics_radio': ['связ', 'радио', 'radio'],
    'avionics_autopilot': ['автопилот', 'autopilot'],
    'avionics_transponder': ['транспондер', 'ответчик'],
    'air_condition': ['кондиционер', 'климат', 'air cond'],
    'fire_system': ['пожар', 'fire', 'дым'],
    'door_issue': ['двер', 'door', 'люк'],
    'oxygen_system': ['кислород', 'oxygen'],
    'electrical_lighting': ['освещ', 'свет', 'фар'],
}


def normalize_fault_type(raw):
    if not raw:
        return 'other'
    raw_lower = str(raw).lower().strip()
    if raw_lower in FAULT_SKILL:
        return raw_lower
    for code, synonyms in FAULT_SYNONYMS.items():
        if any(syn in raw_lower for syn in synonyms):
            return code
    return 'other'


def get_free_engineer_for(conn, fault_type, required_skill_map,
                          target_lat=None, target_lng=None, exclude_ids=None,
                          required_skill_override=None):
    if exclude_ids is None:
        exclude_ids = set()
    exclude_ids = {str(x) for x in exclude_ids}
    required_skill = required_skill_override or required_skill_map.get(fault_type, 'general')

    cur = conn.cursor()
    cur.execute("""SELECT e.id, e.name, e.device_id, e.specialization,
                          COALESCE(gps.lat, p.lat, e.coords_lat) AS coords_lat,
                          COALESCE(gps.lng, p.lng, e.coords_lng) AS coords_lng
                   FROM engineers e
                   LEFT JOIN map_object_positions p
                     ON p.object_type='engineer' AND p.object_id=e.id
                   LEFT JOIN map_object_positions gps
                     ON gps.object_type='engineer_gps' AND gps.object_id=e.id
                   WHERE e.status='free'""")
    all_free = [dict(r) for r in cur.fetchall()]
    assignments = load_airport_assignments()
    selected_airport = current_airport_payload()['airport']
    all_free = [e for e in all_free
                if assigned_airport('engineers', e['id'], assignments) == selected_airport]

    cur.execute("""SELECT DISTINCT engineer_id FROM requests
                   WHERE routing_stage='technician'
                     AND status IN ('assigned','arriving','in_progress','working')
                     AND engineer_id IS NOT NULL""")
    busy_with_requests = {str(r['engineer_id']) for r in cur.fetchall()}

    all_free = [e for e in all_free
                if str(e['id']) not in exclude_ids
                and str(e['id']) not in busy_with_requests]
    pool = [e for e in all_free if e['specialization'] == required_skill]

    if not pool:
        return None, None
    if target_lat is None or target_lng is None:
        return pool[0], {'travel_time': 0, 'walk_time': 0, 'using_car': False,
                         'best_car': None, 'best_car_time': 0, 'walk_to_car': 0,
                         'path_coords': None}

    cur.execute("SELECT id, device_id, lat, lng, speed FROM transport WHERE assigned_to IS NULL")
    cars = [] if selected_airport == 'vdnh' else filter_objects_by_airport('transport', cur.fetchall(), selected_airport)

    feasible = []
    for e in pool:
        if e['coords_lat'] is None or e['coords_lng'] is None:
            continue
        e_lat = float(e['coords_lat'])
        e_lng = float(e['coords_lng'])
        walk_path, walk_dist, walk_time = get_airport_road_path(
            selected_airport, e_lat, e_lng, target_lat, target_lng
        )
        if not walk_path:
            walk_dist = distance_km(e_lat, e_lng, target_lat, target_lng)
            walk_time = walk_dist / WALK_SPEED_KMH * 60
            walk_path = [[e_lat, e_lng], [target_lat, target_lng]]
        tool_delay = TOOL_PICKUP_DELAY_MIN if get_engineer_tool_status(e['id'])[0] not in ('on_hand', 'at_site', 'none') else 0.0
        walk_time += tool_delay
        best_car = None
        best_time = walk_time
        best_car_time = 0
        best_walk_to_car = 0
        best_combined_path = walk_path

        for car in cars:
            car_lat = float(car.get('lat') or 0)
            car_lng = float(car.get('lng') or 0)
            if not car_lat or not car_lng:
                continue
            seg1_path, seg1_dist, _seg1_time = get_airport_road_path(
                selected_airport, e_lat, e_lng, car_lat, car_lng
            )
            if not seg1_path:
                seg1_dist = distance_km(e_lat, e_lng, car_lat, car_lng)
                seg1_path = [[e_lat, e_lng], [car_lat, car_lng]]
            else:
                seg1_path = [list(p) for p in seg1_path]
                seg1_path[0] = [e_lat, e_lng]
                seg1_path[-1] = [car_lat, car_lng]
            wtc = seg1_dist / WALK_SPEED_KMH * 60
            if selected_airport == 'vdnh':
                result = get_airport_transport_path('vdnh', car_lat, car_lng, target_lat, target_lng)
                if not result:
                    continue
                seg2_path, seg2_dist, road_time, _legs = result
            else:
                seg2_path, seg2_dist, road_time = get_airport_road_path(
                    selected_airport, car_lat, car_lng, target_lat, target_lng
                )
                if not seg2_path:
                    seg2_dist = distance_km(car_lat, car_lng, target_lat, target_lng)
                    road_time = seg2_dist / max(float(car.get('speed') or 30), 1.0) * 60
                    seg2_path = [[car_lat, car_lng], [target_lat, target_lng]]
                road_time = max(float(road_time or 0),
                                seg2_dist / max(float(car.get('speed') or 30), 1.0) * 60)
            seg2_path = list(seg2_path or [])
            if len(seg2_path) < 2:
                seg2_path = [[car_lat, car_lng], [target_lat, target_lng]]
            seg2_path[0] = [car_lat, car_lng]
            seg2_path[-1] = [target_lat, target_lng]
            dt = float(road_time or 0) + TRACK_BOARD_DELAY_MIN + tool_delay
            t = wtc + dt
            if t < best_time:
                best_time = t
                best_car = car
                best_car_time = dt
                best_walk_to_car = wtc
                best_combined_path = seg1_path + seg2_path[1:]

        feasible.append({
            'engineer': e,
            'travel_time': round(best_time, 1),
            'walk_time': round(walk_time, 1),
            'using_car': best_car is not None, 'best_car': best_car,
            'best_car_time_exact': best_car_time,
            'walk_to_car_exact': best_walk_to_car,
            'best_car_time': round(best_car_time, 1),
            'walk_to_car': round(best_walk_to_car, 1),
            'path_coords': best_combined_path,
        })

    if not feasible:
        return None, None
    feasible.sort(key=lambda x: x['travel_time'])
    return feasible[0]['engineer'], feasible[0]


def try_reassign_queued():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT engineer_id FROM requests
                   WHERE routing_stage='technician'
                     AND status IN ('assigned','arriving','in_progress','working')
                     AND engineer_id IS NOT NULL""")
    busy = {str(r['engineer_id']) for r in cur.fetchall()}

    cur.execute("""SELECT id, aircraft, fault_type, parking_id, source, rejected_by
                   FROM requests
                   WHERE status='queued'
                     AND (delay_started_at IS NOT NULL OR all_rejected=1 OR delay_reason LIKE 'Ожидание%')
                   ORDER BY created_at ASC""")
    queued = [dict(r) for r in cur.fetchall()]
    if not queued:
        conn.close()
        return

    assigned = []
    for q in queued:
        rejected = {x.strip() for x in str(q['rejected_by']).split(',') if x.strip()}
        exclude = rejected | busy
        target = _request_target_coords(q['parking_id'], q['source']) or AIRPORT_CENTERS.get(current_airport_payload()['airport'], (55.975, 37.410))
        eng, route = get_free_engineer_for(conn, q['fault_type'] or 'engine_failure',
                                            FAULT_SKILL, target[0], target[1],
                                            exclude_ids=exclude)
        if not eng:
            continue
        cur.execute("""UPDATE requests SET engineer_id=?, engineer_name=?,
            travel_time=0, using_car=0, walk_to_car=0, car_time=0, best_car=NULL,
            engineer_comment='', status='assigned', routing_stage='technician',
            inspection_assigned_at=NULL,
            delay_minutes=0, delay_reason='', delay_started_at=NULL,
            delay_approved_by='', all_rejected=0
            WHERE id=?""",
            (eng['id'], eng['name'], q['id']))
        cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                    ('ВС ' + (q['aircraft'] or ''), eng['id']))
        busy.add(eng['id'])
        assigned.append(q['id'])
    conn.commit()
    conn.close()

    # Для автоматически назначенных заявок сразу готовим маршрут-превью.
    for assigned_rid in assigned:
        _log_event('assignment', 'Система', assigned_rid, {
            'mode': 'auto_from_queue', 'reason': 'Освободился подходящий инженер',
        })
        _log_event('status_change', 'Система', assigned_rid, {
            'from': 'queued', 'to': 'assigned', 'reason': 'Автоназначение из очереди',
        })
        _recalc_route_for_request_id(assigned_rid, force=True)

# ============================================================
# Пересчёт ROUTE
# ============================================================
def _engineer_route_start(conn, engineer_id):
    """Живой GPS мобильного инженера приоритетнее координат симуляции."""
    cur = conn.cursor()
    cur.execute("""SELECT COALESCE(gps.lat, p.lat, e.coords_lat) AS lat,
                          COALESCE(gps.lng, p.lng, e.coords_lng) AS lng
                   FROM engineers e
                   LEFT JOIN map_object_positions p
                     ON p.object_type='engineer' AND p.object_id=e.id
                   LEFT JOIN map_object_positions gps
                     ON gps.object_type='engineer_gps' AND gps.object_id=e.id
                   WHERE e.id=?""", (str(engineer_id),))
    row = cur.fetchone()
    if row and row['lat'] is not None and row['lng'] is not None:
        return float(row['lat']), float(row['lng'])
    return None


_VDNH_WAREHOUSE_CACHE = None


def _vdnh_warehouses():
    """Warehouses from the open VDNKh objects layer (Склад №1/№2)."""
    global _VDNH_WAREHOUSE_CACHE
    if _VDNH_WAREHOUSE_CACHE is not None:
        return _VDNH_WAREHOUSE_CACHE
    warehouses = []
    try:
        with open(os.path.join(STATIC_DIR, 'vdnh', 'facilities.json'), 'r', encoding='utf-8') as f:
            facilities = json.load(f)
        for facility in facilities if isinstance(facilities, list) else []:
            if str(facility.get('kind') or '').lower() == 'warehouse':
                warehouses.append({
                    'name': facility.get('name') or 'Склад',
                    'lat': float(facility['lat']),
                    'lng': float(facility['lon']),
                })
        if warehouses:
            _VDNH_WAREHOUSE_CACHE = warehouses
            return warehouses
    except (OSError, ValueError, TypeError, KeyError):
        pass
    path = os.path.join(STATIC_DIR, 'vdnh', 'objects.geojson')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            features = json.load(f).get('features') or []
        for feature in features:
            props = feature.get('properties') or {}
            name = str(props.get('name') or props.get('NAME') or '').strip()
            if 'склад' not in name.lower():
                continue
            geom = feature.get('geometry') or {}
            coords = geom.get('coordinates') or []
            polygons = coords if geom.get('type') == 'MultiPolygon' else [coords]
            points = []
            for polygon in polygons:
                if not polygon:
                    continue
                for point in (polygon[0] or []):
                    if isinstance(point, (list, tuple)) and len(point) >= 2:
                        points.append((float(point[1]), float(point[0])))
            if points:
                warehouses.append({
                    'name': name,
                    'lat': sum(p[0] for p in points) / len(points),
                    'lng': sum(p[1] for p in points) / len(points),
                })
    except (OSError, ValueError, TypeError) as exc:
        print(f'[VDNH WAREHOUSE] {exc}')
    _VDNH_WAREHOUSE_CACHE = warehouses
    return warehouses


def _warehouse_route(airport, start_lat, start_lng, end_lat, end_lng):
    """Shortest allowed-road route start → nearest warehouse → destination."""
    if str(airport or '').lower() != 'vdnh':
        return None
    best = None
    warehouses = sorted(
        _vdnh_warehouses(),
        key=lambda w: distance_km(start_lat, start_lng, w['lat'], w['lng'])
    )
    for warehouse in warehouses:
        p1, d1, _ = get_airport_road_path(airport, start_lat, start_lng,
                                          warehouse['lat'], warehouse['lng'])
        p2, d2, _ = get_airport_road_path(airport, warehouse['lat'], warehouse['lng'],
                                          end_lat, end_lng)
        if not p1 or not p2:
            continue
        combined = _dedupe_route_points(list(p1) + list(p2)[1:])
        total = float(d1 or 0) + float(d2 or 0)
        # First reachable warehouse in nearest-distance order.
        best = {
            'path': combined,
            'distance': total,
            'warehouse': warehouse,
        }
        break
    return best


def _needs_warehouse_visit(engineer_id, request_row):
    if not _request_needs_tools(request_row):
        return False
    status, _item = get_engineer_tool_status(engineer_id)
    return status not in ('on_hand', 'at_site', 'none')


def _recompute_route_for_request(request_row):
    eng_id = request_row['engineer_id']
    parking = str(request_row['parking_id'] or '')
    if not eng_id or not parking:
        return None

    target = _request_target_coords(parking, request_row['source'] if 'source' in request_row.keys() else None)
    if target is None:
        return None

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM engineers WHERE id=?", (eng_id,))
    if not cur.fetchone():
        conn.close()
        return None

    # Живой GPS мобильного инженера (engineer_gps) важнее координат
    # симуляции: на ВДНХ именно он находится внутри территории.
    start = _engineer_route_start(conn, eng_id)
    if start is None:
        conn.close()
        return None
    e_lat, e_lng = start

    engineer_airport = assigned_airport('engineers', eng_id)
    is_vdnh_route = engineer_airport == 'vdnh'
    walk_path, walk_dist, walk_time = get_airport_road_path(
        engineer_airport, e_lat, e_lng, target[0], target[1]
    )

    if walk_path is None:
        conn.close()
        return None
    else:
        walk_path = list(walk_path)
        if not walk_path:
            walk_path = [[e_lat, e_lng], [target[0], target[1]]]
        else:
            walk_path[0] = [e_lat, e_lng]
            if len(walk_path) == 1:
                walk_path.append([target[0], target[1]])
            else:
                walk_path[-1] = [target[0], target[1]]

    warehouse_info = None
    if is_vdnh_route and _needs_warehouse_visit(eng_id, request_row):
        warehouse_result = _warehouse_route(engineer_airport, e_lat, e_lng,
                                            target[0], target[1])
        if warehouse_result:
            walk_path = warehouse_result['path']
            walk_dist = warehouse_result['distance']
            warehouse_info = warehouse_result['warehouse']

    # Инженер идёт ровно 5 км/ч. Получение инструмента со склада добавляет 30 с,
    # если инструмент ещё не у инженера/на точке.
    walk_travel_time = walk_dist / WALK_SPEED_KMH * 60.0
    tool_delay = _norm_tool_delay_min(eng_id, request_row)
    board_delay = TRACK_BOARD_DELAY_MIN
    walk_time = walk_travel_time + tool_delay

    assigned_car = None
    if not is_vdnh_route:
        cur.execute("SELECT id, device_id, lat, lng, speed FROM transport WHERE assigned_to=?",
                    (request_row['id'],))
        assigned_car = cur.fetchone()
        if assigned_car and not object_belongs_to_airport('transport', assigned_car['id']):
            assigned_car = None

    best_time = walk_time
    best_car = None
    best_car_time = 0.0
    best_walk_to_car = 0.0
    best_path = walk_path
    best_legs = []
    best_mode = 'walk'

    def _car_option(car):
        """Return option for a car or None. Times include boarding and tool delay."""
        try:
            car_lat = float(car['lat'])
            car_lng = float(car['lng'])
        except (TypeError, ValueError):
            return None
        to_car_path, to_car_dist, _to_car_time = get_airport_road_path(
            engineer_airport, e_lat, e_lng, car_lat, car_lng
        )
        if not to_car_path:
            to_car_path = [[e_lat, e_lng], [car_lat, car_lng]]
            to_car_dist = distance_km(e_lat, e_lng, car_lat, car_lng)
        to_car_path = [list(p) for p in to_car_path]
        to_car_path[0] = [e_lat, e_lng]
        to_car_path[-1] = [car_lat, car_lng]
        wtc = to_car_dist / WALK_SPEED_KMH * 60.0
        if is_vdnh_route:
            result = get_airport_transport_path('vdnh', car_lat, car_lng, target[0], target[1])
            if not result:
                return None
            car_path, car_dist, road_time, legs = result
        else:
            car_path, car_dist, road_time = get_airport_road_path(
                engineer_airport, car_lat, car_lng, target[0], target[1]
            )
            if car_path is None:
                return None
            legs = []
            sp = float(car['speed'] or 30)
            # Speed limit of an edge is an upper bound even for a fast vehicle.
            road_time = max(float(road_time or 0), car_dist / max(sp, 1.0) * 60.0)
        car_path = list(car_path or [])
        if len(car_path) < 2:
            car_path = [[car_lat, car_lng], [target[0], target[1]]]
        else:
            car_path[0] = [car_lat, car_lng]
            car_path[-1] = [target[0], target[1]]
        car_time = float(road_time or 0) + board_delay + tool_delay
        total = wtc + car_time
        path = to_car_path + car_path[1:]
        return {
            'car': car, 'total': total, 'car_time': car_time,
            'walk_to_car': wtc, 'path': path, 'legs': legs,
            'board_delay': board_delay, 'tool_delay': tool_delay,
        }

    if assigned_car is not None:
        option = _car_option(assigned_car)
        if option and option['total'] <= walk_time:
            best_time = option['total']
            best_car = assigned_car
            best_car_time = option['car_time']
            best_walk_to_car = option['walk_to_car']
            best_path = option['path']
            best_legs = option['legs']
            best_mode = 'transport'
    elif not is_vdnh_route:
        cur.execute("SELECT id, device_id, lat, lng, speed FROM transport WHERE assigned_to IS NULL")
        cars = filter_objects_by_airport('transport', cur.fetchall(), engineer_airport)
        for car in cars:
            option = _car_option(car)
            if option and option['total'] < best_time:
                best_time = option['total']
                best_car = car
                best_car_time = option['car_time']
                best_walk_to_car = option['walk_to_car']
                best_path = option['path']
                best_legs = option['legs']
                best_mode = 'transport'

    conn.close()

    best_path = _dedupe_route_points(best_path)
    rounded = [[round(p[0], 6), round(p[1], 6)] for p in best_path]
    snap_start_m = 0.0
    snap_end_m = 0.0
    if len(best_path) >= 2:
        snap_start_m = distance_km(e_lat, e_lng, best_path[1][0], best_path[1][1]) * 1000.0
        snap_end_m = distance_km(best_path[-2][0], best_path[-2][1], target[0], target[1]) * 1000.0
    legs_json = []
    for leg in (best_legs or []):
        legs_json.append({
            'from': [round(leg['from'][0], 6), round(leg['from'][1], 6)],
            'to': [round(leg['to'][0], 6), round(leg['to'][1], 6)],
            'distance_km': leg.get('distance_km'),
            'speed_kmh': leg.get('speed_kmh'),
            'time_min': leg.get('time_min'),
            'mode': leg.get('mode'),
        })

    route_json = {
        'flat': round(e_lat, 6), 'flng': round(e_lng, 6),
        'tlat': round(target[0], 6), 'tlng': round(target[1], 6),
        'tt': round(best_time, 1),
        'uc': best_car is not None,
        'wc': round(best_walk_to_car, 1),
        'ct': round(best_car_time, 1),
        'wc_exact': round(best_walk_to_car, 3),
        'ct_exact': round(best_car_time, 3),
        'bclat': round(float(best_car['lat']), 6) if best_car else None,
        'bclng': round(float(best_car['lng']), 6) if best_car else None,
        'bc': best_car['device_id'] if best_car else None,
        'pid': parking,
        'mode': best_mode,
        'graph': 'vdnh_official_network' if is_vdnh_route else 'osm',
        'snap_start_m': round(snap_start_m, 1),
        'snap_end_m': round(snap_end_m, 1),
        'walk_travel_min': round(walk_travel_time, 1),
        'tool_delay_min': round(tool_delay, 2),
        'via_label': warehouse_info['name'] if warehouse_info else '',
        'warehouse': ({'name': warehouse_info['name'],
                       'lat': round(warehouse_info['lat'], 6),
                       'lng': round(warehouse_info['lng'], 6)}
                      if warehouse_info else None),
        'board_delay_min': round(board_delay, 2) if best_car else 0,
        'legs': legs_json,
        'destination_label': (('Сервисная зона ' + parking)
                              if is_vdnh_route and _vdnh_service_zone_center(parking) is not None
                              else ('Стоянка ' + parking)),
        'path': rounded,
    }
    fields = {
        'travel_time': round(best_time, 1),
        'using_car': 1 if best_car else 0,
        'walk_to_car': round(best_walk_to_car, 1),
        'car_time': round(best_car_time, 1),
        'best_car': best_car['device_id'] if best_car else None,
    }
    return json.dumps(route_json, ensure_ascii=False), fields


def _strip_route_payload(comment):
    """Удаляет только служебный ROUTE JSON, сохраняя текст отчёта инженера."""
    comment = comment or ''
    if not comment.startswith('ROUTE:'):
        return comment
    rest = comment[6:]
    depth = 0
    end = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(rest):
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return rest.strip()
    return rest[end:].strip()


def _clear_route_for_request(cur, request_id, comment):
    """Стирает маршрут у заявки, которая больше не находится в активной работе."""
    cleaned = _strip_route_payload(comment)
    cur.execute("""UPDATE requests SET engineer_comment=?, using_car=0,
                   walk_to_car=0, car_time=0, best_car=NULL WHERE id=?""",
                (cleaned, request_id))


def _cleanup_inactive_route_payloads(cur):
    """Удаляет ROUTE JSON у всех заявок, которые не принадлежат работающим инженерам."""
    cur.execute("""SELECT r.id, r.engineer_comment FROM requests r
                   LEFT JOIN engineers e ON e.id=r.engineer_id
                   WHERE r.engineer_comment LIKE 'ROUTE:%'
                     AND NOT (
                       r.routing_stage='technician'
                       AND r.status NOT IN ('completed','cancelled','flight_cancelled','draft','report_submitted','deleted')
                       AND e.status='busy'
                       AND e.task_step IN ('pending','accepted','in_car','arrived','diagnosing')
                       AND e.coords_lat IS NOT NULL AND e.coords_lng IS NOT NULL
                       AND r.parking_id IS NOT NULL AND r.parking_id != ''
                     )""")
    stale = cur.fetchall()
    for row in stale:
        _clear_route_for_request(cur, row['id'], row['engineer_comment'])
    return len(stale)


def _recalc_route_for_request_id(request_id, force=False):
    if not request_id:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.id, r.engineer_id, r.parking_id, r.source, r.engineer_comment, r.status,
                          r.fault_type, r.description, r.criticality,
                          r.routing_stage, e.task_step AS engineer_task_step, e.status AS engineer_status,
                          COALESCE(gps.lat, p.lat, e.coords_lat) AS engineer_lat,
                          COALESCE(gps.lng, p.lng, e.coords_lng) AS engineer_lng
                   FROM requests r
                   LEFT JOIN engineers e ON e.id=r.engineer_id
                   LEFT JOIN map_object_positions p
                     ON p.object_type='engineer' AND p.object_id=e.id
                   LEFT JOIN map_object_positions gps
                     ON gps.object_type='engineer_gps' AND gps.object_id=e.id
                   WHERE r.id=?""", (request_id,))
    req = cur.fetchone()
    if not req:
        conn.close()
        return
    # Маршрут строится сразу после назначения инженера (task_step=pending); принятие задачи не требуется.
    route_terminal_statuses = ('completed', 'cancelled', 'flight_cancelled', 'draft', 'report_submitted')
    route_work_steps = ('pending', 'accepted', 'in_car', 'arrived', 'diagnosing')
    is_working = (req['status'] not in route_terminal_statuses and
                  req['routing_stage'] == 'technician' and
                  req['engineer_id'] is not None and req['engineer_status'] == 'busy' and
                  req['engineer_task_step'] in route_work_steps and
                  req['engineer_lat'] is not None and req['engineer_lng'] is not None and
                  _request_target_coords(req['parking_id'], req['source']) is not None)
    if not is_working:
        _clear_route_for_request(cur, request_id, req['engineer_comment'])
        conn.commit()
        conn.close()
        return
    old_comment = req['engineer_comment'] or ''
    if not force and old_comment.startswith('ROUTE:') and req['status'] in ('assigned', 'arriving', 'in_progress', 'working'):
        conn.close()
        return

    result = _recompute_route_for_request(req)
    if not result:
        _clear_route_for_request(cur, request_id, req['engineer_comment'])
        conn.commit()
        conn.close()
        return
    new_route, fields = result

    old_comment = req['engineer_comment'] or ''
    report_part = ''
    if old_comment.startswith('ROUTE:'):
        rest = old_comment[6:]
        depth, end = 0, -1
        for i, c in enumerate(rest):
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            report_part = rest[end:].strip()

    new_comment = 'ROUTE:' + new_route
    if report_part:
        new_comment += '\n' + report_part

    cur.execute("""UPDATE requests SET
                   engineer_comment=?, travel_time=?, using_car=?,
                   walk_to_car=?, car_time=?, best_car=?
                   WHERE id=?""",
                (new_comment, fields['travel_time'], fields['using_car'],
                 fields['walk_to_car'], fields['car_time'], fields['best_car'],
                 request_id))
    conn.commit()
    conn.close()
    try:
        route_data = json.loads(new_route)
        _log_event('route', 'Система', request_id, {
            'travel_time': fields['travel_time'],
            'using_car': bool(fields['using_car']),
            'best_car': fields.get('best_car'),
            'path_points': len(route_data.get('path') or []),
        })
    except Exception:
        pass
    print(f'[SYNC] ROUTE recalculated for {request_id}')


def _recalc_routes_for_engineer(eng_id, force=False):
    if not eng_id:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id FROM requests
                   WHERE engineer_id=? AND routing_stage='technician' AND status NOT IN
                   ('completed','cancelled','flight_cancelled','draft','report_submitted','deleted')""", (eng_id,))
    ids = [r['id'] for r in cur.fetchall()]
    conn.close()
    for rid in ids:
        _recalc_route_for_request_id(rid, force=force)
        
def _recalc_all_active_routes():
    """Пересчитывает маршруты только инженеров с принятым активным заданием.
    У остальных заявок удаляет сохранённый ROUTE JSON, не затрагивая отчёт."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.id FROM requests r
                   JOIN engineers e ON e.id=r.engineer_id
                   WHERE r.status NOT IN ('completed','cancelled','flight_cancelled','draft','report_submitted','deleted')
                     AND r.routing_stage='technician'
                     AND e.task_step IN ('pending','accepted','in_car','arrived','diagnosing')
                     AND e.status='busy'
                     AND e.coords_lat IS NOT NULL AND e.coords_lng IS NOT NULL
                     AND r.engineer_id IS NOT NULL
                     AND r.parking_id IS NOT NULL AND r.parking_id != ''""")
    active_ids = [r['id'] for r in cur.fetchall()]
    _cleanup_inactive_route_payloads(cur)
    conn.commit()
    conn.close()
    for rid in active_ids:
        try:
            _recalc_route_for_request_id(rid, force=True)
        except Exception as e:
            print(f'[RECALC] {rid}: {e}')


def _archive_requests_before_delete(cur, where_sql, params=(), reason='Удалена'):
    """Сохраняет отметку об удалении в routing_history существующей строки requests.
    Физически запись не удаляется: это и есть долговременный архив без новых таблиц.
    """
    cur.execute('SELECT id, routing_history FROM requests WHERE ' + where_sql, params)
    rows = cur.fetchall()
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        try:
            history = json.loads(row['routing_history'] or '[]')
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
        history.append({'at': now, 'stage': 'deleted', 'actor': 'Система', 'note': reason})
        cur.execute("UPDATE requests SET routing_history=?, status='deleted', routing_stage='done' WHERE id=?",
                    (json.dumps(history, ensure_ascii=False), row['id']))
    return len(rows)


def _log_routing_event(conn, rid, stage, actor, note=''):
    cur = conn.cursor()
    cur.execute("SELECT routing_history FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        return
    try:
        history = json.loads(row['routing_history'] or '[]')
    except Exception:
        history = []
    history.append({
        'at': datetime.now(timezone.utc).isoformat(),
        'stage': stage,
        'actor': actor,
        'note': note,
    })
    cur.execute("UPDATE requests SET routing_history=? WHERE id=?",
                (json.dumps(history, ensure_ascii=False), rid))
    _log_event('routing', actor, rid, {'stage': stage, 'note': note})


# ============================================================
# Страницы
# ============================================================
@app.route('/')
def index():
    return redirect(url_for('login_page'))


@app.route('/login.html')
def login_page():
    return render_template('login.html')


@app.route('/admin/dashboard.html')
def admin_dashboard():
    return render_template('admin/dashboard.html')


@app.route('/dispatcher/dashboard.html')
def dispatcher_dashboard():
    return render_template('dispatcher/dashboard.html', initial_airport=current_airport_payload()['airport'])


@app.route('/engineer/dashboard.html')
def engineer_dashboard():
    return render_template('engineer/dashboard.html', initial_airport=current_airport_payload()['airport'])


@app.route('/engineer/map.html')
def engineer_live_map():
    # Компьютерная страница только отображает GPS-метку с телефона.
    # Получение геолокации и отправка координат здесь намеренно отсутствуют.
    return render_template('engineer/map.html', initial_airport=current_airport_payload()['airport'])


@app.route('/pilot/dashboard.html')
def pilot_dashboard():
    return render_template('pilot/dashboard.html')


# ============================================================
# Мобильное приложение, раздаваемое с сервера
# ============================================================
@app.route('/mobile')
@app.route('/mobile/')
def mobile_entry():
    return redirect('/mobile/login.html')


@app.route('/mobile/<path:filename>')
def mobile_files(filename):
    return send_from_directory(MOBILE_DIR, filename)


# Совместимость с мобильным клиентом, который открывает <base>/m/engineer
@app.route('/m/engineer')
@app.route('/m/engineer/')
def mobile_engineer_entry():
    return redirect('/m/engineer/login.html')


@app.route('/m/engineer/<path:filename>')
def mobile_engineer_files(filename):
    return send_from_directory(MOBILE_DIR, filename)


@app.route('/mcc/dashboard.html')
def mcc_dashboard():
    return render_template('mcc/dashboard.html')


@app.route('/cpu/dashboard.html')
def cpu_dashboard():
    return render_template('cpu/dashboard.html')


# ============================================================
# Стоянки и объекты — API
# ============================================================
@app.route('/api/health', methods=['GET'])
def api_health():
    return jsonify({
        'success': True,
        'service': 'InTrAvia',
        'api_version': 'gps-heading-1',
        'gps_location_endpoint': True,
        'server_time': datetime.now().isoformat(timespec='seconds')
    })


@app.route('/api/parking', methods=['GET'])
def api_parking():
    def sort_key(k):
        m = re.match(r'^(\d+)', str(k))
        return (int(m.group(1)) if m else 9999, str(k))
    if current_airport_payload().get('airport') == 'vdnh':
        zones = []
        try:
            with open(os.path.join(STATIC_DIR, 'vdnh', 'service_zones.geojson'), 'r', encoding='utf-8') as f:
                features = json.load(f).get('features') or []
            for feature in features:
                props = feature.get('properties') or {}
                zone_id = props.get('id')
                center = _vdnh_service_zone_center(zone_id)
                if zone_id is None or center is None:
                    continue
                zones.append({
                    'ref': str(zone_id),
                    'name': 'Сервисная зона ' + str(zone_id),
                    'lat': center[0], 'lng': center[1],
                    'terminal': 'ВДНХ',
                })
        except (OSError, ValueError):
            zones = []
        zones.sort(key=lambda z: sort_key(z['ref']))
        return jsonify(zones)
    result = [PARKING_META[k] for k in sorted(PARKING_META.keys(), key=sort_key)]
    return jsonify(result)


@app.route('/api/map-objects', methods=['GET'])
def api_map_objects():
    return jsonify(MAP_OBJECTS)


@app.route('/api/aircraft/taxi-reservations', methods=['POST'])
def api_aircraft_taxi_reservations():
    """Обновляет временные зоны занятости рулёжных маршрутов самолётами."""
    data = request.get_json(silent=True) or {}
    items = data.get('aircraft') or []
    if not isinstance(items, list) or len(items) > 100:
        return jsonify({'success': False, 'error': 'Некорректный список самолётов'}), 400
    now = time.time()
    updated = {}
    for item in items:
        if not isinstance(item, dict) or not item.get('id'):
            continue
        route = item.get('route') or []
        clean_route = []
        if isinstance(route, list):
            for point in route[:5000]:
                try:
                    lat, lng = float(point[0]), float(point[1])
                    if -90 <= lat <= 90 and -180 <= lng <= 180:
                        clean_route.append([lat, lng])
                except (TypeError, ValueError, IndexError):
                    continue
        if clean_route:
            updated[str(item['id'])] = {
                'aircraft': str(item.get('aircraft') or 'самолёт')[:80],
                'route': clean_route,
                'updated_at': now,
            }
    with AIRCRAFT_TAXI_LOCK:
        AIRCRAFT_TAXI_RESERVATIONS.clear()
        AIRCRAFT_TAXI_RESERVATIONS.update(updated)
    return jsonify({'success': True, 'active': len(updated)})


@app.route('/api/map/object-positions', methods=['GET'])
def api_map_object_positions():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT object_type,object_id,lat,lng,updated_at
                   FROM map_object_positions
                   WHERE object_type != 'engineer_gps_heading'
                   ORDER BY object_type,object_id""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/restricted-zones', methods=['GET'])
def api_restricted_zones_legacy():
    return jsonify(MAP_OBJECTS)


@app.route('/api/map-debug', methods=['GET'])
def api_map_debug():
    polys = sum(1 for o in MAP_OBJECTS if not o.get('isLine'))
    lines = sum(1 for o in MAP_OBJECTS if o.get('isLine'))
    return jsonify({
        'parking_count': len(PARKING_COORDS),
        'object_count': len(MAP_OBJECTS),
        'objects_polygons': polys,
        'objects_lines': lines,
        'sample_parking': list(PARKING_META.values())[:3],
        'sample_objects': MAP_OBJECTS[:2],
    })


# ============================================================
# Авторизация
# ============================================================
def _mobile_sessions_connection():
    """Отдельное хранилище блокировок мобильных сессий; основная БД не меняется."""
    conn = sqlite3.connect(MOBILE_SESSIONS_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute("""CREATE TABLE IF NOT EXISTS mobile_engineer_sessions (
        user_id TEXT PRIMARY KEY,
        session_token TEXT NOT NULL,
        last_seen REAL NOT NULL
    )""")
    return conn


def _claim_mobile_engineer_session(user_id):
    token = uuid.uuid4().hex
    now = time.time()
    conn = _mobile_sessions_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT session_token, last_seen FROM mobile_engineer_sessions WHERE user_id=?',
            (str(user_id),)
        ).fetchone()
        if row and now - float(row['last_seen'] or 0) <= MOBILE_SESSION_TTL_SECONDS:
            conn.rollback()
            return None
        conn.execute("""INSERT INTO mobile_engineer_sessions(user_id, session_token, last_seen)
            VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            session_token=excluded.session_token, last_seen=excluded.last_seen""",
            (str(user_id), token, now))
        conn.commit()
        return token
    finally:
        conn.close()


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = (data.get('password') or '').strip()
    is_mobile = (str(data.get('client_type') or '').lower() == 'mobile' or
                 request.headers.get('X-InTrAvia-Client', '').lower() == 'mobile')
    if not email or not password:
        return jsonify({'error': 'Заполните все поля'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,email,name,role,avatar FROM users WHERE email=? AND password=?",
                (email, password))
    u = cur.fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'Неверный email или пароль'}), 401

    mobile_session_token = None
    if is_mobile and u['role'] == 'engineer':
        try:
            mobile_session_token = _claim_mobile_engineer_session(u['id'])
        except Exception as exc:
            conn.close()
            print(f'[AUTH] Не удалось проверить мобильную сессию инженера: {exc}')
            return jsonify({'success': False, 'error': 'Не удалось проверить активную сессию. Повторите попытку.'}), 503
        if not mobile_session_token:
            conn.close()
            return jsonify({
                'success': False,
                'error': 'Отказано в доступе: в данном аккаунте инженера уже выполнена мобильная сессия на другом устройстве.'
            }), 409

    cur.execute("UPDATE users SET status='online' WHERE id=?", (u['id'],))
    conn.commit()
    extra = {}
    if u['role'] == 'pilot':
        cur.execute("SELECT aircraft, rank FROM pilots WHERE id=?", (u['id'],))
        p = cur.fetchone()
        if p:
            extra['aircraft'] = p['aircraft'] or ''
            extra['rank'] = p['rank'] or 'pilot'
    conn.close()
    res = {'id': u['id'], 'email': u['email'], 'name': u['name'],
           'role': u['role'], 'avatar': u['avatar'] or '👤'}
    if mobile_session_token:
        res['mobile_session_token'] = mobile_session_token
    res.update(extra)
    return jsonify({'success': True, 'user': res})


@app.route('/api/auth/mobile-heartbeat', methods=['POST'])
def mobile_engineer_session_heartbeat():
    data = request.get_json(silent=True) or {}
    user_id = str(data.get('user_id') or '').strip()
    token = str(data.get('session_token') or '').strip()
    if not user_id or not token:
        return jsonify({'success': False, 'error': 'Нет данных мобильной сессии'}), 400
    conn = _mobile_sessions_connection()
    try:
        cur = conn.execute("UPDATE mobile_engineer_sessions SET last_seen=? WHERE user_id=? AND session_token=?",
                           (time.time(), user_id, token))
        conn.commit()
        if cur.rowcount != 1:
            return jsonify({'success': False, 'error': 'Мобильная сессия завершена или открыта на другом устройстве.'}), 401
        return jsonify({'success': True})
    finally:
        conn.close()


@app.route('/api/auth/mobile-logout', methods=['POST'])
def mobile_engineer_session_logout():
    data = request.get_json(silent=True) or {}
    user_id = str(data.get('user_id') or '').strip()
    token = str(data.get('session_token') or '').strip()
    if user_id and token:
        conn = _mobile_sessions_connection()
        try:
            conn.execute('DELETE FROM mobile_engineer_sessions WHERE user_id=? AND session_token=?',
                         (user_id, token))
            conn.commit()
        finally:
            conn.close()
    return jsonify({'success': True})


# ============================================================
# Текущий аэропорт
# ============================================================
@app.route('/api/system/current-airport', methods=['GET'])
def get_current_airport():
    return jsonify(current_airport_payload())


@app.route('/api/system/current-airport', methods=['POST'])
def set_current_airport():
    data = request.get_json(silent=True) or {}
    code = str(data.get('airport') or '').strip().lower()
    if code not in AIRPORTS:
        return jsonify({'success': False, 'error': 'Неизвестный аэропорт'}), 400

    settings = load_system_settings()
    settings['airport'] = code
    settings['confirmed_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    confirmed_by = str(data.get('confirmed_by') or '').strip()
    if confirmed_by:
        settings['confirmed_by'] = confirmed_by[:120]
    save_system_settings(settings)

    payload = current_airport_payload()
    payload['success'] = True
    return jsonify(payload)


# ============================================================
# Users
# ============================================================
@app.route('/api/users', methods=['GET'])
def get_users():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,email,name,role,avatar,phone,shift,status FROM users ORDER BY role,name")
    res = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(res)


@app.route('/api/users', methods=['POST'])
def create_user():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    role = data.get('role', 'dispatcher')
    if not name or not email:
        return jsonify({'error': 'Имя и email обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email=?", (email,))
    if cur.fetchone():
        conn.close()
        return jsonify({'error': 'Email уже существует'}), 400
    uid = role + '_' + str(int(time.time() * 1000))
    avatar = {'dispatcher': '👨‍✈️', 'mcc': '👨‍💻', 'cpu': '🎧'}.get(role, '👤')
    default_pwd = {'dispatcher': 'disp123', 'mcc': 'mcc123', 'cpu': 'cpu123'}.get(role, 'user123')
    password = data.get('password') or default_pwd
    cur.execute("""INSERT INTO users (id,email,password,name,role,avatar,phone,shift,status)
                   VALUES (?,?,?,?,?,?,?,?,'offline')""",
                (uid, email, password, name, role, avatar,
                 data.get('phone', ''), data.get('shift', 'day')))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'id': uid})


@app.route('/api/users/<user_id>', methods=['PUT'])
def update_user(user_id):
    data = request.get_json() or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, role FROM users WHERE id=?", (user_id,))
    u = cur.fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'Не найден'}), 404
    if data.get('password'):
        cur.execute("UPDATE users SET password=? WHERE id=?", (data.get('password'), user_id))
    cur.execute("""UPDATE users SET name=?, phone=?, shift=?, status=? WHERE id=?""",
                (data.get('name'), data.get('phone', ''), data.get('shift', 'day'),
                 data.get('status', 'offline'), user_id))
    if data.get('name'):
        if u['role'] == 'pilot':
            cur.execute("UPDATE pilots SET name=? WHERE id=?", (data['name'], user_id))
        elif u['role'] == 'engineer':
            cur.execute("UPDATE engineers SET name=? WHERE id=?", (data['name'], user_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/users/<user_id>', methods=['DELETE'])
def delete_user(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# Operator reference-data REST integration (no Kupol integration)
# ============================================================
def _mel_import_api_authorized():
    expected = os.environ.get('INTRAVIA_IMPORT_API_KEY', '').strip()
    supplied = (request.headers.get('X-API-Key') or '').strip()
    return bool(expected) and supplied and supplied == expected


def _stable_mel_catalog_id(item):
    raw = '|'.join(str(item.get(k) or '').strip() for k in
                   ('aircraft_type', 'configuration', 'ata', 'item_ref', 'revision'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


@app.route('/api/v1/import/reference-data', methods=['POST'])
def import_reference_data_v1():
    """Import aircraft profiles and approved operator MEL catalogue records via REST."""
    if not os.environ.get('INTRAVIA_IMPORT_API_KEY', '').strip():
        return jsonify({'success': False, 'error': 'REST import is disabled: configure INTRAVIA_IMPORT_API_KEY on the server'}), 503
    if not _mel_import_api_authorized():
        return jsonify({'success': False, 'error': 'Invalid or missing X-API-Key'}), 401

    data = request.get_json(silent=True) or {}
    aircraft_rows = data.get('aircraft') or data.get('aircraft_registry') or []
    mel_rows = data.get('mel_items') or data.get('mel_catalog') or []
    if not isinstance(aircraft_rows, list) or not isinstance(mel_rows, list) or not (aircraft_rows or mel_rows):
        return jsonify({'success': False, 'error': 'Expected aircraft[] and/or mel_items[] arrays'}), 400
    if len(aircraft_rows) > 5000 or len(mel_rows) > 10000:
        return jsonify({'success': False, 'error': 'Import batch exceeds limits'}), 413

    conn = get_db()
    cur = conn.cursor()
    imported_aircraft = 0
    imported_mel = 0
    try:
        for a in aircraft_rows:
            if not isinstance(a, dict):
                raise ValueError('Each aircraft record must be an object')
            registration = str(a.get('registration') or '').strip().upper()
            aircraft_type = str(a.get('aircraft_type') or '').strip()
            configuration = str(a.get('configuration') or '').strip()
            if not registration or not aircraft_type:
                raise ValueError('Aircraft record requires registration and aircraft_type')
            cur.execute("""INSERT INTO aircraft_registry(registration, aircraft_type, configuration, updated_at)
                VALUES(?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(registration) DO UPDATE SET aircraft_type=excluded.aircraft_type,
                    configuration=excluded.configuration, updated_at=CURRENT_TIMESTAMP""",
                (registration, aircraft_type, configuration))
            imported_aircraft += 1

        allowed_modes = ('CALENDAR_DAYS', 'FLIGHT_HOURS', 'FLIGHT_CYCLES', 'CONDITION')
        allowed_start_rules = ('DEFECT_REPORTED_AT', 'ENGINEER_ASSESSMENT_AT')
        for item in mel_rows:
            if not isinstance(item, dict):
                raise ValueError('Each mel_items record must be an object')
            aircraft_type = str(item.get('aircraft_type') or '').strip()
            configuration = str(item.get('configuration') or '').strip()
            ata = str(item.get('ata') or '').strip()
            item_ref = str(item.get('item_ref') or '').strip()
            revision = str(item.get('revision') or '').strip()
            category = str(item.get('category') or '').strip().upper()
            description = str(item.get('description') or '').strip()
            interval = str(item.get('rectification_interval') or '').strip()
            mode = str(item.get('deadline_mode') or '').strip().upper()
            restrictions = str(item.get('restrictions') or '').strip()
            if not all((aircraft_type, ata, item_ref, revision, description, interval, restrictions)):
                raise ValueError('MEL item requires aircraft_type, ATA, item_ref, revision, description, rectification_interval and restrictions')
            o_required = bool(item.get('o_required'))
            m_required = bool(item.get('m_required'))
            o_procedure = str(item.get('o_procedure') or '').strip()
            m_procedure = str(item.get('m_procedure') or '').strip()
            if o_required and not o_procedure:
                raise ValueError('MEL item with o_required=true must include the complete o_procedure text')
            if m_required and not m_procedure:
                raise ValueError('MEL item with m_required=true must include the complete m_procedure text')
            if category not in ('A', 'B', 'C', 'D'):
                raise ValueError('MEL category must be A, B, C or D')
            if mode not in allowed_modes:
                raise ValueError('deadline_mode is required and must be CALENDAR_DAYS, FLIGHT_HOURS, FLIGHT_CYCLES or CONDITION')
            value = str(item.get('deadline_value') or '').strip()
            start_rule = str(item.get('deadline_start_rule') or '').strip().upper()
            condition = str(item.get('deadline_condition') or '').strip()
            if mode == 'CALENDAR_DAYS':
                try:
                    days = int(value)
                except (TypeError, ValueError):
                    raise ValueError('CALENDAR_DAYS requires a positive integer deadline_value')
                if days < 1 or start_rule not in allowed_start_rules:
                    raise ValueError('CALENDAR_DAYS requires deadline_start_rule DEFECT_REPORTED_AT or ENGINEER_ASSESSMENT_AT')
            elif mode in ('FLIGHT_HOURS', 'FLIGHT_CYCLES'):
                try:
                    if float(value) <= 0: raise ValueError()
                except (TypeError, ValueError):
                    raise ValueError(mode + ' requires positive deadline_value')
            elif not condition:
                raise ValueError('CONDITION requires deadline_condition')
            row_id = str(item.get('id') or _stable_mel_catalog_id({
                'aircraft_type': aircraft_type, 'configuration': configuration,
                'ata': ata, 'item_ref': item_ref, 'revision': revision
            })).strip()
            cur.execute("""INSERT INTO mel_catalog(
                id,aircraft_type,configuration,ata,item_ref,revision,category,description,
                rectification_interval,deadline_mode,deadline_value,deadline_start_rule,
                deadline_condition,restrictions,o_required,o_procedure,m_required,m_procedure,
                applicability_codes,source_document,active,imported_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(aircraft_type,configuration,ata,item_ref,revision) DO UPDATE SET
                    id=excluded.id, category=excluded.category, description=excluded.description,
                    rectification_interval=excluded.rectification_interval,
                    deadline_mode=excluded.deadline_mode, deadline_value=excluded.deadline_value,
                    deadline_start_rule=excluded.deadline_start_rule,
                    deadline_condition=excluded.deadline_condition, restrictions=excluded.restrictions,
                    o_required=excluded.o_required, o_procedure=excluded.o_procedure,
                    m_required=excluded.m_required, m_procedure=excluded.m_procedure,
                    applicability_codes=excluded.applicability_codes,
                    source_document=excluded.source_document, active=excluded.active,
                    imported_at=CURRENT_TIMESTAMP""", (
                row_id, aircraft_type, configuration, ata, item_ref, revision, category,
                description, interval, mode, value, start_rule, condition, restrictions,
                1 if o_required else 0, o_procedure,
                1 if m_required else 0, m_procedure,
                str(item.get('applicability_codes') or '').strip(),
                str(item.get('source_document') or '').strip(),
                1 if item.get('active', True) else 0))
            imported_mel += 1
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': str(exc)}), 400
    conn.close()
    return jsonify({'success': True, 'imported_aircraft': imported_aircraft,
                    'imported_mel_items': imported_mel})


@app.route('/api/mel/catalog', methods=['GET'])
def get_engineer_mel_catalog():
    request_id = str(request.args.get('request_id') or '').strip()
    ata = str(request.args.get('ata') or '').strip()
    search = str(request.args.get('q') or '').strip()
    if not request_id:
        return jsonify({'success': False, 'error': 'request_id is required'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.aircraft, ar.aircraft_type, ar.configuration
        FROM requests r LEFT JOIN aircraft_registry ar ON ar.registration=UPPER(r.aircraft)
        WHERE r.id=?""", (request_id,))
    profile = cur.fetchone()
    if not profile:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    aircraft_type = str(profile['aircraft_type'] or '').strip()
    configuration = str(profile['configuration'] or '').strip()
    if not aircraft_type:
        conn.close()
        return jsonify({'success': False, 'error': 'Для борта не загружен профиль типа/конфигурации. Импортируйте aircraft[] через REST API; выбор MEL пока заблокирован.'}), 409
    sql = """SELECT id, aircraft_type, configuration, ata, item_ref, revision, category,
        description, rectification_interval, deadline_mode, deadline_value, deadline_start_rule,
        deadline_condition, restrictions, o_required, o_procedure, m_required, m_procedure,
        applicability_codes, source_document
        FROM mel_catalog WHERE active=1 AND aircraft_type=?
          AND (configuration='' OR configuration=?)
          AND (o_required=0 OR TRIM(COALESCE(o_procedure,''))<>'')
          AND (m_required=0 OR TRIM(COALESCE(m_procedure,''))<>'')"""
    params = [aircraft_type, configuration]
    if ata:
        sql += ' AND ata=?'; params.append(ata)
    if search:
        sql += ' AND (item_ref LIKE ? OR description LIKE ? OR applicability_codes LIKE ?)'
        q = '%' + search + '%'; params.extend([q, q, q])
    sql += ' ORDER BY ata, item_ref LIMIT 500'
    cur.execute(sql, params)
    rows = [dict(x) for x in cur.fetchall()]
    conn.close()
    return jsonify({'success': True, 'aircraft': profile['aircraft'],
                    'aircraft_type': aircraft_type, 'configuration': configuration,
                    'items': rows})


# ============================================================
# MCC
# ============================================================
def _compute_recurring_map(conn):
    cur = conn.cursor()
    cur.execute("""SELECT aircraft, fault_type, COUNT(*) AS cnt, MAX(created_at) AS last_seen
                   FROM requests
                   WHERE aircraft IS NOT NULL AND aircraft!='' AND fault_type IS NOT NULL
                     AND status NOT IN ('completed','cancelled','flight_cancelled','draft','deleted')
                     AND created_at >= datetime('now','-30 days')
                   GROUP BY aircraft, fault_type""")
    out = {}
    for r in cur.fetchall():
        out[(str(r['aircraft']).strip(), str(r['fault_type']).strip())] = {
            'occurrences': r['cnt'] or 0, 'last_occurrence': r['last_seen']
        }
    return out


def sync_expired_mel(conn):
    """Move MEL cases past their operator-entered calendar deadline to DO_NOT_FLY.

    This covers both an activated MEL and one still awaiting final activation. No category-based
    default interval is calculated: due dates are stored exactly as entered from the approved MEL.
    Category A may be condition-based and have no date, in which case the condition needs manual control.
    """
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, mcc_mel_ref, mcc_due_date FROM requests
        WHERE mcc_status IN ('MEL_ACTIVE','MEL_READY','MEL') AND COALESCE(TRIM(mcc_due_date),'')!=''
          AND date('now') > date(mcc_due_date)""")
    expired = [dict(r) for r in cur.fetchall()]
    for row in expired:
        cur.execute("""UPDATE requests SET mcc_status='MEL_EXPIRED', status='mel_expired',
            routing_stage='pilot_notice', flight_status='DO_NOT_FLY',
            flight_status_reason=?, pilot_notice_at=CURRENT_TIMESTAMP
            WHERE id=? AND mcc_status IN ('MEL_ACTIVE','MEL_READY','MEL')""", (
                'Срок MEL ' + str(row.get('mcc_mel_ref') or '') + ' истёк. Полёты запрещены до устранения дефекта или допустимого оформления продления.',
                row['id']))
        if cur.rowcount:
            _log_routing_event(conn, row['id'], 'pilot_notice', 'Система',
                'Срок MEL истёк (' + str(row.get('mcc_due_date') or '') + '); установлен DO_NOT_FLY')
    if expired:
        conn.commit()
    return len(expired)


@app.route('/api/defects', methods=['GET'])
def list_defects():
    """Open defect catalog: search by free text, ATA chapter and category."""
    q = str(request.args.get('q') or request.args.get('query') or '').strip()
    ata = str(request.args.get('ata') or '').strip()
    category = str(request.args.get('category') or '').strip()
    try:
        limit = min(100, max(1, int(request.args.get('limit') or 20)))
    except (TypeError, ValueError):
        limit = 20
    catalog = load_defect_catalog()
    items = search_defect_catalog(q, ata, category, limit) if (q or ata or category) else list(catalog.get('defects') or [])[:limit]
    return jsonify({
        'success': True,
        'query': q, 'ata': ata, 'category': category,
        'count': len(items),
        'defects': items,
        'categories': catalog.get('categories') or DEFECT_CATEGORIES,
        'ata_chapters': catalog.get('ata_chapters') or ATA_CHAPTERS,
    })


@app.route('/api/defects', methods=['POST'])
def create_defect():
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or data.get('description') or '').strip()
    ata = str(data.get('ata') or '').strip()
    category = str(data.get('category') or '').strip()
    synonyms = data.get('synonyms') or []
    if isinstance(synonyms, str):
        synonyms = [s.strip() for s in synonyms.split(',') if s.strip()]
    try:
        item, created = add_defect_to_catalog(text, ata, category, data.get('skill'), synonyms)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True, 'created': created, 'defect': item,
                    'classification': classify_defect(text, item.get('ata'), item.get('category'))})


@app.route('/api/defects/classify', methods=['POST'])
def classify_defect_endpoint():
    data = request.get_json(silent=True) or {}
    result = classify_defect(data.get('text') or data.get('description') or '',
                             data.get('ata') or '', data.get('category') or '')
    result['success'] = True
    return jsonify(result)


@app.route('/api/defects/ata', methods=['GET'])
def defect_ata_reference():
    catalog = load_defect_catalog()
    return jsonify({'success': True, 'categories': catalog.get('categories') or DEFECT_CATEGORIES,
                    'ata_chapters': catalog.get('ata_chapters') or ATA_CHAPTERS})


@app.route('/api/mcc/ata', methods=['POST'])
def mcc_set_ata():
    data = request.get_json() or {}
    rid = data.get('request_id')
    ata = str(data.get('ata') or '').strip()
    # Разрешаем только реальные главы ATA, которые представлены в интерфейсе MCC.
    valid_ata = {
        '00','01','02','03','04','05','06','07','08','09','10','11','12','20','21','22','23','24','25','26',
        '27','28','29','30','31','32','33','34','35','36','37','38','41','42','44','45','46','47','48','49','50',
        '51','52','53','54','55','56','57','60','61','62','63','64','65','66','67','70',
        '71','72','73','74','75','76','77','78','79','80','81','82','83','84'
    }
    if not rid:
        return jsonify({'success': False, 'error': 'request_id обязателен'}), 400
    if ata not in valid_ata:
        return jsonify({'success': False, 'error': 'Укажите действующую главу ATA из списка'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM requests WHERE id=?', (rid,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    cur.execute('UPDATE requests SET mcc_ata=? WHERE id=?', (ata, rid))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'ata': ata})


@app.route('/api/mcc/defects', methods=['GET'])
def get_mcc_defects():
    conn = get_db()
    sync_expired_mel(conn)
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, fault_type, description, parking_id, terminal,
        source, pilot_name, pilot_rank, pilot_audio_text, engineer_id, engineer_name,
        created_at, status, mcc_status, mcc_decision, mcc_comment, engineer_comment, mcc_ata,
        mcc_mel_choice, mcc_mel_ref, mcc_amm_verdict, mcc_proc_o, mcc_proc_m,
        mcc_proc_mh, mcc_mel_category, mcc_due_date, mcc_combined_check,
        mcc_mel_revision, mcc_mel_description, mcc_mel_restrictions, mcc_due_condition,
        mcc_proc_o_text, mcc_proc_m_text, cpu_mel_o_completed, cpu_mel_m_completed,
        mel_catalog_id, mel_assessed_by, mel_assessment_comment, mel_rectification_interval, mcc_mel_return_comment,
        mel_m_procedure_completed, mel_m_procedure_comment,
        mel_initial_engineer_report, mel_initial_engineer_report_at, mel_initial_assessed_by,
        mel_deadline_mode, mel_deadline_value, mel_deadline_start_rule,
        cpu_mel_applicability_confirmed, cpu_mel_o_comment, cpu_mel_m_comment,
        mcc_mel_reviewed_by, mcc_mel_reviewed_at, mcc_mel_review_comment,
        mcc_mel_combined_checked, mcc_mel_crew_briefed, mcc_mel_restrictions_verified, mcc_mel_repair_plan,
        cpu_mel_check_comment, cpu_mel_checked_at, cpu_mel_checked_by,
        cpu_mel_engineer_required, cpu_mel_m_comment,
        mcc_recurring_action, mcc_escalation, mcc_dce_name, mcc_kupol_ref,
        aog_since, etr, reported_by, severity, criticality,
        engineer_verdict, engineer_verdict_at, aog_action, inspection_assigned_at,
        flight_number, travel_time, using_car, best_car,
        crs_signature, crs_signed_at, crs_signed_by,
        routing_stage, routing_history, flight_status, flight_status_reason,
        engineer_condition, engineer_report_at, dispatcher_report_at, cpu_report_at, pilot_notice_at
        FROM requests WHERE status NOT IN ('cancelled','flight_cancelled','draft','deleted')
        ORDER BY created_at DESC""")
    rows = cur.fetchall()
    rec_map = _compute_recurring_map(conn)
    conn.close()
    out = []
    for r in rows:
        d = dict(r)

        # ── Подпись источника: пилот/голос → «КВС», остальное → «Инженер» ──
        src = (d.get('source') or '').strip().lower()
        if src in ('pilot', 'voice'):
            d['source_label'] = 'КВС'
        else:
            d['source_label'] = 'Инженер'

        d['reported_by'] = d.get('pilot_name') or d.get('engineer_name') or 'Pilot'
        d['mel_category'] = d.get('mcc_mel_category') or ''
        d['mel_ref'] = d.get('mcc_mel_ref') or ''
        d['mel_due_date'] = d.get('mcc_due_date') or ''
        d['engineer_verdict'] = d.get('engineer_verdict') or ''
        d['aog_action'] = d.get('aog_action') or ''
        d['crs_signed'] = bool(d.get('crs_signed_at'))
        try:
            d['routing_history'] = json.loads(d.get('routing_history') or '[]')
        except Exception:
            d['routing_history'] = []
        key = (str(d.get('aircraft') or '').strip(), str(d.get('fault_type') or '').strip())
        rec = rec_map.get(key, {'occurrences': 1, 'last_occurrence': d.get('created_at')})
        d['occurrences'] = rec['occurrences']
        d['recurring'] = rec['occurrences'] >= 3
        out.append(d)
    return jsonify(out)


@app.route('/api/mcc/start_mel_review', methods=['POST'])
def mcc_start_mel_review():
    # Deprecated by the engineer-led MEL workflow. Keep the route for old clients,
    # but refuse to let MCC manually set the item/category/deadline.
    return jsonify({'success': False,
                    'error': 'Пункт MEL, категорию и интервал определяет инженер по справочнику утверждённого MEL. MCC выполняет проверку и контроль.'}), 410


@app.route('/api/mcc/send_to_cpu', methods=['POST'])
def mcc_send_to_cpu():
    data = request.get_json() or {}
    rid = data.get('request_id')
    comment = (data.get('comment') or '').strip()

    if not rid:
        return jsonify({'success': False, 'error': 'request_id обязателен'}), 400
    if not comment:
        return jsonify({'success': False, 'error': 'Комментарий обязателен'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, aircraft, routing_stage, mcc_status FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    expired_mel_recovery = (row['routing_stage'] == 'pilot_notice' and row['mcc_status'] == 'MEL_EXPIRED')
    if row['routing_stage'] not in ('mcc', None, '') and not expired_mel_recovery:
        conn.close()
        return jsonify({'success': False,
                        'error': 'Заявка уже передана (stage=%s)' % row['routing_stage']}), 400

    event_note = ('Срок MEL истёк. MCC направил заявку в обычную ветку AOG/ремонта: ' if expired_mel_recovery else '') + comment
    log_prefix = '\n[→ЦПУ · MEL EXPIRED → AOG/TECH] ' if expired_mel_recovery else '\n[→ЦПУ · AOG/TECH] '
    cur.execute("""UPDATE requests SET
                   routing_stage='cpu', mcc_decision='AOG_DIAGNOSIS', mcc_mel_choice='',
                   mcc_comment=COALESCE(mcc_comment,'') || ?,
                   mcc_status='INSPECTION',
                   flight_status='DO_NOT_FLY',
                   flight_status_reason='Передано на техническую диагностику по ветке AOG/ремонт. До отдельного допуска MCC полёт запрещён.'
                   WHERE id=?""",
                (log_prefix + comment, rid))
    _log_routing_event(conn, rid, 'cpu', 'MCC', event_note)
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'cpu',
                    'expired_mel_recovery': expired_mel_recovery})


@app.route('/api/mcc/send_to_dispatcher', methods=['POST'])
def mcc_send_pilot_to_dispatcher():
    """MCC forwards a pilot-originated, parking-identified request directly to dispatch.

    This is a dedicated pilot workflow; other request sources continue to use the CPU path.
    """
    data = request.get_json(silent=True) or {}
    rid = str(data.get('request_id') or '').strip()
    comment = str(data.get('comment') or '').strip()
    actor = str(data.get('actor') or '').strip() or 'MCC'
    if not rid:
        return jsonify({'success': False, 'error': 'request_id обязателен'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, source, parking_id, routing_stage, mcc_status, status
                   FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if str(row['source'] or '').lower() != 'pilot':
        conn.close()
        return jsonify({'success': False, 'error': 'Прямой маршрут без ЦПУ доступен только для заявок от пилота'}), 403
    if row['routing_stage'] != 'mcc' or row['mcc_status'] != 'PENDING':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка уже обработана MCC или находится на другом этапе'}), 409
    if not str(row['parking_id'] or '').strip():
        conn.close()
        return jsonify({'success': False, 'error': 'В заявке пилота не указан ID стоянки'}), 400
    if row['status'] in ('cancelled', 'flight_cancelled', 'completed', 'deleted', 'draft'):
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка уже закрыта или отменена'}), 409

    note = comment or 'Первичная проверка MCC завершена. Заявка направлена диспетчеру напрямую, без этапа ЦПУ.'
    cur.execute("""UPDATE requests SET
                   routing_stage='dispatcher', status='queued',
                   mcc_status='DIRECT_TO_DISPATCHER', mcc_decision='DIRECT_TO_DISPATCHER',
                   mcc_comment=COALESCE(mcc_comment,'') || ?
                   WHERE id=? AND routing_stage='mcc' AND mcc_status='PENDING'""",
                ('\n[MCC→Диспетчер · без ЦПУ] ' + actor + ': ' + note, rid))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': 'Не удалось передать заявку: состояние уже изменилось'}), 409
    _log_routing_event(conn, rid, 'dispatcher', actor,
                       'Заявка пилота направлена напрямую диспетчеру без ЦПУ. ' + note)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'dispatcher',
                    'status': 'queued', 'bypassed_cpu': True})




@app.route('/api/mcc/assign_inspection', methods=['POST'])
def mcc_assign_inspection_legacy():
    return mcc_send_to_cpu()


@app.route('/api/engineer/reports', methods=['GET'])
def engineer_past_reports():
    """История отчётов конкретного инженера; удалённые заявки в неё не попадают."""
    engineer_id = str(request.args.get('engineer_id') or '').strip()
    if not engineer_id:
        return jsonify({'success': False, 'error': 'Не указан engineer_id'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, parking_id, severity,
                   engineer_verdict, engineer_condition, engineer_comment,
                   engineer_report_at, created_at, status, mcc_status, mcc_decision,
                   routing_stage, mcc_mel_ref, mcc_ata, mcc_mel_category,
                   mcc_mel_revision, mel_rectification_interval
                   FROM requests
                   WHERE engineer_id=? AND engineer_report_at IS NOT NULL
                     AND TRIM(COALESCE(engineer_comment,'')) != ''
                   ORDER BY engineer_report_at DESC, created_at DESC""", (engineer_id,))
    reports = [dict(row) for row in cur.fetchall()]
    conn.close()
    return jsonify({'success': True, 'reports': reports})


@app.route('/api/engineer/submit_report', methods=['POST'])
def engineer_submit_report():
    """Инженер завершает диагностику и сразу передаёт отчёт в ЦПУ, не выпуская ВС."""
    data = request.get_json() or {}
    rid = data.get('request_id')
    engineer_id = str(data.get('engineer_id') or '')
    condition = (data.get('condition') or '').strip()
    category = (data.get('category') or '').strip()
    findings = (data.get('findings') or '').strip()
    report_text = (data.get('report_text') or '').strip()
    mel_item_ref = str(data.get('mel_item_ref') or '').strip()[:80]
    mel_ata = str(data.get('mel_ata') or '').strip()[:20]
    mel_category = str(data.get('mel_category') or '').strip()[:20]
    mel_revision = str(data.get('mel_revision') or '').strip()[:80]
    mel_interval_limit = str(data.get('mel_interval_limit') or '').strip()[:500]
    mel_catalog_id = ''
    mel_assessment_comment = ''
    mel_m_procedure_completed = bool(data.get('mel_m_procedure_completed'))
    mel_m_procedure_comment = str(data.get('mel_m_procedure_comment') or '').strip()
    need_parts = bool(data.get('need_parts'))
    waiting_parts = bool(condition == 'aog' and need_parts)
    try:
        duration = int(data.get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0
    if not rid or condition not in ('aog', 'repaired', 'mel_deferred'):
        return jsonify({'success': False, 'error': 'Выберите один из трёх результатов диагностики: AOG, MEL или «Устранено»'}), 400
    if condition == 'mel_deferred' and not mel_item_ref:
        return jsonify({'success': False, 'error': 'Укажите номер пункта MEL, например 27-2-1'}), 400
    if condition == 'mel_deferred' and not (mel_ata and mel_category and mel_revision and mel_interval_limit):
        return jsonify({'success': False, 'error': 'Заполните ATA, категорию, редакцию MEL и интервал / лимит'}), 400
    if not findings or not report_text:
        return jsonify({'success': False, 'error': 'Отчёт и описание обнаруженного обязательны'}), 400
    if duration < 1 or duration > 480:
        return jsonify({'success': False, 'error': 'Время диагностики должно быть от 1 до 480 минут'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, created_at, engineer_id, engineer_name, routing_stage, status, engineer_comment,
        mcc_mel_choice, mel_catalog_id, mcc_proc_m, mcc_proc_o, mcc_proc_m_text, mcc_proc_o_text, cpu_mel_applicability_confirmed,
        cpu_mel_o_completed, cpu_mel_o_comment, mel_rectification_interval FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'technician' or row['status'] in ('completed','cancelled','flight_cancelled','deleted'):
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка уже не находится у инженера'}), 409
    if engineer_id and str(row['engineer_id'] or '') != engineer_id:
        conn.close()
        return jsonify({'success': False, 'error': 'Эта заявка назначена другому инженеру'}), 403

    # Инженер вручную указывает номер пункта MEL; операторский справочник не блокирует ввод.
    mel_values = {
        'mcc_mel_choice': '', 'mcc_ata': '', 'mcc_mel_ref': '', 'mcc_mel_category': '',
        'mcc_due_date': None, 'mcc_due_condition': '', 'mcc_mel_revision': '',
        'mcc_mel_description': '', 'mcc_mel_restrictions': '', 'mcc_proc_o': 0,
        'mcc_proc_m': 0, 'mcc_proc_o_text': '', 'mcc_proc_m_text': '',
        'mel_catalog_id': '', 'mel_assessed_by': '', 'mel_assessment_comment': '',
        'mel_rectification_interval': '', 'mel_deadline_mode': '', 'mel_deadline_value': '',
        'mel_deadline_start_rule': ''
    }
    preliminary_verdict = {'aog': 'AOG', 'mel_deferred': 'MEL', 'repaired': 'REPAIR'}[condition]
    is_mel_m_followup = bool(row['mcc_mel_choice'] == 'MEL' and row['mcc_proc_m'])
    if preliminary_verdict == 'MEL':
        if is_mel_m_followup and str(row['mcc_mel_ref'] or '').strip().casefold() != mel_item_ref.casefold():
            conn.close()
            return jsonify({'success': False, 'error': 'Для отдельной M-задачи нельзя менять исходный номер пункта MEL.'}), 409
        mel_m_maintenance_task = is_mel_m_followup
        if mel_m_maintenance_task and (not mel_m_procedure_completed or len(mel_m_procedure_comment) < 5):
            conn.close()
            return jsonify({'success': False, 'error': 'Для этой задачи обязательна M-процедура. Подтвердите выполнение и опишите работы.'}), 400
        if not mel_m_maintenance_task:
            mel_m_procedure_completed = False
            mel_m_procedure_comment = ''
        mel_values.update({
            'mcc_mel_choice': 'MEL', 'mcc_mel_ref': mel_item_ref,
            'mcc_ata': mel_ata, 'mcc_mel_category': mel_category,
            'mcc_mel_revision': mel_revision,
            'mel_rectification_interval': (str(row['mel_rectification_interval'] or '').strip()[:500] if mel_m_maintenance_task else mel_interval_limit),
            'mcc_proc_m': 1 if mel_m_maintenance_task else 0,
            'mcc_proc_m_text': str(row['mcc_proc_m_text'] or '') if mel_m_maintenance_task else '',
            'mcc_proc_o': int(row['mcc_proc_o'] or 0) if mel_m_maintenance_task else 0,
            'mcc_proc_o_text': str(row['mcc_proc_o_text'] or '') if mel_m_maintenance_task else '',
            'mel_assessed_by': row['engineer_name'] or engineer_id,
            'mel_m_procedure_completed': 1 if mel_m_procedure_completed else 0,
            'mel_m_procedure_comment': mel_m_procedure_comment
        })
    if preliminary_verdict != 'MEL':
        mel_m_procedure_completed = False
        mel_m_procedure_comment = ''

    preserve_cpu_mel_o = bool(preliminary_verdict == 'MEL' and row['mcc_mel_choice'] == 'MEL' and
        str(row['mcc_mel_ref'] or '').strip().casefold() == mel_item_ref.casefold() and row['cpu_mel_applicability_confirmed'])

    flight_reason = (
        ('AOG установлен инженером: полёт запрещён. Ожидается запчасть — инженер возвращён в пул.'
         if waiting_parts else
         'AOG установлен инженером: полёт запрещён. Отчёт передан в ЦПУ.') if preliminary_verdict == 'AOG' else
        ('Инженер сопоставил дефект с пунктом MEL ' + str(mel_values['mcc_mel_ref']) +
         '. Условная эксплуатация не активна; ожидаются проверка ЦПУ и решение MCC.' if preliminary_verdict == 'MEL' else
         'Инженер завершил диагностику. Отчёт передан в ЦПУ; полёт запрещён до решения MCC и отдельного допуска.')
    )
    # При передаче отчёта в ЦПУ маршрут инженера удаляется; в комментарии остаётся только отчёт.
    cur.execute("""UPDATE requests SET
        status='report_submitted', routing_stage='cpu_report',
        engineer_condition=?, engineer_verdict=?, engineer_verdict_at=CURRENT_TIMESTAMP,
        engineer_report_at=CURRENT_TIMESTAMP, cpu_report_at=CURRENT_TIMESTAMP,
        engineer_comment=?,
        mcc_mel_choice=?, mcc_ata=?, mcc_mel_ref=?, mcc_mel_category=?, mcc_due_date=?,
        mcc_due_condition=?, mcc_mel_revision=?, mcc_mel_description=?, mcc_mel_restrictions=?,
        mcc_proc_o=?, mcc_proc_m=?, mcc_proc_o_text=?, mcc_proc_m_text=?,
        mel_catalog_id=?, mel_assessed_by=?, mel_assessment_comment=?, mel_rectification_interval=?,
        mel_m_procedure_completed=?, mel_m_procedure_comment=?,
        mel_deadline_mode=?, mel_deadline_value=?, mel_deadline_start_rule=?,
        mcc_status=CASE WHEN ?='MEL' THEN 'MEL_PENDING_CPU' ELSE 'INSPECTION' END,
        cpu_mel_o_completed=CASE WHEN ?=1 THEN cpu_mel_o_completed ELSE 0 END,
        cpu_mel_m_completed=0,
        cpu_mel_applicability_confirmed=CASE WHEN ?=1 THEN cpu_mel_applicability_confirmed ELSE 0 END,
        cpu_mel_checked_at=NULL, cpu_mel_checked_by='', cpu_mel_check_comment='',
        cpu_mel_o_comment=CASE WHEN ?=1 THEN cpu_mel_o_comment ELSE '' END, cpu_mel_m_comment='',
        flight_status='DO_NOT_FLY', flight_status_reason=?,
        aog_action=CASE WHEN ?=1 THEN 'WAITING_PARTS' WHEN ?='AOG' THEN 'AOG' ELSE aog_action END,
        aog_since=CASE WHEN ?='AOG' THEN COALESCE(aog_since,CURRENT_TIMESTAMP) ELSE aog_since END
        WHERE id=?""", (
        condition, preliminary_verdict, report_text,
        mel_values['mcc_mel_choice'], mel_values['mcc_ata'], mel_values['mcc_mel_ref'],
        mel_values['mcc_mel_category'], mel_values['mcc_due_date'], mel_values['mcc_due_condition'],
        mel_values['mcc_mel_revision'], mel_values['mcc_mel_description'], mel_values['mcc_mel_restrictions'],
        mel_values['mcc_proc_o'], mel_values['mcc_proc_m'], mel_values['mcc_proc_o_text'], mel_values['mcc_proc_m_text'],
        mel_values['mel_catalog_id'], mel_values['mel_assessed_by'], mel_values['mel_assessment_comment'],
        mel_values['mel_rectification_interval'], 1 if mel_m_procedure_completed else 0, mel_m_procedure_comment,
        mel_values['mel_deadline_mode'], mel_values['mel_deadline_value'],
        mel_values['mel_deadline_start_rule'], preliminary_verdict,
        1 if preserve_cpu_mel_o else 0, 1 if preserve_cpu_mel_o else 0,
        1 if preserve_cpu_mel_o else 0, flight_reason,
        1 if waiting_parts else 0, preliminary_verdict, preliminary_verdict, rid))
    if row['engineer_id']:
        release_engineers_without_active_requests(conn, [row['engineer_id']])
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    event_text = 'Техник установил результат «' + preliminary_verdict + '». Отчёт передан в ЦПУ. Состояние: ' + condition
    if preliminary_verdict == 'MEL':
        event_text += ' · выбран пункт ' + str(mel_values['mcc_mel_ref']) + ' · Cat ' + str(mel_values['mcc_mel_category'])
    _log_routing_event(conn, rid, 'cpu_report', 'Инженер', event_text)
    _log_event('status_change', 'Инженер', rid, {
        'from': row['status'], 'to': 'report_submitted',
        'reason': 'Инженер отправил отчёт о диагностике',
        'condition': condition, 'verdict': preliminary_verdict,
    })
    conn.commit()
    conn.close()
    if waiting_parts:
        _log_event('waiting_parts', 'Инженер', rid, {
            'reason': 'AOG: требуется запчасть, инженер освобождён и возвращён в пул',
            'engineer_id': str(row['engineer_id'] or ''),
            'aog_action': 'WAITING_PARTS',
        })
    _log_event('report_submitted', 'Инженер', rid, {
        'condition': condition, 'verdict': preliminary_verdict,
        'need_parts': need_parts, 'waiting_parts': waiting_parts,
        'engineer_id': str(row['engineer_id'] or ''),
    })
    _recalc_route_for_request_id(rid, force=True)
    # Инженер освобождён: сразу запускаем автоподбор для ожидающих заявок.
    try:
        try_reassign_queued()
    except Exception as exc:
        print('[ENGINEER REPORT] queued auto-assign:', exc)
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'cpu_report',
                    'status': 'report_submitted', 'engineer_verdict': preliminary_verdict,
        'flight_status': 'DO_NOT_FLY', 'flight_status_reason': flight_reason})


@app.route('/api/engineer/verdict', methods=['POST'])
def engineer_verdict():
    """Deprecated: only the technician's submitted diagnosis report may set the verdict."""
    return jsonify({
        'success': False,
        'error': 'Технический вердикт устанавливает только техник при отправке отчёта: MEL, AOG или «Устранено».',
    }), 410


@app.route('/api/mcc/finalize', methods=['POST'])
def mcc_finalize():
    data = request.get_json() or {}
    rid = data.get('request_id')
    action = data.get('action')
    comment = (data.get('comment') or '').strip()
    if not rid or action not in ('CONFIRM_VERDICT', 'AOG_RECOVERY', 'MEL_DEFER', 'MEL_RETURN_CPU', 'CLOSE'):
        return jsonify({'success': False, 'error': 'Неверное действие'}), 400
    if not comment and action != 'CONFIRM_VERDICT':
        return jsonify({'success': False, 'error': 'Комментарий обязателен'}), 400
    if action == 'CONFIRM_VERDICT' and not comment:
        comment = 'Технический вердикт инженера подтверждён MCC.'

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, engineer_verdict, engineer_id, mcc_status, routing_stage, engineer_condition, engineer_report_at, engineer_comment, status, mcc_mel_choice, mcc_proc_o, mcc_proc_m, cpu_mel_o_completed, cpu_mel_m_completed, cpu_mel_checked_at, mcc_mel_ref, mcc_mel_category, mcc_due_date, mcc_due_condition FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    is_mel_workflow = row['mcc_mel_choice'] == 'MEL'
    requested_action = action
    if action == 'CONFIRM_VERDICT':
        if is_mel_workflow and row['engineer_verdict'] == 'MEL':
            action = 'MEL_DEFER'
        elif row['engineer_verdict'] == 'AOG' and row['engineer_condition'] == 'aog':
            action = 'AOG_RECOVERY'
        else:
            conn.close()
            return jsonify({'success': False, 'error': 'Подтверждение доступно только для вердикта MEL или AOG'}), 409
    if is_mel_workflow:
        if row['engineer_verdict'] != 'MEL' or row['engineer_condition'] != 'mel_deferred':
            conn.close()
            return jsonify({'success': False, 'error': 'Ветка MEL должна начинаться с заключения техника MEL и выбранного пункта утверждённого справочника'}), 409
        if row['mcc_status'] not in ('MEL_CPU_CHECKED', 'MEL_REVIEW') or row['routing_stage'] != 'mcc_mel_finalize' or not row['cpu_mel_checked_at']:
            conn.close()
            return jsonify({'success': False, 'error': 'Проверка применимости и процедур MEL в ЦПУ ещё не завершена'}), 409
        if action == 'MEL_DEFER':
            if row['engineer_verdict'] == 'AOG':
                conn.close()
                return jsonify({'success': False, 'error': 'Инженер установил AOG. MEL не может заменить AOG-ограничение'}), 409
            if row['mcc_proc_o'] and not row['cpu_mel_o_completed']:
                conn.close()
                return jsonify({'success': False, 'error': 'O-процедура MEL не подтверждена ЦПУ'}), 409
            if row['mcc_proc_m'] and not row['cpu_mel_m_completed']:
                conn.close()
                return jsonify({'success': False, 'error': 'M-процедура MEL не подтверждена после работы инженера и проверки ЦПУ'}), 409
            if not bool(data.get('mel_combined_checked')):
                conn.close()
                return jsonify({'success': False, 'error': 'MCC должно проверить совокупное влияние всех активных MEL/CDL'}), 400
            if not bool(data.get('mel_restrictions_verified')):
                conn.close()
                return jsonify({'success': False, 'error': 'MCC должно подтвердить ограничения и их применимость к конфигурации ВС'}), 400
            if not bool(data.get('mel_crew_briefed')):
                conn.close()
                return jsonify({'success': False, 'error': 'Зафиксируйте доведение MEL-ограничений до экипажа / ответственного канала'}), 400
            if len(str(data.get('mel_repair_plan') or '').strip()) < 5:
                conn.close()
                return jsonify({'success': False, 'error': 'Укажите план и ответственного за устранение дефекта'}), 400
        elif action == 'CLOSE':
            conn.close()
            return jsonify({'success': False, 'error': 'Для MEL выберите MEL_DEFER после выполнения процедур либо AOG_RECOVERY при AOG'}), 409
    else:
        if row['mcc_status'] != 'INSPECTION' or row['routing_stage'] != 'mcc_finalize':
            conn.close()
            return jsonify({'success': False, 'error': 'Заявка ещё не прошла обратную цепочку диспетчер → ЦПУ → MCC'}), 409
        if (not row['engineer_report_at'] or not row['engineer_verdict'] or
                row['engineer_condition'] not in ('aog', 'repaired') or
                row['status'] != 'report_submitted' or 'ОТЧЁТ О ДИАГНОСТИКЕ' not in (row['engineer_comment'] or '')):
            conn.close()
            return jsonify({'success': False, 'error': 'MCC не может финализировать заявку до получения полного отчёта инженера'}), 409

    # No Kupol integration is performed. Legacy field is intentionally left empty.
    kupol_ref = ''
    dce_name = data.get('dce_name') or 'DCE'
    mcc_reviewer = str(data.get('reviewer') or dce_name).strip()

    if action == 'MEL_RETURN_CPU':
        if not is_mel_workflow or row['routing_stage'] != 'mcc_mel_finalize' or row['mcc_status'] not in ('MEL_CPU_CHECKED', 'MEL_REVIEW'):
            conn.close()
            return jsonify({'success': False, 'error': 'Вернуть в ЦПУ можно только MEL-отчёт, ожидающий решения MCC'}), 409
        return_comment = 'не удалось принять решение'
        cur.execute("""UPDATE requests SET
            mcc_status='MEL_CPU_RECHECK', mcc_decision='MEL_RETURNED_TO_CPU',
            routing_stage='cpu_report', status='report_submitted',
            mcc_mel_return_comment=?, mcc_comment=COALESCE(mcc_comment,'') || ?,
            flight_status='DO_NOT_FLY',
            flight_status_reason='MCC вернул MEL-отчёт в ЦПУ на перепроверку. Полёт запрещён.'
            WHERE id=?""", (return_comment,
                '\n[MCC→ЦПУ · MEL перепроверка] ' + return_comment, rid))
        _log_routing_event(conn, rid, 'cpu_report', mcc_reviewer,
            'MEL возвращён в ЦПУ на перепроверку: ' + return_comment)
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'request_id': rid, 'action': action,
                        'mcc_status': 'MEL_CPU_RECHECK', 'routing_stage': 'cpu_report',
                        'comment': return_comment, 'flight_status': 'DO_NOT_FLY'})

    if action == 'AOG_RECOVERY':
        if row['engineer_verdict'] != 'AOG' or row['engineer_condition'] != 'aog':
            conn.close()
            return jsonify({'success': False, 'error': 'AOG Recovery доступен только если техник установил результат AOG'}), 400
        etr = (data.get('etr') or '').strip()
        aog_action = (data.get('aog_action') or '').strip()
        cur.execute("""UPDATE requests SET
            mcc_status='AOG_CONFIRMED', status='aog_waiting',
            mcc_decision='AOG_RECOVERY', mcc_mel_choice='',
            mcc_comment=COALESCE(mcc_comment,'') || ?,
            etr=?, aog_action=?, mcc_dce_name=?, mcc_kupol_ref=?,
            aog_since=COALESCE(aog_since,CURRENT_TIMESTAMP),
            routing_stage='pilot_notice', flight_status='DO_NOT_FLY',
            flight_status_reason='AOG: полёт запрещён до устранения дефекта и нового решения MCC',
            pilot_notice_at=CURRENT_TIMESTAMP
            WHERE id=?""",
            ('\n[MCC] AOG Recovery: ' + comment, etr, aog_action, dce_name, kupol_ref, rid))
        _log_routing_event(conn, rid, 'pilot_notice', 'MCC', 'Вердикт AOG подтверждён MCC. ETR=' + (etr or 'не указан; будет заполнен при планировании'))

    elif action == 'MEL_DEFER':
        if is_mel_workflow:
            # Initial MEL details and time limits were entered from the operator-approved MEL.
            # CPU has confirmed required O/M procedures; MCC makes the final operational decision.
            if row['engineer_verdict'] == 'AOG':
                conn.close()
                return jsonify({'success': False, 'error': 'AOG от инженера: требуется ветка AOG Recovery, а не MEL'}), 409
            cur.execute("""UPDATE requests SET
                mcc_status='MEL_READY', status='deferred', mcc_decision='MEL_DEFER',
                mcc_comment=COALESCE(mcc_comment,'') || ?,
                mcc_mel_reviewed_by=?, mcc_mel_reviewed_at=CURRENT_TIMESTAMP, mcc_mel_review_comment=?,
                mcc_mel_combined_checked=1, mcc_mel_crew_briefed=1, mcc_mel_restrictions_verified=1,
                mcc_mel_repair_plan=?, mcc_dce_name=?, mcc_kupol_ref='', routing_stage='pilot_notice',
                flight_status='DO_NOT_FLY',
                flight_status_reason='Пункт MEL проверен ЦПУ и MCC. Условная эксплуатация ещё не активирована: требуется отдельное решение MCC.',
                pilot_notice_at=CURRENT_TIMESTAMP
                WHERE id=?""", ('\n[MCC] MEL принято к оформлению: ' + comment, mcc_reviewer, comment,
                    str(data.get('mel_repair_plan') or '').strip(), dce_name, rid))
            _log_routing_event(conn, rid, 'pilot_notice', 'MCC',
                'Проверка MEL завершена. Ожидается отдельное включение условной эксплуатации по пункту ' + str(row['mcc_mel_ref'] or ''))
        else:
            conn.close()
            return jsonify({'success': False, 'error': 'MEL нельзя создать решением MCC. Техник должен установить результат MEL и выбрать пункт из утверждённого справочника.'}), 409

    else:  # CLOSE: только подтверждённое устранение / неподтверждённый дефект
        if row['engineer_condition'] != 'repaired' or row['engineer_verdict'] != 'REPAIR':
            conn.close()
            return jsonify({'success': False, 'error': 'Закрытие доступно только если техник указал результат «Устранено»'}), 400
        cur.execute("""UPDATE requests SET
            mcc_status='REPAIRED', status='completed', mcc_decision='CLOSE',
            mcc_comment=COALESCE(mcc_comment,'') || ?,
            mcc_dce_name=?, mcc_kupol_ref=?, routing_stage='pilot_notice',
            flight_status='DO_NOT_FLY',
            flight_status_reason='Работы отмечены как выполненные. Ожидается отдельное оформление допуска MCC',
            pilot_notice_at=CURRENT_TIMESTAMP
            WHERE id=?""",
            ('\n[MCC] Заключение о ремонте: ' + comment, dce_name, kupol_ref, rid))
        _log_routing_event(conn, rid, 'pilot_notice', 'MCC', 'Пилоту передано уведомление: полёт пока запрещён до оформления допуска')

    if row['engineer_id']:
        cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle' WHERE id=?", (row['engineer_id'],))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({
        'success': True, 'request_id': rid, 'action': requested_action,
        'mcc_status': {'AOG_RECOVERY': 'AOG_CONFIRMED', 'MEL_DEFER': 'MEL_READY', 'CLOSE': 'REPAIRED'}.get(action, action),
        'routing_stage': 'pilot_notice', 'flight_status': 'DO_NOT_FLY'
    })


@app.route('/api/mcc/aog_recovered', methods=['POST'])
def mcc_aog_recovered():
    """MCC records completion of AOG recovery; this does NOT authorize a flight."""
    data = request.get_json() or {}
    rid = data.get('request_id')
    comment = (data.get('comment') or '').strip()
    authorized_by = (data.get('authorized_by') or '').strip()
    if not rid or not comment or not authorized_by:
        return jsonify({'success': False, 'error': 'Укажите заявку, сотрудника MCC и описание выполненного восстановления'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, mcc_status, routing_stage FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['mcc_status'] != 'AOG_CONFIRMED' or row['routing_stage'] != 'pilot_notice':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не находится в активном AOG Recovery'}), 409
    cur.execute("""UPDATE requests SET mcc_status='REPAIRED', status='completed',
                   mcc_decision='AOG_RECOVERED', mcc_comment=COALESCE(mcc_comment,'') || ?,
                   routing_stage='pilot_notice', flight_status='DO_NOT_FLY',
                   flight_status_reason='AOG Recovery отмечен MCC как завершённый. Полёт запрещён до отдельного оформления допуска MCC',
                   pilot_notice_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                ('\n[MCC AOG RECOVERED] ' + comment + ' (' + authorized_by + ')', rid))
    _log_routing_event(conn, rid, 'pilot_notice', 'MCC', 'AOG Recovery завершён; пилоту оставлен запрет на вылет до отдельного допуска. ' + comment)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'pilot_notice', 'flight_status': 'DO_NOT_FLY', 'mcc_status': 'REPAIRED'})


@app.route('/api/mcc/aog_update', methods=['POST'])
def mcc_aog_update():
    data = request.get_json() or {}
    rid = data.get('request_id')
    etr = data.get('etr', '')
    aog_action = data.get('aog_action', '')
    if not rid:
        return jsonify({'success': False, 'error': 'Не указан request_id'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""UPDATE requests SET etr=?, aog_action=?
        WHERE id=? AND mcc_status='AOG_CONFIRMED'""", (etr, aog_action, rid))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid})


@app.route('/api/mcc/clear/<clear_type>', methods=['POST'])
def mcc_clear(clear_type):
    valid = ('queue', 'inspection', 'deferred', 'recurring', 'aog', 'history', 'all')
    if clear_type not in valid:
        return jsonify({'success': False, 'error': f'Unknown: {clear_type}'}), 400
    conn = get_db()
    cur = conn.cursor()
    try:
        deleted = 0
        delete_where = {
            'queue': "mcc_status='PENDING' AND status!='deleted'",
            'inspection': "mcc_status='INSPECTION' AND status!='deleted'",
            'deferred': "mcc_status IN ('MEL','MEL_REVIEW','MEL_READY','MEL_ACTIVE','MEL_EXPIRED') AND status!='deleted'",
            'aog': "mcc_status='AOG_CONFIRMED' AND status!='deleted'",
            'history': "mcc_status IN ('REPAIRED','RELEASED','CLOSED') AND status!='deleted'",
            'all': "status NOT IN ('draft','deleted')",
        }[clear_type]
        cur.execute(f"SELECT DISTINCT engineer_id FROM requests WHERE {delete_where} AND engineer_id IS NOT NULL")
        affected_engineers = [r['engineer_id'] for r in cur.fetchall()]
        deleted = _archive_requests_before_delete(cur, delete_where, reason='Очищено через MCC: ' + clear_type)
        release_engineers_without_active_requests(conn, affected_engineers)
        cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to IN (SELECT id FROM requests WHERE status='deleted')")
        conn.commit()
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/pilot/confirm/<rid>', methods=['POST'])
def confirm_pilot_request(rid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE requests SET status='new', mcc_status='PENDING', routing_stage='mcc', flight_status='DO_NOT_FLY', flight_status_reason='Заявка о техническом дефекте зарегистрирована; полёт запрещён до решения MCC' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/pilot/sign_crs/<rid>', methods=['POST'])
@app.route('/api/requests/<rid>/pilot-signature', methods=['POST'])
def pilot_sign_crs(rid):
    """Сохраняет подпись пилота об ознакомлении с уже полученным вердиктом.

    Второй URL /api/requests/<id>/pilot-signature используется как основной:
    некоторые reverse-proxy/правила доступа блокируют старый URL /api/pilot/....
    Никакого отдельного имени/подтверждения от пилота не требуется.
    """
    data = request.get_json(silent=True) or {}
    signature = str(data.get('signature') or '').strip()
    signed_by = str(data.get('signed_by') or '').strip()

    if not signature.startswith('data:image/') or ',' not in signature:
        return jsonify({'success': False, 'error': 'Нарисуйте подпись в поле и повторите отправку'}), 400
    if len(signature) > 2_500_000:
        return jsonify({'success': False, 'error': 'Изображение подписи слишком большое'}), 413

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, pilot_name, engineer_verdict, engineer_condition,
            engineer_report_at, mcc_decision, routing_stage, crs_signed_at
            FROM requests WHERE id=?""", (rid,))
        row = cur.fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404

        # Разрешаем подпись, когда в заявке уже есть переданный результат.
        # Не привязываем сохранение к одному конкретному mcc_status: для
        # MEL/AOG/ремонта используются разные статусы.
        has_verdict = bool(row['engineer_verdict'] or row['engineer_condition'] or
                           row['engineer_report_at'] or row['mcc_decision'])
        if not has_verdict:
            return jsonify({'success': False, 'error': 'Сначала дождитесь итогового вердикта'}), 409
        if row['crs_signed_at']:
            return jsonify({'success': True, 'already_signed': True,
                            'message': 'Подпись уже сохранена'}), 200

        if not signed_by:
            signed_by = str(row['pilot_name'] or '').strip() or 'Пилот'

        cur.execute("""UPDATE requests
            SET crs_signature=?, crs_signed_at=CURRENT_TIMESTAMP, crs_signed_by=?
            WHERE id=? AND (crs_signed_at IS NULL OR crs_signed_at='')""",
            (signature, signed_by, rid))
        if cur.rowcount != 1:
            # Идемпотентность: повторный клик не превращается в ошибку.
            cur.execute("SELECT crs_signed_at, crs_signed_by FROM requests WHERE id=?", (rid,))
            saved = cur.fetchone()
            if saved and saved['crs_signed_at']:
                conn.commit()
                return jsonify({'success': True, 'already_signed': True,
                                'message': 'Подпись уже сохранена'}), 200
            return jsonify({'success': False, 'error': 'Не удалось сохранить подпись. Повторите попытку'}), 409

        try:
            _log_routing_event(conn, rid, row['routing_stage'] or 'pilot_notice', 'Пилот',
                'Пилот подписал подтверждение ознакомления с техническим вердиктом; допуск ВС не изменён')
        except Exception as log_error:
            # Ошибка журнала не должна отменять сохранение самой подписи.
            print(f'[PILOT SIGN] routing log warning for {rid}: {log_error}')
        conn.commit()
        return jsonify({'success': True, 'message': 'Подпись сохранена',
                        'signed_by': signed_by, 'flight_status_unchanged': True}), 200
    except Exception as exc:
        conn.rollback()
        print(f'[PILOT SIGN] save error for {rid}: {exc}')
        return jsonify({'success': False, 'error': 'Ошибка сохранения подписи. Проверьте журнал сервера'}), 500
    finally:
        conn.close()


@app.route('/api/mcc/authorize_release', methods=['POST'])
def mcc_authorize_release():
    """Отдельное действие MCC для снятия программного ограничения после проверки документов."""
    data = request.get_json() or {}
    rid = data.get('request_id')
    authorized_by = (data.get('authorized_by') or '').strip()
    comment = (data.get('comment') or '').strip()
    if not rid or not authorized_by or not comment:
        return jsonify({'success': False, 'error': 'Укажите заявку, ФИО уполномоченного сотрудника MCC и основание допуска'}), 400
    conn = get_db()
    cur = conn.cursor()
    sync_expired_mel(conn)
    cur.execute("SELECT id, mcc_status, routing_stage, flight_status, mcc_mel_ref, mcc_due_date, mcc_due_condition, mcc_mel_restrictions, mcc_proc_o, mcc_proc_m, cpu_mel_o_completed, cpu_mel_m_completed, mcc_mel_reviewed_by, mcc_mel_reviewed_at, mcc_mel_combined_checked, mcc_mel_crew_briefed, mcc_mel_restrictions_verified, mcc_mel_repair_plan FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'pilot_notice' or row['flight_status'] != 'DO_NOT_FLY' or row['mcc_status'] not in ('REPAIRED', 'MEL_READY', 'MEL'):
        conn.close()
        return jsonify({'success': False, 'error': 'Для этой заявки нет основания оформить допуск. AOG или незавершённый MEL остаётся ограничением.'}), 409

    if row['mcc_status'] in ('MEL_READY', 'MEL'):
        if row['mcc_due_date']:
            try:
                if datetime.strptime(str(row['mcc_due_date'])[:10], '%Y-%m-%d').date() < datetime.utcnow().date():
                    conn.close()
                    return jsonify({'success': False, 'error': 'Срок MEL истёк. Условный допуск запрещён до устранения дефекта или отдельного допустимого продления.'}), 409
            except ValueError:
                conn.close()
                return jsonify({'success': False, 'error': 'Некорректный срок MEL. Требуется проверка MCC.'}), 409
        if row['mcc_status'] == 'MEL_READY' and ((row['mcc_proc_o'] and not row['cpu_mel_o_completed']) or (row['mcc_proc_m'] and not row['cpu_mel_m_completed'])):
            conn.close()
            return jsonify({'success': False, 'error': 'Не все процедуры MEL подтверждены ЦПУ.'}), 409
        if row['mcc_status'] == 'MEL_READY':
            if (not row['mcc_mel_reviewed_by'] or not row['mcc_mel_reviewed_at'] or
                    not row['mcc_mel_combined_checked'] or not row['mcc_mel_crew_briefed'] or
                    not row['mcc_mel_restrictions_verified'] or not row['mcc_mel_repair_plan']):
                conn.close()
                return jsonify({'success': False, 'error': 'Не завершён контроль MCC: совокупное влияние, ограничения, информирование экипажа и план устранения.'}), 409
        flight_reason = 'MEL ' + str(row['mcc_mel_ref'] or '') + ' активен. Эксплуатация возможна только при соблюдении утверждённых ограничений: ' + str(row['mcc_mel_restrictions'] or '')
        cur.execute("""UPDATE requests SET flight_status='MEL_ACTIVE', flight_status_reason=?,
            mcc_status='MEL_ACTIVE', routing_stage='mel_active', status='deferred'
            WHERE id=?""", (flight_reason, rid))
        _log_routing_event(conn, rid, 'mel_active', 'MCC', 'Условная эксплуатация по MEL активирована уполномоченным сотрудником ' + authorized_by + '. ' + comment)
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'request_id': rid, 'flight_status': 'MEL_ACTIVE', 'routing_stage': 'mel_active', 'mcc_status': 'MEL_ACTIVE'})

    cur.execute("""UPDATE requests SET flight_status='RELEASED', flight_status_reason=?,
                   mcc_status='RELEASED', routing_stage='released', status='completed'
                   WHERE id=?""", ('Допуск MCC: ' + comment + ' (' + authorized_by + ')', rid))
    _log_routing_event(conn, rid, 'released', 'MCC', 'Программное ограничение снято. Уполномоченный: ' + authorized_by + '. ' + comment)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'flight_status': 'RELEASED', 'routing_stage': 'released'})

@app.route('/api/cpu/mel_queue', methods=['GET'])
def cpu_mel_queue():
    conn = get_db()
    sync_expired_mel(conn)
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description, parking_id,
        mcc_ata, mcc_mel_ref, mcc_mel_category, mcc_due_date, mcc_due_condition,
        mcc_mel_revision, mcc_mel_description, mcc_mel_restrictions,
        mcc_proc_o, mcc_proc_m, mcc_proc_o_text, mcc_proc_m_text,
        mcc_comment, cpu_mel_o_completed, cpu_mel_m_completed,
        cpu_mel_check_comment, created_at, routing_stage, mcc_status
        FROM requests WHERE routing_stage='cpu_mel_review' AND mcc_status='MEL_REVIEW'
          AND status NOT IN ('cancelled','flight_cancelled','completed','draft','deleted')
        ORDER BY created_at ASC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/cpu/mel_return_to_mcc', methods=['POST'])
def cpu_mel_return_to_mcc():
    """Return a MEL item to MCC when CPU cannot confirm applicability/procedures."""
    data = request.get_json() or {}
    rid = data.get('request_id')
    reason = str(data.get('reason') or '').strip()
    reviewer = str(data.get('reviewer') or '').strip() or 'ЦПУ'
    if not rid or not reason:
        return jsonify({'success': False, 'error': 'Укажите заявку и причину возврата MEL в MCC'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, mcc_status, routing_stage, mcc_mel_ref FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['mcc_status'] != 'MEL_REVIEW' or row['routing_stage'] != 'cpu_mel_review':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка больше не ожидает первичной проверки MEL в ЦПУ'}), 409
    cur.execute("""UPDATE requests SET mcc_status='PENDING', mcc_decision='MEL_RETURNED',
        mcc_mel_choice='', status='open', routing_stage='mcc',
        mcc_comment=COALESCE(mcc_comment,'') || ?,
        flight_status='DO_NOT_FLY',
        flight_status_reason='ЦПУ вернуло MEL в MCC: требуется повторная оценка применимости. Полёт запрещён.'
        WHERE id=?""", ('\n[ЦПУ→MCC: возврат MEL] ' + reviewer + ': ' + reason, rid))
    _log_routing_event(conn, rid, 'mcc', reviewer,
        'MEL ' + str(row['mcc_mel_ref'] or '') + ' возвращён на повторную оценку: ' + reason)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'mcc', 'mcc_status': 'PENDING'})


@app.route('/api/cpu/mel_review', methods=['POST'])
def cpu_mel_review():
    data = request.get_json() or {}
    rid = data.get('request_id')
    o_completed = bool(data.get('o_completed'))
    comment = str(data.get('comment') or '').strip()
    reviewer = str(data.get('reviewer') or '').strip() or 'ЦПУ'
    if not rid or not comment:
        return jsonify({'success': False, 'error': 'Укажите заявку и комментарий проверки ЦПУ'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, mcc_proc_o, mcc_proc_m, routing_stage,
        mcc_status, mcc_mel_ref FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'cpu_mel_review' or row['mcc_status'] != 'MEL_REVIEW':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не находится в очереди MEL ЦПУ'}), 409
    if row['mcc_proc_o'] and not o_completed:
        conn.close()
        return jsonify({'success': False, 'error': 'Подтвердите выполнение O-процедуры или верните заявку на уточнение MCC'}), 400

    if row['mcc_proc_m']:
        cur.execute("""UPDATE requests SET cpu_mel_o_completed=?, cpu_mel_engineer_required=1,
            cpu_mel_check_comment=?, cpu_mel_checked_by=?, cpu_mel_checked_at=CURRENT_TIMESTAMP,
            status='queued', routing_stage='dispatcher',
            mcc_comment=COALESCE(mcc_comment,'') || ?
            WHERE id=?""", (1 if (o_completed or not row['mcc_proc_o']) else 0,
                comment, reviewer, '\n[ЦПУ MEL] O-процедура проверена. Требуется инженер для M-процедуры: ' + comment, rid))
        _log_routing_event(conn, rid, 'dispatcher', 'ЦПУ',
            'MEL ' + str(row['mcc_mel_ref'] or '') + ': O-процедура проверена; направлено диспетчеру для выполнения M-процедуры инженером.')
        conn.commit()
        conn.close()
        try:
            try_reassign_queued()
        except Exception as exc:
            print('[CPU MEL] auto assign:', exc)
        return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'dispatcher',
                        'engineer_required': True, 'message': 'Заявка передана диспетчеру для выполнения M-процедуры'})

    cur.execute("""UPDATE requests SET cpu_mel_o_completed=?, cpu_mel_m_completed=1,
        cpu_mel_engineer_required=0, cpu_mel_check_comment=?, cpu_mel_checked_by=?,
        cpu_mel_checked_at=CURRENT_TIMESTAMP, routing_stage='mcc_mel_finalize',
        status='mel_review', mcc_comment=COALESCE(mcc_comment,'') || ?
        WHERE id=?""", (1 if (o_completed or not row['mcc_proc_o']) else 0,
            comment, reviewer, '\n[ЦПУ MEL проверено] ' + comment, rid))
    _log_routing_event(conn, rid, 'mcc_mel_finalize', 'ЦПУ',
        'Проверка MEL завершена; передано MCC для итогового решения.')
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'mcc_mel_finalize',
                    'engineer_required': False})


@app.route('/api/cpu/queue', methods=['GET'])
def cpu_queue():
    """Заявки, ожидающие решения ЦПУ."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description,
                   parking_id, terminal, source, pilot_name, pilot_rank,
                   mcc_status, mcc_decision, mcc_comment, routing_history,
                   created_at, severity, criticality, engineer_comment, flight_status, flight_status_reason
                   FROM requests
                   WHERE routing_stage='cpu'
                     AND status NOT IN ('cancelled','flight_cancelled','completed','draft','deleted')
                   ORDER BY created_at ASC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        try:
            r['routing_history'] = json.loads(r['routing_history'] or '[]')
        except Exception:
            r['routing_history'] = []
    return jsonify(rows)


@app.route('/api/cpu/send_to_dispatcher', methods=['POST'])
def cpu_send_to_dispatcher():
    """ЦПУ передаёт заявку диспетчеру; инженер автоматически подбирается
    по специализации и минимальному времени прибытия. Заявка сразу появляется
    в активных заявках диспетчера. Если подходящих свободных инженеров нет,
    она остаётся там в статусе ожидания и назначается автоматически позже."""
    data = request.get_json() or {}
    rid = data.get('request_id')
    comment = (data.get('comment') or '').strip()
    if not rid:
        return jsonify({'success': False, 'error': 'request_id обязателен'}), 400
    if not comment:
        return jsonify({'success': False, 'error': 'Комментарий обязателен'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, parking_id, fault_type,
                          routing_stage, source
                   FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'cpu':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не находится в очереди ЦПУ'}), 409

    aircraft = row['aircraft'] or 'ВС'
    parking_id = str(row['parking_id'] or '')
    fault = row['fault_type'] or 'other'
    target = _request_target_coords(parking_id, row['source']) or AIRPORT_CENTERS.get(current_airport_payload()['airport'], (55.975, 37.410))

    # Передаём заявку в диспетчерскую зону, сохраняя исходный source='pilot'.
    # Это необходимо, чтобы пилот продолжал видеть именно свою заявку.
    cur.execute("""UPDATE requests SET routing_stage='dispatcher',
                   mcc_comment=COALESCE(mcc_comment,'') || ?
                   WHERE id=?""", ('\n[ЦПУ→Диспетчер] ' + comment, rid))
    _log_routing_event(conn, rid, 'dispatcher', 'ЦПУ', comment)

    # Автоподбор инженера по профилю дефекта и кратчайшему ETA.
    cur.execute("""SELECT DISTINCT engineer_id FROM requests
                   WHERE routing_stage='technician'
                     AND status IN ('assigned','arriving','in_progress','working')
                     AND engineer_id IS NOT NULL AND id!=?""", (rid,))
    busy = {str(r['engineer_id']) for r in cur.fetchall()}
    eng, route = get_free_engineer_for(
        conn, fault, FAULT_SKILL, target[0], target[1], exclude_ids=busy
    )

    if eng:
        cur.execute("""UPDATE requests SET
                       engineer_id=?, engineer_name=?, status='assigned',
                       routing_stage='technician', mcc_status=CASE WHEN mcc_mel_choice='MEL' THEN 'MEL_REVIEW' ELSE 'INSPECTION' END,
                       travel_time=0, using_car=0, walk_to_car=0, car_time=0, best_car=NULL,
                       engineer_comment='', inspection_assigned_at=NULL,
                       delay_minutes=0, delay_reason='', delay_started_at=NULL,
                       mcc_comment=COALESCE(mcc_comment,'') || ?
                       WHERE id=?""",
                    (eng['id'], eng['name'],
                     '\n[Автоназначение] ' + eng['name'], rid))
        cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                    ('ВС ' + aircraft, eng['id']))
        _log_routing_event(conn, rid, 'technician', 'Диспетчер (авто)',
                           'Автоматически назначен ' + eng['name'])
        conn.commit()
        conn.close()
        # До принятия задачи инженером маршрут не создаётся.
        return jsonify({
            'success': True, 'request_id': rid, 'routing_stage': 'technician',
            'assigned': True, 'queued': False,
            'engineer_id': eng['id'], 'engineer_name': eng['name'],
            'travel_time': 0, 'awaiting_acceptance': True
        })

    # Нет свободного специалиста нужного профиля: заявка уже в активном списке
    # диспетчера и будет автоматически подобрана при освобождении инженера.
    cur.execute("""UPDATE requests SET status='queued',
                   delay_started_at=COALESCE(delay_started_at,CURRENT_TIMESTAMP),
                   delay_reason=CASE WHEN delay_reason IS NULL OR delay_reason=''
                                     THEN 'Автоматический подбор инженера' ELSE delay_reason END
                   WHERE id=?""", (rid,))
    _log_routing_event(conn, rid, 'dispatcher', 'Диспетчер (авто)',
                       'Нет свободного инженера подходящей специализации; включён автоподбор')
    conn.commit()
    conn.close()
    _schedule_recheck(rid)
    return jsonify({
        'success': True, 'request_id': rid, 'routing_stage': 'dispatcher',
        'assigned': False, 'queued': True,
        'message': 'Заявка добавлена в активные заявки; инженер будет назначен автоматически'
    })


@app.route('/api/dispatcher/report_queue', methods=['GET'])
def dispatcher_report_queue():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description, parking_id,
                   engineer_id, engineer_name, engineer_condition, engineer_verdict,
                   engineer_comment, engineer_report_at, created_at, status, criticality,
                   flight_status, flight_status_reason, routing_history
                   FROM requests WHERE routing_stage='dispatcher_report'
                     AND engineer_report_at IS NOT NULL
                     AND engineer_verdict IN ('AOG','REPAIR')
                     AND engineer_condition IN ('aog','repaired')
                     AND status='report_submitted'
                   ORDER BY engineer_report_at ASC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        try: r['routing_history'] = json.loads(r.get('routing_history') or '[]')
        except Exception: r['routing_history'] = []
    return jsonify(rows)


@app.route('/api/dispatcher/forward_report_to_cpu', methods=['POST'])
def dispatcher_forward_report_to_cpu():
    data = request.get_json() or {}
    rid = data.get('request_id')
    comment = (data.get('comment') or '').strip()
    if not rid or not comment:
        return jsonify({'success': False, 'error': 'request_id и комментарий обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, routing_stage, engineer_report_at, engineer_verdict, engineer_condition, status FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'dispatcher_report':
        conn.close()
        return jsonify({'success': False, 'error': 'Отчёт не ожидает обработки диспетчером'}), 409
    if (not row['engineer_report_at'] or not row['engineer_verdict'] or
            row['engineer_condition'] not in ('aog', 'repaired') or
            row['status'] != 'report_submitted'):
        conn.close()
        return jsonify({'success': False, 'error': 'Нельзя передать заявку дальше: инженер ещё не отправил полный отчёт'}), 409
    cur.execute("""UPDATE requests SET routing_stage='cpu_report', dispatcher_report_at=CURRENT_TIMESTAMP,
                   mcc_comment=COALESCE(mcc_comment,'') || ? WHERE id=?""",
                ('\n[Диспетчер→ЦПУ] ' + comment, rid))
    _log_routing_event(conn, rid, 'cpu_report', 'Диспетчер', comment)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'cpu_report'})


@app.route('/api/cpu/report_queue', methods=['GET'])
def cpu_report_queue():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description, parking_id,
                   engineer_id, engineer_name, engineer_condition, engineer_verdict,
                   engineer_comment, engineer_report_at, dispatcher_report_at, cpu_report_at, created_at,
                   status, criticality, flight_status, flight_status_reason, routing_history,
                   mcc_mel_choice, mcc_ata, mcc_mel_ref, mcc_mel_category, mcc_due_date, mcc_due_condition,
                   mcc_mel_revision, mcc_mel_description, mcc_mel_restrictions,
                   mcc_proc_o, mcc_proc_m, mcc_proc_o_text, mcc_proc_m_text,
                   mel_catalog_id, mel_assessed_by, mel_assessment_comment, mel_rectification_interval,
                   mel_m_procedure_completed, mel_m_procedure_comment,
                   mel_initial_engineer_report, mel_initial_engineer_report_at, mel_initial_assessed_by,
                   mel_deadline_mode, mel_deadline_value, mel_deadline_start_rule,
                   mcc_proc_m_text, cpu_mel_o_completed, cpu_mel_m_completed,
                   cpu_mel_engineer_required, cpu_mel_applicability_confirmed,
                   cpu_mel_o_comment, cpu_mel_m_comment, cpu_mel_checked_by, cpu_mel_checked_at,
                   mcc_mel_return_comment
                   FROM requests WHERE routing_stage='cpu_report'
                     AND engineer_report_at IS NOT NULL
                     AND engineer_verdict IN ('AOG','REPAIR','MEL')
                     AND engineer_condition IN ('aog','repaired','mel_deferred')
                     AND status='report_submitted'
                   ORDER BY COALESCE(cpu_report_at, engineer_report_at) ASC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        try: r['routing_history'] = json.loads(r.get('routing_history') or '[]')
        except Exception: r['routing_history'] = []
    return jsonify(rows)


@app.route('/api/cpu/prepare_m_procedure', methods=['POST'])
def cpu_prepare_m_procedure():
    """CPU validates a technician-selected MEL item and routes required M-work to an engineer."""
    data = request.get_json() or {}
    rid = str(data.get('request_id') or '').strip()
    comment = str(data.get('comment') or '').strip()
    reviewer = str(data.get('reviewer') or '').strip() or 'ЦПУ'
    applicable = bool(data.get('mel_applicability_confirmed'))
    o_completed = bool(data.get('o_completed'))
    o_comment = str(data.get('o_comment') or '').strip()
    if not rid or len(comment) < 5:
        return jsonify({'success': False, 'error': 'Укажите заявку и комментарий проверки ЦПУ'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, routing_stage, status, engineer_verdict, engineer_condition,
        mcc_mel_choice, mel_catalog_id, mcc_mel_ref, mcc_proc_o, mcc_proc_m,
        mel_m_procedure_completed, engineer_comment, engineer_report_at, engineer_name
        FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'cpu_report' or row['status'] != 'report_submitted' or row['engineer_verdict'] != 'MEL' or row['engineer_condition'] != 'mel_deferred' or row['mcc_mel_choice'] != 'MEL':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не является техническим заключением MEL в очереди ЦПУ'}), 409
    if not row['mel_catalog_id'] or not row['mcc_mel_ref']:
        conn.close()
        return jsonify({'success': False, 'error': 'Нет выбранного пункта из утверждённого MEL'}), 409
    if not row['mcc_proc_m']:
        conn.close()
        return jsonify({'success': False, 'error': 'Для этого пункта M-процедура не требуется. Используйте «Передать MCC»'}), 409
    if row['mel_m_procedure_completed']:
        conn.close()
        return jsonify({'success': False, 'error': 'M-процедура уже отмечена техником как выполненная. Передайте отчёт на финальную проверку ЦПУ.'}), 409
    if not applicable:
        conn.close()
        return jsonify({'success': False, 'error': 'Подтвердите применимость MEL к типу и конфигурации борта'}), 400
    if row['mcc_proc_o'] and (not o_completed or len(o_comment) < 5):
        conn.close()
        return jsonify({'success': False, 'error': 'Сначала подтвердите O-процедуру и укажите результат её проверки'}), 400
    initial_engineer_report = str(row['engineer_comment'] or '')
    if initial_engineer_report.startswith('ROUTE:'):
        route_payload = initial_engineer_report[6:]
        try:
            _route_data, _route_end = json.JSONDecoder().raw_decode(route_payload)
            initial_engineer_report = route_payload[_route_end:].lstrip()
        except Exception:
            pass

    cur.execute("""UPDATE requests SET status='queued', routing_stage='dispatcher',
        mcc_status='MEL_MAINTENANCE_REQUIRED', cpu_mel_applicability_confirmed=1,
        cpu_mel_o_completed=?, cpu_mel_m_completed=0, cpu_mel_engineer_required=1,
        cpu_mel_checked_by=?, cpu_mel_checked_at=NULL, cpu_mel_check_comment=?,
        cpu_mel_o_comment=?, cpu_mel_m_comment='',
        mel_initial_engineer_report=CASE WHEN COALESCE(TRIM(mel_initial_engineer_report),'')='' THEN ? ELSE mel_initial_engineer_report END,
        mel_initial_engineer_report_at=CASE WHEN mel_initial_engineer_report_at IS NULL THEN ? ELSE mel_initial_engineer_report_at END,
        mel_initial_assessed_by=CASE WHEN COALESCE(TRIM(mel_initial_assessed_by),'')='' THEN ? ELSE mel_initial_assessed_by END,
        engineer_id=NULL, engineer_name=NULL, inspection_assigned_at=NULL, rejected_by='',
        mcc_comment=COALESCE(mcc_comment,'') || ?
        WHERE id=?""", (
        1 if (o_completed or not row['mcc_proc_o']) else 0, reviewer, comment, o_comment,
        initial_engineer_report, row['engineer_report_at'], row['engineer_name'] or '',
        '\n[ЦПУ→Диспетчер MEL M-процедура] ' + comment +
        ((' · O-процедура: ' + o_comment) if o_comment else ''), rid))
    _log_routing_event(conn, rid, 'dispatcher', reviewer,
        'Применимость MEL проверена; требуется назначить инженера для M-процедуры по пункту ' + str(row['mcc_mel_ref']))
    conn.commit()
    conn.close()
    try:
        try_reassign_queued()
    except Exception as exc:
        print('[CPU MEL M PROCEDURE] auto-assign:', exc)
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'dispatcher',
                    'mcc_status': 'MEL_MAINTENANCE_REQUIRED', 'engineer_required': True})


@app.route('/api/cpu/forward_report_to_mcc', methods=['POST'])
def cpu_forward_report_to_mcc():
    data = request.get_json() or {}
    rid = data.get('request_id')
    comment = (data.get('comment') or '').strip()
    if not rid or not comment:
        return jsonify({'success': False, 'error': 'request_id и комментарий обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, routing_stage, mcc_status, mcc_mel_choice, mcc_proc_o, mcc_proc_m,
        engineer_report_at, engineer_verdict, engineer_condition, status,
        mel_catalog_id, mcc_mel_ref, mcc_mel_category,
        mel_m_procedure_completed, mel_m_procedure_comment
        FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'cpu_report':
        conn.close()
        return jsonify({'success': False, 'error': 'Отчёт не ожидает обработки ЦПУ'}), 409
    if (not row['engineer_report_at'] or not row['engineer_verdict'] or
            row['engineer_condition'] not in ('aog','repaired','mel_deferred') or
            row['status'] != 'report_submitted'):
        conn.close()
        return jsonify({'success': False, 'error': 'Нельзя передать заявку в MCC: отчёт инженера ещё не отправлен'}), 409

    reviewer = str(data.get('reviewer') or '').strip() or 'ЦПУ'
    if row['engineer_verdict'] == 'MEL':
        applicable = bool(data.get('mel_applicability_confirmed'))
        o_completed = bool(data.get('mel_o_completed'))
        m_completed = bool(data.get('mel_m_completed'))
        o_comment = str(data.get('mel_o_comment') or '').strip()
        m_comment = str(data.get('mel_m_comment') or '').strip()
        if not row['mcc_mel_ref']:
            conn.close()
            return jsonify({'success': False, 'error': 'В отчёте MEL не указан номер пункта'}), 409
        if row['mcc_proc_m'] and (not row['mel_m_procedure_completed'] or len(str(row['mel_m_procedure_comment'] or '').strip()) < 5):
            conn.close()
            return jsonify({'success': False, 'error': 'В отчёте техника нет подтверждения выполнения обязательной M-процедуры'}), 409
        if not applicable:
            conn.close()
            return jsonify({'success': False, 'error': 'ЦПУ должно подтвердить применимость пункта к борту и конфигурации'}), 400
        if row['mcc_proc_o'] and (not o_completed or not o_comment):
            conn.close()
            return jsonify({'success': False, 'error': 'Подтвердите O-процедуру и добавьте комментарий ЦПУ'}), 400
        if row['mcc_proc_m'] and (not m_completed or not m_comment):
            conn.close()
            return jsonify({'success': False, 'error': 'Подтвердите M-процедуру по отчёту инженера и добавьте комментарий ЦПУ'}), 400
        cur.execute("""UPDATE requests SET routing_stage='mcc_mel_finalize', cpu_report_at=CURRENT_TIMESTAMP,
            mcc_status='MEL_CPU_CHECKED', cpu_mel_applicability_confirmed=1,
            cpu_mel_o_completed=?, cpu_mel_m_completed=?, cpu_mel_checked_by=?,
            cpu_mel_checked_at=CURRENT_TIMESTAMP, cpu_mel_check_comment=?, mcc_mel_return_comment='',
            cpu_mel_o_comment=?, cpu_mel_m_comment=?,
            mcc_comment=COALESCE(mcc_comment,'') || ?
            WHERE id=?""", (
            1 if (o_completed or not row['mcc_proc_o']) else 0,
            1 if (m_completed or not row['mcc_proc_m']) else 0,
            reviewer, comment, o_comment, m_comment,
            '\n[ЦПУ→MCC MEL] ' + comment +
              ((' · O-процедура: ' + o_comment) if o_comment else '') +
              ((' · M-процедура: ' + m_comment) if m_comment else ''), rid))
        _log_routing_event(conn, rid, 'mcc_mel_finalize', 'ЦПУ',
            'Применимость MEL и необходимые процедуры проверены; передано MCC на контроль и решение.')
        next_stage = 'mcc_mel_finalize'
    else:
        cur.execute("""UPDATE requests SET routing_stage='mcc_finalize', cpu_report_at=CURRENT_TIMESTAMP,
                       mcc_comment=COALESCE(mcc_comment,'') || ?, mcc_status='INSPECTION'
                       WHERE id=?""", ('\n[ЦПУ→MCC] ' + comment, rid))
        _log_routing_event(conn, rid, 'mcc_finalize', 'ЦПУ', comment)
        next_stage = 'mcc_finalize'
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': next_stage,
                    'mcc_status': 'MEL_CPU_CHECKED' if next_stage == 'mcc_mel_finalize' else 'INSPECTION'})


@app.route('/api/mcc/mel_return_to_engineer', methods=['POST'])
def mcc_mel_return_to_engineer():
    data = request.get_json() or {}
    rid = str(data.get('request_id') or '').strip()
    reason = str(data.get('reason') or '').strip()
    if not rid or not reason:
        return jsonify({'success': False, 'error': 'Укажите заявку и причину возврата инженеру'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, mcc_status, routing_stage, engineer_id FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['mcc_status'] != 'MEL_CPU_CHECKED' or row['routing_stage'] != 'mcc_mel_finalize':
        conn.close()
        return jsonify({'success': False, 'error': 'Возврат возможен только после проверки MEL в ЦПУ и до решения MCC'}), 409
    if row['engineer_id']:
        cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?", (row['engineer_id'],))
    cur.execute("""UPDATE requests SET mcc_status='MEL_RETURNED', mcc_decision='MEL_RETURNED',
        mcc_comment=COALESCE(mcc_comment,'') || ?, status='queued', routing_stage='dispatcher',
        engineer_id=NULL, engineer_name=NULL, inspection_assigned_at=NULL,
        engineer_verdict='', engineer_verdict_at=NULL, engineer_condition='', engineer_report_at=NULL,
        mel_catalog_id='', mel_assessed_by='', mel_assessment_comment='',
        mel_m_procedure_completed=0, mel_m_procedure_comment='',
        mel_initial_engineer_report='', mel_initial_engineer_report_at=NULL, mel_initial_assessed_by='',
        mcc_mel_choice='', mcc_ata='', mcc_mel_ref='', mcc_mel_category='', mcc_due_date=NULL,
        mcc_due_condition='', mcc_mel_revision='', mcc_mel_description='', mcc_mel_restrictions='',
        mcc_proc_o=0, mcc_proc_m=0, mcc_proc_o_text='', mcc_proc_m_text='',
        cpu_mel_applicability_confirmed=0, cpu_mel_o_completed=0, cpu_mel_m_completed=0,
        cpu_mel_checked_at=NULL, cpu_mel_checked_by='', cpu_mel_check_comment='',
        flight_status='DO_NOT_FLY',
        flight_status_reason='MEL возвращён инженеру на повторную оценку. Условная эксплуатация не активна.'
        WHERE id=?""", ('\n[MCC→Инженер: возврат MEL] ' + reason, rid))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    _log_routing_event(conn, rid, 'dispatcher', 'MCC', 'MEL возвращён на повторную техническую оценку: ' + reason)
    conn.commit()
    conn.close()
    try:
        try_reassign_queued()
    except Exception as exc:
        print('[MEL RETURN] auto assign:', exc)
    return jsonify({'success': True, 'request_id': rid, 'routing_stage': 'dispatcher'})


@app.route('/api/mcc/mel_mark_expired', methods=['POST'])
def mcc_mark_mel_expired():
    data = request.get_json() or {}
    rid = str(data.get('request_id') or '').strip()
    reason = str(data.get('reason') or '').strip()
    actor = str(data.get('actor') or '').strip()
    if not rid or not reason or not actor:
        return jsonify({'success': False, 'error': 'Укажите заявку, сотрудника MCC и основание фиксации достижения MEL-лимита'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, mcc_status, mel_deadline_mode, mcc_mel_ref FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['mcc_status'] != 'MEL_ACTIVE' or row['mel_deadline_mode'] == 'CALENDAR_DAYS':
        conn.close()
        return jsonify({'success': False, 'error': 'Ручная фиксация доступна только для активных MEL с некалендарным лимитом'}), 409
    cur.execute("""UPDATE requests SET mcc_status='MEL_EXPIRED', status='mel_expired',
        routing_stage='pilot_notice', flight_status='DO_NOT_FLY',
        flight_status_reason=?, pilot_notice_at=CURRENT_TIMESTAMP,
        mcc_comment=COALESCE(mcc_comment,'') || ? WHERE id=?""",
        ('Достигнут / превышен лимит MEL ' + str(row['mcc_mel_ref'] or '') + '. Полёт запрещён.',
         '\n[MCC: MEL limit reached] ' + actor + ': ' + reason, rid))
    _log_routing_event(conn, rid, 'pilot_notice', actor, 'Достигнут лимит MEL; установлен DO_NOT_FLY. ' + reason)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'mcc_status': 'MEL_EXPIRED', 'flight_status': 'DO_NOT_FLY'})


# ============================================================
# Dispatcher — ручное назначение (fallback)
# ============================================================


def _dispatcher_engineer_recommendations(conn, request_row, limit=5):
    """Explainable multi-criteria ranking: urgency, position, qualification/ATA,
    workload/started work, norm consequences and tool availability."""
    started_at = time.perf_counter()
    def row_get(key, default=None):
        try:
            value = request_row[key]
            return default if value is None else value
        except (KeyError, IndexError, TypeError):
            return default
    classification = _request_classification(request_row)
    required_skill = classification.get('skill') or FAULT_SKILL.get(str(row_get('fault_type') or 'other'), 'general')
    skill_labels = {
        'engine': 'Двигатель', 'avionics': 'Авионика', 'hydraulics': 'Гидравлика',
        'electrical': 'Электрика', 'mechanical': 'Механика', 'general': 'Общая специализация'
    }
    priority = str(row_get('criticality') or row_get('severity') or 'medium').lower()
    text_lower = (str(row_get('description') or '') + ' ' + str(row_get('fault_type') or '')).lower()
    aog = bool(row_get('aog_since')) or priority == 'high' or 'aog' in text_lower
    created = _parse_db_dt(row_get('created_at')) or datetime.now(timezone.utc)
    deadline = created + timedelta(minutes=NORM_LIMIT_MINUTES)

    selected_airport = current_airport_payload()['airport']
    parking_id = str(row_get('parking_id') or '')
    target = _request_target_coords(parking_id, row_get('source'))
    target_known = target is not None
    if target is None:
        target = AIRPORT_CENTERS.get(selected_airport, (55.975, 37.410))

    cur = conn.cursor()
    cur.execute("""SELECT e.id, e.name, e.device_id, e.specialization,
                          COALESCE(gps.lat, p.lat, e.coords_lat) AS coords_lat,
                          COALESCE(gps.lng, p.lng, e.coords_lng) AS coords_lng,
                          e.status, e.task, e.task_step
                   FROM engineers e
                   LEFT JOIN map_object_positions p
                     ON p.object_type='engineer' AND p.object_id=e.id
                   LEFT JOIN map_object_positions gps
                     ON gps.object_type='engineer_gps' AND gps.object_id=e.id""")
    engineers = [dict(row) for row in cur.fetchall()]
    assignments = load_airport_assignments()
    selected_airport = current_airport_payload()['airport']
    engineers = [e for e in engineers
                 if assigned_airport('engineers', e['id'], assignments) == selected_airport]
    cur.execute("""SELECT r.engineer_id, r.id, r.status, r.aircraft, r.parking_id, r.fault_type,
                          e.task_step AS engineer_task_step
                   FROM requests r
                   LEFT JOIN engineers e ON e.id=r.engineer_id
                   WHERE r.routing_stage='technician'
                     AND r.status IN ('assigned','arriving','in_progress','working')
                     AND r.engineer_id IS NOT NULL""")
    active_by_engineer = {}
    for row in cur.fetchall():
        active_by_engineer[str(row['engineer_id'])] = dict(row)
    active_engineer_ids = set(active_by_engineer)
    cur.execute("SELECT id, device_id, lat, lng, speed FROM transport WHERE assigned_to IS NULL")
    cars = [] if selected_airport == 'vdnh' else filter_objects_by_airport('transport', cur.fetchall(), selected_airport, assignments)

    free = [e for e in engineers
            if str(e.get('status') or '').lower() == 'free'
            and str(e.get('id')) not in active_engineer_ids]
    skill_pool = [e for e in free if str(e.get('specialization') or 'general') == required_skill]

    feasible = []
    for engineer in skill_pool:
        # ETA is computed before assignment from the engineer's latest real GPS position.
        if engineer.get('coords_lat') is None or engineer.get('coords_lng') is None:
            continue
        e_lat = float(engineer['coords_lat'])
        e_lng = float(engineer['coords_lng'])
        engineer_airport = assigned_airport('engineers', engineer['id'], assignments)
        _walk_path, walk_distance, walk_time = get_airport_road_path(
            engineer_airport, e_lat, e_lng, target[0], target[1]
        )
        if not _walk_path:
            walk_distance = distance_km(e_lat, e_lng, target[0], target[1])
        tool_status, tool_info = get_engineer_tool_status(engineer['id'])
        warehouse_name = ''
        if (selected_airport == 'vdnh' and _request_needs_tools(request_row) and
                tool_status not in ('on_hand', 'at_site', 'none')):
            warehouse_result = _warehouse_route('vdnh', e_lat, e_lng, target[0], target[1])
            if warehouse_result:
                walk_distance = warehouse_result['distance']
                warehouse_name = warehouse_result['warehouse']['name']
        walk_time = walk_distance / WALK_SPEED_KMH * 60.0
        tool_delay = TOOL_PICKUP_DELAY_MIN if tool_status not in ('on_hand', 'at_site', 'none') else 0.0
        walk_time += tool_delay
        best_time = walk_time
        best_car = None
        best_car_time = 0.0
        walk_to_car = 0.0
        for car in cars:
            car_lat = float(car.get('lat') or 0)
            car_lng = float(car.get('lng') or 0)
            if not car_lat or not car_lng:
                continue
            to_car_path, to_car_dist, _to_car_graph_time = get_airport_road_path(
                selected_airport, e_lat, e_lng, car_lat, car_lng
            )
            if not to_car_path:
                to_car_dist = distance_km(e_lat, e_lng, car_lat, car_lng)
            to_car_time = to_car_dist / WALK_SPEED_KMH * 60
            if selected_airport == 'vdnh':
                result = get_airport_transport_path('vdnh', car_lat, car_lng, target[0], target[1])
                if not result:
                    continue
                _path, road_distance, road_time, _legs = result
            else:
                _path, road_distance, road_time = get_airport_road_path(
                    selected_airport, car_lat, car_lng, target[0], target[1]
                )
                if not _path:
                    road_distance = distance_km(car_lat, car_lng, target[0], target[1])
                    road_time = road_distance / max(float(car.get('speed') or 30), 1.0) * 60
                # Road network ETA respects per-segment limits; vehicle speed is an upper bound.
                road_time = max(float(road_time or 0),
                                road_distance / max(float(car.get('speed') or 30), 1.0) * 60)
            car_leg_time = float(road_time or 0) + TRACK_BOARD_DELAY_MIN + tool_delay
            total_time = to_car_time + car_leg_time
            if total_time < best_time:
                best_time = total_time
                best_car = car
                best_car_time = car_leg_time
                walk_to_car = to_car_time

        eta = round(best_time, 1)
        arrives_at = datetime.now(timezone.utc) + timedelta(minutes=best_time)
        slack = (deadline - arrives_at).total_seconds() / 60.0
        feasible.append({
            'engineer_id': str(engineer['id']),
            'engineer_name': engineer.get('name') or 'Инженер',
            'device_id': engineer.get('device_id') or '',
            'specialization': engineer.get('specialization') or 'general',
            'specialization_label': skill_labels.get(engineer.get('specialization') or 'general', engineer.get('specialization') or '—'),
            'travel_time': eta,
            'using_car': best_car is not None,
            'best_car': (best_car or {}).get('device_id') if best_car else None,
            'walk_to_car': round(walk_to_car, 1),
            'car_time': round(best_car_time, 1),
            'position_known': bool(engineer.get('coords_lat') is not None and engineer.get('coords_lng') is not None),
            'ata': classification.get('ata'),
            'ata_match': True,
            'tool_status': tool_status,
            'tool_ready': tool_status in ('on_hand', 'at_site'),
            'tool_comment': (tool_info or {}).get('comment') or '',
            'via_label': warehouse_name,
            'slack_min': round(slack, 1),
            'meets_norm': slack >= 0,
            'priority': priority,
            'aog': aog,
            'workload': 0,
            'started_work': False,
        })

    feasible.sort(key=lambda item: (not item['meets_norm'], item['travel_time'],
                                    item['engineer_name'].lower(), item['engineer_id']))
    for idx, candidate in enumerate(feasible[:limit], start=1):
        candidate['rank'] = idx
        route_text = ('пешком до ' + str(candidate['best_car']) + ' ' + str(candidate['walk_to_car']) +
                      ' мин, затем на спецтранспорте ' + str(candidate['car_time']) + ' мин'
                      if candidate['using_car'] else 'пешком')
        reason_parts = [
            'Допуск соответствует: ' + skill_labels.get(required_skill, required_skill) +
            (' · ATA ' + str(classification.get('ata')) + ' ' + str(classification.get('ata_name') or '') if classification.get('ata') else ''),
            'инженер свободен, активных заявок нет (загрузка 0)',
            'расчётное прибытие ' + str(candidate['travel_time']) + ' мин (' + route_text + ')',
            ('норматив 15 мин соблюдается, запас ' + str(candidate['slack_min']) + ' мин'
             if candidate['meets_norm'] else 'РИСК: норматив 15 мин не соблюдается'),
            ('инструмент готов' if candidate['tool_ready'] else
             'инструмент не подтверждён — +30 с' + (' и заход на ' + candidate['via_label'] if candidate.get('via_label') else ' на складе')),
        ]
        if aog:
            reason_parts.append('заявка AOG/высокой срочности — приоритет отдан соблюдению норматива')
        if not candidate['position_known']:
            reason_parts.append('координаты инженера не получены — использована базовая точка аэропорта')
        if not target_known:
            reason_parts.append('ID стоянки отсутствует в координатном справочнике — использована базовая точка аэропорта')
        if idx == 1:
            reason_parts.append('лучший по совокупности факторов, а не только по расстоянию')
        else:
            delta = round(candidate['travel_time'] - feasible[0]['travel_time'], 1)
            reason_parts.append('на ' + str(delta) + ' мин дольше лидера рейтинга')
        candidate['reason'] = '; '.join(reason_parts) + '.'

    if feasible:
        no_candidate_reason = ''
    elif not free:
        no_candidate_reason = 'Нет свободных инженеров: все специалисты заняты или недоступны.'
    elif not skill_pool:
        no_candidate_reason = 'Нет свободного инженера со специализацией «' + skill_labels.get(required_skill, required_skill) + '» (ATA ' + str(classification.get('ata') or '—') + ').'
    else:
        no_candidate_reason = 'Нет инженеров с доступными координатами для расчёта времени прибытия.'

    candidates = feasible[:limit]
    feasible_ids = {str(c.get('engineer_id')) for c in feasible}
    skill_pool_ids = {str(e.get('id')) for e in skill_pool}
    rejected = []
    for engineer in engineers:
        eid = str(engineer.get('id'))
        if eid in feasible_ids:
            continue
        active = active_by_engineer.get(eid)
        if str(engineer.get('status') or '').lower() != 'free' or eid in active_engineer_ids:
            if active:
                reason = ('занят: заявка ' + str(active.get('id') or '') +
                          ', этап «' + str(active.get('status') or '') + '»' +
                          (' — снятие после начала работ только решением старшего смены'
                           if active.get('status') in ('in_progress', 'working') else ''))
            else:
                reason = 'занят или уже имеет активную заявку'
        elif eid not in skill_pool_ids:
            reason = ('не соответствует допуску: требуется ' +
                      skill_labels.get(required_skill, required_skill) +
                      (' (ATA ' + str(classification.get('ata')) + ')' if classification.get('ata') else '') +
                      ', у инженера ' + skill_labels.get(str(engineer.get('specialization') or 'general'),
                                                          str(engineer.get('specialization') or '—')))
        elif engineer.get('coords_lat') is None or engineer.get('coords_lng') is None:
            reason = 'нет координат для расчёта времени прибытия'
        else:
            reason = 'не прошёл по маршруту/времени прибытия'
        rejected.append({
            'engineer_id': eid,
            'engineer_name': engineer.get('name') or 'Инженер',
            'reason': reason,
        })

    duration_ms = round((time.perf_counter() - started_at) * 1000.0, 1)
    result = {
        'required_skill': required_skill,
        'required_skill_label': skill_labels.get(required_skill, required_skill),
        'classification': classification,
        'priority': priority,
        'aog': aog,
        'duration_ms': duration_ms,
        'norm_deadline': _iso_ms(deadline),
        'candidates': candidates,
        'rejected': rejected,
        'no_candidate_reason': no_candidate_reason,
        'free_engineers_count': len(free),
        'matching_engineers_count': len(skill_pool),
    }
    _log_event('candidates', 'Система', str(row_get('id') or ''), {
        'duration_ms': duration_ms,
        'duration_sec': round(duration_ms / 1000.0, 3),
        'required_skill': required_skill,
        'classification': classification,
        'priority': priority,
        'aog': aog,
        'target_known': target_known,
        'candidates': [{
            'engineer_id': c.get('engineer_id'),
            'engineer_name': c.get('engineer_name'),
            'eta_min': c.get('travel_time'),
            'using_car': c.get('using_car'),
            'slack_min': c.get('slack_min'),
            'meets_norm': c.get('meets_norm'),
            'tool_ready': c.get('tool_ready'),
            'reason': c.get('reason'),
        } for c in candidates],
        'rejected': rejected,
        'no_candidate_reason': no_candidate_reason,
    })
    return result


@app.route('/api/dispatcher/priorities', methods=['GET'])
def dispatcher_priorities():
    """Priority queue + explainable engineer recommendations for dispatcher."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description, parking_id,
                          source, criticality, severity, created_at, status,
                          mcc_ata, mcc_comment, aog_since
                   FROM requests
                   WHERE routing_stage='dispatcher'
                     AND engineer_id IS NULL
                     AND status NOT IN ('cancelled','flight_cancelled','completed','draft','deleted')
                   ORDER BY CASE COALESCE(criticality, severity, 'medium')
                              WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END,
                            created_at ASC""")
    requests_rows = [dict(row) for row in cur.fetchall()]
    result = []
    priority_labels = {'high': 'Высокий', 'medium': 'Средний', 'low': 'Низкий'}
    for row in requests_rows:
        ranking = _dispatcher_engineer_recommendations(conn, row)
        priority = str(row.get('criticality') or row.get('severity') or 'medium').lower()
        result.append({
            'request_id': row['id'],
            'aircraft': row.get('aircraft') or '—',
            'flight_number': row.get('flight_number') or '',
            'fault_type': row.get('fault_type') or 'other',
            'description': row.get('description') or '',
            'parking_id': row.get('parking_id') or '—',
            'source': row.get('source') or '',
            'created_at': row.get('created_at'),
            'status': row.get('status') or 'queued',
            'priority': priority,
            'priority_label': priority_labels.get(priority, 'Средний'),
            'ranking': ranking,
        })
    conn.close()
    return jsonify({'success': True, 'requests': result})


@app.route('/api/dispatcher/requests/<rid>/priority', methods=['POST'])
def dispatcher_set_request_priority(rid):
    data = request.get_json() or {}
    value = str(data.get('priority') or '').strip()
    priority_map = {'1': 'high', '2': 'medium', '3': 'low'}
    if value not in priority_map:
        return jsonify({'success': False, 'error': 'Приоритет должен быть 1, 2 или 3'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, engineer_id, routing_stage FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'dispatcher' or row['engineer_id']:
        conn.close()
        return jsonify({'success': False, 'error': 'Приоритет можно менять только до назначения инженера'}), 409
    cur.execute("UPDATE requests SET criticality=?, severity=? WHERE id=?",
                (priority_map[value], priority_map[value], rid))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid, 'priority': value})


@app.route('/api/dispatcher/queue', methods=['GET'])
def dispatcher_queue():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, flight_number, fault_type, description,
                   parking_id, terminal, source, pilot_name, pilot_rank,
                   mcc_status, mcc_decision, mcc_comment, routing_history,
                   created_at, severity, criticality, flight_status, flight_status_reason
                   FROM requests
                   WHERE routing_stage='dispatcher'
                     AND status NOT IN ('cancelled','flight_cancelled','completed','draft','deleted')
                   ORDER BY created_at ASC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        try:
            r['routing_history'] = json.loads(r['routing_history'] or '[]')
        except Exception:
            r['routing_history'] = []
    return jsonify(rows)


@app.route('/api/dispatcher/assign', methods=['POST'])
def dispatcher_assign():
    data = request.get_json() or {}
    rid = data.get('request_id')
    eng_id = data.get('engineer_id')
    comment = (data.get('comment') or '').strip()
    if not rid or not eng_id:
        return jsonify({'success': False, 'error': 'Не указаны заявка или инженер'}), 400
    if not comment:
        comment = 'Назначение диспетчером'

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, aircraft, parking_id, fault_type, description, criticality,
                          severity, source, created_at, mcc_ata, mcc_comment, aog_since,
                          routing_stage, status
                   FROM requests WHERE id=?""", (rid,))
    req = cur.fetchone()
    if not req:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if req['routing_stage'] != 'dispatcher':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не у диспетчера'}), 400

    cur.execute("SELECT id, name, device_id, status FROM engineers WHERE id=?", (eng_id,))
    eng = cur.fetchone()
    if not eng:
        conn.close()
        return jsonify({'success': False, 'error': 'Техник не найден'}), 404
    if not object_belongs_to_airport('engineers', eng_id):
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер относится к другому аэропорту'}), 403
    if (eng['status'] or 'free') != 'free':
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер уже занят или недоступен'}), 409
    request_classification = _request_classification(req)
    cur.execute("SELECT specialization FROM engineers WHERE id=?", (eng_id,))
    spec_row = cur.fetchone()
    engineer_skill = spec_row['specialization'] if spec_row else None
    if (request_classification.get('skill') and engineer_skill != request_classification.get('skill')
            and not bool(data.get('override_qualification'))):
        conn.close()
        return jsonify({
            'success': False,
            'requires_override': True,
            'error': 'Допуск инженера не соответствует дефекту: требуется ' +
                     str(request_classification.get('skill')) + ', у инженера ' + str(engineer_skill) +
                     '. Назначение возможно только с подтверждением старшего смены.',
            'required_skill': request_classification.get('skill'),
            'engineer_skill': engineer_skill,
        }), 409
    cur.execute("SELECT id FROM requests WHERE engineer_id=? AND status NOT IN ('completed','cancelled','flight_cancelled','deleted') AND routing_stage='technician'", (eng_id,))
    if cur.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'У инженера уже есть активная заявка'}), 409

    # ETA вычисляется до назначения по текущей GPS-позиции и дорожному графу.
    eta_minutes = 0.0
    request_dict = dict(req)
    ranking = _dispatcher_engineer_recommendations(conn, request_dict)
    for candidate in ranking.get('candidates', []):
        if str(candidate.get('engineer_id')) == str(eng_id):
            eta_minutes = float(candidate.get('travel_time') or 0)
            break

    cur.execute("""UPDATE requests SET
                   engineer_id=?, engineer_name=?, travel_time=?, using_car=0,
                   walk_to_car=0, car_time=0, best_car=NULL, engineer_comment='',
                   status='assigned', routing_stage='technician',
                   mcc_status=CASE WHEN mcc_mel_choice='MEL' THEN 'MEL_REVIEW' ELSE 'INSPECTION' END,
                   inspection_assigned_at=NULL,
                   mcc_comment=COALESCE(mcc_comment,'') || ?
                   WHERE id=?""",
                (eng_id, eng['name'], eta_minutes,
                 '\n[Диспетчер→Техник] ' + comment + ' — ' + eng['name'], rid))
    cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                ('ВС ' + (req['aircraft'] or ''), eng_id))
    _log_routing_event(conn, rid, 'technician', 'Диспетчер',
                       f'Назначен {eng["name"]}: {comment}')
    conn.commit()
    conn.close()
    # Сразу после назначения строим и сохраняем маршрут. Инженер увидит его в карточке назначенной задачи.
    _recalc_route_for_request_id(rid, force=True)
    _log_event('assignment', 'Диспетчер', rid, {
        'engineer_id': eng_id, 'engineer_name': eng['name'],
        'eta_min': eta_minutes, 'comment': comment,
    })
    if str(req['status'] or '') != 'assigned':
        _log_event('status_change', 'Диспетчер', rid, {
            'from': req['status'], 'to': 'assigned',
            'reason': 'Назначен инженер ' + eng['name'],
        })
    _refresh_risk_for_request(rid)

    return jsonify({
        'success': True, 'request_id': rid,
        'engineer_id': eng_id, 'engineer_name': eng['name'],
        'travel_time': eta_minutes, 'awaiting_acceptance': True, 'route_ready': True,
    })


def _append_routing_history(conn, rid, entry):
    cur = conn.cursor()
    cur.execute("SELECT routing_history FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    try:
        history = json.loads(row['routing_history'] or '[]') if row else []
    except (TypeError, ValueError):
        history = []
    if not isinstance(history, list):
        history = []
    history.append(entry)
    cur.execute("UPDATE requests SET routing_history=? WHERE id=?",
                (json.dumps(history, ensure_ascii=False), rid))


@app.route('/api/dispatcher/requests/<rid>/reassign', methods=['POST'])
def dispatcher_reassign(rid):
    """Reassignment. After work starts it requires the senior dispatcher's confirmation."""
    data = request.get_json(silent=True) or {}
    new_engineer_id = str(data.get('engineer_id') or '').strip()
    reason = str(data.get('reason') or '').strip()
    senior_confirmed = bool(data.get('senior_confirmed') or data.get('confirm_senior'))
    actor = str(data.get('actor') or 'Старший смены').strip()[:120]
    if not new_engineer_id:
        return jsonify({'success': False, 'error': 'Укажите нового инженера'}), 400
    if len(reason) < 5:
        return jsonify({'success': False, 'error': 'Укажите причину переназначения (не менее 5 символов)'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.id, r.aircraft, r.parking_id, r.fault_type, r.description,
                          r.criticality, r.severity, r.source, r.created_at, r.mcc_ata,
                          r.mcc_comment, r.aog_since, r.status, r.routing_stage,
                          r.engineer_id, r.engineer_name, r.engineer_comment,
                          e.task_step AS old_task_step
                   FROM requests r LEFT JOIN engineers e ON e.id=r.engineer_id
                   WHERE r.id=?""", (rid,))
    req = cur.fetchone()
    if not req:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if req['routing_stage'] != 'technician':
        conn.close()
        return jsonify({'success': False, 'error': 'Переназначение доступно только на этапе инженера'}), 409
    if str(req['engineer_id'] or '') == new_engineer_id:
        conn.close()
        return jsonify({'success': False, 'error': 'Этот инженер уже назначен'}), 409
    work_started = (str(req['status'] or '') in ('in_progress', 'working') or
                    str(req['old_task_step'] or '') in ('in_car', 'arrived', 'diagnosing'))
    if work_started and not senior_confirmed:
        conn.close()
        return jsonify({
            'success': False,
            'requires_senior_confirmation': True,
            'error': 'Работы уже начаты. Переназначение возможно только с подтверждением старшего смены.',
        }), 409

    cur.execute("SELECT id, name, device_id, status FROM engineers WHERE id=?", (new_engineer_id,))
    eng = cur.fetchone()
    if not eng:
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер не найден'}), 404
    if not object_belongs_to_airport('engineers', new_engineer_id):
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер относится к другому аэропорту'}), 403
    if str(eng['status'] or 'free') != 'free':
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер уже занят'}), 409
    classification_for_check = _request_classification(req)
    new_engineer_skill = None
    cur.execute("SELECT specialization FROM engineers WHERE id=?", (new_engineer_id,))
    spec_row = cur.fetchone()
    if spec_row:
        new_engineer_skill = spec_row['specialization']
    if classification_for_check.get('skill') and new_engineer_skill != classification_for_check.get('skill'):
        if not senior_confirmed:
            conn.close()
            return jsonify({
                'success': False,
                'requires_senior_confirmation': True,
                'error': 'Инженер не соответствует требуемому допуску (' +
                         str(classification_for_check.get('skill')) + '). Переназначение возможно только решением старшего смены.',
                'required_skill': classification_for_check.get('skill'),
                'engineer_skill': new_engineer_skill,
            }), 409
    cur.execute("""SELECT id FROM requests WHERE engineer_id=?
                   AND status NOT IN ('completed','cancelled','flight_cancelled','deleted')
                   AND routing_stage='technician'""", (new_engineer_id,))
    if cur.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'У инженера уже есть активная заявка'}), 409

    old_engineer_id = req['engineer_id']
    old_engineer_name = req['engineer_name'] or ''
    now = datetime.now(timezone.utc)
    old_created = _parse_db_dt(req['created_at']) or now
    deadline = old_created + timedelta(minutes=NORM_LIMIT_MINUTES)
    classification = _request_classification(req)
    target = _request_target_coords(req['parking_id'], req['source']) or AIRPORT_CENTERS.get(current_airport_payload()['airport'], (55.975, 37.410))
    eta = 0.0
    try:
        ranking = _dispatcher_engineer_recommendations(conn, dict(req), limit=10)
        for candidate in ranking.get('candidates', []):
            if str(candidate.get('engineer_id')) == new_engineer_id:
                eta = float(candidate.get('travel_time') or 0)
                break
    except Exception as exc:
        print(f'[REASSIGN] eta: {exc}')

    cleaned_comment = _strip_route_payload(req['engineer_comment'] or '')
    cur.execute("""UPDATE requests SET engineer_id=?, engineer_name=?, status='assigned',
                   travel_time=?, using_car=0, walk_to_car=0, car_time=0, best_car=NULL,
                   engineer_comment=?, inspection_assigned_at=NULL
                   WHERE id=?""",
                (new_engineer_id, eng['name'], eta, cleaned_comment, rid))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    if old_engineer_id:
        release_engineers_without_active_requests(conn, [old_engineer_id])
    cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                ('ВС ' + (req['aircraft'] or ''), new_engineer_id))
    _append_routing_history(conn, rid, {
        'at': _iso_ms(now), 'stage': 'reassignment', 'actor': actor,
        'from_engineer_id': str(old_engineer_id or ''), 'from_engineer_name': old_engineer_name,
        'to_engineer_id': new_engineer_id, 'to_engineer_name': eng['name'],
        'reason': reason, 'senior_confirmed': senior_confirmed,
        'work_started': work_started,
    })
    _log_routing_event(conn, rid, 'technician', actor,
                       'Переназначение: ' + old_engineer_name + ' → ' + eng['name'] + '. ' + reason)
    conn.commit()
    conn.close()
    _log_event('reassignment', actor, rid, {
        'from_engineer_id': str(old_engineer_id or ''), 'from_engineer_name': old_engineer_name,
        'to_engineer_id': new_engineer_id, 'to_engineer_name': eng['name'],
        'reason': reason, 'senior_confirmed': senior_confirmed,
        'work_started': work_started, 'eta_min': eta,
        'deadline_at': _iso_ms(deadline), 'classification': classification,
    })
    if str(req['status'] or '') != 'assigned':
        _log_event('status_change', actor, rid, {
            'from': req['status'], 'to': 'assigned',
            'reason': 'Переназначение на ' + eng['name'],
        })
    _recalc_route_for_request_id(rid, force=True)
    _refresh_risk_for_request(rid)
    try:
        conn = get_db()
        row = conn.execute("SELECT travel_time FROM requests WHERE id=?", (rid,)).fetchone()
        conn.close()
        if row and row['travel_time'] is not None:
            eta = float(row['travel_time'] or 0)
    except Exception:
        pass
    return jsonify({'success': True, 'request_id': rid,
                    'engineer_id': new_engineer_id, 'engineer_name': eng['name'],
                    'status': 'assigned', 'travel_time': eta,
                    'senior_confirmed': senior_confirmed})


@app.route('/api/dispatcher/assigned', methods=['GET'])
def dispatcher_assigned():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.id, r.aircraft, r.fault_type, r.parking_id,
                   r.status, r.engineer_id, r.engineer_name, r.travel_time,
                   r.inspection_assigned_at, r.created_at, e.device_id, e.specialization
                   FROM requests r
                   LEFT JOIN engineers e ON e.id = r.engineer_id
                   WHERE r.routing_stage='technician'
                     AND r.status NOT IN ('completed','cancelled','flight_cancelled','draft','deleted')
                   ORDER BY r.created_at DESC""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


# ============================================================
# Engineers
# ============================================================
@app.route('/api/dispatcher/requests/<rid>/route', methods=['GET'])
def dispatcher_request_route(rid):
    """Return the route preview immediately after assignment. It is visible
    while the engineer decides; actual travel starts only after acceptance.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT r.id, r.engineer_id, r.parking_id, r.source, r.engineer_comment,
                          r.status, r.routing_stage, e.task_step AS engineer_task_step,
                          e.status AS engineer_status,
                          COALESCE(gps.lat, p.lat, e.coords_lat) AS engineer_lat,
                          COALESCE(gps.lng, p.lng, e.coords_lng) AS engineer_lng
                   FROM requests r
                   LEFT JOIN engineers e ON e.id=r.engineer_id
                   LEFT JOIN map_object_positions p
                     ON p.object_type='engineer' AND p.object_id=e.id
                   LEFT JOIN map_object_positions gps
                     ON gps.object_type='engineer_gps' AND gps.object_id=e.id
                   WHERE r.id=?""", (rid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if row['routing_stage'] != 'technician':
        return jsonify({'success': False, 'error': 'Заявка уже вышла из этапа инженера'}), 409
    if (row['status'] in ('completed', 'cancelled', 'flight_cancelled', 'draft', 'report_submitted') or
            row['routing_stage'] != 'technician' or row['engineer_status'] != 'busy' or
            row['engineer_lat'] is None or row['engineer_lng'] is None or
            row['engineer_task_step'] not in ('pending', 'accepted', 'in_car', 'arrived', 'diagnosing')):
        return jsonify({'success': False, 'error': 'Маршрут доступен только назначенному инженеру с активной заявкой',
                        'awaiting_acceptance': False}), 409

    # Rebuild on demand if route data is missing or malformed.
    comment = row['engineer_comment'] or ''
    if not comment.startswith('ROUTE:'):
        _recalc_route_for_request_id(rid, force=True)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT engineer_comment, travel_time FROM requests WHERE id=?", (rid,))
        fresh = cur.fetchone()
        conn.close()
        comment = (fresh['engineer_comment'] or '') if fresh else ''

    if not comment.startswith('ROUTE:'):
        return jsonify({'success': False, 'error': 'Не удалось рассчитать маршрут'}), 422
    raw = comment[6:]
    depth = 0
    end = -1
    for i, ch in enumerate(raw):
        if ch == '{': depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return jsonify({'success': False, 'error': 'Данные маршрута повреждены'}), 422
    try:
        route = json.loads(raw[:end])
    except Exception:
        return jsonify({'success': False, 'error': 'Не удалось прочитать данные маршрута'}), 422
    return jsonify({'success': True, 'request_id': rid, 'engineer_id': row['engineer_id'],
                    'route': route})


@app.route('/api/engineers', methods=['GET'])
def get_engineers():
    conn = get_db()
    sync_engineer_availability(conn)
    conn.commit()
    cur = conn.cursor()
    cur.execute("""SELECT e.id,e.name,e.device_id,e.specialization,
                   COALESCE(p.lat,e.coords_lat) AS coords_lat,
                   COALESCE(p.lng,e.coords_lng) AS coords_lng,
                   gps.lat AS gps_lat,gps.lng AS gps_lng,
                   gps_heading.lat AS gps_heading,gps_heading.lat AS heading,gps.updated_at AS gps_updated_at,
                   e.status,e.task,e.phone,e.task_step,e.task_started_at
                   FROM engineers e
                   LEFT JOIN map_object_positions p
                     ON p.object_type='engineer' AND p.object_id=e.id
                   LEFT JOIN map_object_positions gps
                     ON gps.object_type='engineer_gps' AND gps.object_id=e.id
                   LEFT JOIN map_object_positions gps_heading
                     ON gps_heading.object_type='engineer_gps_heading' AND gps_heading.object_id=e.id""")
    rows = cur.fetchall()
    conn.close()
    assignments = load_airport_assignments()
    selected_airport = current_airport_payload()['airport']
    res = []
    for r in rows:
        d = dict(r)
        d['airport'] = assigned_airport('engineers', d['id'], assignments)
        if d['airport'] != selected_airport:
            continue
        num = get_engineer_number_from_device(d.get('device_id'))
        d['number'] = num
        d['device_id'] = normalize_device_id(d.get('device_id'), num)
        # на случай, если колонка ещё NULL (старые записи)
        if not d.get('task_step'):
            d['task_step'] = 'idle'
        res.append(d)
    res.sort(key=lambda x: x.get('number') or 0)
    return jsonify(res)


@app.route('/api/engineers/me', methods=['GET'])
def get_my_engineer():
    eid = request.args.get('id')
    if not eid:
        return jsonify({'error': 'Не указан id'}), 400
    return get_engineer(eid)


@app.route('/api/engineers/<eng_id>', methods=['GET'])
def get_engineer(eng_id):
    conn = get_db()
    sync_engineer_availability(conn)
    conn.commit()
    cur = conn.cursor()
    cur.execute("""SELECT e.id,e.name,e.device_id,e.specialization,
                   COALESCE(p.lat,e.coords_lat) AS coords_lat,
                   COALESCE(p.lng,e.coords_lng) AS coords_lng,
                   gps.lat AS gps_lat,gps.lng AS gps_lng,
                   gps_heading.lat AS gps_heading,gps_heading.lat AS heading,gps.updated_at AS gps_updated_at,
                   e.status,e.task,e.phone,e.task_step,e.task_started_at
                   FROM engineers e
                   LEFT JOIN map_object_positions p
                     ON p.object_type='engineer' AND p.object_id=e.id
                   LEFT JOIN map_object_positions gps
                     ON gps.object_type='engineer_gps' AND gps.object_id=e.id
                   LEFT JOIN map_object_positions gps_heading
                     ON gps_heading.object_type='engineer_gps_heading' AND gps_heading.object_id=e.id
                   WHERE e.id=?""", (eng_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Не найден'}), 404
    d = dict(row)
    d['airport'] = assigned_airport('engineers', d['id'])
    if d['airport'] != current_airport_payload()['airport']:
        return jsonify({'error': 'Инженер относится к другому аэропорту'}), 404
    num = get_engineer_number_from_device(d.get('device_id'))
    d['number'] = num
    d['device_id'] = normalize_device_id(d.get('device_id'), num)
    if not d.get('task_step'):
        d['task_step'] = 'idle'
    return jsonify(d)


@app.route('/api/engineers', methods=['POST'])
def create_engineer():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    if not name or not email:
        return jsonify({'error': 'Имя и email обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email=?", (email,))
    if cur.fetchone():
        conn.close()
        return jsonify({'error': 'Email уже существует'}), 400
    num = next_engineer_number(conn)
    eid = f'eng_{num}'
    dev = f'EN-{num:03d}'
    cur.execute("SELECT COUNT(*) FROM engineers")
    total = cur.fetchone()[0]
    lat = 55.975 + (total % 6) * 0.0008
    lng = 37.410 + (total % 6) * 0.0008
    cur.execute("""INSERT INTO engineers (id,name,device_id,specialization,coords_lat,coords_lng,status,phone,task_step)
                   VALUES (?,?,?,?,?,?,?,?,'idle')""",
                (eid, name, dev, data.get('specialization', 'general'),
                 lat, lng, data.get('status', 'free'), data.get('phone', '')))
    cur.execute("""INSERT INTO users (id,email,password,name,role,avatar,status)
                   VALUES (?,?,'eng123',?,'engineer','🔧','offline')""",
                (eid, email, name))
    conn.commit()
    conn.close()
    set_assigned_airport('engineers', eid, data.get('airport', current_airport_payload()['airport']))
    return jsonify({'success': True, 'id': eid, 'device_id': dev, 'number': num})


@app.route('/api/engineers/<eng_id>', methods=['PUT'])
def update_engineer(eng_id):
    data = request.get_json() or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM engineers WHERE id=?", (eng_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'error': 'Не найден'}), 404
    work_status = data.get('work_status')
    if work_status not in (None, 'free', 'assigned', 'working'):
        conn.close()
        return jsonify({'error': 'Недопустимый статус инженера'}), 400
    if work_status == 'free':
        cur.execute("""UPDATE engineers SET name=?, specialization=?, status='free', task=NULL,
                       task_step='idle', task_started_at=NULL, phone=? WHERE id=?""",
                    (data.get('name'), data.get('specialization', 'general'), data.get('phone', ''), eng_id))
    elif work_status == 'assigned':
        cur.execute("""UPDATE engineers SET name=?, specialization=?, status='busy',
                       task_step='pending', task_started_at=NULL, phone=? WHERE id=?""",
                    (data.get('name'), data.get('specialization', 'general'), data.get('phone', ''), eng_id))
    elif work_status == 'working':
        cur.execute("""UPDATE engineers SET name=?, specialization=?, status='busy',
                       task_step='diagnosing', phone=? WHERE id=?""",
                    (data.get('name'), data.get('specialization', 'general'), data.get('phone', ''), eng_id))
    else:
        cur.execute("""UPDATE engineers SET name=?, specialization=?, status=?, task=?, phone=?
                       WHERE id=?""",
                    (data.get('name'), data.get('specialization', 'general'),
                     data.get('status', 'free'), data.get('task'), data.get('phone', ''), eng_id))
    if data.get('name'):
        cur.execute("UPDATE users SET name=? WHERE id=?", (data['name'], eng_id))
    conn.commit()
    conn.close()
    _recalc_routes_for_engineer(eng_id, force=True)
    if 'airport' in data:
        try:
            set_assigned_airport('engineers', eng_id, data['airport'])
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True})

@app.route('/api/requests/<rid>/accept', methods=['POST'])
def accept_request_assignment(rid):
    """Подтверждение назначения инженером. Только после него запускается маршрут и ETA."""
    data = request.get_json() or {}
    engineer_id = str(data.get('engineer_id') or '').strip()
    if not engineer_id:
        return jsonify({'success': False, 'error': 'Не указан инженер'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, engineer_id, engineer_name, aircraft, parking_id,
                          routing_stage, status
                   FROM requests WHERE id=?""", (rid,))
    req = cur.fetchone()
    if not req:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    if str(req['engineer_id'] or '') != engineer_id:
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка назначена другому инженеру'}), 403
    if req['routing_stage'] != 'technician':
        conn.close()
        return jsonify({'success': False, 'error': 'Заявка больше не находится у инженера'}), 409

    cur.execute("SELECT id, status, task, task_step FROM engineers WHERE id=?", (engineer_id,))
    engineer = cur.fetchone()
    if not engineer:
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер не найден'}), 404
    if not object_belongs_to_airport('engineers', engineer_id):
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер относится к другому аэропорту'}), 403

    if req['status'] in ('arriving', 'in_progress', 'working') and engineer['task_step'] in ('accepted', 'in_car', 'arrived', 'diagnosing'):
        conn.close()
        return jsonify({'success': True, 'already_accepted': True, 'request_id': rid})
    if req['status'] != 'assigned' or engineer['task_step'] not in ('pending', 'idle', '', None):
        conn.close()
        return jsonify({'success': False, 'error': 'Назначение уже обработано или недоступно'}), 409

    cur.execute("""UPDATE engineers SET status='busy', task_step='accepted',
                   task_started_at=CURRENT_TIMESTAMP, task=COALESCE(task, ?)
                   WHERE id=?""", ('ВС ' + (req['aircraft'] or ''), engineer_id))
    cur.execute("""UPDATE requests SET status='arriving', inspection_assigned_at=CURRENT_TIMESTAMP,
                   travel_time=0, using_car=0, walk_to_car=0, car_time=0, best_car=NULL
                   WHERE id=?""", (rid,))
    _log_routing_event(conn, rid, 'technician', req['engineer_name'] or engineer_id,
                       'Инженер принял назначение; маршрут запущен')
    _append_routing_history(conn, rid, {
        'at': _iso_ms(), 'stage': 'status', 'actor': req['engineer_name'] or engineer_id,
        'from': 'assigned', 'to': 'arriving', 'note': 'Инженер принял заявку',
    })
    conn.commit()
    conn.close()
    _log_event('status_change', req['engineer_name'] or engineer_id, rid, {
        'from': 'assigned', 'to': 'arriving', 'reason': 'Инженер принял назначение',
    })

    # On acceptance, refresh the preview as the active travel route.
    _recalc_route_for_request_id(rid, force=True)

    # Reserve the chosen special vehicle after the route is calculated. In a race,
    # recalculate once more so the route does not point to an already occupied car.
    for attempt in range(2):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT best_car FROM requests WHERE id=?", (rid,))
        found = cur.fetchone()
        best_car = found['best_car'] if found else None
        if not best_car:
            conn.close()
            break
        cur.execute("SELECT id FROM transport WHERE device_id=?", (best_car,))
        car_row = cur.fetchone()
        if not car_row or not object_belongs_to_airport('transport', car_row['id']):
            conn.close()
            _recalc_route_for_request_id(rid, force=True)
            break
        cur.execute("UPDATE transport SET assigned_to=? WHERE device_id=? AND assigned_to IS NULL",
                    (rid, best_car))
        reserved = cur.rowcount > 0
        conn.commit()
        conn.close()
        if reserved:
            break
        _recalc_route_for_request_id(rid, force=True)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT travel_time, using_car, walk_to_car, car_time, best_car, engineer_comment, inspection_assigned_at FROM requests WHERE id=?", (rid,))
    result = cur.fetchone()
    conn.close()
    return jsonify({
        'success': True, 'request_id': rid, 'route_started': True,
        'travel_time': result['travel_time'] if result else 0,
        'using_car': bool(result['using_car']) if result else False,
        'best_car': result['best_car'] if result else None,
        'inspection_assigned_at': result['inspection_assigned_at'] if result else None,
    })


@app.route('/api/engineers/<eng_id>/step', methods=['PUT'])
def update_engineer_step(eng_id):
    """Инженер сообщает свой текущий шаг: idle/pending/accepted/in_car/arrived/diagnosing/completed.
    При accepted — фиксируется task_started_at; при idle/completed — очищается."""
    data = request.get_json() or {}
    step = (data.get('step') or '').strip()
    valid = ('idle', 'pending', 'accepted', 'in_car', 'arrived', 'diagnosing', 'completed')
    if step not in valid:
        return jsonify({'success': False, 'error': 'Неверный step'}), 400
    if step == 'accepted':
        return jsonify({'success': False, 'error': 'Примите заявку через кнопку «Принять» в карточке задания'}), 409

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, task_step FROM engineers WHERE id=?", (eng_id,))
    engineer = cur.fetchone()
    if not engineer:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найден'}), 404
    # Нельзя перескочить кнопки «Принять / Отклонить» прямым вызовом шага.
    if step in ('in_car', 'arrived', 'diagnosing') and engineer['task_step'] in ('pending', 'idle', None, ''):
        conn.close()
        return jsonify({'success': False, 'error': 'Сначала примите назначение кнопкой «Принять»'}), 409
    order = ['pending', 'accepted', 'in_car', 'arrived', 'diagnosing', 'completed']
    current_step = str(engineer['task_step'] or '')
    if current_step in order and step in order:
        if order.index(step) < order.index(current_step) and step not in ('accepted',):
            conn.close()
            return jsonify({'success': False,
                            'error': 'Недопустимый обратный переход этапа: ' +
                                     current_step + ' → ' + step}), 409

    if step in ('idle', 'completed'):
        cur.execute(
            "UPDATE engineers SET task_step=?, task_started_at=NULL WHERE id=?",
            (step, eng_id))
    else:
        cur.execute("UPDATE engineers SET task_step=? WHERE id=?", (step, eng_id))

    # Заявка отражает этап работ: начало диагностики = «В работе».
    cur.execute("""SELECT id, status FROM requests WHERE engineer_id=?
                   AND routing_stage='technician'
                   AND status NOT IN ('completed','cancelled','flight_cancelled','draft','report_submitted','deleted')
                   ORDER BY created_at DESC LIMIT 1""", (eng_id,))
    active_req = cur.fetchone()
    if active_req and step in ('arrived', 'diagnosing') and active_req['status'] == 'arriving':
        cur.execute("UPDATE requests SET status='in_progress' WHERE id=?", (active_req['id'],))
        _log_event('status_change', 'Инженер', active_req['id'], {
            'from': 'arriving', 'to': 'in_progress',
            'reason': 'Инженер прибыл на место и начал работу',
        })
        _append_routing_history(conn, active_req['id'], {
            'at': _iso_ms(), 'stage': 'status', 'actor': 'Инженер',
            'from': 'arriving', 'to': 'in_progress', 'note': 'Инженер прибыл на место и начал работу',
        })

    conn.commit()
    conn.close()
    _log_event('engineer_step', 'Инженер', active_req['id'] if active_req else '', {
        'engineer_id': str(eng_id), 'from': current_step, 'to': step,
    })
    _recalc_routes_for_engineer(eng_id, force=True)
    return jsonify({'success': True, 'step': step})


@app.route('/api/engineers/<eng_id>', methods=['DELETE'])
def delete_engineer(eng_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM engineers WHERE id=?", (eng_id,))
    cur.execute("DELETE FROM users WHERE id=?", (eng_id,))
    cur.execute("DELETE FROM map_object_positions WHERE object_type IN ('engineer_gps','engineer_gps_heading') AND object_id=?", (str(eng_id),))
    conn.commit()
    conn.close()
    _recalc_routes_for_engineer(eng_id, force=True)
    return jsonify({'success': True})


@app.route('/api/engineers/<eng_id>/location', methods=['GET', 'PUT'])
@app.route('/api/engineers/<eng_id>/gps', methods=['GET', 'PUT'])
@app.route('/api/engineer/<eng_id>/location', methods=['GET', 'PUT'])
def engineer_gps_location(eng_id):
    """Отдельные реальные GPS-координаты мобильного устройства инженера.

    Хранятся отдельно от coords, которые используются симуляцией маршрутов
    и диспетчерской картой. Компьютерный экран читает только этот endpoint.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,name FROM engineers WHERE id=?", (eng_id,))
    engineer = cur.fetchone()
    if not engineer:
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер не найден'}), 404

    if request.method == 'GET':
        cur.execute("""SELECT lat,lng,updated_at
                       FROM map_object_positions
                       WHERE object_type='engineer_gps' AND object_id=?""", (str(eng_id),))
        row = cur.fetchone()
        cur.execute("""SELECT lat,lng FROM map_object_positions
                       WHERE object_type='engineer_gps_heading' AND object_id=?""", (str(eng_id),))
        heading_row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({'success': True, 'available': False, 'engineer_id': str(eng_id),
                            'name': engineer['name'], 'location': None})
        return jsonify({
            'success': True,
            'available': True,
            'engineer_id': str(eng_id),
            'name': engineer['name'],
            'location': {
                'lat': row['lat'],
                'lng': row['lng'],
                'heading': heading_row['lat'] if heading_row else None,
                'accuracy': heading_row['lng'] if heading_row else None,
                'updated_at': row['updated_at'],
            }
        })

    data = request.get_json(silent=True) or {}
    lat = data.get('lat')
    lng = data.get('lng')
    accuracy = data.get('accuracy')
    heading = data.get('heading')
    if lat is None or lng is None:
        conn.close()
        return jsonify({'success': False, 'error': 'lat/lng обязательны'}), 400
    try:
        lat = float(lat)
        lng = float(lng)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError('Координаты вне допустимого диапазона')
        if accuracy is not None:
            accuracy = float(accuracy)
            if not (0 <= accuracy <= 100000):
                raise ValueError('Некорректная точность GPS')
        if heading is not None:
            heading = float(heading) % 360.0
            if not (0 <= heading < 360):
                raise ValueError('Некорректный курс GPS')
    except (TypeError, ValueError):
        conn.close()
        return jsonify({'success': False, 'error': 'Некорректные координаты GPS'}), 400

    # Важно: GPS — фактическое положение, его нельзя блокировать правилами
    # симуляции движения самолётов или перезаписывать координатами маршрута.
    cur.execute("""INSERT INTO map_object_positions (object_type,object_id,lat,lng,updated_at)
                   VALUES ('engineer_gps',?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(object_type,object_id) DO UPDATE SET
                     lat=excluded.lat,lng=excluded.lng,updated_at=CURRENT_TIMESTAMP""",
                (str(eng_id), lat, lng))
    if heading is not None:
        # Keep compass metadata in a separate row in the existing table;
        # no database migration or schema alteration is required.
        cur.execute("""INSERT INTO map_object_positions (object_type,object_id,lat,lng,updated_at)
                       VALUES ('engineer_gps_heading',?,?,?,CURRENT_TIMESTAMP)
                       ON CONFLICT(object_type,object_id) DO UPDATE SET
                         lat=excluded.lat,lng=excluded.lng,updated_at=CURRENT_TIMESTAMP""",
                    (str(eng_id), heading, accuracy if accuracy is not None else 0.0))
    conn.commit()
    conn.close()
    _after_engineer_position(eng_id, lat, lng)
    return jsonify({'success': True, 'engineer_id': str(eng_id), 'lat': lat,
                    'lng': lng, 'accuracy': accuracy, 'heading': heading})


@app.route('/api/engineers/<eng_id>/coords', methods=['PUT'])
def update_engineer_coords(eng_id):
    data = request.get_json() or {}
    lat = data.get('lat')
    lng = data.get('lng')
    if lat is None or lng is None:
        return jsonify({'error': 'lat/lng обязательны'}), 400
    try:
        lat, lng = float(lat), float(lng)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError('Координаты вне допустимого диапазона')
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Некорректные координаты'}), 400
    conflicting_aircraft = _aircraft_taxi_conflict(lat, lng)
    if conflicting_aircraft:
        return jsonify({'success': False, 'error': 'Перемещение запрещено: рулит самолёт ' + conflicting_aircraft, 'safety_block': True}), 409
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE engineers SET coords_lat=?, coords_lng=? WHERE id=?",
                (lat, lng, eng_id))
    if cur.rowcount == 0:
        conn.close()
        return jsonify({'success': False, 'error': 'Инженер не найден'}), 404
    cur.execute("""INSERT INTO map_object_positions (object_type,object_id,lat,lng,updated_at)
                   VALUES ('engineer',?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(object_type,object_id) DO UPDATE SET
                     lat=excluded.lat,lng=excluded.lng,updated_at=CURRENT_TIMESTAMP""",
                (str(eng_id), lat, lng))
    conn.commit()
    conn.close()
    _recalc_routes_for_engineer(eng_id, force=True)
    _after_engineer_position(eng_id, lat, lng, recalc_route=False)
    return jsonify({'success': True, 'object_type': 'engineer', 'id': str(eng_id),
                    'lat': lat, 'lng': lng})


# ============================================================
# Финальный этап: геопозиция агентов, динамический маршрут,
# норматив 15 минут и проактивные предупреждения
# ============================================================
@app.route('/api/engineers/<eng_id>/tools', methods=['GET', 'PUT'])
def engineer_tools_status(eng_id):
    """Engineer-controlled tool availability. No DB schema changes."""
    if request.method == 'GET':
        status, item = get_engineer_tool_status(eng_id)
        return jsonify({'success': True, 'engineer_id': str(eng_id),
                        'status': status, 'item': item})
    data = request.get_json(silent=True) or {}
    try:
        item = set_engineer_tool_status(eng_id, data.get('status') or 'unknown',
                                        data.get('comment') or '')
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    try:
        _recalc_routes_for_engineer(eng_id, force=True)
        conn = get_db()
        active_row = _active_request_for_engineer(conn, eng_id)
        conn.close()
        if active_row:
            _refresh_risk_for_request(active_row['id'])
    except Exception as exc:
        print(f'[TOOLS] route recalc: {exc}')
    return jsonify({'success': True, 'engineer_id': str(eng_id),
                    'status': item['status'], 'item': item})


def _parse_db_dt(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sql_time(value=None):
    dt = _parse_db_dt(value) or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _parse_route_payload(comment):
    text = str(comment or '')
    if not text.startswith('ROUTE:'):
        return None
    payload = text[6:]
    depth = 0
    end = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(payload):
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end <= 0:
        return None
    try:
        data = json.loads(payload[:end])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _active_request_for_engineer(conn, engineer_id):
    cur = conn.cursor()
    cur.execute("""SELECT * FROM requests
                   WHERE engineer_id=?
                     AND routing_stage='technician'
                     AND status NOT IN ('completed','cancelled','flight_cancelled','draft','report_submitted','deleted')
                   ORDER BY created_at DESC LIMIT 1""", (str(engineer_id),))
    return cur.fetchone()


def _norm_info(conn, row):
    now = datetime.now(timezone.utc)
    created = _parse_db_dt(row['created_at']) or now
    deadline = created + timedelta(minutes=NORM_LIMIT_MINUTES)
    eta = float(row['travel_time'] or 0)
    route = _parse_route_payload(row['engineer_comment'])
    if route and route.get('tt') is not None:
        try:
            eta = float(route.get('tt') or 0)
        except (TypeError, ValueError):
            pass
    eta = max(0.0, eta)

    arrived_at = None
    try:
        history = json.loads(row['routing_history'] or '[]')
        if isinstance(history, list):
            for entry in history:
                if isinstance(entry, dict) and entry.get('stage') == 'arrival' and entry.get('at'):
                    arrived_at = _parse_db_dt(entry.get('at'))
                    if arrived_at:
                        break
    except (TypeError, ValueError):
        arrived_at = None

    if arrived_at:
        actual_min = (arrived_at - created).total_seconds() / 60.0
        forecast = arrived_at
        remaining = 0.0
        risk = False
        late = actual_min > NORM_LIMIT_MINUTES
        eta = actual_min
    else:
        forecast = now + timedelta(minutes=eta)
        remaining = (deadline - now).total_seconds() / 60.0
        risk = forecast > deadline
        late = now > deadline
    level = 'critical' if late else ('warning' if risk else 'ok')
    if arrived_at and not late:
        level = 'ok'
    return {
        'request_id': str(row['id']),
        'aircraft': row['aircraft'] or '—',
        'parking_id': row['parking_id'] or '—',
        'source': row['source'] or '',
        'status': row['status'] or '',
        'engineer_id': str(row['engineer_id'] or ''),
        'engineer_name': row['engineer_name'] or '',
        'eta_min': round(eta, 1),
        'created_at': _iso_ms(created),
        'deadline_at': _iso_ms(deadline),
        'forecast_at': _iso_ms(forecast),
        'remaining_min': round(remaining, 1),
        'arrived_at': _iso_ms(arrived_at) if arrived_at else None,
        'norm_minutes': round((arrived_at - created).total_seconds() / 60.0, 1) if arrived_at else None,
        'risk': risk,
        'late': late,
        'level': level,
    }


def _point_in_ring(lat, lng, ring):
    inside = False
    if not ring:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        yi, xi = ring[i][1], ring[i][0]
        yj, xj = ring[j][1], ring[j][0]
        if ((yi > lat) != (yj > lat)) and (lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _vdnh_point_in_zone(zone_id, lat, lng):
    try:
        with open(os.path.join(STATIC_DIR, 'vdnh', 'service_zones.geojson'), 'r', encoding='utf-8') as f:
            features = json.load(f).get('features', [])
        for feature in features:
            props = feature.get('properties') or {}
            if str(props.get('id')) != str(zone_id):
                continue
            geom = feature.get('geometry') or {}
            coords = geom.get('coordinates') or []
            polygons = coords if geom.get('type') == 'MultiPolygon' else [coords]
            for polygon in polygons:
                if not polygon:
                    continue
                if _point_in_ring(lat, lng, polygon[0] or []):
                    if not any(_point_in_ring(lat, lng, hole or []) for hole in polygon[1:]):
                        return True
    except (OSError, ValueError, TypeError):
        return False
    return False


def _record_arrival_geofence(conn, row, lat, lng):
    target = _request_target_coords(row['parking_id'], row['source'])
    if target is None:
        return False
    arrived = False
    try:
        if current_airport_payload().get('airport') == 'vdnh':
            arrived = _vdnh_point_in_zone(row['parking_id'], lat, lng)
    except Exception:
        arrived = False
    if not arrived:
        arrived = distance_km(lat, lng, target[0], target[1]) * 1000.0 <= 45.0
    if not arrived:
        return False

    cur = conn.cursor()
    cur.execute("SELECT routing_history FROM requests WHERE id=?", (row['id'],))
    history_row = cur.fetchone()
    try:
        history = json.loads(history_row['routing_history'] or '[]') if history_row else []
    except Exception:
        history = []
    if not isinstance(history, list):
        history = []
    if any(isinstance(e, dict) and (e.get('stage') == 'arrival' or e.get('type') == 'arrival') for e in history):
        return False

    now = datetime.now(timezone.utc)
    created = _parse_db_dt(row['created_at']) or now
    norm_minutes = (now - created).total_seconds() / 60.0
    late = norm_minutes > NORM_LIMIT_MINUTES
    history.append({
        'at': _iso_ms(now),
        'stage': 'arrival',
        'actor': 'GPS',
        'note': 'Прибытие в зону обслуживания зафиксировано автоматически',
    })
    cur.execute("UPDATE requests SET routing_history=? WHERE id=?",
                (json.dumps(history, ensure_ascii=False), row['id']))
    if str(row['status'] or '') == 'arriving':
        cur.execute("UPDATE requests SET status='in_progress' WHERE id=?", (row['id'],))
        _log_event('status_change', 'GPS', row['id'], {
            'from': 'arriving', 'to': 'in_progress',
            'reason': 'Автоматическая фиксация прибытия в зону обслуживания',
        })
    _log_event('arrival', 'GPS', row['id'], {
        'lat': lat, 'lng': lng, 'norm_minutes': round(norm_minutes, 1),
        'limit_minutes': NORM_LIMIT_MINUTES, 'late': late,
    })
    _log_event('norm_violation' if late else 'norm_ok', 'GPS', row['id'], {
        'norm_minutes': round(norm_minutes, 1), 'limit_minutes': NORM_LIMIT_MINUTES, 'late': late,
    })
    _RISK_WARNINGS.pop(str(row['id']), None)
    return True


def _maybe_dynamic_route(eng_id, lat, lng, force=False):
    now = time.time()
    key = str(eng_id)
    state = _ROUTE_RECALC_STATE.get(key) or {}
    moved = None
    if state.get('lat') is not None and state.get('lng') is not None:
        moved = distance_km(float(state['lat']), float(state['lng']), lat, lng) * 1000.0
    if not force and state and now - float(state.get('t') or 0) < 2.5 and (moved is None or moved < 8.0):
        return
    conn = get_db()
    row = _active_request_for_engineer(conn, eng_id)
    conn.close()
    if not row:
        return
    _recalc_route_for_request_id(row['id'], force=True)
    _ROUTE_RECALC_STATE[key] = {'lat': lat, 'lng': lng, 't': now}


def _refresh_risk_for_request(rid):
    rid = str(rid)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if (not row or row['routing_stage'] != 'technician' or not row['engineer_id'] or
            row['status'] in ('completed', 'cancelled', 'flight_cancelled', 'draft', 'report_submitted', 'deleted')):
        _RISK_WARNINGS.pop(rid, None)
        conn.close()
        return None
    norm = _norm_info(conn, row)
    if not norm or not norm['risk']:
        _RISK_WARNINGS.pop(rid, None)
        conn.close()
        return None

    deadline = _parse_db_dt(norm['deadline_at'])
    alternative = None
    try:
        ranking = _dispatcher_engineer_recommendations(conn, dict(row), limit=5)
        for candidate in ranking.get('candidates', []):
            if str(candidate.get('engineer_id')) == str(row['engineer_id']):
                continue
            eta = float(candidate.get('travel_time') or 0)
            arrives = datetime.now(timezone.utc) + timedelta(minutes=eta)
            fits = bool(deadline and arrives <= deadline)
            alternative = {
                'engineer_id': candidate.get('engineer_id'),
                'engineer_name': candidate.get('engineer_name'),
                'eta_min': round(eta, 1),
                'arrives_at': _iso_ms(arrives),
                'fits': fits,
            }
            if fits:
                break
    except Exception as exc:
        print(f'[RISK] alternative: {exc}')

    deadline_text = _parse_db_dt(norm['deadline_at'])
    forecast_text = _parse_db_dt(norm['forecast_at'])
    fmt = lambda d: d.astimezone().strftime('%H:%M:%S') if d else '—'
    message = ('Заявка %s (%s, стоянка %s): прогноз прибытия %s, норматив до %s, запас %s мин.'
               % (rid, norm['aircraft'], norm['parking_id'], fmt(forecast_text), fmt(deadline_text),
                  norm['remaining_min']))
    if norm['late']:
        message = 'НОРМАТИВ УЖЕ НАРУШЕН. ' + message
    if alternative:
        if alternative['fits']:
            message += ' Может успеть %s: ETA %s мин.' % (alternative['engineer_name'], alternative['eta_min'])
        else:
            message += ' Свободных инженеров, успевающих в норматив, нет.'
    warning = {
        'request_id': rid,
        'type': 'norm_risk',
        'level': norm['level'],
        'aircraft': norm['aircraft'],
        'parking_id': norm['parking_id'],
        'engineer_id': norm['engineer_id'],
        'engineer_name': norm['engineer_name'],
        'deadline_at': norm['deadline_at'],
        'forecast_at': norm['forecast_at'],
        'remaining_min': norm['remaining_min'],
        'eta_min': norm['eta_min'],
        'late': norm['late'],
        'message': message,
        'alternative': alternative,
    }
    _RISK_WARNINGS[rid] = warning
    state = _RISK_WARNING_STATE.get(rid) or {}
    if time.time() - float(state.get('logged') or 0) > 60.0:
        _log_event('risk', 'Система', rid, warning)
        _RISK_WARNING_STATE[rid] = {'logged': time.time()}
    conn.close()
    return warning


def _log_data_warning_once(rid, warning_type, message, details=None, throttle_sec=60.0):
    key = str(rid) + ':' + str(warning_type)
    now = time.time()
    state = _DATA_WARNING_STATE.get(key) or {}
    if now - float(state.get('t') or 0) < throttle_sec:
        return
    _DATA_WARNING_STATE[key] = {'t': now}
    payload = {'message': message, 'warning_type': warning_type}
    if details:
        payload.update(details)
    _log_event('data_warning', 'Система', rid, payload)


def _refresh_all_norms():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT * FROM requests
                   WHERE routing_stage='technician'
                     AND engineer_id IS NOT NULL
                     AND status NOT IN ('completed','cancelled','flight_cancelled','draft','report_submitted','deleted')
                   ORDER BY created_at ASC""")
    rows = [dict(r) for r in cur.fetchall()]
    norms = []
    warnings = []
    for row in rows:
        norm = _norm_info(conn, row)
        warning = _refresh_risk_for_request(row['id'])
        if warning:
            warnings.append(warning)
            norm['warning'] = warning

        data_warnings = []
        cur.execute("""SELECT lat,lng,updated_at FROM map_object_positions
                       WHERE object_type='engineer_gps' AND object_id=?""", (str(row['engineer_id']),))
        gps = cur.fetchone()
        if gps and gps['updated_at']:
            updated = _parse_db_dt(gps['updated_at'])
            if updated:
                age = (datetime.now(timezone.utc) - updated).total_seconds()
                if age > 30:
                    msg = 'GPS инженера %s не обновлялся %d сек (последняя позиция %s).' % (
                        row['engineer_name'] or row['engineer_id'], int(age),
                        updated.astimezone().strftime('%H:%M:%S'))
                    data_warnings.append({'type': 'stale_gps', 'message': msg, 'age_sec': round(age, 1)})
                    warnings.append({'request_id': str(row['id']), 'type': 'stale_gps',
                                     'level': 'warning', 'message': msg,
                                     'engineer_id': str(row['engineer_id'])})
                    _log_data_warning_once(row['id'], 'stale_gps', msg,
                                           {'age_sec': round(age, 1),
                                            'last_position_at': _iso_ms(updated)})
        route_payload = _parse_route_payload(row['engineer_comment'])
        if not route_payload:
            msg = 'Для заявки %s ещё не рассчитан маршрут.' % row['id']
            data_warnings.append({'type': 'route_missing', 'message': msg})
            warnings.append({'request_id': str(row['id']), 'type': 'route_missing',
                             'level': 'warning', 'message': msg,
                             'engineer_id': str(row['engineer_id'])})
        elif gps and gps['updated_at']:
            path = route_payload.get('path') or []
            if len(path) >= 2:
                min_dist = min(
                    _point_segment_distance_m(float(gps['lat']), float(gps['lng']),
                                              (float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
                    for a, b in zip(path, path[1:])
                )
                if min_dist > 120:
                    msg = ('Инженер %s отклонился от маршрута на %d м (рассогласование данных).'
                           % (row['engineer_name'] or row['engineer_id'], int(min_dist)))
                    data_warnings.append({'type': 'route_deviation', 'message': msg,
                                          'deviation_m': round(min_dist, 1)})
                    warnings.append({'request_id': str(row['id']), 'type': 'route_deviation',
                                     'level': 'warning', 'message': msg,
                                     'engineer_id': str(row['engineer_id']),
                                     'deviation_m': round(min_dist, 1)})
                    _log_data_warning_once(row['id'], 'route_deviation', msg,
                                           {'deviation_m': round(min_dist, 1)})
        tool_status, _tool_item = get_engineer_tool_status(row['engineer_id'])
        if tool_status == 'unknown' and _request_needs_tools(row):
            msg = 'Статус инструмента инженера %s не подтверждён; ETA учитывает +30 с на складе.' % (
                row['engineer_name'] or row['engineer_id'])
            data_warnings.append({'type': 'tool_unknown', 'message': msg})
            warnings.append({'request_id': str(row['id']), 'type': 'tool_unknown',
                             'level': 'info', 'message': msg,
                             'engineer_id': str(row['engineer_id'])})
        if data_warnings:
            norm['data_warnings'] = data_warnings
        norms.append(norm)
    conn.close()
    return norms, warnings


def _after_engineer_position(eng_id, lat, lng, recalc_route=True):
    try:
        conn = get_db()
        row = _active_request_for_engineer(conn, eng_id)
        if row:
            _record_arrival_geofence(conn, row, lat, lng)
            conn.commit()
        conn.close()
        if row:
            if recalc_route:
                _maybe_dynamic_route(eng_id, lat, lng)
            _refresh_risk_for_request(row['id'])
    except Exception as exc:
        print(f'[FINAL GPS] {exc}')


def _after_transport_position(tid, lat, lng):
    try:
        now = time.time()
        key = 'transport_' + str(tid)
        state = _ROUTE_RECALC_STATE.get(key) or {}
        if state and now - float(state.get('t') or 0) < 2.5:
            return
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT assigned_to FROM transport WHERE id=?", (tid,))
        row = cur.fetchone()
        conn.close()
        if row and row['assigned_to']:
            _recalc_route_for_request_id(row['assigned_to'], force=True)
            _refresh_risk_for_request(row['assigned_to'])
        _ROUTE_RECALC_STATE[key] = {'t': now}
    except Exception as exc:
        print(f'[FINAL TRANSPORT] {exc}')


def _load_track_object_map():
    path = os.path.join(BASE_DIR, 'data', 'track_objects.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _resolve_track_object(conn, obj):
    raw = str(obj or '').strip()
    if not raw:
        return None, None
    cur = conn.cursor()
    lower = raw.lower()

    mapped = _load_track_object_map().get(raw)
    if mapped:
        lower = str(mapped).strip().lower()
        raw_lookup = str(mapped).strip()
    else:
        raw_lookup = raw

    row = cur.execute("SELECT id,device_id,name FROM engineers WHERE id=? OR device_id=? OR lower(id)=? OR lower(device_id)=?",
                      (raw_lookup, raw_lookup, lower, lower)).fetchone()
    if row:
        return 'engineer', row['id']

    match = re.match(r'^(?:e|eng)[-_]?0*(\d+)$', lower)
    if match:
        number = int(match.group(1))
        for candidate in ('EN-%03d' % number, 'eng_%d' % number, str(number)):
            row = cur.execute("SELECT id FROM engineers WHERE id=? OR device_id=?", (candidate, candidate)).fetchone()
            if row:
                return 'engineer', row['id']

    row = cur.execute("SELECT id,device_id FROM transport WHERE CAST(id AS TEXT)=? OR lower(device_id)=?",
                      (lower, lower)).fetchone()
    if row:
        return 'transport', row['id']
    match = re.match(r'^v[-_]?0*(\d+)$', lower)
    if match:
        number = int(match.group(1))
        for candidate in (number, 'CAR-%03d' % number, 'v-%02d' % number):
            row = cur.execute("SELECT id FROM transport WHERE CAST(id AS TEXT)=? OR lower(device_id)=?",
                              (str(candidate), str(candidate).lower())).fetchone()
            if row:
                return 'transport', row['id']

    row = cur.execute("SELECT id,callsign FROM aircraft WHERE id=? OR lower(callsign)=?",
                      (raw_lookup, lower)).fetchone()
    if row:
        return 'aircraft', row['id']
    if raw.upper().startswith('RA-'):
        return 'aircraft_new', raw.upper()
    return None, None


def _record_aircraft_arrival(obj, lat, lng, at):
    """Log aircraft reaching the parking area when it is visible from track data."""
    callsign = str(obj or '').strip()
    if not callsign:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""SELECT id, aircraft, parking_id, source, routing_history
                       FROM requests
                       WHERE UPPER(COALESCE(aircraft,''))=UPPER(?)
                         AND status NOT IN ('completed','cancelled','flight_cancelled','deleted','draft')
                       ORDER BY created_at DESC LIMIT 20""", (callsign,))
        for row in cur.fetchall():
            key = str(row['id'])
            if _AIRCRAFT_ARRIVAL_STATE.get(key):
                continue
            target = _request_target_coords(row['parking_id'], row['source'])
            if target is None:
                continue
            if distance_km(lat, lng, target[0], target[1]) * 1000.0 > 80.0:
                continue
            try:
                history = json.loads(row['routing_history'] or '[]')
            except (TypeError, ValueError):
                history = []
            if not isinstance(history, list):
                history = []
            history.append({'at': _iso_ms(at), 'stage': 'aircraft_arrival', 'actor': 'GPS',
                            'note': 'ВС в зоне стоянки по данным трека'})
            cur.execute("UPDATE requests SET routing_history=? WHERE id=?",
                        (json.dumps(history, ensure_ascii=False), key))
            conn.commit()
            _AIRCRAFT_ARRIVAL_STATE[key] = time.time()
            _log_event('aircraft_arrival', 'GPS', key, {
                'aircraft': callsign, 'lat': lat, 'lng': lng,
                'parking_id': row['parking_id'],
                'source': 'фактическое положение ВС',
            })
        conn.close()
    except Exception as exc:
        print(f'[AIRCRAFT ARRIVAL] {exc}')


def _apply_track_point(point):
    obj = str(point.get('obj') or '')
    lat = float(point['lat'])
    lng = float(point['lng'])
    t = _iso_ms(point.get('t') or None)
    t_sql = _sql_time(t)
    spd = point.get('spd')
    acc = point.get('acc')
    conn = get_db()
    applied = False
    kind = None
    ident = None
    try:
        kind, ident = _resolve_track_object(conn, obj)
        if not kind:
            _log_event('track_error', 'GPS', '', {
                'error': 'объект не найден', 'obj': obj, 'lat': lat, 'lon': lng,
            })
            return {'accepted': True, 'applied': False, 'obj': obj, 't': t}
        cur = conn.cursor()
        if kind == 'engineer':
            cur.execute("""INSERT INTO map_object_positions (object_type,object_id,lat,lng,updated_at)
                           VALUES ('engineer_gps',?,?,?,?)
                           ON CONFLICT(object_type,object_id) DO UPDATE SET
                             lat=excluded.lat,lng=excluded.lng,updated_at=excluded.updated_at""",
                        (str(ident), lat, lng, t_sql))
            applied = True
        elif kind == 'transport':
            speed_kmh = None
            if spd is not None:
                try:
                    parsed_speed = float(spd) * 3.6
                    if parsed_speed > 0.5:
                        speed_kmh = round(parsed_speed, 1)
                except (TypeError, ValueError):
                    speed_kmh = None
            cur.execute("UPDATE transport SET lat=?, lng=?, speed=COALESCE(?, speed) WHERE id=?",
                        (lat, lng, speed_kmh, ident))
            cur.execute("""INSERT INTO map_object_positions (object_type,object_id,lat,lng,updated_at)
                           VALUES ('transport',?,?,?,?)
                           ON CONFLICT(object_type,object_id) DO UPDATE SET
                             lat=excluded.lat,lng=excluded.lng,updated_at=excluded.updated_at""",
                        (str(ident), lat, lng, t_sql))
            if speed_kmh is not None:
                cur.execute("UPDATE transport SET speed=? WHERE id=?", (speed_kmh, ident))
            applied = cur.rowcount > 0
        elif kind in ('aircraft', 'aircraft_new'):
            aircraft_id = ident
            if kind == 'aircraft_new':
                aircraft_id = 'trk_' + hashlib.sha1(obj.encode('utf-8')).hexdigest()[:12]
            cur.execute("SELECT id FROM aircraft WHERE id=? OR lower(callsign)=?",
                        (aircraft_id, obj.lower()))
            existing = cur.fetchone()
            if existing:
                cur.execute("UPDATE aircraft SET lat=?, lng=?, status='tracked', updated_at=? WHERE id=?",
                            (lat, lng, t_sql, existing['id']))
            else:
                cur.execute("INSERT INTO aircraft (id,callsign,lat,lng,status,updated_at) VALUES (?,?,?,?,?,?)",
                            (aircraft_id, obj, lat, lng, 'tracked', t_sql))
            cur.execute("""INSERT INTO map_object_positions (object_type,object_id,lat,lng,updated_at)
                           VALUES ('aircraft_gps',?,?,?,?)
                           ON CONFLICT(object_type,object_id) DO UPDATE SET
                             lat=excluded.lat,lng=excluded.lng,updated_at=excluded.updated_at""",
                        (str(aircraft_id), lat, lng, t_sql))
            applied = True
        conn.commit()
        _log_event('track', 'GPS', '', {
            'obj': obj, 'kind': kind, 'internal_id': str(ident), 'lat': lat, 'lng': lng,
            'spd': spd, 'acc': acc, 'source_t': point.get('t'),
        })
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        _log_event('track_error', 'GPS', '', {'obj': obj, 'lat': lat, 'lng': lng, 'error': str(exc)})
        return {'accepted': True, 'applied': False, 'obj': obj, 't': t}
    finally:
        conn.close()

    if applied:
        try:
            if kind == 'engineer':
                _after_engineer_position(ident, lat, lng)
            elif kind == 'transport':
                _after_transport_position(ident, lat, lng)
            elif kind in ('aircraft', 'aircraft_new'):
                _record_aircraft_arrival(obj, lat, lng, t)
        except Exception as exc:
            print(f'[FINAL TRACK] {exc}')
    return {'accepted': True, 'applied': applied, 'obj': obj, 't': t}


@app.route('/api/v1/track', methods=['POST'])
def api_v1_track():
    """Контракт приёма геопозиции агентов из приложения к заданию."""
    payload = request.get_json(silent=True)
    single = True
    if isinstance(payload, list):
        points = payload
        single = False
    elif isinstance(payload, dict) and isinstance(payload.get('points'), list):
        points = payload['points']
        single = False
    elif isinstance(payload, dict):
        points = [payload]
    else:
        return jsonify({'accepted': False, 'error': 'Ожидается JSON-объект или массив точек'}), 400
    prepared = []
    for index, item in enumerate(points):
        if not isinstance(item, dict):
            return jsonify({'accepted': False, 'error': 'Точка должна быть объектом', 'index': index}), 400
        try:
            lat = float(item.get('lat'))
            # Контракт положения использует lon; lng принимаем для совместимости со старыми клиентами.
            lng = float(item.get('lon') if item.get('lon') is not None else item.get('lng'))
        except (TypeError, ValueError):
            _log_event('track_error', 'API', '', {'error': 'lat/lon не числа', 'index': index,
                                                   'obj': item.get('obj')})
            return jsonify({'accepted': False, 'error': 'lat/lon обязательны и должны быть числами', 'index': index}), 400
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            _log_event('track_error', 'API', '', {'error': 'координаты вне диапазона', 'index': index,
                                                   'obj': item.get('obj'), 'lat': lat, 'lon': lng})
            return jsonify({'accepted': False, 'error': 'Координаты вне допустимого диапазона', 'index': index}), 400
        if not str(item.get('obj') or '').strip():
            _log_event('track_error', 'API', '', {'error': 'obj отсутствует', 'index': index})
            return jsonify({'accepted': False, 'error': 'obj обязателен', 'index': index}), 400
        prepared.append({
            'obj': str(item.get('obj')).strip(),
            'lat': lat,
            'lng': lng,
            't': item.get('t'),
            'acc': item.get('acc'),
            'spd': item.get('spd'),
        })
    results = [_apply_track_point(p) for p in prepared]
    return jsonify(results[0] if single and results else results)


@app.route('/api/dispatcher/norms', methods=['GET'])
def dispatcher_norms():
    norms, warnings = _refresh_all_norms()
    return jsonify({'success': True, 'norms': norms, 'warnings': warnings,
                    'count': len(norms)})


@app.route('/api/dispatcher/warnings', methods=['GET'])
def dispatcher_warnings():
    _norms, warnings = _refresh_all_norms()
    return jsonify({'success': True, 'warnings': warnings, 'count': len(warnings)})

# ============================================================
# Pilots
# ============================================================
@app.route('/api/pilots', methods=['GET'])
def get_pilots():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,name,email,phone,aircraft,rank,status FROM pilots ORDER BY name")
    res = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(res)


@app.route('/api/pilots', methods=['POST'])
def create_pilot():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    if not name or not email:
        return jsonify({'error': 'Имя и email обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM pilots WHERE email=?", (email,))
    if cur.fetchone():
        conn.close()
        return jsonify({'error': 'Email уже существует'}), 400
    pid = 'pilot_' + str(int(time.time() * 1000))
    cur.execute("""INSERT INTO pilots (id,name,email,phone,aircraft,rank,status)
                   VALUES (?,?,?,?,?,?,?)""",
                (pid, name, email, data.get('phone', ''), data.get('aircraft', ''),
                 data.get('rank', 'pilot'), data.get('status', 'active')))
    cur.execute("""INSERT INTO users (id,email,password,name,role,avatar,status)
                   VALUES (?,?,'pilot123',?,'pilot','👨‍✈️','offline')""",
                (pid, email, name))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'id': pid})


@app.route('/api/pilots/<pid>', methods=['PUT'])
def update_pilot(pid):
    data = request.get_json() or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM pilots WHERE id=?", (pid,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'error': 'Не найден'}), 404
    cur.execute("""UPDATE pilots SET name=?, phone=?, aircraft=?, rank=?, status=? WHERE id=?""",
                (data.get('name'), data.get('phone', ''), data.get('aircraft', ''),
                 data.get('rank', 'pilot'), data.get('status', 'active'), pid))
    if data.get('name'):
        cur.execute("UPDATE users SET name=? WHERE id=?", (data['name'], pid))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/pilots/<pid>', methods=['DELETE'])
def delete_pilot(pid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM pilots WHERE id=?", (pid,))
    cur.execute("DELETE FROM users WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# Transport
# ============================================================
@app.route('/api/aircraft', methods=['GET'])
def get_aircraft():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id,callsign,lat,lng,heading,speed,status,updated_at
                   FROM aircraft
                   WHERE callsign LIKE 'RA-%' OR status='tracked'
                   ORDER BY callsign""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/transport', methods=['GET'])
def get_transport():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT t.id,t.device_id,COALESCE(p.lat,t.lat) AS lat,
                   COALESCE(p.lng,t.lng) AS lng,t.speed,t.assigned_to
                   FROM transport t LEFT JOIN map_object_positions p
                     ON p.object_type='transport' AND p.object_id=CAST(t.id AS TEXT)
                   ORDER BY t.id""")
    res = [dict(r) for r in cur.fetchall()]
    conn.close()
    assignments = load_airport_assignments()
    selected_airport = current_airport_payload()['airport']
    for item in res:
        item['airport'] = assigned_airport('transport', item['id'], assignments)
    return jsonify([item for item in res if item['airport'] == selected_airport])


@app.route('/api/transport', methods=['POST'])
def create_transport():
    data = request.get_json() or {}
    dev = (data.get('device_id') or '').strip()
    lat = data.get('lat')
    lng = data.get('lng')
    if not dev or lat is None or lng is None:
        return jsonify({'error': 'device_id, lat, lng обязательны'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM transport WHERE device_id=?", (dev,))
    if cur.fetchone():
        conn.close()
        return jsonify({'error': 'device_id уже существует'}), 400
    cur.execute("""INSERT INTO transport (device_id,lat,lng,speed,assigned_to)
                   VALUES (?,?,?,?,?)""",
                (dev, lat, lng, data.get('speed', 50), data.get('assigned_to')))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    set_assigned_airport('transport', tid, data.get('airport', current_airport_payload()['airport']))
    return jsonify({'success': True, 'id': tid})


@app.route('/api/transport/<int:tid>', methods=['PUT'])
def update_transport(tid):
    data = request.get_json() or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM transport WHERE id=?", (tid,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'error': 'Не найден'}), 404
    cur.execute("""UPDATE transport SET device_id=?, lat=?, lng=?, speed=?, assigned_to=?
                   WHERE id=?""",
                (data.get('device_id'), data.get('lat'), data.get('lng'),
                 data.get('speed', 50), data.get('assigned_to'), tid))
    conn.commit()
    conn.close()
    if 'airport' in data:
        try:
            set_assigned_airport('transport', tid, data['airport'])
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True})


@app.route('/api/transport/<int:tid>/coords', methods=['PUT'])
def update_transport_coords(tid):
    data = request.get_json() or {}
    lat = data.get('lat')
    lng = data.get('lng')
    if lat is None or lng is None:
        return jsonify({'error': 'lat/lng обязательны'}), 400
    try:
        lat, lng = float(lat), float(lng)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError('Координаты вне допустимого диапазона')
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Некорректные координаты'}), 400
    conflicting_aircraft = _aircraft_taxi_conflict(lat, lng)
    if conflicting_aircraft:
        return jsonify({'success': False, 'error': 'Перемещение запрещено: рулит самолёт ' + conflicting_aircraft, 'safety_block': True}), 409
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, assigned_to FROM transport WHERE id=?", (tid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Не найден'}), 404
    if not object_belongs_to_airport('transport', tid):
        conn.close()
        return jsonify({'success': False, 'error': 'Транспорт относится к другому аэропорту и недоступен в текущем режиме'}), 403
    if 'speed' in data and data['speed'] is not None:
        cur.execute("UPDATE transport SET lat=?, lng=?, speed=? WHERE id=?",
                    (lat, lng, data['speed'], tid))
    else:
        cur.execute("UPDATE transport SET lat=?, lng=? WHERE id=?", (lat, lng, tid))
    cur.execute("""INSERT INTO map_object_positions (object_type,object_id,lat,lng,updated_at)
                   VALUES ('transport',?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(object_type,object_id) DO UPDATE SET
                     lat=excluded.lat,lng=excluded.lng,updated_at=CURRENT_TIMESTAMP""",
                (str(tid), lat, lng))
    conn.commit()
    assigned_to = row['assigned_to']
    conn.close()
    if assigned_to:
        _recalc_route_for_request_id(assigned_to, force=True)
        _refresh_risk_for_request(assigned_to)
    return jsonify({'success': True, 'object_type': 'transport', 'id': str(tid),
                    'lat': lat, 'lng': lng})


@app.route('/api/transport/<int:tid>', methods=['DELETE'])
def delete_transport(tid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM transport WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# Журнал событий финального этапа (append-only JSONL -> CSV/XLSX)
# ============================================================
@app.route('/api/event-log', methods=['GET'])
def export_event_log():
    events = _read_event_log()
    fmt = str(request.args.get('format') or 'csv').lower()
    if fmt == 'json':
        return jsonify({'success': True, 'count': len(events), 'events': events})
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['time', 'type', 'actor', 'request_id', 'details'])
    for event in events:
        writer.writerow([
            event.get('t', ''), event.get('type', ''), event.get('actor', ''),
            event.get('request_id', ''),
            json.dumps(event.get('details') or {}, ensure_ascii=False),
        ])
    data = '﻿' + buf.getvalue()
    buf.close()
    return Response(data, mimetype='text/csv; charset=utf-8', headers={
        'Content-Disposition': 'attachment; filename=InTrAvia_event_log.csv'
    })


@app.route('/api/event-log.xlsx', methods=['GET'])
def export_event_log_xlsx():
    try:
        from openpyxl import Workbook
    except ImportError:
        return jsonify({'success': False, 'error': 'openpyxl не установлен'}), 503
    events = _read_event_log()
    wb = Workbook()
    ws = wb.active
    ws.title = 'Event log'
    ws.append(['Время', 'Тип', 'Участник', 'Заявка', 'Детали'])
    for event in events:
        ws.append([
            event.get('t', ''), event.get('type', ''), event.get('actor', ''),
            event.get('request_id', ''),
            json.dumps(event.get('details') or {}, ensure_ascii=False),
        ])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return Response(bio.getvalue(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename=InTrAvia_event_log.xlsx'})


# ============================================================
# Excel export for operational / BI reporting
# ============================================================
@app.route('/api/reports/export.xlsx', methods=['GET'])
def export_operational_reports_xlsx():
    try:
        from io import BytesIO
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify({'success': False, 'error': 'Для экспорта Excel установите пакет openpyxl'}), 503

    conn = get_db()
    sync_expired_mel(conn)
    cur = conn.cursor()
    cur.execute("""SELECT r.*, ar.aircraft_type, ar.configuration AS aircraft_configuration
        FROM requests r LEFT JOIN aircraft_registry ar ON ar.registration=UPPER(r.aircraft)
        ORDER BY r.created_at DESC""")
    rows = [dict(x) for x in cur.fetchall()]
    archived = []
    audit_rows = []
    for row in rows:
        is_deleted = row.get('status') == 'deleted'
        row['record_storage'] = 'Архив удалённых' if is_deleted else 'В базе данных'
        row['archive_reason'] = ''
        row['archived_at'] = ''
        try:
            history = json.loads(row.get('routing_history') or '[]')
        except Exception:
            history = []
        for event in history if isinstance(history, list) else []:
            if isinstance(event, dict):
                audit_rows.append({
                    'request_id': row.get('id', ''), 'occurred_at': event.get('at') or event.get('timestamp') or '',
                    'action': event.get('action') or 'Событие маршрута', 'actor': event.get('actor') or '',
                    'stage': event.get('stage') or '', 'details': event.get('note') or event.get('details') or ''
                })
        if is_deleted:
            deleted_events = [e for e in history if isinstance(e, dict) and e.get('stage') == 'deleted'] if isinstance(history, list) else []
            if deleted_events:
                row['archive_reason'] = deleted_events[-1].get('note') or ''
                row['archived_at'] = deleted_events[-1].get('at') or ''
            archived.append(row)
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = 'Заявки'
    columns = [
        ('ID', 'id'), ('Борт', 'aircraft'), ('Тип ВС', 'aircraft_type'),
        ('Конфигурация', 'aircraft_configuration'), ('Рейс', 'flight_number'),
        ('Неисправность', 'fault_type'), ('Описание', 'description'), ('Статус', 'mcc_status'),
        ('Этап', 'routing_stage'), ('Статус полёта', 'flight_status'),
        ('Инженер', 'engineer_name'), ('Вердикт инженера', 'engineer_verdict'),
        ('Состояние ВС', 'engineer_condition'), ('Отчёт инженера', 'engineer_comment'),
        ('Дата отчёта инженера', 'engineer_report_at'), ('Пункт MEL', 'mcc_mel_ref'),
        ('ATA', 'mcc_ata'), ('Категория MEL', 'mcc_mel_category'),
        ('Интервал MEL', 'mel_rectification_interval'), ('Первичный отчёт техника', 'mel_initial_engineer_report'),
        ('Первичный отчёт — дата', 'mel_initial_engineer_report_at'), ('Первичную оценку выполнил', 'mel_initial_assessed_by'),
        ('M-процедура выполнена техником', 'mel_m_procedure_completed'),
        ('Отчёт техника по M-процедуре', 'mel_m_procedure_comment'), ('Тип контроля срока', 'mel_deadline_mode'),
        ('Значение лимита', 'mel_deadline_value'), ('Срок до', 'mcc_due_date'),
        ('Условие срока', 'mcc_due_condition'), ('Ограничения', 'mcc_mel_restrictions'),
        ('O-процедура нужна', 'mcc_proc_o'), ('O-процедура проверена', 'cpu_mel_o_completed'),
        ('M-процедура нужна', 'mcc_proc_m'), ('M-процедура проверена', 'cpu_mel_m_completed'),
        ('Применимость ЦПУ', 'cpu_mel_applicability_confirmed'),
        ('Проверил ЦПУ', 'cpu_mel_checked_by'), ('Комментарий ЦПУ', 'cpu_mel_check_comment'),
        ('Проверил MCC', 'mcc_mel_reviewed_by'), ('Дата проверки MCC', 'mcc_mel_reviewed_at'),
        ('Комментарий MCC', 'mcc_mel_review_comment'), ('Совокупное влияние проверено', 'mcc_mel_combined_checked'),
        ('Экипаж проинформирован', 'mcc_mel_crew_briefed'), ('Ограничения проверены', 'mcc_mel_restrictions_verified'),
        ('План устранения', 'mcc_mel_repair_plan'), ('Причина статуса полёта', 'flight_status_reason'),
        ('Создано', 'created_at'), ('Хранение записи', 'record_storage')]
    ws.append([c[0] for c in columns])
    for r in rows:
        ws.append([r.get(c[1]) if r.get(c[1]) is not None else '' for c in columns])

    mel_ws = wb.create_sheet('MEL')
    mel_cols = [
        ('Заявка', 'id'), ('Борт', 'aircraft'), ('Тип ВС', 'aircraft_type'),
        ('Конфигурация', 'aircraft_configuration'), ('Дефект', 'fault_type'),
        ('Пункт', 'mcc_mel_ref'), ('ATA', 'mcc_ata'), ('Категория', 'mcc_mel_category'),
        ('Редакция', 'mcc_mel_revision'), ('Описание пункта', 'mcc_mel_description'),
        ('Интервал устранения', 'mel_rectification_interval'), ('Тип лимита', 'mel_deadline_mode'),
        ('Значение', 'mel_deadline_value'), ('Правило начала отсчёта', 'mel_deadline_start_rule'),
        ('Дата устранения', 'mcc_due_date'), ('Условие', 'mcc_due_condition'),
        ('Ограничения', 'mcc_mel_restrictions'), ('O procedure', 'mcc_proc_o_text'),
        ('M procedure', 'mcc_proc_m_text'), ('Инженер', 'mel_assessed_by'),
        ('Обоснование инженера', 'mel_assessment_comment'), ('M-процедура выполнена техником', 'mel_m_procedure_completed'),
        ('Отчёт техника по M-процедуре', 'mel_m_procedure_comment'), ('Подтверждение ЦПУ', 'cpu_mel_applicability_confirmed'),
        ('O проверена', 'cpu_mel_o_completed'), ('M проверена', 'cpu_mel_m_completed'),
        ('Проверка MCC', 'mcc_mel_reviewed_by'), ('Проверено совокупное влияние', 'mcc_mel_combined_checked'),
        ('Экипаж проинформирован', 'mcc_mel_crew_briefed'), ('План устранения', 'mcc_mel_repair_plan'), ('Статус MEL', 'mcc_status'),
        ('Статус ВС', 'flight_status')]
    mel_ws.append([c[0] for c in mel_cols])
    for r in rows:
        if r.get('mcc_mel_choice') == 'MEL' or r.get('engineer_verdict') == 'MEL':
            mel_ws.append([r.get(c[1]) if r.get(c[1]) is not None else '' for c in mel_cols])

    log_ws = wb.create_sheet('Журнал действий')
    log_ws.append(['Заявка', 'Борт', 'Время', 'Этап', 'Исполнитель', 'Действие'])
    for r in rows:
        try:
            history = json.loads(r.get('routing_history') or '[]')
        except Exception:
            history = []
        for event in history:
            if isinstance(event, dict):
                log_ws.append([r.get('id',''), r.get('aircraft',''), event.get('timestamp') or event.get('at') or '',
                               event.get('stage') or '', event.get('actor') or '', event.get('note') or event.get('action') or ''])
    for event in audit_rows:
        log_ws.append([event.get('request_id',''), '', event.get('occurred_at',''),
                       event.get('stage',''), event.get('actor',''),
                       (event.get('action','') + (': ' + event.get('details','') if event.get('details') else ''))])

    reports_ws = wb.create_sheet('Отчёты инженеров')
    report_cols = [('Заявка','id'),('Борт','aircraft'),('Рейс','flight_number'),('Неисправность','fault_type'),
                   ('Инженер','engineer_name'),('Вердикт','engineer_verdict'),('Состояние','engineer_condition'),
                   ('Отчёт','engineer_comment'),('Дата отчёта','engineer_report_at'),('MEL','mcc_mel_ref'),
                   ('ATA','mcc_ata'),('Категория MEL','mcc_mel_category'),('Редакция MEL','mcc_mel_revision'),
                   ('Интервал MEL','mel_rectification_interval'),('Хранение записи','record_storage')]
    reports_ws.append([x[0] for x in report_cols])
    for r in rows:
        if r.get('engineer_report_at') or r.get('engineer_comment') or r.get('mel_initial_engineer_report'):
            reports_ws.append([r.get(k) if r.get(k) is not None else '' for _, k in report_cols])

    archive_ws = wb.create_sheet('Архив удалённых')
    archive_ws.append(['Заявка','Борт','Дата создания','Дата архивации','Причина','Инженер','Вердикт','Отчёт инженера'])
    for r in archived:
        archive_ws.append([r.get('id',''), r.get('aircraft',''), r.get('created_at',''), r.get('archived_at',''),
                           r.get('archive_reason',''), r.get('engineer_name',''), r.get('engineer_verdict',''), r.get('engineer_comment','')])

    for sheet in wb.worksheets:
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='17365D')
            cell.alignment = Alignment(vertical='center', wrap_text=True)
        sheet.row_dimensions[1].height = 28
        for col_idx in range(1, sheet.max_column + 1):
            max_len = 12
            for row_idx in range(1, min(sheet.max_row, 250) + 1):
                val = sheet.cell(row=row_idx, column=col_idx).value
                if val is not None:
                    max_len = max(max_len, min(len(str(val)), 55))
            sheet.column_dimensions[get_column_letter(col_idx)].width = max(12, min(max_len + 2, 45))
        for row_cells in sheet.iter_rows(min_row=2):
            for cell in row_cells:
                cell.alignment = Alignment(vertical='top', wrap_text=True)

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    filename = 'InTrAvia_operational_report_' + datetime.utcnow().strftime('%Y%m%d_%H%M%S') + '.xlsx'
    return Response(output.getvalue(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="' + filename + '"'})


# ============================================================
# Requests
# ============================================================
@app.route('/api/requests', methods=['GET'])
def get_requests():
    conn = get_db()
    sync_expired_mel(conn)
    cur = conn.cursor()
    _cleanup_inactive_route_payloads(cur)
    conn.commit()
    source = request.args.get('source')
    pilot_name = request.args.get('pilot_name')

    sql = """SELECT r.id, r.aircraft, r.flight_number, r.fault_type, r.severity, r.terminal, r.parking_id,
        r.description, r.source, r.engineer_id, r.engineer_name, e.task_step AS engineer_task_step, e.status AS engineer_status,
        ar.aircraft_type, ar.configuration AS aircraft_configuration,
        r.travel_time, r.using_car, r.walk_to_car, r.car_time, r.best_car,
        r.status, r.criticality, r.engineer_comment, r.created_at,
        r.pilot_name, r.pilot_rank, r.pilot_audio_text, r.rejected_by,
        r.cancel_reason, r.cancelled_by_name, r.cancelled_at,
        r.replaces_request_id, r.delay_minutes, r.delay_reason, r.delay_started_at,
        r.delay_approved_by, r.flight_cancelled, r.flight_cancel_reason,
        r.flight_cancelled_at, r.all_rejected, r.mcc_status, r.mcc_decision, r.mcc_comment,
        r.engineer_verdict, r.engineer_verdict_at, r.aog_action, r.aog_since, r.mcc_ata, r.inspection_assigned_at,
        r.mcc_mel_choice, r.mcc_proc_o, r.mcc_proc_m, r.mcc_proc_o_text, r.mcc_proc_m_text,
        r.mcc_mel_ref, r.mcc_mel_category, r.mcc_due_date, r.mcc_due_condition,
        r.mcc_mel_revision, r.mcc_mel_description, r.mcc_mel_restrictions,
        r.mel_catalog_id, r.mel_assessed_by, r.mel_assessment_comment, r.mel_rectification_interval,
        r.mel_m_procedure_completed, r.mel_m_procedure_comment,
        r.mel_initial_engineer_report, r.mel_initial_engineer_report_at, r.mel_initial_assessed_by,
        r.mel_deadline_mode, r.mel_deadline_value, r.mel_deadline_start_rule,
        r.cpu_mel_applicability_confirmed, r.cpu_mel_checked_by, r.cpu_mel_checked_at,
        r.mcc_mel_reviewed_by, r.mcc_mel_reviewed_at, r.mcc_mel_review_comment,
        r.crs_signature, r.crs_signed_at, r.crs_signed_by,
        r.routing_stage, r.routing_history, r.flight_status, r.flight_status_reason,
        r.engineer_condition, r.engineer_report_at, r.dispatcher_report_at, r.cpu_report_at, r.pilot_notice_at
        FROM requests r LEFT JOIN engineers e ON e.id=r.engineer_id
        LEFT JOIN aircraft_registry ar ON ar.registration=UPPER(r.aircraft)
        WHERE r.status NOT IN ('draft','deleted')"""
    params = []
    if source:
        sql += " AND source=?"
        params.append(source)
    if pilot_name:
        sql += " AND pilot_name=?"
        params.append(pilot_name)
    sql += " ORDER BY r.created_at DESC"

    cur.execute(sql, params)
    res = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in res:
        try:
            r['routing_history'] = json.loads(r['routing_history'] or '[]')
        except Exception:
            r['routing_history'] = []
    return jsonify(res)


@app.route('/api/requests/<rid>', methods=['PUT'])
def update_request(rid):
    data = request.get_json() or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM requests WHERE id=?", (rid,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'error': 'Не найдена'}), 404

    cur.execute("SELECT status, routing_stage, engineer_id, engineer_comment FROM requests WHERE id=?", (rid,))
    current = cur.fetchone()
    if data.get('status') == 'completed' and current and current['routing_stage'] in ('technician','dispatcher','dispatcher_report','cpu_report','mcc_finalize'):
        conn.close()
        return jsonify({'success': False, 'error': 'Нельзя закрыть техническую задачу вручную. Инженер отправляет отчёт, затем он проходит диспетчера, ЦПУ и MCC.'}), 409

    new_status = data.get('status')
    if new_status and current and new_status != current['status']:
        allowed = {
            'draft': ('new', 'queued', 'cancelled'),
            'new': ('queued', 'assigned', 'cancelled', 'mcc_review'),
            'queued': ('assigned', 'cancelled'),
            'assigned': ('arriving', 'queued', 'cancelled'),
            'arriving': ('in_progress', 'cancelled'),
            'in_progress': ('working', 'report_submitted', 'cancelled'),
            'working': ('in_progress', 'report_submitted', 'cancelled'),
            'report_submitted': ('completed', 'cancelled'),
            'completed': (),
            'cancelled': (),
        }.get(str(current['status'] or ''), ())
        if new_status not in allowed:
            conn.close()
            return jsonify({
                'success': False,
                'error': 'Недопустимый переход статуса: ' + str(current['status']) + ' → ' + new_status,
                'allowed': list(allowed),
            }), 409
        if str(current['status']) in ('in_progress', 'working') and not bool(data.get('senior_confirmed')):
            conn.close()
            return jsonify({
                'success': False,
                'requires_senior_confirmation': True,
                'error': 'Снятие с начатой работы требует подтверждения старшего смены.',
            }), 409

    def _optional_float(key):
        if key not in data or data.get(key) is None or data.get(key) == '':
            return None
        try:
            value = float(data.get(key))
            return value if value >= 0 else None
        except (TypeError, ValueError):
            return None

    travel_time = _optional_float('travel_time')
    walk_to_car = _optional_float('walk_to_car')
    car_time = _optional_float('car_time')
    using_car = None
    if 'using_car' in data:
        using_car = 1 if bool(data.get('using_car')) else 0

    cur.execute("""UPDATE requests
        SET status=COALESCE(?,status), criticality=COALESCE(?,criticality),
            engineer_comment=COALESCE(?,engineer_comment),
            aircraft=COALESCE(?,aircraft), fault_type=COALESCE(?,fault_type),
            terminal=COALESCE(?,terminal), parking_id=COALESCE(?,parking_id),
            severity=COALESCE(?,severity), flight_number=COALESCE(?,flight_number),
            travel_time=COALESCE(?,travel_time), using_car=COALESCE(?,using_car),
            walk_to_car=COALESCE(?,walk_to_car), car_time=COALESCE(?,car_time),
            best_car=CASE WHEN ?=1 THEN ? ELSE best_car END
        WHERE id=?""",
                (data.get('status'), data.get('criticality'),
                 data.get('engineer_comment'), data.get('aircraft'),
                 data.get('fault_type'), data.get('terminal'),
                 data.get('parking_id'), data.get('severity'),
                 data.get('flight_number'), travel_time, using_car,
                 walk_to_car, car_time, 1 if 'best_car' in data else 0,
                 data.get('best_car') or None, rid))

    new_status = data.get('status')
    if new_status == 'completed':
        cur.execute("SELECT engineer_id FROM requests WHERE id=?", (rid,))
        r = cur.fetchone()
        if r and r['engineer_id']:
            other = engineer_has_active_requests(conn, r['engineer_id'], rid)
            if other == 0:
                cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?",
                            (r['engineer_id'],))
        cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    elif new_status == 'cancelled':
        cur.execute("UPDATE requests SET routing_stage='done' WHERE id=?", (rid,))
        cur.execute("SELECT engineer_id FROM requests WHERE id=?", (rid,))
        r = cur.fetchone()
        if r and r['engineer_id']:
            other = engineer_has_active_requests(conn, r['engineer_id'], rid)
            if other == 0:
                cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?",
                            (r['engineer_id'],))
        cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))

    if new_status and current and new_status != current['status']:
        _log_event('status_change', str(data.get('actor') or 'Диспетчер'), rid, {
            'from': current['status'], 'to': new_status,
            'senior_confirmed': bool(data.get('senior_confirmed')),
            'reason': str(data.get('reason') or '')[:300],
        })
        _append_routing_history(conn, rid, {
            'at': _iso_ms(), 'stage': 'status', 'actor': str(data.get('actor') or 'Диспетчер'),
            'from': current['status'], 'to': new_status,
            'note': str(data.get('reason') or 'смена статуса'),
        })
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/requests/<rid>', methods=['DELETE'])
def delete_request(rid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, engineer_id, status FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Не найдена'}), 404
    eng_id = row['engineer_id']
    _archive_requests_before_delete(cur, 'id=?', (rid,), reason='Удалена диспетчером')
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    # Освобождаем исполнителя независимо от прежнего статуса удалённой заявки.
    # Если у него осталась другая активная заявка, статус busy сохраняется.
    release_engineers_without_active_requests(conn, [eng_id])
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# DISPATCH — создание заявки
# ============================================================
@app.route('/api/dispatch', methods=['POST'])
def dispatch():
    data = request.get_json() or {}
    source = data.get('source', 'dispatcher')
    conn = get_db()
    cur = conn.cursor()

    if source == 'pilot':
        pilot_name = (data.get('pilot_name') or '').strip() or 'Пилот'
        pilot_rank = (data.get('pilot_rank') or '').strip() or 'КВС'
        aircraft = (data.get('aircraft') or '').strip() or None
        pilot_audio = data.get('pilot_audio') or ''

        if pilot_audio:
            rid = 'req_' + str(int(time.time() * 1000))
            cur.execute("""INSERT INTO requests (id, aircraft, fault_type, severity, terminal,
                parking_id, description, source, engineer_id, engineer_name, travel_time,
                status, criticality, pilot_name, pilot_rank, pilot_audio,
                pilot_audio_mime, pilot_audio_duration, mcc_status, routing_stage)
                VALUES (?,?,?,'medium',NULL,NULL,?,'pilot',NULL,NULL,0,'draft','medium',?,?,?,?,?,'DRAFT','mcc')""",
                        (rid, aircraft, 'other', 'Голосовая заявка от командира ВС',
                         pilot_name, pilot_rank, pilot_audio,
                         data.get('pilot_audio_mime') or 'audio/webm',
                         data.get('pilot_audio_duration') or 0))
            conn.commit()
            conn.close()
            _log_event('request_created', 'КВС', rid, {
                'source': 'pilot', 'aircraft': aircraft, 'mode': 'voice',
                'note': 'Голосовая заявка зафиксирована, ожидает расшифровки MCC',
            })
            return jsonify({'success': True, 'request_id': rid})

        parking_id = str(data.get('parking_id') or '')
        if not parking_id:
            conn.close()
            return jsonify({'success': False, 'error': 'Не указана стоянка'}), 400
        flight_number = (data.get('flight_number') or '').strip()
        description = str(data.get('description') or data.get('defect_text') or '').strip()
        classification = classify_defect(description or data.get('fault_type') or 'other',
                                         data.get('ata') or '', data.get('defect_category') or '')
        fault = normalize_fault_type(data.get('fault_type') or classification.get('ata') or 'other')
        if fault == 'other' and classification.get('skill'):
            fault = next((code for code, ata in FAULT_ATA.items()
                          if ata == classification.get('ata') and FAULT_SKILL.get(code) == classification.get('skill')),
                         'other')
        description = description or classification.get('matched_text') or fault
        severity = 'medium'

        rid = 'req_' + str(int(time.time() * 1000))
        cur.execute("""INSERT INTO requests (id, aircraft, flight_number, fault_type, severity,
            parking_id, description, source, status, criticality, mcc_status, routing_stage,
            pilot_name, pilot_rank, mcc_ata)
            VALUES (?,?,?,?,?,?,?,'pilot','new',?,'PENDING','mcc',?,?,?)""",
                    (rid, aircraft or '—', flight_number, fault, severity, parking_id, description,
                     severity, pilot_name, pilot_rank, classification.get('ata') or ''))
        conn.commit()
        conn.close()
        _log_event('request_created', 'КВС', rid, {
            'source': 'pilot', 'aircraft': aircraft, 'flight_number': flight_number,
            'parking_id': parking_id, 'description': description,
            'classification': classification,
        })
        if data.get('scheduled_at') or data.get('aircraft_eta'):
            _log_event('aircraft_scheduled', 'КВС', rid, {
                'scheduled_at': data.get('scheduled_at') or data.get('aircraft_eta'),
                'source': 'предполагаемое расписание',
            })
        return jsonify({'success': True, 'request_id': rid, 'classification': classification})

    # source == 'dispatcher'
    aircraft = (data.get('aircraft') or '').strip()
    parking_id = str(data.get('parking_id') or '')
    if not aircraft or not parking_id:
        conn.close()
        return jsonify({'success': False, 'error': 'Не указан борт или стоянка'}), 400

    flight_number = (data.get('flight_number') or '').strip()
    description = str(data.get('description') or data.get('defect_text') or '').strip()
    classification = classify_defect(description or data.get('fault_type') or 'other',
                                     data.get('ata') or '', data.get('defect_category') or '')
    fault = normalize_fault_type(data.get('fault_type') or classification.get('ata') or 'other')
    if fault == 'other' and classification.get('skill'):
        fault = next((code for code, ata in FAULT_ATA.items()
                      if ata == classification.get('ata') and FAULT_SKILL.get(code) == classification.get('skill')),
                     'other')
    description = description or classification.get('matched_text') or fault
    severity = data.get('severity', 'medium')
    pilot_name = (data.get('pilot_name') or '').strip() or None
    pilot_rank = (data.get('pilot_rank') or '').strip() or None

    rid = 'req_' + str(int(time.time() * 1000))
    target = _request_target_coords(parking_id, source) or AIRPORT_CENTERS.get(current_airport_payload()['airport'], (55.975, 37.410))

    # Диспетчерский интерфейс создаёт заявку без автоназначения:
    # диспетчер сам выбирает исполнителя из объяснимой таблицы кандидатов.
    if data.get('auto_assign') is False:
        cur.execute("""INSERT INTO requests (id, aircraft, flight_number, fault_type, severity,
            parking_id, description, source, status, criticality, mcc_status, routing_stage,
            pilot_name, pilot_rank, mcc_ata, mcc_comment)
            VALUES (?,?,?,?,?,?,?,?,'new',?,'INSPECTION','dispatcher',?,?,?,?)""",
                    (rid, aircraft, flight_number, fault, severity, parking_id, description,
                     source, severity, pilot_name, pilot_rank,
                     classification.get('ata') or '',
                     '[CLASS] ' + json.dumps(classification, ensure_ascii=False)))
        conn.commit()
        conn.close()
        _log_event('request_created', 'Диспетчер', rid, {
            'source': source, 'aircraft': aircraft, 'flight_number': flight_number,
            'parking_id': parking_id, 'description': description,
            'classification': classification, 'queued_for_dispatch': True,
        })
        if data.get('scheduled_at'):
            _log_event('aircraft_scheduled', 'Диспетчер', rid, {
                'scheduled_at': data.get('scheduled_at'),
                'source': 'предполагаемое расписание',
            })
        return jsonify({'success': True, 'queued': True, 'request_id': rid,
                        'classification': classification,
                        'message': 'Заявка создана. Выберите инженера из таблицы рекомендаций.'})

    cur.execute("""SELECT DISTINCT engineer_id FROM requests
        WHERE status NOT IN ('completed','cancelled','flight_cancelled','deleted')
          AND engineer_id IS NOT NULL""")
    busy = {r['engineer_id'] for r in cur.fetchall()}

    eng, route = get_free_engineer_for(conn, fault, FAULT_SKILL,
                                       target[0], target[1],
                                       exclude_ids=busy,
                                       required_skill_override=classification.get('skill'))

    if eng:
        cur.execute("""INSERT INTO requests (id, aircraft, flight_number, fault_type, severity,
            parking_id, description, source, status, criticality, mcc_status, routing_stage,
            engineer_id, engineer_name, travel_time, using_car, walk_to_car, car_time, best_car,
            pilot_name, pilot_rank, mcc_ata, mcc_comment)
            VALUES (?,?,?,?,?,?,?,?,'assigned',?,'INSPECTION','technician',?,?,?,?,?,?,?,?,?,?,?)""",
                    (rid, aircraft, flight_number, fault, severity, parking_id, description,
                     source, severity,
                     eng['id'], eng['name'],
                     0, 0, 0, 0, None, pilot_name, pilot_rank,
                     classification.get('ata') or '',
                     '[CLASS] ' + json.dumps(classification, ensure_ascii=False)))
        cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                    ('ВС ' + aircraft, eng['id']))
        conn.commit()
        conn.close()
        _log_event('request_created', 'Диспетчер', rid, {
            'source': source, 'aircraft': aircraft, 'flight_number': flight_number,
            'parking_id': parking_id, 'description': description,
            'classification': classification, 'assigned_immediately': True,
            'engineer_id': eng['id'],
        })
        if data.get('scheduled_at') or data.get('aircraft_eta'):
            _log_event('aircraft_scheduled', 'Диспетчер', rid, {
                'scheduled_at': data.get('scheduled_at') or data.get('aircraft_eta'),
                'source': 'предполагаемое расписание',
            })
        _log_event('assignment', 'Диспетчер', rid, {
            'engineer_id': eng['id'], 'engineer_name': eng['name'],
            'eta_min': float(route.get('travel_time') or 0) if route else 0,
            'mode': 'auto_recommendation', 'classification': classification,
        })
        # Маршрут строится только после принятия задачи инженером; до этого сохраняется ETA.
        final_time = float(route.get('travel_time') or 0) if route else 0

        return jsonify({
            'success': True, 'request_id': rid, 'classification': classification,
            'selected': {
                'request_id': rid,
                'engineer': {
                    'id': eng['id'], 'name': eng['name'],
                    'device_id': normalize_device_id(eng['device_id'],
                                                     get_engineer_number_from_device(eng['device_id'])),
                    'specialization': eng['specialization'],
                    'coords_lat': eng['coords_lat'],
                    'coords_lng': eng['coords_lng']
                },
                'travel_time': final_time,
                'using_car': route['using_car'],
                'walk_time': route['walk_time'],
                'walk_to_car': route['walk_to_car'],
                'car_time': route['best_car_time'],
                'best_car': route['best_car']['device_id'] if route['using_car'] else None,
                'path': route.get('path_coords')
            }
        })

    cur.execute("""INSERT INTO requests (id, aircraft, flight_number, fault_type, severity,
        parking_id, description, source, status, criticality, mcc_status, routing_stage,
        pilot_name, pilot_rank, delay_started_at, delay_reason, mcc_ata, mcc_comment)
        VALUES (?,?,?,?,?,?,?,?,'queued',?,'PENDING','dispatcher',?,?,CURRENT_TIMESTAMP,'Ожидание инженера',?,?)""",
                (rid, aircraft, flight_number, fault, severity, parking_id, description,
                 source, severity, pilot_name, pilot_rank,
                 classification.get('ata') or '',
                 '[CLASS] ' + json.dumps(classification, ensure_ascii=False)))
    conn.commit()
    conn.close()
    _log_event('request_created', 'Диспетчер', rid, {
        'source': source, 'aircraft': aircraft, 'flight_number': flight_number,
        'parking_id': parking_id, 'description': description,
        'classification': classification, 'queued': True,
    })
    _schedule_recheck(rid)
    return jsonify({'success': True, 'queued': True, 'request_id': rid,
                    'classification': classification,
                    'message': 'Заявка в очереди — нет свободных специалистов'})


# ============================================================
# /api/route
# ============================================================
def _parse_route_point(raw):
    """Point may be [lat,lng], {lat,lon}, {parking_id}, zone id or 'lat,lng'."""
    if isinstance(raw, dict):
        if raw.get('lat') is not None and (raw.get('lon') is not None or raw.get('lng') is not None):
            return float(raw.get('lat')), float(raw.get('lon') if raw.get('lon') is not None else raw.get('lng'))
        ident = raw.get('parking_id') or raw.get('zone_id') or raw.get('id') or raw.get('ref')
        if ident is not None:
            return _request_target_coords(str(ident), raw.get('source'))
        return None
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return float(raw[0]), float(raw[1])
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if ',' in text:
            parts = [p.strip() for p in text.split(',')]
            if len(parts) >= 2:
                try:
                    return float(parts[0]), float(parts[1])
                except ValueError:
                    pass
        return _request_target_coords(text)
    return None


@app.route('/api/route', methods=['POST'])
def compute_route():
    """Graph route with 1+ intermediate points, off-graph snapping and transport legs."""
    data = request.get_json() or {}
    try:
        start_lat = float(data.get('start_lat'))
        start_lng = float(data.get('start_lng'))
        end_lat = float(data.get('end_lat'))
        end_lng = float(data.get('end_lng'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'start/end обязательны'}), 400
    if not (-90 <= start_lat <= 90 and -90 <= end_lat <= 90 and
            -180 <= start_lng <= 180 and -180 <= end_lng <= 180):
        return jsonify({'success': False, 'error': 'Координаты вне допустимого диапазона'}), 400

    raw_waypoints = data.get('waypoints')
    if raw_waypoints is None:
        raw_waypoints = data.get('via') or []
    if isinstance(raw_waypoints, (str, dict)):
        raw_waypoints = [raw_waypoints]
    if not isinstance(raw_waypoints, list):
        return jsonify({'success': False, 'error': 'waypoints должен быть массивом'}), 400

    waypoints = []
    for index, raw in enumerate(raw_waypoints):
        try:
            point = _parse_route_point(raw)
        except (TypeError, ValueError):
            point = None
        if point is None:
            return jsonify({'success': False, 'error': 'Некорректная промежуточная точка',
                            'index': index}), 400
        if not (-90 <= point[0] <= 90 and -180 <= point[1] <= 180):
            return jsonify({'success': False, 'error': 'Промежуточная точка вне диапазона',
                            'index': index}), 400
        delay_sec = 0.0
        label = ''
        if isinstance(raw, dict):
            try:
                delay_sec = float(raw.get('delay_sec') or 0)
            except (TypeError, ValueError):
                delay_sec = 0.0
            label = str(raw.get('label') or raw.get('type') or '')
            kind = str(raw.get('type') or '').lower()
            if delay_sec <= 0:
                if kind in ('tool', 'warehouse', 'склад', 'инструмент'):
                    delay_sec = TOOL_PICKUP_DELAY_MIN * 60.0
                elif kind in ('transfer', 'car', 'parking', 'спецтранспорт'):
                    delay_sec = TRACK_BOARD_DELAY_MIN * 60.0
        waypoints.append({'lat': point[0], 'lng': point[1], 'delay_sec': max(0.0, delay_sec),
                          'label': label})

    car_lat = data.get('car_lat')
    car_lng = data.get('car_lng')
    using_car = car_lat is not None and car_lng is not None
    if using_car:
        try:
            car_lat = float(car_lat)
            car_lng = float(car_lng)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Некорректные координаты спецтранспорта'}), 400

    selected_airport = current_airport_payload()['airport']
    points = [(start_lat, start_lng)] + [(w['lat'], w['lng']) for w in waypoints] + [(end_lat, end_lng)]

    combined = []
    legs = []
    total_distance = 0.0
    total_time = 0.0
    fallback = False

    def append_segment(path):
        nonlocal combined
        if not path:
            return
        if combined and distance_km(combined[-1][0], combined[-1][1], path[0][0], path[0][1]) < 0.002:
            combined.extend(path[1:])
        else:
            combined.extend(path)

    if using_car:
        # Walk from the agent to the vehicle, then drive through all intermediate points.
        walk_path, walk_dist, walk_time = get_airport_road_path(
            selected_airport, start_lat, start_lng, car_lat, car_lng
        )
        if not walk_path:
            walk_path = [[start_lat, start_lng], [car_lat, car_lng]]
            walk_dist = distance_km(start_lat, start_lng, car_lat, car_lng)
            walk_time = walk_dist / WALK_SPEED_KMH * 60.0
            fallback = True
        append_segment([list(p) for p in walk_path])
        total_distance += walk_dist
        total_time += walk_dist / WALK_SPEED_KMH * 60.0
        legs.append({'mode': 'walk_to_transport', 'distance_km': round(walk_dist, 3),
                     'time_min': round(walk_dist / WALK_SPEED_KMH * 60.0, 2)})
        total_time += TRACK_BOARD_DELAY_MIN
        car_points = [(car_lat, car_lng)] + [(w['lat'], w['lng']) for w in waypoints] + [(end_lat, end_lng)]
        for index in range(len(car_points) - 1):
            a, b = car_points[index], car_points[index + 1]
            result = get_airport_transport_path(selected_airport, a[0], a[1], b[0], b[1])
            if result:
                car_path, car_dist, car_time, _car_legs = result
            else:
                car_path = [[a[0], a[1]], [b[0], b[1]]]
                car_dist = distance_km(a[0], a[1], b[0], b[1])
                car_time = car_dist / 5.0 * 60.0
                fallback = True
            append_segment([list(p) for p in car_path])
            total_distance += car_dist
            total_time += car_time
            legs.append({'mode': 'transport', 'from_index': index,
                         'distance_km': round(car_dist, 3), 'time_min': round(car_time, 2),
                         'waypoint_delay_sec': waypoints[index]['delay_sec'] if index < len(waypoints) else 0})
            if index < len(waypoints):
                total_time += waypoints[index]['delay_sec'] / 60.0
    else:
        for index in range(len(points) - 1):
            a, b = points[index], points[index + 1]
            path, dist, _graph_time = get_airport_road_path(selected_airport, a[0], a[1], b[0], b[1])
            if not path:
                path = [[a[0], a[1]], [b[0], b[1]]]
                dist = distance_km(a[0], a[1], b[0], b[1])
                fallback = True
            append_segment([list(p) for p in path])
            seg_time = dist / WALK_SPEED_KMH * 60.0
            total_distance += dist
            total_time += seg_time
            legs.append({'mode': 'walk', 'from_index': index, 'distance_km': round(dist, 3),
                         'time_min': round(seg_time, 2),
                         'waypoint_delay_sec': waypoints[index]['delay_sec'] if index < len(waypoints) else 0})
            if index < len(waypoints):
                total_time += waypoints[index]['delay_sec'] / 60.0

    if not combined:
        return jsonify({'success': True, 'path': None, 'distance': 0,
                        'total_time_min': 0, 'waypoints': len(waypoints), 'fallback': True})
    response = {
        'success': True,
        'path': [[round(p[0], 6), round(p[1], 6)] for p in combined],
        'distance': round(total_distance, 3),
        'total_time_min': round(total_time, 2),
        'speed_kmh': WALK_SPEED_KMH if not using_car else None,
        'using_car': bool(using_car),
        'waypoints': len(waypoints),
        'waypoint_details': waypoints,
        'legs': legs,
        'fallback': fallback,
    }
    return jsonify(response)


# ============================================================
# Отмена / Отказ / Задержки
# ============================================================
@app.route('/api/requests/<rid>/cancel', methods=['POST'])
def cancel_request(rid):
    data = request.get_json() or {}
    engineer_id = str(data.get('engineer_id') or '').strip()
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({'success': False, 'error': 'Укажите причину'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id,aircraft,fault_type,severity,terminal,parking_id,
        description,engineer_id,engineer_name,status,rejected_by,created_at
        FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    if row['status'] == 'completed':
        conn.close()
        return jsonify({'success': False, 'error': 'Уже выполнена'}), 400
    if row['status'] == 'cancelled':
        conn.close()
        return jsonify({'success': False, 'error': 'Уже отменена'}), 400

    who_id = engineer_id or row['engineer_id']
    who_name = row['engineer_name'] or ''
    if who_id:
        cur.execute("SELECT name FROM engineers WHERE id=?", (who_id,))
        e = cur.fetchone()
        if e and e['name']:
            who_name = e['name']
    old_eng = row['engineer_id']
    if old_eng:
        cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?", (old_eng,))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))

    rej_prev = {x.strip() for x in str(row['rejected_by']).split(',') if x.strip()}
    if who_id:
        rej_prev.add(str(who_id))
    rej_str = ','.join(sorted(rej_prev))

    fault = row['fault_type'] or 'engine_failure'
    parking = str(row['parking_id'] or '')
    aircraft = row['aircraft'] or 'ВС'
    target = _request_target_coords(parking, row['source'] if 'source' in row.keys() else None) or AIRPORT_CENTERS.get(current_airport_payload()['airport'], (55.975, 37.410))

    cur.execute("""SELECT DISTINCT engineer_id FROM requests
        WHERE status NOT IN ('completed','cancelled','flight_cancelled','deleted')
          AND engineer_id IS NOT NULL AND id!=?""", (rid,))
    busy = {r['engineer_id'] for r in cur.fetchall()}

    new_eng, route = get_free_engineer_for(conn, fault, FAULT_SKILL,
                                           target[0], target[1],
                                           exclude_ids=rej_prev | busy)
    cur.execute("""UPDATE requests SET status='cancelled', cancel_reason=?,
        cancelled_by_name=?, cancelled_at=CURRENT_TIMESTAMP, rejected_by=?,
        routing_stage='done' WHERE id=?""", (reason, who_name, rej_str, rid))

    if new_eng:
        new_rid = 'req_' + str(int(time.time() * 1000)) + '_r'
        cur.execute("""INSERT INTO requests (id,aircraft,fault_type,severity,terminal,
            parking_id,description,source,engineer_id,engineer_name,travel_time,
            using_car,walk_to_car,car_time,best_car,status,criticality,rejected_by,
            replaces_request_id,all_rejected,mcc_status,routing_stage)
            VALUES (?,?,?,?,?,?,?,'dispatcher',?,?,?,?,?,?,?,'assigned',?,?,?,0,'APPROVED','technician')""",
                    (new_rid, aircraft, fault, row['severity'], row['terminal'], parking,
                     row['description'], new_eng['id'], new_eng['name'],
                     0, 0, 0, 0, None, row['severity'], rej_str, rid))
        cur.execute("UPDATE engineers SET status='busy', task=?, task_step='pending', task_started_at=NULL WHERE id=?",
                    ('ВС ' + aircraft, new_eng['id']))
        conn.commit()
        conn.close()
        # При переназначении после отказа/отмены новый маршрут также
        # сохраняется немедленно, пока новый инженер ещё не ответил.
        _recalc_route_for_request_id(new_rid, force=True)
        return jsonify({
            'success': True, 'request_id': rid,
            'cancelled_by_name': who_name, 'reason': reason,
            'reassigned': True, 'new_request_id': new_rid,
            'selected': {
                'request_id': new_rid,
                'engineer': {
                    'id': new_eng['id'], 'name': new_eng['name'],
                    'device_id': normalize_device_id(new_eng['device_id'],
                                                     get_engineer_number_from_device(new_eng['device_id'])),
                    'specialization': new_eng['specialization'],
                    'coords_lat': new_eng['coords_lat'],
                    'coords_lng': new_eng['coords_lng']
                },
                'travel_time': 0,
                'using_car': False,
                'awaiting_acceptance': True
            }
        })

    cur.execute("""UPDATE requests SET status='queued', all_rejected=?,
        delay_started_at=COALESCE(delay_started_at,CURRENT_TIMESTAMP),
        delay_reason=CASE WHEN delay_reason='' OR delay_reason IS NULL
                          THEN 'Ожидание инженера' ELSE delay_reason END
        WHERE id=?""", (0, rid))
    conn.commit()
    conn.close()
    _schedule_recheck(rid)
    return jsonify({
        'success': True, 'request_id': rid,
        'cancelled_by_name': who_name, 'reason': reason,
        'reassigned': False, 'queued': True
    })


@app.route('/api/requests/<rid>/reject', methods=['POST'])
def reject_request(rid):
    data = request.get_json() or {}
    rejected_by = str(data.get('engineer_id') or '')
    reason = (data.get('reason') or '').strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id,aircraft,fault_type,severity,terminal,parking_id,
        description,engineer_id,rejected_by,status,routing_stage FROM requests WHERE id=?""", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    if row['status'] == 'completed':
        conn.close()
        return jsonify({'success': False, 'error': 'Уже выполнена'}), 400
    if row['status'] != 'assigned' or row['routing_stage'] != 'technician':
        conn.close()
        return jsonify({'success': False, 'error': 'Отклонить можно только новое назначение до принятия'}), 409
    if not reason:
        conn.close()
        return jsonify({'success': False, 'error': 'Укажите причину отказа'}), 400
    if not rejected_by:
        rejected_by = str(row['engineer_id'] or '')
    if row['engineer_id'] and rejected_by != str(row['engineer_id']):
        conn.close()
        return jsonify({'success': False, 'error': 'Назначение принадлежит другому инженеру'}), 403
    if row['engineer_id']:
        cur.execute("SELECT task_step FROM engineers WHERE id=?", (row['engineer_id'],))
        current_engineer = cur.fetchone()
        if current_engineer and current_engineer['task_step'] not in ('pending', 'idle', '', None):
            conn.close()
            return jsonify({'success': False, 'error': 'Назначение уже принято; отклонить его нельзя'}), 409

    fault = row['fault_type'] or 'engine_failure'
    parking = str(row['parking_id'] or '')
    aircraft = row['aircraft'] or 'ВС'
    target = _request_target_coords(parking, row['source'] if 'source' in row.keys() else None) or AIRPORT_CENTERS.get(current_airport_payload()['airport'], (55.975, 37.410))
    old_eng = row['engineer_id']
    rej_prev = {x.strip() for x in str(row['rejected_by']).split(',') if x.strip()}
    if rejected_by:
        rej_prev.add(rejected_by)
    rej_str = ','.join(sorted(rej_prev))
    if old_eng:
        set_engineer_free(conn, old_eng)
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))

    cur.execute("""SELECT DISTINCT engineer_id FROM requests
        WHERE status NOT IN ('completed','cancelled','flight_cancelled','deleted')
          AND engineer_id IS NOT NULL AND id!=?""", (rid,))
    busy = {r['engineer_id'] for r in cur.fetchall()}

    new_eng, route = get_free_engineer_for(conn, fault, FAULT_SKILL,
                                           target[0], target[1],
                                           exclude_ids=rej_prev | busy)
    if new_eng:
        cur.execute("""UPDATE requests SET engineer_id=?, engineer_name=?,
            travel_time=0, using_car=0, walk_to_car=0, car_time=0, best_car=NULL,
            engineer_comment='', status='assigned', routing_stage='technician',
            inspection_assigned_at=NULL, rejected_by=?, all_rejected=0 WHERE id=?""",
                    (new_eng['id'], new_eng['name'], rej_str, rid))
        set_engineer_busy(conn, new_eng['id'], 'ВС ' + aircraft)
        _log_routing_event(conn, rid, 'technician', row['engineer_name'] or 'Инженер',
                           'Отказ от назначения; переназначено на ' + new_eng['name'])
        conn.commit()
        conn.close()
        _log_event('reassignment', 'Система', rid, {
            'from_engineer_id': str(old_eng or ''), 'from_engineer_name': row['engineer_name'] or '',
            'to_engineer_id': str(new_eng['id']), 'to_engineer_name': new_eng['name'],
            'reason': 'Отказ инженера: ' + rejected_by, 'auto': True,
        })
        # После отказа назначаем следующего инженера и сразу строим его маршрут.
        _recalc_route_for_request_id(rid, force=True)
        _recalc_all_active_routes()
        return jsonify({
            'success': True, 'reassigned': True,
            'rejected_engineer_id': rejected_by,
            'selected': {
                'engineer': {
                    'id': new_eng['id'], 'name': new_eng['name'],
                    'device_id': normalize_device_id(new_eng['device_id'],
                                                     get_engineer_number_from_device(new_eng['device_id'])),
                    'specialization': new_eng['specialization'],
                    'coords_lat': new_eng['coords_lat'],
                    'coords_lng': new_eng['coords_lng']
                },
                'travel_time': 0,
                'using_car': False,
                'awaiting_acceptance': True
            }
        })

    cur.execute("""UPDATE requests SET status='queued', engineer_id=NULL,
        engineer_name=NULL, travel_time=0, using_car=0, walk_to_car=0, car_time=0,
        best_car=NULL, rejected_by=?, all_rejected=1,
        delay_started_at=COALESCE(delay_started_at,CURRENT_TIMESTAMP),
        delay_reason='Все отказались'
        WHERE id=?""", (rej_str, rid))
    conn.commit()
    conn.close()
    _recalc_all_active_routes()
    _schedule_recheck(rid)
    return jsonify({'success': True, 'reassigned': False, 'queued': True})


def _schedule_recheck(rid):
    def worker():
        time.sleep(RECHECK_DELAY_SEC)
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT status, flight_cancelled FROM requests WHERE id=?", (rid,))
            r = cur.fetchone()
            conn.close()
            if r and r['status'] == 'queued' and not r['flight_cancelled']:
                try_reassign_queued()
        except Exception as e:
            print(f'[RECHECK] {e}')
    threading.Thread(target=worker, daemon=True).start()


@app.route('/api/requests/<rid>/approve_delay', methods=['POST'])
def approve_delay(rid):
    data = request.get_json() or {}
    minutes = int(data.get('minutes') or 0)
    reason = (data.get('reason') or '').strip()
    dispatcher = (data.get('dispatcher_name') or 'Диспетчер').strip()
    if minutes <= 0 or minutes > 720:
        return jsonify({'success': False, 'error': 'Некорректная длительность'}), 400
    if not reason:
        return jsonify({'success': False, 'error': 'Укажите причину'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, flight_cancelled FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    if row['flight_cancelled']:
        conn.close()
        return jsonify({'success': False, 'error': 'Рейс уже отменён'}), 400
    cur.execute("""UPDATE requests SET delay_minutes=?, delay_reason=?,
        delay_approved_by=?, delay_started_at=COALESCE(delay_started_at,CURRENT_TIMESTAMP)
        WHERE id=?""", (minutes, reason, dispatcher, rid))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'request_id': rid,
                    'delay_minutes': minutes, 'delay_reason': reason,
                    'delay_approved_by': dispatcher})


@app.route('/api/requests/<rid>/cancel_flight', methods=['POST'])
def cancel_flight(rid):
    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()
    dispatcher = (data.get('dispatcher_name') or 'Диспетчер').strip()
    if not reason:
        return jsonify({'success': False, 'error': 'Укажите причину'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,aircraft,engineer_id,flight_cancelled,status FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    if row['flight_cancelled']:
        conn.close()
        return jsonify({'success': False, 'error': 'Рейс уже отменён'}), 400
    eng = row['engineer_id']
    if eng:
        other = engineer_has_active_requests(conn, eng, rid)
        if other == 0:
            cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?", (eng,))
    cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to=?", (rid,))
    _archive_requests_before_delete(cur, 'id=?', (rid,), reason='Удалена при отмене рейса')
    cur.execute("UPDATE requests SET flight_cancelled=1, flight_cancel_reason=?, flight_cancelled_at=CURRENT_TIMESTAMP WHERE id=?", (reason, rid))
    conn.commit()
    conn.close()
    try_reassign_queued()
    return jsonify({'success': True, 'request_id': rid,
                    'flight_cancelled': True, 'deleted': True,
                    'reason': reason, 'cancelled_by': dispatcher})


# ============================================================
# Complete + Clear
# ============================================================
@app.route('/api/complete/<eng_id>', methods=['POST'])
def complete_engineer(eng_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id FROM requests WHERE engineer_id=?
        AND status NOT IN ('completed','cancelled','flight_cancelled','deleted')""", (eng_id,))
    if cur.fetchall():
        conn.close()
        return jsonify({'success': False, 'reason': 'has_active_requests'})
    cur.execute("""UPDATE transport SET assigned_to=NULL
        WHERE assigned_to IN (SELECT id FROM requests WHERE engineer_id=?)""", (eng_id,))
    cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL WHERE id=?", (eng_id,))
    conn.commit()
    conn.close()
    try_reassign_queued()
    return jsonify({'success': True})


@app.route('/api/clear_all_requests', methods=['POST'])
def clear_all_requests():
    conn = get_db()
    cur = conn.cursor()
    try:
        deleted = _archive_requests_before_delete(cur, "status NOT IN ('draft','deleted')", reason='Массовая очистка заявок')
        cur.execute("UPDATE engineers SET status='free', task=NULL, task_step='idle', task_started_at=NULL")
        cur.execute("UPDATE transport SET assigned_to=NULL")
        conn.commit()
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/clear_all_pilot_requests', methods=['POST'])
def clear_all_pilot_requests():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT DISTINCT engineer_id FROM requests WHERE source='pilot' AND engineer_id IS NOT NULL")
        affected_engineers = [r['engineer_id'] for r in cur.fetchall()]
        deleted = _archive_requests_before_delete(cur, "source='pilot' AND status!='deleted'", reason='Очищены заявки пилотов')
        release_engineers_without_active_requests(conn, affected_engineers)
        cur.execute("UPDATE transport SET assigned_to=NULL WHERE assigned_to IN (SELECT id FROM requests WHERE status='deleted')")
        conn.commit()
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()


# ============================================================
# Audio + Transcribe
# ============================================================
@app.route('/api/requests/<rid>/audio', methods=['GET'])
def get_request_audio(rid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT pilot_audio, pilot_audio_mime FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    conn.close()
    if not row or not row['pilot_audio']:
        return jsonify({'error': 'Аудио не найдено'}), 404
    raw = row['pilot_audio']
    mime = row['pilot_audio_mime'] or 'audio/webm'
    b64 = raw.split(',', 1)[1] if raw.startswith('data:') else raw
    try:
        audio_bytes = base64.b64decode(b64)
    except Exception:
        return jsonify({'error': 'Ошибка декодирования'}), 500
    return Response(audio_bytes, mimetype=mime)


_whisper_model_cache = None
_whisper_lock = threading.Lock()


def _get_whisper_model():
    global _whisper_model_cache
    if _whisper_model_cache is not None:
        return _whisper_model_cache
    with _whisper_lock:
        if _whisper_model_cache is not None:
            return _whisper_model_cache
        try:
            import whisper
            _whisper_model_cache = whisper.load_model(WHISPER_MODEL)
            print(f'[WHISPER] {WHISPER_MODEL} загружена')
            return _whisper_model_cache
        except Exception as e:
            print(f'[WHISPER] недоступен: {e}')
            return None


def _parse_pilot_text(text):
    if not text:
        return {}
    out = {}
    m = re.search(r'\b(RA[-\s]?\d{5})\b', text, re.IGNORECASE)
    if m:
        out['aircraft'] = m.group(1).replace(' ', '-').upper()
    for code, syns in FAULT_SYNONYMS.items():
        if any(s in text.lower() for s in syns):
            out['fault_type'] = code
            break
    m = re.search(r'\b(\d{3}[A-Za-zА-Яа-я]?)\b', text)
    if m:
        pid = m.group(1).upper()
        if pid.isdigit() or (len(pid) == 4 and pid[:3].isdigit()):
            out['parking_id'] = pid
    for t in ('B', 'C', 'D', 'E', 'F'):
        if re.search(rf'\b{t}\b', text):
            out['terminal'] = t
            break
    return out


@app.route('/api/requests/<rid>/transcribe', methods=['POST'])
def transcribe_request_audio(rid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, pilot_audio, pilot_audio_mime, pilot_audio_text FROM requests WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Не найдена'}), 404
    if row['pilot_audio_text']:
        conn.close()
        return jsonify({'success': True, 'text': row['pilot_audio_text'],
                        'parsed': _parse_pilot_text(row['pilot_audio_text'])})
    if not row['pilot_audio']:
        conn.close()
        return jsonify({'success': False, 'error': 'no_audio'}), 400

    model = _get_whisper_model()
    if model is None:
        conn.close()
        return jsonify({'success': False, 'error': 'whisper_unavailable'}), 200

    raw = row['pilot_audio']
    b64 = raw.split(',', 1)[1] if raw.startswith('data:') else raw
    try:
        audio_bytes = base64.b64decode(b64)
    except Exception:
        conn.close()
        return jsonify({'success': False, 'error': 'decode_error'}), 500

    mime = row['pilot_audio_mime'] or 'audio/webm'
    ext = '.webm' if 'webm' in mime else ('.mp4' if 'mp4' in mime else '.wav')

    tmp = None
    wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(audio_bytes)
            tmp = f.name
        wav = tmp + '.wav'
        cmd = ['ffmpeg', '-y', '-i', tmp, '-ar', '16000', '-ac', '1',
               '-c:a', 'pcm_s16le', wav]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        except Exception as e:
            print(f'[FFMPEG] {e}')
        audio_for_whisper = wav if os.path.exists(wav) else tmp
        result = model.transcribe(audio_for_whisper, language='ru', fp16=False)
        text = (result.get('text') or '').strip()
        if not text:
            conn.close()
            return jsonify({'success': False, 'error': 'empty'}), 200
        parsed = _parse_pilot_text(text)
        cur.execute("UPDATE requests SET pilot_audio_text=? WHERE id=?", (text, rid))
        if parsed.get('aircraft'):
            cur.execute("UPDATE requests SET aircraft=? WHERE id=?", (parsed['aircraft'], rid))
        if parsed.get('fault_type'):
            cur.execute("UPDATE requests SET fault_type=? WHERE id=?", (parsed['fault_type'], rid))
        if parsed.get('terminal'):
            cur.execute("UPDATE requests SET terminal=? WHERE id=?", (parsed['terminal'], rid))
        if parsed.get('parking_id'):
            cur.execute("UPDATE requests SET parking_id=? WHERE id=?", (parsed['parking_id'], rid))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'text': text, 'parsed': parsed})
    except Exception as e:
        print(f'[TRANSCRIBE] {e}')
        try: conn.close()
        except: pass
        return jsonify({'success': False, 'error': 'transcribe_error'}), 500
    finally:
        for p in (tmp, wav):
            if p and os.path.exists(p):
                try: os.remove(p)
                except: pass


# ============================================================
# Запуск
# ============================================================
def _startup():
    init_db()
    load_svo_geojson()
    load_road_graph()
    load_vdnh_road_graph()
    load_osm_road_graphs()
    try:
        _recalc_all_active_routes()
    except Exception as e:
        print(f'[STARTUP ROUTE CLEANUP] {e}')


_startup()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)