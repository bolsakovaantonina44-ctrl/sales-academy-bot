"""PDF delivery built from public report data; no hidden card access."""
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, PageBreak
from .domain import SKILLS
from .reporting import total_score
from .pacing import FOCUS_OBJECTIONS


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

    normal = ParagraphStyle('body', fontName='Academy', fontSize=10.5, leading=15.5,
                            textColor=colors.HexColor('#26354A'), spaceAfter=7)
    heading = ParagraphStyle('heading', parent=normal, fontName='AcademyBold', fontSize=14.5,
                             leading=19, spaceBefore=15, spaceAfter=8, keepWithNext=True)
    title = ParagraphStyle('title', parent=heading, fontSize=24, leading=30, spaceBefore=0)
    small = ParagraphStyle('small', parent=normal, fontSize=9, leading=13)
    label = ParagraphStyle('label', parent=small, fontName='AcademyBold')
    p = lambda text, style=normal: Paragraph(escape(str(text)).replace('\n', '<br/>'), style)

    stream = BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=38, bottomMargin=40,
                            title='Академия продаж — Результат тренировки', author='Академия продаж')
    story = [p('АКАДЕМИЯ ПРОДАЖ', small), p('Результат тренировки', title),
             p('Отчёт сотруднику' if audience == 'employee' else 'Отчёт руководителю', heading)]
    employee = session.get('employee', {})
    fields = session['fields']
    focus_label = FOCUS_OBJECTIONS.get(session.get('training_focus'), {}).get('label', 'Общий разговор')
    metadata = [
        ('Сотрудник', (employee.get('name') or 'ФИО не указано') + ' · ID ' + str(employee.get('id', 'не указан'))),
        ('Дата', session.get('completed_at', session.get('started_at', 'не сохранена'))),
        ('Сценарий', fields['customer'] + ' · ' + fields['product']),
        ('Цель', fields['goal']),
        ('Фокус', focus_label),
        ('Сложность', {'easy':'1 — лёгкая', 'medium':'2 — средняя', 'hard':'3 — сложная'}[fields['difficulty']]),
        ('Тренировка', str(session['id'])),
    ]
    table = Table([[p(k, label), p(v)] for k, v in metadata], colWidths=[96, 427])
    table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F1F5F9')),
        ('BOX', (0,0), (-1,-1), 0.4, colors.HexColor('#D8E1EB')),
        ('INNERGRID', (0,0), (-1,-1), 0.25, colors.HexColor('#E5EBF1')),
        ('TOPPADDING', (0,0), (-1,-1), 7), ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LEFTPADDING', (0,0), (-1,-1), 8), ('RIGHTPADDING', (0,0), (-1,-1), 8),
    ]))
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
    rows = [[p('Навык', label), p('Баллы', label)]]
    for key, skill_label, maximum in SKILLS:
        value = skills[key]['score']
        rows.append([p(skill_label), p(f'{value}/{maximum}' if value is not None else 'Недоступно', small)])
    table = Table(rows, colWidths=[418, 105], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#DFEAF3')),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F6F8FA')]),
        ('VALIGN',(0,0),(-1,-1),'TOP'),
        ('TOPPADDING',(0,0),(-1,-1),7), ('BOTTOMPADDING',(0,0),(-1,-1),7),
        ('LEFTPADDING',(0,0),(-1,-1),7), ('RIGHTPADDING',(0,0),(-1,-1),7),
    ]))
    story.append(table)

    story += [PageBreak(), p('Разбор и следующий фокус', title)]
    for title_text, field in [('Сильные стороны', 'strengths'), ('Ошибки и зоны развития', 'mistakes')]:
        story.append(p(title_text, heading))
        story += [p('• ' + item['text']) for item in data[field]] or [p('Подтверждённые выводы недоступны.' if data.get('technical_partial') else 'Дополнительных подтверждённых наблюдений нет.')]
    story.append(p('Что удалось выяснить', heading))
    story += [p('• ' + item['text']) for item in data.get('findings', [])] or [p('Подтверждённые результаты выявления недоступны.')]
    story.append(p('Рекомендации и следующий фокус', heading))
    for i, task in enumerate(data['recommendations'], 1):
        story.append(KeepTogether([
            p(f"Задание {i}. {task['exercise']}"),
            p('Пример: ' + task['example']),
            p('Проверка: ' + task['success_check'])
        ]))

    if audience == 'supervisor':
        story += [PageBreak(), p('Коротко для руководителя', title)]
        summary_rows = [
            [p('Сотрудник', label), p(employee.get('name') or 'ФИО не указано')],
            [p('Фокус', label), p(focus_label)],
            [p('Общий балл', label), p(f'{score}/100' if score is not None else 'недоступен')],
            [p('Цель', label), p(goals[data['goal']])],
            [p('Коммерческий результат', label), p(str(data['outcome']) + '/3' if data['outcome'] is not None else 'недоступен')],
            [p('Следующий шаг', label), p(data['next_step'] or 'не зафиксирован')],
        ]
        summary = Table(summary_rows, colWidths=[145, 378])
        summary.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F3F7FA')),
            ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#D6E1EA')),
            ('INNERGRID', (0,0), (-1,-1), 0.25, colors.HexColor('#E2E9EF')),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('TOPPADDING', (0,0), (-1,-1), 8), ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('LEFTPADDING', (0,0), (-1,-1), 8), ('RIGHTPADDING', (0,0), (-1,-1), 8),
        ]))
        story.append(summary)

        story.append(p('Сильные стороны', heading))
        story += [p(f'{i}. {item["text"]}') for i, item in enumerate(data['strengths'][:3], 1)] or [p('Подтверждённых сильных сторон пока недостаточно.')]
        story.append(p('Зоны развития', heading))
        story += [p(f'{i}. {item["text"]}') for i, item in enumerate(data['mistakes'][:3], 1)] or [p('Дополнительных подтверждённых зон развития нет.')]
        story.append(p('2 приоритетных задания', heading))
        for i, task in enumerate(data['recommendations'][:2], 1):
            story.append(p(f'{i}. {task["exercise"]}'))
        story.append(p('Что проверить в следующей тренировке', heading))
        story += [p('• ' + task['success_check']) for task in data['recommendations']]

        previous = session.get('comparison')
        story.append(p('Динамика', heading))
        if previous and score is not None:
            story.append(p(f"Тренировка №{previous['session_id']}: {previous['score']}/100 → {score}/100 ({score - previous['score']:+d})."))
            story.append(p('Это сравнение учебных сессий, а не вывод об устойчивом росте навыка.', small))
        else:
            story.append(p('Нет сопоставимой проверенной оценки по той же методике, сценарию, фокусу и сложности.'))

        story.append(p('Основания по 8 навыкам', title))
        for key, skill_label, _ in SKILLS:
            story.extend([p(skill_label, heading), p(skills[key]['reason'])])
            for evidence in skills[key]['evidence'][:2]:
                speaker = 'Менеджер' if evidence['speaker'] == 'manager' else 'Клиент'
                quote = evidence['quote']
                excerpt = quote[:700] + (' […]' if len(quote) > 700 else '')
                story.append(p(f"{speaker}, реплика {evidence['message_id']}: «{excerpt}»", small))

    story += [Spacer(1, 14), p('Учебная диагностика. Отсутствие сделки не обнуляет навыки. Вывод о сотруднике требует нескольких тренировок.', small)]

    def footer(canvas, document):
        canvas.setFont('Academy', 8.5)
        canvas.setFillColor(colors.HexColor('#63748A'))
        canvas.drawString(36, 22, 'Академия продаж · ' + session.get('versions', {}).get('rubric', ''))
        canvas.drawRightString(A4[0] - 36, 22, str(document.page))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    stream.seek(0)
    stream.name = f"academy_{session['id']}_{audience}.pdf"
    return stream
