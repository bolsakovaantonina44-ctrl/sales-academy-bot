import os
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
6. Если пользователь пишет "завершить тренировку",
   прекрати роль клиента и дай оценку разговора по 100-балльной шкале.
"""

@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(
        message.chat.id,
        "Привет! Я тренажёр Академии продаж.\n\n"
        "Напиши, какую ситуацию хочешь отработать, и начнём переговоры."
    )

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        response = client.responses.create(
            model="gpt-5.6-luna",
            instructions=SYSTEM_PROMPT,
            input=message.text
        )

        bot.send_message(message.chat.id, response.output_text)

    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка: {e}")

bot.infinity_polling()


