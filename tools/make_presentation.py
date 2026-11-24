# -*- coding: utf-8 -*-
"""Презентация InTrAvia для финала ИТ-2026 («Авиаперевозки»).

Принцип: шаблон — только оформление (16:9, фирменные цвета/шрифты).
Содержание строится по критериям PDF: по одному слайду на критерий 4–22,
отдельные слайды для критериев 1–3 и дополнительных Д1–Д3.

Шаблон: C:\\Users\\Вячеслав\\Downloads\\Shablon_prezentatsii_final.pptx
Результат: C:\\Users\\Вячеслав\\Desktop\\InTrAvia_ИТ2026_финал.pptx
"""
import copy
import os

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

TEMPLATE = r'C:\Users\Вячеслав\Downloads\Shablon_prezentatsii_final.pptx'
OUTPUT = r'C:\Users\Вячеслав\Desktop\InTrAvia_ИТ2026_финал.pptx'
FALLBACK = r'C:\Users\Вячеслав\Desktop\InTrAvia_ИТ2026_финал_criteria.pptx'

ACCENT = RGBColor(0xEC, 0x67, 0x4C)
BLUE = RGBColor(0x30, 0x5B, 0xA7)
DARK = RGBColor(0x33, 0x3F, 0x4F)
GREY = RGBColor(0x55, 0x66, 0x77)
RED = RGBColor(0xC0, 0x00, 0x00)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
BLUE_LIGHT = RGBColor(0xF2, 0xF6, 0xFC)
GREY_LIGHT = RGBColor(0xF5, 0xF6, 0xF8)
GREEN_FILL = RGBColor(0xE8, 0xF5, 0xE9)
GREEN_LINE = RGBColor(0x2E, 0x7D, 0x32)
GREEN_TEXT = RGBColor(0x1B, 0x5E, 0x20)
AMBER_FILL = RGBColor(0xFF, 0xF4, 0xE0)
AMBER_LINE = RGBColor(0xE6, 0x8A, 0x00)
AMBER_TEXT = RGBColor(0x8A, 0x52, 0x00)
PART_FILL = RGBColor(0xFD, 0xEC, 0xEA)
PART_TEXT = RGBColor(0x9A, 0x2E, 0x1E)
BORDER_BLUE = RGBColor(0xB8, 0xC8, 0xE0)
BORDER_GREY = RGBColor(0xC9, 0xCE, 0xD6)


# ---------------------------------------------------------------------------
# Базовые помощники шаблона
# ---------------------------------------------------------------------------

def _template_rpr(text_frame):
    for paragraph in text_frame.paragraphs:
        for run in paragraph.runs:
            if run.text.strip():
                return copy.deepcopy(run._r.get_or_add_rPr())
    return None


def set_lines(shape, lines, size=None, bullet=False, color=None):
    if shape is None or not shape.has_text_frame:
        return
    tf = shape.text_frame
    rpr = _template_rpr(tf)
    ppr = None
    if tf.paragraphs:
        ppr = copy.deepcopy(tf.paragraphs[0]._p.get_or_add_pPr())
    for paragraph in list(tf.paragraphs)[1:]:
        paragraph._p.getparent().remove(paragraph._p)
    first = tf.paragraphs[0]
    for run in list(first.runs):
        run._r.getparent().remove(run._r)
    for index, raw in enumerate(lines):
        paragraph = first if index == 0 else tf.add_paragraph()
        if ppr is not None and index > 0:
            paragraph._p.insert(0, copy.deepcopy(ppr))
        text = ('• ' + raw) if bullet and not raw.startswith(('•', '—')) else raw
        run = paragraph.add_run()
        run.text = text
        if rpr is not None:
            run._r.insert(0, copy.deepcopy(rpr))
        if size:
            run.font.size = Pt(size)
        if color is not None:
            run.font.color.rgb = color
        paragraph.space_after = Pt(4)


def iter_shapes(shapes):
    for shape in shapes:
        yield shape
        if shape.shape_type == 6:
            for child in iter_shapes(shape.shapes):
                yield child


def find_shape(slide, name):
    for shape in iter_shapes(slide.shapes):
        if shape.name == name:
            return shape
    return None


# ---------------------------------------------------------------------------
# Примитивы
# ---------------------------------------------------------------------------

def _fill_tf(tf, lines, size=10, bold=False, color=DARK,
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.MIDDLE, space=2):
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = Inches(0.07)
    tf.margin_right = Inches(0.05)
    tf.margin_top = Inches(0.03)
    tf.margin_bottom = Inches(0.02)
    for index, item in enumerate(lines):
        paragraph = tf.paragraphs[0] if index == 0 else tf.add_paragraph()
        paragraph.alignment = align
        paragraph.space_after = Pt(space)
        paragraph.line_spacing = 1.0
        if isinstance(item, tuple):
            text, item_size, item_bold, item_color = item
        else:
            text, item_size, item_bold, item_color = item, size, bold, color
        run = paragraph.add_run()
        run.text = text
        run.font.size = Pt(item_size)
        run.font.bold = item_bold
        run.font.color.rgb = item_color
        run.font.name = 'Arial'


def add_box(slide, x, y, w, h, lines, fill, border, size=10, bold=False,
            color=DARK, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.MIDDLE,
            radius=0.08, space=2, line_w=0.75):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.adjustments[0] = radius
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.color.rgb = border
    shape.line.width = Pt(line_w)
    _fill_tf(shape.text_frame, lines, size, bold, color, align, anchor, space)
    return shape


def add_label(slide, x, y, w, h, lines, size=10, bold=False, color=DARK,
              align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, space=2):
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    _fill_tf(shape.text_frame, lines, size, bold, color, align, anchor, space)
    return shape


def add_arrow(slide, x, y, w, h=0.20, color=ACCENT):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RIGHT_ARROW, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def add_rule(slide, x, y, w, h, color):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def add_top_brand(slide, right='Авиаперевозки · финал'):
    add_label(slide, 0.45, 0.07, 8.0, 0.24,
              ['InTrAvia · ИТ-2026'], size=9.5, bold=True, color=GREY)
    add_label(slide, 8.3, 0.07, 4.55, 0.24, [right], size=9.5, bold=True,
              color=ACCENT, align=PP_ALIGN.RIGHT)


def ball_word(count):
    # После «до» для 1 — родительный падеж «балла», для остальных — «баллов».
    return 'балла' if count == 1 else 'баллов'


def add_criterion_header(slide, title, jury):
    add_top_brand(slide)
    add_label(slide, 0.45, 0.34, 12.45, 0.74,
              [(title, 19.5, True, BLUE)])
    add_rule(slide, 0.50, 1.12, 12.33, 0.035, ACCENT)
    add_box(slide, 0.50, 1.20, 12.33, 0.54,
            [('ЧТО ПРОВЕРЯЕТ ЖЮРИ', 7.8, True, BLUE),
             (jury, 9.3, False, DARK)],
            BLUE_LIGHT, BORDER_BLUE, radius=0.06, anchor=MSO_ANCHOR.TOP,
            space=1)
    return slide


def add_status(slide, text, kind='closed'):
    if kind == 'closed':
        fill, line, color = GREEN_FILL, GREEN_LINE, GREEN_TEXT
    elif kind == 'partial':
        fill, line, color = PART_FILL, ACCENT, PART_TEXT
    else:
        fill, line, color = AMBER_FILL, AMBER_LINE, AMBER_TEXT
    add_box(slide, 0.50, 6.42, 12.33, 0.62,
            [('СТАТУС: ' + text, 10.2, True, color)],
            fill, line, radius=0.08)


def add_criterion_slide(presentation, spec):
    slide = presentation.slides.add_slide(presentation.slide_layouts[12])
    add_criterion_header(
        slide,
        'Критерий %s. %s — до %d %s' % (
            spec['num'], spec['title'], spec['max'], ball_word(spec['max'])),
        spec['jury'])
    impl = [('ЧТО РЕАЛИЗОВАНО', 9.0, True, BLUE)]
    impl += [('• ' + line, 9.6, False, DARK) for line in spec['impl']]
    add_box(slide, 0.50, 1.84, 7.72, 4.44, impl, WHITE, BLUE,
            radius=0.03, anchor=MSO_ANCHOR.TOP, space=4)
    proof = [('ПОДТВЕРЖДЕНИЕ', 9.0, True, BLUE)]
    proof += [('• ' + line, 8.9, False, DARK) for line in spec['proof']]
    add_box(slide, 8.38, 1.84, 4.45, 4.44, proof, GREY_LIGHT, BORDER_GREY,
            radius=0.03, anchor=MSO_ANCHOR.TOP, space=4)
    add_status(slide, spec['status'], spec.get('kind', 'closed'))
    return slide


def ensure_table_rows(table, count):
    tbl = table._tbl
    while len(table.rows) < count:
        rows = tbl.findall(qn('a:tr'))
        tbl.append(copy.deepcopy(rows[-1]))


def fill_table(slide, rows, widths, top, height, header_size=9.5,
               body_size=8.8):
    for shape in slide.shapes:
        if not shape.has_table:
            continue
        table = shape.table
        ensure_table_rows(table, len(rows))
        shape.left = Inches(0.52)
        shape.top = Inches(top)
        shape.width = Inches(sum(widths))
        shape.height = Inches(height)
        for index, width in enumerate(widths):
            table.columns[index].width = Inches(width)
        header_h = 0.30
        body_h = max(0.26, (height - header_h) / max(1, len(rows) - 1))
        for r, row in enumerate(rows):
            table.rows[r].height = Inches(header_h if r == 0 else body_h)
            for c, value in enumerate(row):
                cell = table.cell(r, c)
                cell.text = str(value)
                cell.margin_left = Inches(0.05)
                cell.margin_right = Inches(0.04)
                cell.margin_top = Inches(0.01)
                cell.margin_bottom = Inches(0.01)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.fill.solid()
                if r == 0:
                    cell.fill.fore_color.rgb = BLUE
                else:
                    cell.fill.fore_color.rgb = WHITE if r % 2 else BLUE_LIGHT
                for paragraph in cell.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(header_size if r == 0 else body_size)
                        run.font.bold = (r == 0)
                        run.font.name = 'Arial'
                        run.font.color.rgb = WHITE if r == 0 else DARK
        return


# ---------------------------------------------------------------------------
# Управление слайдами шаблона
# ---------------------------------------------------------------------------

def keep_only_template_slides(presentation, keep):
    # Убираем слайды только из sldIdLst. Части шаблона остаются в пакете
    # сиротами: так новые слайды получают свободные имена slideN.xml и
    # в PPTX не возникает дублей частей.
    sld_ids = presentation.slides._sldIdLst
    for index, sld in enumerate(list(sld_ids)):
        if index in keep:
            continue
        sld_ids.remove(sld)


# ---------------------------------------------------------------------------
# Слайды 1–3
# ---------------------------------------------------------------------------

def build_title(presentation):
    s = presentation.slides[0]
    for shape in list(s.shapes):
        if shape.shape_type == 13:
            top = Emu(shape.top).inches
            width = Emu(shape.width).inches
            if 2.6 < top < 2.9 and 6.0 < width < 6.5:
                shape._element.getparent().remove(shape._element)
    box = find_shape(s, 'TextBox 12')
    box.left = Inches(0.7)
    box.top = Inches(1.20)
    box.width = Inches(11.9)
    box.height = Inches(4.6)
    set_lines(box, [
        'ИТ-2026 · Финал · Направление «Авиаперевозки»',
        'InTrAvia',
        'Умная диспетчеризация инженеров\nна полигоне ВДНХ',
        'Команда InTrAvia · Москва',
    ], size=18)
    for paragraph, size in zip(box.text_frame.paragraphs, [15, 52, 21, 14]):
        paragraph.alignment = PP_ALIGN.CENTER
        for run in paragraph.runs:
            run.font.size = Pt(size)


def build_team(presentation):
    s = presentation.slides[2]
    set_lines(find_shape(s, 'TextBox 32'), ['InTrAvia'])
    set_lines(find_shape(s, 'TextBox 4'), ['Команда InTrAvia'])
    set_lines(find_shape(s, 'TextBox 31'), ['Наставник: —'], color=RED, size=14)
    set_lines(find_shape(s, 'TextBox 34'), ['Москва'])
    set_lines(find_shape(s, 'TextBox 36'), ['Организация: —'], color=RED, size=14)
    for name in ('TextBox 31', 'TextBox 36'):
        shape = find_shape(s, name)
        if shape is not None:
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.bold = True
    add_label(s, 7.10, 5.62, 5.10, 0.55,
              [('! Заполнить данные наставника и организации до сдачи.',
                10.5, True, RED)])
    set_lines(find_shape(s, 'TextBox 25'), [''])
    shot = r'C:\Users\Вячеслав\Desktop\admin-agent-map.png'
    if os.path.isfile(shot):
        s.shapes.add_picture(shot, Inches(7.15), Inches(1.65), width=Inches(5.15))


def build_overview(presentation):
    slide = presentation.slides.add_slide(presentation.slide_layouts[12])
    add_top_brand(slide)
    add_label(slide, 0.45, 0.34, 12.45, 0.58,
              [('ЧТО ЗА СИСТЕМА', 26, True, BLUE)])
    add_rule(slide, 0.50, 1.02, 12.33, 0.035, ACCENT)
    add_label(slide, 0.52, 1.10, 12.2, 0.36,
              [('Единая цепочка заявки, маршрут по графу, прогноз прибытия '
                'и журнал каждого решения.', 11, False, GREY)])

    cards = [
        ('ПРОБЛЕМА', [
            'Пилот сообщает о дефекте, назначение инженера и норматив 15 минут ведутся вручную.',
            'Простой воздушного судна, непрозрачный выбор исполнителя, потеря времени на согласования.',
        ]),
        ('РЕШЕНИЕ', [
            'Заявка: пилот → MCC/ЦПУ → диспетчер → инженер.',
            'Маршрут по трём графам, ETA, автоприбытие по геозоне, warning/breach.',
        ]),
        ('РОЛИ', [
            'MCC/ЦПУ, диспетчер (начальник смены), полевой инженер, администратор.',
            'Права проверяются на сервере: действие не по роли — 403.',
        ]),
        ('МАСШТАБ', [
            '124 маршрута, 10 модулей intravia/, 3 графа ВДНХ.',
            '3831 запись FIM 007-00, 10 стоянок SZ, 325 дорожных сегментов.',
        ]),
    ]
    for index, (header, bullets) in enumerate(cards):
        x = 0.50 + (index % 2) * 6.22
        y = 1.58 + (index // 2) * 1.95
        add_box(slide, x, y, 6.05, 1.82, [], WHITE, BLUE, radius=0.04)
        add_box(slide, x, y, 6.05, 0.38, [(header, 9.5, True, WHITE)],
                BLUE if index % 2 == 0 else ACCENT,
                BLUE if index % 2 == 0 else ACCENT, radius=0.12)
        add_label(slide, x + 0.10, y + 0.45, 5.85, 1.30,
                  ['• ' + b for b in bullets], size=9.8, space=5)
    add_box(slide, 0.50, 5.58, 12.33, 1.24,
            [('Полигон ВДНХ · 10 стоянок SZ-1…SZ-10 · 6 объектов · '
              '325 дорожных сегментов', 12, True, BLUE),
             ('Норматив 15 минут · SSE 500 мс · журнал ISO 8601 с миллисекундами',
              10.5, False, DARK)],
            BLUE_LIGHT, BORDER_BLUE, radius=0.05)
    return slide


# ---------------------------------------------------------------------------
# Критерий 1 — схема архитектуры
# ---------------------------------------------------------------------------

def build_c1(presentation):
    slide = presentation.slides.add_slide(presentation.slide_layouts[12])
    add_criterion_header(
        slide, 'Критерий 1. Схема архитектуры — до 3 баллов',
        'Наличие схемы: основные модули, связи между ними и технологический стек.')
    add_box(slide, 0.56, 1.82, 12.20, 4.50, [], WHITE, ACCENT, radius=0.02,
            line_w=1.0)

    add_label(slide, 0.72, 1.88, 2.42, 0.28,
              ['КЛИЕНТЫ · HTML/JS'], size=10, bold=True, color=BLUE)
    add_label(slide, 3.55, 1.88, 5.45, 0.28,
              ['СЕРВЕР · Python 3.13, Flask, waitress'], size=10, bold=True,
              color=BLUE)
    add_label(slide, 9.40, 1.88, 3.16, 0.28,
              ['ДАННЫЕ · SQLite, GeoJSON'], size=10, bold=True, color=BLUE)

    clients = [
        ('Браузеры ролей', 'пилот · MCC/ЦПУ · диспетчер · инженер · админ'),
        ('Android WebView / Capacitor', 'клиенты «Инженер» и «Агенты»'),
        ('Имитатор организаторов', 'POST /api/v1/track — точка или пакет'),
        ('Карты Leaflet', 'локальный vendor, обновление по SSE'),
    ]
    for index, (title, subtitle) in enumerate(clients):
        add_box(slide, 0.72, 2.18 + index * 0.84, 2.42, 0.74,
                [(title, 9.0, True, DARK), (subtitle, 7.4, False, GREY)],
                RGBColor(0xFD, 0xF0, 0xEC), ACCENT, radius=0.10, space=0)

    add_box(slide, 3.55, 2.18, 5.45, 3.55, [], BLUE_LIGHT, BLUE, radius=0.03)
    add_label(slide, 3.68, 2.21, 5.20, 0.40,
              [('app.py — 124 маршрута', 9.4, True, BLUE),
               ('Flask + waitress · auth before/after', 7.3, False, DARK)])
    add_label(slide, 3.68, 2.61, 5.20, 0.21,
              [('REST /api/* ← клиенты   ·   SSE /api/v1/stream → карты',
                7.8, False, GREY)])
    modules = [
        ('config', 'конфиг'), ('events', 'SSE + журнал'),
        ('authz', 'роли, 403'), ('db', 'SQLite'),
        ('geo', 'геозоны'), ('routing', 'графы, ETA'),
        ('faults', 'FIM/MEL/AOG'), ('candidates', 'скоринг'),
        ('requests_logic', 'норматив'), ('tracking', 'GPS'),
    ]
    for index, (name, subtitle) in enumerate(modules):
        col = index % 2
        row = index // 2
        add_box(slide, 3.68 + col * 2.62, 2.86 + row * 0.43, 2.50, 0.37,
                [(name, 8.0, True, BLUE), (subtitle, 6.8, False, DARK)],
                WHITE, BORDER_BLUE, radius=0.12, space=0)
    add_label(slide, 3.68, 5.06, 5.20, 0.58,
              [('Слои: config → db/events/authz → geo → routing → faults → '
                'candidates → requests_logic → tracking', 7.0, False, GREY)])

    data = [
        ('SQLite database.db', '16 учёток · 3831 FIM · MEL 3553 / AOG 278'),
        ('location.json + GeoJSON', '10 стоянок, 6 объектов, 325 дорог'),
        ('NetworkX — 3 графа', '431/459 · 332/348 · 351/379'),
        ('data/events.jsonl', 'append-only, ISO мс → XLSX/CSV'),
        ('Whisper / Ollama', 'опционально'),
    ]
    for index, (title, subtitle) in enumerate(data):
        height = 0.38 if index == 4 else 0.62
        add_box(slide, 9.40, 2.18 + index * 0.70, 3.16, height,
                [(title, 8.0, True, DARK), (subtitle, 6.8, False, GREY)],
                GREY_LIGHT, BORDER_GREY, radius=0.08, space=0)
    for y in (2.42, 4.05):
        add_arrow(slide, 3.19, y, 0.30, 0.18)
        add_arrow(slide, 9.03, y, 0.31, 0.18)
    add_label(slide, 0.70, 5.82, 11.9, 0.42,
              [('Связи: браузеры ↔ REST /api/*; сервер → SSE /api/v1/stream → '
                'карты; сервер ↔ SQLite и GeoJSON; сервер → графы и events.jsonl '
                '→ XLSX.', 8.0, False, GREY)])
    add_status(slide, 'ЗАКРЫТ — схема соответствует коду: app.py + 10 модулей '
                      'intravia/, стек указан.', 'closed')
    return slide


# ---------------------------------------------------------------------------
# Критерий 2 — список реализованного
# ---------------------------------------------------------------------------

def build_c2(presentation):
    slide = presentation.slides.add_slide(presentation.slide_layouts[12])
    add_criterion_header(
        slide, 'Критерий 2. Соответствие функционала — до 3 баллов',
        'Список реализованного функционала и его соответствие факту во время '
        'финальных испытаний.')
    slide.shapes.add_table(10, 4, Inches(0.52), Inches(1.82), Inches(12.30),
                           Inches(4.46))
    fill_table(slide, [
        ['Блок', 'Что реализовано', 'Чем подтверждается', 'Есть'],
        ['Заявка и цепочка',
         'Пилот → MCC/ЦПУ → диспетчер → инженер; пилот не выбирает инженера',
         'Очереди MCC и диспетчера, история', 'да'],
        ['Маршрутизация',
         '3 графа NetworkX; скорости 5/8/10/20; waypoints; точки вне графа',
         'GET/POST /api/route, pytest', 'да'],
        ['Спецтранспорт',
         'Сравнение «пешком / с машиной»; force_transport в §5.1',
         'choose_transport, /api/route', 'да'],
        ['Геопозиция',
         'Интерфейс инженера + POST /api/v1/track; SSE; автоприбытие',
         'Контракт §8, /api/v1/stream', 'да'],
        ['Подбор',
         'Допуск ATA, занятость, ETA, загрузка, score; причина отказа',
         'GET /api/dispatcher/candidates', 'да'],
        ['Классификатор',
         'FIM 3831, 12 правил, 33 главы ATA; MEL/AOG; свободный текст',
         'GET /api/faults/classify', 'да'],
        ['Журнал',
         'ISO 8601 с мс, duration_ms, append-only, XLSX и CSV',
         'GET /api/v1/events/export.*', 'да'],
        ['§5.1 организаторам',
         'test_options: норматив, force_transport, пауза агента',
         'POST /api/requests/<rid>/test_options', 'да'],
        ['Интерфейсы',
         'Веб-роли, mobile fullscreen, карта агентов, локальный Leaflet',
         'templates/, mobile/www, APK', 'да'],
    ], widths=[1.90, 5.35, 3.95, 1.10], top=1.82, height=4.46,
        header_size=9.5, body_size=8.7)
    add_status(slide, 'ЗАКРЫТ — live 20/20 + сценарный прогон 33/33, pytest '
                      '75 passed / 104 skipped / 0 failed; каждая строка '
                      'проверена.', 'closed')
    return slide


# ---------------------------------------------------------------------------
# Критерий 3 — процесс разработки
# ---------------------------------------------------------------------------

def build_c3(presentation):
    slide = presentation.slides.add_slide(presentation.slide_layouts[12])
    add_criterion_header(
        slide, 'Критерий 3. Процесс разработки — до 3 баллов',
        'Три блока: автоматизация и инструменты, контроль качества кода, '
        'авторство кода. Оценивается конкретность фактов.')
    blocks = [
        ('1 · АВТОМАТИЗАЦИЯ И ИНСТРУМЕНТЫ', ACCENT, [
            'Итерации: цепочка ролей → графы → спецтранспорт и ETA → мобильные клиенты → безопасность → §5.1 → split.',
            'Git: 62 коммита, ветки SlavasTest / main / merged; отдельный коммит на изменение.',
            'pytest после каждого этапа: 75 passed, 104 skipped, 0 failed (179 собрано).',
            'Playwright: сценарии в браузере 15–16.11.2026 — заявки, очередь, отчёты, карта.',
            'Субагенты: аудит бэкенда, фронтенда и мобильного клиента параллельно, у каждого своя зона файлов.',
        ]),
        ('2 · КОНТРОЛЬ КАЧЕСТВА', BLUE, [
            'Аудит 118 маршрутов (на момент аудита) AST-разбором и матрица ролей; найдены P0: GPS чужого инженера, подмена engineer_id, норматив от принятия.',
            'Фиксы: чужой GPS → 403, engineer_id из сессии, норматив от created_at + тикер, §5.1 test_options.',
            'node --check: 20 inline-блоков на 17 страницах и 5 внешних JS — ошибок нет.',
            'py_compile после каждого этапа: app.py 7575 → 4438 строк на этапе split, 119 правил url_map совпадали.',
            'Гейт: 75 passed / 104 skipped до и после split; поведение не изменилось.',
        ]),
        ('3 · АВТОРСТВО', RGBColor(0x2E, 0x7D, 0x8C), [
            'Людьми: постановка цепочки ролей, схема данных, сценарии диспетчера и инженера, приоритеты, приёмка.',
            'Совместно: три графа и проекция точек на ребро, таблица кандидатов, миграция паролей, тесты, аудит, split.',
            'ИИ: черновики шаблонов, комментарии, тексты; принимались только после прогона тестов и чтения diff.',
            'Пример: XSS в карточках найден на ревью, исправлен (escapeHtml / escapeTaskHtml), закрыт тестом.',
            'Полное ведение кодовой базы ИИ не доверялось: решения и приёмка — за командой.',
        ]),
    ]
    for index, (header, color, bullets) in enumerate(blocks):
        x = 0.50 + index * 4.14
        add_box(slide, x, 1.84, 3.94, 4.44, [], WHITE, color, radius=0.03)
        add_box(slide, x, 1.84, 3.94, 0.40, [(header, 9.5, True, WHITE)],
                color, color, radius=0.10)
        add_label(slide, x + 0.09, 2.32, 3.76, 3.88,
                  ['• ' + b for b in bullets], size=8.6, space=4)
    add_status(slide, 'ЗАКРЫТ — три блока с конкретными фактами; live 20/20.',
               'closed')
    return slide


# ---------------------------------------------------------------------------
# Финал
# ---------------------------------------------------------------------------

def build_final(presentation):
    slide = presentation.slides.add_slide(presentation.slide_layouts[12])
    add_top_brand(slide)
    add_label(slide, 0.45, 0.34, 12.45, 0.58,
              [('ДЕМО И КОНТАКТЫ', 26, True, BLUE)])
    add_rule(slide, 0.50, 1.02, 12.33, 0.035, ACCENT)

    add_box(slide, 0.50, 1.20, 7.55, 5.20, [], WHITE, BLUE, radius=0.03)
    add_label(slide, 0.66, 1.28, 7.25, 0.34,
              [('СЦЕНАРИЙ ДЕМО', 10, True, BLUE)])
    add_label(slide, 0.66, 1.66, 7.25, 4.60, [
        '• Пилот создаёт заявку: борт, стоянка, дефект текстом, серьёзность.',
        '• MCC/ЦПУ проверяет MEL/ATA и отправляет заявку диспетчеру.',
        '• Диспетчер видит кандидатов с ETA, загрузкой и причинами, назначает.',
        '• Инженер принимает, идёт по маршруту; GPS → SSE → карты; автоприбытие.',
        '• Норматив 15 минут: warning/breach в журнале; выгрузка XLSX/CSV.',
        '• §5.1: норматив, force_transport, пауза — POST test_options.',
    ], size=10.5, space=7)

    add_box(slide, 8.25, 1.20, 4.58, 5.20, [], WHITE, ACCENT, radius=0.03)
    add_label(slide, 8.40, 1.28, 4.30, 0.34,
              [('СТЕНД И РЕПОЗИТОРИЙ', 10, True, BLUE)])
    contacts = add_label(slide, 8.40, 1.66, 4.30, 4.60, [
        '• waitress + Cloudflare Tunnel, HTTPS; URL — на защите.',
        '• Репозиторий Dorabotka_InTrAvia, ветки SlavasTest / main / merged.',
        '• 124 маршрута, 10 модулей intravia/, pytest 75/104/0.',
        '• БД: integrity ok; MEL 3553 / AOG 278 / FIM 3831.',
        '• Контакты команды: — (заполнить до защиты)',
    ], size=10.5, space=7)
    for paragraph in contacts.text_frame.paragraphs:
        if 'Контакты команды' in paragraph.text or 'Заполнить контакты' in paragraph.text:
            for run in paragraph.runs:
                run.font.color.rgb = RED
                run.font.bold = True
    add_box(slide, 0.50, 6.50, 12.33, 0.56,
            [('Спасибо! Готовы ответить на вопросы.', 13, True, WHITE)],
            BLUE, BLUE, radius=0.10)
    return slide


# ---------------------------------------------------------------------------
# Критерии 4–22 и Д1–Д3
# ---------------------------------------------------------------------------

CRITERIA = [
    dict(num=4, title='Адаптация под полигон ВДНХ', max=5,
         jury='Возможность использовать решение с другими аэропортами на '
              'примере испытательного полигона ВДНХ.',
         impl=[
             "Полигон ВДНХ по умолчанию: DEFAULT_AIRPORT='vdnh'; SVO — второй полигон.",
             'Геоданные: config/location.json + static/geodata/vdnh/ (границы, дороги, объекты).',
             '3 графа NetworkX: 431/459, 332/348, 351/379 узлов/рёбер.',
             '10 стоянок SZ-1…SZ-10, 6 объектов, 325 дорожных сегментов.',
             'Стартовые позиции: tools/apply_vdnh_location.py (идемпотентно, с бэкапом БД).',
         ],
         proof=[
             'Startup-лог: 10 стоянок, 6 объектов, 325 дорог.',
             'pytest tests/test_vdnh.py — passed.',
             'Карта Leaflet: границы, дороги, зоны, объекты, агенты.',
         ],
         status='ЗАКРЫТ — полигон адаптирован, цифры совпадают с GeoJSON.'),
    dict(num=5, title='Маршрутизация по графу', max=5,
         jury='Маршрут строится по графу дорог, учитывает ограничения на '
              'передвижение и скоростной режим участков.',
         impl=[
             'GET/POST /api/route: path, legs, distance_m, time_s, eta_min, warnings.',
             'Вес ребра = distance/(speed/3.6); скорости 5/8/10/20 км/ч из road_rules.',
             'Пешеходный граф без taxi/runway; авиационный — только для ВС.',
             'Стоянки — полигоны SZ-N; старые числовые id поддержаны как алиасы.',
         ],
         proof=[
             'pytest: маршрут по графу, а не по прямой.',
             'live /api/route: 200, eta_min и warnings в ответе.',
             '3 графа: 431/459, 332/348, 351/379.',
         ],
         status='ЗАКРЫТ — маршрут по графу со скоростями подтверждён.'),
    dict(num=6, title='Маршрут с промежуточными точками', max=4,
         jury='Маршрут с 1 и более промежуточными точками: склад за '
              'инструментом, парковка спецтранспорта, пересадка.',
         impl=[
             'build_route(..., waypoints); POST /api/route принимает список waypoints.',
             'Посадка/высадка — 10 с; склад за инструментом — 30 с за waypoint.',
             'Сравнение «пешком / с машиной» на одном маршруте.',
         ],
         proof=[
             'pytest: time_s растёт на 10/30 с при добавлении waypoint.',
             'live /api/route с waypoints: 200, legs по участкам.',
             'В живом назначении waypoints пока пустые — демо через /api/route.',
         ],
         status='ЗАКРЫТ — критерий демонстрируется через /api/route.'),
    dict(num=7, title='Точки вне графа дорог', max=1,
         jury='Маршрут от агента вне графа дорог до ближайшего ребра и далее '
              'по дорогам.',
         impl=[
             '_edge_projection / _attach_projection: проекция точки на ближайшее ребро.',
             'Маршрут от инженера или машины вне графа до дороги и далее по сети.',
             'Предупреждение в ответе, если точка далеко от ребра.',
         ],
         proof=[
             'pytest: точка вне графа → маршрут строится.',
             'live: инженер у стоянки вне ребра → путь есть.',
         ],
         status='ЗАКРЫТ — проекция и предупреждение работают.'),
    dict(num=8, title='Обновление маршрута по продвижению', max=3,
         jury='Маршрут динамически обновляется по мере продвижения инженера.',
         impl=[
             '_track_apply_position(..., recalc=True) → _recalc_routes_for_engineer.',
             'Пересчёт при обновлении GPS/трека; SSE position двигает метку.',
             'Полевой GPS инженера (PUT /location) пересчитывает маршрут: recalc=True (app.py:2849).',
         ],
         proof=[
             'pytest: маршрут пересчитывается после track.',
             'live /api/v1/track: applied:true, SSE position.',
             'Живой проход на ВДНХ — впереди.',
         ],
         status='ЗАКРЫТ, ЖДЁТ ПОЛЕВОГО ПРОГОНА — пересчёт работает; живой '
                'проход на ВДНХ впереди.',
         kind='field'),
    dict(num=9, title='Применение спецтранспорта', max=5,
         jury='Система учитывает необходимость спецтранспорта, а не использует '
              'его безусловно.',
         impl=[
             'choose_transport сравнивает пешком и с машиной, выбирает минимум времени.',
             'Учитываются путь до парковки, посадка/высадка 10 с.',
             '§5.1: force_transport принудительно включает спецтранспорт для теста.',
         ],
         proof=[
             'pytest: car_route vs walk_route, выбирается быстрейший.',
             'live test_options force_transport=1 → маршрут с машиной.',
         ],
         status='ЗАКРЫТ — сравнение и принудительный режим подтверждены.'),
    dict(num=10, title='Интерфейс инженера', max=5,
         jury='Приём и отклонение заявок, смена статуса, маршрут, позиция, '
              'ETA и корректная работа на мобильном.',
         impl=[
             'Интерфейс инженера: принять/отклонить, статусы, карта, ETA, позиция.',
             'Инструмент: «Есть / Нет / На складе»; маршрут на склад.',
             'Мобильный fullscreen-экран, ВДНХ по умолчанию, локальный Leaflet, компас.',
         ],
         proof=[
             'pytest test_roles / test_dispatch.',
             'Playwright 15–16.11: заявки, очередь, отчёты, карта.',
             'Мобильный код готов; живой прогон — впереди.',
         ],
         status='ЗАКРЫТ, ЖДЁТ ПОЛЕВОГО ПРОГОНА — мобильный экран готов, '
                'проверка на телефоне впереди.',
         kind='field'),
    dict(num=11, title='Работа в полевых условиях', max=5,
         jury='Живой инженер: движение на карте, автоматическое прибытие в '
              'зону, фиксация выполнения или нарушения норматива.',
         impl=[
             'Автоприбытие point-in-polygon по геозоне стоянки; SSE arrival.',
             'В журнале elapsed_min, eta_min, delta_min.',
             'Цикл до completed; отклонение и замена фиксируются.',
         ],
         proof=[
             'pytest: автоприбытие по зоне стоянки.',
             'Live на ВДНХ не проводился — следующий этап.',
         ],
         status='ГОТОВО К ПОЛЕВОМУ ПРОГОНУ — механика готова, живой '
                'проход впереди.',
         kind='field'),
    dict(num=12, title='Приём геопозиции', max=5,
         jury='Приём геопозиции через интерфейс инженера и через API от других '
              'агентов: инженеров, спецтехники, ВС.',
         impl=[
             'PUT /api/engineers/<id>/location|gps|coords — интерфейс инженера.',
             'POST /api/v1/track — контракт §8: точка или пакет; неизвестный obj accepted:false.',
             'Права: чужой GPS → 403, свой → 200; id из сессии.',
         ],
         proof=[
             'live: bad lat/lon 400; unknown obj 200 accepted; eng_1 applied:true.',
             'live: чужой GPS 403, свой 200.',
         ],
         status='ЗАКРЫТ — интерфейс и API подтверждены, права закрыты.'),
    dict(num=13, title='Штатная цепочка заявки', max=5,
         jury='Заявка проходит MCC/ЦПУ → начальник смены (диспетчер) → '
              'инженер, а не назначается пилотом напрямую.',
         impl=[
             'Цепочка: пилот → MCC/ЦПУ (одна роль) → диспетчер → инженер.',
             'Пилот не выбирает инженера; назначает только диспетчер.',
             'Очереди MCC и диспетчера; история переходов.',
         ],
         proof=[
             'live-сценарий 33/33: полная цепочка MCC→ЦПУ→диспетчер→инженер→отчёт.',
             'Прямой маршрут MCC→диспетчер — опция, не замена цепочки.',
         ],
         status='ЗАКРЫТ — цепочка воспроизведена, диспетчера не миновать.'),
    dict(num=14, title='Права и полномочия ролей', max=5,
         jury='Ограничения соответствуют реальным полномочиям; действие не по '
              'роли блокируется.',
         impl=[
             'WRITE_ENDPOINT_ROLES + WRITE_ROLE_PREFIXES; действие не по роли → 403.',
             'engineer_id берётся из сессии; чужой GPS → 403, свой → 200.',
             'MEL/AOG выставляет инженер; AOG — отдельная ветка.',
         ],
         proof=[
             'live: foreign GPS 403, own 200.',
             'pytest test_roles / test_ownership / test_security.',
         ],
         status='ЗАКРЫТ — матрица прав и проверка сессии подтверждены.'),
    dict(num=15, title='Статусная модель работ', max=5,
         jury='Наличие и корректность переходов по статусам; снятие после '
              '«В работе» — только решением старшего смены.',
         impl=[
             'Статусы: assigned → arriving → in_progress → report_submitted → completed.',
             'awaiting_parts — ожидание запчастей, возврат в пул.',
             'Снятие после «В работе» — диспетчер/MCC с подтверждением; история в заявке.',
         ],
         proof=[
             'pytest test_dispatch.',
             'live: переходы и история статусов.',
         ],
         status='ЗАКРЫТ — переходы проверены live.',
         kind='closed'),
    dict(num=16, title='Многокритериальный подбор', max=5,
         jury='Учитываются срочность, местоположение, квалификация, загрузка '
              'и начатая работа; выбирается лучший по совокупности.',
         impl=[
             'build_candidate_table: допуск ATA, занятость, ETA, загрузка, срочность.',
             'score = travel_time×urgency + workload×2 + 0.5 за машину.',
             'awaiting_parts исключён; critical первым; кэш TTL 60 с.',
         ],
         proof=[
             'live: 6 кандидатов, eta=9.0, workload реальный, duration_ms=65.7.',
             'pytest test_dispatch.',
         ],
         status='ЗАКРЫТ — факторы и скоринг подтверждены.'),
    dict(num=17, title='Объяснимость назначения', max=5,
         jury='Показывается сравнение кандидатов с основаниями выбора и '
              'причинами отклонения остальных.',
         impl=[
             'GET /api/dispatcher/candidates/<rid>: eligible, reason, travel_time, eta, workload, score, path.',
             'UI-таблица: допуск, занятость, ETA, загрузка, причина отказа.',
             'Причины: занят, нет допуска, не успевает, уже отказался.',
         ],
         proof=[
             'live: 6 кандидатов, eta=9.0, duration_ms=65.7.',
             'Сравнение альтернатив видно в UI диспетчера.',
         ],
         status='ЗАКРЫТ — таблица кандидатов и причины подтверждены.'),
    dict(num=18, title='Открытый классификатор', max=5,
         jury='Свободный текст, поиск по существующим категориям и главам ATA, '
              'добавление новых типов без правки кода.',
         impl=[
             'GET /api/faults/classify и /catalog; свободный ввод текста.',
             'FIM 007-00: 3831 запись, 12 правил, 33 главы ATA; MEL 3553 / AOG 278.',
             'Новый тип сохраняется без правки кода; needs_review=1.',
         ],
         proof=[
             'live: «течь гидравлики» → MEL/ATA29/5 matches.',
             'live: «двигатель не запускается» → AOG/ATA71/3 matches.',
             'live: «AOG» → AOG.',
         ],
         status='ЗАКРЫТ — свободный текст и MEL/AOG подтверждены.'),
    dict(num=19, title='Несколько срочных заявок и AOG', max=5,
         jury='Конкуренция заявок за людей: AOG, ожидание запчастей, возврат '
              'инженера в пул, смена исполнителя.',
         impl=[
             'Параллельные заявки; awaiting_parts исключает инженера из кандидатов.',
             'Возврат в пул после запчастей; try_reassign_queued.',
             'critical первым; смена исполнителя — через диспетчера.',
         ],
         proof=[
             'код+pytest: awaiting_parts исключён.',
             'live: eta/workload в кандидатах.',
         ],
         status='ЗАКРЫТ — параллельные заявки и возврат в пул подтверждены.'),
    dict(num=20, title='Контроль норматива 15 минут', max=5,
         jury='Норматив считается от фиксации заявки до прибытия; прогноз '
              'обновляется, факт фиксируется по зоне стоянки.',
         impl=[
             'Норматив от created_at (фиксация заявки), не от принятия.',
             'Тикер warning/breach без GPS (не чаще 10 с).',
             'ETA по маршруту; факт прибытия — по геозоне.',
         ],
         proof=[
             'live: warning 3.8 мин, breach −1.2 без телеметрии.',
             'live: тикер пишет события (throttle 10 с).',
         ],
         status='ЗАКРЫТ — норматив от фиксации и тикер подтверждены.'),
    dict(num=21, title='Упреждающее оповещение', max=5,
         jury='Диспетчер узнаёт о риске до нарушения срока; уведомление '
              'содержит вариант решения.',
         impl=[
             '_check_regulation_warning: за 5 мин, SSE warning.',
             'В уведомлении nearest_free и can_make_it — кто ещё успевает.',
             'Тикер без GPS (не чаще 10 с).',
         ],
         proof=[
             'live: warning 3.8 мин, breach −1.2.',
             'SSE warning видит диспетчер.',
         ],
         status='ЗАКРЫТ — предупреждение и вариант решения подтверждены.'),
    dict(num=22, title='Журнал событий', max=5,
         jury='Полнота событий по п. 3.2, время до миллисекунды, время '
              'формирования кандидатов, выгрузка.',
         impl=[
             'Append-only data/events.jsonl; ISO 8601 с миллисекундами.',
             'События п.3.2: заявка, кандидаты, назначение, статусы, маршрут, warning, breach.',
             'duration_ms кандидатов; выгрузка XLSX и CSV.',
         ],
         proof=[
             'live: CSV 200, duration_ms=63.61.',
             'pytest test_export.',
         ],
         status='ЗАКРЫТ — полнота, миллисекунды и выгрузка подтверждены.'),
    dict(num='Д1', title='Доп. сведения о маршруте', max=4,
         jury='Обновление маршрута, текстовые подсказки, озвучка, подсказки '
              'диспетчеру о некорректном течении процесса.',
         impl=[
             '+1: маршрут обновляется по продвижению (recalc в track).',
             'Текстовые подсказки по маршруту — нет.',
             'Озвучка — нет.',
             '+1 частично: warning/can_make_it для диспетчера.',
         ],
         proof=[
             'live /api/route: path, legs, eta_min.',
             'UI: маршрут на карте инженера.',
         ],
         status='ЧАСТИЧНО — реально 1–2 из 4 пунктов; текст и озвучка не '
                'заявлены.',
         kind='partial'),
    dict(num='Д2', title='Нестабильная связь и GPS', max=5,
         jury='Сохранение работоспособности при обрывах связи и потере GPS: '
              'локальное хранение, досылка, пометка устаревшей позиции.',
         impl=[
             'Офлайн-очередь в мобильном клиенте; автоматическая досылка после связи.',
             'SSE, локальный Leaflet, компас, ВДНХ по умолчанию.',
             'Устаревшая позиция: последняя известная + время.',
         ],
         proof=[
             'Код готов; pytest зелёный.',
             'Живой прогон на ВДНХ — впереди.',
         ],
         status='ГОТОВО К ПРОГОНУ — код готов, проверка обрыва связи на ВДНХ.',
         kind='field'),
    dict(num='Д3', title='Оригинальность', max=2,
         jury='Наличие и уместность авторских идей, органично расширяющих '
              'базовое задание.',
         impl=[
             'FIM 007-00 в БД: 3831 запись, 12 правил, 33 главы ATA.',
             'Объяснимость кандидатов: score, ETA, workload, причина.',
             'Журнал с миллисекундами и duration_ms.',
             'Автоприбытие по геозоне стоянки.',
         ],
         proof=[
             'live: классификатор, кандидаты, CSV.',
             'pytest 75/104/0.',
         ],
         status='ЗАКРЫТ — авторские элементы встроены в основной функционал.'),
]


# ---------------------------------------------------------------------------
# Сборка
# ---------------------------------------------------------------------------

def main():
    if not os.path.isfile(TEMPLATE):
        raise SystemExit('Шаблон не найден: ' + TEMPLATE)
    presentation = Presentation(TEMPLATE)
    build_title(presentation)
    build_team(presentation)
    build_overview(presentation)
    build_c1(presentation)
    build_c2(presentation)
    build_c3(presentation)
    for spec in CRITERIA:
        add_criterion_slide(presentation, spec)
    build_final(presentation)
    # Прячем лишние слайды шаблона только после добавления новых: иначе
    # python-pptx выдаёт новым слайдам имена уже существующих частей.
    keep = {0, 2} | set(range(13, len(presentation.slides)))
    keep_only_template_slides(presentation, keep)

    try:
        presentation.save(OUTPUT)
        saved = OUTPUT
    except PermissionError:
        presentation.save(FALLBACK)
        saved = FALLBACK
    print('Сохранено:', saved)
    print('Слайдов:', len(presentation.slides))


if __name__ == '__main__':
    main()
