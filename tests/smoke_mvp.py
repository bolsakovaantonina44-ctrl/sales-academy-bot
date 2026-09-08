"""Live model/audio test in an isolated database. No Telegram sends or production writes."""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openai import OpenAI
from academy.ai import AI
from academy.engine import Engine, deliver
from academy.store import Store
from academy.diagnostics import log_failure
from bot import worker
from smoke_evaluation import main as evaluation_smoke


def main():
    model=os.getenv('OPENAI_MODEL','gpt-5.6-luna')
    with tempfile.TemporaryDirectory() as temp, OpenAI(api_key=os.environ['OPENAI_API_KEY'],timeout=90,max_retries=1) as client:
        ai=AI(client,model,os.getenv('OPENAI_TRANSCRIBE_MODEL','gpt-4o-mini-transcribe'),os.getenv('OPENAI_EVAL_MODEL',model))
        store=Store(Path(temp)/'smoke.sqlite3'); engine=Engine(store,ai);seq=0
        def send(text):
            nonlocal seq
            seq+=1;store.enqueue('text-'+str(seq),9001,9001,'text',text)
            event=store.claim()
            try:engine.handle(event)
            except Exception as exc:
                log_failure(event,store.current(9001),'live_smoke',exc);raise
            replies=[];deliver(store,9001,lambda c,t:replies.append(t))
            print('SMOKE_TEXT '+json.dumps(replies,ensure_ascii=False),flush=True)
            return store.current(9001)
        send('/start')
        s=send('Продаю услугу бухгалтерского сопровождения небольшим компаниям. Разговариваю с собственником, это первый холодный звонок. Цель — согласовать короткую встречу для обсуждения задач бухгалтерии.')
        if s['phase']!='ready':raise RuntimeError('Custom setup failed')
        s=send('Начать тренировку'); identity=json.dumps(s['card'],sort_keys=True);scenario=s['scenario_id'];product=s['fields']['product']
        for text in ('Как к вам обращаться?','Какая у вас должность?',
                     'Какие задачи по бухгалтерии вам сейчас приходится решать лично?',
                     'Что для вас важнее при выборе подрядчика?',
                     'Давайте пока отложим условия оплаты. Как сейчас устроена передача документов?'):
            s=send(text)
            if json.dumps(s['card'],sort_keys=True)!=identity or s['scenario_id']!=scenario or s['fields']['product']!=product:
                raise RuntimeError('Scenario changed')
        raw_audio={}
        for i,text in enumerate(('Представьтесь, пожалуйста.','Какой следующий шаг вам был бы удобен?')):
            path=Path(temp)/f'voice-{i}.ogg'
            with client.audio.speech.with_streaming_response.create(model='gpt-4o-mini-tts',voice='coral',input=text,response_format='opus') as audio:
                audio.stream_to_file(path)
            raw_audio[str(i)]=path.read_bytes()
        transport=SimpleNamespace(send_chat_action=lambda *a:None,
                    get_file=lambda ident:SimpleNamespace(file_path=ident),download_file=lambda ident:raw_audio[ident])
        stop=threading.Event();thread=threading.Thread(target=worker,args=(store,engine,ai,transport,stop,lambda c,t:print('SMOKE_VOICE_REPLY '+t,flush=True)),daemon=True)
        thread.start()
        try:
            for i in range(2):
                key='voice-'+str(i);store.enqueue(key,9001,9001,'voice',str(i));deadline=time.monotonic()+150
                while time.monotonic()<deadline:
                    with store.db() as db:row=db.execute('SELECT status,kind,text FROM inbox WHERE event_key=?',(key,)).fetchone()
                    if row['status'] in ('done','failed'):break
                    time.sleep(.1)
                if row['status']!='done' or row['kind']!='text':raise RuntimeError('Voice pipeline failed')
                print('SMOKE_TRANSCRIPT '+row['text'],flush=True)
        finally:stop.set();thread.join(5)
        s=send('Завершить тренировку')
        if s['phase']!='completed' or s.get('report_status')!='verified':raise RuntimeError('Verified report unavailable')
        if Store(store.path).current(9001)!=s or Store(store.path).attempts(9001)!=1:raise RuntimeError('Persistence failed')
        print('SMOKE_MVP_PASS',flush=True)
    evaluation_smoke()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        log_failure({'id':'smoke'},{'id':'isolated'},'live_smoke',exc)
        raise SystemExit(1)
