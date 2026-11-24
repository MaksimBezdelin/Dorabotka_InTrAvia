"""Классификация неисправностей и MEL/AOG (вынесено из app.py)."""
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading

from intravia.config import BASE_DIR
from intravia.db import get_db

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


# Открытый классификатор неисправностей. Каталог хранится отдельно от БД
# (схему database.db не меняем): data/fault_catalog.json.
FAULT_CATALOG_PATH = (os.environ.get('INTRAVIA_FAULT_CATALOG', '').strip()
                      or os.path.join(BASE_DIR, 'data', 'fault_catalog.json'))
_FAULT_CATALOG_LOCK = threading.Lock()

FAULT_ATA_BY_SKILL = {
    'engine': '72',
    'avionics': '34',
    'hydraulics': '29',
    'electrical': '24',
    'mechanical': '32',
    'general': '',
}

FAULT_NAMES = {
    'engine_failure': 'Отказ двигателя',
    'engine_oil': 'Масляная система двигателя',
    'engine_overheat': 'Перегрев двигателя',
    'engine_vibration': 'Повышенная вибрация двигателя',
    'engine_start': 'Запуск двигателя',
    'fuel_leak': 'Течь топлива',
    'fuel_system': 'Топливная система',
    'avionics_nav': 'Навигационное оборудование',
    'avionics_radio': 'Радиосвязное оборудование',
    'avionics_autopilot': 'Автопилот',
    'avionics_transponder': 'Транспондер (ответчик)',
    'avionics_display': 'Дисплеи кабины',
    'avionics_gps': 'GPS-оборудование',
    'hydraulic': 'Гидравлическая система',
    'hydraulic_leak': 'Течь гидравлики',
    'hydraulic_pressure': 'Падение давления гидравлики',
    'electrical': 'Электрическая система',
    'electrical_generator': 'Генератор',
    'electrical_battery': 'Аккумулятор',
    'electrical_short': 'Короткое замыкание',
    'electrical_lighting': 'Освещение',
    'landing_gear': 'Шасси',
    'pneumatic': 'Пневматическая система',
    'brake_system': 'Тормозная система',
    'flap_issue': 'Закрылки',
    'aileron_issue': 'Элероны',
    'rudder_issue': 'Руль направления',
    'wing_damage': 'Повреждение крыла',
    'fuselage_damage': 'Повреждение фюзеляжа',
    'door_issue': 'Дверь/люк',
    'air_condition': 'Система кондиционирования',
    'pressurization': 'Герметизация кабины',
    'oxygen_system': 'Кислородная система',
    'fire_system': 'Пожарная система',
    'deicing_system': 'Противообледенительная система',
    'pilot_voice': 'Голосовая заявка пилота',
    'other': 'Прочее',
}


def _default_fault_catalog():
    """Каталог по умолчанию, если файл ещё не создан или повреждён."""
    catalog = []
    for code, skill in FAULT_SKILL.items():
        catalog.append({
            'code': code,
            'name': FAULT_NAMES.get(code, code),
            'ata': FAULT_ATA_BY_SKILL.get(skill, ''),
            'skill': skill,
            'synonyms': list(FAULT_SYNONYMS.get(code, [])),
            'custom': False,
        })
    for code, synonyms in FAULT_SYNONYMS.items():
        if all(entry['code'] != code for entry in catalog):
            skill = FAULT_SKILL.get(code, 'general')
            catalog.append({
                'code': code,
                'name': FAULT_NAMES.get(code, code),
                'ata': FAULT_ATA_BY_SKILL.get(skill, ''),
                'skill': skill,
                'synonyms': list(synonyms),
                'custom': False,
            })
    return catalog


def _normalize_catalog_entry(entry):
    if not isinstance(entry, dict):
        return None
    code = str(entry.get('code') or '').strip()
    if not code:
        return None
    skill = str(entry.get('skill') or 'general').strip() or 'general'
    synonyms = entry.get('synonyms') or []
    if not isinstance(synonyms, list):
        synonyms = [str(synonyms)]
    return {
        'code': code,
        'name': str(entry.get('name') or code),
        'ata': str(entry.get('ata') or ''),
        'skill': skill,
        'synonyms': [str(s) for s in synonyms if str(s).strip()],
        'custom': bool(entry.get('custom')),
    }


def _load_fault_catalog():
    """Читает каталог с диска; при любой ошибке возвращает встроенный набор."""
    try:
        with open(FAULT_CATALOG_PATH, encoding='utf-8') as fh:
            raw = json.load(fh)
        entries = [e for e in (_normalize_catalog_entry(x) for x in raw)
                   if e is not None]
        if entries:
            return entries
    except (OSError, ValueError, TypeError):
        pass
    return _default_fault_catalog()


def _write_fault_catalog(entries):
    """Атомарно перезаписывает каталог (tmp-файл в том же каталоге)."""
    directory = os.path.dirname(os.path.abspath(FAULT_CATALOG_PATH)) or '.'
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.fault_catalog.', suffix='.tmp',
                                    dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(entries, fh, ensure_ascii=False, indent=2)
            fh.write('\n')
        os.replace(tmp_path, FAULT_CATALOG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _match_fault_entry(catalog, raw):
    """Ищет запись по code/name/синониму, затем по самой длинной подстроке."""
    text = str(raw or '').strip().lower()
    if not text:
        return None
    for entry in catalog:
        candidates = [entry['code'], entry['name'], *entry.get('synonyms', [])]
        if any(text == str(c).strip().lower() for c in candidates if str(c).strip()):
            return entry
    best, best_len = None, 0
    for entry in catalog:
        candidates = [entry['code'], entry['name'], *entry.get('synonyms', [])]
        for candidate in candidates:
            value = str(candidate or '').strip().lower()
            if value and value in text and len(value) > best_len:
                best, best_len = entry, len(value)
    return best


def classify_fault(raw):
    """Классифицирует текст в запись каталога, пополняя его custom-записями.

    Возвращает {'code', 'ata', 'skill', 'matched', 'name'}. Никогда не падает
    из-за ошибки записи: в этом случае возвращается custom-результат в памяти.
    """
    text = str(raw or '').strip()
    if not text:
        entry = _match_fault_entry(_default_fault_catalog(), 'other') or {
            'code': 'other', 'ata': '', 'skill': 'general', 'name': 'Прочее'}
        return {'code': entry['code'], 'ata': entry['ata'],
                'skill': entry['skill'], 'matched': True,
                'name': entry['name']}

    catalog = _load_fault_catalog()
    entry = _match_fault_entry(catalog, text)
    if entry:
        return {'code': entry['code'], 'ata': entry['ata'],
                'skill': entry['skill'], 'matched': True,
                'name': entry['name']}

    code = 'custom_' + hashlib.sha1(text.lower().encode('utf-8')).hexdigest()[:8]
    custom = {
        'code': code,
        'name': text,
        'ata': '',
        'skill': 'general',
        'synonyms': [],
        'custom': True,
    }
    try:
        with _FAULT_CATALOG_LOCK:
            current = _load_fault_catalog()
            existing = _match_fault_entry(current, text)
            if existing:
                return {'code': existing['code'], 'ata': existing['ata'],
                        'skill': existing['skill'], 'matched': True,
                        'name': existing['name']}
            if not any(e['code'] == code for e in current):
                current.append(custom)
                _write_fault_catalog(current)
    except Exception as exc:
        print(f'[FAULT] Не удалось сохранить custom-запись: {exc}')
    return {'code': code, 'ata': '', 'skill': 'general',
            'matched': False, 'name': text}


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


MEL_AOG_ATA_CATEGORY = {
    '21': ('C', 10), '22': ('B', 3), '23': ('C', 10), '24': ('B', 3),
    '25': ('D', 120), '26': ('B', 3), '27': ('A', 1), '28': ('B', 3),
    '29': ('A', 1), '30': ('B', 3), '31': ('C', 10), '32': ('A', 1),
    '33': ('D', 120), '34': ('C', 10), '35': ('A', 1), '36': ('B', 3),
    '38': ('D', 120), '44': ('D', 120), '45': ('C', 10), '46': ('C', 10),
    '47': ('C', 10), '49': ('C', 10), '52': ('A', 1), '53': ('A', 1),
    '71': ('B', 3), '73': ('B', 3), '74': ('B', 3), '75': ('B', 3),
    '76': ('B', 3), '77': ('C', 10), '78': ('B', 3), '79': ('B', 3),
    '80': ('B', 3),
}
MEL_AOG_CRITICAL_ATA = {'27', '32', '35', '52',
                        '71', '73', '74', '75', '76', '78', '79', '80'}
MEL_AOG_ACTUAL_FIRE = re.compile(
    r'пожар\s+(двигател|вс|самолет|отсек|кабин|гондол)|возгоран|взрыв')
MEL_AOG_FAULT_WORDS = re.compile(r'отказ|потер|утечк|заклиниван|не выпуска|не убира|не запуска')
# Свободная формулировка «двигатель не запускается» (обратный порядок слов
# относительно правила AOG-ENGINE в БД).
AOG_ENGINE_START_RE = re.compile(
    r'двигател[а-яё]*\s+(?:не\s+)?запуска|невозможн[а-яё]*\s+запуск[а-яё]*\s+двигател',
    re.IGNORECASE)
MEL_APU_RE = re.compile(r'(?<![а-яёa-z])апу(?![а-яёa-z])|вспомогательн', re.IGNORECASE)


ATA_TEXT_HINTS = (
    ('26', r'пожар|огнетуш|дым'),
    ('27', r'элерон|руль|закрылк|предкрылк|управлен.*(самолет|элерон|рул|закрыл|предкрыл)'),
    ('29', r'гидросистем|гидравлич|гидравлик'),
    ('32', r'шасси|тормоз'),
    ('73', r'топлив.*двигател'),
    ('28', r'топлив'),
    ('24', r'генератор|электропитан|аккумулятор|контактор|азс|шина питания'),
    ('35', r'кислород'),
    ('52', r'двер|люк'),
    ('23', r'связ|радио|антенн'),
    ('34', r'навигац|курс|ils|vor|dme|adf|gnss'),
    ('30', r'обледен|противооблед'),
    ('21', r'кондицион|наддув|герметизац'),
    ('22', r'автопилот|автомат.*управлен'),
    ('31', r'индикац|дисплей|регистратор'),
    ('33', r'освещен|фар'),
    ('38', r'водоснабж|унитаз|кухн'),
    ('44', r'салон'),
    ('49', r'всу|вспомогательн.*установк'),
    ('36', r'пневматич'),
    ('25', r'кресл|багаж'),
    ('77', r'индикац.*двигател'),
    ('71', r'двигател'),
)


def _infer_ata_from_text(low):
    for ata, pattern in ATA_TEXT_HINTS:
        if re.search(pattern, low):
            return ata
    return ''


def classify_mel_aog(text, ata=None, conn=None):
    """Классифицирует формулировку как AOG или MEL по словарю в БД.

    Правила читаются из fault_classification_rules; при отсутствии таблиц
    используется тот же встроенный набор. Результат — рекомендация,
    требует подтверждения инженером/MCC.
    """
    raw = str(text or '').strip()
    if not raw:
        raise ValueError('Укажите текст неисправности')
    low = raw.lower()
    ata = str(ata or '').strip()
    if not ata:
        ata = _infer_ata_from_text(low) or ''
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        # Литерал «AOG» — явное указание на запрет эксплуатации.
        if re.fullmatch(r'a\.?\s*o\.?\s*g\.?', low):
            return _mel_aog_result(raw, ata, 'AOG', '', 0,
                                   'Текст явно указывает AOG: эксплуатация запрещена',
                                   'AOG-LITERAL', 'high', conn)

        try:
            rules = [dict(row) for row in conn.execute(
                'SELECT rule_code, rule_type, pattern, decision, category, '
                'interval_days, note FROM fault_classification_rules '
                'ORDER BY priority')]
        except sqlite3.Error:
            rules = []
        if not rules:
            rules = [
                {'rule_code': 'MEL-IND', 'rule_type': 'mel',
                 'pattern': r'индикац|сигнализац|датчик|кнопк|табло|лампа|подсвет|дисплей|панель|пульт|разъем|провод|цепь',
                 'decision': 'MEL', 'category': 'C', 'interval_days': 10,
                 'note': 'Отказ индикации/датчика/цепи'},
                {'rule_code': 'AOG-ENGINE', 'rule_type': 'aog',
                 'pattern': r'отказ двигател|не запускается двигател|двигател[а-яё]*\s+(?:не\s+)?запуска',
                 'decision': 'AOG', 'category': '', 'interval_days': 0,
                 'note': 'Отказ двигателя'},
                {'rule_code': 'AOG-DEPRESS', 'rule_type': 'aog',
                 'pattern': r'разгерметизац|полная потеря|полный отказ|обесточ',
                 'decision': 'AOG', 'category': '', 'interval_days': 0,
                 'note': 'Потеря герметизации/полное обесточивание'},
            ]

        def _rule_matches(rule):
            try:
                if rule.get('rule_code') == 'MEL-APU' and not MEL_APU_RE.search(low):
                    # «запускается» не должно считаться упоминанием АПУ/ВСУ.
                    return None
                return re.search(rule['pattern'], low, re.IGNORECASE)
            except (re.error, TypeError):
                return None

        # AOG имеет приоритет над MEL: безопасность важнее отложимого пункта.
        for rule in rules:
            if rule.get('rule_type') != 'aog':
                continue
            if _rule_matches(rule):
                return _mel_aog_result(raw, ata, 'AOG', '', 0,
                                       rule.get('note') or '',
                                       rule['rule_code'], 'high', conn)
        if AOG_ENGINE_START_RE.search(low):
            return _mel_aog_result(raw, ata, 'AOG', '', 0,
                                   'Двигатель не запускается — эксплуатация запрещена',
                                   'AOG-ENGINE-START', 'high', conn)
        if MEL_AOG_ACTUAL_FIRE.search(low):
            return _mel_aog_result(raw, ata, 'AOG', '', 0,
                                   'Пожар/взрыв — эксплуатация запрещена',
                                   'AOG-FIRE', 'high', conn)

        mel_rule = None
        for rule in rules:
            if rule.get('rule_type') != 'mel':
                continue
            if _rule_matches(rule):
                mel_rule = rule
                break
        if mel_rule:
            return _mel_aog_result(raw, ata, mel_rule['decision'],
                                   mel_rule.get('category') or 'C',
                                   int(mel_rule.get('interval_days') or 10),
                                   mel_rule.get('note') or '',
                                   mel_rule['rule_code'], 'high', conn)
        if ata in MEL_AOG_CRITICAL_ATA and MEL_AOG_FAULT_WORDS.search(low):
            return _mel_aog_result(raw, ata, 'AOG', '', 0,
                                   f'Критическая глава ATA {ata}',
                                   'ATA-CRITICAL', 'medium', conn)
        category, days = MEL_AOG_ATA_CATEGORY.get(ata, ('C', 10))
        return _mel_aog_result(raw, ata, 'MEL', category, days,
                               'Отложимый пункт MEL', 'ATA-DEFAULT',
                               'medium', conn)
    finally:
        if own_conn:
            conn.close()


def _catalog_tokens(text):
    """Значимые слова запроса с простым расширением синонимов FIM."""
    words = [w for w in re.findall(r'[А-Яа-яЁёA-Za-z]{4,}', text.lower())][:4]
    groups = []
    for word in words:
        variants = {word}
        if 'гидравл' in word:
            variants.update({'гидро', 'гидрожидкост', 'гидросистем'})
        if word.startswith('течь') or 'утечк' in word:
            variants.update({'течь', 'утечк', 'потек'})
        if 'запуск' in word:
            variants.update({'запуск', 'запуска'})
        groups.append(sorted(variants))
    return groups


def _find_catalog_matches(conn, text, ata):
    """Ищет записи FIM/MEL/AOG по свободному тексту (OR по токенам)."""
    groups = _catalog_tokens(text)
    try:
        for combine in ('AND', 'OR'):
            if not groups:
                break
            subclauses, params = [], []
            for variants in groups:
                inner = []
                for variant in variants:
                    inner.append('lower(title) LIKE ?')
                    params.append('%' + variant + '%')
                subclauses.append('(' + ' OR '.join(inner) + ')')
            where = (' ' + combine + ' ').join(subclauses)
            for use_ata in ([ata, None] if ata else [None]):
                sql = ('SELECT fim_code, ata, title, decision, mel_category '
                       'FROM fault_classification WHERE ' + where)
                query_params = list(params)
                if use_ata:
                    sql += ' AND ata=?'
                    query_params.append(use_ata)
                sql += (" ORDER BY CASE decision WHEN 'AOG' THEN 0 ELSE 1 END,"
                        ' ata, fim_code LIMIT 5')
                rows = [dict(row) for row in conn.execute(sql, query_params)]
                if rows:
                    return rows
        # Запасной вариант: показать записи по определённой главе ATA.
        if ata:
            return [dict(row) for row in conn.execute(
                'SELECT fim_code, ata, title, decision, mel_category '
                'FROM fault_classification WHERE ata=? '
                "ORDER BY CASE decision WHEN 'AOG' THEN 0 ELSE 1 END, fim_code "
                'LIMIT 5', (ata,))]
        # Для явного литерала «AOG» без других слов — примеры записей AOG.
        return [dict(row) for row in conn.execute(
            'SELECT fim_code, ata, title, decision, mel_category '
            "FROM fault_classification WHERE decision='AOG' "
            'ORDER BY ata, fim_code LIMIT 5')]
    except sqlite3.Error:
        pass
    return []


def _mel_aog_result(text, ata, decision, category, days, reason, rule,
                    confidence, conn):
    interval = '—' if decision == 'AOG' else (f'{days} дней' if days else '—')
    if decision == 'AOG':
        conditions = ('Эксплуатация запрещена до устранения; оформить AOG, '
                      'уведомить MCC и диспетчера.')
    else:
        conditions = (f'Допускается отложить по MEL категории {category or "C"} '
                      f'на {interval} с выполнением ограничений и процедур MEL.')
    matches = _find_catalog_matches(conn, text, ata)
    return {
        'text': text,
        'ata': ata or None,
        'decision': decision,
        'mel_category': category,
        'interval_days': days,
        'interval_text': interval,
        'reason': reason,
        'rule': rule,
        'confidence': confidence,
        'conditions': conditions,
        'needs_review': True,
        'matches': matches,
        'source': 'FIM 007-00 + правила MEL/AOG (требует сверки с MEL ВС)',
    }
