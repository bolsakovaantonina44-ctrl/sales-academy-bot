"""Compact Telegram report rendering. Evidence stays in structured data/PDF, not chat walls."""
from .domain import SKILLS
from .reporting import fallback_data


def _short(text, limit=220):
    value = ' '.join((text or '').split())
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def render_report(data, session, include_hidden=False):
    items = {v['id']: v for v in data['skills']}
    earned = sum(x['score'] or 0 for x in items.values())
    maximum = sum(m for k, _, m in SKILLS if items[k]['score'] is not None)
    lines = ['РЕЗУЛЬТАТ ТРЕНИРОВКИ']
    if data.get('technical_partial'):
        lines += ['Автоматическую оценку не удалось подтвердить. Баллы не выставлены — это не 0/100.']
    elif not data['simulation_valid']:
        lines += ['Симуляция требует проверки. Итоговый балл не используется для аттестации.']
    elif maximum == 100:
        lines += [f'Навыки: {earned}/100']
    else:
        lines += [f'По наблюдаемым навыкам: {earned}/{maximum}.']

    goal = dict(achieved='достигнута', partial='частично достигнута', not_achieved='не достигнута', unavailable='недоступна')
    step_status = dict(absent='не предложен', proposed='предложен, но не согласован', agreed='согласован', unavailable='недоступен')
    lines += [f"Цель: {goal[data['goal']]}",
              ('Коммерческий результат: недоступен' if data['outcome'] is None else f"Коммерческий результат: {data['outcome']}/3"),
              'Следующий шаг: ' + step_status[data['next_step_status']] + ((' — ' + _short(data['next_step'], 180)) if data.get('next_step') else ''),
              '', 'ОЦЕНКА ПО НАВЫКАМ']

    for k, title, m in SKILLS:
        item = items[k]
        score = 'н/д' if item['score'] is None else f"{item['score']}/{m}"
        lines.append(f'• {title}: {score} — {_short(item["reason"], 170)}')

    if data.get('strengths'):
        lines += ['', 'ЧТО ПОЛУЧИЛОСЬ'] + ['• ' + _short(x['text'], 180) for x in data['strengths'][:2]]
    if data.get('mistakes'):
        lines += ['', 'ГЛАВНЫЕ ЗОНЫ РОСТА'] + ['• ' + _short(x['text'], 180) for x in data['mistakes'][:2]]
    if data.get('recommendations'):
        lines += ['', 'ЧТО ОТРАБОТАТЬ'] + ['• ' + _short(x['exercise'], 190) for x in data['recommendations'][:2]]
    if data.get('findings'):
        lines += ['', 'ЧТО УДАЛОСЬ ВЫЯСНИТЬ'] + ['• ' + _short(x['text'], 170) for x in data['findings'][:3]]

    lines += ['', 'Тренировка: ' + str(session['id'])]
    return '\n'.join(lines)


def partial_report(session):
    return render_report(fallback_data(session), session)
