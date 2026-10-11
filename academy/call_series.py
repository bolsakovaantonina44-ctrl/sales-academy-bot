"""Fixed call manifests, durable progression and evidence-based aggregate reports."""
import copy
import hashlib
import json
import math
import random
import sqlite3
import time
from datetime import datetime, timezone
from collections import Counter, defaultdict
from .domain import (session_empty, validate_card,
                     normalize_command, is_finish_command, SKILLS, score_level)
from .reporting import total_score

VERSION = 'call-series-v1'
# Two materially different causes for every surface objection.
OBJECTIONS = {
    'no_money': ('Нет денег', ['Бюджет на новые покупки закрыт до следующего периода.', 'Средства есть, но приоритетом назначен другой проект.']),
    'expensive': ('Дорого', ['Не видит пользы по сравнению с затратами.', 'Сравнивает с другим объёмом или составом предложения.']),
    'competitor': ('Конкурент дешевле', ['Есть более дешёвое предложение с другим составом.', 'Использует цену конкурента как повод проверить условия.']),
    'already_have': ('Товар или услуга уже есть', ['Задача полностью решена, замена не нужна.', 'Решение есть, но появились новые требования.']),
    'supplier': ('Действующий поставщик', ['Действующий договор ограничивает смену поставщика.', 'Поставщик устраивает, но важно иметь резерв.']),
    'poor_quality': ('Прошлый некачественный продукт', ['Ранее получил брак и опасается повторения.', 'Не подошёл способ применения, причина ещё не выяснена.']),
    'deadlines': ('Срыв сроков', ['Предыдущая поставка сорвала собственные обязательства.', 'Не доверяет общим обещаниям без подтверждения сроков.']),
    'other_manager': ('Работал с другим менеджером', ['Доверяет прежнему контакту и не понимает замену.', 'Прежний менеджер оставил незакрытый вопрос.']),
    'not_you': ('С вами не хочу', ['Есть негативный опыт общения с этой компанией.', 'Не считает текущего собеседника подходящим контактом.']),
    'think': ('Я подумаю', ['Не определил критерии выбора.', 'Нужно обсудить решение с коллегой.']),
    'proposal': ('Пришлите предложение', ['Хочет сравнить релевантные варианты.', 'Просит общий файл, чтобы закончить неактуальный разговор.']),
    'no_time': ('Нет времени', ['Сейчас занят срочной задачей.', 'Не видит причины выделять время на предложение.']),
    'not_decider': ('Не принимаю решение', ['Может передать информацию реальному ЛПР.', 'Не влияет на решение и не имеет разрешения передавать контакты.']),
    'defer': ('Нужна отсрочка', ['Платёж зависит от поступления средств по проекту.', 'Хочет сохранить оборотные средства, условия обсуждаемы.']),
}
GOALS = {
    'meeting': ('Согласовать встречу', 'Обе стороны согласовали встречу: тему, участников и время либо конкретное условие назначения.'),
    'next_step': ('Согласовать следующий шаг', 'Обе стороны согласовали конкретное действие, ответственного и срок либо условие контакта.'),
    'purchase': ('Получить покупку', 'Клиент явно подтвердил заказ или покупку и согласовал необходимые условия; интерес или запрос КП недостаточен.'),
    'proposal': ('Согласовать предложение', 'Клиент подтвердил требования к релевантному предложению и согласовал его обсуждение; просьба общего прайса недостаточна.'),
    'samples': ('Согласовать образцы', 'Согласованы нужные образцы, получатель и порядок передачи.'),
    'referral': ('Получить контакт ЛПР', 'Клиент согласовал передачу контакта ЛПР или соединение; имя без способа связи недостаточно.'),
    'free_demo': ('Согласовать бесплатное тестирование', 'Клиент явно согласился на бесплатный тестовый доступ и согласовал конкретное действие: получение ссылки, подключение доступа или дату начала. Просто интерес или просьба прислать информацию недостаточны.'),
    'custom_goal': ('Своя цель', 'Критерий успешного звонка задан в настройках серии.'),
}
OUTCOMES = {'meeting': 'Встреча', 'next_step': 'Согласованный следующий шаг', 'purchase': 'Покупка',
            'proposal': 'Предложение с обсуждением', 'samples': 'Образцы', 'referral': 'Контакт ЛПР', 'free_demo': 'Бесплатное тестирование', 'custom_goal': 'Своя цель', 'refusal': 'Отказ',
            'no_agreement': 'Без договорённости', 'unavailable': 'Не проверено'}
DIFFICULTIES = {'easy': 'лёгкая', 'medium': 'средняя', 'hard': 'высокая'}
REASONS = {'employee_finish': 'менеджер завершил звонок', 'natural_end': 'клиент завершил разговор',
           'employee_abort': 'сотрудник прервал серию', 'technical_abort': 'серия завершена после технического сбоя'}
INTERRUPTIONS = {'employee_reentry': 'повторный вход', 'employee_navigation': 'попытка выхода из режима',
                'unexplained_gap': 'интервал ответа без установленной причины', 'unexplained_between_call_gap': 'интервал между звонками без установленной причины',
                'employee_abort': 'прерывание сотрудником', 'technical_abort': 'выход после сбоя',
                'technical_restart': 'перезапуск сервиса', 'technical_delivery': 'сбой доставки',
                'technical_outcome': 'результат не прошёл проверку', 'technical_assessment': 'оценка недоступна'}


def cost_envelope(count, turns=8, voice_seconds_per_turn=30):
    if count not in (10, 20, 30) or not 1 <= turns <= 18:
        raise ValueError('Invalid cost envelope')
    return dict(calls=count, assumed_turns=turns,
        typical_llm_requests=count * (turns * 2 + 4),
        normal_max_llm_requests=count * (18 * 4 + 6 + 2),
        typical_transcriptions=count * turns,
        assumed_audio_minutes=count * turns * voice_seconds_per_turn / 60,
        maximum_audio_minutes=count * 18 * 3,
        maximum_output_tokens=count * (18 * 2 * (1800 + 1200) + 3 * (10000 + 2000) + 1200 + 800))
HELP = ('СЕРИЯ ЗВОНКОВ\n\nСамостоятельная практика: /series 10, /series 20 или /series 30 '
        'после подготовки продукта и роли клиента в обычном тренажёре. Нужен корпоративный доступ '
        'или оплаченные попытки на всю серию.\n'
        'Настройки: /series_config JSON (продукт, роль, цели, частоты, сложность, собственные ситуации).\n'
        'Руководитель: /series_assign TELEGRAM_ID JSON. Сотрудник: /series_begin.\n'
        '/series_list — мои серии; /series_report НОМЕР — итог.\n'
        'Без подсказок, разбора между звонками, паузы и пересдачи. /series_abort завершает серию с прерыванием. '
        'При сбое /retry повторяет обработку той же реплики.\n'
        'Пример настроек: {"count":10,"product":"Бухгалтерское сопровождение",'
        '"customer":"Собственник компании","goals":["meeting","next_step"],'
        '"objections":{"expensive":2,"supplier":1},"interested_percent":20,"difficulty":"hard"}.\n'
        'Частоты возражений — относительные веса; точные количества фиксируются до старта. '
        'Названия: ' + ', '.join(f'{k} — {v[0]}' for k, v in OBJECTIONS.items()))

STEPS = ['count', 'product', 'customer', 'goals', 'objections', 'interested_percent', 'difficulty', 'custom', 'confirm']


def wizard_prompt(draft):
    step, data = draft['step'], draft['data']
    prompts = {
        'target': 'Кому назначить аттестацию? Укажите Telegram ID сотрудника с корпоративным доступом.',
        'count': 'СЕРИЯ ЗВОНКОВ\n\nВыберите объём: 10, 20 или 30 звонков. Условия и порядок фиксируются до старта. Подсказок, паузы и пересдачи нет. /cancel — отменить настройку.',
        'product': 'Какой продукт или услугу продаёт менеджер? До 300 символов.',
        'customer': 'Кому звонит менеджер? Укажите роль клиента и тип компании. Каждый звонок получит нового вымышленного клиента.',
        'goals': 'Выберите цели номерами через запятую:\n' + '\n'.join(f'{i+1}. {v[0]}' for i, v in enumerate(GOALS.values())) + '\nЦели будут чередоваться. Критерии фиксируются до старта.',
        'objections': 'Выберите возражения номерами через запятую. Частоты можно указать как «1:2, 5:1»: первое будет встречаться вдвое чаще второго. Или нажмите «Все возражения».\n'
                      + '\n'.join(f'{i+1}. {v[0]}' for i, v in enumerate(OBJECTIONS.values())),
        'interested_percent': 'Какая доля клиентов будет заинтересована без возражений? Введите процент от 5 до 80. Точное количество будет округлено и закреплено до старта.',
        'difficulty': 'Выберите максимальную сложность: лёгкая, средняя или высокая. Внутри серии сложность постепенно повышается до выбранного уровня.',
        'custom': 'Добавьте собственную ситуацию или нажмите «Готово». Формат:\n'
                  'Название | реплика клиента | причина 1 / причина 2 | можно или нельзя | частота 1–100\n'
                  'Например: Аудит | Нужен аудит безопасности. | Нужны документы / Нужен доступ проверяющему | нельзя | 1\n'
                  '«Нельзя» означает объективное ограничение: сделка в этом звонке недостижима. Корректный отказ не обнуляет качество продажи.',
    }
    if step != 'confirm':
        return prompts[step]
    lines = [f"СЕРИЯ: {data['count']} звонков", f"Продукт: {data['product']}", f"Клиент: {data['customer']}",
             'Цели: ' + ', '.join((data.get('custom_goal_label', GOALS[g][0]) if g == 'custom_goal' else GOALS[g][0]) for g in data['goals']),
             f"Заинтересованных без возражений: {data['interested_percent']}%. Максимальная сложность: {DIFFICULTIES[data['difficulty']]}.",
             'Возражения и относительные частоты:']
    for key, weight in data['objections'].items():
        label = data['custom'][int(key.split('_')[1])]['label'] if key.startswith('custom_') else OBJECTIONS[key][0]
        lines.append(f'{label}: {weight}')
    lines += ['В базовых ситуациях часть ограничений объективно неустранима. Это не основание автоматически снижать навыки.',
              'Нажмите «Назначить серию».' if draft['mode'] == 'attestation' else 'Нажмите «Подготовить серию», затем /series_begin. Оплаченные попытки резервируются на весь объём при старте.']
    return '\n\n'.join(lines)


def wizard_rows(session):
    draft = session.get('series_setup')
    if not draft:
        return None
    return {'count': [['10', '20', '30']], 'goals': [['1', '2', '3'], ['4', '5', '6']],
            'objections': [['Все возражения']], 'interested_percent': [['10', '20', '30', '50']],
            'difficulty': [['Лёгкая', 'Средняя', 'Высокая']], 'custom': [['Готово']],
            'confirm': [['Назначить серию' if draft['mode'] == 'attestation' else 'Подготовить серию']]}.get(draft['step'], []) + [['/cancel']]


def config(value):
    if not isinstance(value, dict):
        raise ValueError('Настройки должны содержать поля серии')
    value = copy.deepcopy(value)
    allowed = {'custom_goal_label', 'custom_goal_criterion', 'count', 'product', 'customer', 'goals', 'objections', 'interested_percent',
               'difficulty', 'custom', 'unreachable_percent', 'response_gap_seconds'}
    if set(value) - allowed:
        raise ValueError('Неизвестные поля настроек')
    if type(value.get('count')) is not int or value['count'] not in (10, 20, 30):
        raise ValueError('Количество звонков: 10, 20 или 30')
    for field in ('product', 'customer'):
        if not isinstance(value.get(field), str) or not 2 <= len(value[field].strip()) <= 300:
            raise ValueError('Укажите продукт и роль клиента (2–300 символов)')
    value.setdefault('goals', ['next_step'])
    if (not isinstance(value['goals'], list) or not value['goals']
            or any(not isinstance(g, str) or g not in GOALS for g in value['goals']) or len(value['goals']) > len(GOALS)):
        raise ValueError('Допустимые цели: ' + ', '.join(GOALS))
    if 'custom_goal' in value['goals']:
        for field in ('custom_goal_label', 'custom_goal_criterion'):
            if not isinstance(value.get(field), str) or not 5 <= len(value[field].strip()) <= 300:
                raise ValueError('Укажите собственную цель и критерий успеха (5–300 символов)')
    value.setdefault('custom', [])
    if not isinstance(value['custom'], list) or len(value['custom']) > 14:
        raise ValueError('До 14 собственных ситуаций')
    for item in value['custom']:
        if not isinstance(item, dict) or set(item) != {'label', 'objection', 'causes', 'overcome'}:
            raise ValueError('Ситуация: label, objection, causes (список причин), overcome (true/false)')
        if (any(not isinstance(item[k], str) or not 2 <= len(item[k]) <= 200 for k in ('label', 'objection'))
                or type(item['overcome']) is not bool or not isinstance(item['causes'], list)
                or not 1 <= len(item['causes']) <= 5
                or any(not isinstance(c, str) or not 2 <= len(c) <= 500 for c in item['causes'])):
            raise ValueError('Некорректная собственная ситуация')
    value.setdefault('objections', {key: 1 for key in OBJECTIONS})
    known = set(OBJECTIONS) | {f'custom_{i}' for i in range(len(value['custom']))}
    weights = value['objections']
    if (not isinstance(weights, dict) or not weights or set(weights) - known
            or any(type(w) is not int or not 1 <= w <= 100 for w in weights.values())):
        raise ValueError('Возражения: известный ID → целый относительный вес 1–100')
    for key, default, low, high in [('interested_percent', 20, 5, 80),
                                   ('unreachable_percent', 20, 0, 100),
                                   ('response_gap_seconds', 300, 30, 3600)]:
        value.setdefault(key, default)
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(f'{key}: целое число {low}–{high}')
    value.setdefault('difficulty', 'hard')
    if value['difficulty'] not in ('easy', 'medium', 'hard'):
        raise ValueError('Сложность: easy, medium или hard')
    return value


def manifest_hash(calls):
    return hashlib.sha256(json.dumps(calls, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build_manifest(settings, seed=None):
    cfg = config(settings)
    rng = random.Random(seed if seed is not None else random.SystemRandom().getrandbits(128))
    n = cfg['count']
    interested = max(1, round(n * cfg['interested_percent'] / 100))
    objection_count = n - interested
    total = sum(cfg['objections'].values())
    exact = {k: objection_count * w / total for k, w in cfg['objections'].items()}
    counts = {k: math.floor(v) for k, v in exact.items()}
    ranked = sorted(exact, key=lambda k: (exact[k] - counts[k], rng.random()), reverse=True)
    for key in ranked[:objection_count - sum(counts.values())]:
        counts[key] += 1
    schedule = ['interested'] * interested + [k for k, count in counts.items() for _ in range(count)]
    rng.shuffle(schedule)
    blocked = set(rng.sample([i for i, k in enumerate(schedule) if k != 'interested'],
                             round(objection_count * cfg['unreachable_percent'] / 100)))
    calls = []
    contexts = ['новый проект', 'расширение текущей деятельности', 'проверка альтернатив',
                'замена устаревшего решения', 'сезонная задача', 'резервный вариант']
    names = ['Алексей', 'Мария', 'Ирина', 'Дмитрий', 'Елена', 'Сергей', 'Ольга', 'Андрей']
    for i, key in enumerate(schedule):
        goal = cfg['goals'][i % len(cfg['goals'])]
        difficulty = {'easy': ['easy'] * 3, 'medium': ['easy', 'medium', 'medium'],
                      'hard': ['easy', 'medium', 'hard']}[cfg['difficulty']][min(2, i * 3 // n)]
        if key == 'interested':
            label, reason, objection, overcome = 'Без возражений', 'Заинтересован в решении текущей задачи.', '', True
        elif key.startswith('custom_'):
            custom = cfg['custom'][int(key.split('_')[1])]
            label, reason, objection, overcome = custom['label'], rng.choice(custom['causes']), custom['objection'], custom['overcome']
        else:
            label, causes = OBJECTIONS[key]
            reason, objection, overcome = rng.choice(causes), label + '.', i not in blocked
        context = f'Учебная компания {i+1}: {contexts[i % len(contexts)]}. {reason}'
        if not overcome:
            reason += ' Обязательное внутреннее ограничение запрещает согласование покупки и нового шага сейчас.'
        goal_label = cfg['custom_goal_label'] if goal == 'custom_goal' else GOALS[goal][0]
        goal_criterion = cfg['custom_goal_criterion'] if goal == 'custom_goal' else GOALS[goal][1]
        card = dict(identity=dict(name=names[i % len(names)], job_title=cfg['customer'],
                                 company=f'Учебная компания {i+1}', purchasing_role=cfg['customer']),
            background=dict(situation=context, need=f'Проверить пригодность решения для задачи: {contexts[i % len(contexts)]}.',
                priorities='Релевантность текущей задаче и соблюдение согласованных условий.',
                timeframe='', budget='', constraints=reason, current_supplier='',
                supplier_attitude='', price_sensitivity=''),
            refusal_condition='Явное давление после отказа или игнорирование объективного ограничения.',
            role=cfg['customer'], segment='Учебный клиент', behavior_type='Заинтересован' if key == 'interested' else difficulty,
            company_context=context, opening='Добрый день. Слушаю вас.' if difficulty != 'hard' else 'Добрый день. У меня мало времени. По какому вопросу?',
            facts=[dict(id='task', text=f'Текущая задача: {contexts[i % len(contexts)]}.', reveal_when='Уместный вопрос о текущей ситуации.'),
                   dict(id='cause', text=reason, reveal_when='Менеджер уточнил причину сомнения или ограничения.')],
            unknown=['Цена, характеристики и срок продукта продавца не известны без его подтверждённого предложения.', 'Точные суммы и даты бюджета не заданы.'],
            barriers=[] if not objection else [dict(id='series_objection', text=objection,
                resolved_when=('Менеджер выяснил причину и предложил релевантный проверяемый вариант с согласия клиента.'
                               if overcome else 'В текущем звонке не может быть устранено; не соглашаться ради завершения игры.'))],
            success={'next_step': 'meeting', 'purchase': 'order', 'proposal': 'quote'}.get(goal, goal),
            success_condition=goal_criterion + ('' if overcome else ' Цель сделки в этом звонке недостижима; корректный отказ не снижает навыки автоматически.'),
            hidden_motive=reason)
        validate_card(card)
        calls.append(dict(ordinal=i+1, objection=key, label=label, reason=reason,
                          overcome=overcome, difficulty=difficulty, goal=goal, goal_label=goal_label, card=card))
    return cfg, calls


def new_call(series, employee):
    call = series['manifest'][series['index']]
    s = session_empty()
    s.update(series_id=series['id'], series_index=call['ordinal'], series_active=True,
             employee=employee, phase='active', card=copy.deepcopy(call['card']),
             series_call_unreachable=not call['overcome'],
             lpr_gate_passed=True, source='call_series', training_focus=None,
             started_at=datetime.now(timezone.utc).isoformat(),
             history=[dict(role='assistant', content=call['card']['opening'])],
             response_times=[], last_prompt_at=time.time())
    s['fields'].update(product=series['config']['product'], customer=series['config']['customer'],
                       goal=call.get('goal_label', call.get('goal_label', GOALS[call['goal']][0])), difficulty=call['difficulty'])
    s['versions']['scenario'] = VERSION
    return s


def intro(series, s):
    return [f"Серия №{series['id']} · звонок {s['series_index']}/{series['config']['count']}\n"
            f"Клиент: {s['fields']['customer']} · {s['card']['identity']['company']}\n"
            f"Вы предлагаете: {s['fields']['product']}\nЦель: {s['fields']['goal']}\n"
            'Начните разговор. Подсказок и разбора до конца серии нет.', s['card']['opening']]


def report(series, results):
    rows = [r for r in results if r['status'] != 'pending']
    verified = [r for r in rows if r.get('assessment_status') == 'verified'
                and (r['session'].get('report_data') or {}).get('simulation_valid')]
    conversion = [r for r in rows if r.get('outcome_status') == 'verified'
                  and (r in verified or r.get('assessment_status') == 'no_speech')]
    achieved = sum(r.get('goal_achieved', False) for r in conversion)
    scores = [total_score(r['session'].get('report_data')) for r in verified]
    scores = [s for s in scores if s is not None]
    interruptions = series.get('interruptions', [])
    technical = sum(x['kind'].startswith('technical_') for x in interruptions)
    unknown = sum(x['kind'].startswith('unexplained_') for x in interruptions)
    employee = len(interruptions) - technical - unknown
    lines = [f"ИТОГ СЕРИИ №{series['id']} · {'Аттестация' if series['mode'] == 'attestation' else 'Самостоятельная практика'}",
             f"Статус: {'Прервана' if series['status'] == 'aborted' else 'Завершена'}. Сохранено звонков: {len(rows)}/{series['config']['count']}.",
             f"Завершено: {sum(r['status'] == 'completed' for r in rows)}; прервано: {sum(r['status'] == 'interrupted' for r in rows)}.",
             f'События сотрудника: {employee}; технические события: {technical}; интервалы без установленной причины: {unknown}.',
             f"Учебная конверсия: {achieved}/{len(conversion)} проверенных звонков"
             + (f' ({achieved/len(conversion):.0%}).' if conversion else ' — недостаточно данных.'),
             f"В знаменатель не включены: {len(rows)-len(conversion)} технически/семантически непроверенных; "
             f"не проведено: {series['config']['count']-len(rows)}.",
             'Цели и критерии зафиксированы до старта. Прерванный проверенный звонок остаётся в знаменателе.',
             'Разные результаты: ' + '; '.join(f'{OUTCOMES[k]}: {v}' for k, v in Counter(r.get('outcome', 'unavailable') for r in rows).items())]
    usage = series.get('usage')
    if usage:
        lines.append(f"ИИ: {usage['calls']} попыток API; входных токенов {usage['input_tokens'] or 0}, "
                     f"выходных {usage['output_tokens'] or 0}; резерв голоса {usage['audio_seconds'] or 0} сек. "
                     + (f"Стоимость/резерв: ${usage['usd'] or 0:.4f} по настроенным тарифам."
                        if not usage['unpriced'] else 'Долларовая стоимость неполная: тарифы не настроены для всех вызовов.'))
    if scores:
        avg = sum(scores)/len(scores)
        lines.append(f'Качество продажи: {avg:.1f}/100 · {score_level(avg)}; полных оценок {len(scores)}.')
    else:
        lines.append('Общий балл качества недоступен: недостаточно полных проверенных оценок.')
    lines.append('Критерии качества: ' + '; '.join(f'{title} {maximum}' for _, title, maximum in SKILLS)
                 + '. Баллы только за наблюдаемые реплики; N/A не превращается в ноль.')
    lines.append('Уровни: 0–39 системная отработка; 40–59 рабочая база; 60–74 уверенный; '
                 '75–89 сильный; 90–100 очень сильный. Это интервалы взвешенного учебного балла, не кадровый допуск.')
    groups = defaultdict(list)
    for r in rows:
        groups[series['manifest'][r['session']['series_index']-1]['label']].append(r)
    lines.append('ПО ГРУППАМ ВОЗРАЖЕНИЙ')
    for label, group in groups.items():
        values = [total_score(r['session'].get('report_data')) for r in group if r.get('assessment_status') == 'verified']
        values = [v for v in values if v is not None]
        checked = [r for r in group if r in conversion]
        mean = f'{sum(values)/len(values):.1f}/100' if values else 'недостаточно данных'
        lines.append(f"{label}: {len(group)} звонков, качество {mean}, целей {sum(r.get('goal_achieved', False) for r in checked)}/{len(checked)}.")
    for phase, title in enumerate(['Начало', 'Середина', 'Конец']):
        phase_rows = [r for r in verified if min(2, (r['session']['series_index']-1)*3//series['config']['count']) == phase]
        values = [total_score(r['session']['report_data']) for r in phase_rows]
        values = [v for v in values if v is not None]
        lines.append(f'{title}: ' + (f'{sum(values)/len(values):.1f}/100 ({len(values)} полных оценок)' if values else 'недостаточно данных'))
    timing = [t['seconds'] for r in rows for t in r['session'].get('response_times', []) if t.get('seconds') is not None]
    lines += [f'Время ответа: среднее {sum(timing)/len(timing):.1f} сек, измерено {len(timing)} ответов.' if timing else 'Время ответа: нет подтверждённых измерений доставки.',
              'Изменение сложности влияет на динамику: это наблюдение в данной серии, не доказательство выносливости за рабочий день.',
              'Учебная конверсия не является прогнозом реальных продаж.', 'ЗВОНКИ И ПОДТВЕРЖДАЮЩИЕ РЕПЛИКИ']
    for r in rows:
        s = r['session']
        call = series['manifest'][s['series_index']-1]
        score = total_score(s.get('report_data')) if r.get('assessment_status') == 'verified' else None
        lines.append(f"Звонок {s['series_index']} · {call['label']} · сложность {DIFFICULTIES[call['difficulty']]} · цель: {GOALS[call['goal']][0]}\n"
                     f"Результат: {OUTCOMES.get(r.get('outcome'), 'Не проверено')}; качество: "
                     f"{score if score is not None else 'недоступно'}; {'прерван' if r['status'] == 'interrupted' else 'завершён'}.")
        lines.append(f"Завершение: {REASONS.get(r['reason'], r['reason'])}. " + r.get('outcome_reason', ''))
        for skill in (s.get('report_data') or {}).get('skills', []):
            refs = ' / '.join(f"#{e['message_id']} {e['speaker']}: «{e['quote']}»" for e in skill.get('evidence', []))
            title = next(title for ident, title, _ in SKILLS if ident == skill['id'])
            lines.append(f"{title}: {skill['score'] if skill['score'] is not None else 'N/A'} — {skill['reason']}" + (f'\n{refs}' if refs else ''))
        for ref in r.get('outcome_evidence', []):
            lines.append(f"Результат #{ref['message_id']} {ref['speaker']}: «{ref['quote']}»")
    lines += ['Журнал прерываний: ' + ('; '.join(f"{INTERRUPTIONS.get(i['kind'], 'Технический сбой' if i['kind'].startswith('technical_') else i['kind'])}"
              + (f", звонок {i['call']}" if i.get('call') else '')
              + (f", {i['seconds']:.1f} сек" if i.get('seconds') else '') for i in interruptions) or 'нет'),
              'Критерии целей: ' + '; '.join(f'{GOALS[g][0]}: {GOALS[g][1]}' for g in series['config']['goals']),
              'Состав и критерии сохранены до старта серии.']
    return '\n\n'.join(lines)


class SeriesEngine:
    def __init__(self, engine):
        self.engine, self.store, self.ai = engine, engine.store, engine.ai

    def handle(self, event, s):
        user, text = event['user_id'], event['text'].strip()
        cmd = normalize_command(text)
        series = self.store.open_series(user)
        if s.get('series_setup') and not series:
            return self.wizard(event, s)
        if not series and not (cmd.startswith('/series') or cmd == 'серия звонков'):
            return False
        replies = []
        if series and series['status'] == 'active':
            self.active(event, s, series)
            return True
        if not series and cmd in ('/series', 'серия звонков', '/series_assign'):
            mode = 'attestation' if cmd == '/series_assign' else 'practice'
            if mode == 'attestation' and user not in self.engine.admin_ids and self.engine.access_role(user) != 'supervisor':
                self.store.commit(event, s, ['Назначение доступно руководителю.'])
                return True
            s['series_setup'] = dict(mode=mode, step='target' if mode == 'attestation' else 'count', data={'custom': []})
            self.store.commit(event, s, [wizard_prompt(s['series_setup'])])
            return True
        if cmd.startswith('/series_report '):
            try:
                value = self.store.get_series(int(text.split()[1]))
                allowed = value and (value['user_id'] == user or user in self.engine.admin_ids
                                     or (value['assigned_by'] == user and self.engine.access_role(user) == 'supervisor'))
                if not allowed:
                    replies = ['Серия не найдена или недоступна.']
                elif value['status'] in ('active', 'assigned'):
                    replies = ['Итог доступен после завершения серии.']
                else:
                    replies = [report(value, self.store.series_results(value['id']))]
            except (ValueError, IndexError):
                replies = ['Формат: /series_report НОМЕР']
        elif cmd == '/series_list':
            replies = ['МОИ СЕРИИ\n' + '\n'.join(f"№{v['id']}: {v['config']['count']} звонков, {v['status']}" for v in self.store.recent_series(user))]
        elif cmd.startswith('/series_assign '):
            if user not in self.engine.admin_ids and self.engine.access_role(user) != 'supervisor':
                replies = ['Назначение доступно руководителю.']
            else:
                try:
                    _, target, raw = text.split(maxsplit=2)
                    target = int(target)
                    if target == user or target <= 0 or self.engine.access_role(target) not in ('akenso', 'supervisor'):
                        raise ValueError('Назначать можно другому сотруднику текущего корпоративного контура.')
                    cfg, calls = build_manifest(json.loads(raw))
                    value = self.create(target, cfg, calls, 'attestation', user)
                    replies = [f"Назначена серия №{value['id']} сотруднику {target}. Он запускает /series_begin.\n"
                               'Состав зафиксирован: ' + value['manifest_hash']]
                except (ValueError, sqlite3.IntegrityError) as exc:
                    replies = [str(exc) if isinstance(exc, ValueError) else 'У сотрудника уже есть незавершённая серия.']
        elif cmd == '/series_begin':
            if not series:
                replies = ['Нет назначенной серии. /series — самостоятельная практика.']
            elif not s.get('employee', {}).get('name'):
                replies = ['Сначала задайте имя через /name, затем /series_begin.']
            elif series['mode'] == 'attestation' and not self.engine.has_unlimited_training(user):
                replies = ['Корпоративный доступ отозван. Обратитесь к руководителю.']
            else:
                series.update(status='active', started_at=self.store.now())
                s = new_call(series, s['employee'])
                s['versions'].update(model=self.ai.model, evaluator=self.ai.eval_model)
                self.store.commit(event, s, intro(series, s), series=series)
                return True
        elif series:
            if cmd.startswith('/series') or cmd in ('серия звонков', '/start'):
                replies = [f"Вам назначена серия №{series['id']}: {series['config']['count']} звонков. /series_begin — начать. Условия уже зафиксированы."]
            else:
                # Profile and existing training remain available before activation.
                return False
        elif cmd == '/series' or cmd == 'серия звонков':
            replies = [HELP]
        elif cmd.startswith('/series ') or cmd.startswith('/series_config '):
            try:
                if cmd.startswith('/series_config '):
                    raw = json.loads(text.split(maxsplit=1)[1])
                else:
                    raw = dict(count=int(text.split()[1]), product=s['fields']['product'], customer=s['fields']['customer'])
                cfg, calls = build_manifest(raw)
                if not self.engine.has_unlimited_training(user) and self.store.paid_remaining(user) < cfg['count']:
                    raise ValueError(f"Для серии нужно {cfg['count']} оплаченных попыток или корпоративный доступ.")
                value = self.create(user, cfg, calls, 'practice')
                replies = [f"Подготовлена серия №{value['id']}: {cfg['count']} звонков. /series_begin — начать.\n"
                           'Без паузы, подсказок и пересдачи. Условия и порядок уже зафиксированы.']
            except (ValueError, IndexError, sqlite3.IntegrityError) as exc:
                replies = [str(exc) if isinstance(exc, ValueError) else 'Серия уже назначена или формат команды неверен.']
        else:
            replies = [HELP]
        self.store.commit(event, s, replies)
        return True

    def wizard(self, event, s):
        draft = s['series_setup']
        text, user = event['text'].strip(), event['user_id']
        cmd = normalize_command(text)
        data, step = draft['data'], draft['step']
        if cmd in ('/cancel', 'отмена'):
            s.pop('series_setup')
            self.store.commit(event, s, ['Настройка серии отменена.'])
            return True
        try:
            if draft['mode'] == 'attestation' and user not in self.engine.admin_ids and self.engine.access_role(user) != 'supervisor':
                raise ValueError('Назначение доступно руководителю.')
            if step == 'target':
                target = int(text)
                if target == user or target <= 0 or self.engine.access_role(target) not in ('akenso', 'supervisor'):
                    raise ValueError('Укажите ID другого сотрудника с корпоративным доступом.')
                draft['target'] = target
                draft['step'] = 'count'
            elif step == 'count':
                count = int(text)
                if count not in (10, 20, 30):
                    raise ValueError('Выберите 10, 20 или 30.')
                data[step] = count
            elif step in ('product', 'customer'):
                if not 2 <= len(text) <= 300:
                    raise ValueError('Введите от 2 до 300 символов.')
                data[step] = text
            elif step == 'goals':
                ids = [int(v.strip()) for v in text.split(',')]
                if not ids or any(not 1 <= i <= len(GOALS) for i in ids):
                    raise ValueError('Выберите номера целей 1–6 через запятую.')
                data[step] = list(dict.fromkeys(list(GOALS)[i-1] for i in ids))
            elif step == 'objections':
                if cmd == 'все возражения':
                    data[step] = {key: 1 for key in OBJECTIONS}
                else:
                    weights = {}
                    for token in text.split(','):
                        pair = token.strip().split(':')
                        index, weight = int(pair[0]), int(pair[1]) if len(pair) == 2 else 1
                        if not 1 <= index <= 14 or not 1 <= weight <= 100 or len(pair) > 2:
                            raise ValueError('Номера 1–14, частоты 1–100. Пример: 1:2, 5:1.')
                        weights[list(OBJECTIONS)[index-1]] = weight
                    data[step] = weights
            elif step == 'interested_percent':
                percent = int(text)
                if not 5 <= percent <= 80:
                    raise ValueError('Введите процент 5–80.')
                data[step] = percent
            elif step == 'difficulty':
                value = {'легкая': 'easy', 'средняя': 'medium', 'высокая': 'hard', '1': 'easy', '2': 'medium', '3': 'hard'}.get(cmd)
                if not value:
                    raise ValueError('Выберите лёгкая, средняя или высокая.')
                data[step] = value
            elif step == 'custom':
                if cmd != 'готово':
                    if len(data['custom']) >= 14:
                        raise ValueError('Добавлено 14 ситуаций. Нажмите «Готово».')
                    label, objection, raw_causes, overcome, weight = [v.strip() for v in text.split('|')]
                    if normalize_command(overcome) not in ('можно', 'нельзя'):
                        raise ValueError('Преодолимость: можно или нельзя.')
                    item = dict(label=label, objection=objection, causes=[v.strip() for v in raw_causes.split('/')], overcome=normalize_command(overcome) == 'можно')
                    weight = int(weight)
                    candidate = copy.deepcopy(data)
                    candidate['custom'].append(item)
                    candidate['objections'][f"custom_{len(data['custom'])}"] = weight
                    config(candidate)
                    data.update(candidate)
                    self.store.commit(event, s, ['Ситуация добавлена. Можно добавить ещё или нажать «Готово».'])
                    return True
            elif step == 'confirm':
                expected = 'назначить серию' if draft['mode'] == 'attestation' else 'подготовить серию'
                if cmd != expected:
                    raise ValueError('Подтвердите кнопкой или /cancel — отменить настройку.')
                cfg, calls = build_manifest(data)
                target = draft.get('target', user)
                if not self.engine.has_unlimited_training(target) and self.store.paid_remaining(target) < cfg['count']:
                    raise ValueError(f"Для серии нужно {cfg['count']} оплаченных попыток или корпоративный доступ.")
                value = self.create(target, cfg, calls, draft['mode'], user if draft['mode'] == 'attestation' else None)
                s.pop('series_setup')
                self.store.commit(event, s, [f"Серия №{value['id']} зафиксирована: {cfg['count']} звонков. "
                    + (f'Сотрудник {target} запускает /series_begin.' if draft['mode'] == 'attestation' else '/series_begin — начать.')])
                return True
            if step not in ('target', 'confirm'):
                draft['step'] = STEPS[STEPS.index(step)+1]
            self.store.commit(event, s, [wizard_prompt(draft)])
        except (ValueError, sqlite3.IntegrityError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else 'У сотрудника уже есть открытая серия.'
            if any(v in message for v in ('invalid literal', 'not enough values', 'too many values')):
                message = 'Ответ не распознан. Используйте формат, указанный в вопросе.'
            self.store.commit(event, s, ['Проверьте ответ: ' + message])
        return True

    def create(self, user, cfg, calls, mode, actor=None):
        return self.store.create_series(user, dict(config=cfg, manifest=calls,
            manifest_hash=manifest_hash(calls), mode=mode, index=0, interruptions=[],
            created_at=self.store.now(), version=VERSION,
            credits_required=0 if self.engine.has_unlimited_training(user) else cfg['count']), actor)

    def finish(self, event, s, series, reason, interrupted=False):
        self.store.checkpoint_series_call(series, s, reason)
        s['phase'] = 'completed'
        if reason == 'technical_abort':
            s['report_status'] = 'technical_partial'
            s['report_data'] = None
        elif any(m['role'] == 'user' for m in s['history']):
            self.engine.make_report(s, event)
        else:
            s['report_status'] = 'technical_partial'
            s['report_data'] = None
        result = dict(status='interrupted' if interrupted else 'completed', session=s,
                      assessment_status=s['report_status'], reason=reason, outcome='unavailable',
                      goal_achieved=False, outcome_status='unavailable', outcome_evidence=[])
        if s['report_status'] == 'verified' and s['report_data']['simulation_valid']:
            try:
                outcome = self.ai.series_outcome(s, series['manifest'][series['index']]['goal'])
                result.update(outcome=outcome['outcome'], goal_achieved=outcome['goal_achieved'],
                              outcome_status='verified', outcome_evidence=outcome['evidence'],
                              outcome_reason=outcome.get('reason', ''))
            except Exception:
                series['interruptions'].append(dict(kind='technical_outcome', call=s['series_index']))
        elif any(m['role'] == 'user' for m in s['history']) and reason != 'technical_abort':
            series['interruptions'].append(dict(kind='technical_assessment', call=s['series_index']))
        elif reason != 'technical_abort':
            result.update(outcome='no_agreement', outcome_status='verified', assessment_status='no_speech')
        if series['index'] == len(series['manifest']) - 1 or reason in ('employee_abort', 'technical_abort'):
            series['status'] = 'aborted' if reason in ('employee_abort', 'technical_abort') else 'completed'
            series['completed_at'] = self.store.now()
            s['series_active'] = False
            budget = vars(self.ai).get('budget')
            if budget:
                series['usage'] = budget.summary(series['id'])
            saved = [r for r in self.store.series_results(series['id'])
                     if r['session']['series_index'] != s['series_index']]
            self.store.commit(event, s, [report(series, saved + [result])],
                              series=series, call_result=result)
        else:
            self.store.commit(event, s, ['Результат звонка сохранён. Следующий звонок: /series_next. Разбор будет в конце серии.'],
                              series=series, call_result=result)

    def active(self, event, s, series):
        text, user = event['text'].strip(), event['user_id']
        cmd = normalize_command(text)
        if manifest_hash(series['manifest']) != series['manifest_hash']:
            raise ValueError('Fixed series manifest was altered')
        if s.get('series_id') != series['id']:
            raise ValueError('Series session mismatch; manual recovery required')
        if series['mode'] == 'attestation' and not self.engine.has_unlimited_training(user):
            self.store.commit(event, s, ['Корпоративный доступ отозван. Серия сохранена; обратитесь к руководителю.'], series=series)
            return
        if s['phase'] == 'active' and s['state']['close'] != 'continue':
            self.finish(event, s, series, 'natural_end')
            return
        failed = self.store.failed(user)
        if cmd == '/support':
            import os
            contact = os.getenv('COMMERCIAL_CONTACT', '').strip() or 'обратитесь к владельцу бота'
            self.store.commit(event, s, [f"Поддержка: {contact}. Укажите серию №{series['id']}. Сценарий сохранён."], series=series)
            return
        if cmd in ('/retry', 'повторить обработку'):
            if failed and failed['attempts'] < 2:
                return self.active(self.store.retry_event(event, failed), s, series)
            self.store.commit(event, s, ['Нет доступной повторной обработки. Сценарий сохранён; обратитесь в поддержку.'], series=series)
            return
        if cmd == '/series_abort':
            reason = 'technical_abort' if failed else 'employee_abort'
            series['interruptions'].append(dict(kind=reason, call=s['series_index'], at=self.store.now()))
            if s['phase'] == 'completed':
                series.update(status='aborted', completed_at=self.store.now())
                s['series_active'] = False
                self.store.commit(event, s, [f"Серия прервана. Итог: /series_report {series['id']}"], series=series)
            else:
                event['discard_failed'] = True
                self.finish(event, s, series, reason, True)
            return
        if cmd in ('/series_next', 'следующий звонок') and s['phase'] == 'completed':
            delivered = s.get('next_call_delivered_at')
            uncertain = series.pop('timing_uncertain', False)
            gap = max(0, (event.get('received_at') or time.time()) - delivered) if delivered and not uncertain else None
            series.setdefault('between_call_times', []).append(dict(call=s['series_index'], seconds=gap))
            if gap is not None and gap > series['config']['response_gap_seconds']:
                series['interruptions'].append(dict(kind='unexplained_between_call_gap', call=s['series_index'], seconds=gap))
            series['index'] += 1
            s = new_call(series, s['employee'])
            self.store.commit(event, s, intro(series, s), series=series)
            return
        if cmd in ('/start', '/series_begin'):
            series['interruptions'].append(dict(kind='employee_reentry', call=s['series_index'], at=self.store.now()))
            self.store.commit(event, s, ['Продолжается тот же звонок.' if s['phase'] == 'active' else 'Результат сохранён. /series_next — следующий звонок.'], series=series)
            return
        if cmd.startswith('/') and not is_finish_command(text) or cmd in ('новая тренировка', 'посмотреть разбор', 'скачать результат', 'показать скрытый сценарий', 'обновить разбор', 'к академии', 'база знаний', 'аттестация', 'мой прогресс', 'пропустить эту реплику'):
            series['interruptions'].append(dict(kind='employee_navigation', call=s['series_index'], at=self.store.now()))
            self.store.commit(event, s, ['Серия продолжается. Подсказки, пауза, разбор и пересдача недоступны. /series_abort — завершить с прерыванием.'], series=series)
            return
        if failed and not event.get('retry_of'):
            self.store.commit(event, s, ['Технический сбой. /retry повторит обработку той же реплики; сценарий сохранён.'], series=series)
            return
        if is_finish_command(text) or cmd == 'завершить звонок':
            if s['phase'] == 'active':
                self.finish(event, s, series, 'employee_finish')
            else:
                self.store.commit(event, s, ['Этот звонок уже сохранён. /series_next — следующий звонок.'], series=series)
            return
        if s['phase'] != 'active':
            self.store.commit(event, s, ['Звонок завершён. /series_next — следующий звонок.'], series=series)
            return
        received = event.get('received_at') or time.time()
        delivered = s.pop('last_delivered_at', None)
        uncertain = series.pop('timing_uncertain', False)
        seconds = max(0, received - delivered) if delivered is not None and not uncertain else None
        s.setdefault('response_times', []).append(dict(event_id=event['id'], received_at=received,
            seconds=seconds, status='measured' if seconds is not None else 'technical_or_delivery_unconfirmed'))
        if seconds is not None and seconds > series['config']['response_gap_seconds']:
            series['interruptions'].append(dict(kind='unexplained_gap', call=s['series_index'], seconds=seconds, event_id=event['id']))
        answer, state, plan = self.ai.turn(s, text)
        s['history'] += [dict(role='user', content=text), dict(role='assistant', content=answer)]
        s['state'] = state
        s.setdefault('turn_events', []).append(plan)
        s['last_prompt_at'] = time.time()
        if state['close'] != 'continue' or state['turns'] >= self.engine.max_turns:
            # Conversation is persisted first; evaluation never loses a completed call.
            self.store.commit(event, s, [answer], series=series)
            self.finish(event, s, series, 'natural_end')
        else:
            self.store.commit(event, s, [answer], series=series)
