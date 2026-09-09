"""Pure contracts and deterministic rules, independent of Telegram and OpenAI."""
import copy
import json
import re
import uuid
from .knowledge import profile

VERSION = 'demo-4.1'
RUBRIC_VERSION = 'skills-8-v2'
SKILLS = [
    ('contact', 'Начало разговора и контакт', 10),
    ('questions', 'Качество вопросов', 15),
    ('needs', 'Выявление ситуации и потребности', 20),
    ('listening', 'Активное слушание и реакция', 15),
    ('control', 'Управление разговором', 10),
    ('arguments', 'Аргументация через потребность', 10),
    ('objections', 'Работа с возражениями', 10),
    ('next_step', 'Фиксация следующего шага', 10),
]


def obj(**props):
    return dict(type='object', properties=props, required=list(props), additionalProperties=False)


def arr(item):
    return dict(type='array', items=item)


S = {'type': 'string'}
B = {'type': 'boolean'}
I = {'type': 'integer'}


def choice(*values):
    return dict(type='string', enum=list(values))


FIELDS_SCHEMA = obj(product=S, customer=S, goal=S, difficulty=choice('easy', 'medium', 'hard'))
CARD_SCHEMA = obj(
    identity=obj(name=S, job_title=S, company=S, purchasing_role=S),
    background=obj(situation=S, need=S, priorities=S, timeframe=S, budget=S,
                   constraints=S, current_supplier=S, supplier_attitude=S, price_sensitivity=S),
    refusal_condition=S,
    role=S, segment=S, behavior_type=S, company_context=S, opening=S,
    facts=arr(obj(id=S, text=S, reveal_when=S)),
    unknown=arr(S), barriers=arr(obj(id=S, text=S, resolved_when=S)),
    success=choice('meeting', 'quote', 'samples', 'referral', 'order'),
    success_condition=S, hidden_motive=S,
)
PLAN_SCHEMA = obj(
    intent=choice('name', 'role', 'need', 'terms', 'objection', 'next_step', 'other'),
    issue_updates=arr(obj(id=S, status=choice('open', 'resolved', 'deferred'),
                          reopen_reason=choice('none', 'new_fact', 'contradiction', 'unanswered', 'critical'))),
    focus_issue_id=S,
    action=choice('question', 'relevant_argument', 'objection_work', 'next_step',
                  'monologue', 'pressure', 'ignored_answer', 'reflection', 'other'),
    reveal_ids=arr(S), resolved_ids=arr(S),
    trust_delta={'type': 'integer', 'minimum': -1, 'maximum': 1},
    interest_delta={'type': 'integer', 'minimum': -1, 'maximum': 1},
    close=choice('continue', 'refusal', 'qualified_refusal', 'success'),
    next_step_requested=B, agreement=S, reason=S,
)
REPLY_SCHEMA = obj(reply=S, used_fact_ids=arr(S))
EVIDENCE_SCHEMA = obj(message_id=I, speaker=choice('manager', 'client'), quote=S)
OBSERVATION_SCHEMA = obj(text=S, evidence=arr(EVIDENCE_SCHEMA))
ACTION_SCHEMA = obj(skill_id=choice(*(k for k, _, _ in SKILLS)),
                    observation=S, evidence=arr(EVIDENCE_SCHEMA), business_risk=S,
                    exercise=S, example=S, success_check=S)
EVAL_SCHEMA = obj(
    simulation_valid=B, simulation_issues=arr(S),
    skills=arr(obj(id=S, score={'type': ['integer', 'null']}, reason=S,
                   evidence=arr(EVIDENCE_SCHEMA))),
    goal=choice('achieved', 'partial', 'not_achieved'),
    outcome={'type': 'integer', 'minimum': 0, 'maximum': 3},
    next_step=S, next_step_status=choice('absent', 'proposed', 'agreed'),
    strengths=arr(OBSERVATION_SCHEMA), mistakes=arr(OBSERVATION_SCHEMA),
    recommendations=arr(ACTION_SCHEMA),
    revealed=arr(S), missed=arr(S),
)


def evidence_reference_schema(schema):
    """The model selects source references; the engine copies actual quotations."""
    schema = copy.deepcopy(schema)
    if schema.get('type') == 'object':
        props = schema['properties']
        if set(props) == {'message_id', 'speaker', 'quote'}:
            del props['quote']
            schema['required'].remove('quote')
        for key, value in props.items():
            props[key] = evidence_reference_schema(value)
    elif schema.get('type') == 'array':
        schema['items'] = evidence_reference_schema(schema['items'])
    return schema


EVAL_MODEL_SCHEMA = evidence_reference_schema(EVAL_SCHEMA)


def attach_evidence(data, history):
    data = copy.deepcopy(data)
    for field in ('skills', 'strengths', 'mistakes', 'recommendations'):
        for item in data[field]:
            for ref in item['evidence']:
                ident = ref['message_id']
                if type(ident) is not int or not 1 <= ident <= len(history):
                    raise EvaluationError('Unknown evidence message')
                message = history[ident - 1]
                speaker = 'manager' if message['role'] == 'user' else 'client'
                if ref['speaker'] != speaker:
                    raise EvaluationError('Evidence speaker mismatch')
                ref['quote'] = message['content']
    return data


def validate(value, schema, path='result'):
    t = schema['type']
    types = t if isinstance(t, list) else [t]
    matches = {'object': type(value) is dict, 'array': type(value) is list,
               'string': type(value) is str, 'integer': type(value) is int,
               'boolean': type(value) is bool, 'null': value is None}
    if not any(matches[k] for k in types):
        raise ValueError(f'{path}: invalid type')
    if value is None:
        return value
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path}: invalid enum')
    if type(value) is dict:
        if set(value) != set(schema['properties']):
            raise ValueError(f'{path}: invalid fields')
        for k, sub in schema['properties'].items():
            validate(value[k], sub, f'{path}.{k}')
    if type(value) is list:
        if len(value) > 100:
            raise ValueError(f'{path}: too many items')
        for sub in value:
            validate(sub, schema['items'], path)
    if type(value) is str and len(value) > 12000:
        raise ValueError(f'{path}: too long')
    if type(value) is int:
        if value < schema.get('minimum', value) or value > schema.get('maximum', value):
            raise ValueError(f'{path}: out of bounds')
    return value


def validate_card(card):
    card.setdefault('identity', dict(name='Алексей', job_title=card.get('role', ''),
                                    company='Учебная компания', purchasing_role=card.get('role', '')))
    card.setdefault('background', dict(situation=card.get('company_context', ''),
                    need='', priorities='', timeframe='', budget='', constraints='',
                    current_supplier='', supplier_attitude='', price_sensitivity=''))
    card.setdefault('refusal_condition', 'Менеджер продолжает давление после явного отказа или игнорирует ключевое ограничение.')
    validate(card, CARD_SCHEMA)
    if not 1 <= len(card['facts']) <= 12 or not 1 <= len(card['barriers']) <= 3:
        raise ValueError('Card size invalid')
    for field in ('facts', 'barriers'):
        ids = [v['id'] for v in card[field]]
        if len(set(ids)) != len(ids) or any(not re.fullmatch(r'[a-z][a-z0-9_]{0,30}', i) for i in ids):
            raise ValueError('Card IDs invalid')
        if any(not v['text'].strip() for v in card[field]):
            raise ValueError('Empty fact or barrier')
    if not card['role'].strip() or not card['opening'].strip():
        raise ValueError('Empty identity')
    if len(card['opening']) > 500:
        raise ValueError('Opening too long')
    return card


def normalize_command(text):
    text = text.lower().strip().replace('ё', 'е')
    text = re.sub(r'[.!?,;:]+$', '', text).strip()
    return re.sub(r'\s+', ' ', text)


def is_finish_command(text):
    return normalize_command(text) in {
        '/finish', 'завершить тренировку', 'заверши тренировку',
        'закончить тренировку', 'закончи тренировку', 'завершить тест', 'закончить тест'}


def chunks(text, limit=3500):
    """Count UTF-16 units; Telegram limits apply even to emoji-heavy reports."""
    result, current, count = [], [], 0
    for ch in text:
        width = 2 if ord(ch) > 0xffff else 1
        if count + width > limit:
            result.append(''.join(current))
            current, count = [], 0
        current.append(ch)
        count += width
    if current:
        result.append(''.join(current))
    return result


def reduce_plan(state, plan, card):
    plan.setdefault('intent', 'other')
    plan.setdefault('issue_updates', [])
    plan.setdefault('focus_issue_id', '')
    validate(plan, PLAN_SCHEMA)
    new = copy.deepcopy(state)
    facts = {x['id'] for x in card['facts']}
    barriers = {x['id'] for x in card['barriers']}
    new.setdefault('issues', {i: ('resolved' if i in new['resolved'] else 'unknown') for i in barriers})
    new.setdefault('issue_mentions', {})
    updated = {u['id']: u for u in plan['issue_updates']}
    if not set(updated) <= barriers or (plan['focus_issue_id'] and plan['focus_issue_id'] not in barriers):
        raise ValueError('Plan invented issue IDs')
    for ident, update in updated.items():
        old = new['issues'][ident]
        if old in ('resolved', 'deferred') and update['status'] == 'open' and update['reopen_reason'] == 'none':
            raise ValueError('Reopening issue without cause')
        new['issues'][ident] = update['status']
        if update['status'] == 'open' and update['reopen_reason'] != 'none':
            new['resolved'] = [i for i in new['resolved'] if i != ident]
    if not set(plan['reveal_ids']) <= facts or not set(plan['resolved_ids']) <= barriers:
        raise ValueError('Plan invented IDs')
    new['revealed'] = sorted(set(new['revealed']) | set(plan['reveal_ids']))
    new['resolved'] = sorted(set(new['resolved']) | set(plan['resolved_ids']))
    for ident, update in updated.items():
        if update['status'] == 'resolved':
            new['resolved'] = sorted(set(new['resolved']) | {ident})
    for ident in new['resolved']:
        new['issues'][ident] = 'resolved'
    focus = plan['focus_issue_id']
    new['focus_issue_id'] = focus
    new['suppress_issue_repeat'] = False
    if focus:
        justified = updated.get(focus, {}).get('reopen_reason', 'none') != 'none'
        new['suppress_issue_repeat'] = (new['issues'][focus] in ('resolved', 'deferred') or
                                       (new['issue_mentions'].get(focus, 0) >= 2 and not justified))
        if not new['suppress_issue_repeat']:
            new['issue_mentions'][focus] = new['issue_mentions'].get(focus, 0) + 1
    for name in ('trust', 'interest'):
        delta = plan[name + '_delta']
        if plan['action'] in ('pressure', 'ignored_answer', 'monologue'):
            delta = min(delta, 0)
        new[name] = max(0, min(5, new[name] + delta))
    new['turns'] += 1
    new['last_action'] = plan['action']
    new['last_intent'] = plan['intent']
    new['close'] = plan['close']
    # Acceptance must never be manufactured solely by reaching a numeric threshold.
    if plan['close'] == 'success':
        if set(new['resolved']) != barriers or not plan['next_step_requested'] or not plan['agreement'].strip():
            new['close'] = 'continue'
        else:
            new['agreement'] = plan['agreement']
    return new


def initial_state():
    return dict(trust=2, interest=1, revealed=[], resolved=[], turns=0,
                last_action='other', close='continue', agreement='')


def session_empty():
    return dict(id=None, scenario_id=str(uuid.uuid4()), source='manager', knowledge=profile(),
                phase='setup', fields=dict(product='', customer='', goal='', difficulty='medium'),
                setup=[], awaiting=None, card=None, state=initial_state(), history=[], report=None,
                report_data=None, template=None, technical_errors=0,
                versions=dict(app=VERSION, rubric=RUBRIC_VERSION, scenario='synthetic-v1', company='demo-v1'))


def upgrade_session(s):
    s.setdefault('scenario_id', 'legacy-' + str(s['id']))
    s.setdefault('source', 'legacy')
    s.setdefault('knowledge', profile())
    if s.get('card'):
        validate_card(s['card'])
    return s


class EvaluationError(ValueError):
    """Safe diagnostic code; never contains conversation text or secrets."""
    def __init__(self, message, stage='evaluation_validation'):
        super().__init__(message)
        self.stage = stage


def check_evaluation(data, session):
    try:
        return _check_evaluation(data, session)
    except ValueError as exc:
        raise EvaluationError(str(exc)) from None


def _check_evaluation(data, session):
    validate(data, EVAL_SCHEMA)
    expected = {k: maximum for k, _, maximum in SKILLS}
    got = [x['id'] for x in data['skills']]
    if len(got) != len(expected) or set(got) != set(expected):
        raise ValueError('Evaluator omitted/duplicated criteria')
    messages = {i + 1: m for i, m in enumerate(session['history'])}

    def check_evidence(evidence, require_manager=False):
        manager_found = False
        for item in evidence:
            message = messages.get(item['message_id'])
            if message is None:
                raise ValueError('Unknown evidence message')
            speaker = 'manager' if message['role'] == 'user' else 'client'
            if item['speaker'] != speaker:
                raise ValueError('Evidence speaker mismatch')
            quote = ' '.join(item['quote'].split())
            if not quote or quote not in ' '.join(message['content'].split()):
                raise ValueError('Fabricated evidence')
            manager_found |= speaker == 'manager'
        if require_manager and not manager_found:
            raise ValueError('Manager observation without manager evidence')

    for item in data['skills']:
        score = item['score']
        if score is not None and not 0 <= score <= expected[item['id']]:
            raise ValueError('Score out of bounds')
        if not item['reason'].strip():
            raise ValueError('Missing score reason')
        if score is not None and not item['evidence']:
            raise ValueError('Score without evidence')
        check_evidence(item['evidence'], require_manager=score is not None)
    for field in ('strengths', 'mistakes'):
        if len(data[field]) > 3:
            raise ValueError('Too many observations')
        for item in data[field]:
            if not item['text'].strip():
                raise ValueError('Empty observation')
            check_evidence(item['evidence'], require_manager=True)
    if not 1 <= len(data['recommendations']) <= 2:
        raise ValueError('Expected one or two coaching tasks')
    for task in data['recommendations']:
        if any(not task[k].strip() for k in ('observation', 'business_risk', 'exercise', 'example', 'success_check')):
            raise ValueError('Incomplete coaching task')
        check_evidence(task['evidence'], require_manager=True)
    if data['next_step_status'] == 'agreed' and not data['next_step'].strip():
        raise ValueError('Agreed step without description')
    if data['outcome'] >= 2 and data['next_step_status'] != 'agreed':
        raise ValueError('Outcome without agreed step')
    ids = {f['id'] for f in session['card']['facts']}
    if not set(data['revealed']) <= ids or not set(data['missed']) <= ids:
        raise ValueError('Evaluator invented facts')
    if set(data['revealed']) & set(data['missed']):
        raise ValueError('Conflicting fact attribution')
    return data


def render_report(data, session, include_hidden=False):
    items = {v['id']: v for v in data['skills']}
    earned = sum(x['score'] or 0 for x in items.values())
    maximum = sum(m for k, _, m in SKILLS if items[k]['score'] is not None)
    lines = ['РЕЗУЛЬТАТ ЭТОЙ ТРЕНИРОВКИ', 'Это учебная диагностика, не итоговая аттестация.']
    if session.get('technical_errors'):
        lines += [f"Из-за технических ошибок не учтено реплик: {session['technical_errors']}. За них баллы не снижены."]
    if data.get('technical_partial'):
        lines += ['Автоматическую оценку сейчас не удалось проверить. Баллы не выставлены; это не оценка 0/100.', 'Навыки: недоступны /100']
    elif not data['simulation_valid']:
        lines += ['Симуляция требует проверки. Итоговый балл не выставлен.']
    elif maximum == 100:
        lines += [f'Навыки: {earned}/100']
    else:
        lines += [f'По наблюдаемым навыкам: {earned}/{maximum}.',
                  'Общий балл из 100 не рассчитан: по части навыков недостаточно данных.']
    goal = dict(achieved='достигнута', partial='частично достигнута', not_achieved='не достигнута', unavailable='недоступна: требуется проверка')
    step_status = dict(absent='не предложен', proposed='предложен, но не согласован полностью', agreed='согласован', unavailable='недоступен: требуется проверка')
    lines += [f"Цель: {goal[data['goal']]}", ('Коммерческий результат: недоступен (отдельно)' if data['outcome'] is None else f"Коммерческий результат: {data['outcome']}/3 (отдельно)"),
              'Статус следующего шага: ' + step_status[data['next_step_status']],
              'Следующий шаг: ' + (data['next_step'] or 'не зафиксирован'), '', 'ОЦЕНКА ПО НАВЫКАМ']
    for k, title, m in SKILLS:
        item = items[k]
        score = 'недостаточно данных' if item['score'] is None else f"{item['score']}/{m}"
        lines += [f'{title}: {score}', item['reason']]
        for e in item['evidence'][:2]:
            speaker = 'Менеджер' if e['speaker'] == 'manager' else 'Клиент'
            lines += [f"{speaker}, реплика {e['message_id']}: «{e['quote']}»"]
    lines += ['', 'ПРОВЕРКА СИМУЛЯЦИИ'] + (['• ' + x for x in data['simulation_issues']] or ['Нет замечаний.'])
    for title, field in [('ЧТО ПОЛУЧИЛОСЬ', 'strengths'), ('ЧТО СНИЗИЛО ОЦЕНКУ', 'mistakes')]:
        lines += ['', title] + (['• ' + x['text'] for x in data[field]] or ['Недоступно: требуется проверка.' if data.get('technical_partial') else 'Не отмечено.'])
    lines += ['', 'КОНКРЕТНЫЕ РЕКОМЕНДАЦИИ И ЗАДАНИЯ МЕНЕДЖЕРУ', 'ПЛАН ДЛЯ РУКОВОДИТЕЛЯ',
              'По этой тренировке: выберите одно задание и проверьте его в повторном разговоре. '
              'Вывод о сотруднике делайте по нескольким разговорам.']
    if not data['simulation_valid']:
        lines += ['Сначала исправьте замечания к симуляции. Задания ниже предварительные.']
    titles = {k: title for k, title, _ in SKILLS}
    for index, task in enumerate(data['recommendations'], 1):
        lines += ['', f"{index}. {titles[task['skill_id']]}",
                  'Наблюдение: ' + task['observation']]
        for e in task['evidence']:
            speaker = 'Менеджер' if e['speaker'] == 'manager' else 'Клиент'
            lines += [f"{speaker}, реплика {e['message_id']}: «{e['quote']}»"]
        lines += ['Риск для продажи: ' + task['business_risk'],
                  'Задание сотруднику: ' + task['exercise'],
                  'Пример формулировки: ' + task['example'],
                  'Как руководителю проверить: ' + task['success_check']]
    lines += ['', 'ЧТО УДАЛОСЬ ВЫЯСНИТЬ']
    if data.get('technical_partial'):
        lines += ['Недоступно: требуется сверка вопросов и ответов по сохранённому диалогу.']
    else:
        lines += ['Ответы клиента в разговоре (факты необходимо отличать от интерпретаций):']
        client_replies = [m['content'] for m in session['history'][1:] if m['role'] == 'assistant']
        lines += ['• ' + t for t in client_replies[:5]] or ['Нет зафиксированных ответов.']
    lines += ['', 'ЧТО ОСТАЛОСЬ СКРЫТЫМ']
    if include_hidden:
        facts = {f['id']: f['text'] for f in session['card']['facts']}
        lines += ['• ' + facts[i] for i in data['missed']] or ['Не установлено.']
    else:
        lines += ['Внутренние факты клиента в отчёт сотруднику не включены. Направления дальнейшей проверки указаны в заданиях.']
    from .reporting import manager_summary
    lines += ['', manager_summary(data, session)]
    lines += ['', f"Тренировка: {session['id']}", 'Методика: ' + session['versions']['rubric']]
    return '\n'.join(lines)


def partial_report(session):
    """Same sections and rubric, with explicit unavailability instead of invented scores."""
    from .reporting import fallback_data
    return render_report(fallback_data(session), session)
