import os
import tempfile
import telebot
from openai import OpenAI

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(api_key=OPENAI_API_KEY)


SYSTEM_PROMPT = """
Ты — тренажёр переговоров Академии продаж.

Твоя задача — имитировать реального клиента и помогать менеджеру тренироваться в продажах.

Правила:

1. Не подсказывай менеджеру правильные ответы во время переговоров.
2. Отвечай как реальный клиент.
3. Задавай вопросы, сомневайся и выдвигай возражения.
4. Следи, выяснил ли менеджер потребность клиента.
5. Следи, зафиксировал ли менеджер следующий шаг.
6. Не выходи из роли клиента, пока пользователь сам не завершит тренировку.

Если пользователь пишет или говорит:
"завершить тренировку"

прекрати роль клиента и дай структурированный итог тренировки.

Формат итогового отчёта:

РЕЗУЛЬТАТ ТРЕНИРОВКИ

Итоговый балл: X/100

Уровень готовности:
- готов к реальному разговору
- частично готов
- пока не готов

Цель разговора:
- достигнута
- достигнута частично
- не достигнута

Далее дай разбор по пунктам:

1. Что получилось хорошо
2. Какие ошибки были допущены
3. Какие важные вопросы менеджер не задал
4. Насколько хорошо была выявлена потребность клиента
5. Насколько внимательно менеджер слушал клиента
6. Насколько хорошо менеджер вёл диалог
7. Как менеджер работал с возражениями
8. Был ли зафиксирован конкретный следующий шаг
9. Что менеджер мог сказать или сделать лучше
10. Три конкретных рекомендации для следующей тренировки

Оценивай строго, объективно и профессионально.

Не завышай оценку из вежливости.

Высокую оценку можно поставить только если менеджер:
- выяснил ситуацию клиента;
- задал достаточное количество вопросов;
- выявил реальную потребность;
- не начал презентацию слишком рано;
- аргументировал предложение с точки зрения потребностей клиента;
- корректно отработал возражения;
- управлял разговором;
- договорился о конкретном следующем шаге.

Во время самой тренировки не анализируй менеджера вслух и не сообщай ему баллы.
Анализ и оценку давай только после команды "завершить тренировку".
"""


def get_ai_response(user_text):
    response = client.responses.create(
        model="gpt-5.6-luna",
        instructions=SYSTEM_PROMPT,
        input=user_text
    )
    return response.output_text


@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(
        message.chat.id,
        "Привет! Я тренажёр переговоров Академии продаж.\n\n"
        "Здесь ты можешь отработать реальный разговор с клиентом: "
        "холодный звонок, встречу, работу с возражениями, закупщиком, директором и другие ситуации.\n\n"
        "Во время тренировки я буду играть роль клиента и не буду подсказывать правильные ответы. "
        "Я могу сомневаться, задавать неудобные вопросы и выдвигать возражения — как в реальных переговорах.\n\n"
        "Чтобы начать, напиши или отправь голосом, какую ситуацию ты хочешь отработать.\n\n"
        "Когда захочешь закончить, напиши или скажи: «Завершить тренировку».\n\n"
        "После этого я разберу разговор, поставлю оценку по 100-балльной шкале, "
        "покажу сильные стороны, ошибки и что нужно улучшить."
    )


@bot.message_handler(content_types=["voice"])
def handle_voice(message):
    temp_path = None

    try:
        bot.send_chat_action(message.chat.id, "typing")

        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        with tempfile.NamedTemporaryFile(
            suffix=".ogg",
            delete=False
        ) as temp_file:
            temp_file.write(downloaded_file)
            temp_path = temp_file.name

        with open(temp_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=audio_file
            )

        user_text = transcription.text

        answer = get_ai_response(user_text)

        bot.send_message(message.chat.id, answer)

    except Exception as e:
        print(f"Voice error: {e}")
        bot.send_message(
            message.chat.id,
            "Не получилось обработать голосовое сообщение. Попробуй ещё раз."
        )

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@bot.message_handler(content_types=["text"])
def handle_text(message):
    try:
        bot.send_chat_action(message.chat.id, "typing")

        answer = get_ai_response(message.text)

        bot.send_message(message.chat.id, answer)

    except Exception as e:
        print(f"Text error: {e}")
        bot.send_message(
            message.chat.id,
            "Произошла ошибка. Попробуй отправить сообщение ещё раз."
        )


bot.infinity_polling()
