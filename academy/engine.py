"""Application service. No transport imports or network side effects."""
import copy
import json
from .domain import (session_empty, initial_state, normalize_command, is_finish_command,
                     render_report, partial_report, VERSION, RUBRIC_VERSION)
from .diagnostics import log_failure
from .scenarios import TEMPLATES, template, menu

QUESTIONS = {'product': 'Что ты продаёшь?', 'customer': 'Кому продаёшь: роль клиента и тип компании?',
             'goal': 'Какого результата хочешь достичь в этом разговоре?'}
LEVELS = {'лёгкий': 'easy', 'легкий': 'easy', 'средний': 'medium', 'сложный': 'hard'}
SKIP = ('/skip', 'пропустить эту реплику')


class Engine:
    def __init__(self, store, ai, limit=3, admin_ids=(), max_turns=40):
        self.store, self.ai, self.limit = store, ai, limit
        self.admin_ids, self.max_turns = set(admin_ids), max_turns

    def make_report(self, s, event):
        s['versions'].update(app=VERSION, rubric=RUBRIC_VERSION, evaluator=self.ai.eval_model)
        try:
            s['prior_observations'] = [dict(session_id=p['id'], mistakes=p['report_data'].get('mistakes', []))
                                      for p in self.store.recent(event['user_id'])
                                      if p['id'] != s['id'] and p.get('report_status') == 'verified'
                                      and p.get('report_data')][:3]
            data = self.ai.evaluate(s)
            s['report_data'], s['report'] = data, render_report(data, s)
            s['report_status'] = 'verified'
        except Exception as exc:
            log_failure(event, s, 'evaluation', exc)
            s['report_data'], s['report'] = None, partial_report(s)
            s['report_status'] = 'technical_partial'

    def omit_failed(self, s, event, failed):
        if failed:
            if not is_finish_command(failed['text']) and normalize_command(failed['text']) not in ('/recheck', 'обновить разбор'):
                s['technical_errors'] = s.get('technical_errors', 0) + 1
            event['discard_failed'] = True

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
        failed = self.store.failed(user)
        if cmd in ('/retry', 'повторить обработку'):
            failed = self.store.failed(user)
            if failed and failed['attempts'] < 2:
                restored = self.store.retry_event(event, failed)
                return self.handle(restored)
            replies = (['Повторная попытка уже использована. Нажми «Пропустить эту реплику» или «Завершить тренировку».']
                       if failed else ['Необработанных реплик нет. Сохранённые ответы доставляются автоматически.'])
        elif cmd in SKIP:
            self.omit_failed(s, event, failed)
            replies = ['Проблемная реплика пропущена и не повлияет на оценку. Продолжайте разговор или завершите тренировку.'
                       if failed else 'Нет реплики, которую нужно пропустить.']
        elif failed and not event.get('retry_of') and cmd not in ('/new', 'новая тренировка', '/recheck', 'обновить разбор') and not is_finish_command(text):
            replies = ['Реплика не обработана. Можно повторить один раз, пропустить её или завершить тренировку.']
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
            report = target.get('report')
            if report and target.get('versions', {}).get('rubric') != RUBRIC_VERSION:
                report = ('Это разбор прежней версии: его выводы требуют перепроверки. '
                          'Для текущей завершённой тренировки доступно «Обновить разбор».\n\n' + report)
            replies = [report or 'Сохранённого разбора для этой тренировки нет.']
        elif cmd in ('/recheck', 'обновить разбор'):
            if s['phase'] != 'completed' or not s['card'] or not any(m['role'] == 'user' for m in s['history']):
                replies = ['Обновить можно разбор текущей завершённой тренировки с репликами менеджера.']
            else:
                self.omit_failed(s, event, failed)
                self.make_report(s, event)
                replies = [s['report'], 'Разбор обновлён. Новая тренировка не списана.']
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
            self.omit_failed(s, event, failed)
            if s['phase'] == 'completed' and s['report']:
                replies = [s['report']]
            elif s['phase'] not in ('active', 'closed'):
                replies = ['Активной тренировки нет.']
            elif not any(m['role'] == 'user' for m in s['history']):
                s['phase'] = 'completed'
                s['report'] = 'Тренировка завершена без реплик менеджера. Оценка не выставлена, попытка не списана.'
                replies = [s['report']]
            else:
                self.make_report(s, event)
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
                replies = [f'Использованы все {self.limit} бесплатные тренировки. Сохранённые разборы доступны в «Мои тренировки».\n\n' + s['knowledge']['offer']]
            else:
                s['card'] = s['card'] or self.ai.card({**s['fields'], 'situation': s['setup'],
                    'corporate_knowledge': {k: s['knowledge'][k] for k in ('product_knowledge', 'company_rules')}})
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
            s['source'] = t.get('source', 'company' if s['knowledge']['scenarios'] else 'demo')
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
