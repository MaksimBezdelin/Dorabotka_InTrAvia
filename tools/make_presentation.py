# -*- coding: utf-8 -*-
"""Презентация InTrAvia для гостей финала ИТ-2026 («Авиаперевозки»).

11 слайдов: польза и функции крупным планом, критерии — мелким футером.
Оформление — официальный шаблон 16:9 (цвета #305BA7/#EC674C, Arial/Arial Black).
Без кода, эндпоинтов, pytest-цифр и названий модулей в основном тексте.

Шаблон: C:\\Users\\Вячеслав\\Downloads\\Shablon_prezentatsii_final.pptx
Результат: C:\\Users\\Вячеслав\\Desktop\\InTrAvia_ИТ2026_финал_guests.pptx
"""
import copy
import os

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

TEMPLATE = r'C:\Users\Вячеслав\Downloads\Shablon_prezentatsii_final.pptx'
OUTPUT = r'C:\Users\Вячеслав\Desktop\InTrAvia_ИТ2026_финал.pptx'
GUESTS = r'C:\Users\Вячеслав\Desktop\InTrAvia_ИТ2026_финал_guests.pptx'

ACCENT = RGBColor(0xEC, 0x67, 0x4C)
BLUE = RGBColor(0x30, 0x5B, 0xA7)
DARK = RGBColor(0x2E, 0x38, 0x45)
GREY = RGBColor(0x5B, 0x66, 0x72)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
BLUE_LIGHT = RGBColor(0xEE, 0xF4, 0xFC)
GREY_LIGHT = RGBColor(0xF5, 0xF6, 0xF8)
CORAL_LIGHT = RGBColor(0xFD, 0xF0, 0xEC)
GREEN_LIGHT = RGBColor(0xE8, 0xF5, 0xE9)
GREEN = RGBColor(0x2E, 0x7D, 0x32)
AMBER_LIGHT = RGBColor(0xFF, 0xF4, 0xE0)
AMBER = RGBColor(0xB2, 0x6A, 0x00)
BORDER_BLUE = RGBColor(0xBF, 0xD0, 0xE8)
BORDER_GREY = RGBColor(0xD5, 0xDA, 0xE1)

TOTAL = 11


def _template_rpr(text_frame):
    for paragraph in text_frame.paragraphs:
        for run in paragraph.runs:
            if run.text.strip():
                return copy.deepcopy(run._r.get_or_add_rPr())
    return None


def set_lines(shape, lines, size=None, color=None):
    if shape is None or not shape.has_text_frame:
        return
    tf = shape.text_frame
    rpr = _template_rpr(tf)
    ppr = copy.deepcopy(tf.paragraphs[0]._p.get_or_add_pPr()) if tf.paragraphs else None
    for paragraph in list(tf.paragraphs)[1:]:
        paragraph._p.getparent().remove(paragraph._p)
    first = tf.paragraphs[0]
    for run in list(first.runs):
        run._r.getparent().remove(run._r)
    for index, raw in enumerate(lines):
        paragraph = first if index == 0 else tf.add_paragraph()
        if ppr is not None and index > 0:
            paragraph._p.insert(0, copy.deepcopy(ppr))
        run = paragraph.add_run()
        run.text = raw
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


def _fill_tf(tf, lines, size=16, bold=False, color=DARK,
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, space=4):
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = Inches(0.08)
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


def add_box(slide, x, y, w, h, lines, fill, border, size=16, bold=False,
            color=DARK, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
            radius=0.06, space=4, line_w=1.0):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.adjustments[0] = radius
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.color.rgb = border
    shape.line.width = Pt(line_w)
    _fill_tf(shape.text_frame, lines, size, bold, color, align, anchor, space)
    return shape


def add_label(slide, x, y, w, h, lines, size=16, bold=False, color=DARK,
              align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, space=4):
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    _fill_tf(shape.text_frame, lines, size, bold, color, align, anchor, space)
    return shape


def add_rule(slide, x, y, w, h, color):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def add_arrow(slide, x, y, w, h=0.32, color=ACCENT):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RIGHT_ARROW, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def add_down_arrow(slide, x, y, w=0.26, h=0.18, color=ACCENT):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.DOWN_ARROW, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def add_header(slide, title, subtitle, footer, page):
    add_label(slide, 0.45, 0.10, 7.35, 0.28,
              ['InTrAvia · ИТ-2026'], size=10, bold=True, color=GREY)
    add_label(slide, 7.95, 0.10, 4.90, 0.28,
              ['Авиаперевозки · финал'], size=10, bold=True, color=ACCENT,
              align=PP_ALIGN.RIGHT)
    add_label(slide, 0.50, 0.40, 12.35, 0.96,
              [(title, 28, True, BLUE)], space=0)
    add_label(slide, 0.52, 1.38, 12.2, 0.42,
              [(subtitle, 16, False, GREY)], space=0)
    add_rule(slide, 0.50, 1.90, 12.33, 0.035, ACCENT)
    add_label(slide, 0.50, 7.02, 8.5, 0.28,
              [(footer, 9.5, False, GREY)], size=9.5)
    add_label(slide, 10.6, 7.02, 2.2, 0.28,
              ['%d / %d' % (page, TOTAL)], size=9.5, color=GREY,
              align=PP_ALIGN.RIGHT)


def new_slide(presentation, title, subtitle, footer, page):
    slide = presentation.slides.add_slide(presentation.slide_layouts[12])
    add_header(slide, title, subtitle, footer, page)
    return slide


def add_card(slide, x, y, w, h, big, header, body, color=BLUE,
             fill=WHITE, border=BORDER_BLUE, big_size=34):
    add_box(slide, x, y, w, h, [], fill, border, radius=0.06)
    cursor = y + 0.18
    if big:
        add_label(slide, x + 0.18, y + 0.12, w - 0.36, 0.62,
                  [(big, big_size, True, color)], space=0)
        cursor = y + 0.78
    add_label(slide, x + 0.18, cursor, w - 0.36, 0.40,
              [(header, 17, True, DARK)], space=0)
    add_label(slide, x + 0.18, cursor + 0.40, w - 0.36,
              max(0.35, h - (cursor - y) - 0.50),
              [(body, 14.5, False, GREY)], space=0)


def keep_only(presentation, keep):
    sld_ids = presentation.slides._sldIdLst
    for index, sld in enumerate(list(sld_ids)):
        if index in keep:
            continue
        sld_ids.remove(sld)


# ---------------------------------------------------------------------------
# 1. Титул
# ---------------------------------------------------------------------------

def slide_title(presentation):
    s = presentation.slides[0]
    for shape in list(s.shapes):
        if shape.shape_type == 13:
            top = Emu(shape.top).inches
            width = Emu(shape.width).inches
            if 2.6 < top < 2.9 and 6.0 < width < 6.5:
                shape._element.getparent().remove(shape._element)
    box = find_shape(s, 'TextBox 12')
    box.left = Inches(0.7)
    box.top = Inches(1.05)
    box.width = Inches(11.9)
    box.height = Inches(5.2)
    set_lines(box, [
        'ИТ-2026 · Финал · Направление «Авиаперевозки»',
        'InTrAvia',
        'Инженеры приходят за 15 минут,\nа не «когда получится»',
        'Андронов Вячеслав · Городнов Владислав · Безделин Максим',
        'Кванториум · Тольятти  →  финал: Москва · ВДНХ',
    ])
    for paragraph, size in zip(box.text_frame.paragraphs,
                               [14, 56, 22, 14, 14]):
        paragraph.alignment = PP_ALIGN.CENTER
        for run in paragraph.runs:
            run.font.size = Pt(size)


# ---------------------------------------------------------------------------
# 2. Проблема → решение + для кого
# ---------------------------------------------------------------------------

def slide_problem(presentation):
    slide = new_slide(
        presentation,
        'Аэропорт теряет минуты, а рейсы — расписание',
        'Ручная координация → единая карта, подбор кандидатов и норматив 15 минут',
        'Критерий 2', 2)
    left = ['Диспетчер держит всё в голове',
            'Звонки и ручное назначение инженера',
            'Задержки рейсов и простой техники']
    right = ['Одна карта: инженеры, машины, борты',
             'Система предлагает лучшего кандидата, назначает диспетчер',
             'Маршрут, ETA и норматив 15 минут']
    add_box(slide, 0.55, 2.05, 6.0, 2.78,
            [('БЫЛО', 19, True, ACCENT)] +
            [('• ' + x, 16, False, DARK) for x in left],
            CORAL_LIGHT, ACCENT, radius=0.06, space=10)
    add_box(slide, 6.78, 2.05, 6.0, 2.78,
            [('СТАЛО', 19, True, BLUE)] +
            [('• ' + x, 16, False, DARK) for x in right],
            BLUE_LIGHT, BLUE, radius=0.06, space=10)
    add_label(slide, 0.55, 4.98, 12.2, 0.34,
              [('ДЛЯ КОГО', 14, True, BLUE)], space=0)
    chips = [
        ('Диспетчер', 'видит всю картину'),
        ('Инженер', 'знает маршрут и ETA'),
        ('Авиакомпания', 'меньше простоев'),
        ('Экипаж и пассажиры', 'меньше задержек'),
    ]
    for index, (role, benefit) in enumerate(chips):
        x = 0.55 + index * 3.12
        add_box(slide, x, 5.38, 2.95, 0.98,
                [(role, 14.5, True, DARK), (benefit, 14, False, GREY)],
                WHITE, BORDER_BLUE, radius=0.10, space=1)


# ---------------------------------------------------------------------------
# 3. Цепочка заявки
# ---------------------------------------------------------------------------

def slide_chain(presentation):
    slide = new_slide(
        presentation,
        'Заявка идёт по цепочке, а не напрямую инженеру',
        'Каждое звено отвечает за своё решение',
        'Критерии 13–15', 3)
    steps = [
        ('Пилот', 'сообщает о дефекте'),
        ('MCC / ЦПУ', 'проверяет и передаёт'),
        ('Диспетчер', 'назначает инженера'),
        ('Инженер', 'принимает и выезжает'),
    ]
    for index, (role, text) in enumerate(steps):
        x = 0.55 + index * 3.15
        add_box(slide, x, 2.10, 2.90, 1.45,
                [(role, 18, True, BLUE), (text, 14.5, False, DARK)],
                WHITE, BLUE, radius=0.08, space=6)
        if index < 3:
            add_arrow(slide, x + 2.93, 2.66, 0.20, 0.30)
    add_label(slide, 0.70, 3.90, 12.0, 1.45, [
        '• Назначает только диспетчер — пилот не выбирает инженера.',
        '• Статусы: назначен → в пути → в работе → отчёт → выполнено.',
        '• Снятие после «В работе» — только с подтверждением.',
    ], size=16, space=10)
    add_box(slide, 0.55, 5.60, 12.23, 1.10,
            [('Администратор', 16, True, BLUE),
             ('учётные записи, инженеры, машины и стоянки — всё в одном месте',
              14.5, False, DARK)],
            BLUE_LIGHT, BORDER_BLUE, radius=0.08, space=4)


# ---------------------------------------------------------------------------
# 4. Карта и маршруты
# ---------------------------------------------------------------------------

def slide_map(presentation):
    slide = new_slide(
        presentation,
        'Карта ведёт по аэродрому, а не по прямой',
        'Пешком или на спецтранспорте — что быстрее',
        'Критерии 4–9', 4)
    cards = [
        ('3', 'графа дорог', 'пешеходный, транспортный, авиационный'),
        ('10', 'стоянок ВДНХ', '6 объектов · 325 дорожных сегментов'),
        ('+30 с', 'склад за инструментом', 'посадка и высадка +10 с'),
        ('GPS', 'маршрут обновляется', 'по мере движения инженера'),
    ]
    for index, (big, header, body) in enumerate(cards):
        x = 0.55 + (index % 2) * 6.23
        y = 2.05 + (index // 2) * 1.98
        add_card(slide, x, y, 6.0, 1.82, big, header, body)
    add_box(slide, 0.55, 6.02, 12.23, 0.68,
            [('Точки вне дорог — на ближайшее ребро; маршрут не режет через '
              'рулёжку и ВПП.', 14.5, False, DARK)],
            WHITE, BORDER_GREY, radius=0.08)


# ---------------------------------------------------------------------------
# 5. Инженер в поле
# ---------------------------------------------------------------------------

def slide_field(presentation):
    slide = new_slide(
        presentation,
        'Инженер в поле: телефон, карта и автоприбытие',
        'Интерфейс работает на телефоне; прибытие фиксируется само',
        'Критерии 10–12, Д2', 5)
    cards = [
        ('Принять / отклонить', 'заявка и статусы на телефоне'),
        ('Маршрут и ETA', 'видно, сколько идти и когда придёшь'),
        ('Автоприбытие', 'система сама отмечает зону стоянки'),
        ('Офлайн-очередь', 'при потере связи данные досылаются'),
    ]
    for index, (header, body) in enumerate(cards):
        x = 0.55 + (index % 2) * 6.23
        y = 2.05 + (index // 2) * 1.98
        add_card(slide, x, y, 6.0, 1.82, None, header, body)
    add_box(slide, 0.55, 6.02, 12.23, 0.68,
            [('Готово к полевому прогону на ВДНХ: интерфейс есть, живой '
              'проход впереди.', 14.5, True, AMBER)],
            AMBER_LIGHT, AMBER, radius=0.08)


# ---------------------------------------------------------------------------
# 6. Кого назначить
# ---------------------------------------------------------------------------

def slide_assign(presentation):
    slide = new_slide(
        presentation,
        'Система выбирает не «ближайшего», а лучшего',
        'ETA, загрузка, допуск и причина отказа — в одной таблице',
        'Критерии 16, 17, 19', 6)
    cards = [
        ('9 мин', 'ETA', 'прогноз прибытия'),
        ('AOG', 'Срочные заявки', 'несколько одновременно'),
        (None, 'Допуск', 'по типу неисправности'),
        (None, 'Причина отказа', 'занят, нет допуска, не успевает'),
    ]
    for index, (big, header, body) in enumerate(cards):
        x = 0.55 + (index % 2) * 6.23
        y = 2.05 + (index // 2) * 1.98
        add_card(slide, x, y, 6.0, 1.82, big, header, body,
                 color=ACCENT if index == 0 else BLUE)
    add_box(slide, 0.55, 6.02, 12.23, 0.68,
            [('Ожидание запчастей освобождает инженера; назначение остаётся '
              'за диспетчером.', 14.5, False, DARK)],
            WHITE, BORDER_GREY, radius=0.08)


# ---------------------------------------------------------------------------
# 7. Неисправности и голос
# ---------------------------------------------------------------------------

def slide_faults(presentation):
    slide = new_slide(
        presentation,
        'Пилот говорит — система понимает',
        'Голосовая заявка и открытый словарь неисправностей',
        'Критерий 18, Д3', 7)
    add_box(slide, 0.55, 2.05, 6.0, 3.85,
            [('ГОЛОСОВАЯ ЗАЯВКА', 18, True, ACCENT),
             ('1. Пилот нажимает кнопку', 16, False, DARK),
             ('2. Говорит, что случилось', 16, False, DARK),
             ('3. Диспетчер получает готовую заявку', 16, False, DARK),
             ('Авторская функция: Whisper распознаёт речь.', 14, False, GREY)],
            CORAL_LIGHT, ACCENT, radius=0.06, space=12)
    add_box(slide, 6.78, 2.05, 6.0, 3.85,
            [('СЛОВАРЬ НЕИСПРАВНОСТЕЙ', 18, True, BLUE),
             ('3 831', 40, True, ACCENT),
             ('неисправность в базе FIM', 15, False, DARK),
             ('33 главы ATA · MEL / AOG', 16, False, DARK),
             ('Незнакомый дефект не теряется: его можно ввести текстом.',
              14.5, False, GREY)],
            BLUE_LIGHT, BLUE, radius=0.06, space=5)
    add_box(slide, 0.55, 6.02, 12.23, 0.68,
            [('Голос — авторская функция; словарь пополняется без правки кода.',
              14, False, GREY)],
            WHITE, BORDER_GREY, radius=0.08)


# ---------------------------------------------------------------------------
# 8. Норматив 15 минут + журнал
# ---------------------------------------------------------------------------

def slide_norm_journal(presentation):
    slide = new_slide(
        presentation,
        '15 минут под контролем',
        'Прогноз, предупреждение, нарушение и журнал',
        'Критерии 20–22', 8)
    add_box(slide, 0.55, 2.05, 4.15, 4.50, [], WHITE, ACCENT, radius=0.06)
    add_label(slide, 0.70, 2.20, 3.85, 1.50,
              [('15', 86, True, ACCENT)], space=0)
    add_label(slide, 0.75, 3.76, 3.80, 0.52,
              [('минут', 28, True, BLUE)], space=0)
    add_label(slide, 0.75, 4.32, 3.75, 0.55,
              [('норматив прибытия', 16, False, GREY)], space=0)
    cards = [
        ('Прогноз ETA', 'обновляется по маршруту'),
        ('За 5 минут', 'предупреждение диспетчеру'),
        ('Нарушение', 'фиксируется в журнале'),
        ('Журнал', 'каждое событие до мс · Excel/CSV'),
    ]
    for index, (header, body) in enumerate(cards):
        x = 4.95 + (index % 2) * 3.95
        y = 2.05 + (index // 2) * 2.28
        add_card(slide, x, y, 3.80, 2.10, None, header, body, big_size=24)


# ---------------------------------------------------------------------------
# 9. Как устроено и как делали
# ---------------------------------------------------------------------------

def slide_arch_process(presentation):
    slide = new_slide(
        presentation,
        'Как устроено и как делали',
        'Одна система и проверенный процесс',
        'Критерии 1, 3', 9)
    blocks = [
        ('КЛИЕНТЫ', 'браузеры ролей, телефон, имитатор'),
        ('СЕРВЕР', 'заявки, маршруты, подбор, журнал'),
        ('ДАННЫЕ', 'база, карты, словарь'),
    ]
    for index, (header, body) in enumerate(blocks):
        y = 2.05 + index * 1.23
        add_box(slide, 0.55, y, 5.80, 1.00,
                [(header, 16, True, BLUE), (body, 14.5, False, DARK)],
                WHITE, BLUE, radius=0.08, space=3)
        if index < 2:
            add_down_arrow(slide, 3.32, y + 1.02, 0.26, 0.18)
    add_label(slide, 0.55, 5.72, 5.80, 0.40,
              [('Python · Flask · SQLite · Leaflet · NetworkX', 14,
                True, GREY)], space=0)
    right = [
        ('Git', 'каждое изменение отдельно'),
        ('Автотесты', 'после каждого изменения'),
        ('Аудит и авторство', 'независимая проверка; люди + ИИ-помощник'),
    ]
    for index, (header, body) in enumerate(right):
        y = 2.05 + index * 1.40
        add_box(slide, 6.65, y, 6.13, 1.25,
                [(header, 17, True, DARK), (body, 14.5, False, GREY)],
                GREY_LIGHT if index != 1 else BLUE_LIGHT,
                BORDER_GREY if index != 1 else BORDER_BLUE,
                radius=0.08, space=4)
    add_label(slide, 6.65, 6.30, 6.13, 0.36,
              [('Без «написали и надеемся».', 14, False, GREY)], space=0)


# ---------------------------------------------------------------------------
# 10. Почему мы и что дальше
# ---------------------------------------------------------------------------

def slide_compare_econ(presentation):
    slide = new_slide(
        presentation,
        'Почему мы и что дальше',
        'Сравнение, экономика и перспективы',
        'Критерии 2, Д3', 10)
    rows = [
        ('Решение', 'Цена', 'Карта', 'Диспетчеризация'),
        ('Amadeus', 'дорого, >$1 млн', 'нет', 'вручную'),
        ('SITA', 'дорого', 'нет', 'вручную'),
        ('1С:ТОИР', 'доступнее', 'не под аэропорт', 'вручную'),
        ('TRIM', 'доступнее', 'требует доработки', 'вручную'),
        ('InTrAvia', 'доступно', 'карта и маршруты', 'автоматическая'),
    ]
    table_shape = slide.shapes.add_table(
        6, 4, Inches(0.55), Inches(2.05), Inches(7.45), Inches(3.25))
    table = table_shape.table
    widths = [1.95, 1.55, 2.05, 1.90]
    for index, width in enumerate(widths):
        table.columns[index].width = Inches(width)
    for r, row in enumerate(rows):
        table.rows[r].height = Inches(0.45 if r == 0 else 0.56)
        for c, value in enumerate(row):
            cell = table.cell(r, c)
            cell.text = value
            cell.margin_left = Inches(0.06)
            cell.margin_right = Inches(0.03)
            cell.margin_top = Inches(0.01)
            cell.margin_bottom = Inches(0.01)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.fill.solid()
            if r == 0:
                cell.fill.fore_color.rgb = BLUE
            elif r == 5:
                cell.fill.fore_color.rgb = CORAL_LIGHT
            else:
                cell.fill.fore_color.rgb = WHITE if r % 2 else BLUE_LIGHT
            for paragraph in cell.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(14.5 if r == 0 else 14)
                    run.font.bold = (r == 0 or r == 5)
                    run.font.name = 'Arial'
                    run.font.color.rgb = WHITE if r == 0 else DARK

    numbers = [
        ('10,79 млн ₽', 'оборудование'),
        ('10 120 ₽/мес', 'электроэнергия'),
        ('~3 года', 'окупаемость'),
    ]
    for index, (big, label) in enumerate(numbers):
        y = 2.05 + index * 1.02
        add_box(slide, 8.25, y, 4.50, 0.92, [], WHITE, BORDER_BLUE,
                radius=0.08)
        add_label(slide, 8.40, y + 0.06, 4.25, 0.42,
                  [(big, 20, True, ACCENT)], space=0)
        add_label(slide, 8.40, y + 0.48, 4.25, 0.30,
                  [(label, 14, False, GREY)], space=0)
    add_label(slide, 8.25, 5.10, 4.55, 0.30,
              [('предварительная оценка', 14, True, AMBER)], space=0)

    add_box(slide, 0.55, 5.42, 12.23, 1.12,
            [('ПЕРСПЕКТИВЫ', 14, True, BLUE),
             ('BLE-маячки · мобильное приложение · СКУД · прогноз '
              'неисправностей', 15, False, DARK),
             ('Оценка и перспективы, а не текущий функционал.', 14, True,
              AMBER)],
            BLUE_LIGHT, BORDER_BLUE, radius=0.06, space=2)


# ---------------------------------------------------------------------------
# 11. Оригинальность + демо + контакты
# ---------------------------------------------------------------------------

def slide_final(presentation):
    slide = new_slide(
        presentation,
        'Что делает нас особенными',
        'Голос, объяснимость, журнал и автоприбытие',
        'Критерии Д1, Д3', 11)
    cards = [
        ('Голос', 'заявка без ручного ввода'),
        ('Объяснимость', 'видно, почему выбран инженер'),
        ('Журнал', 'каждое событие до миллисекунды'),
        ('Автоприбытие', 'по геозоне, без кнопки'),
    ]
    for index, (header, body) in enumerate(cards):
        x = 0.55 + index * 3.12
        add_card(slide, x, 2.05, 2.95, 1.95, None, header, body,
                 color=ACCENT if index == 0 else BLUE)
    add_box(slide, 0.55, 4.18, 12.23, 0.95,
            [('Демо: заявка → назначение → маршрут → отчёт', 17, True, BLUE),
             ('Подсказки по маршруту и озвучка — частично (Д1).', 14, False,
              GREY)],
            WHITE, BORDER_BLUE, radius=0.08, space=2)
    add_box(slide, 0.55, 5.30, 12.23, 1.42,
            [('Команда: Андронов В. · Городнов В. · Безделин М.', 14.5, True,
              DARK),
             ('Кванториум г. Тольятти · Самарская область', 14, False, DARK),
             ('Наставник: Кузьмин Владимир Ильич', 14, False, DARK),
             ('Финал: Москва · ВДНХ', 14, False, DARK)],
            BLUE_LIGHT, BORDER_BLUE, radius=0.08, space=3)


# ---------------------------------------------------------------------------
# Сборка
# ---------------------------------------------------------------------------

def main():
    if not os.path.isfile(TEMPLATE):
        raise SystemExit('Шаблон не найден: ' + TEMPLATE)
    presentation = Presentation(TEMPLATE)
    slide_title(presentation)
    slide_problem(presentation)
    slide_chain(presentation)
    slide_map(presentation)
    slide_field(presentation)
    slide_assign(presentation)
    slide_faults(presentation)
    slide_norm_journal(presentation)
    slide_arch_process(presentation)
    slide_compare_econ(presentation)
    slide_final(presentation)

    # Добавляем новые слайды до скрытия шаблонных: иначе python-pptx
    # переиспользует имена slideN.xml и в ZIP появляются дубли.
    keep = {0} | set(range(13, len(presentation.slides)))
    keep_only(presentation, keep)

    try:
        presentation.save(OUTPUT)
        saved = OUTPUT
    except PermissionError:
        presentation.save(GUESTS)
        saved = GUESTS
    print('Сохранено:', saved)
    print('Слайдов:', len(presentation.slides))


if __name__ == '__main__':
    main()
