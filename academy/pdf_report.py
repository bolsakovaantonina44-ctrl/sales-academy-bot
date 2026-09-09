"""PDF delivery built from public report data; no hidden card access."""
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, PageBreak
from .domain import SKILLS
from .reporting import total_score, manager_summary


def render_pdf(session, audience='employee'):
    if audience not in ('employee', 'supervisor'):
        raise ValueError('Invalid report audience')
    data = session.get('report_data')
    if not data:
        raise ValueError('No structured report')
    font_dir = Path(__file__).with_name('fonts')
    for name, file in [('Academy', 'DejaVuSans.ttf'), ('AcademyBold', 'DejaVuSans-Bold.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_dir / file)))
    normal = ParagraphStyle('body', fontName='Academy', fontSize=9, leading=14, textColor=colors.HexColor('#26354A'), spaceAfter=6)
    heading = ParagraphStyle('heading', parent=normal, fontName='AcademyBold', fontSize=13, leading=18, spaceBefore=14, spaceAfter=8, keepWithNext=True)
    title = ParagraphStyle('title', parent=heading, fontSize=23, leading=29, spaceBefore=0)
    small = ParagraphStyle('small', parent=normal, fontSize=8, leading=12)
    p = lambda text, style=normal: Paragraph(escape(str(text)).replace('\n', '<br/>'), style)
    stream = BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=A4, rightMargin=42, leftMargin=42, topMargin=42, bottomMargin=40,
                            title='Академия продаж — Результат тренировки', author='Академия продаж')
    story = [p('АКАДЕМИЯ ПРОДАЖ', small), p('Результат тренировки', title),
             p('Отчёт сотруднику' if audience == 'employee' else 'Отчёт руководителю', heading)]
    employee = session.get('employee', {})
    fields = session['fields']
    metadata = [
        ('Сотрудник', (employee.get('name') or 'ФИО не указано') + ' · ID ' + str(employee.get('id', 'не указан'))),
        ('Дата', session.get('completed_at', session.get('started_at', 'не сохранена'))),
        ('Сценарий', fields['customer'] + ' · ' + fields['product']),
        ('Цель', fields['goal']),
        ('Уровень', dict(easy='Лёгкий', medium='Средний', hard='Сложный')[fields['difficulty']]),
        ('Тренировка', str(session['id'])),
    ]
    table = Table([[p(k, small), p(v)] for k, v in metadata], colWidths=[90, 421])
    table.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'TOP'), ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F1F5F9')),
                               ('TOPPADDING', (0,0), (-1,-1), 5), ('BOTTOMPADDING', (0,0), (-1,-1), 4)]))
    story.append(table)
    score = total_score(data)
    story.append(p('Навыки: ' + (f'{score}/100' if score is not None else 'балл недоступен'), heading))
    if data.get('technical_partial'):
        story.append(p('Технический сбой оценки. Баллы не выставлены; это не 0/100. Все разделы сохранены для проверки.'))
    elif not data['simulation_valid']:
        story.append(p('Симуляция требует проверки. Итог нельзя использовать для аттестации.'))
    goals = dict(achieved='достигнута', partial='частично достигнута', not_achieved='не достигнута', unavailable='недоступно')
    story += [p('Цель разговора: ' + goals[data['goal']]),
              p('Коммерческий результат (отдельно): ' + (str(data['outcome']) + '/3' if data['outcome'] is not None else 'недоступен')),
              p('Следующий шаг: ' + (data['next_step'] or 'не зафиксирован'))]
    skills = {x['id']: x for x in data['skills']}
    rows = [[p('Навык', small), p('Баллы', small)]]
    for key, label, maximum in SKILLS:
        value = skills[key]['score']
        rows.append([p(label), p(f'{value}/{maximum}' if value is not None else 'Недоступно', small)])
    table = Table(rows, colWidths=[411, 100], repeatRows=1)
    table.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), colors.HexColor('#DFEAF3')),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F6F8FA')]),
        ('VALIGN',(0,0),(-1,-1),'TOP'), ('TOPPADDING',(0,0),(-1,-1),6), ('BOTTOMPADDING',(0,0),(-1,-1),5)]))
    story.append(table)
    story += [PageBreak(), p('Разбор и следующий фокус', title)]
    for title_text, field in [('Сильные стороны', 'strengths'), ('Ошибки и зоны развития', 'mistakes')]:
        story.append(p(title_text, heading))
        story += [p('• ' + item['text']) for item in data[field]] or [p('Подтверждённые выводы недоступны.' if data.get('technical_partial') else 'Дополнительных подтверждённых наблюдений нет.')]
    story.append(p('Что удалось выяснить', heading))
    story += [p('• ' + item['text']) for item in data.get('findings', [])] or [p('Подтверждённые результаты выявления недоступны.')]
    story.append(p('Рекомендации и следующий фокус', heading))
    for i, task in enumerate(data['recommendations'], 1):
        story.append(KeepTogether([p(f"Задание {i}. {task['exercise']}"), p('Пример: ' + task['example']), p('Проверка: ' + task['success_check'])]))
    if audience == 'supervisor':
        story.append(p('Результат для руководителя', heading))
        # Same management block; no card or hidden scenario is consulted.
        story += [p(line) for line in manager_summary(data, session).splitlines()[1:]]
        story.append(p('Основания по 8 навыкам', heading))
        for key, label, _ in SKILLS:
            story.extend([p(label, heading), p(skills[key]['reason'])])
            for evidence in skills[key]['evidence'][:2]:
                speaker = 'Менеджер' if evidence['speaker'] == 'manager' else 'Клиент'
                quote = evidence['quote']
                # Bounded excerpts remain actual quotes and state when shortened.
                excerpt = quote[:700] + (' […]' if len(quote) > 700 else '')
                story.append(p(f"{speaker}, реплика {evidence['message_id']}: «{excerpt}»", small))
    story += [Spacer(1, 12), p('Учебная диагностика. Отсутствие сделки не обнуляет навыки. Вывод о сотруднике требует нескольких тренировок.', small)]
    def footer(canvas, document):
        canvas.setFont('Academy', 8)
        canvas.setFillColor(colors.HexColor('#63748A'))
        canvas.drawString(42, 22, 'Академия продаж · ' + session.get('versions', {}).get('rubric', ''))
        canvas.drawRightString(A4[0] - 42, 22, str(document.page))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    stream.seek(0)
    stream.name = f"academy_{session['id']}_{audience}.pdf"
    return stream
