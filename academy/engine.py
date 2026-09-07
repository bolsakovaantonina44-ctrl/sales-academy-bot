"""Application service. No transport imports or network side effects."""
import copy
import json
from .domain import (session_empty, initial_state, normalize_command, is_finish_command,
                     render_report, VERSION)
from .scenarios import TEMPLATES, template, menu

QUESTIONS = {'product': 'Что ты продаёшь?', 'customer': 'Кому продаёшь: роль клиента и тип компании?',
             'goal': 'Какого результата хочешь достичь в этом разговоре?'}
LEVELS = {'лёгкий': 'easy', 'легкий': 'easy', 'средний': 'medium', 'сложный': 'hard'}


class Engine:
    def __init__(self, store, ai, limit=3, admin_ids=(), max_turns=40):
        self.store, self.ai, self.limit = store, ai, limit
        self.admin_ids, self.max_turns = set(admin_ids), max_turns

    def setup_summary(self, s):
        f = s['fields']
        level = dict(easy='лёгкий', medium='средний', hard='сложный')[f['difficulty']]
        return (f"Продукт: {f['product']}\nКлиент: {f['customer']}\nЦель: {f['goal']}\nУровень: {level}\n\n"
                'Нажми «Начать тренировку». Можно изменить уровень: «Лёгкий», «Средний» или «Сложный».')

    def handle(self, event):
        user = event['user_id']
        text = event['text'].strip()
        cmd = normalize_command(text)
        s = self.store.current(user)
        replies, charge = [], False
        if cmd in ('/retry', 'повторить обработку'):
            failed = self.store.failed(user)
            if failed:
                restored = self.store.retry_event(event, failed)
                return self.handle(restored)
            replies = ['Необработанных реплик нет. Сохранённые ответы доставляются автоматически.']
        elif self.store.failed(user) and not event.get('retry_of') and cmd not in ('/new', 'новая тренировка'):
            replies = ['Предыдущая реплика ещё не обработана. Нажми «Повторить обработку» или «Новая тренировка».']
        elif cmd in ('/new', 'новая тренировка'):
            self.store.discard_failed(user)
            s = session_empty()
            replies = [menu()]
        elif cmd in ('/history', 'мои тренировки'):
            recent = [x for x in self.store.recent(user) if x['card']]
            replies = ['ПОСЛЕДНИЕ ТРЕНИРОВКИ\n' + ('\n'.join(
                f"№{x['id']}: {x['fields']['customer']} — {x['phase']}" for x in recent) or 'Пока нет тренировок.') +
                '\n\nДля сохранённого разбора: /report НОМЕР']
        elif cmd.startswith('/report ') or cmd in ('/report', 'посмотреть разбор'):
            target = s
            if cmd.startswith('/report '):
                try:
                    sid = int(cmd.split()[1])
                    target = next((x for x in self.store.recent(user) if x['id'] == sid), {})
                except (ValueError, IndexError):
                    target = {}
            replies = [target.get('report') or 'Сохранённого разбора для этой тренировки нет.']
        elif cmd in ('/scenario', 'показать скрытый сценарий'):
            if s['phase'] != 'completed':
                replies = ['Скрытый сценарий доступен после завершения и разбора тренировки.']
            else:
                card = s['card']
                replies = ['СКРЫТЫЙ УЧЕБНЫЙ СЦЕНАРИЙ\n' + '\n'.join([
                    'Роль: '+card['role'], 'Контекст: '+card['company_context'],
                    'Мотив: '+card['hidden_motive'],
                    'Факты:\n'+'\n'.join('• '+f['text'] for f in card['facts']),
                    'Барьеры:\n'+'\n'.join('• '+b['text'] for b in card['barriers']),
                    'Неизвестно клиенту:\n'+'\n'.join('• '+x for x in card['unknown']),
                    'Реалистичный успех: '+card['success_condition']])]
        elif cmd in ('/start', '/help'):
            if s['phase'] == 'active':
                replies = ['Тренировка продолжается с сохранённого места. Отправь реплику клиенту или нажми «Завершить тренировку».']
            elif s['phase'] == 'ready':
                replies = [self.setup_summary(s)]
            elif s['phase'] == 'completed':
                replies = ['Разбор сохранён. Нажми «Посмотреть разбор» или «Новая тренировка».']
            elif s['phase'] == 'closed':
                replies = ['Разговор закончен. Нажми «Завершить тренировку», чтобы получить разбор.']
            else:
                replies = ['Привет! Это Академия продаж. Клиент не подсказывает во время разговора; разбор — после завершения. '
                           'Можно писать или отправлять голосовые до 3 минут.\n\n' + menu()]
        elif is_finish_command(text):
            if s['phase'] not in ('active', 'closed'):
                replies = ['Активной тренировки нет.']
            elif not any(m['role'] == 'user' for m in s['history']):
                s['phase'] = 'completed'
                s['report'] = 'Тренировка завершена без реплик менеджера. Оценка не выставлена, попытка не списана.'
                replies = [s['report']]
            else:
                data = self.ai.evaluate(s)
                s['report_data'] = data
                s['report'] = render_report(data, s)
                s['phase'] = 'completed'
                replies = [s['report'], 'Разбор сохранён. Доступны «Показать скрытый сценарий» и «Новая тренировка».']
        elif s['phase'] == 'completed':
            replies = ['Эта тренировка завершена. Нажми «Новая тренировка» или «Посмотреть разбор».']
        elif s['phase'] == 'closed':
            replies = ['Клиент закончил разговор. Нажми «Завершить тренировку» для разбора.']
        elif s['phase'] == 'active':
            if s['state']['turns'] >= self.max_turns:
                s['phase'] = 'closed'
                replies = ['Достигнут лимит длины учебного разговора. Нажми «Завершить тренировку».']
            else:
                answer, state, plan = self.ai.turn(s, text)
                s['history'] += [dict(role='user', content=text), dict(role='assistant', content=answer)]
                s['state'] = state
                s.setdefault('turn_events', []).append(plan)
                charge = True  # Charged once, atomically, only after first successful manager turn.
                replies = [answer]
                if state['close'] != 'continue':
                    s['phase'] = 'closed'
                    replies += ['Разговор закончен. Нажми «Завершить тренировку» — получишь разбор.']
        elif s['phase'] == 'ready' and cmd in ('начать тренировку', '/begin'):
            if user not in self.admin_ids and self.store.attempts(user) >= self.limit:
                replies = [f'Использованы все {self.limit} бесплатные тренировки. Сохранённые разборы доступны в «Мои тренировки».']
            else:
                s['card'] = s['card'] or self.ai.card(s['fields'])
                s['state'] = initial_state()
                s['phase'] = 'active'
                s['versions'].update(model=self.ai.model, evaluator=self.ai.eval_model,
                                     transcription=self.ai.transcribe_model)
                s['history'] = [dict(role='assistant', content=s['card']['opening'])]
                replies = [s['card']['opening']]
        elif cmd in LEVELS and s['phase'] in ('setup', 'ready'):
            s['fields']['difficulty'] = LEVELS[cmd]
            replies = [self.setup_summary(s) if s['phase']=='ready' else 'Уровень выбран. ' + QUESTIONS.get(s['awaiting'], QUESTIONS['product'])]
        elif cmd in TEMPLATES and s['phase'] in ('setup', 'ready'):
            t = template(cmd)
            s['template'], s['card'] = cmd, t['card']
            s['fields'].update({k: t[k] for k in ('product', 'customer', 'goal')})
            s['phase'], s['awaiting'] = 'ready', None
            replies = [self.setup_summary(s)]
        else:
            # When awaiting a field, store the direct answer without asking the AI to decide readiness again.
            if s['awaiting']:
                if len(text) < 2:
                    replies = [QUESTIONS[s['awaiting']]]
                else:
                    s['fields'][s['awaiting']] = text
                    s['setup'].append(dict(role='user', content=text))
                    s['awaiting'] = None
            else:
                s['fields'] = self.ai.extract(s, text)
                s['setup'].append(dict(role='user', content=text))
                s['card'], s['template'] = None, None
            if not replies:
                missing = next((k for k in QUESTIONS if not s['fields'][k].strip()), None)
                if missing:
                    s['phase'], s['awaiting'] = 'setup', missing
                    s['setup'].append(dict(role='assistant', content=QUESTIONS[missing]))
                    replies = [QUESTIONS[missing]]
                else:
                    s['phase'] = 'ready'
                    replies = [self.setup_summary(s)]
        self.store.commit(event, s, replies, counted=charge)


def deliver(store, user_id, send):
    """Stop at first delivery failure. Never generate another turn before this drains."""
    for item in store.outgoing(user_id):
        send(item['chat_id'], item['body'])
        store.sent(item['id'])
    store.release_waiting(user_id)
