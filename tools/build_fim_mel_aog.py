# -*- coding: utf-8 -*-
"""Строит в database.db словарь неисправностей RRJ-95 из FIM 007-00.

Из PDF извлекаются коды RRJ-A-XX-... и названия неисправностей, затем
каждая запись классифицируется как AOG или MEL по явным правилам
(критические системы, ключевые слова, индикация/салон). Классификация
эвристическая: needs_review=1, требуется сверка с действующим MEL ВС.

Запуск:
    python tools/build_fim_mel_aog.py --fim-dir "C:\\path\\FIM 007-00"
    python tools/build_fim_mel_aog.py --fim-dir ... --db database.db --no-backup
"""
import argparse
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import time

try:
    import pymupdf
except ImportError:  # pragma: no cover
    print('Нужен pymupdf: .venv\\Scripts\\pip install pymupdf')
    raise


ATA_NAMES = {
    '21': 'Кондиционирование и наддув',
    '22': 'Автоматическое управление полётом',
    '23': 'Связь',
    '24': 'Электропитание',
    '25': 'Оборудование салона',
    '26': 'Пожарная защита',
    '27': 'Управление самолётом',
    '28': 'Топливная система',
    '29': 'Гидравлическая система',
    '30': 'Защита от обледенения и дождя',
    '31': 'Индикация и регистрация',
    '32': 'Шасси',
    '33': 'Освещение',
    '34': 'Навигация',
    '35': 'Кислородное оборудование',
    '36': 'Пневматическая система',
    '38': 'Водоснабжение и канализация',
    '44': 'Бортовые системы салона',
    '45': 'Бортовая система ТО',
    '46': 'Информационные системы',
    '47': 'Инертный газ',
    '49': 'ВСУ',
    '52': 'Двери',
    '53': 'Планер',
    '71': 'Силовая установка',
    '73': 'Топливная система двигателя',
    '74': 'Зажигание',
    '75': 'Воздушная система двигателя',
    '76': 'Управление двигателем',
    '77': 'Индикация двигателя',
    '78': 'Выхлоп',
    '79': 'Масляная система',
    '80': 'Запуск двигателя',
}

CRITICAL_ATA = {'27', '32', '35', '52',
                '71', '73', '74', '75', '76', '78', '79', '80'}

# Правила классификации: (код, тип, regex, решение, категория, дней, пояснение)
RULES = [
    ('AOG-FIRE', 'aog', r'пожар\s+(двигател|вс|самолет|отсек|кабин|гондол)|возгоран|взрыв|помпаж|самовыключ',
     'AOG', '', 0, 'Пожар/взрыв/помпаж/самовыключение — эксплуатация запрещена'),
    ('AOG-DEPRESS', 'aog', r'разгерметизац|полная потеря|полный отказ|обесточ',
     'AOG', '', 0, 'Потеря герметизации/полное обесточивание/полная потеря функции'),
    ('AOG-ENGINE', 'aog', r'отказ двигател|не запускается двигател',
     'AOG', '', 0, 'Отказ/невозможность запуска двигателя'),
    ('AOG-FUEL', 'aog', r'утечка топлива|течь топлива',
     'AOG', '', 0, 'Утечка топлива'),
    ('AOG-GEAR', 'aog', r'не выпускается|не убирается|заклиниван.*шасси|отказ тормоз|потеря тормоз',
     'AOG', '', 0, 'Отказ шасси/тормозов'),
    ('AOG-CONTROL', 'aog', r'отказ.*управлен(?!.*(индикац|сигнализац|датчик|кнопк|табло|лампа|цепь|провод))',
     'AOG', '', 0, 'Отказ системы управления самолётом'),
    ('AOG-BOTH', 'aog', r'отказ.*(оба|обе|обоих|двух)',
     'AOG', '', 0, 'Отказ обоих каналов/систем'),
    ('AOG-OXY', 'aog', r'кислород.*(отказ|потер)',
     'AOG', '', 0, 'Отказ кислородного оборудования'),
    ('AOG-STRUCT', 'aog', r'поврежден.*(обшивк|лонжерон|стойк)',
     'AOG', '', 0, 'Повреждение силовых элементов планера'),
    ('MEL-IND', 'mel', r'индикац|сигнализац|датчик|кнопк|табло|лампа|подсвет|дисплей|панель|пульт|разъем|провод|цепь',
     'MEL', 'C', 10, 'Отказ индикации/датчика/цепи — обычно отложимый пункт MEL'),
    ('MEL-CABIN', 'mel', r'пассажир|салон|кресл|багаж|вода|унитаз|кухн|освещен|чтен',
     'MEL', 'D', 120, 'Салонное/бытовое оборудование — категория D'),
    ('MEL-APU', 'mel', r'апу|вспомогательн',
     'MEL', 'C', 10, 'ВСУ — обычно отложимый пункт MEL категории C'),
]

# Категории по главам ATA для отложимых неисправностей
ATA_CATEGORY = {
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

INDICATION_RE = re.compile(RULES[9][2])
ACTUAL_FIRE_RE = re.compile(r'пожар\s+(двигател|вс|самолет|отсек|кабин|гондол)|возгоран|взрыв')
FAULT_WORDS_RE = re.compile(r'отказ|потер|утечк|заклиниван|не выпуска|не убира|не запуска')
CODE_RE = re.compile(r'^RRJ-[A-Z]-(\d{2})-(?:[\dA-Z]+-)+[0-9A-Z]{3,5}-[A-Z]$')
MONTH_RE = re.compile(
    r'(?:Январ[ья]|Феврал[ья]|Март[а]?|Апрел[ья]|Ма[йя]|Июн[ья]?|Июл[ья]?|'
    r'Август[а]?|Сентябр[ья]|Октябр[ья]|Ноябр[ья]|Декабр[ья]|'
    r'Янв|Фев|Мар|Апр|Июн|Июл|Авг|Сен|Окт|Ноя|Дек)'
    r'\.?\s+\d{1,2}/\d{2}\s+\d+\b')
FRONT_TITLES = ('ПЕРЕЧЕНЬ ДЕЙСТВУЮЩИХ МОДУЛЕЙ', 'ЛИСТ РЕГИСТРАЦИИ',
                'СВЕДЕНИЯ ОБ ИЗМЕНЕНИЯХ', 'СОДЕРЖАНИЕ')
HEADER_PREFIXES = ('RRJ-95', 'Наименование', 'Код модуля', 'Дата',
                   'издания', 'Число', 'стр.', 'Действи-', 'тельно')


def clean_title(raw):
    title = MONTH_RE.sub(' ', raw or '')
    title = re.sub(r'\bС\s+\d{5}\b', ' ', title)
    title = re.split(r',\s*RRJ-', title, maxsplit=1)[0]
    title = re.sub(r'\bRRJ-[A-Z]-[\dA-Z-]+', ' ', title)
    title = re.sub(r'^АЭ,?\s*', '', title)
    title = re.sub(r'\s+', ' ', title).strip(' -–—,')
    return title


def parse_fim(fim_dir, verbose=True):
    files = sorted(n for n in os.listdir(fim_dir)
                   if n.startswith('M7_92_0FIM_Chapter') and n.endswith('.pdf'))
    # Сначала профильные главы, затем общий TOC — чтобы у кодов были
    # подробные названия неисправностей, а не строки содержания.
    files = [n for n in files if 'Chapter000' not in n] + \
            [n for n in files if 'Chapter000' in n]
    entries = {}
    pages = 0
    started = time.time()
    for name in files:
        doc = pymupdf.open(os.path.join(fim_dir, name))
        pages += doc.page_count
        for pno in range(doc.page_count):
            lines = [line.strip() for line in doc[pno].get_text().splitlines()
                     if line.strip()]
            for idx, line in enumerate(lines):
                first = line.split()[0] if line.split() else ''
                match = CODE_RE.match(first)
                if not match:
                    continue
                code = first
                if code in entries:
                    continue
                if '-441' in code or code.endswith(
                        ('-00SA-A', '-00TA-A', '-00UA-A', '-009A-A')):
                    continue
                title_lines = []
                for prev in reversed(lines[max(0, idx - 10):idx]):
                    if prev.startswith('RRJ-') or prev.startswith(HEADER_PREFIXES):
                        break
                    if re.match(r'^\d+$', prev):
                        break
                    title_lines.append(prev)
                title = clean_title(' '.join(reversed(title_lines)))
                if len(title) < 8:
                    continue
                if any(marker in title.upper() for marker in FRONT_TITLES):
                    continue
                latin = sum(1 for ch in title if 'A' <= ch <= 'Z' or 'a' <= ch <= 'z')
                if latin > len(title) * 0.8:
                    continue
                entries[code] = {
                    'code': code,
                    'ata': match.group(1),
                    'title': title,
                    'file': name,
                    'page': pno + 1,
                }
        if verbose:
            print(f'  {name}: всего {len(entries)}')
    if verbose:
        print(f'Извлечено {len(entries)} записей из {len(files)} PDF, '
              f'{pages} страниц, {round(time.time() - started, 1)} с')
    return list(entries.values())


def classify(entry):
    title = entry['title']
    low = title.lower()
    ata = entry['ata']

    # Индикация/салон/ВСУ: отложимые, если это не реальный пожар.
    for code, kind, pattern, decision, category, days, note in RULES:
        if kind != 'mel':
            continue
        if re.search(pattern, low, re.IGNORECASE):
            if code == 'MEL-IND' and ACTUAL_FIRE_RE.search(low):
                break
            return decision, category, days, note, code, 'high'
    for code, kind, pattern, decision, category, days, note in RULES:
        if kind != 'aog':
            continue
        if re.search(pattern, low, re.IGNORECASE):
            return decision, '', 0, note, code, 'high'
    if ata in CRITICAL_ATA and FAULT_WORDS_RE.search(low):
        return ('AOG', '', 0,
                f'Критическая глава ATA {ata}: отказ/потеря функции',
                'ATA-CRITICAL', 'medium')
    category, days = ATA_CATEGORY.get(ata, ('C', 10))
    return ('MEL', category, days,
            f'Отложимый пункт MEL по главе ATA {ata} ({ATA_NAMES.get(ata, "—")})',
            'ATA-DEFAULT', 'medium')


def interval_text(days):
    if not days:
        return '—'
    if days == 1:
        return '1 день'
    if days in (3, 10, 120):
        return f'{days} дней'
    return f'{days} дн.'


def conditions_text(decision, category):
    if decision == 'AOG':
        return ('Эксплуатация запрещена до устранения. Оформить AOG, '
                'уведомить MCC/диспетчера, внести в журнал.')
    limits = {'A': '1 день', 'B': '3 дня', 'C': '10 дней', 'D': '120 дней'}
    return ('Допускается отложить по MEL на срок категории '
            f'{category or "C"} ({limits.get(category, "10 дней")}) '
            'с выполнением ограничений и процедур MEL; требуется решение MCC.')


def ensure_schema(conn):
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS fault_ata_systems (
        ata TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        default_decision TEXT NOT NULL DEFAULT 'MEL'
    );
    CREATE TABLE IF NOT EXISTS fault_classification_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_code TEXT UNIQUE NOT NULL,
        rule_type TEXT NOT NULL,
        pattern TEXT NOT NULL,
        decision TEXT NOT NULL,
        category TEXT DEFAULT '',
        interval_days INTEGER DEFAULT 0,
        note TEXT DEFAULT '',
        priority INTEGER DEFAULT 100
    );
    CREATE TABLE IF NOT EXISTS fault_classification (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fim_code TEXT UNIQUE NOT NULL,
        ata TEXT NOT NULL,
        title TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('AOG', 'MEL')),
        mel_category TEXT DEFAULT '',
        interval_days INTEGER DEFAULT 0,
        interval_text TEXT DEFAULT '—',
        conditions TEXT DEFAULT '',
        reason TEXT DEFAULT '',
        matched_rule TEXT DEFAULT '',
        confidence TEXT DEFAULT 'medium',
        needs_review INTEGER DEFAULT 1,
        source_file TEXT DEFAULT '',
        source_page INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_fault_class_ata ON fault_classification(ata);
    CREATE INDEX IF NOT EXISTS idx_fault_class_decision ON fault_classification(decision);
    CREATE INDEX IF NOT EXISTS idx_fault_rules_type ON fault_classification_rules(rule_type);
    """)


def seed_rules(conn):
    cur = conn.cursor()
    cur.execute('DELETE FROM fault_classification_rules')
    for priority, (code, kind, pattern, decision, category, days, note) in enumerate(RULES, 1):
        cur.execute("""INSERT INTO fault_classification_rules
            (rule_code, rule_type, pattern, decision, category, interval_days, note, priority)
            VALUES (?,?,?,?,?,?,?,?)""",
                    (code, kind, pattern, decision, category, days, note, priority))
    cur.execute('DELETE FROM fault_ata_systems')
    for ata, name in ATA_NAMES.items():
        default = 'AOG' if ata in CRITICAL_ATA else 'MEL'
        cur.execute('INSERT INTO fault_ata_systems (ata, name, default_decision) VALUES (?,?,?)',
                    (ata, name, default))


def store_entries(conn, entries):
    cur = conn.cursor()
    cur.execute('DELETE FROM fault_classification')
    counts = {'AOG': 0, 'MEL': 0}
    for entry in entries:
        decision, category, days, reason, rule, confidence = classify(entry)
        counts[decision] += 1
        cur.execute("""INSERT INTO fault_classification
            (fim_code, ata, title, decision, mel_category, interval_days,
             interval_text, conditions, reason, matched_rule, confidence,
             needs_review, source_file, source_page)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (entry['code'], entry['ata'], entry['title'], decision,
                     category, days, interval_text(days),
                     conditions_text(decision, category), reason, rule,
                     confidence, entry['file'], entry['page']))
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fim-dir', default=r'C:\Users\Вячеслав\OneDrive\Desktop\FIM 007-00')
    parser.add_argument('--db', default=os.path.join(os.path.dirname(__file__), '..', 'database.db'))
    parser.add_argument('--no-backup', action='store_true')
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.isfile(db_path):
        raise SystemExit('БД не найдена: ' + db_path)
    if not os.path.isdir(args.fim_dir):
        raise SystemExit('Каталог FIM не найден: ' + args.fim_dir)
    if not args.no_backup:
        backup = db_path + '.bak_fim_' + time.strftime('%Y%m%d_%H%M%S')
        shutil.copyfile(db_path, backup)
        print('Бэкап БД:', backup)

    entries = parse_fim(args.fim_dir)
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        seed_rules(conn)
        counts = store_entries(conn, entries)
        conn.commit()
        total = conn.execute('SELECT COUNT(*) FROM fault_classification').fetchone()[0]
        aog = conn.execute("SELECT COUNT(*) FROM fault_classification WHERE decision='AOG'").fetchone()[0]
        mel = conn.execute("SELECT COUNT(*) FROM fault_classification WHERE decision='MEL'").fetchone()[0]
    finally:
        conn.close()
    print(f'Готово: {total} записей (AOG {aog}, MEL {mel}) в {db_path}')


if __name__ == '__main__':
    main()
