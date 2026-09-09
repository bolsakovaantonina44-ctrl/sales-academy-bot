"""Shared report data and management summary; unavailable is never a zero score."""
from .domain import SKILLS
from .pacing import FOCUS_OBJECTIONS

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
        skills.append(dict(id=key, score=None, reason='Баллы не выставлены: требуется проверка сохранённого диалога.', evidence=refs[:1]))
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


def supervisor_recommendation(data, session):
    """Cautious hiring signal: one simulation can suggest level, not prove trainability."""
    score = total_score(data)
    if score is None:
        return dict(decision='Решение: пока не принимать кадровое решение.',
                    level='Уровень: не определён',
                    trainability='Обучаемость: не определена',
                    focus=['Получить проверенный повторный результат.'])

    if score >= 80:
        level = 'Уровень: сильный'
        decision = 'Решение: можно выводить в самостоятельные продажи после короткого ввода.'
    elif score >= 65:
        level = 'Уровень: средний'
        decision = 'Решение: можно брать на испытательный срок с контролем первых разговоров.'
    elif score >= 50:
        level = 'Уровень: слабый'
        decision = 'Решение: брать только при готовности обучать; до самостоятельных продаж — повторная проверка.'
    else:
        level = 'Уровень: слабый'
        decision = 'Решение: пока не выводить в продажи; сначала обучение и повторная аттестация.'

    previous = session.get('comparison')
    if previous and previous.get('score') is not None:
        delta = score - previous['score']
        if delta >= 8:
            trainability = 'Обучаемость: предварительно высокая — заметен рост в сопоставимой тренировке.'
        elif delta > 0:
            trainability = 'Обучаемость: предварительно средняя — есть положительная динамика.'
        else:
            trainability = 'Обучаемость: пока не подтверждена — роста в сопоставимой тренировке нет.'
    else:
        trainability = 'Обучаемость: нужна ещё одна сопоставимая тренировка; по одной попытке вывод не делаем.'

    focus = [item['text'] for item in data.get('mistakes', [])[:2] if item.get('text')]
    if not focus:
        focus = [task['observation'] for task in data.get('recommendations', [])[:2] if task.get('observation')]
    if not focus:
        focus = ['Закрепить результат на более сложном сценарии.']
    return dict(decision=decision, level=level, trainability=trainability, focus=focus[:2])


def manager_summary(data, session):
    score = total_score(data)
    employee = session.get('employee', {})
    focus = session.get('training_focus')
    focus_label = FOCUS_OBJECTIONS.get(focus, {}).get('label', 'Общий разговор')
    verdict = supervisor_recommendation(data, session)
    skills = {x['id']: x for x in data['skills']}
    lines = ['РЕЗУЛЬТАТ ДЛЯ РУКОВОДИТЕЛЯ',
             'Сотрудник: ' + (employee.get('name') or 'ФИО не указано'),
             'Фокус: ' + focus_label + ' · сложность ' + {'easy':'1', 'medium':'2', 'hard':'3'}[session['fields']['difficulty']],
             'Общий балл: ' + (f'{score}/100' if score is not None else 'недоступен'),
             '', verdict['decision'], verdict['level'], verdict['trainability'],
             '', 'Зоны работы:']
    lines.extend('• ' + item for item in verdict['focus'])
    lines += ['', 'Навыки:']
    for key, title, maximum in SKILLS:
        value = skills[key]['score']
        lines.append('• ' + title + ': ' + (f'{value}/{maximum}' if value is not None else 'недоступно'))
    if data.get('strengths'):
        lines += ['', 'Сильные стороны:'] + ['• ' + x['text'] for x in data['strengths'][:2]]
    if data.get('recommendations'):
        lines += ['', 'Что делать дальше:'] + ['• ' + x['exercise'] for x in data['recommendations'][:2]]
    previous = session.get('comparison')
    lines += ['', 'Динамика:']
    if previous and score is not None:
        lines.append(f"№{previous['session_id']}: {previous['score']}/100 → {score}/100 ({score - previous['score']:+d}).")
    else:
        lines.append('Пока нет сопоставимой тренировки для оценки динамики.')
    return '\n'.join(lines)
