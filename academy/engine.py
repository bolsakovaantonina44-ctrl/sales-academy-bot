"""Application service. No transport imports or network side effects."""
import copy
import json
import re
from datetime import datetime, timezone
from .domain import (session_empty, initial_state, normalize_command, is_finish_command,
                     render_report, partial_report, score_level, VERSION, RUBRIC_VERSION)
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


def _display_customer(value):
    """Do not surface obvious keyboard-smash setup values in saved history."""
    value = ' '.join(str(value or '').split()).strip()
    if not value:
        return 'Клиент не указан'
    letters = ''.join(ch.lower() for ch in value if ch.isalpha())
    if len(letters) >= 6:
        vowels = set('аеёиоуыэюяaeiouy')
        vowel_ratio = sum(ch in vowels for ch in letters) / len(letters)
        longest_consonants = 0
        run = 0
        for ch in letters:
            if ch in vowels:
                run = 0
            else:
                run += 1
                longest_consonants = max(longest_consonants, run)
        if vowel_ratio < 0.18 or longest_consonants >= 6:
            return 'Некорректно указанный клиент'
    return value[:120]


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

    def history_summary(self, user):
        attempts = self.store.attempts(user)
        recent = [x for x in self.store.recent(user) if x['card']]
        if user in self.admin_ids:
            usage = 'Тестовый лимит: для администратора не применяется.'
        else:
            used = min(attempts, self.limit)
            remaining = max(0, self.limit - attempts)
            usage = f'Бесплатные тренировки: использовано {used} из {self.limit}, осталось {remaining}.'

        lines = ['МОИ ТРЕНИРОВКИ', usage, '']
        if not recent:
            lines.append('Пока нет тренировок.')
        for item in recent:
            focus = FOCUS_OBJECTIONS.get(item.get('training_focus'), {}).get('label', 'Общий разговор')
            score = total_score(item.get('report_data'))
            if score is not None:
                result = f'{score}/100 · {score_level(score)}'
            elif item['phase'] == 'completed':
                result = 'оценка недоступна'
            elif item['phase'] == 'active':
                result = 'в процессе'
            elif item['phase'] == 'closed':
                result = 'ожидает разбора'
            elif item['phase'] == 'abandoned':
                result = 'не завершена'
            else:
                result = 'подготовка'
            customer = _display_customer(item.get('fields', {}).get('customer'))
            lines.append(f"Тренировка №{item['id']}\nНавык: {focus}\nРезультат: {result}\nКлиент: {customer}")
        if recent:
            lines += ['', 'Открыть сохранённый разбор: /report НОМЕР']
        return '\n\n'.join(lines)

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
            replies = [self.history_summary(user)]
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
                replies = ['Разбор уже сформирован. Нажмите «Посмотреть разбор» или «Скачать результат».']
            elif s['phase'] not in ('active', 'closed'):
                replies = ['Сначала начните тренировку.']
            else:
                if s['phase'] == 'active':
                    s['phase'], s['outcome'] = 'closed', 'manual_finish'
                self.make_report(s, event)
                s['phase'] = 'completed'
                replies = [s['report'], f"__academy_pdf__:{s['id']}:employee",
                           'Разбор сохранён. PDF-файл отправлен автоматически. Доступны «Посмотреть разбор», «Скачать результат» и «Новая тренировка».']
        elif s['phase'] == 'completed':
            replies = ['Эта тренировка завершена. Нажмите «Посмотреть разбор» или «Новая тренировка».']
        elif s['phase'] == 'closed':
            replies = ['Разговор уже завершён. Нажмите «Завершить тренировку», чтобы получить разбор.']
        elif s['phase'] == 'active':
            if cmd in ('/start_training', 'начать тренировку'):
                replies = ['Тренировка уже запущена. Отправьте реплику клиенту или нажмите «Завершить тренировку».']
            else:
                try:
                    plan = self.ai.plan(s, text)
                    st = s['state']
                    st['turns'] += 1
                    st['substantive_turns'] += 1
                    for key in ('revealed', 'resolved'):
                        for ident in plan[key[:-2]+'_ids']:
                            if ident not in st[key]: st[key].append(ident)
                    st['trust'] = max(0, min(5, st['trust'] + plan['trust_delta']))
                    st['interest'] = max(0, min(5, st['interest'] + plan['interest_delta']))
                    st['issues'] = {u['id']: u['status'] for u in plan['issue_updates']}
                    st['close'] = plan['close']
                    st['ending_reason'] = plan.get('reason', '')
                    if plan['close'] != 'continue': st['required_objection'] = ''
                    client = self.ai.reply(s, text, st)
                    s['history'] += [dict(role='user', content=text), dict(role='assistant', content=client)]
                    replies = [client]
                    if st['close'] != 'continue':
                        s['phase'], s['outcome'] = 'closed', st['close']
                        replies.append('Разговор завершён. Нажмите «Завершить тренировку», чтобы получить разбор.')
                    elif st['turns'] >= self.max_turns:
                        s['phase'], s['outcome'] = 'closed', 'limit'
                        replies.append('Лимит диалога достигнут. Нажмите «Завершить тренировку», чтобы получить разбор.')
                except Exception as exc:
                    log_failure(event, s, 'dialogue', exc)
                    event['failed_text'] = text
                    replies = ['Не удалось обработать реплику. Можно повторить один раз, пропустить её или завершить тренировку.']
        elif s['phase'] == 'ready':
            if cmd in ('/start_training', 'начать тренировку'):
                if not s.get('training_focus'):
                    replies = ['Сначала выберите одно возражение для тренировки.']
                else:
                    s['phase'], s['state'] = 'active', initial_state(s['card'])
                    replies = ['Запускаю тренировку…', s['card']['opening']]
            elif cmd in FOCUS_BY_LABEL:
                s['training_focus'] = FOCUS_BY_LABEL[cmd]
                s['card'] = prepare_card(s['fields'], s['training_focus'])
                replies = [self.setup_summary(s)]
            elif cmd in LEVELS:
                s['fields']['difficulty'] = LEVELS[cmd]
                s['card'] = prepare_card(s['fields'], s.get('training_focus'))
                replies = [self.setup_summary(s)]
            else:
                replies = ['Выберите возражение кнопкой, при необходимости сложность 1–3, затем нажмите «Начать тренировку».']
        else:
            if cmd in TEMPLATES:
                fields = template(cmd)
                s.update(fields=fields, phase='ready', training_focus=None, card=prepare_card(fields, None))
                replies = [self.setup_summary(s)]
            elif cmd in FOCUS_BY_LABEL:
                replies = ['Сначала опишите рабочую ситуацию одним сообщением.']
            else:
                try:
                    fields = self.ai.extract_fields(text)
                    fields['difficulty'] = fields.get('difficulty') if fields.get('difficulty') in ('easy', 'medium', 'hard') else 'medium'
                    s.update(fields=fields, phase='ready', training_focus=None, card=prepare_card(fields, None))
                    replies = [self.setup_summary(s)]
                except Exception as exc:
                    log_failure(event, s, 'setup', exc)
                    replies = ['Не удалось разобрать описание. Напишите одним сообщением: что продаёте, кому и какого результата хотите достичь.']
        s['updated_at'] = datetime.now(timezone.utc).isoformat()
        self.store.save(s, event, replies, charge=charge)
        return replies


def deliver(store, user_id, send):
    """Stop at first delivery failure. Never generate another turn before this drains."""
    for item in store.outgoing(user_id):
        send(item['chat_id'], item['body'])
        store.sent(item['id'])
    store.release_waiting(user_id)
