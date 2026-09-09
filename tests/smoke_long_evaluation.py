"""Live long-session regression. Synthetic fixture by default; --session-json replays a real saved session.
No Telegram sends and no writes to the source session/database. Never commit personal dialogue fixtures.
"""
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from academy.ai import AI
from academy.domain import session_empty, SKILLS, check_evaluation
from academy.scenarios import template


def long_session():
    s=session_empty();t=template('2');s['card']=t['card'];s['id']=0
    s['fields'].update({k:t[k] for k in ('product','customer','goal')})
    pairs=[
        ('Добрый день. Анна, поставки отделочных материалов. Удобно буквально пару минут?', 'Коротко, пожалуйста.'),
        ('Мы занимаемся комплексной поставкой. С кем могу обсудить условия?', 'Снабжение ведёт сотрудник, но сбои разбираю я.'),
        ('Какие сбои требуют вашего участия?', 'Материал не приходит к нужному этапу, приходится разбираться.'),
        ('Как вы узнаёте о таких ситуациях?', 'Мне звонят с объекта.'),
        ('Правильно понимаю, вмешиваться приходится, когда проблема уже случилась?', 'Да, обычно так.'),
        ('Как сейчас согласуют график?', 'Снабженец общается с поставщиками и прорабом.'),
        ('Кто собирает общую картину?', 'Каждый смотрит свою часть.'),
        ('И вы в итоге связываете всё сами?', 'Когда срыв, да.'),
        ('Мы можем предложить разные материалы и ассортимент.', 'Пришлите презентацию.'),
        ('Пришлю. Что из нашей информации будет для вас полезнее?', 'Не ассортимент, а кто отвечает за поставку.'),
        ('То есть важнее ответственность за согласованный график?', 'Да, но обещают это все.'),
        ('Какие подтверждения помогли бы проверить это на небольшой задаче?', 'Понятный порядок работы по конкретному объекту.'),
        ('Можно сначала разобрать одну поставку и границы ответственности без обещания заказа?', 'Сначала хочу понять, сколько это займёт времени.'),
        ('Предлагаю коротко собрать вводные со снабженцем и подготовить вопросы. Я не могу сейчас обещать сроки поставки без проверки.', 'Это разумно, но я не хочу ещё одну длинную презентацию.'),
        ('Без презентации: только ваша задача, график, кто за что отвечает. Какой результат вам был бы полезен?', 'Чтобы после обсуждения было ясно, что проверять дальше.'),
        ('Тогда оформим список открытых вопросов и ответственных за проверку, верно?', 'Такой результат понятен.'),
        ('Есть ли другие ограничения?', 'Нельзя обещать наличие без конкретной заявки.'),
        ('Согласна. Наличие подтверждаем по заявке отдельно, сейчас обсуждаем процесс.', 'Хорошо.'),
        ('Кого лучше привлечь со стороны компании?', 'Сотрудника снабжения.'),
        ('Можете дать его номер, чтобы я направила предложение?', 'Без его согласия номер не передам. Лучше перешлю ваши данные.'),
        ('Поняла, чужой номер не нужен. Направлю кратко цель и вопросы, сможете передать ему?', 'Передам, если будет актуально — подключусь.'),
        ('Предлагаю после этого обсудить один объект за двадцать минут. Как вам такой формат?', 'Возможно, но сначала посмотрим информацию.'),
        ('Тогда сегодня направлю вопросы и напомню о себе завтра, чтобы уточнить актуальность.', 'Буду на связи.'),
        ('Спасибо за разговор.', 'До свидания.'),
    ]
    s['history']=[dict(role='assistant',content='Да, слушаю.')]
    for manager,client in pairs:s['history'].extend([dict(role='user',content=manager),dict(role='assistant',content=client)])
    return s


def main(session=None):
    from openai import OpenAI
    s=session or long_session()
    model=os.getenv('OPENAI_MODEL','gpt-5.6-luna')
    with OpenAI(api_key=os.environ['OPENAI_API_KEY'],timeout=90,max_retries=1) as client:
        try:
            data=AI(client,model,'unused',os.getenv('OPENAI_EVAL_MODEL',model)).evaluate(s)
        except Exception:
            # Synthetic fixture can be logged; private replay diagnostics stay in caller-provided file only.
            if session is None: print('LONG_REVIEW_DIAGNOSTICS '+json.dumps(s.get('evaluation_diagnostics'),ensure_ascii=False),flush=True)
            raise
    check_evaluation(data,s)
    if len(data['skills'])!=8:raise RuntimeError('Long report omitted skills')
    if session is None and (data['next_step_status']!='proposed' or data['outcome']>=2):
        raise RuntimeError('Long report confuses proposed followup with agreement')
    print('SMOKE_LONG_EVALUATION_PASS turns='+str(sum(m['role']=='user' for m in s['history'])),flush=True)
    return data


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--session-json');args=parser.parse_args()
    main(json.loads(Path(args.session_json).read_text()) if args.session_json else None)
