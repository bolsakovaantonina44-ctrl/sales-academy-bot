"""Deterministic guards for client replies that must never rely on model compliance alone."""
import re

from .pacing import manager_history, price_context_exists

_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?%?(?!\w)")
_WORD_RE = re.compile(r"[А-ЯЁ][а-яё]{2,}")
_TERMINAL_MARKERS = (
    'на этом закончим',
    'закончим разговор',
    'разговор закончим',
    'разговор закончен',
    'всего доброго',
    'до свидания',
)

_CONTACT_REPLY_RE = re.compile(
    r'\b(?:личн\w*\s+(?:контакт\w*|номер\w*)|номер\w*\s+телефон\w*|'
    r'телефон\w*|контакт\w*|мессенджер\w*|ватсап\w*|whatsapp|телеграм\w*|telegram)\b',
    re.IGNORECASE,
)
_CONTACT_REQUEST_RE = re.compile(
    r'(?:да(?:й|йте)|остав(?:ь|ьте)|переда(?:й|йте)|подскаж(?:и|ите)|назов(?:и|ите)|'
    r'пришл(?:и|ите)|скин(?:ь|ьте)|напиш(?:и|ите)|можно\s+(?:ваш|твой)|как\s+с\s+вами\s+связаться)'
    r'.{0,60}\b(?:контакт\w*|номер\w*|телефон\w*|мессенджер\w*|ватсап\w*|whatsapp|'
    r'телеграм\w*|telegram)\b|'
    r'\b(?:контакт\w*|номер\w*|телефон\w*|мессенджер\w*|ватсап\w*|whatsapp|'
    r'телеграм\w*|telegram)\b.{0,35}(?:да(?:й|йте)|остав(?:ь|ьте)|пришл(?:и|ите)|скин(?:ь|ьте))',
    re.IGNORECASE,
)
_PRICE_REPLY_RE = re.compile(
    r'\b(?:дорог\w*|дороже|дешевле|цен\w*\s+высок\w*|конкурент\w*.{0,25}\sцен\w*|'
    r'цен\w*.{0,25}\sне\s+устраива\w*)\b',
    re.IGNORECASE,
)


def _source_text(session, manager_text, state):
    card = session.get('card') or {}
    revealed = set(state.get('revealed', []))
    facts = ' '.join(
        str(item.get('text', ''))
        for item in card.get('facts', [])
        if item.get('id') in revealed
    )
    identity = ' '.join(str(v or '') for v in (card.get('identity') or {}).values())
    prior_client = ' '.join(
        str(item.get('content', '')) for item in session.get('history', [])
        if item.get('role') == 'assistant'
    )
    fields = session.get('fields') or {}
    field_text = ' '.join(str(fields.get(k, '') or '') for k in ('product', 'customer', 'goal'))
    return ' '.join((facts, identity, prior_client, field_text, manager_text or ''))


def _unsupported_numbers(reply, source):
    known = {m.group(0).replace(',', '.') for m in _NUMBER_RE.finditer(source)}
    used = {m.group(0).replace(',', '.') for m in _NUMBER_RE.finditer(reply)}
    return used - known


def _unsupported_proper_names(reply, source):
    """Catch invented Russian names/places while ignoring sentence-initial capitalization."""
    known = {w.lower() for w in _WORD_RE.findall(source)}
    unknown = []
    for match in _WORD_RE.finditer(reply):
        word = match.group(0)
        before = reply[:match.start()].rstrip()
        sentence_initial = not before or before.endswith(('.', '!', '?', ':'))
        if not sentence_initial and word.lower() not in known:
            unknown.append(word)
    return unknown


def contact_request_exists(session, current_text=''):
    return bool(_CONTACT_REQUEST_RE.search(manager_history(session, current_text)))


def _dedupe_sentences(text):
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    result, seen = [], set()
    for part in parts:
        key = re.sub(r'\W+', ' ', part.lower().replace('ё', 'е')).strip()
        if key and key not in seen:
            result.append(part)
            seen.add(key)
    return ' '.join(result)


def ground_reply(session, manager_text, reply):
    """Remove reactions to actions the manager did not actually take."""
    reply = _dedupe_sentences(reply)
    if _CONTACT_REPLY_RE.search(reply) and not contact_request_exists(session, manager_text):
        kept = [
            part for part in re.split(r'(?<=[.!?])\s+', reply)
            if not _CONTACT_REPLY_RE.search(part)
        ]
        reply = ' '.join(kept).strip() or 'Что конкретно вы предлагаете?'
    if _PRICE_REPLY_RE.search(reply) and not price_context_exists(session, manager_text):
        kept = [
            part for part in re.split(r'(?<=[.!?])\s+', reply)
            if not _PRICE_REPLY_RE.search(part)
        ]
        reply = ' '.join(kept).strip() or 'Что входит в ваше предложение?'
    return reply


def install():
    from .ai import AI

    if getattr(AI, '_grounding_guards_installed', False):
        return
    original_reply = AI.reply

    def guarded_reply(self, session, text, state):
        reply = original_reply(self, session, text, state)
        reply = ground_reply(session, text, reply)
        source = _source_text(session, text, state)

        if _unsupported_numbers(reply, source):
            reply = 'Точные цифры без подтверждения сейчас не назову.'

        if _unsupported_proper_names(reply, source):
            reply = 'Не расслышал. Повторите, пожалуйста.'

        low = reply.lower().replace('ё', 'е')
        if state.get('close') == 'continue' and any(marker in low for marker in _TERMINAL_MARKERS):
            state['close'] = 'refusal'
            state['ending_reason'] = 'Клиент явно завершил разговор.'
            state['required_objection'] = ''
        return reply

    AI.reply = guarded_reply
    AI._grounding_guards_installed = True
