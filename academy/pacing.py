"""Small deterministic constraints around the existing planner and client writer."""
import copy
from datetime import datetime, timezone


FOCUS_OBJECTIONS = {
    'price': {
        'label': 'Дорого',
        'text': 'Дорого.',
        'resolved_when': ('Менеджер уточнил, что именно клиент считает дорогим или с чем сравнивает, '
                          'связал ценность с выявленной задачей и не ушёл сразу в скидку.'),
    },
    'no_need': {
        'label': 'Нам не надо',
        'text': 'Нам это сейчас не нужно.',
        'resolved_when': ('Менеджер выяснил причину неактуальности и текущую ситуацию, а затем либо нашёл '
                          'реальный повод продолжить, либо корректно квалифицировал отказ.'),
    },
    'supplier': {
        'label': 'Уже есть поставщик',
        'text': 'У нас уже есть поставщик.',
        'resolved_when': ('Менеджер выяснил, что устраивает и что важно в текущем поставщике, не атаковал конкурента '
                          'и нашёл критерий, при котором имеет смысл рассмотреть альтернативу.'),
    },
    'send_info': {
        'label': 'Пришлите информацию',
        'text': 'Пришлите информацию, я посмотрю.',
        'resolved_when': ('Менеджер уточнил, какая информация действительно нужна, зачем она клиенту, и согласовал '
                          'содержательное условие следующего контакта вместо простой отправки презентации.'),
    },
    'no_time': {
        'label': 'Нет времени',
        'text': 'Сейчас нет времени.',
        'resolved_when': ('Менеджер коротко обозначил причину разговора и пользу через задачу клиента, затем согласовал '
                          'допустимый формат или конкретное условие следующего контакта.'),
    },
}


def prepare_card(card, difficulty, focus=None):
    card = copy.deepcopy(card)
    count = {'easy': 1, 'medium': 2, 'hard': 3}[difficulty]
    # Existing template barriers keep their meaning but become spoken objections.
    spoken = {
        'comparable': 'У других дешевле.',
        'time': 'Сейчас нет времени на презентацию.',
        'loyalty': 'У нас уже есть поставщик.',
        'proof': 'Мне нужны подтверждения, а не обещания.',
    }
    for b in card['barriers']:
        b['text'] = spoken.get(b['id'], b['text'])

    # A selected training focus is deterministic: the trainee must encounter it.
    if focus in FOCUS_OBJECTIONS:
        selected = FOCUS_OBJECTIONS[focus]
        focused = dict(id='focus_' + focus, text=selected['text'], resolved_when=selected['resolved_when'])
        card['barriers'] = [focused] + [b for b in card['barriers'] if b['id'] != focused['id']
                                            and b['text'].strip().lower() != focused['text'].strip().lower()]

    additions = [
        ('attention', 'Пришлите информацию, я посмотрю.',
         'Менеджер выяснил актуальность и согласовал содержательный повод продолжения, а не просто согласился прислать презентацию.'),
        ('urgency', 'Сейчас нет времени.',
         'Менеджер кратко обозначил пользу через задачу клиента и согласовал допустимый формат общения.'),
        ('doubt', 'Я пока не вижу смысла что-то менять.',
         'Менеджер связал конкретный аргумент с выявленной потребностью и проверил реакцию клиента.'),
    ]
    for ident, text, condition in additions:
        if len(card['barriers']) >= count:
            break
        ident = 'required_' + ident
        if ident not in {b['id'] for b in card['barriers']} and text.lower() not in {b['text'].lower() for b in card['barriers']}:
            card['barriers'].append(dict(id=ident, text=text, resolved_when=condition))
    card['barriers'] = card['barriers'][:count]
    if difficulty == 'hard':
        card['opening'] = 'Здравствуйте. У меня мало времени. Коротко: по какому вопросу?'
    return card


def apply_behavior(session, plan, state):
    """Never manufacture agreement; schedule barriers and a natural ending."""
    old = session['state']
    barriers = session['card']['barriers']
    shown = set(old.get('presented_barriers', []))
    # A barrier cannot silently disappear before the trainee encounters it.
    state['resolved'] = [i for i in state['resolved'] if i in shown]
    for b in barriers:
        if b['id'] not in state['resolved'] and state['issues'].get(b['id']) == 'resolved':
            state['issues'][b['id']] = 'unknown'
    substantive = old.get('substantive_turns', 0) + int(plan['intent'] != 'name')
    state['substantive_turns'] = substantive
    action = plan['action']
    if action in ('monologue', 'pressure', 'ignored_answer'):
        state['trust'] = max(0, old['trust'] - 1)
        state['interest'] = max(0, old['interest'] - 1)
    elif action in ('reflection', 'relevant_argument', 'objection_work'):
        state['trust'] = min(5, old['trust'] + 1)
    # Information must be earned, one new fact at a time.
    fresh = [i for i in plan['reveal_ids'] if i not in old['revealed']]
    permitted = fresh[:1] if action in ('question', 'reflection', 'objection_work') and plan['intent'] != 'name' else []
    state['revealed'] = sorted(set(old['revealed']) | set(permitted))
    state['required_objection'] = ''
    state['required_objection_id'] = ''
    unseen = [b for b in barriers if b['id'] not in shown]
    # A focused exercise must not end before the selected objection is ever spoken.
    # The manager should get at least one real opportunity to handle the trained skill.
    if state['close'] == 'refusal' and unseen:
        state['close'] = 'continue'
        state['agreement'] = old.get('agreement', '')
        state['ending_reason'] = ''
    # Easy gives room to establish contact. Medium introduces resistance quickly.
    # Hard keeps pressure throughout the conversation.
    schedule = {
        'easy': (3,),
        'medium': (1, 4),
        'hard': (1, 3, 5),
    }[session['fields']['difficulty']]
    due = schedule[min(len(shown), len(schedule) - 1)]
    if unseen and substantive >= due and plan['intent'] != 'name' and state['close'] in ('continue', 'success'):
        b = unseen[0]
        state['required_objection'], state['required_objection_id'] = b['text'], b['id']
        state['issues'][b['id']] = 'open'
        state['focus_issue_id'] = b['id']
        state['suppress_issue_repeat'] = False
        shown.add(b['id'])
    state['presented_barriers'] = sorted(shown)
    if state['close'] == 'success' and (state['required_objection'] or len(shown) < len(barriers)
                                       or len(state['resolved']) < len(barriers)):
        state['close'] = 'continue'
        state['agreement'] = old.get('agreement', '')
    elapsed = 0
    if session.get('started_at'):
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(session['started_at'])).total_seconds()
        except (ValueError, TypeError):
            pass
    state['wrap_up'] = substantive >= 12 or elapsed >= 480
    if state['close'] == 'continue' and (substantive >= 16 or state['turns'] >= 18 or (elapsed >= 600 and substantive >= 4)):
        state['close'] = 'refusal'
        state['required_objection'] = ''
        state['ending_reason'] = 'Время разговора закончилось; согласованного следующего шага нет.'
    return state
