"""Live check of the series outcome judge against human-labelled reference dialogues.

Each case in golden_goal_cases.json is a synthetic dialogue with the verdict a person
agreed on (goal achieved or not). The admin command /golden_check runs them through the
configured model and reports agreement, so a prompt or criterion change can be checked
on real model behaviour before it is trusted. Costs a few AI calls per case.
"""
import json
import threading
import time
from pathlib import Path

from .domain import EvaluationError

CASES_PATH = Path(__file__).with_name('golden_goal_cases.json')
RUNNING = threading.Lock()


def load_cases():
    cases = json.loads(CASES_PATH.read_text(encoding='utf-8'))
    for case in cases:
        if (not isinstance(case.get('id'), str) or case.get('goal') is None
                or not isinstance(case.get('expected'), bool) or not case.get('dialogue')):
            raise ValueError(f"Invalid golden case: {case.get('id')}")
    return cases


def run_case(ai, case):
    session = {'history': [{'role': role, 'content': text} for role, text in case['dialogue']]}
    try:
        result = ai.series_outcome(session, case['goal'])
    except EvaluationError as exc:
        # The judge or its independent review refused a positive verdict: production then
        # records no achieved goal, so that is the effective answer.
        return dict(id=case['id'], expected=case['expected'], got=False, error=f'review: {exc}'[:120])
    except Exception as exc:  # network, budget, malformed response
        return dict(id=case['id'], expected=case['expected'], got=None, error=type(exc).__name__)
    return dict(id=case['id'], expected=case['expected'], got=bool(result['goal_achieved']),
                reason=str(result.get('reason') or '')[:200])


def run_all(ai, cases=None):
    cases = cases if cases is not None else load_cases()
    return [run_case(ai, case) for case in cases]


def format_report(results, seconds=None):
    checked = [r for r in results if r['got'] is not None]
    matched = [r for r in checked if r['got'] == r['expected']]
    lines = ['ПРОВЕРКА ОЦЕНКИ ЦЕЛЕЙ (эталонные диалоги)', '',
             f'Совпало с эталоном: {len(matched)} из {len(checked)}' + (f' ({len(matched) / len(checked):.0%})' if checked else '')]
    failed = [r for r in results if r['got'] is None]
    if failed:
        lines.append(f'Не удалось проверить (сбой ИИ или лимит): {len(failed)}')
    if seconds is not None:
        lines.append(f'Время: {seconds:.0f} сек.')
    wrong = [r for r in checked if r['got'] != r['expected']]
    if wrong:
        lines += ['', 'РАСХОЖДЕНИЯ']
        for r in wrong:
            want = 'засчитать' if r['expected'] else 'не засчитывать'
            lines.append(f"• {r['id']}: нужно {want}, ИИ решил иначе. {r.get('reason') or r.get('error') or ''}".strip())
    for r in failed:
        lines.append(f"• {r['id']}: не проверен ({r.get('error')})")
    lines += ['', 'Планка: не меньше 9 из 10 совпадений. Эталоны лежат в academy/golden_goal_cases.json.']
    return '\n'.join(lines)


def start(engine, user):
    """Run in the background and queue the report; returns the immediate reply."""
    if not RUNNING.acquire(blocking=False):
        return 'Проверка уже идёт. Результат придёт сообщением.'
    try:
        cases = load_cases()
    except Exception as exc:
        RUNNING.release()
        return f'Эталоны не загрузились: {type(exc).__name__}.'

    def work():
        began = time.monotonic()
        try:
            budget = vars(engine.ai).get('budget')
            if budget:
                with budget.scope(user, None):
                    results = run_all(engine.ai, cases)
            else:
                results = run_all(engine.ai, cases)
            body = format_report(results, time.monotonic() - began)
        except Exception as exc:
            body = f'Проверка оценки остановилась с ошибкой: {type(exc).__name__}.'
        finally:
            RUNNING.release()
        with engine.store.db() as db:
            db.execute('INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)', (int(user), int(user), body))

    threading.Thread(target=work, name='golden-check', daemon=True).start()
    return (f'Запустила проверку оценки на {len(cases)} эталонных диалогах. '
            'Это займёт 1–3 минуты и несколько рублей; результат придёт отдельным сообщением.')
