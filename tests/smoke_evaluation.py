"""Explicit live smoke test using synthetic dialogue only; never sends Telegram messages."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openai import OpenAI
from academy.ai import AI
from academy.domain import session_empty, render_report
from academy.scenarios import template


class DiagnosticAI(AI):
    def request(self, name, instructions, payload, schema, *args, **kwargs):
        result = super().request(name, instructions, payload, schema, *args, **kwargs)
        if name == 'evaluation_review':
            # This script contains synthetic fixtures only, never production conversations.
            print('SMOKE_REVIEW ' + json.dumps({'review': result, 'report': payload['report']}, ensure_ascii=False), flush=True)
        return result


def main():
    s = session_empty(); t = template('2')
    s['id'] = 0
    s['fields'].update({k: t[k] for k in ('product', 'customer', 'goal')})
    s['card'] = t['card']
    dialogue = [
        ('assistant', 'У меня мало времени. Что вы предлагаете?'),
        ('user', 'Добрый день. Мы поставляем отделочные материалы. Кто у вас отвечает за снабжение?'),
        ('assistant', 'Этим занимается сотрудник снабжения. Сам подключаюсь к срочным закупкам.'),
        ('user', 'Могу прислать информацию о поставках. Дадите его контакт?'),
        ('assistant', 'Номер без согласия не передам. Лучше перешлю ему ваши данные. Если будет актуально, подключусь к встрече.'),
        ('user', 'Хорошо, направлю информацию и напомню о себе завтра.'),
        ('assistant', 'Передам информацию сотруднику, а дальше посмотрим по актуальности.'),
    ]
    s['history'] = [dict(role=role, content=text) for role, text in dialogue]
    model = os.getenv('OPENAI_MODEL', 'gpt-5.6-luna')
    with OpenAI(api_key=os.environ['OPENAI_API_KEY'], timeout=90, max_retries=1) as client:
        data = DiagnosticAI(client, model, 'unused', os.getenv('OPENAI_EVAL_MODEL', model)).evaluate(s)
    if data['next_step_status'] != 'proposed':
        raise RuntimeError('Smoke: tentative followup was not recognized')
    if 'ПЛАН ДЛЯ РУКОВОДИТЕЛЯ' not in render_report(data, s):
        raise RuntimeError('Smoke: manager plan missing')
    print('SMOKE_EVALUATION_PASS ' + json.dumps({k: data[k] for k in
          ('next_step_status', 'next_step', 'strengths', 'recommendations')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('SMOKE_EVALUATION_FAILED kind=' + type(exc).__name__, flush=True)
        raise SystemExit(1)
