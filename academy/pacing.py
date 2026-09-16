"""Small deterministic constraints around the existing planner and client writer."""
import copy
import re
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
        'label': 'Уже работаем с другим',
        'text': 'У нас уже есть поставщик, менять его сейчас не планируем.',
        'resolved_when': ('Менеджер выяснил, что устраивает и что важно в текущем поставщике или исполнителе, не атаковал конкурента '
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


CONTEXT_REQUIRED_BARRIERS = {
    'focus_price', 'focus_no_need', 'focus_supplier', 'focus_send_info',
    'comparable', 'loyalty', 'required_attention', 'required_doubt',
}
OFFER_CONTEXT_ACTIONS = {'monologue', 'relevant_argument', 'objection_work'}

_PRICE_CONTEXT_RE = re.compile(
    r'\b(?:цен(?:а|ы|е|у|ой|ою|ам|ами|ах|ник\w*|ов\w*)|стоим\w*|бюджет\w*|'
    r'скид\w*|дорог\w*|дешев\w*|руб\w*|₽|коммерческ\w+\s+предлож\w*)\b',
    re.IGNORECASE,
)
_CONTACT_BARRIER_RE = re.compile(
    r'\b(?:личн\w*\s+(?:контакт\w*|номер\w*)|номер\w*\s+телефон\w*|мессенджер\w*|'
    r'ватсап\w*|whatsapp|телеграм\w*|telegram)\b',
    re.IGNORECASE,
)
_CONTACT_REQUEST_RE = re.compile(
    r'(?:да(?:й|йте)|остав(?:ь|ьте)|переда(?:й|йте)|подскаж(?:и|ите)|назов(?:и|ите)|'
    r'пришл(?:и|ите)|скин(?:ь|ьте)|напиш(?:и|ите)|можно\s+(?:ваш|твой)|как\s+с\s+вами\s+связаться)'
    r'.{0,60}\b(?:контакт\w*|номер\w*|телефон\w*|мессенджер\w*|ватсап\w*|whatsapp|'
    r'телеграм\w*|telegram)\b',
    re.IGNORECASE,
)


def manager_history(session, current_text=''):
    """Only what the manager actually said; scenario goals are deliberately excluded."""
    messages = [
        str(item.get('content', ''))
        for item in session.get('history', [])
        if item.get('role') == 'user'
    ]
    if current_text:
        messages.append(str(current_text))
    return ' '.join(messages)


def price_context_exists(session, current_text=''):
    return bool(_PRICE_CONTEXT_RE.search(manager_history(session, current_text)))


def contact_context_exists(session, current_text=''):
    return bool(_CONTACT_REQUEST_RE.search(manager_history(session, current_text)))


def prepare_card(card, difficulty, focus=None):
    card = copy.deepcopy(card)
    count = {'easy': 1, 'medium': 2, 'hard': 3}[difficulty]
    spoken = {
        'comparable': 'У других дешевле.',
        'time': 'Сейчас нет времени на презентацию.',
        'loyalty': 'У нас уже есть поставщик, менять его сейчас не планируем.',
        'proof': 'Мне нужны подтверждения, а не обещания.',
    }
    for b in card['barriers']:
        b['text'] = spoken.get(b['id'], b['text'])

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
        # Hard mode should make the trainee earn access instead of being helped through the call.
        # The client still remains professional: resistance comes from lack of relevance, not rudeness.
        card['opening'] = 'Добрый день. У меня буквально минута. Что конкретно вы предлагаете и почему это может быть нам актуально?'
    return card


def _barrier_requires_evidence(barrier):
    return (barrier.get('id') in CONTEXT_REQUIRED_BARRIERS
            or bool(_CONTACT_BARRIER_RE.search(barrier.get('text', ''))))


def _barrier_has_context(barrier, offer_context, price_context=False, contact_context=False):
    """Delay objections that are nonsensical before the client has an offer to react to."""
    if barrier.get('id') in {'focus_price', 'comparable'}:
        return price_context
    if _CONTACT_BARRIER_RE.search(barrier.get('text', '')):
        return contact_context
    return barrier.get('id') not in CONTEXT_REQUIRED_BARRIERS or offer_context


def apply_behavior(session, plan, state):
    """Never manufacture agreement; schedule barriers and a natural ending."""
    old = session['state']
    barriers = session['card']['barriers']
    shown = set(old.get('presented_barriers', []))
    state['resolved'] = [i for i in state['resolved'] if i in shown]
    for b in barriers:
        if b['id'] not in state['resolved'] and state['issues'].get(b['id']) == 'resolved':
            state['issues'][b['id']] = 'unknown'
    substantive = old.get('substantive_turns', 0) + int(plan['intent'] != 'name')
    state['substantive_turns'] = substantive
    action = plan['action']
    offer_context = old.get('offer_context', False) or action in OFFER_CONTEXT_ACTIONS
    state['offer_context'] = offer_context
    if action in ('monologue', 'pressure', 'ignored_answer'):
        state['trust'] = max(0, old['trust'] - 1)
        state['interest'] = max(0, old['interest'] - 1)
    elif action in ('reflection', 'relevant_argument', 'objection_work'):
        state['trust'] = min(5, old['trust'] + 1)
    # Information is still earned one fact at a time. Hard mode creates resistance through
    # its opening, barrier cadence and trust/interest dynamics; a valid discovery question
    # must remain able to reveal one relevant fact so the conversation cannot deadlock.
    fresh = [i for i in plan['reveal_ids'] if i not in old['revealed']]
    permitted = fresh[:1] if action in ('question', 'reflection', 'objection_work') and plan['intent'] != 'name' else []
    state['revealed'] = sorted(set(old['revealed']) | set(permitted))
    state['required_objection'] = ''
    state['required_objection_id'] = ''
    unseen = [b for b in barriers if b['id'] not in shown]
    forced_focus = state['close'] == 'refusal' and bool(unseen)
    if forced_focus:
        state['close'] = 'continue'
        state['agreement'] = old.get('agreement', '')
        state['ending_reason'] = ''
    schedule = {
        'easy': (3,),
        'medium': (1, 4),
        'hard': (1, 3, 5),
    }[session['fields']['difficulty']]
    due = schedule[min(len(shown), len(schedule) - 1)]
    if unseen and substantive >= due and plan['intent'] != 'name' and state['close'] in ('continue', 'success'):
        b = unseen[0]
        current_text = session.get('_current_manager_text', '')
        if (forced_focus and not _barrier_requires_evidence(b)) or _barrier_has_context(
                b, offer_context, price_context_exists(session, current_text),
                contact_context_exists(session, current_text)):
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
