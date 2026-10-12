"""Saved series reports: a short employee result and a downloadable evidence appendix."""
from collections import Counter
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from .domain import SKILLS, score_level
from .reporting import total_score


def pdf_marker(ident):
    return f'__academy_series_pdf__:{int(ident)}'


def summary(series, results):
    from .call_series import GOALS, OUTCOMES
    rows = [r for r in results if r['status'] != 'pending']
    valid = [r for r in rows if r.get('assessment_status') == 'verified'
             and (r['session'].get('report_data') or {}).get('simulation_valid')]
    checked = [r for r in rows if r.get('outcome_status') == 'verified'
               and (r in valid or r.get('assessment_status') == 'no_speech')]
    scores = [total_score(r['session'].get('report_data')) for r in valid]
    scores = [v for v in scores if v is not None]
    achieved = sum(bool(r.get('goal_achieved')) for r in checked)
    planned = series['config']['count']
    partial = series['status'] != 'completed' or len(rows) != planned
    lines = [f"CENTARO · ИТОГ СЕРИИ №{series['id']}", 'Отчёт сотруднику',
             ('Серия прервана. Это частичный результат.' if partial else 'Серия завершена.'),
             f'Сохранено звонков: {len(rows)}/{planned}.',
             'Цель: ' + ', '.join(series['config'].get('custom_goal_label', GOALS[g][0]) if g == 'custom_goal' else GOALS[g][0] for g in series['config']['goals']),
             'Качество продажи: ' + (f'{sum(scores)/len(scores):.1f}/100 · {score_level(sum(scores)/len(scores))}.' if scores else 'недостаточно проверенных оценок.'),
             f'Учебная конверсия: {achieved}/{len(checked)} проверенных звонков' + (f' ({achieved/len(checked):.0%}).' if checked else ' — недостаточно данных.'),
             f'Не проверено: {len(rows)-len(checked)}; не проведено: {planned-len(rows)}.',
             'Результаты: ' + ('; '.join(f'{OUTCOMES.get(k, "Не проверено")}: {v}' for k, v in Counter(r.get('outcome', 'unavailable') for r in rows).items()) or 'нет'),
             f"Завершено: {sum(r['status'] == 'completed' for r in rows)}; прервано: {sum(r['status'] == 'interrupted' for r in rows)}."]
    strengths, gaps = [], []
    for key, title, maximum in SKILLS:
        values = [item['score'] / maximum for r in valid for item in r['session']['report_data']['skills'] if item['id'] == key and item['score'] is not None]
        if values:
            gaps.append((sum(values)/len(values), title))
    if gaps:
        ordered = sorted(gaps)
        strengths = [title for _, title in ordered[-2:][::-1]]
        lines += ['Сильные стороны: ' + ', '.join(strengths) + '.',
                  'Следующий фокус: ' + ', '.join(title for _, title in ordered[:2]) + '.']
    lines += ['В PDF — критерии, результаты по возражениям, начало/середина/конец и конкретные реплики, подтверждающие оценки.',
              'Учебная конверсия не является прогнозом реальных продаж. Серия не доказывает выносливость за рабочий день.']
    return '\n\n'.join(lines)


def render_series_pdf(series, results):
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from .call_series import report
    for name, file in [('Academy', 'DejaVuSans.ttf'), ('AcademyBold', 'DejaVuSans-Bold.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(Path(__file__).with_name('fonts') / file)))
    normal = ParagraphStyle('series_body', fontName='Academy', fontSize=10.5, leading=15, spaceAfter=8)
    heading = ParagraphStyle('series_heading', parent=normal, fontName='AcademyBold', fontSize=15, leading=20, spaceBefore=12)
    para = lambda text, style=normal: Paragraph(escape(str(text)).replace('\n', '<br/>'), style)
    text = report(series, results)
    # The employee's development report never requires business inputs.
    text = text.split('\n\nБИЗНЕС-МОДЕЛЬ CENTARO', 1)[0]
    story = [para(summary(series, results).split('\n\n')[0], heading)]
    story += [para(p) for p in summary(series, results).split('\n\n')[1:]]
    story += [PageBreak(), para('Подробный разбор и подтверждающие реплики', heading), Spacer(1, 10)]
    story += [para(p) for p in text.split('\n\n')]
    stream = BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36,
                           title=f"CENTARO — отчёт сотруднику, серия {series['id']}")
    def footer(canvas, doc):
        canvas.setFont('Academy', 9)
        canvas.drawRightString(A4[0]-36, 20, f'CENTARO · {doc.page}')
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    stream.seek(0)
    return stream


def deliver_series_pdf(bot, store, chat_id, marker, admins=()):
    ident = int(marker.split(':', 1)[1])
    series = store.get_series(ident)
    if not series or series['status'] not in ('completed', 'aborted'):
        raise LookupError('Series report unavailable')
    if chat_id != series['user_id'] and chat_id not in set(admins):
        from .access import get_role
        if series.get('assigned_by') != chat_id or get_role(store.path, chat_id) != 'supervisor':
            raise PermissionError('Series report belongs to another user')
    bot.send_document(chat_id, render_series_pdf(series, store.series_results(ident)),
                      visible_file_name=f'CENTARO_Отчёт_сотруднику_серия_{ident}.pdf')
