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
from .reporting import total_score, supervisor_recommendation, recommended_training_cases
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

    normal_size = 12 if audience == 'supervisor' else 10.5
    normal = ParagraphStyle('body', fontName='Academy', fontSize=normal_size, leading=normal_size * 1.45,
                            textColor=colors.HexColor('#26354A'), spaceAfter=8)
    heading = ParagraphStyle('heading', parent=normal, fontName='AcademyBold', fontSize=16 if audience == 'supervisor' else 14.5,
                             leading=21, spaceBefore=15, spaceAfter=9, keepWithNext=True)
    title = ParagraphStyle('title', parent=heading, fontSize=25 if audience == 'supervisor' else 24,
                           leading=31, spaceBefore=0)
    small = ParagraphStyle('small', parent=normal, fontSize=10 if audience == 'supervisor' else 9, leading=14)
    label = ParagraphStyle('label', parent=small, fontName='AcademyBold')
    verdict_style = ParagraphStyle('verdict', parent=normal, fontName='AcademyBold', fontSize=14, leading=20, spaceAfter=8)
    p = lambda text, style=normal: Paragraph(escape(str(text)).replace('\n', '<br/>'), style)

    stream = BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=A4, rightMargin=34, leftMargin=34, topMargin=34, bottomMargin=40,
                            title='Академия продаж — Результат тренировки', author='Академия продаж')
    employee = session.get('employee', {})
    fields = session['fields']
    focus_label = FOCUS_OBJECTIONS.get(session.get('training_focus'), {}).get('label', 'Общий разговор')
    score = total_score(data)
    goals = dict(achieved='достигнута', partial='частично достигнута', not_achieved='не достигнута', unavailable='недоступно')
    skills = {x['id']: x for x in data['skills']}

    story = [p('АКАДЕМИЯ ПРОДАЖ', small), p('Результат тренировки', title),
             p('Отчёт сотруднику' if audience == 'employee' else 'Отчёт руководителю', heading)]

    metadata = [
        ('Сотрудник', (employee.get('name') or 'ФИО не указано') + ' · ID ' + str(employee.get('id', 'не указан'))),
        ('Дата', session.get('completed_at', session.get('started_at', 'не сохранена'))),
        ('Сценарий', fields['customer'] + ' · ' + fields['product']),
        ('Цель', fields['goal']),
        ('Фокус', focus_label),
        ('Сложность', {'easy':'1 — лёгкая', 'medium':'2 — средняя', 'hard':'3 — сложная'}[fields['difficulty']]),
        ('Тренировка', str(session['id'])),
    ]
    table = Table([[p(k, label), p(v)] for k, v in metadata], colWidths=[110, 413])
    table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'), ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F1F5F9')),
        ('BOX', (0,0), (-1,-1), 0.4, colors.HexColor('#D8E1EB')),
        ('INNERGRID', (0,0), (-1,-1), 0.25, colors.HexColor('#E5EBF1')),
        ('TOPPADDING', (0,0), (-1,-1), 8), ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING', (0,0), (-1,-1), 8), ('RIGHTPADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(table)

    story.append(p('Навыки: ' + (f'{score}/100' if score is not None else 'балл недоступен'), heading))
    if data.get('technical_partial'):
        story.append(p('Технический сбой оценки. Баллы не выставлены; это не 0/100.'))
    elif not data['simulation_valid']:
        story.append(p('Симуляция требует проверки. Итог нельзя использовать для аттестации.'))
    story += [p('Цель разговора: ' + goals[data['goal']]),
              p('Коммерческий результат: ' + (str(data['outcome']) + '/3' if data['outcome'] is not None else 'недоступен')),
              p('Следующий шаг: ' + (data['next_step'] or 'не зафиксирован'))]

    rows = [[p('Навык', label), p('Баллы', label)]]
    for key, skill_label, maximum in SKILLS:
        value = skills[key]['score']
        rows.append([p(skill_label), p(f'{value}/{maximum}' if value is not None else 'Недоступно', small)])
    table = Table(rows, colWidths=[418, 105], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#DFEAF3')),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F6F8FA')]),
        ('VALIGN',(0,0),(-1,-1),'TOP'), ('TOPPADDING',(0,0),(-1,-1),8),
        ('BOTTOMPADDING',(0,0),(-1,-1),8), ('LEFTPADDING',(0,0),(-1,-1),7), ('RIGHTPADDING',(0,0),(-1,-1),7),
    ]))
    story.append(table)

    if audience == 'employee':
        story += [PageBreak(), p('Разбор и следующий фокус', title)]
        for title_text, field in [('Сильные стороны', 'strengths'), ('Ошибки и зоны развития', 'mistakes')]:
            story.append(p(title_text, heading))
            story += [p('• ' + item['text']) for item in data[field][:3]] or [p('Дополнительных подтверждённых наблюдений нет.')]
        story.append(p('Что удалось выяснить', heading))
        story += [p('• ' + item['text']) for item in data.get('findings', [])[:4]] or [p('Подтверждённые результаты выявления не отмечены.')]
        story.append(p('Что отработать дальше', heading))
        for i, task in enumerate(data['recommendations'][:2], 1):
            story.append(KeepTogether([p(f"{i}. {task['exercise']}"), p('Пример: ' + task['example'])]))
        training_cases = recommended_training_cases(data, session)
        if training_cases:
            story.append(p('Как использовать тренажёр дальше', heading))
            story += [p(f'{i}. Повторить тренировку: {case}') for i, case in enumerate(training_cases, 1)]
    else:
        verdict = supervisor_recommendation(data, session)
        story += [PageBreak(), p('Коротко для руководителя', title),
                  p(verdict['decision'], verdict_style), p(verdict['level'], verdict_style),
                  p(verdict['trainability'])]
        story.append(p('На что обратить внимание', heading))
        story += [p('• ' + x) for x in verdict['focus']]

        if data.get('strengths'):
            story.append(p('Сильные стороны', heading))
            story += [p('• ' + x['text']) for x in data['strengths'][:2]]
        if data.get('mistakes'):
            story.append(p('Зоны роста', heading))
            story += [p('• ' + x['text']) for x in data['mistakes'][:2]]
        if data.get('recommendations'):
            story.append(p('Что делать с сотрудником', heading))
            story += [p(f'{i}. {task["exercise"]}') for i, task in enumerate(data['recommendations'][:2], 1)]
            story.append(p('Что проверить на повторной тренировке', heading))
            story += [p('• ' + task['success_check']) for task in data['recommendations'][:2]]
        training_cases = recommended_training_cases(data, session)
        if training_cases:
            story.append(p('Как использовать тренажёр дальше', heading))
            story.append(p('Рекомендуется назначить сотруднику следующие повторные тренировки:'))
            story += [p(f'{i}. {case}') for i, case in enumerate(training_cases, 1)]

        previous = session.get('comparison')
        story.append(p('Динамика', heading))
        if previous and score is not None:
            story.append(p(f"Тренировка №{previous['session_id']}: {previous['score']}/100 → {score}/100 ({score - previous['score']:+d})."))
            story.append(p('Это сравнение учебных сессий, а не окончательный вывод о сотруднике.', small))
        else:
            story.append(p('Пока нет сопоставимой тренировки. Обучаемость оцениваем только по повторной попытке.'))

        story.append(p('Кратко по 8 навыкам', heading))
        for key, skill_label, maximum in SKILLS:
            item = skills[key]
            value = item['score']
            short_reason = ' '.join(item['reason'].split())
            story.append(p(f"{skill_label}: {value}/{maximum if value is not None else ''} — {short_reason}" if value is not None
                           else f"{skill_label}: недоступно — {short_reason}"))

    story += [Spacer(1, 14), p('Учебная диагностика. Решение о найме и обучаемости подтверждается несколькими сопоставимыми тренировками.', small)]

    def footer(canvas, document):
        canvas.setFont('Academy', 8.5)
        canvas.setFillColor(colors.HexColor('#63748A'))
        canvas.drawString(34, 22, 'Академия продаж · ' + session.get('versions', {}).get('rubric', ''))
        canvas.drawRightString(A4[0] - 34, 22, str(document.page))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    stream.seek(0)
    stream.name = f"academy_{session['id']}_{audience}.pdf"
    return stream
