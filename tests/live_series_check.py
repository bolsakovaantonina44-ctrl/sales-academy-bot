"""Cost-bounded real OpenAI checks in an isolated DB; no Telegram/network sends.

Requires OPENAI_API_KEY, OPENAI_MODEL, AI_DAILY_USD, AI_PRICES_JSON.
Optional --voice-file and --voice-seconds exercise real transcription on each call.
This script has NOT run unless its PASS output is recorded in the acceptance log.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openai import OpenAI
from academy.ai import AI
from academy.engine import Engine, deliver
from academy.store import Store
from academy.usage import UsageBudget
from bot import worker


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--counts', nargs='+', type=int, default=[10, 20, 30], choices=[10, 20, 30])
    parser.add_argument('--voice-file')
    parser.add_argument('--voice-seconds', type=int, default=180)
    args = parser.parse_args()
    for key in ('OPENAI_API_KEY', 'OPENAI_MODEL', 'AI_DAILY_USD', 'AI_PRICES_JSON'):
        if not os.getenv(key):
            parser.error(key + ' must be configured')
    if float(os.environ['AI_DAILY_USD']) <= 0 or not 1 <= args.voice_seconds <= 180:
        parser.error('Positive spending budget and voice duration 1–180 required')
    raw = Path(args.voice_file).read_bytes() if args.voice_file else None
    if raw is not None and len(raw) > 10*1024*1024:
        parser.error('Voice file exceeds 10 MB')
    with tempfile.TemporaryDirectory() as temp, OpenAI(timeout=90, max_retries=0) as client:
        store = Store(Path(temp) / 'isolated.sqlite3')
        ai = AI(client, os.environ['OPENAI_MODEL'], os.getenv('OPENAI_TRANSCRIBE_MODEL', 'gpt-4o-mini-transcribe'),
                os.getenv('OPENAI_EVAL_MODEL', os.environ['OPENAI_MODEL']))
        ai.budget = UsageBudget(store)
        engine = Engine(store, ai, admin_ids=[9001])
        seq = 0
        def send(text):
            nonlocal seq
            seq += 1
            store.enqueue(str(seq), 9001, 9001, 'text', text)
            engine.handle(store.claim())
            deliver(store, 9001, lambda c, t: None)

        send('/start')
        send('Изолированный Тестовый Менеджер')
        for count in args.counts:
            send('/series_config ' + json.dumps(dict(count=count, product='Бухгалтерское сопровождение',
                customer='Собственник небольшой компании', goals=['meeting', 'next_step']), ensure_ascii=False))
            send('/series_begin')
            series = store.open_series(9001)
            for index in range(count):
                if raw:
                    seq += 1
                    store.enqueue(str(seq), 9001, 9001, 'voice', 'test-voice')
                    with store.db() as db:
                        db.execute('UPDATE inbox SET voice_seconds=? WHERE event_key=?', (args.voice_seconds, str(seq)))
                    stop = threading.Event()
                    transport = SimpleNamespace(send_chat_action=lambda *a: None,
                        get_file=lambda *a: SimpleNamespace(file_path='voice.ogg'), download_file=lambda *a: raw)
                    runner = threading.Thread(target=worker, args=(store, engine, ai, transport, stop, lambda *a: None))
                    runner.start()
                    # No blind long sleep; event completion is inspected at short intervals.
                    for _ in range(2400):
                        with store.db() as db:
                            status = db.execute('SELECT status FROM inbox WHERE event_key=?', (str(seq),)).fetchone()[0]
                        if status in ('done', 'failed'):
                            break
                        stop.wait(.1)
                    stop.set()
                    runner.join(timeout=100)
                    if runner.is_alive() or status != 'done':
                        raise RuntimeError('Voice processing failed or timed out')
                for line in ['Здравствуйте. Мы предлагаем бухгалтерское сопровождение. Можно уточнить вашу текущую задачу?',
                             'Как сейчас решаете эту задачу и что важно сохранить?',
                             'Что вызывает сомнение и с чем сравниваете вариант?',
                             'Предлагаю обсудить выявленную задачу на короткой встрече завтра в десять. Подходит?']:
                    if store.current(9001)['phase'] == 'completed':
                        break
                    send(line)
                if store.current(9001)['phase'] != 'completed':
                    send('Завершить звонок')
                saved = store.series_results(series['id'])
                if (len(saved) != index+1 or saved[-1]['assessment_status'] != 'verified'
                        or not saved[-1]['session']['report_data']['simulation_valid']
                        or saved[-1]['outcome_status'] != 'verified'):
                    raise RuntimeError(f'Unverified call: series={series["id"]}, ordinal={index+1}')
                print(json.dumps(dict(result='CALL_SAVED', count=count, ordinal=index+1)), flush=True)
                if index < count-1:
                    send('/series_next')
            send(f'/series_report {series["id"]}')
            print(json.dumps(dict(result='PASS', count=count, saved=count, voice=bool(raw),
                                  usage=ai.budget.summary(series['id'])), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
