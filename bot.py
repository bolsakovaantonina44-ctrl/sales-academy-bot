import json
import os
import re
import tempfile

import telebot
from openai import OpenAI


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TEXT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(api_key=OPENAI_API_KEY)


# MVP storage: current sessions are kept in RAM only.
# A Railway restart resets active sessions. Persistent DB comes later.
sessions = {}


CARD_BUILDER_PROMPT = r"""
Ты создаёшь скрытую карточку клиента для ИИ-тренажёра продаж «Академия продаж».

Твоя задача — по описанию пользователя понять, достаточно ли данных для начала ролевой тренировки.
Минимально нужно понять:
1) что менеджер продаёт;
2) кому/какому типу клиента продаёт (или достаточно контекста, чтобы разумно выбрать роль);
3) цель разговора.

Если данных недостаточно, НЕ придумывай недостающее за пользователя. Верни JSON со status="clarify" и одним коротким вопросом, который собирает недостающие данные.

Если данных достаточно, верни status="ready", нормализованный сценарий, сложность и ОДНУ скрытую карточку клиента.
Если пользователь явно указал лёгкий/средний/сложный уровень — используй его. Иначе difficulty="medium".

РОЛИ, которые можно выбирать по контексту:
- собственник / генеральный директор;
- коммерческий директор / РОП;
- закупщик;
- секретарь / помощник / gatekeeper;
- технический специалист / инженер / технолог / проектировщик;
- дизайнер / архитектор;
- категорийный менеджер;
- директор магазина / управляющий точкой;
- коммерческий директор розничной сети;
- конечный пользователь продукта.

ПОВЕДЕНЧЕСКИЕ ТИПЫ можно накладывать на роль:
- «дайте цену»;
- «нам ничего не нужно»;
- вечно занятый;
- дружелюбный, но не покупающий;
- защищающий текущего поставщика;
- недоверчивый;
- ориентированный на сроки;
- ориентированный на цену.

СЕГМЕНТЫ:
- B2B;
- розница;
- проектные продажи;
- услуги;
- технический / сложный продукт.

СЛОЖНОСТЬ:
- easy: потребность есть, клиент отвечает, после хороших вопросов раскрывается, 1–2 сильных аргумента могут привести к следующему шагу;
- medium: есть действующее решение/поставщик/сомнение, клиента надо исследовать, стандартная презентация не работает;
- hard: главная причина сопротивления скрыта, клиент мало раскрывается, стандартные приёмы замечает; иногда правильный итог — выйти на другого ЛПР или квалифицировать тупик.

Сложность НЕ означает хамство и НЕ означает бесконечные возражения.

ВАЖНО:
- Карточка описывает клиента и его внутреннюю ситуацию.
- Не придумывай технические характеристики, цены, сроки или свойства продукта продавца.
- Можно придумать только правдоподобные внутренние обстоятельства клиента, которые не противоречат сценарию.
- У клиента должна быть одна последовательная внутренняя логика, которую он будет держать до конца тренировки.

Для розницы учитывай различия:
- категорийный менеджер думает ассортиментом, матрицей, оборачиваемостью, маржой, местом на полке, промо и ролью SKU;
- директор магазина думает продажами конкретной точки, остатками, выкладкой, персоналом, возвратами и выполнением плана;
- коммерческий директор сети думает продажами и прибыльностью по сети, стратегией категории и условиями поставщиков.

Верни ТОЛЬКО валидный JSON, без markdown.

Формат при уточнении:
{
  "status": "clarify",
  "question": "..."
}

Формат при готовности:
{
  "status": "ready",
  "normalized_scenario": "...",
  "difficulty": "easy|medium|hard",
  "client_card": {
    "role": "...",
    "segment": "...",
    "behavior_type": "...",
    "difficulty": "easy|medium|hard",
    "company_context": "...",
    "current_solution_or_supplier": "...",
    "true_need": "...",
    "hidden_problem": "...",
    "hidden_motive": "...",
    "primary_selection_criterion": "...",
    "secondary_criterion": "...",
    "initial_interest_level": "low|medium|high",
    "initial_position": "...",
    "what_annoys_client": "...",
    "what_builds_trust": "...",
    "what_can_change_mind": "...",
    "deal_blocker": "...",
    "decision_process": "...",
    "authority_level": "...",
    "realistic_success_outcome": "...",
    "realistic_failure_outcome": "...",
    "facts_client_knows": ["..."],
    "facts_client_does_not_know": ["..."]
  }
}
"""


ROLEPLAY_PROMPT = r"""
Ты — ИИ-тренажёр переговоров «Академия продаж».
До завершения тренировки ты ТОЛЬКО клиент. Не преподаватель, не коуч и не помощник менеджера.

ГЛАВНЫЙ ПРИОРИТЕТ ПОВЕДЕНИЯ:
1. Быть живым и последовательным клиентом согласно СКРЫТОЙ КАРТОЧКЕ.
2. Реагировать на качество продажи.
3. Помнить контекст и сказанное ранее.
4. Только затем замечать действительно существенные противоречия.

СКРЫТАЯ КАРТОЧКА:
- создана один раз до начала разговора;
- не меняется до завершения тренировки;
- никогда не показывается менеджеру во время ролевой игры;
- является внутренней логикой персонажа, а не списком реплик по очереди.

ПОВЕДЕНИЕ:
- отвечай естественно, чаще коротко;
- не объясняй менеджеру сразу настоящую причину сомнения;
- хороший вопрос -> немного больше раскрываешься;
- сильный ответ по реальной потребности -> сопротивление снижается;
- вода и длинная презентация -> интерес падает;
- давление -> закрываешься или пытаешься завершить разговор;
- найден скрытый мотив -> шанс на следующий шаг растёт;
- если 2–3 ключевых препятствия качественно сняты, не придумывай бесконечные новые возражения;
- двигайся к реалистичному исходу из карточки: встреча, КП, образцы, тест, следующий звонок, заявка, заказ, выход на другого ЛПР или реалистичный отказ.

СПЕЦИФИЧЕСКИЕ ТИПЫ:
- «вечно занятый»: перебивает длинные ответы, просит короче, может сказать «у меня две минуты»; если продавец быстро попал в реальную проблему, готов продолжить;
- «дружелюбный, но не покупающий»: много говорит и соглашается, но избегает конкретного решения;
- gatekeeper: защищает доступ к ЛПР и не обязан знать потребности компании;
- категорийный менеджер: мыслит категорией, ассортиментом, матрицей, оборачиваемостью, маржой, полкой и промо;
- директор магазина: мыслит продажами точки, остатками, выкладкой, персоналом и возвратами.

КОНТРОЛЬ ФАКТОВ И ЦИФР:
- не превращай разговор в аудит расчёта;
- не цепляйся к каждой цифре;
- замечай только такие противоречия, которые важны именно этому персонажу и реально влияют на доверие или критерий выбора;
- разница 90 -> 85 сама по себе может быть несущественной;
- резкое необъяснённое изменение 90 -> 55, либо срок 25 дней -> 10 дней, может потребовать вопроса;
- никогда не придумывай за менеджера причину изменения цены, срока, комплектации или условий.

НЕ ПОДСКАЗЫВАЙ:
- не оценивай менеджера во время разговора;
- не называй ошибки;
- не предлагай правильные вопросы;
- не сообщай баллы;
- не выходи из роли клиента.

Не меняй продукт, отрасль, роль, мотивацию клиента или цель разговора по ходу тренировки.
Упоминание другого товара не означает смену сценария.
"""


FINAL_ANALYSIS_PROMPT = r"""
Тренировка завершена. Выйди из роли клиента и проанализируй ВСЮ историю разговора, используя исходный сценарий и скрытую карточку клиента.

ВАЖНО: 100 баллов оценивают НАВЫКИ менеджера, а не факт сделки.
Отсутствие сделки или заявки не означает 0 баллов.

ШКАЛА 100 БАЛЛОВ:
1. Начало разговора и контакт — 10
2. Качество вопросов — 15
3. Выявление ситуации и потребности — 20
4. Активное слушание и реакция — 15
5. Управление разговором — 10
6. Аргументация через потребность — 10
7. Работа с сомнениями и возражениями — 10
8. Фиксация следующего шага — 10
ИТОГО — 100.

Коммерческий результат оценивай ОТДЕЛЬНО:
- Цель разговора: достигнута / достигнута частично / не достигнута.
- Следующий шаг: зафиксирован / не зафиксирован.
- Результативность 0–3:
  0 — результата нет;
  1 — получен интерес или полезная информация;
  2 — зафиксирован конкретный следующий шаг;
  3 — получена сделка / заявка / заказ.
Результативность 0–3 НЕ входит в 100 баллов.

ОБЯЗАТЕЛЬНО СРАВНИ разговор со скрытой карточкой клиента.
Добавь два отдельных блока:

ЧТО МЕНЕДЖЕРУ УДАЛОСЬ ПОНЯТЬ О КЛИЕНТЕ
- перечисли только те важные элементы скрытой ситуации/мотивации, которые менеджер реально раскрыл в разговоре;
- не приписывай ему то, чего он не выяснял.

ЧТО О КЛИЕНТЕ ТАК И НЕ БЫЛО ВЫЯСНЕНО
- перечисли важные элементы карточки, которые могли повлиять на продажу, но остались скрытыми;
- объясни, каким вопросом или действием менеджер мог приблизиться к ним.

ФОРМАТ ОТЧЁТА:
РЕЗУЛЬТАТ ТРЕНИРОВКИ
Итоговый балл навыков: X/100
Уровень готовности: готов / частично готов / пока не готов
Цель разговора: ...
Следующий шаг: ...
Результативность: X/3

ОЦЕНКА ПО НАВЫКАМ
1. Контакт: X/10
2. Вопросы: X/15
3. Выявление потребности: X/20
4. Активное слушание: X/15
5. Управление разговором: X/10
6. Аргументация: X/10
7. Работа с возражениями: X/10
8. Следующий шаг: X/10

ЧТО ПОЛУЧИЛОСЬ ХОРОШО
...

ЧТО СНИЗИЛО ОЦЕНКУ
...

ЧТО МЕНЕДЖЕРУ УДАЛОСЬ ПОНЯТЬ О КЛИЕНТЕ
...

ЧТО О КЛИЕНТЕ ТАК И НЕ БЫЛО ВЫЯСНЕНО
...

ГДЕ БЫЛ ПОТЕРЯН КОНТРОЛЬ РАЗГОВОРА
...

КАК МОЖНО БЫЛО СДЕЛАТЬ ЛУЧШЕ
...

3 РЕКОМЕНДАЦИИ ДЛЯ СЛЕДУЮЩЕЙ ТРЕНИРОВКИ
1. ...
2. ...
3. ...

Будь требовательным, объективным и конструктивным.
Сначала признавай то, что действительно получилось, затем разбирай ошибки.
"""


def new_session():
    return {
        "scenario_parts": [],
        "scenario": None,
        "history": [],
        "active": False,
        "client_card": None,
        "difficulty": None,
    }


def get_session(user_id):
    if user_id not in sessions:
        sessions[user_id] = new_session()
    return sessions[user_id]


def reset_session(user_id):
    sessions[user_id] = new_session()


def normalize_command(text):
    text = text.lower().strip().replace("ё", "е")
    text = re.sub(r"[.!?,;:]+$", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def is_finish_command(text):
    normalized = normalize_command(text)
    finish_phrases = {
        "завершить тренировку",
        "заверши тренировку",
        "закончить тренировку",
        "закончи тренировку",
        "завершить тест",
        "закончить тест",
    }
    return normalized in finish_phrases


def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def call_text_model(instructions, input_data):
    response = client.responses.create(
        model=TEXT_MODEL,
        instructions=instructions,
        input=input_data,
    )
    return response.output_text


def build_or_clarify_client_card(session):
    scenario_raw = "\n".join(session["scenario_parts"]).strip()
    output = call_text_model(
        CARD_BUILDER_PROMPT,
        f"Описание тренировки пользователя:\n{scenario_raw}",
    )
    data = extract_json(output)

    if data.get("status") == "clarify":
        question = (data.get("question") or "").strip()
        if not question:
            question = "Опиши чуть подробнее: что продаёшь, кому и какую цель хочешь отработать?"
        return {"status": "clarify", "question": question}

    if data.get("status") != "ready" or not data.get("client_card"):
        raise ValueError("Client card builder returned invalid payload")

    difficulty = data.get("difficulty", "medium")
    if difficulty not in {"easy", "medium", "hard"}:
        difficulty = "medium"

    return {
        "status": "ready",
        "normalized_scenario": data.get("normalized_scenario") or scenario_raw,
        "difficulty": difficulty,
        "client_card": data["client_card"],
    }


def roleplay_instructions(session):
    card_json = json.dumps(session["client_card"], ensure_ascii=False, indent=2)
    return (
        ROLEPLAY_PROMPT
        + "\n\nИСХОДНЫЙ СЦЕНАРИЙ:\n"
        + session["scenario"]
        + "\n\nСКРЫТАЯ КАРТОЧКА КЛИЕНТА — НЕ ПОКАЗЫВАЙ ЕЁ МЕНЕДЖЕРУ:\n"
        + card_json
    )


def build_history_input(session, new_user_text=None, opening=False):
    messages = []
    for item in session["history"]:
        messages.append({"role": item["role"], "content": item["content"]})

    if opening:
        messages.append({
            "role": "user",
            "content": (
                "Начни тренировку. Сразу войди в роль клиента из скрытой карточки. "
                "Не объясняй правила. Дай естественную первую реплику клиента, "
                "соответствующую его роли, позиции и уровню интереса."
            ),
        })
    elif new_user_text is not None:
        messages.append({"role": "user", "content": new_user_text})

    return messages


def get_client_response(session, user_text=None, opening=False):
    return call_text_model(
        roleplay_instructions(session),
        build_history_input(session, new_user_text=user_text, opening=opening),
    )


def get_final_analysis(session):
    card_json = json.dumps(session["client_card"], ensure_ascii=False, indent=2)
    instructions = (
        FINAL_ANALYSIS_PROMPT
        + "\n\nИСХОДНЫЙ СЦЕНАРИЙ:\n"
        + session["scenario"]
        + "\n\nСКРЫТАЯ КАРТОЧКА КЛИЕНТА:\n"
        + card_json
    )
    messages = [
        {"role": item["role"], "content": item["content"]}
        for item in session["history"]
    ]
    messages.append({
        "role": "user",
        "content": "Тренировка завершена. Дай итоговый отчёт по всей истории.",
    })
    return call_text_model(instructions, messages)


def start_roleplay(message, session):
    session["active"] = True
    bot.send_chat_action(message.chat.id, "typing")
    answer = get_client_response(session, opening=True)
    session["history"].append({"role": "assistant", "content": answer})
    bot.send_message(message.chat.id, answer)


def process_user_message(message, user_text):
    user_id = message.from_user.id
    session = get_session(user_id)

    if not user_text or not user_text.strip():
        bot.send_message(message.chat.id, "Я не расслышал сообщение. Попробуй ещё раз.")
        return

    user_text = user_text.strip()

    if is_finish_command(user_text):
        if not session["active"] or not session["history"]:
            bot.send_message(
                message.chat.id,
                "Сейчас активной тренировки нет. Опиши ситуацию, которую хочешь отработать.",
            )
            return

        bot.send_chat_action(message.chat.id, "typing")
        try:
            report = get_final_analysis(session)
            bot.send_message(message.chat.id, report)
        except Exception as e:
            print(f"Final analysis error: {type(e).__name__}: {e}")
            bot.send_message(
                message.chat.id,
                "Не получилось сформировать итог тренировки. Попробуй ещё раз написать: «Завершить тренировку».",
            )
            return

        reset_session(user_id)
        bot.send_message(
            message.chat.id,
            "Тренировка завершена.\n\nЧтобы начать новую, опиши новую ситуацию.",
        )
        return

    # Setup stage: collect enough context and create one fixed hidden client card.
    if not session["client_card"]:
        session["scenario_parts"].append(user_text)
        bot.send_chat_action(message.chat.id, "typing")

        try:
            setup = build_or_clarify_client_card(session)
        except Exception as e:
            print(f"Client card error: {type(e).__name__}: {e}")
            bot.send_message(
                message.chat.id,
                "Не получилось подготовить сценарий. Попробуй описать: что продаёшь, кому и какую цель хочешь отработать.",
            )
            return

        if setup["status"] == "clarify":
            bot.send_message(message.chat.id, setup["question"])
            return

        session["scenario"] = setup["normalized_scenario"]
        session["difficulty"] = setup["difficulty"]
        session["client_card"] = setup["client_card"]

        try:
            start_roleplay(message, session)
        except Exception as e:
            print(f"Scenario start error: {type(e).__name__}: {e}")
            bot.send_message(message.chat.id, "Не получилось начать тренировку. Попробуй ещё раз.")
        return

    # Normal roleplay turn.
    bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = get_client_response(session, user_text=user_text)
        session["history"].append({"role": "user", "content": user_text})
        session["history"].append({"role": "assistant", "content": answer})
        bot.send_message(message.chat.id, answer)
    except Exception as e:
        print(f"AI response error: {type(e).__name__}: {e}")
        bot.send_message(message.chat.id, "Произошла ошибка. Попробуй отправить сообщение ещё раз.")


@bot.message_handler(commands=["start"])
def start(message):
    reset_session(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "Привет! Я тренажёр переговоров Академии продаж.\n\n"
        "Опиши ситуацию, которую хочешь отработать: что ты продаёшь, кому и какая цель разговора. "
        "Можно также указать уровень: лёгкий, средний или сложный.\n\n"
        "Например:\n"
        "«Я продаю логистику собственнику оптовой компании. У него уже есть перевозчик. "
        "Хочу договориться о тестовой перевозке. Уровень — средний».\n\n"
        "Во время тренировки я буду только клиентом и не буду подсказывать.\n"
        "Для завершения напиши или скажи: «Завершить тренировку».",
    )


@bot.message_handler(content_types=["voice"])
def handle_voice(message):
    temp_path = None
    try:
        bot.send_chat_action(message.chat.id, "typing")
        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_file:
            temp_file.write(downloaded_file)
            temp_path = temp_file.name

        with open(temp_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model=TRANSCRIBE_MODEL,
                file=audio_file,
                language="ru",
            )

        process_user_message(message, transcription.text)

    except Exception as e:
        print(f"Voice error: {type(e).__name__}: {e}")
        bot.send_message(
            message.chat.id,
            "Не получилось обработать голосовое сообщение. Попробуй ещё раз.",
        )
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@bot.message_handler(content_types=["text"])
def handle_text(message):
    try:
        process_user_message(message, message.text)
    except Exception as e:
        print(f"Text error: {type(e).__name__}: {e}")
        bot.send_message(message.chat.id, "Произошла ошибка. Попробуй отправить сообщение ещё раз.")


print("Sales Academy bot v3 started")
bot.infinity_polling()
