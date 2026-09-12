"""Deterministic guards for client replies that must never rely on model compliance alone."""
import re

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


def install():
    from .ai import AI

    if getattr(AI, '_grounding_guards_installed', False):
        return
    original_reply = AI.reply

    def guarded_reply(self, session, text, state):
        reply = original_reply(self, session, text, state)
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
