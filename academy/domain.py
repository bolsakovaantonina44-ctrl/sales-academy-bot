"""Pure contracts and deterministic rules, independent of Telegram and OpenAI."""
import copy
import json
import re

VERSION = 'demo-4.0'
RUBRIC_VERSION = 'skills-8-v1'
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
    role=S, segment=S, behavior_type=S, company_context=S, opening=S,
    facts=arr(obj(id=S, text=S, reveal_when=S)),
    unknown=arr(S), barriers=arr(obj(id=S, text=S, resolved_when=S)),
    success=choice('meeting', 'quote', 'samples', 'referral', 'order'),
    success_condition=S, hidden_motive=S,
)
PLAN_SCHEMA = obj(
    action=choice('question', 'relevant_argument', 'objection_work', 'next_step',
                  'monologue', 'pressure', 'ignored_answer', 'other'),
    reveal_ids=arr(S), resolved_ids=arr(S),
    trust_delta={'type': 'integer', 'minimum': -1, 'maximum': 1},
    interest_delta={'type': 'integer', 'minimum': -1, 'maximum': 1},
    close=choice('continue', 'refusal', 'qualified_refusal', 'success'),
    next_step_requested=B, agreement=S, reason=S,
)
REPLY_SCHEMA = obj(reply=S, used_fact_ids=arr(S))
EVIDENCE_SCHEMA = obj(message_id=I, quote=S)
EVAL_SCHEMA = obj(
    simulation_valid=B, simulation_issues=arr(S),
    skills=arr(obj(id=S, score={'type': ['integer', 'null']}, reason=S,
                   evidence=arr(EVIDENCE_SCHEMA))),
    goal=choice('achieved', 'partial', 'not_achieved'),
    outcome={'type': 'integer', 'minimum': 0, 'maximum': 3},
    next_step=S, strengths=arr(S), mistakes=arr(S), recommendations=arr(S),
    revealed=arr(S), missed=arr(S),
)


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
    validate(plan, PLAN_SCHEMA)
    new = copy.deepcopy(state)
    facts = {x['id'] for x in card['facts']}
    barriers = {x['id'] for x in card['barriers']}
    if not set(plan['reveal_ids']) <= facts or not set(plan['resolved_ids']) <= barriers:
        raise ValueError('Plan invented IDs')
    new['revealed'] = sorted(set(new['revealed']) | set(plan['reveal_ids']))
    new['resolved'] = sorted(set(new['resolved']) | set(plan['resolved_ids']))
    for name in ('trust', 'interest'):
        delta = plan[name + '_delta']
        if plan['action'] in ('pressure', 'ignored_answer', 'monologue'):
            delta = min(delta, 0)
        new[name] = max(0, min(5, new[name] + delta))
    new['turns'] += 1
    new['last_action'] = plan['action']
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
    return dict(id=None, phase='setup', fields=dict(product='', customer='', goal='', difficulty='medium'),
                setup=[], awaiting=None, card=None, state=initial_state(), history=[], report=None,
                report_data=None, template=None, technical_errors=0,
                versions=dict(app=VERSION, rubric=RUBRIC_VERSION, scenario='synthetic-v1', company='demo-v1'))


def check_evaluation(data, session):
    validate(data, EVAL_SCHEMA)
    expected = {k: maximum for k, _, maximum in SKILLS}
    got = [x['id'] for x in data['skills']]
    if len(got) != len(expected) or set(got) != set(expected):
        raise ValueError('Evaluator omitted/duplicated criteria')
    messages = {i + 1: m['content'] for i, m in enumerate(session['history'])}
    for item in data['skills']:
        score = item['score']
        if score is not None and not 0 <= score <= expected[item['id']]:
            raise ValueError('Score out of bounds')
        if not item['reason'].strip():
            raise ValueError('Missing score reason')
        if score is not None and not item['evidence']:
            raise ValueError('Score without evidence')
        for evidence in item['evidence']:
            quote = evidence['quote'].strip()
            if not quote or quote not in messages.get(evidence['message_id'], ''):
                raise ValueError('Fabricated evidence')
    ids = {f['id'] for f in session['card']['facts']}
    if not set(data['revealed']) <= ids or not set(data['missed']) <= ids:
        raise ValueError('Evaluator invented facts')
    if set(data['revealed']) & set(data['missed']):
        raise ValueError('Conflicting fact attribution')
    return data


def render_report(data, session):
    items = {v['id']: v for v in data['skills']}
    earned = sum(x['score'] or 0 for x in items.values())
    maximum = sum(m for k, _, m in SKILLS if items[k]['score'] is not None)
    lines = ['РЕЗУЛЬТАТ ЭТОЙ ТРЕНИРОВКИ', 'Это учебная диагностика, не итоговая аттестация.']
    if not data['simulation_valid']:
        lines += ['Симуляция требует проверки. Итоговый балл не выставлен.']
    elif maximum == 100:
        lines += [f'Навыки: {earned}/100']
    else:
        lines += [f'По наблюдаемым навыкам: {earned}/{maximum}.',
                  'Общий балл из 100 не рассчитан: по части навыков недостаточно данных.']
    goal = dict(achieved='достигнута', partial='частично достигнута', not_achieved='не достигнута')
    lines += [f"Цель: {goal[data['goal']]}", f"Коммерческий результат: {data['outcome']}/3 (отдельно)",
              'Следующий шаг: ' + (data['next_step'] or 'не зафиксирован'), '', 'ОЦЕНКА ПО НАВЫКАМ']
    for k, title, m in SKILLS:
        item = items[k]
        score = 'недостаточно данных' if item['score'] is None else f"{item['score']}/{m}"
        lines += [f'{title}: {score}', item['reason']]
        for e in item['evidence'][:2]:
            lines += [f"Реплика {e['message_id']}: «{e['quote']}»"]
    for title, field in [('ПРОВЕРКА СИМУЛЯЦИИ', 'simulation_issues'), ('ЧТО ПОЛУЧИЛОСЬ', 'strengths'),
                         ('ЧТО СНИЗИЛО ОЦЕНКУ', 'mistakes'), ('ЧТО ТРЕНИРОВАТЬ ДАЛЬШЕ', 'recommendations')]:
        lines += ['', title] + (['• ' + x for x in data[field]] or ['Нет замечаний.'])
    facts = {f['id']: f['text'] for f in session['card']['facts']}
    for title, field in [('ЧТО УДАЛОСЬ ВЫЯСНИТЬ', 'revealed'), ('ЧТО ОСТАЛОСЬ СКРЫТЫМ', 'missed')]:
        lines += ['', title] + (['• ' + facts[i] for i in data[field]] or ['Не отмечено.'])
    lines += ['', f"Тренировка: {session['id']}", 'Методика: ' + session['versions']['rubric']]
    return '\n'.join(lines)
