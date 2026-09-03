
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

Твоя задача — играть роль клиента и тренировать менеджера по продажам.

Правила:
1. Не подсказывай менеджеру правильные ответы во время переговоров.
2. Отвечай как реальный клиент.
3. Задавай вопросы, сомневайся и выдвигай возражения.
4. Следи, выяснил ли менеджер потребность клиента.
5. Следи, зафиксировал ли менеджер следующий шаг.
6. Если пользователь пишет "завершить тренировку", прекрати роль клиента и дай структурированный итог:

- Итоговый балл: X/100
- Достигнута ли цель разговора
- Что получилось хорошо
- Какие ошибки были допущены
- Какие вопросы менеджер не задал
- Насколько хорошо была выявлена потребность клиента
- Как менеджер работал с возражениями
- Был ли зафиксирован следующий шаг
- 3 конкретные рекомендации, что улучшить
- Что стоит потренировать следующим

Оценивай строго и объективно. Не завышай балл.
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
        "Когда захочешь закончить, напиши: «Завершить тренировку».\n\n"
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
        ) as temp_audio:
            temp_audio.write(downloaded_file)
            temp_path = temp_audio.name

        with open(temp_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=audio_file
            )

        user_text = transcription.text

        answer = get_ai_response(user_text)

        bot.send_message(message.chat.id, answer)

    except Exception as e:
        bot.send_message(
            message.chat.id,
            f"Не удалось обработать голосовое сообщение: {e}"
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
        bot.send_message(message.chat.id, f"Ошибка: {e}")


bot.infinity_polling()
