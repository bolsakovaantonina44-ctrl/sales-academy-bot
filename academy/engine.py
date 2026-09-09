"""Application service. No transport imports or network side effects."""
import copy
import json
from datetime import datetime, timezone
from .domain import (session_empty, initial_state, normalize_command, is_finish_command,
                     render_report, partial_report, VERSION, RUBRIC_VERSION)
from .diagnostics import log_failure
from .scenarios import TEMPLATES, template, menu
from .pacing import prepare_card, FOCUS_OBJECTIONS
from .reporting import fallback_data, total_score

QUESTIONS = {'product': 'Что ты продаёшь?', 'customer': 'Кому продаёшь: роль клиента и тип компании?',
             'goal': 'Какого результата хочешь достичь в этом разговоре?'}
LEVELS = {
    '1': 'easy', '2': 'medium', '3': 'hard',
    'лёгкий': 'easy', 'легкий': 'easy', 'средний': 'medium', 'сложный': 'hard',
}
FOCUS_BY_LABEL = {normalize_command(value['label']): key for key, value in FOCUS_OBJECTIONS.items()}
SKIP = ('/skip', 'пропустить эту реплику')


class Engine:
    def __init__(self, store, ai, limit=3, admin_ids=(), max_turns=18):
        self.store, self.ai, self.limit = store, ai, limit
        self.admin_ids, self.max_turns = set(admin_ids), max_turns

    def make_report(self, s, event):
        s.pop('comparison', None)
        s['report_public_only'] = True
        s.setdefault('completed_at', datetime.now(timezone.utc).isoformat())
        s['employee'] = dict(id=event['user_id'], name=s.get('employee', {}).get('name', ''))
        s['versions'].update(app=VERSION, rubric=RUBRIC_VERSION, evaluator=self.ai.eval_model)
        try:
            s['prior_observations'] = [dict(session_id=p['id'], mistakes=p['report_data'].get('mistakes', []))
                                      for p in self.store.recent(event['user_id'])
                                      if p['id'] != s['id'] and p.get('report_status') == 'verified'
                                      and p.get('report_data') and p.get('report_public_only')][:3]
            for previous in self.store.recent(event['user_id']):
                if (previous['id'] != s['id'] and previous.get('report_status') == 'verified'
                    and previous.get('versions', {}).get('rubric') == RUBRIC_VERSION
                    and previous.get('fields') == s['fields']
                    and previous.get('training_focus') == s.get('training_focus')):
                    score = total_score(previous.get('report_data'))
                    if score is not None:
                        s['comparison'] = dict(session_id=previous['id'], score=score)
                        break
            data = self.ai.evaluate(s)
            s['report_data'], s['report'] = data, render_report(data, s)
            s['report_status'] = 'verified'
        except Exception as exc:
            log_failure(event, s, 'evaluation', exc)
            s['report_data'] = fallback_data(s)
            s['report'] = render_report(s['report_data'], s)
            s['report_status'] = 'technical_partial'

    def omit_failed(self, s, event, failed):
        if failed:
            if not is_finish_command(failed['text']) and normalize_command(failed['text']) not in ('/recheck', 'обновить разбор'):
                s['technical_errors'] = s.get('technical_errors', 0) + 1
            event['discard_failed'] = True

    def setup_summary(self, s):
        f = s['fields']
        difficulty = {'easy': '1', 'medium': '2', 'hard': '3'}[f['difficulty']]
        focus = s.get('training_focus')
        focus_label = FOCUS_OBJECTIONS.get(focus, {}).get('label', 'не выбрано')
        base = (f"Продукт: {f['product']}\nКлиент: {f['customer']}\nЦель: {f['goal']}\n"
                f"Что тренируем: {focus_label}\nСложность: {difficulty}\n\n"
                'Обычно тренировка занимает до 10 минут.\n')
        if not focus:
            return base + 'Выберите одно возражение кнопкой — это будет главный навык текущей тренировки.'
        return base + 'При необходимости выберите сложность 1, 2 или 3, затем нажмите «Начать тренировку».'

    def after_name_message(self, s):
        if s['phase'] == 'ready':
            return 'Имя сохранено.\n\n' + self.setup_summary(s)
        if s['phase'] == 'active':
            return 'Имя сохранено. Тренировка продолжается — отправьте следующую реплику клиенту.'
        if s['phase'] == 'closed':
            return 'Имя сохранено. Нажмите «Завершить тренировку», чтобы получить разбор.'
        if s['phase'] == 'completed':
            return 'Имя сохранено. Оно будет указано в отчётах по этой и следующим тренировкам.'
        return ('Спасибо, имя сохранено.\n\nПривет! Это Академия продаж. Клиент не подсказывает во время разговора; '
                'разбор — после завершения. Можно писать или отправлять голосовые до 3 минут. '
                'Обычно тренировка занимает до 10 минут.\n\n' + menu())

    def handle(self, event):
        user = event['user_id']
        text = event['text'].strip()
        cmd = normalize_command(text)
        s = self.store.current(user)
        employee = s.setdefault('employee', {'id': user, 'name': ''})
        employee['id'] = user
        employee.setdefault('name', '')
        replies, charge = [], False
        failed = self.store.failed(user)

        if cmd in ('/name', 'изменить имя', 'сменить имя'):
            s['awaiting_employee_name'] = True
            replies = ['Напишите имя и фамилию сотрудника. Они будут указаны в отчётах.']
        elif s.get('awaiting_employee_name'):
            name = ' '.join(text.split())
            if text.startswith('/') or len(name) < 2 or len(name) > 80:
                replies = ['Напишите имя и фамилию сотрудника обычным текстом, до 80 символов.']
            else:
                s['employee'] = {'id': user, 'name': name}
                s.pop('awaiting_employee_name', None)
                replies = [self.after_name_message(s)]
        elif not employee.get('name'):
            s['awaiting_employee_name'] = True
            replies = ['Перед первой тренировкой напишите, пожалуйста, имя и фамилию сотрудника. Они будут указаны в отчётах.']
        elif cmd in ('/retry', 'повторить обработку'):
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
            saved_employee = copy.deepcopy(s.get('employee', {'id': user, 'name': ''}))
            s = session_empty()
            s['employee'] = saved_employee
            replies = [menu()]
        elif cmd in ('/history', 'мои тренировки'):
            recent = [x for x in self.store.recent(user) if x['card']]
            replies = ['ПОСЛЕДНИЕ ТРЕНИРОВКИ\n' + ('\n'.join(
                f"№{x['id']}: {x['fields']['customer']} — {x['phase']}" for x in recent) or 'Пока нет тренировок.') +
                '\n\nДля сохранённого разбора: /report НОМЕР']
        elif cmd in ('/pdf', 'сформировать отчет', 'скачать результат', 'отчет сотруднику', 'отчет руководителю'):
            if s['phase'] != 'completed' or not s.get('report'):
                replies = ['Сначала завершите тренировку и получите разбор.']
            elif cmd == 'отчет руководителю' and user not in self.admin_ids:
                replies = ['Расширенный отчёт доступен администратору. Для пересылки доступно «Скачать результат».']
            else:
                if not s.get('report_public_only'):
                    self.make_report(s, event)
                audience = 'supervisor' if cmd == 'отчет руководителю' else 'employee'
                replies = [f"__academy_pdf__:{s['id']}:{audience}"]
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
            if user not in self.admin_ids:
                replies = ['Скрытый сценарий доступен только администратору.']
            elif s['phase'] != 'completed':
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
                           'Можно писать или отправлять голосовые до 3 минут. Обычно тренировка занимает до 10 минут.\n\n' + menu()]
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
                replies = [s['report'], 'Разбор сохранён. Доступны «Посмотреть разбор», «Скачать результат» и «Новая тренировка».']
                if user not in self.admin_ids and self.store.attempts(user) >= self.limit:
                    replies.append(f'Вы завершили доступные {self.limit} тренировки.\n\n' + s['knowledge']['offer'])
        elif s['phase'] == 'completed':
            replies = ['Эта тренировка завершена. Нажми «Новая тренировка» или «Посмотреть разбор».']
        elif s['phase'] == 'closed':
            replies = ['Клиент закончил разговор. Нажми «Завершить тренировку» для разбора.']
        elif cmd in ('начать тренировку', '/begin') and s['phase'] != 'ready':
            replies = ['Тренировка уже запущена. Отправьте реплику клиенту.' if s['phase'] == 'active'
                       else 'Сначала выберите новую ситуацию для тренировки.']
        elif s['phase'] == 'active':
            if s['state']['turns'] >= self.max_turns:
                s['phase'] = 'closed'
                replies = ['Достигнут лимит длины учебного разговора. Нажми «Завершить тренировку».']
            else:
                answer, state, plan = self.ai.turn(s, text)
                s['history'] += [dict(role='user', content=text), dict(role='assistant', content=answer)]
                s['state'] = state
                s.setdefault('turn_events', []).append(plan)
                charge = True
                replies = [answer]
                if state['close'] != 'continue':
                    s['phase'] = 'closed'
                    replies += ['Разговор закончен. Нажми «Завершить тренировку» — получишь разбор.']
        elif s['phase'] == 'ready' and cmd in FOCUS_BY_LABEL:
            s['training_focus'] = FOCUS_BY_LABEL[cmd]
            replies = [self.setup_summary(s)]
        elif s['phase'] == 'ready' and cmd in LEVELS:
            s['fields']['difficulty'] = LEVELS[cmd]
            replies = [self.setup_summary(s)]
        elif s['phase'] == 'ready' and cmd in ('начать тренировку', '/begin'):
            if not s.get('training_focus'):
                replies = ['Сначала выберите, какое возражение тренируем.']
            elif user not in self.admin_ids and self.store.attempts(user) >= self.limit:
                replies = [f'Использованы все {self.limit} бесплатные тренировки. Сохранённые разборы доступны в «Мои тренировки».\n\n' + s['knowledge']['offer']]
            else:
                s['card'] = s['card'] or self.ai.card({**s['fields'], 'situation': s['setup'],
                    'corporate_knowledge': {k: s['knowledge'][k] for k in ('product_knowledge', 'company_rules')}})
                s['card'] = prepare_card(s['card'], s['fields']['difficulty'], s.get('training_focus'))
                s['state'] = initial_state()
                s['phase'] = 'active'
                s['started_at'] = datetime.now(timezone.utc).isoformat()
                s['versions'].update(model=self.ai.model, evaluator=self.ai.eval_model,
                                     transcription=self.ai.transcribe_model)
                s['history'] = []
                focus_label = FOCUS_OBJECTIONS[s['training_focus']]['label']
                replies = [f'Тренировка началась. Фокус: «{focus_label}». Начните разговор первой репликой — как будто вы сами звоните или пишете клиенту.']
        elif cmd in TEMPLATES and s['phase'] in ('setup', 'ready') and s['phase'] != 'ready':
            t = template(cmd)
            s['template'], s['card'] = cmd, t['card']
            s['source'] = t.get('source', 'company' if s['knowledge']['scenarios'] else 'demo')
            s['fields'].update({k: t[k] for k in ('product', 'customer', 'goal')})
            s['phase'], s['awaiting'] = 'ready', None
            replies = [self.setup_summary(s)]
        else:
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
