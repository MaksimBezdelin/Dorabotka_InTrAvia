import os
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.abspath(os.path.join(BASE, '..', '..'))
OUT = os.path.join(BASE, 'InTrAvia_final_stage.pptx')

NAVY = RGBColor(0x07, 0x1B, 0x33)
BLUE = RGBColor(0x0B, 0x4A, 0x8F)
BLUE2 = RGBColor(0x1D, 0x6F, 0xC9)
PALE = RGBColor(0xF3, 0xF7, 0xFC)
PALE2 = RGBColor(0xE7, 0xF0, 0xFA)
GOLD = RGBColor(0xC9, 0xA8, 0x4C)
GREEN = RGBColor(0x16, 0xA3, 0x4A)
ORANGE = RGBColor(0xD9, 0x77, 0x06)
RED = RGBColor(0xB9, 0x1C, 0x1C)
TEXT = RGBColor(0x17, 0x24, 0x33)
MUTED = RGBColor(0x5B, 0x6B, 0x7E)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LINE = RGBColor(0xD8, 0xE3, 0xEF)

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
BLANK = prs.slide_layouts[6]


def set_bg(slide, color=WHITE):
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def box(slide, x, y, w, h, text='', fill=WHITE, line=LINE, radius=True,
        size=12, color=TEXT, bold=False, align=PP_ALIGN.LEFT, margin=0.08,
        anchor=MSO_ANCHOR.TOP):
    shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE
    shape = slide.shapes.add_shape(shape_type, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.color.rgb = line
    shape.line.width = Pt(0.8)
    if radius:
        try:
            shape.adjustments[0] = 0.06
        except Exception:
            pass
    tf = shape.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(margin)
    tf.margin_top = tf.margin_bottom = Inches(margin)
    tf.vertical_anchor = anchor
    if text:
        lines = text.split('\n')
        for i, line_text in enumerate(lines):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = align
            p.space_after = Pt(2)
            run = p.add_run()
            run.text = line_text
            run.font.name = 'Arial'
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.color.rgb = color
    return shape


def text(slide, x, y, w, h, content, size=12, color=TEXT, bold=False,
         align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=4):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, line_text in enumerate(content.split('\n')):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(spacing)
        run = p.add_run()
        run.text = line_text
        run.font.name = 'Arial'
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
    return tb


def rich(slide, x, y, w, h, paragraphs, spacing=6):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    first = True
    for item in paragraphs:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.space_after = Pt(spacing)
        p.alignment = item.get('align', PP_ALIGN.LEFT)
        if item.get('bullet'):
            run = p.add_run()
            run.text = '• '
            run.font.name = 'Arial'
            run.font.size = Pt(item.get('size', 12))
            run.font.color.rgb = item.get('color', TEXT)
            run.font.bold = True
        for chunk in item.get('runs', []):
            run = p.add_run()
            run.text = chunk.get('text', '')
            run.font.name = 'Arial'
            run.font.size = Pt(chunk.get('size', item.get('size', 12)))
            run.font.bold = chunk.get('bold', False)
            run.font.color.rgb = chunk.get('color', item.get('color', TEXT))
    return tb


def line(slide, x1, y1, x2, y2, color=BLUE, width=1.5, dash=False):
    conn = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1),
                                      Inches(x2), Inches(y2))
    conn.line.color.rgb = color
    conn.line.width = Pt(width)
    if dash:
        conn.line.dash_style = 4


def arrow(slide, x1, y1, x2, y2, color=BLUE, width=1.8):
    conn = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1),
                                      Inches(x2), Inches(y2))
    conn.line.color.rgb = color
    conn.line.width = Pt(width)
    try:
        conn.line.end_arrowhead = 2
    except Exception:
        pass


def header(slide, number, title, subtitle=''):
    set_bg(slide, PALE)
    box(slide, 0, 0, 13.333, 1.02, '', fill=NAVY, line=NAVY, radius=False)
    box(slide, 0.48, 0.24, 0.08, 0.52, '', fill=GOLD, line=GOLD, radius=False)
    text(slide, 0.72, 0.18, 9.8, 0.55, title, 24, WHITE, True, anchor=MSO_ANCHOR.MIDDLE)
    text(slide, 11.55, 0.25, 1.4, 0.45, '%02d / 10' % number, 11, RGBColor(0xB9, 0xC7, 0xD8),
         False, PP_ALIGN.RIGHT, MSO_ANCHOR.MIDDLE)
    if subtitle:
        text(slide, 0.72, 0.72, 11.6, 0.25, subtitle, 9, RGBColor(0xBF, 0xD1, 0xE4))


def footer(slide, source='InTrAvia · финальный этап · ВДНХ'):
    text(slide, 0.48, 7.18, 8.0, 0.2, source, 8, MUTED)
    text(slide, 11.0, 7.18, 1.85, 0.2, '25.11.2026', 8, MUTED, False, PP_ALIGN.RIGHT)


def chip(slide, x, y, w, h, label, color=BLUE, fill=PALE2):
    box(slide, x, y, w, h, label, fill=fill, line=color, radius=True, size=9, color=color, bold=True,
        align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, margin=0.03)


# ============================================================
# 1. Title
# ============================================================
s = prs.slides.add_slide(BLANK)
set_bg(s, NAVY)
box(s, 0, 0, 13.333, 7.5, '', fill=NAVY, line=NAVY, radius=False)
box(s, 0.7, 0.75, 0.10, 1.8, '', fill=GOLD, line=GOLD, radius=False)
text(s, 1.0, 0.72, 11.2, 0.35, 'ФИНАЛЬНЫЙ ЭТАП · УМНАЯ ДИСПЕТЧЕРИЗАЦИЯ ОТО', 12, GOLD, True)
text(s, 1.0, 1.20, 11.4, 1.65, 'InTrAvia', 54, WHITE, True)
text(s, 1.0, 2.45, 11.4, 0.9, 'маршрутизация, динамическая обстановка\nи внештатные ситуации', 24,
     RGBColor(0xD9, 0xE7, 0xF6))
text(s, 1.0, 3.62, 10.8, 1.2,
     'Испытательный полигон ВДНХ · 10 сервисных зон · 3 инженера · 3 единицы спецтранспорта\n'
     'Норматив прибытия к ВС — 15 минут · пешая скорость — 5 км/ч', 13,
     RGBColor(0xBF, 0xD1, 0xE4))
chip(s, 1.0, 5.15, 2.55, 0.5, 'REST API /api/v1/track', GOLD, NAVY)
chip(s, 3.72, 5.15, 2.15, 0.5, 'Flask · SQLite · Leaflet', RGBColor(0x8F, 0xC7, 0xFF), NAVY)
chip(s, 6.04, 5.15, 2.1, 0.5, 'Capacitor Android', RGBColor(0x8F, 0xC7, 0xFF), NAVY)
chip(s, 8.31, 5.15, 2.4, 0.5, 'Офлайн + голос', GREEN, NAVY)
text(s, 1.0, 6.35, 11.5, 0.35, 'Команда InTrAvia · исходный код: github.com/MaksimBezdelin/Dorabotka_InTrAvia',
     10, RGBColor(0x9F, 0xB4, 0xCB))

# ============================================================
# 2. Result / scoring map
# ============================================================
s = prs.slides.add_slide(BLANK)
header(s, 2, 'Решение в цифрах', 'Что закрывает финальное задание и как это проверить')
metrics = [
    ('15 мин', 'норматив от фиксации\nдо прибытия к ВС', BLUE),
    ('5 / 8 / 10 / 20', 'км/ч — скорости\nспецтранспорта', ORANGE),
    ('2 Гц', 'частота GPS\nполевого инженера', GREEN),
    ('0.5 с', 'масштаб времени\nжурнала — мс', GOLD),
]
for i, (value, label, color) in enumerate(metrics):
    x = 0.75 + i * 3.08
    box(s, x, 1.35, 2.78, 1.55, '', WHITE, LINE, True)
    text(s, x + 0.16, 1.48, 2.45, 0.55, value, 24, color, True)
    text(s, x + 0.16, 2.02, 2.45, 0.7, label, 10, MUTED)

rich(s, 0.78, 3.22, 5.75, 3.5, [
    {'runs': [{'text': 'Обязательная часть — максимум', 'bold': True, 'size': 14, 'color': NAVY}]},
    {'bullet': True, 'runs': [{'text': 'архитектура, функционал, процесс разработки и авторство;', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'ВДНХ: карта, 10 зон, объекты, 4 типа дорог;', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'маршруты по графу, промежуточные точки, точки вне графа;', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'спецтранспорт с реальными скоростями и пересадкой;', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'интерфейс инженера, полевая работа, приём геопозиции;', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'роли, статусы, многокритериальный и объяснимый подбор;', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'MEL/AOG, открытый классификатор, несколько AOG;', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'норматив, риск, журнал CSV/XLSX.', 'size': 11}]},
], spacing=5)

rich(s, 6.95, 3.22, 5.65, 3.5, [
    {'runs': [{'text': 'Дополнительные баллы', 'bold': True, 'size': 14, 'color': NAVY}]},
    {'bullet': True, 'runs': [{'text': 'маршрут обновляется по мере движения;', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'текстовые подсказки и озвучивание (ru-RU);', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'предупреждения диспетчеру о рассогласовании данных;', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'офлайн-очередь GPS и действий, автосинхронизация;', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'устаревшая позиция с временем последнего обновления;', 'size': 11}]},
    {'bullet': True, 'runs': [{'text': 'авторские элементы: открытый каталог дефектов,', 'size': 11}]},
    {'runs': [{'text': '     альтернативный инженер в риск-уведомлении.', 'size': 11, 'color': MUTED}]},
])
footer(s)

# ============================================================
# 3. Architecture
# ============================================================
s = prs.slides.add_slide(BLANK)
header(s, 3, 'Архитектура', 'Модули, связи и технологический стек')
# Column labels
text(s, 0.55, 1.28, 3.0, 0.25, 'КЛИЕНТЫ', 10, BLUE, True)
text(s, 4.0, 1.28, 2.4, 0.25, 'ИНТЕГРАЦИЯ', 10, GOLD, True)
text(s, 6.65, 1.28, 3.2, 0.25, 'СЕРВЕРНАЯ ЛОГИКА', 10, BLUE, True)
text(s, 10.35, 1.28, 2.5, 0.25, 'ДАННЫЕ', 10, GREEN, True)

clients = [
    ('Диспетчер', 'HTML · JS · Leaflet\nкарта, очередь, кандидаты'),
    ('Инженер desktop', 'HTML · JS · Leaflet\nзаявки, маршрут, отчёт'),
    ('Мобильный инженер', 'Capacitor Android\nGPS 2 Гц · компас · офлайн'),
    ('MCC · ЦПУ · КВС', 'HTML · JS\nцепочка заявки'),
]
for i, (name, sub) in enumerate(clients):
    y = 1.62 + i * 1.12
    box(s, 0.55, y, 3.05, 0.92, '', WHITE, LINE, True)
    text(s, 0.7, y + 0.10, 2.75, 0.25, name, 12, NAVY, True)
    text(s, 0.7, y + 0.38, 2.75, 0.5, sub, 8.5, MUTED)
    arrow(s, 3.62, y + 0.46, 4.02, y + 0.46, BLUE2, 1.4)

integration = [
    ('REST / JSON', 'HTTPS · контракт\n/api/v1/track'),
    ('Живой обмен', 'GPS 0.5–3 с\nочереди 1–5 с'),
    ('Внешние данные', 'имитатор · OSM-тайлы\nрасписание ВС'),
]
for i, (name, sub) in enumerate(integration):
    y = 1.62 + i * 1.52
    box(s, 4.02, y, 2.35, 1.22, '', PALE2, GOLD, True)
    text(s, 4.16, y + 0.13, 2.05, 0.25, name, 11, BLUE, True)
    text(s, 4.16, y + 0.44, 2.05, 0.6, sub, 8.5, MUTED)

server_modules = [
    ('Маршрутизатор', 'NetworkX · графы ВДНХ\nвеса по скорости и типу'),
    ('Подбор и роли', 'срочность · позиция · ATA\nзагрузка · этап · риск'),
    ('Норматив и риск', 'ETA · прогноз · геозона\nальтернативный инженер'),
    ('Журнал и каталоги', 'JSONL · CSV · XLSX\nдефекты · ATA · MEL'),
]
for i, (name, sub) in enumerate(server_modules):
    y = 1.62 + i * 1.12
    box(s, 6.65, y, 3.25, 0.92, '', WHITE, BLUE, True)
    text(s, 6.8, y + 0.10, 2.95, 0.25, name, 11.5, NAVY, True)
    text(s, 6.8, y + 0.38, 2.95, 0.5, sub, 8.5, MUTED)
    arrow(s, 6.40, y + 0.46, 6.60, y + 0.46, GOLD, 1.4)
    arrow(s, 9.95, y + 0.46, 10.32, y + 0.46, GREEN, 1.4)

data_blocks = [
    ('SQLite', 'существующая схема\nбез миграций'),
    ('GeoJSON ВДНХ', 'дороги · зоны · объекты'),
    ('JSON-конфиги', 'аэропорт · дефекты\nинструмент · привязки'),
    ('event_log.jsonl', 'append-only\nметки UTC, мс'),
]
for i, (name, sub) in enumerate(data_blocks):
    y = 1.62 + i * 1.12
    box(s, 10.35, y, 2.45, 0.92, '', WHITE, GREEN, True)
    text(s, 10.5, y + 0.10, 2.15, 0.25, name, 11, NAVY, True)
    text(s, 10.5, y + 0.38, 2.15, 0.5, sub, 8.5, MUTED)

# Stack strip
box(s, 0.55, 6.28, 12.25, 0.55, '', NAVY, NAVY, True)
text(s, 0.75, 6.39, 11.9, 0.32,
     'Стек: Python 3.13 · Flask · Flask-CORS · SQLite · NetworkX · HTML/CSS/JS · Leaflet · Capacitor 6 · Gradle 8.2 · openpyxl',
     10, WHITE, True, anchor=MSO_ANCHOR.MIDDLE)
text(s, 0.55, 6.92, 12.2, 0.22,
     'Связи: клиенты → REST/JSON → серверные модули → SQLite/GeoJSON/JSONL; сервер читает конфигурацию и возвращает карту, ETA и предупреждения.',
     8.5, MUTED)
footer(s)

# ============================================================
# 4. Functionality
# ============================================================
s = prs.slides.add_slide(BLANK)
header(s, 4, 'Реализованный функционал', 'Список соответствует фактически работающим экранам и API')
groups = [
    ('Заявка и роли', [
        'КВС → MCC/ЦПУ → диспетчер → инженер',
        'валидируемые статусы и история',
        'MEL/AOG устанавливает инженер',
        'переназначение после старта — старший смены',
    ], BLUE),
    ('Подбор', [
        'срочность, позиция, загрузка, этап',
        'допуск по главе ATA',
        'последствия задержки и норматив',
        'таблица кандидатов + причины отклонения',
    ], GOLD),
    ('Маршруты', [
        'граф ВДНХ, 4 типа дорог, ВПП исключена',
        '1+ промежуточных точек',
        'точки вне графа, динамический пересчёт',
        'пешком / спецтранспорт 5–20 км/ч',
    ], ORANGE),
    ('Поле и данные', [
        'GPS инженера 2 Гц через API',
        'имитатор: e-XX, v-XX, RA-XXXXX',
        'автофиксация прибытия в зону',
        'офлайн-очередь и автосинхронизация',
    ], GREEN),
    ('Норматив и риск', [
        'прогноз от фиксации до прибытия',
        'учёт пересадки +10 с и склада +30 с',
        'предупреждение заранее + альтернатива',
        'факт нарушения в журнале',
    ], RED),
    ('Журнал и каталоги', [
        'все события из п. 3.2',
        'метки UTC с миллисекундами',
        'время формирования кандидатов',
        'выгрузка CSV и Excel',
    ], BLUE2),
    ('Открытый классификатор', [
        'свободный текст не теряется',
        'поиск по категориям и ATA',
        'добавление новых типов без кода',
        'классификация в подборе',
    ], ORANGE),
    ('Мобильный инженер', [
        'приём/отклонение, смена статуса',
        'маршрут, ETA, позиция, инструмент',
        'поворотные подсказки и озвучивание',
        'устаревшая GPS-позиция помечается',
    ], GREEN),
]
for i, (title, items, color) in enumerate(groups):
    col = i % 4
    row = i // 4
    x = 0.55 + col * 3.11
    y = 1.35 + row * 2.78
    box(s, x, y, 2.92, 2.52, '', WHITE, LINE, True)
    box(s, x, y, 2.92, 0.45, '', color, color, True)
    text(s, x + 0.12, y + 0.09, 2.65, 0.28, title, 11.5, WHITE, True)
    rich(s, x + 0.14, y + 0.60, 2.64, 1.82,
         [{'bullet': True, 'runs': [{'text': item, 'size': 8.8}]} for item in items], spacing=4)
footer(s)

# ============================================================
# 5. Process / QA / authorship
# ============================================================
s = prs.slides.add_slide(BLANK)
header(s, 5, 'Процесс разработки, контроль качества и авторство', 'Три обязательных блока — с конкретными фактами')
# Three columns
cols = [
    ('1 · Процесс и автоматизация', BLUE, [
        'Git/GitHub, отдельные ветки и worktree для каждой доработки;',
        'Flask + SQLite: живой сервер и отдельная копия БД для прогонов;',
        'Capacitor/Gradle: сборка debug APK, проверка assets и разрешений;',
        'Playwright: smoke диспетчера, desktop-инженера и мобильной версии;',
        'автоматизация API: curl/python-проверки контрактов и маршрутов.',
        'Пример: маршрут ВДНХ 4.557 км / 55.19 мин проверен на 1 и 2 точках.',
    ]),
    ('2 · Контроль качества кода', ORANGE, [
        'py_compile перед каждым запуском;',
        'интеграционный прогон: 26 проверок PASS;',
        'контракт /api/v1/track: lon+lng, пакет, 200/400;',
        'браузерный прогон без console/page errors;',
        'ручная проверка маршрута на карте и сценария 15 минут.',
        'Найдено и исправлено: клиент отправлял lng вместо lon; VDNKh-маршрут брал координаты SVO; VDNKh-транспорт был отключён.',
    ]),
    ('3 · Авторство кода', GREEN, [
        'Люди: постановка задачи, архитектура, сценарии, приёмка;',
        'ИИ-ассистент (Claude Code): генерация значительной части кода, тестов и документации;',
        'Совместно: человек задавал правила (5 км/ч, запрет рулёжек и ВПП, роли), ИИ реализовывал;',
        'Человеческое ревью каждого изменения перед запуском и коммитом;',
        'Пример: endpoint /api/v1/track сгенерирован ИИ, человек исправил поле lon и UTC-мс;',
        'Пример: отчёт инженера и MEL/AOG — требования и проверка человеком.',
    ]),
]
for i, (title, color, items) in enumerate(cols):
    x = 0.5 + i * 4.16
    box(s, x, 1.32, 3.95, 5.55, '', WHITE, LINE, True)
    box(s, x, 1.32, 3.95, 0.55, '', color, color, True)
    text(s, x + 0.18, 1.43, 3.6, 0.32, title, 12.5, WHITE, True)
    rich(s, x + 0.18, 2.02, 3.6, 4.6,
         [{'bullet': True, 'runs': [{'text': item, 'size': 9.3}]} for item in items], spacing=7)
text(s, 0.55, 7.0, 12.3, 0.2,
     'Воспроизводимость: все проверки запускаются на копии БД; рабочая БД и схема не изменяются.',
     8.5, MUTED)
footer(s)

# ============================================================
# 6. Polygon & routing
# ============================================================
s = prs.slides.add_slide(BLANK)
header(s, 6, 'Адаптация под ВДНХ и маршрутизация', 'Явный граф с атрибутами, скоростями и запретами')
# Left map facts
box(s, 0.55, 1.35, 6.25, 5.45, '', WHITE, LINE, True)
text(s, 0.78, 1.52, 5.8, 0.3, 'Испытательный полигон', 14, NAVY, True)
rich(s, 0.78, 1.95, 5.8, 4.6, [
    {'bullet': True, 'runs': [{'text': '10 сервисных зон обслуживания ВС (полигоны GeoJSON).', 'size': 10.5}]},
    {'bullet': True, 'runs': [{'text': '463 ребра сети: 129 taxi (ВС), 269 service (инженер/спецтранспорт), 62 pedestrian (инженер), 3 runway (ВС).', 'size': 10.5}]},
    {'bullet': True, 'runs': [{'text': 'Официальная сеть полигона: 801 узел / 842 ребра; спецтранспорт 677 / 706. Taxi/ВПП исключены.', 'size': 10.5}]},
    {'bullet': True, 'runs': [{'text': 'Граф спецтранспорта: 637 / 647; скорость каждого ребра из spd_kmh.', 'size': 10.5}]},
    {'bullet': True, 'runs': [{'text': 'ВПП и рулёжные ограничения исключены из пешего маршрута.', 'size': 10.5}]},
    {'bullet': True, 'runs': [{'text': 'От инженера до стоянки: проекция на ближайшее ребро roads.geojson, затем движение только по рёбрам графа.', 'size': 10.5}]},
    {'bullet': True, 'runs': [{'text': 'Смена локации = замена GeoJSON + JSON-справочников, без кода.', 'size': 10.5}]},
], spacing=6)
# Right speed table
box(s, 7.15, 1.35, 5.65, 2.65, '', WHITE, LINE, True)
text(s, 7.38, 1.50, 5.1, 0.3, 'Скоростной режим спецтранспорта', 13, NAVY, True)
rows = [
    ('5 км/ч', 'подъезд к ВС, отъезд, маневрирование'),
    ('8 км/ч', 'перрон вдоль терминалов'),
    ('10 км/ч', 'перрон к местам стоянок ВС'),
    ('20 км/ч', 'остальная территория аэродрома'),
]
for i, (speed, desc) in enumerate(rows):
    y = 1.95 + i * 0.45
    text(s, 7.4, y, 0.95, 0.25, speed, 11, BLUE, True)
    text(s, 8.45, y, 4.0, 0.25, desc, 9.5, MUTED)
text(s, 7.4, 3.70, 4.9, 0.2, 'Посадка/высадка +10 с · склад +30 с', 9, ORANGE, True)
# Right route facts
box(s, 7.15, 4.18, 5.65, 2.62, '', WHITE, LINE, True)
text(s, 7.38, 4.33, 5.1, 0.3, 'Проверенные маршруты', 13, NAVY, True)
rich(s, 7.38, 4.72, 5.1, 1.9, [
    {'bullet': True, 'runs': [{'text': 'Зона 9 → зона 1 (склад) → зона 10: 4.557 км, 55.19 мин, 352 координаты.', 'size': 9.8}]},
    {'bullet': True, 'runs': [{'text': 'Инженер → CAR-V01 → зона 10: 8.3 мин (6.8 пешком + 1.6 на машине).', 'size': 9.8}]},
    {'bullet': True, 'runs': [{'text': 'Пешая скорость — ровно 5 км/ч; формула distance / 5 × 60.', 'size': 9.8}]},
    {'bullet': True, 'runs': [{'text': 'Пересчёт при движении: каждые 2.5 с или 8 м.', 'size': 9.8}]},
], spacing=5)
footer(s)

# ============================================================
# 7. Transport & waypoints / dynamic
# ============================================================
s = prs.slides.add_slide(BLANK)
header(s, 7, 'Спецтранспорт, промежуточные точки и динамика', 'Маршрут выбирается сравнением вариантов, а не «всегда на машине»')
# three cards
cards = [
    ('Пешком vs спецтранспорт', BLUE, [
        'Система считает оба варианта и выбирает быстрейший.',
        'Учтены доступность машины, позиция, посадка +10 с и инструмент +30 с.',
        'Для ВДНХ — 3 машины: CAR-V01, CAR-V02, CAR-V03.',
        'Скорости берутся из рёбер service-дорог, а не из одной константы.',
        'Назначение автомобиля закрепляется за заявкой после принятия.',
    ]),
    ('1+ промежуточных точек', GOLD, [
        'API /api/route принимает waypoints/via.',
        'Поддерживаются координаты, ID стоянки, ID сервисной зоны.',
        'Задержки: склад 30 с, пересадка 10 с.',
        'UI: карточка «Маршрут с промежуточными точками».',
        'Проверено: 1 и 2 промежуточные точки, 229 координат.',
    ]),
    ('Динамика и точки вне графа', GREEN, [
        'Маршрут пересчитывается по GPS инженера и спецтехники.',
        'Старт и финиш вне графа привязываются к ближайшему ребру.',
        'После восстановления связи маршрут пересчитывается автоматически.',
        'Диспетчер видит отклонение > 120 м как рассогласование данных.',
        'Мобильная карта поворачивается по азимуту, как в Яндекс Картах.',
    ]),
]
for i, (title, color, items) in enumerate(cards):
    x = 0.5 + i * 4.16
    box(s, x, 1.35, 3.95, 3.55, '', WHITE, LINE, True)
    box(s, x, 1.35, 3.95, 0.48, '', color, color, True)
    text(s, x + 0.16, 1.44, 3.6, 0.3, title, 12, WHITE, True)
    rich(s, x + 0.16, 1.98, 3.6, 2.75,
         [{'bullet': True, 'runs': [{'text': item, 'size': 9.2}]} for item in items], spacing=5)
# bottom flow
box(s, 0.5, 5.12, 12.28, 1.55, '', NAVY, NAVY, True)
text(s, 0.75, 5.28, 11.8, 0.3, 'Как принимается решение о транспорте', 12, GOLD, True)
flow = ['GPS инженера', 'вариант пешком', 'вариант на машине', 'сравнение ETA', 'выбор и резерв']
for i, label in enumerate(flow):
    x = 0.8 + i * 2.42
    box(s, x, 5.72, 2.08, 0.55, label, RGBColor(0x10, 0x2C, 0x4E), RGBColor(0x51, 0x78, 0xA8),
        True, 9.5, WHITE, True, PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    if i < len(flow) - 1:
        arrow(s, x + 2.09, 5.99, x + 2.36, 5.99, GOLD, 1.6)
footer(s)

# ============================================================
# 8. Interfaces
# ============================================================
s = prs.slides.add_slide(BLANK)
header(s, 8, 'Интерфейсы и полевая работа', 'Диспетчер, инженер и мобильное устройство используют один серверный маршрут')
shots = [
    ('Диспетчерская ВДНХ', 'Карта, зоны, объекты, кандидаты и журнал', 'dispatcher_final.png', 0.45, 1.28, 6.2, 4.65),
    ('Мобильный инженер', 'GPS, карта, инструмент, офлайн-баннер', 'mobile_mobile_engineer.png', 6.78, 1.28, 2.55, 4.65),
    ('Навигация', 'Поворотные подсказки и озвучивание', 'mobile_nav_active.png', 9.48, 1.28, 2.55, 4.65),
]
for title, subtitle, filename, x, y, w, h in shots:
    path = os.path.join(BASE, 'assets', filename)
    if os.path.exists(path):
        pic = s.shapes.add_picture(path, Inches(x), Inches(y), width=Inches(w), height=Inches(h))
        pic.line.color.rgb = LINE
        pic.line.width = Pt(1.2)
    else:
        box(s, x, y, w, h, 'Screenshot', WHITE, LINE, True, 10, MUTED, False, PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    text(s, x + 0.2, y + h + 0.08, w - 0.3, 0.22, title, 9, NAVY, True)
    text(s, x + 0.2, y + h + 0.30, w - 0.3, 0.22, subtitle, 7.5, MUTED)
box(s, 0.5, 6.62, 12.3, 0.45, '', BLUE, BLUE, True)
text(s, 0.7, 6.71, 11.9, 0.28,
     'Единый источник маршрута: сервер сохраняет ROUTE JSON; desktop, mobile и диспетчер рисуют одинаковый path и ETA.',
     9, WHITE, True, anchor=MSO_ANCHOR.MIDDLE)
footer(s)

# ============================================================
# 9. Journal, risk, explainability
# ============================================================
s = prs.slides.add_slide(BLANK)
header(s, 9, 'Журнал, риск и объяснимость', 'Диспетчер видит не только результат, но и основания')
# left journal
box(s, 0.55, 1.32, 6.05, 5.55, '', WHITE, LINE, True)
text(s, 0.78, 1.48, 5.5, 0.3, 'Журнал событий', 14, NAVY, True)
rich(s, 0.78, 1.90, 5.55, 4.6, [
    {'bullet': True, 'runs': [{'text': 'фиксация заявки, кандидаты, назначение, переназначение;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'изменения статусов, построение и пересчёт маршрута;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'прибытие ВС по расписанию и фактическое;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'риск норматива, нарушение, ошибки приёма;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'время формирования списка: duration_ms и duration_sec;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'UTC-метки с миллисекундами, append-only JSONL;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'выгрузка CSV и Excel, одна строка = одно событие.', 'size': 9.5}]},
    {'runs': [{'text': 'Проверено: 26 автоматических проверок PASS;', 'size': 9.5, 'bold': True, 'color': GREEN}]},
    {'runs': [{'text': 'факт прибытия в геозоне: 0.3 мин, normal_ok.', 'size': 9.5, 'color': GREEN}]},
], spacing=5)
# right risk chronology
box(s, 6.75, 1.32, 6.05, 2.62, '', WHITE, RED, True)
text(s, 6.98, 1.48, 5.5, 0.3, 'Упреждающий риск', 14, RED, True)
rich(s, 6.98, 1.86, 5.55, 1.95, [
    {'bullet': True, 'runs': [{'text': 'прогноз = текущее время + ETA маршрута;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'сравнение с дедлайном «фиксация + 15 мин»;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'предупреждение заранее + альтернативный инженер;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'учёт пересадки и склада в ETA;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'после прибытия риск снимается, фиксируется факт.', 'size': 9.5}]},
], spacing=5)
box(s, 6.75, 4.15, 6.05, 2.72, '', WHITE, GOLD, True)
text(s, 6.98, 4.31, 5.5, 0.3, 'Объяснимость подбора', 14, ORANGE, True)
rich(s, 6.98, 4.69, 5.55, 2.05, [
    {'bullet': True, 'runs': [{'text': 'по каждому кандидату: допуск, ATA, загрузка, ETA, запас;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'по отклонённым: занят/этап, нет допуска, нет координат;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'причина выбора — совокупность факторов;', 'size': 9.5}]},
    {'bullet': True, 'runs': [{'text': 'пустой список тоже объясняется.', 'size': 9.5}]},
], spacing=5)
footer(s)

# ============================================================
# 10. Results / contacts
# ============================================================
s = prs.slides.add_slide(BLANK)
set_bg(s, NAVY)
box(s, 0, 0, 13.333, 7.5, '', fill=NAVY, line=NAVY, radius=False)
text(s, 0.75, 0.55, 11.9, 0.3, 'ИТОГИ ФИНАЛЬНОГО ЭТАПА', 11, GOLD, True)
text(s, 0.75, 1.0, 11.9, 0.7, 'Готово к испытаниям на ВДНХ', 34, WHITE, True)
checks = [
    'Маршруты: граф, 5 км/ч, спецтранспорт 5/8/10/20, промежуточные точки, динамика.',
    'Поле: GPS 2 Гц, автофиксация прибытия, норматив и факт в журнале.',
    'Диспетчер: объяснимый подбор, риск заранее, переназначение по роли.',
    'Данные: открытый каталог дефектов, ATA, MEL/AOG, несколько AOG.',
    'Журнал: все события, UTC-мс, CSV/XLSX.',
]
rich(s, 0.8, 1.95, 11.5, 3.2,
     [{'bullet': True, 'runs': [{'text': item, 'size': 13, 'color': WHITE}]} for item in checks], spacing=10)
box(s, 0.75, 5.4, 5.7, 1.0, '', RGBColor(0x0E, 0x2B, 0x4D), RGBColor(0x2E, 0x59, 0x8A), True)
text(s, 0.95, 5.54, 5.3, 0.25, 'Доступ к решению', 9, GOLD, True)
text(s, 0.95, 5.80, 5.3, 0.4, 'http://<сервер>:5000 · dispatcher@aeroflot.ru', 10, WHITE)
box(s, 6.65, 5.4, 5.95, 1.0, '', RGBColor(0x0E, 0x2B, 0x4D), RGBColor(0x2E, 0x59, 0x8A), True)
text(s, 6.85, 5.54, 5.55, 0.25, 'Исходный код и презентация', 9, GOLD, True)
text(s, 6.85, 5.80, 5.55, 0.4, 'github.com/MaksimBezdelin/Dorabotka_InTrAvia', 10, WHITE)
text(s, 0.75, 6.85, 11.9, 0.3, 'InTrAvia · команда финального этапа · 25.11.2026', 9,
     RGBColor(0x8F, 0xA9, 0xC4))

prs.save(OUT)
print('saved', OUT)
