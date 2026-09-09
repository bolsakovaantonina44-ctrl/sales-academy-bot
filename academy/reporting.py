"""Shared report data and management summary; unavailable is never a zero score."""
from .domain import SKILLS

UNAVAILABLE = 'недоступно: требуется проверка сохранённого диалога'


def fallback_data(session):
    skills = []
    events = session.get('turn_events', [])
    actions = {'contact': {'other'}, 'questions': {'question'}, 'needs': {'question'},
               'listening': {'reflection', 'ignored_answer'}, 'control': {'next_step', 'monologue'},
               'arguments': {'relevant_argument'}, 'objections': {'objection_work', 'ignored_answer'},
               'next_step': {'next_step'}}
    manager_turns = [(i + 1, m) for i, m in enumerate(session['history']) if m['role'] == 'user']
    for key, _, _ in SKILLS:
        refs = []
        for index, (mid, message) in enumerate(manager_turns):
            if index < len(events) and events[index].get('action') in actions[key]:
                refs.append(dict(message_id=mid, speaker='manager', quote=message['content']))
        skills.append(dict(id=key, score=None, reason='Баллы не выставлены. Реплики ниже — материал для ручной проверки, не подтверждённый вывод.', evidence=refs[:2]))
    return dict(simulation_valid=False, simulation_issues=['Автоматическая проверка недоступна; корректность симуляции не установлена.'],
                skills=skills, goal='unavailable', outcome=None, next_step_status='unavailable', next_step=UNAVAILABLE,
                strengths=[], mistakes=[], findings=[], revealed=[], missed=[], technical_partial=True,
                recommendations=[dict(skill_id=key, observation='Технический сбой: недостаток сотрудника не установлен.',
                    evidence=[], business_risk='Не установлен по непроверенному отчёту.', exercise=exercise,
                    example=example, success_check=check) for key, exercise, example, check in [
                    ('needs', 'Выберите из сохранённого диалога вопрос и ответ. Объясните, какую задачу выявили и как использовали ответ.',
                     'Правильно понимаю, что основная задача — …?',
                     'Сотрудник указывает конкретный вопрос и ответ, отделяет факт от предположения.'),
                    ('next_step', 'Найдите в конце диалога предложение продолжения и ответ клиента. Назовите, что согласовано и чего не хватает.',
                     'Если это актуально, когда и как вам удобно продолжить?',
                     'Сотрудник различает предложение и согласие; фиксирует действие, ответственного и срок либо условие связи.')]])


def total_score(data):
    if not data or not data.get('simulation_valid') or data.get('technical_partial'):
        return None
    if len(data.get('skills', [])) != 8 or any(x['score'] is None for x in data['skills']):
        return None
    return sum(x['score'] for x in data['skills'])


def manager_summary(data, session):
    score = total_score(data)
    employee = session.get('employee', {})
    lines = ['РЕЗУЛЬТАТ ДЛЯ РУКОВОДИТЕЛЯ',
             'Сотрудник: ' + (employee.get('name') or 'ФИО не указано') + ' · ID ' + str(employee.get('id', 'не указан')),
             'Дата: ' + session.get('completed_at', session.get('started_at', 'не сохранена')),
             'Сценарий: ' + session['fields']['customer'] + ' / ' + session['fields']['goal'],
             'Сложность: ' + dict(easy='лёгкий', medium='средний', hard='сложный')[session['fields']['difficulty']],
             'Общий балл: ' + (f'{score}/100' if score is not None else 'недоступен')]
    skills = {x['id']: x for x in data['skills']}
    for key, title, maximum in SKILLS:
        value = skills[key]['score']
        lines.append(title + ': ' + (f'{value}/{maximum}' if value is not None else 'недоступно'))
    for title, key in [('3 сильные стороны', 'strengths'), ('3 зоны развития', 'mistakes')]:
        lines.append(title)
        for index in range(3):
            value = data[key][index]['text'] if index < len(data[key]) else 'Дополнительный подтверждённый вывод отсутствует.'
            lines.append(f'{index + 1}. {value}')
    lines.append('2 приоритетных задания')
    for i, task in enumerate(data['recommendations'][:2], 1):
        lines.append(f"{i}. {task['exercise']}")
    lines.append('Что проверить в следующей тренировке')
    lines.extend('• ' + t['success_check'] for t in data['recommendations'])
    lines.append('Динамика относительно прошлых тренировок')
    previous = session.get('comparison')
    if previous and score is not None:
        lines.append(f"Тренировка №{previous['session_id']}: {previous['score']}/100 → {score}/100 ({score - previous['score']:+d}).")
        lines.append('Сравнение баллов учебных сессий, не вывод об устойчивом росте навыка.')
    else:
        lines.append('Нет сопоставимой проверенной оценки по той же методике, сценарию и сложности.')
    return '\n'.join(lines)
